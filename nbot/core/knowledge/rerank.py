"""知识库检索增强：rerank（模型重排）与 MMR（多样性去重）。

rerank 模型不绑定任何厂商，配置沿用现有模型配置体系（``data/web/ai_models.json``）：
新增一条 ``purpose="rerank"`` 的模型条目即可，字段与其它用途一致
（``api_key`` / ``base_url`` / ``model`` / ``proxy_url`` / ``enabled``），额外可选：

- ``rerank_path``：接口路径，默认按 ``base_url`` 推断（已含 ``/rerank`` 则直用，否则拼 ``/rerank``）
- ``rerank_format``：``documents``（默认，``{"model","query","documents","top_n"}``）
  或 ``texts``（``{"model","query","texts","top_n"}``）
- ``rerank_timeout``：超时秒数，默认 15
- ``rerank_weight``：与向量相似度的混合权重，默认 1.0（纯 rerank 分数排序）
- ``rerank_extra_body``：额外并入请求体的字段（dict），给特殊服务端用

响应兼容三种常见形状（Jina / SiliconFlow / Cohere / TEI 等）::

    {"results": [{"index": 0, "relevance_score": 0.9}, ...]}
    {"data":    [{"index": 0, "score": 0.9}, ...]}
    [{"index": 0, "score": 0.9}, ...]

未配置 rerank 模型、请求失败或响应无法解析时一律返回 ``None``，检索退回纯向量相似度，
行为与接入前一致。

MMR（Maximal Marginal Relevance）用于去掉高度重复的片段：先按相关度排序，
再在"相关度 - 与已选片段的冗余度"之间取平衡，避免同一段话的不同分块占满 top_k。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections.abc import Callable
from typing import Any

_log = logging.getLogger(__name__)

DEFAULT_RERANK_TIMEOUT = 15.0
DEFAULT_MMR_LAMBDA = 0.75
NEAR_DUPLICATE_THRESHOLD = 0.92

# 配置读取缓存（秒）：避免每轮检索都读/解密 ai_models.json
_CONFIG_TTL = 30.0
_config_cache_at = 0.0
_config_cache_value: dict[str, Any] | None = None

# rerank 结果缓存（同一 query + 同一批文档直接复用）
_rerank_cache: dict[str, list[tuple[int, float]] | None] = {}
_cache_limit = 128


def _config_loader():
    try:
        from nbot.web.utils import config_loader

        return config_loader
    except Exception:  # pragma: no cover - web 未就绪时（CLI 早启动）没有配置加载器
        return None


def _raw_model_entry(purpose: str) -> dict[str, Any]:
    """读 ai_models.json 里该用途的原始条目（拿到 rerank_path 这类非通用字段）。"""
    loader = _config_loader()
    if loader is None:
        return {}
    try:
        models = loader._load_ai_models_from_file()
    except Exception:
        return {}
    for model in models or []:
        if not isinstance(model, dict):
            continue
        if model.get("purpose", "chat") != purpose:
            continue
        if not model.get("enabled", True):
            continue
        return model
    return {}


def load_rerank_config(force: bool = False) -> dict[str, Any] | None:
    """从现有模型配置体系取 rerank 模型配置；未配置返回 None。

    结果按 ``_CONFIG_TTL`` 秒缓存，避免每轮对话都重新读/解密 ``ai_models.json``；
    面板改完模型配置后最多等一个 TTL（或调用 ``reset_rerank_service``）即生效。
    """
    global _config_cache_at, _config_cache_value
    now = time.monotonic()
    if not force and _config_cache_at and now - _config_cache_at < _CONFIG_TTL:
        return _config_cache_value

    value = _read_rerank_config()
    _config_cache_at = now
    _config_cache_value = value
    return value


def _read_rerank_config() -> dict[str, Any] | None:
    loader = _config_loader()
    if loader is None:
        return None
    try:
        config = loader.get_model_config_by_purpose("rerank")
    except Exception as exc:  # pragma: no cover - 配置损坏不应影响检索
        _log.debug("[Rerank] 读取模型配置失败: %s", exc)
        return None
    if not config:
        return None

    raw = _raw_model_entry("rerank")
    merged = dict(config)
    for key in (
        "rerank_path",
        "rerank_format",
        "rerank_timeout",
        "rerank_weight",
        "rerank_extra_body",
        "rerank_headers",
    ):
        if key in raw:
            merged[key] = raw[key]
    if not merged.get("base_url"):
        return None
    return merged


def get_rerank_service(force_reload: bool = False) -> RerankService | None:
    """返回缓存的 rerank 服务（配置缺失返回 None）。"""
    global _rerank_service, _rerank_signature
    config = load_rerank_config()
    if not config:
        _rerank_service = None
        _rerank_signature = ()
        return None
    signature = (
        config.get("base_url", ""),
        config.get("model", ""),
        config.get("api_key", ""),
        config.get("rerank_path", ""),
    )
    if force_reload or _rerank_service is None or signature != _rerank_signature:
        _rerank_service = RerankService.from_config(config)
        _rerank_signature = signature
        if _rerank_service is not None:
            _log.info(
                "[Rerank] 已启用重排模型: model=%s endpoint=%s",
                config.get("model", ""),
                _rerank_service.endpoint,
            )
    return _rerank_service


def reset_rerank_service():
    """清空 rerank 服务与结果缓存（配置变更 / 测试用）。"""
    global _rerank_service, _rerank_signature, _config_cache_at, _config_cache_value
    _rerank_service = None
    _rerank_signature = ()
    _config_cache_at = 0.0
    _config_cache_value = None
    _rerank_cache.clear()


class RerankService:
    """通用 rerank 客户端（HTTP，字段可配，不绑定厂商）。"""

    def __init__(
        self,
        api_key: str = "",
        base_url: str = "",
        model: str = "",
        proxy_url: str = "",
        path: str = "",
        timeout: float = DEFAULT_RERANK_TIMEOUT,
        request_format: str = "documents",
        weight: float = 1.0,
        extra_body: dict[str, Any] | None = None,
        extra_headers: dict[str, str] | None = None,
    ):
        self.api_key = api_key or ""
        self.base_url = (base_url or "").rstrip("/")
        self.model = model or ""
        self.proxy_url = proxy_url or ""
        self.path = path or ""
        self.timeout = float(timeout or DEFAULT_RERANK_TIMEOUT)
        self.request_format = "texts" if str(request_format) == "texts" else "documents"
        self.weight = float(weight if weight is not None else 1.0)
        self.extra_body = dict(extra_body or {})
        self.extra_headers = dict(extra_headers or {})

    # -- 构造 ---------------------------------------------------------------

    @classmethod
    def from_config(cls, config: dict[str, Any] | None = None) -> RerankService | None:
        config = config or load_rerank_config()
        if not config or not config.get("base_url"):
            return None
        try:
            timeout = float(config.get("rerank_timeout") or DEFAULT_RERANK_TIMEOUT)
        except (TypeError, ValueError):
            timeout = DEFAULT_RERANK_TIMEOUT
        try:
            weight = float(config.get("rerank_weight", 1.0))
        except (TypeError, ValueError):
            weight = 1.0
        extra_body = config.get("rerank_extra_body")
        extra_headers = config.get("rerank_headers")
        return cls(
            api_key=str(config.get("api_key") or ""),
            base_url=str(config.get("base_url") or ""),
            model=str(config.get("model") or ""),
            proxy_url=str(config.get("proxy_url") or ""),
            path=str(config.get("rerank_path") or ""),
            timeout=timeout,
            request_format=str(config.get("rerank_format") or "documents"),
            weight=weight,
            extra_body=extra_body if isinstance(extra_body, dict) else None,
            extra_headers=extra_headers if isinstance(extra_headers, dict) else None,
        )

    @property
    def enabled(self) -> bool:
        return bool(self.base_url and self.model)

    @property
    def endpoint(self) -> str:
        if self.path:
            if self.path.startswith("http://") or self.path.startswith("https://"):
                return self.path
            return f"{self.base_url}/{self.path.lstrip('/')}"
        if self.base_url.endswith("/rerank"):
            return self.base_url
        return f"{self.base_url}/rerank"

    # -- 请求 ---------------------------------------------------------------

    def rerank(
        self,
        query: str,
        documents: list[str],
        top_n: int | None = None,
    ) -> list[tuple[int, float]] | None:
        """返回 ``[(原始下标, 重排分数), ...]``，按分数降序；失败返回 None。"""
        if not self.enabled or not query or not documents:
            return None

        cache_key = self._cache_key(query, documents, top_n)
        if cache_key in _rerank_cache:
            cached = _rerank_cache[cache_key]
            return list(cached) if cached else cached

        payload: dict[str, Any] = {"model": self.model, "query": query}
        key = "texts" if self.request_format == "texts" else "documents"
        payload[key] = list(documents)
        payload["top_n"] = int(top_n or len(documents))
        payload.update(self.extra_body)

        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        headers.update(self.extra_headers)

        try:
            import requests

            from nbot.core.model_proxy import model_proxy_request_kwargs

            response = requests.post(
                self.endpoint,
                headers=headers,
                json=payload,
                timeout=self.timeout,
                **model_proxy_request_kwargs(self.proxy_url),
            )
            response.raise_for_status()
            data = response.json()
        except Exception as exc:
            _log.warning("[Rerank] 请求失败，退回向量相似度: %s", exc)
            self._remember(cache_key, None)
            return None

        parsed = self._parse(data, len(documents))
        if parsed is None:
            _log.warning("[Rerank] 响应无法解析，退回向量相似度: %.200s", data)
        self._remember(cache_key, parsed)
        return list(parsed) if parsed else parsed

    def _cache_key(self, query: str, documents: list[str], top_n: int | None) -> str:
        digest = hashlib.md5()
        digest.update(f"{self.endpoint}|{self.model}|{top_n}".encode())
        digest.update(query.encode())
        digest.update(b"\x00")
        digest.update("\x1f".join(documents).encode())
        return digest.hexdigest()

    @staticmethod
    def _remember(key: str, value: list[tuple[int, float]] | None) -> None:
        if len(_rerank_cache) >= _cache_limit:
            for stale in list(_rerank_cache)[: _cache_limit // 4]:
                _rerank_cache.pop(stale, None)
        _rerank_cache[key] = list(value) if value else value

    @staticmethod
    def _parse(data: Any, size: int) -> list[tuple[int, float]] | None:
        items: Any = None
        if isinstance(data, dict):
            for key in ("results", "data", "output", "reranked"):
                value = data.get(key)
                if isinstance(value, list):
                    items = value
                    break
        elif isinstance(data, list):
            items = data
        if not isinstance(items, list) or not items:
            return None

        scored: list[tuple[int, float]] = []
        for position, item in enumerate(items):
            if isinstance(item, (int, float)):
                scored.append((position, float(item)))
                continue
            if not isinstance(item, dict):
                continue
            index = item.get("index", item.get("idx", item.get("document_index")))
            if index is None and "document" in item:
                index = position
            score = None
            for key in ("relevance_score", "score", "relevance", "similarity"):
                if item.get(key) is not None:
                    try:
                        score = float(item[key])
                    except (TypeError, ValueError):
                        score = None
                    if score is not None:
                        break
            if index is None or score is None:
                continue
            try:
                index = int(index)
            except (TypeError, ValueError):
                continue
            if 0 <= index < size:
                scored.append((index, score))

        if not scored:
            return None
        scored.sort(key=lambda pair: pair[1], reverse=True)
        return scored

    def scores_for(self, query: str, documents: list[str]) -> dict[int, float] | None:
        """``{下标: 重排分数}``，失败返回 None。"""
        ranked = self.rerank(query, documents)
        if not ranked:
            return None
        return {index: score for index, score in ranked}


# 服务缓存（模块级，get_rerank_service 使用）
_rerank_service: RerankService | None = None
_rerank_signature: tuple = ()


# ---------------------------------------------------------------------------
# MMR 去重
# ---------------------------------------------------------------------------

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


def token_bag(text: str) -> set[str]:
    """轻量分词：英文/数字按词，中文按字并补相邻二元组（用来算重复度）。"""
    lowered = (text or "").lower()
    words = _WORD_RE.findall(lowered)
    tokens = set(words)
    for first, second in zip(words, words[1:], strict=False):
        if _is_cjk(first) and _is_cjk(second):
            tokens.add(first + second)
    return tokens


def _is_cjk(token: str) -> bool:
    return len(token) == 1 and "\u4e00" <= token <= "\u9fff"


def jaccard(first: set[str], second: set[str]) -> float:
    if not first or not second:
        return 0.0
    union = first | second
    if not union:
        return 0.0
    return len(first & second) / len(union)


def mmr_select(
    items: list[dict[str, Any]],
    top_k: int,
    lambda_param: float = DEFAULT_MMR_LAMBDA,
    text_key: str = "text",
    score_key: str = "score",
    similarity_fn: Callable[[set[str], set[str]], float] | None = None,
) -> list[dict[str, Any]]:
    """按 MMR 从候选里挑 ``top_k`` 条，``items`` 需已按相关度降序。

    ``similarity_fn`` 默认 Jaccard（基于 ``token_bag``），也可传其它文本相似度函数。
    相关度用条目自身的 ``score_key`` 字段，按批次内最大值归一化到 0~1。
    """
    if top_k <= 0 or not items:
        return []
    if len(items) <= top_k:
        return list(items)

    similarity = similarity_fn or jaccard
    bags = [token_bag(str(item.get(text_key) or "")) for item in items]
    raw_scores = [float(item.get(score_key, 0.0) or 0.0) for item in items]
    highest = max(raw_scores) or 1.0
    lowest = min(raw_scores)
    span = (highest - lowest) or 1.0
    relevance = [(score - lowest) / span for score in raw_scores]

    selected: list[int] = []
    remaining = set(range(len(items)))

    while remaining and len(selected) < top_k:
        best_index = -1
        best_value = None
        for index in remaining:
            redundancy = 0.0
            if selected:
                redundancy = max(similarity(bags[index], bags[other]) for other in selected)
            value = lambda_param * relevance[index] - (1.0 - lambda_param) * redundancy
            if best_value is None or value > best_value:
                best_value = value
                best_index = index
        if best_index < 0:
            break
        selected.append(best_index)
        remaining.discard(best_index)

    return [items[index] for index in selected]


def drop_near_duplicates(
    items: list[dict[str, Any]],
    threshold: float = NEAR_DUPLICATE_THRESHOLD,
    text_key: str = "text",
) -> list[dict[str, Any]]:
    """去掉与已保留条目高度重复（Jaccard >= threshold）的候选，保持原顺序。"""
    kept: list[dict[str, Any]] = []
    kept_bags: list[set[str]] = []
    for item in items:
        bag = token_bag(str(item.get(text_key) or ""))
        if any(jaccard(bag, other) >= threshold for other in kept_bags):
            continue
        kept.append(item)
        kept_bags.append(bag)
    return kept


__all__ = [
    "DEFAULT_MMR_LAMBDA",
    "DEFAULT_RERANK_TIMEOUT",
    "NEAR_DUPLICATE_THRESHOLD",
    "RerankService",
    "drop_near_duplicates",
    "get_rerank_service",
    "jaccard",
    "load_rerank_config",
    "mmr_select",
    "reset_rerank_service",
    "token_bag",
]

"""
图片生成服务

封装图片生成 API 调用，支持 OpenAI 兼容 / SiliconFlow / 自定义协议。
供以下场景共用：
  1. 角色立绘生成（routes/personality.py）
  2. 角色对话中按需生图（core/ai_pipeline.py 后处理 _try_send_image）

配置来源（优先级从高到低）：
  1. 调用方传入的 image_gen_config 字典
  2. WebChatServer.active_models_by_purpose['image_generation'] 当前选中的模型
  3. data/web/ai_models.json 中 purpose == "image_generation" 的第一个启用模型
  4. 内置默认值
"""

import base64
import copy
import ipaddress
import itertools
import json
import logging
import os
import queue
import random
import re
import socket
import threading
import time
import uuid
from datetime import datetime
from urllib.parse import urlparse

import requests

from nbot.core.model_proxy import model_proxy_request_kwargs

_log = logging.getLogger(__name__)

# API 超时（秒），生图通常较慢
_API_TIMEOUT = 300

# 默认图片大小
_DEFAULT_SIZE = "1024x1024"

# 内联标签正则：匹配 [send_image: ...] 风格
SEND_IMAGE_TAG_PATTERN = re.compile(
    r"\[send_image\s*:\s*([^\]]+?)\s*\]",
    re.IGNORECASE,
)


def get_image_generation_config() -> dict | None:
    """从 ai_models.json 加载当前选中的图片生成模型配置。

    优先使用 WebChatServer 中 active_models_by_purpose['image_generation']
    标记的"当前选中"模型；找不到时回退到第一个启用的 image_generation 模型。

    Returns:
        配置字典，未找到返回 None。
    """
    try:
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        if server is not None:
            config = get_image_generation_config_from_server(server)
            if config:
                return config
    except Exception as exc:
        _log.debug("[ImageService] 从 server 读取图片生成配置失败，回退: %s", exc)

    try:
        from nbot.web.utils.config_loader import get_model_config_by_purpose

        return get_model_config_by_purpose("image_generation")
    except Exception as exc:
        _log.debug("[ImageService] 加载图片生成配置失败: %s", exc)
        return None


def resolve_image_generation_config(model_id: str = "", server=None) -> dict | None:
    """按模型 ID 解析图片生成配置；model_id 为空时返回当前选中的模型配置。

    Args:
        model_id: ai_models.json / server.ai_models 里的模型 id。
        server: WebChatServer 实例，传入后按 id 精确匹配。
    """
    if not model_id:
        return get_image_generation_config()

    if server is not None:
        for model in getattr(server, "ai_models", []) or []:
            if model.get("id") == model_id and model.get("enabled", True):
                return _build_image_generation_config(model)
        return None

    try:
        from nbot.web.utils.config_loader import _load_ai_models_from_file

        for model in _load_ai_models_from_file() or []:
            if model.get("id") == model_id and model.get("enabled", True):
                return _build_image_generation_config(model)
    except Exception as exc:  # noqa: BLE001 - 兼容旧版 config_loader
        _log.debug("[ImageService] 按 id 解析生图配置失败: %s", exc)
    return None


def get_image_generation_config_from_server(server) -> dict | None:
    """从 WebChatServer 的 active_models_by_purpose 中获取当前选中的图片生成模型配置。

    Args:
        server: WebChatServer 实例。

    Returns:
        标准化后的配置字典，未找到返回 None。
    """
    if server is None:
        return None

    active_id = (getattr(server, "active_models_by_purpose", {}) or {}).get(
        "image_generation"
    )
    if not active_id:
        return None

    for model in getattr(server, "ai_models", []) or []:
        if model.get("id") == active_id and model.get("enabled", True):
            return _build_image_generation_config(model)

    return None


def _build_image_generation_config(model: dict) -> dict:
    """将原始模型字典标准化为图片生成调用所需的配置。"""
    from nbot.web.utils.config_loader import resolve_runtime_api_key

    provider_type = model.get("provider_type", "openai_compatible")
    return {
        "api_key": resolve_runtime_api_key(model.get("api_key", ""), provider_type),
        "base_url": model.get("base_url", ""),
        "proxy_url": model.get("proxy_url", ""),
        "model": model.get("model", ""),
        "provider_type": provider_type,
        "provider": model.get("provider", "custom"),
        "append_base_url_path": model.get("append_base_url_path", True),
        "size": model.get("size", ""),
    }


def is_image_generation_enabled() -> bool:
    """检查是否配置了可用的图片生成模型。"""
    config = get_image_generation_config()
    if not config:
        return False
    return bool(config.get("api_key") and config.get("base_url") and config.get("model"))


def _is_volces_ark_provider(provider_type: str = "", base_url: str = "") -> bool:
    """判断 provider 是否为火山引擎 ark（豆包）图片生成服务。

    火山引擎对图片 size 有像素数下限（≥ 3,686,400，约 1920x1920），
    需要把过小的尺寸自动放大。
    """
    pt = (provider_type or "").strip().lower()
    if pt in ("volces", "ark", "doubao", "volcengine"):
        return True
    url = (base_url or "").lower()
    return "volces.com" in url or "ark.cn-beijing" in url


def _resolve_size(size: str, provider_type: str = "", base_url: str = "") -> str:
    """处理火山引擎 ark 的 size 下限要求（≥ 3,686,400 像素）。

    此前实现误把判断逻辑写在 size 字符串里（"volces" in size.lower()），
    导致永远命中不到——size 只可能是 "1024x1024" 这种格式。
    修复后改为通过 provider_type / base_url 识别火山引擎。
    """
    if not _is_volces_ark_provider(provider_type, base_url):
        return size
    try:
        width, height = size.split("x")
        pixels = int(width) * int(height)
        if pixels < 3686400:
            return "1920x1920"
    except (ValueError, AttributeError):
        pass
    return size


def _image_provider(config: dict) -> str:
    """解析图片生成 provider（优先 provider 字段，回退 provider_type）。"""
    return (config.get("provider") or config.get("provider_type") or "").strip().lower()


def _endpoint(base_url: str, suffix: str, default_url: str, append_base_url_path: bool = True) -> str:
    """解析提供商端点地址（与 Android MultimodalProviderSupport.endpoint 对齐）。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return default_url
    if base.endswith(suffix):
        return base
    if not append_base_url_path:
        return base
    return f"{base}{suffix}"


def _dashscope_endpoint(base_url: str, suffix: str, default_url: str, append_base_url_path: bool = True) -> str:
    """解析 DashScope 端点地址，兼容 compatible-mode 前缀。"""
    normalized = (base_url or "").strip().rstrip("/").replace("/compatible-mode/v1", "/api/v1")
    return _endpoint(normalized, suffix, default_url, append_base_url_path)


def _aspect_ratio(size: str) -> str:
    """将尺寸字符串 (如 1024x1024) 归一化为最接近的宽高比。"""
    try:
        parts = size.lower().split("x", 1)
        width = float(parts[0])
        height = float(parts[1]) if len(parts) > 1 else 0.0
        if width <= 0 or height <= 0:
            return "1:1"
        ratio = width / height
        candidates = [
            (1.0, "1:1"),
            (4.0 / 3.0, "4:3"),
            (3.0 / 4.0, "3:4"),
            (16.0 / 9.0, "16:9"),
            (9.0 / 16.0, "9:16"),
            (3.0 / 2.0, "3:2"),
            (2.0 / 3.0, "2:3"),
        ]
        return min(candidates, key=lambda item: abs(item[0] - ratio))[1]
    except (ValueError, IndexError):
        return "1:1"


def _extract_image_url(result: dict, provider_type: str) -> str | None:
    """从图片生成 API 响应中提取图片 URL 或 base64。

    支持的返回结构：
      - OpenAI 标准：{"data": [{"url": "..."} | {"b64_json": "..."}]}
      - 火山 ark：{"data": [{"url": "..."}]}
      - SiliconFlow：{"images": [{"url": "..."}]}
      - 自定义文本格式：{"choices": [{"message": {"content": "..."}}]}
        其中 content 可能是 markdown 图片、http URL 或 base64 data URI
    """
    data_list = result.get("data") or []
    if data_list:
        item = data_list[0] if isinstance(data_list[0], dict) else {}
        url = item.get("url") or item.get("b64_json")
        if url:
            return url

    choices = result.get("choices") or []
    if choices:
        content = (
            choices[0].get("message", {}).get("content", "")
            if isinstance(choices[0], dict)
            else ""
        )
        if content:
            md_match = re.search(r"!\[.*?\]\((https?://\S+)\)", content)
            if md_match:
                return md_match.group(1)
            content = content.strip()
            if content.startswith("http") or content.startswith("data:"):
                return content
            if content and len(content) > 100:
                return content

    images = result.get("images") or []
    if images and isinstance(images[0], dict):
        return images[0].get("url")

    return None


def call_image_generation(
    prompt: str,
    config: dict,
    *,
    size: str = "",
    extra_keywords: list[str] | None = None,
) -> str | None:
    """调用图片生成 API，返回图片 URL 或 base64 data URI。

    Args:
        prompt: 英文图片描述 prompt。
        config: 模型配置，至少含 api_key/base_url/model/provider_type。
        size: 图片尺寸，如 "1024x1024"，为空使用配置或默认。
        extra_keywords: 附加到 prompt 末尾的关键词列表。

    Returns:
        成功返回图片 URL/base64，失败返回 None。
    """
    if not config:
        _log.warning("[ImageService] 缺少图片生成配置")
        return None

    api_key = config.get("api_key", "")
    full_url = config.get("base_url", "")
    model = config.get("model", "dall-e-3")
    provider_type = config.get("provider_type", "openai_compatible")
    proxy_kwargs = model_proxy_request_kwargs(config.get("proxy_url", ""))
    image_size = size or config.get("size", "") or _DEFAULT_SIZE

    # 原生提供商（Gemini/MiniMax/Qwen/豆包 Seed）在未填写 base_url 时使用默认端点
    image_provider = _image_provider(config)
    native_providers = (
        "gemini", "google", "google_gemini",
        "minimax",
        "qwen", "dashscope", "tongyi",
        "doubao", "seed", "seedream", "volcengine", "ark", "volces",
    )
    if not api_key:
        _log.warning("[ImageService] api_key 未配置")
        return None
    if not full_url and image_provider not in native_providers:
        _log.warning("[ImageService] base_url 未配置")
        return None

    if not prompt or not prompt.strip():
        _log.warning("[ImageService] prompt 为空")
        return None

    image_size = _resolve_size(image_size, provider_type=provider_type, base_url=full_url)
    full_prompt = prompt.strip()
    if extra_keywords:
        full_prompt = f"{full_prompt}. {' '.join(extra_keywords)}"

    try:
        # 新增原生提供商：Gemini / MiniMax / Qwen / 豆包 Seed
        if image_provider in ("gemini", "google", "google_gemini"):
            return _call_gemini_image(config, full_prompt, proxy_kwargs)
        if image_provider == "minimax":
            return _call_minimax_image(config, full_prompt, image_size, proxy_kwargs)
        if image_provider in ("qwen", "dashscope", "tongyi"):
            return _call_qwen_image(config, full_prompt, image_size, proxy_kwargs)
        if image_provider in ("doubao", "seed", "seedream", "volcengine", "ark", "volces"):
            return _call_doubao_image(config, full_prompt, image_size, proxy_kwargs)

        if provider_type in ("openai_compatible", "openai") or "openai" in full_url.lower():
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "prompt": full_prompt,
                "n": 1,
                "size": image_size,
            }
            response = requests.post(
                full_url,
                headers=headers,
                json=payload,
                timeout=_API_TIMEOUT,
                **proxy_kwargs,
            )
            if response.status_code != 200:
                _log.error("[ImageService] API 错误 %d: %s", response.status_code, response.text[:300])
                return None
            result = response.json()
            return _extract_image_url(result, provider_type)

        if provider_type == "siliconflow" or "siliconflow" in full_url.lower():
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": model,
                "prompt": full_prompt,
                "image_size": image_size,
                "batch_size": 1,
            }
            response = requests.post(
                full_url,
                headers=headers,
                json=payload,
                timeout=_API_TIMEOUT,
                **proxy_kwargs,
            )
            if response.status_code != 200:
                _log.error("[ImageService] API 错误 %d: %s", response.status_code, response.text[:300])
                return None
            result = response.json()
            return _extract_image_url(result, provider_type)

        # 兜底：通用 OpenAI 格式
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "prompt": full_prompt,
            "n": 1,
            "size": image_size,
        }
        response = requests.post(
            full_url,
            headers=headers,
            json=payload,
            timeout=_API_TIMEOUT,
            **proxy_kwargs,
        )
        if response.status_code != 200:
            _log.error("[ImageService] API 错误 %d: %s", response.status_code, response.text[:300])
            return None
        result = response.json()
        url_or_b64 = result.get("data", [{}])[0].get("url") or result.get("images", [{}])[0].get("url")
        return url_or_b64
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] 图片生成请求超时")
        return None
    except Exception as exc:
        _log.error("[ImageService] 调用失败: %s", exc, exc_info=True)
        return None


def _call_gemini_image(config: dict, prompt: str, proxy_kwargs: dict) -> str | None:
    """Gemini 原生图片生成（generateContent + responseModalities: IMAGE）。"""
    api_key = config.get("api_key", "")
    model = config.get("model") or "gemini-3.1-flash-image"
    url = _endpoint(
        config.get("base_url", ""),
        f"/models/{model}:generateContent",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        config.get("append_base_url_path", True),
    )
    body = {
        "contents": [{"parts": [{"text": prompt}]}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    headers = {
        "x-goog-api-key": api_key,
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs)
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] Gemini 图片生成请求超时")
        return None
    if response.status_code != 200:
        _log.error("[ImageService] Gemini 图片生成错误 %d: %s", response.status_code, response.text[:300])
        return None
    result = response.json()
    for candidate in result.get("candidates") or []:
        for part in ((candidate.get("content") or {}).get("parts")) or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data") or ""
            if data:
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return f"data:{mime};base64,{data}"
    _log.error("[ImageService] Gemini 图片生成未返回图片数据: %s", response.text[:200])
    return None


def _call_minimax_image(config: dict, prompt: str, size: str, proxy_kwargs: dict) -> str | None:
    """MiniMax 图片生成（/image_generation + image_base64 数组）。"""
    api_key = config.get("api_key", "")
    url = _endpoint(
        config.get("base_url", ""),
        "/image_generation",
        "https://api.minimaxi.com/v1/image_generation",
        config.get("append_base_url_path", True),
    )
    body = {
        "model": config.get("model") or "image-01",
        "prompt": prompt,
        "aspect_ratio": _aspect_ratio(size),
        "response_format": "base64",
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs)
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] MiniMax 图片生成请求超时")
        return None
    if response.status_code != 200:
        _log.error("[ImageService] MiniMax 图片生成错误 %d: %s", response.status_code, response.text[:300])
        return None
    result = response.json()
    data = result.get("data") or {}
    images = data.get("image_base64") or []
    if images and isinstance(images[0], str) and images[0]:
        return f"data:image/png;base64,{images[0]}"
    image_urls = data.get("image_urls") or []
    if image_urls and isinstance(image_urls[0], str) and image_urls[0]:
        return image_urls[0]
    _log.error("[ImageService] MiniMax 图片生成未返回图片数据: %s", response.text[:200])
    return None


def _call_qwen_image(config: dict, prompt: str, size: str, proxy_kwargs: dict) -> str | None:
    """通义万相（Qwen）图片生成：异步提交任务 + 轮询结果。"""
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    submit_url = _dashscope_endpoint(
        config.get("base_url", ""),
        "/services/aigc/text2image/image-synthesis",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/text2image/image-synthesis",
        config.get("append_base_url_path", True),
    )
    body = {
        "model": model,
        "input": {"prompt": prompt},
        "parameters": {
            "size": size.replace("x", "*"),
            "n": 1,
            "watermark": False,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-DashScope-Async": "enable",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(submit_url, headers=headers, json=body, timeout=60, **proxy_kwargs)
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] Qwen 图片生成提交超时")
        return None
    if response.status_code != 200:
        _log.error("[ImageService] Qwen 图片生成提交错误 %d: %s", response.status_code, response.text[:300])
        return None

    output = (response.json().get("output")) or {}
    task_id = output.get("task_id")
    if not task_id:
        _log.error("[ImageService] Qwen 图片生成未返回 task_id: %s", response.text[:200])
        return None

    base = (config.get("base_url") or "").strip().rstrip("/").replace("/compatible-mode/v1", "/api/v1")
    if not base:
        base = "https://dashscope.aliyuncs.com/api/v1"
    task_url = _validated_task_url(output.get("task_url") or "", base, task_id)
    poll_headers = {"Authorization": f"Bearer {api_key}"}

    for _ in range(90):
        time.sleep(2)
        try:
            poll_resp = requests.get(task_url, headers=poll_headers, timeout=60, **proxy_kwargs)
        except requests.exceptions.Timeout:
            continue
        if poll_resp.status_code != 200:
            _log.warning("[ImageService] Qwen 图片任务查询错误 %d", poll_resp.status_code)
            continue
        poll_output = (poll_resp.json().get("output")) or {}
        status = str(poll_output.get("task_status", "")).upper()
        if status == "SUCCEEDED":
            results = poll_output.get("results") or []
            if results and isinstance(results[0], dict) and results[0].get("url"):
                return results[0]["url"]
            _log.error("[ImageService] Qwen 图片任务成功但无结果 URL: %s", poll_resp.text[:200])
            return None
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            _log.error("[ImageService] Qwen 图片任务失败: %s", poll_output.get("message") or poll_resp.text[:200])
            return None
    _log.warning("[ImageService] Qwen 图片任务轮询超时")
    return None


def _call_doubao_image(config: dict, prompt: str, size: str, proxy_kwargs: dict) -> str | None:
    """豆包 Seed 图片生成（火山 ark /images/generations）。"""
    api_key = config.get("api_key", "")
    url = _endpoint(
        config.get("base_url", ""),
        "/images/generations",
        "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        config.get("append_base_url_path", True),
    )
    size_upper = size.upper()
    normalized_size = size_upper if size_upper in ("2K", "4K") else ("2K" if "x" in size else size)
    body = {
        "model": config.get("model", ""),
        "prompt": prompt,
        "size": normalized_size,
        "sequential_image_generation": "disabled",
        "stream": False,
        "response_format": "url",
        "watermark": False,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs)
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] 豆包图片生成请求超时")
        return None
    if response.status_code != 200:
        _log.error("[ImageService] 豆包图片生成错误 %d: %s", response.status_code, response.text[:300])
        return None
    result = response.json()
    url_or_b64 = _extract_image_url(result, "doubao")
    if url_or_b64:
        return url_or_b64
    _log.error("[ImageService] 豆包图片生成未返回图片数据: %s", response.text[:200])
    return None


_MAX_DOWNLOAD_BYTES = 30 * 1024 * 1024


def _download_generated_image(url: str, *, max_bytes: int = _MAX_DOWNLOAD_BYTES) -> bytes | None:
    """下载 provider 返回的结果图：拒私网/本机地址，禁重定向，限长。

    provider 返回的 URL 属于外部数据，未校验就抓取会把内网内容落成公开静态文件，
    因此这里复用 assert_public_http_url（只允许公网 http(s)）。
    """
    try:
        assert_public_http_url(url)
        response = requests.get(url, timeout=120, stream=True, allow_redirects=False)
        try:
            if 300 <= response.status_code < 400:
                _log.error("[ImageService] 结果图地址不允许重定向")
                return None
            if response.status_code != 200:
                _log.error("[ImageService] 下载图片失败 %d", response.status_code)
                return None
            chunks: list[bytes] = []
            total = 0
            for chunk in response.iter_content(64 * 1024):
                if not chunk:
                    continue
                total += len(chunk)
                if total > max_bytes:
                    _log.error("[ImageService] 结果图超过 %d 字节，放弃下载", max_bytes)
                    return None
                chunks.append(chunk)
            return b"".join(chunks)
        finally:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                pass
    except ValueError as exc:
        _log.error("[ImageService] 结果图地址被拒绝: %s", exc)
        return None
    except requests.exceptions.RequestException as exc:
        _log.error("[ImageService] 结果图下载失败: %s", exc)
        return None


def save_image_to_uploads(
    image_url_or_b64: str,
    upload_dir: str,
    *,
    prefix: str = "image",
) -> str | None:
    """将图片（URL 或 base64）下载到本地 uploads 目录，返回公开路径。

    Args:
        image_url_or_b64: API 返回的图片 URL 或 data URI 或裸 base64。
        upload_dir: 绝对路径，如 /path/to/nbot/web/static/uploads/character_images。
        prefix: 文件名前缀。

    Returns:
        相对 URL（如 "/static/uploads/.../xxx.png"），失败返回 None。
    """
    if not image_url_or_b64:
        return None

    try:
        os.makedirs(upload_dir, exist_ok=True)

        filename = f"{prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        filepath = os.path.join(upload_dir, filename)

        if image_url_or_b64.startswith("data:"):
            image_bytes = base64.b64decode(image_url_or_b64.split(",", 1)[1])
            with open(filepath, "wb") as f:
                f.write(image_bytes)
        elif image_url_or_b64.startswith("http"):
            image_bytes = _download_generated_image(image_url_or_b64)
            if image_bytes is None:
                return None
            with open(filepath, "wb") as f:
                f.write(image_bytes)
        else:
            # 尝试当作裸 base64
            try:
                image_bytes = base64.b64decode(image_url_or_b64)
                with open(filepath, "wb") as f:
                    f.write(image_bytes)
            except Exception:
                _log.error("[ImageService] 无法识别的图片数据格式")
                return None

        # 计算相对于 static 的 URL 路径
        upload_dir_norm = os.path.normpath(upload_dir)
        static_root = os.path.normpath(
            os.path.join(os.path.dirname(__file__), "..", "web", "static")
        )
        if upload_dir_norm.startswith(static_root):
            rel = os.path.relpath(upload_dir_norm, static_root)
            return f"/static/{rel.replace(os.sep, '/')}/{filename}"
        # 兜底：仅返回文件名（外层自行包装）
        return filename
    except Exception as exc:
        _log.error("[ImageService] 保存图片失败: %s", exc, exc_info=True)
        return None


# ----------------------------------------------------------------------------
# PromptStack 注入：向角色 system prompt 注册"主动发图"能力说明
# ----------------------------------------------------------------------------

# 注入项 key（与 nbot.character.prompt_stack._DYNAMIC_SECTION_KEYS 保持一致）
IMAGE_CAPABILITY_KEY = "character.image_capability"

# 注入项优先级：紧跟 character.runtime_state(40) 之后
IMAGE_CAPABILITY_PRIORITY = 42

# 注入项内容
IMAGE_CAPABILITY_CONTENT = (
    "\n【主动发图能力】\n"
    "你可以选择性地在回复中插入图片发送给用户。\n"
    "方法：在回复文本任意位置添加 [send_image: <英文画面描述>] 标签。\n"
    "规则：\n"
    "1. 仅在用户明确要求看图，或当前情境非常适合用画面表达时使用。\n"
    "2. prompt 使用英文画面描述（场景、风格、构图），不超过 60 词。\n"
    "3. 标签会从最终展示文本中移除，用户只会看到一张图片。\n"
    "4. 每轮最多 1 个标签，避免打扰用户。\n"
    "5. 切勿滥用——只有真正能增强表达时才发图。\n"
    "示例：用户说「想看你做饭的样子」→ 你可以回复 "
    "\"好呀~ [send_image: anime girl cooking in cozy kitchen, "
    "warm lighting, soft smile, upper body shot]\""
)


def build_image_capability_injection(stack) -> bool:
    """向 PromptStack 注册"主动发图"能力说明。

    满足以下全部条件时注册成功：
      1. settings.json 中 features.image_generation 为 True；
      2. ai_models.json 中已配置 purpose == "image_generation" 的可用模型；
      3. 当前非 agent 会话模式（由调用方在调用前检查）。

    Args:
        stack: nbot.character.prompt_stack.PromptStack 实例。

    Returns:
        注入成功返回 True，跳过返回 False。
    """
    if not is_image_generation_feature_enabled():
        return False
    if not is_image_generation_enabled():
        return False

    stack.add(
        key=IMAGE_CAPABILITY_KEY,
        content=IMAGE_CAPABILITY_CONTENT,
        priority=IMAGE_CAPABILITY_PRIORITY,
        scope="turn",
    )
    return True


# ----------------------------------------------------------------------------
# 内联标签解析：用于从 LLM 回复中提取 [send_image: ...] 触发器
# ----------------------------------------------------------------------------


def extract_send_image_tags(content: str) -> tuple[str, list[str]]:
    """从 LLM 回复文本中提取 [send_image: <prompt>] 标签。

    Args:
        content: LLM 原始回复。

    Returns:
        (清理后的文本, prompt 列表)。所有标签会被从文本中移除，
        返回的列表保持原顺序、去重。
    """
    if not content:
        return content or "", []

    prompts: list[str] = []
    seen = set()

    def _replace(match: re.Match) -> str:
        prompt = match.group(1).strip()
        if prompt and prompt not in seen:
            seen.add(prompt)
            prompts.append(prompt)
        return ""

    cleaned = SEND_IMAGE_TAG_PATTERN.sub(_replace, content)
    # 顺手清理多余空行
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned).strip()
    return cleaned, prompts


def should_send_image_probability(probability: float | None = None) -> bool:
    """按概率判断本轮是否应该主动发送图片（与 sticker 同款随机门控）。"""
    if probability is None:
        probability = get_image_generation_probability()
    if probability <= 0:
        return False
    if probability >= 1:
        return True
    return random.random() < probability


# ----------------------------------------------------------------------------
# settings.json 读写（参照 sticker_service 的实现）
# ----------------------------------------------------------------------------

def _get_settings_path() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    return os.path.join(base_dir, "data", "web", "settings.json")


def _load_image_generation_settings() -> dict:
    """从 settings.json 加载图片生成配置，返回 {enabled, probability, size}。"""
    settings_path = _get_settings_path()
    result = {
        "enabled": False,
        "probability": 0.0,
        "size": _DEFAULT_SIZE,
    }

    try:
        if os.path.exists(settings_path):
            with open(settings_path, encoding="utf-8") as f:
                data = json.loads(f.read())
            features = data.get("features", {}) if isinstance(data, dict) else {}
            if isinstance(features, dict):
                enabled = features.get("image_generation")
                if enabled is not None:
                    result["enabled"] = bool(enabled)
            image_cfg = data.get("image_generation", {}) if isinstance(data, dict) else {}
            if isinstance(image_cfg, dict):
                prob = image_cfg.get("probability")
                if prob is not None:
                    try:
                        result["probability"] = max(0.0, min(1.0, float(prob) / 100.0))
                    except (TypeError, ValueError):
                        pass
                size = image_cfg.get("size")
                if size:
                    result["size"] = str(size)
    except Exception as exc:
        _log.debug("[ImageService] 读取 settings.json 失败: %s", exc)

    return result


def is_image_generation_feature_enabled() -> bool:
    """检查 settings.json 中的 image_generation 功能开关。"""
    return _load_image_generation_settings()["enabled"]


def get_image_generation_probability() -> float:
    """获取 settings.json 配置的发送概率 (0.0-1.0)。"""
    return _load_image_generation_settings()["probability"]


def get_image_generation_size() -> str:
    """获取 settings.json 配置的默认图片尺寸。"""
    return _load_image_generation_settings()["size"]


# ============================================================================
# P2-10 图生图（img2img）+ 生图实验室
#
# 能力：
#   1. img2img：参考图 + 重绘强度 + 风格预设 + 尺寸；
#   2. 多图批量：一次请求生成 1~4 张，支持多参考图；
#   3. 生成历史落盘：data/web/image_history.json（含参数与结果路径）；
#   4. 配额与大小校验：按日限额、参考图数量/体积、输出尺寸范围。
#
# 使用方：
#   - Web 路由 nbot/web/routes/images.py（生成 / 历史 / 删除 / 重试 / 配额）；
#   - Agent 工具 generate_image（由 nbot/services/tools.py 注册，走 generate_image_tool）。
# ============================================================================

# 生成结果落盘目录（相对 nbot/web/static/uploads）
_IMAGE_LAB_DIRNAME = "image_lab"
# 参考图库子目录
_IMAGE_REFERENCE_DIRNAME = "references"
# 历史最多保留条数（超出后丢弃最旧的记录，不删磁盘文件）
_MAX_HISTORY_ENTRIES = 1000
# 单次请求最多生成张数
_MAX_IMAGES_PER_REQUEST = 4
# 单次请求最多参考图张数
_MAX_REFERENCE_IMAGES = 4
# 参考图体积上限（字节，单张；settings.json 的 image_generation.quota 可覆盖）
_MAX_REFERENCE_BYTES = 10 * 1024 * 1024
# 参考图合计体积上限（一次请求所有参考图之和）
_MAX_REFERENCE_TOTAL_BYTES = 20 * 1024 * 1024
# 参考图对外 URL 前缀（只允许该目录下的文件作为本地参考图）
_REFERENCE_URL_PREFIX = "/static/uploads/image_lab/references/"
# 默认重绘强度（0=几乎不动，1=完全重画）
_DEFAULT_STRENGTH = 0.6
# 供外部（路由/工具）引用的公开默认值
DEFAULT_IMAGE_STRENGTH = _DEFAULT_STRENGTH
# 输出尺寸范围
_MIN_IMAGE_SIDE = 256
_MAX_IMAGE_SIDE = 4096
_MAX_IMAGE_PIXELS = 4096 * 4096
# 默认每日配额（0 表示不限）
_DEFAULT_DAILY_LIMIT = 200

# 风格预设：prompt_suffix 会拼到 prompt 末尾，negative_prompt 会并入 avoid 列表
IMAGE_STYLE_PRESETS: dict[str, dict] = {
    "none": {
        "label": "不指定",
        "description": "完全按提示词生成",
        "prompt_suffix": "",
        "negative_prompt": "",
    },
    "anime": {
        "label": "二次元",
        "description": "日系动画，赛璐璐上色、干净线稿",
        "prompt_suffix": "anime illustration, clean line art, cel shading, vibrant colors",
        "negative_prompt": "photo, realistic skin texture, 3d render",
    },
    "realistic": {
        "label": "写实",
        "description": "摄影级真实质感",
        "prompt_suffix": "photorealistic, natural lighting, sharp focus, 85mm lens",
        "negative_prompt": "anime, cartoon, painting, lowres",
    },
    "watercolor": {
        "label": "水彩",
        "description": "柔和透明的水彩晕染",
        "prompt_suffix": "watercolor painting, soft washes, paper texture, translucent colors",
        "negative_prompt": "photo, harsh contrast, digital vector",
    },
    "oil": {
        "label": "油画",
        "description": "厚涂笔触与古典油画质感",
        "prompt_suffix": "oil painting, thick brush strokes, classical composition, canvas texture",
        "negative_prompt": "photo, flat colors, pixel art",
    },
    "ink": {
        "label": "水墨",
        "description": "中国水墨，留白与淡墨",
        "prompt_suffix": "traditional chinese ink wash painting, sumi-e, negative space, rice paper",
        "negative_prompt": "photo, saturated colors, 3d render",
    },
    "cyberpunk": {
        "label": "赛博朋克",
        "description": "霓虹、雨夜、未来都市",
        "prompt_suffix": "cyberpunk aesthetic, neon lights, rainy night city, cinematic",
        "negative_prompt": "daylight, rustic, low detail",
    },
    "pixel": {
        "label": "像素",
        "description": "复古像素游戏美术",
        "prompt_suffix": "pixel art, 16-bit game sprite, limited palette, crisp pixels",
        "negative_prompt": "photo, blurry, smooth gradients",
    },
    "3d": {
        "label": "3D 渲染",
        "description": "Pixar 风格三维渲染",
        "prompt_suffix": "3d render, pixar style, soft studio lighting, subsurface scattering",
        "negative_prompt": "flat illustration, sketch, low poly",
    },
    "storybook": {
        "label": "绘本",
        "description": "儿童绘本插画",
        "prompt_suffix": "children storybook illustration, warm pastel palette, soft outlines",
        "negative_prompt": "photo, dark horror, realistic gore",
    },
}

# 常用尺寸（供前端下拉，实际校验放开到任意合法尺寸）
IMAGE_SIZE_PRESETS: list[dict] = [
    {"value": "512x512", "label": "512×512 · 1:1 小图", "ratio": "1:1"},
    {"value": "768x768", "label": "768×768 · 1:1", "ratio": "1:1"},
    {"value": "1024x1024", "label": "1024×1024 · 1:1", "ratio": "1:1"},
    {"value": "1024x1536", "label": "1024×1536 · 2:3 竖图", "ratio": "2:3"},
    {"value": "1536x1024", "label": "1536×1024 · 3:2 横图", "ratio": "3:2"},
    {"value": "768x1344", "label": "768×1344 · 9:16 竖图", "ratio": "9:16"},
    {"value": "1344x768", "label": "1344×768 · 16:9 横图", "ratio": "16:9"},
    {"value": "1920x1920", "label": "1920×1920 · 1:1 高清", "ratio": "1:1"},
]


def _project_root() -> str:
    """仓库根目录（nbot/services/image_service.py 上溯三层）。"""
    return os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def get_image_lab_upload_dir() -> str:
    """生图实验室结果目录：nbot/web/static/uploads/image_lab。"""
    return os.path.join(_project_root(), "nbot", "web", "static", "uploads", _IMAGE_LAB_DIRNAME)


def get_image_reference_dir() -> str:
    """参考图库目录：nbot/web/static/uploads/image_lab/references。"""
    return os.path.join(get_image_lab_upload_dir(), _IMAGE_REFERENCE_DIRNAME)


def get_image_history_path() -> str:
    """生成历史文件路径，可用 NBOT_IMAGE_HISTORY_PATH 覆盖（测试用）。"""
    override = (os.environ.get("NBOT_IMAGE_HISTORY_PATH") or "").strip()
    if override:
        return override
    return os.path.join(_project_root(), "data", "web", "image_history.json")


def list_image_style_presets() -> list[dict]:
    """返回风格预设列表（供前端/工具描述使用）。"""
    return [{"id": style_id, **preset} for style_id, preset in IMAGE_STYLE_PRESETS.items()]


# ----------------------------------------------------------------------------
# 参数校验
# ----------------------------------------------------------------------------


def normalize_image_size(size: str = "") -> str:
    """校验并归一化输出尺寸，非法时抛 ValueError。"""
    raw = (size or "").strip().lower().replace("×", "x").replace("*", "x").replace(" ", "")
    if not raw:
        return _DEFAULT_SIZE
    match = re.fullmatch(r"(\d{2,5})x(\d{2,5})", raw)
    if not match:
        raise ValueError(f"尺寸格式无效：{size}（应形如 1024x1024）")
    width, height = int(match.group(1)), int(match.group(2))
    if width < _MIN_IMAGE_SIDE or height < _MIN_IMAGE_SIDE:
        raise ValueError(f"尺寸过小：{width}x{height}（最短边不得小于 {_MIN_IMAGE_SIDE}）")
    if width > _MAX_IMAGE_SIDE or height > _MAX_IMAGE_SIDE:
        raise ValueError(f"尺寸过大：{width}x{height}（最长边不得大于 {_MAX_IMAGE_SIDE}）")
    if width * height > _MAX_IMAGE_PIXELS:
        raise ValueError(f"尺寸像素数过大：{width * height}（上限 {_MAX_IMAGE_PIXELS}）")
    return f"{width}x{height}"


def normalize_strength(strength) -> float:
    """归一化重绘强度到 0.0~1.0，容错 0~100 的百分数写法。"""
    try:
        value = float(strength)
    except (TypeError, ValueError):
        return _DEFAULT_STRENGTH
    if value > 1.0 and value <= 100.0:
        value = value / 100.0
    return max(0.0, min(1.0, value))


def normalize_style(style: str = "") -> str:
    """归一化风格标识；预设之外的字符串按自定义风格词处理。"""
    return (style or "").strip() or "none"


def style_prompt_parts(style: str = "") -> tuple[str, str]:
    """返回 (prompt 后缀, 负向词)；未知风格视为自定义风格词。"""
    key = normalize_style(style)
    preset = IMAGE_STYLE_PRESETS.get(key)
    if preset:
        return preset.get("prompt_suffix", ""), preset.get("negative_prompt", "")
    return key, ""


def compose_image_prompt(prompt: str, style: str = "", negative_prompt: str = "") -> str:
    """把风格后缀与负向词拼成最终 prompt。"""
    parts = [(prompt or "").strip()]
    suffix, style_negative = style_prompt_parts(style)
    if suffix:
        parts.append(suffix)
    negatives = [item for item in (style_negative, (negative_prompt or "").strip()) if item]
    if negatives:
        parts.append("avoid: " + ", ".join(negatives))
    return ", ".join(part for part in parts if part)


# ----------------------------------------------------------------------------
# 参考图处理
# ----------------------------------------------------------------------------


def _guess_image_mime(source: str) -> str:
    """按扩展名猜 MIME，默认 image/png。"""
    ext = os.path.splitext((source or "").split("?", 1)[0])[1].lower()
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".gif": "image/gif",
        ".bmp": "image/bmp",
    }.get(ext, "image/png")


def _static_url_to_path(source: str) -> str | None:
    """把 /static/... 形式的 URL 映射回本地文件路径。"""
    if not source.startswith("/static/"):
        return None
    static_root = os.path.join(_project_root(), "nbot", "web", "static")
    rel = source[len("/static/"):]
    return os.path.normpath(os.path.join(static_root, rel.replace("/", os.sep)))


def get_static_root() -> str:
    """静态目录绝对路径。"""
    return os.path.abspath(os.path.join(_project_root(), "nbot", "web", "static"))


def static_relative_path(path: str) -> str:
    """把静态目录下的绝对路径转成对外可用的相对路径。

    对外只暴露相对路径（如 `uploads/image_lab/lab_x.png`），
    静态目录之外的路径一律返回空串，避免把本机盘符路径吐给前端。
    """
    raw = (path or "").strip()
    if not raw:
        return ""
    if not os.path.isabs(raw):
        return raw.replace(os.sep, "/")
    static_root = get_static_root()
    abs_path = os.path.abspath(raw)
    try:
        common = os.path.commonpath([abs_path, static_root])
    except ValueError:
        return ""
    if os.path.normcase(common) != os.path.normcase(static_root):
        return ""
    return os.path.relpath(abs_path, static_root).replace(os.sep, "/")


def _static_abs_path(relative: str) -> str:
    """相对路径 -> 静态目录下的绝对路径（已是绝对路径则原样返回）。"""
    rel = (relative or "").strip()
    if not rel:
        return ""
    if os.path.isabs(rel):
        return rel
    return os.path.normpath(os.path.join(get_static_root(), rel.replace("/", os.sep)))


def _resolve_result_file(result: dict) -> str:
    """内部用：把结果记录解析成绝对路径（兼容历史记录里的绝对路径）。"""
    raw = str(result.get("path") or "").strip()
    if raw:
        return raw if os.path.isabs(raw) else _static_abs_path(raw)
    return _static_url_to_path(str(result.get("url") or "")) or ""


def get_reference_limits() -> dict:
    """参考图数量/体积上限（settings.json 为准，模块常量只做默认值）。"""
    quota = get_image_quota_settings()
    max_bytes = quota["max_reference_bytes"]
    return {
        "max_reference_images": quota["max_reference_images"],
        "max_reference_bytes": max_bytes,
        "max_reference_total_bytes": max(max_bytes, _MAX_REFERENCE_TOTAL_BYTES),
    }


def _is_public_ip(address: str) -> bool:
    """判断地址是否为公网可路由 IP（拒绝内网/回环/链路本地/保留/组播）。"""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def assert_public_http_url(url: str) -> None:
    """校验参考图 URL：只允许 http(s) 公网地址，禁止指向本机/内网。

    Raises:
        ValueError: 协议非 http(s)、主机名为本机/内网、解析失败。
    """
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError("参考图只支持 http/https 地址")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise ValueError("参考图地址缺少主机名")
    if host == "localhost" or host.endswith((".local", ".internal", ".localhost")):
        raise ValueError("参考图地址不允许指向本机")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError) as exc:
        raise ValueError(f"参考图地址无法解析：{host}") from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise ValueError(f"参考图地址无法解析：{host}")
    for address in addresses:
        if not _is_public_ip(address):
            raise ValueError("参考图地址不允许指向内网或保留地址")


def _download_reference(url: str, max_bytes: int) -> bytes:
    """下载参考图：禁跟随重定向，流式读取并按上限截断。"""
    assert_public_http_url(url)
    response = None
    try:
        response = requests.get(url, timeout=60, stream=True, allow_redirects=False)
        status = response.status_code
        if 300 <= status < 400:
            raise ValueError("参考图地址不允许重定向")
        if status != 200:
            raise ValueError(f"参考图下载失败：HTTP {status}")
        chunks: list[bytes] = []
        total = 0
        for chunk in response.iter_content(64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                raise ValueError(f"参考图过大：已超过 {max_bytes} 字节上限")
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        if response is not None:
            try:
                response.close()
            except Exception:  # noqa: BLE001 - 关闭失败不影响结果
                pass


def resolve_reference_path(source: str) -> str:
    """把参考图来源解析为参考图库内的真实文件路径。

    只接受参考图库里的文件名或 `/static/uploads/image_lab/references/<name>`，
    拒绝绝对路径、`..`、软链与越界路径。

    Raises:
        ValueError: 路径非法、越界、是软链或文件不存在。
    """
    raw = (source or "").strip()
    if not raw:
        raise ValueError("参考图为空")
    normalized = raw.replace("\\", "/")
    if ".." in normalized.split("/"):
        raise ValueError("参考图路径非法（包含上级目录）")
    if normalized.startswith(_REFERENCE_URL_PREFIX):
        name = normalized[len(_REFERENCE_URL_PREFIX):]
    elif normalized.startswith(("/static/", "/")):
        raise ValueError("参考图只允许使用参考图库中的图片")
    elif os.path.isabs(raw) or raw.startswith("~") or re.match(r"^[A-Za-z]:", raw):
        raise ValueError("参考图只接受已上传到参考图库的图片，不接受本机绝对路径")
    else:
        name = normalized
    if not name or "/" in name or name in (".", ".."):
        raise ValueError("参考图路径非法")

    root = os.path.realpath(get_image_reference_dir())
    candidate = os.path.join(root, name)
    if os.path.islink(candidate):
        raise ValueError("参考图不允许是软链")
    real = os.path.realpath(candidate)
    try:
        inside = os.path.commonpath([real, root]) == root
    except ValueError:
        inside = False
    if not inside:
        raise ValueError("参考图路径越界")
    if not os.path.isfile(real):
        raise ValueError(f"参考图不存在：{source}")
    return real


def load_reference_bytes(source: str) -> bytes:
    """读取参考图字节（http(s) URL / data URI / 参考图库内文件名或 URL）。

    安全约束：
      - 本地只允许参考图库目录内的文件（拒绝绝对路径、`..`、软链与越界读取）；
      - URL 只允许公网 http(s)，禁跟随重定向，流式下载并按上限截断。

    Raises:
        ValueError: 来源无效、越界或超过体积上限。
    """
    source = (source or "").strip()
    if not source:
        raise ValueError("参考图为空")

    max_bytes = get_reference_limits()["max_reference_bytes"]

    if source.startswith("data:"):
        try:
            raw = base64.b64decode(source.split(",", 1)[1])
        except Exception as exc:  # noqa: BLE001 - 兜底为可读错误
            raise ValueError("参考图 data URI 解码失败") from exc
        if len(raw) > max_bytes:
            raise ValueError(f"参考图过大：{len(raw)} 字节（上限 {max_bytes}）")
        return raw

    if source.startswith(("http://", "https://")):
        return _download_reference(source, max_bytes)

    path = resolve_reference_path(source)
    size = os.path.getsize(path)
    if size > max_bytes:
        raise ValueError(f"参考图过大：{size} 字节（上限 {max_bytes}）")
    with open(path, "rb") as handle:
        return handle.read()


def reference_to_inline(source: str) -> tuple[str, str]:
    """参考图转 (mime, base64)，供 Gemini / MiniMax 等内联协议使用。"""
    source = (source or "").strip()
    mime = _guess_image_mime(source)
    if source.startswith("data:"):
        header, _, payload = source.partition(",")
        if ";base64" in header:
            mime = header[5:].split(";", 1)[0] or mime
        load_reference_bytes(source)  # 复用体积校验
        return mime, payload
    return mime, base64.b64encode(load_reference_bytes(source)).decode("ascii")


def reference_to_data_uri(source: str) -> str:
    """参考图转 data URI，供只接受内联图片的接口使用。"""
    source = (source or "").strip()
    if source.startswith("data:"):
        load_reference_bytes(source)
        return source
    mime, payload = reference_to_inline(source)
    return f"data:{mime};base64,{payload}"


def reference_to_http_url(source: str) -> str:
    """取参考图的公网 URL；本地图与 data URI 无法提供时抛 ValueError。"""
    source = (source or "").strip()
    if source.startswith(("http://", "https://")):
        return source
    raise ValueError("该接口的图生图需要可公网访问的参考图 URL，请先用可访问链接")


def normalize_reference_images(references) -> list[str]:
    """归一化参考图列表：去空、去重、校验数量/单图体积/合计体积。"""
    if references is None:
        return []
    if isinstance(references, str):
        references = [references]
    limits = get_reference_limits()
    cleaned: list[str] = []
    for item in references:
        value = str(item or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    max_images = limits["max_reference_images"]
    if len(cleaned) > max_images:
        raise ValueError(f"参考图最多 {max_images} 张，收到 {len(cleaned)} 张")
    total = 0
    for value in cleaned:
        total += len(load_reference_bytes(value))
        if total > limits["max_reference_total_bytes"]:
            raise ValueError(
                f"参考图合计过大：超过 {limits['max_reference_total_bytes']} 字节上限"
            )
    return cleaned


# ----------------------------------------------------------------------------
# 生成历史（data/web/image_history.json）
# ----------------------------------------------------------------------------

_HISTORY_LOCK = threading.RLock()


def _new_history_id(now: datetime | None = None) -> str:
    """生成历史记录 ID。"""
    stamp = (now or datetime.now()).strftime("%Y%m%d_%H%M%S")
    return f"img_{stamp}_{uuid.uuid4().hex[:6]}"


def _empty_history_payload() -> dict:
    return {"version": 1, "entries": []}


def load_image_history() -> dict:
    """读取历史文件；文件缺失或损坏时返回空结构。"""
    path = get_image_history_path()
    if not os.path.isfile(path):
        return _empty_history_payload()
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("[ImageService] 读取生成历史失败，按空历史处理: %s", exc)
        return _empty_history_payload()
    if not isinstance(payload, dict):
        return _empty_history_payload()
    entries = payload.get("entries")
    if not isinstance(entries, list):
        payload["entries"] = []
    payload.setdefault("version", 1)
    return payload


def _save_image_history(payload: dict) -> None:
    """原子写回历史文件。"""
    path = get_image_history_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def _decorate_history_entry(entry: dict, *, sanitize_paths: bool = True) -> dict:
    """给历史记录补派生字段（前端用），默认同时脱敏路径。

    - thumbnail / urls / image_count / success_count / failed_count：前端展示用；
    - results[].path：sanitize_paths=True 时改写成静态目录下的相对路径，
      静态目录外的路径置空，不把本机盘符路径回显给前端；
      内部删除逻辑用 _resolve_result_file 还原（兼容旧的绝对路径记录）。
    """
    results = [item for item in (entry.get("results") or []) if isinstance(item, dict)]
    for item in results:
        if sanitize_paths:
            item["path"] = static_relative_path(str(item.get("path") or ""))
        if not item.get("name"):
            item["name"] = os.path.basename(
                str(item.get("path") or item.get("url") or "")
            )
    urls = [
        str(item.get("url"))
        for item in results
        if item.get("status") == "success" and item.get("url")
    ]
    entry["urls"] = urls
    entry["thumbnail"] = urls[0] if urls else ""
    entry["image_count"] = len(results)
    entry["success_count"] = len(urls)
    failed = [item for item in results if item.get("status") != "success"]
    entry["failed_count"] = len(failed)
    return entry


def append_image_history(entry: dict) -> dict:
    """追加一条历史记录（最新在前），并按上限裁剪。"""
    entry = _decorate_history_entry(entry, sanitize_paths=False)
    with _HISTORY_LOCK:
        payload = load_image_history()
        entries = payload.get("entries") or []
        entries.insert(0, entry)
        if len(entries) > _MAX_HISTORY_ENTRIES:
            entries = entries[:_MAX_HISTORY_ENTRIES]
        payload["entries"] = entries
        _save_image_history(payload)
    return entry


def get_image_history_entry(entry_id: str) -> dict | None:
    """按 ID 取一条历史记录（返回脱敏副本，results[].path 为相对路径）。"""
    for entry in load_image_history().get("entries") or []:
        if entry.get("id") == entry_id:
            return _decorate_history_entry(copy.deepcopy(entry))
    return None


def update_image_history_entry(entry_id: str, **fields) -> dict | None:
    """更新历史记录字段，返回更新后的记录。"""
    with _HISTORY_LOCK:
        payload = load_image_history()
        updated = None
        for entry in payload.get("entries") or []:
            if entry.get("id") == entry_id:
                entry.update(fields)
                updated = entry
                break
        if updated is not None:
            _save_image_history(payload)
        return updated


def _date_from_iso(value: str):
    """解析 ISO 时间字符串为 date，失败返回 None。"""
    try:
        return datetime.fromisoformat(str(value)).date()
    except (TypeError, ValueError):
        return None


def count_images_today(now: datetime | None = None) -> int:
    """统计当天已成功生成的图片张数（配额用）。"""
    today = (now or datetime.now()).date()
    total = 0
    for entry in load_image_history().get("entries") or []:
        created = _date_from_iso(entry.get("created_at"))
        if created != today:
            continue
        total += sum(
            1
            for result in entry.get("results") or []
            if isinstance(result, dict) and result.get("status") == "success"
        )
    return total


def list_image_history(
    *,
    mode: str = "",
    keyword: str = "",
    status: str = "",
    limit: int = 20,
    offset: int = 0,
) -> dict:
    """分页查询历史记录（最新在前）。"""
    mode = (mode or "").strip().lower()
    status = (status or "").strip().lower()
    needle = (keyword or "").strip().lower()
    entries = [
        entry
        for entry in load_image_history().get("entries") or []
        if isinstance(entry, dict)
    ]
    if mode:
        entries = [entry for entry in entries if (entry.get("mode") or "") == mode]
    if status:
        entries = [entry for entry in entries if (entry.get("status") or "") == status]
    if needle:
        entries = [
            entry
            for entry in entries
            if needle in str(entry.get("prompt") or "").lower()
            or needle in str(entry.get("style") or "").lower()
        ]
    safe_limit = max(1, min(int(limit or 20), 100))
    safe_offset = max(0, int(offset or 0))
    page = [
        _decorate_history_entry(entry)
        for entry in entries[safe_offset:safe_offset + safe_limit]
    ]
    return {
        "entries": page,
        "total": len(entries),
        "limit": safe_limit,
        "offset": safe_offset,
    }


def _remove_entry_files(entry: dict) -> int:
    """删除一条历史记录里的结果文件（限定在 image_lab 目录内）。"""
    lab_dir = os.path.abspath(get_image_lab_upload_dir())
    removed = 0
    for result in entry.get("results") or []:
        if not isinstance(result, dict):
            continue
        path = _resolve_result_file(result)
        if not path:
            continue
        abs_path = os.path.abspath(path)
        if not abs_path.startswith(lab_dir + os.sep):
            continue
        try:
            os.unlink(abs_path)
            removed += 1
        except OSError:
            continue
    return removed


def delete_image_history_entry(entry_id: str, *, delete_files: bool = True) -> bool:
    """删除历史记录，可选连带删除结果文件。"""
    with _HISTORY_LOCK:
        payload = load_image_history()
        entries = payload.get("entries") or []
        target = next((item for item in entries if item.get("id") == entry_id), None)
        if target is None:
            return False
        if delete_files:
            _remove_entry_files(target)
        payload["entries"] = [item for item in entries if item.get("id") != entry_id]
        _save_image_history(payload)
    return True


# ----------------------------------------------------------------------------
# 配额
# ----------------------------------------------------------------------------


def get_image_quota_settings() -> dict:
    """读取生图配额配置（settings.json -> image_generation.quota）。"""
    quota = {
        "daily_limit": _DEFAULT_DAILY_LIMIT,
        "max_images_per_request": _MAX_IMAGES_PER_REQUEST,
        "max_reference_images": _MAX_REFERENCE_IMAGES,
        "max_reference_bytes": _MAX_REFERENCE_BYTES,
        "min_side": _MIN_IMAGE_SIDE,
        "max_side": _MAX_IMAGE_SIDE,
    }
    try:
        with open(_get_settings_path(), encoding="utf-8") as handle:
            settings = json.load(handle)
        raw = (settings.get("image_generation") or {}).get("quota") or {}
        if isinstance(raw, dict):
            def _as_int(key: str, fallback: int) -> int:
                try:
                    return max(0, int(raw.get(key, fallback)))
                except (TypeError, ValueError):
                    return fallback

            quota["daily_limit"] = _as_int("daily_limit", quota["daily_limit"])
            quota["max_images_per_request"] = max(
                1, _as_int("max_images_per_request", quota["max_images_per_request"])
            )
            quota["max_reference_images"] = max(
                1, _as_int("max_reference_images", quota["max_reference_images"])
            )
            quota["max_reference_bytes"] = max(
                1024, _as_int("max_reference_bytes", quota["max_reference_bytes"])
            )
    except (OSError, json.JSONDecodeError) as exc:
        _log.debug("[ImageService] 读取生图配额配置失败，使用默认值: %s", exc)
    return quota


def check_image_quota(count: int = 1, now: datetime | None = None) -> dict:
    """检查当日配额是否足够生成 count 张图。"""
    settings = get_image_quota_settings()
    limit = settings["daily_limit"]
    used = count_images_today(now)
    requested = max(1, int(count or 1))
    if limit <= 0:
        return {
            "allowed": True,
            "limit": 0,
            "used": used,
            "remaining": -1,
            "requested": requested,
            "reason": "",
        }
    remaining = max(0, limit - used)
    allowed = remaining >= requested
    return {
        "allowed": allowed,
        "limit": limit,
        "used": used,
        "remaining": remaining,
        "requested": requested,
        "reason": "" if allowed else "quota_exceeded",
    }


# ----------------------------------------------------------------------------
# 图生图：各 provider 实现
# ----------------------------------------------------------------------------


def _endpoint_replace(base_url: str, old_suffix: str, new_suffix: str, default_url: str) -> str:
    """把 base_url 末尾的 old_suffix 换成 new_suffix，用于 images/generations -> edits。"""
    base = (base_url or "").strip().rstrip("/")
    if not base:
        return default_url
    if base.endswith(old_suffix):
        return base[: -len(old_suffix)] + new_suffix
    if base.endswith(new_suffix):
        return base
    return f"{base}{new_suffix}"


def _origin_of(url: str) -> tuple[str, str, int] | None:
    """解析 URL 的 (scheme, host, port)，无法解析返回 None。"""
    try:
        parsed = urlparse(url or "")
    except ValueError:
        return None
    scheme = (parsed.scheme or "").lower()
    host = (parsed.hostname or "").lower()
    if scheme not in ("http", "https") or not host:
        return None
    default_port = 443 if scheme == "https" else 80
    return scheme, host, parsed.port or default_port


def _validated_task_url(task_url: str, base_url: str, task_id: str) -> str:
    """只接受与配置 base_url 同源的 task_url，否则回落到自拼的 tasks 地址。

    task_url 来自 provider 响应，直接带着 Authorization 去轮询等于把密钥
    送给任意主机；这里强制同源（scheme + host + port）才放行。
    """
    base = (base_url or "").strip().rstrip("/")
    fallback = f"{base}/tasks/{task_id}" if base else ""
    candidate = (task_url or "").strip()
    if not candidate:
        return fallback
    if base and _origin_of(candidate) == _origin_of(base):
        return candidate
    _log.warning("[ImageService] 忽略非配置域名的 task_url，改用自拼地址轮询")
    return fallback


def _poll_dashscope_task(
    api_key: str,
    task_url: str,
    proxy_kwargs: dict,
    *,
    rounds: int = 90,
    interval: float = 2.0,
) -> str | None:
    """轮询 DashScope 异步任务，返回结果图 URL。"""
    headers = {"Authorization": f"Bearer {api_key}"}
    for _ in range(rounds):
        time.sleep(interval)
        try:
            response = requests.get(task_url, headers=headers, timeout=60, **proxy_kwargs)
        except requests.exceptions.Timeout:
            continue
        if response.status_code != 200:
            _log.warning("[ImageService] DashScope 任务查询错误 %d", response.status_code)
            continue
        output = (response.json().get("output")) or {}
        status = str(output.get("task_status", "")).upper()
        if status == "SUCCEEDED":
            results = output.get("results") or []
            if results and isinstance(results[0], dict) and results[0].get("url"):
                return results[0]["url"]
            _log.error("[ImageService] DashScope 任务成功但无结果 URL")
            return None
        if status in ("FAILED", "CANCELED", "UNKNOWN"):
            _log.error(
                "[ImageService] DashScope 任务失败: %s",
                output.get("message") or response.text[:200],
            )
            return None
    _log.warning("[ImageService] DashScope 任务轮询超时")
    return None


def _call_gemini_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    proxy_kwargs: dict,
) -> str | None:
    """Gemini 原生图生图：inlineData 参考图 + 文本指令。"""
    api_key = config.get("api_key", "")
    model = config.get("model") or "gemini-3.1-flash-image"
    url = _endpoint(
        config.get("base_url", ""),
        f"/models/{model}:generateContent",
        f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
        config.get("append_base_url_path", True),
    )
    parts: list[dict] = [{"text": prompt}]
    for reference in references:
        mime, payload = reference_to_inline(reference)
        parts.append({"inlineData": {"mimeType": mime, "data": payload}})
    body = {
        "contents": [{"parts": parts}],
        "generationConfig": {"responseModalities": ["IMAGE"]},
    }
    headers = {"x-goog-api-key": api_key, "Content-Type": "application/json"}
    try:
        response = requests.post(
            url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs
        )
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] Gemini 图生图请求超时")
        return None
    if response.status_code != 200:
        _log.error(
            "[ImageService] Gemini 图生图错误 %d: %s", response.status_code, response.text[:300]
        )
        return None
    result = response.json()
    for candidate in result.get("candidates") or []:
        for part in ((candidate.get("content") or {}).get("parts")) or []:
            inline = part.get("inlineData") or part.get("inline_data") or {}
            data = inline.get("data") or ""
            if data:
                mime = inline.get("mimeType") or inline.get("mime_type") or "image/png"
                return f"data:{mime};base64,{data}"
    _log.error("[ImageService] Gemini 图生图未返回图片数据: %s", response.text[:200])
    return None


def _call_minimax_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    size: str,
    proxy_kwargs: dict,
) -> str | None:
    """MiniMax 图生图：以 subject_reference 传入参考图。"""
    api_key = config.get("api_key", "")
    url = _endpoint(
        config.get("base_url", ""),
        "/image_generation",
        "https://api.minimaxi.com/v1/image_generation",
        config.get("append_base_url_path", True),
    )
    body = {
        "model": config.get("model") or "image-01",
        "prompt": prompt,
        "aspect_ratio": _aspect_ratio(size),
        "response_format": "base64",
        "subject_reference": [
            {"type": "character", "image_file": reference_to_data_uri(references[0])}
        ],
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs
        )
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] MiniMax 图生图请求超时")
        return None
    if response.status_code != 200:
        _log.error(
            "[ImageService] MiniMax 图生图错误 %d: %s",
            response.status_code,
            response.text[:300],
        )
        return None
    data = response.json().get("data") or {}
    images = data.get("image_base64") or []
    if images and isinstance(images[0], str) and images[0]:
        return f"data:image/png;base64,{images[0]}"
    image_urls = data.get("image_urls") or []
    if image_urls and isinstance(image_urls[0], str) and image_urls[0]:
        return image_urls[0]
    _log.error("[ImageService] MiniMax 图生图未返回图片数据: %s", response.text[:200])
    return None


def _call_qwen_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    size: str,
    proxy_kwargs: dict,
) -> str | None:
    """通义万相图生图（image2image 异步任务）。"""
    api_key = config.get("api_key", "")
    model = config.get("model") or "wanx2.1-imageedit"
    base_image_url = reference_to_http_url(references[0])
    submit_url = _dashscope_endpoint(
        config.get("base_url", ""),
        "/services/aigc/image2image/image-synthesis",
        "https://dashscope.aliyuncs.com/api/v1/services/aigc/image2image/image-synthesis",
        config.get("append_base_url_path", True),
    )
    body = {
        "model": model,
        "input": {
            "function": "description_edit",
            "prompt": prompt,
            "base_image_url": base_image_url,
        },
        "parameters": {
            "size": size.replace("x", "*"),
            "n": 1,
            "watermark": False,
        },
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "X-DashScope-Async": "enable",
        "Content-Type": "application/json",
    }
    try:
        response = requests.post(submit_url, headers=headers, json=body, timeout=60, **proxy_kwargs)
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] Qwen 图生图提交超时")
        return None
    if response.status_code != 200:
        _log.error(
            "[ImageService] Qwen 图生图提交错误 %d: %s", response.status_code, response.text[:300]
        )
        return None
    output = (response.json().get("output")) or {}
    task_id = output.get("task_id")
    if not task_id:
        _log.error("[ImageService] Qwen 图生图未返回 task_id: %s", response.text[:200])
        return None
    base = (config.get("base_url") or "").strip().rstrip("/").replace(
        "/compatible-mode/v1", "/api/v1"
    )
    if not base:
        base = "https://dashscope.aliyuncs.com/api/v1"
    task_url = _validated_task_url(output.get("task_url") or "", base, task_id)
    return _poll_dashscope_task(api_key, task_url, proxy_kwargs)


def _call_doubao_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    size: str,
    strength: float,
    proxy_kwargs: dict,
) -> str | None:
    """豆包 SeedEdit 图生图（/images/generations + image 字段）。"""
    api_key = config.get("api_key", "")
    url = _endpoint(
        config.get("base_url", ""),
        "/images/generations",
        "https://ark.cn-beijing.volces.com/api/v3/images/generations",
        config.get("append_base_url_path", True),
    )
    reference = references[0].strip()
    image_field = reference if reference.startswith(("http://", "https://")) else (
        reference_to_data_uri(reference)
    )
    size_upper = size.upper()
    normalized_size = size_upper if size_upper in ("2K", "4K") else ("2K" if "x" in size else size)
    body = {
        "model": config.get("model") or "doubao-seededit-3-0-i2i-250628",
        "prompt": prompt,
        "image": image_field,
        "size": normalized_size,
        "strength": round(strength, 2),
        "sequential_image_generation": "disabled",
        "stream": False,
        "response_format": "url",
        "watermark": False,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs
        )
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] 豆包图生图请求超时")
        return None
    if response.status_code != 200:
        _log.error(
            "[ImageService] 豆包图生图错误 %d: %s", response.status_code, response.text[:300]
        )
        return None
    return _extract_image_url(response.json(), "doubao")


def _call_openai_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    size: str,
    strength: float,
    proxy_kwargs: dict,
) -> str | None:
    """OpenAI 兼容 /images/edits（multipart 上传参考图）。"""
    api_key = config.get("api_key", "")
    model = config.get("model", "gpt-image-1")
    url = _endpoint_replace(
        config.get("base_url", ""),
        "/images/generations",
        "/images/edits",
        "https://api.openai.com/v1/images/edits",
    )
    files = []
    for index, reference in enumerate(references):
        mime = _guess_image_mime(reference)
        ext = os.path.splitext(reference.split("?", 1)[0])[1] or ".png"
        files.append(
            ("image", (f"reference_{index}{ext}", load_reference_bytes(reference), mime))
        )
    data = {
        "model": model,
        "prompt": prompt,
        "n": "1",
        "size": size,
        "strength": str(round(strength, 2)),
    }
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        response = requests.post(
            url,
            headers=headers,
            data=data,
            files=files,
            timeout=_API_TIMEOUT,
            **proxy_kwargs,
        )
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] OpenAI 图生图请求超时")
        return None
    if response.status_code != 200:
        _log.error(
            "[ImageService] OpenAI 图生图错误 %d: %s", response.status_code, response.text[:300]
        )
        return None
    return _extract_image_url(response.json(), "openai")


def _call_json_image_edit(
    config: dict,
    prompt: str,
    references: list[str],
    size: str,
    strength: float,
    proxy_kwargs: dict,
) -> str | None:
    """通用 JSON 图生图（SiliconFlow / 自定义兼容接口，image 字段传 data URI）。"""
    api_key = config.get("api_key", "")
    model = config.get("model", "")
    url = _endpoint(
        config.get("base_url", ""),
        "/images/generations",
        "https://api.siliconflow.cn/v1/images/generations",
        config.get("append_base_url_path", True),
    )
    body = {
        "model": model,
        "prompt": prompt,
        "image": reference_to_data_uri(references[0]),
        "image_size": size,
        "strength": round(strength, 2),
        "batch_size": 1,
    }
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    try:
        response = requests.post(
            url, headers=headers, json=body, timeout=_API_TIMEOUT, **proxy_kwargs
        )
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] 通用图生图请求超时")
        return None
    if response.status_code != 200:
        _log.error("[ImageService] 通用图生图错误 %d: %s", response.status_code, response.text[:300])
        return None
    return _extract_image_url(response.json(), config.get("provider_type", ""))


def call_image_to_image(
    prompt: str,
    config: dict,
    *,
    reference_images: list[str],
    strength: float = _DEFAULT_STRENGTH,
    size: str = "",
    negative_prompt: str = "",
    style: str = "",
) -> str | None:
    """调用图生图接口，返回图片 URL 或 base64 data URI。

    Args:
        prompt: 画面描述（英文更稳）。
        config: 图片生成模型配置（同 call_image_generation）。
        reference_images: 参考图列表，1~4 张（URL / data URI / 本地路径）。
        strength: 重绘强度 0.0~1.0。
        size: 输出尺寸。
        negative_prompt: 负向词。
        style: 风格预设 id（会拼进 prompt）。

    Returns:
        成功返回图片 URL/base64，失败返回 None。
    """
    if not config:
        _log.warning("[ImageService] 缺少图片生成配置")
        return None

    api_key = config.get("api_key", "")
    full_url = config.get("base_url", "")
    image_provider = _image_provider(config)
    native_providers = (
        "gemini", "google", "google_gemini",
        "minimax",
        "qwen", "dashscope", "tongyi",
        "doubao", "seed", "seedream", "volcengine", "ark", "volces",
    )
    if not api_key:
        _log.warning("[ImageService] api_key 未配置")
        return None
    if not full_url and image_provider not in native_providers:
        _log.warning("[ImageService] base_url 未配置")
        return None

    references = [str(item).strip() for item in (reference_images or []) if str(item).strip()]
    if not references:
        _log.warning("[ImageService] 图生图缺少参考图")
        return None

    image_size = normalize_image_size(size or config.get("size", "") or _DEFAULT_SIZE)
    image_size = _resolve_size(
        image_size, provider_type=config.get("provider_type", ""), base_url=full_url
    )
    full_prompt = compose_image_prompt(prompt, style=style, negative_prompt=negative_prompt)
    strength = normalize_strength(strength)
    proxy_kwargs = model_proxy_request_kwargs(config.get("proxy_url", ""))
    provider_type = config.get("provider_type", "openai_compatible")

    try:
        if image_provider in ("gemini", "google", "google_gemini"):
            return _call_gemini_image_edit(config, full_prompt, references, proxy_kwargs)
        if image_provider == "minimax":
            return _call_minimax_image_edit(
                config, full_prompt, references, image_size, proxy_kwargs
            )
        if image_provider in ("qwen", "dashscope", "tongyi"):
            return _call_qwen_image_edit(
                config, full_prompt, references, image_size, proxy_kwargs
            )
        if image_provider in ("doubao", "seed", "seedream", "volcengine", "ark", "volces"):
            return _call_doubao_image_edit(
                config, full_prompt, references, image_size, strength, proxy_kwargs
            )
        if provider_type in ("openai_compatible", "openai") or "openai" in full_url.lower():
            return _call_openai_image_edit(
                config, full_prompt, references, image_size, strength, proxy_kwargs
            )
        return _call_json_image_edit(
            config, full_prompt, references, image_size, strength, proxy_kwargs
        )
    except ValueError as exc:
        _log.warning("[ImageService] 图生图参数不合法: %s", exc)
        return None
    except requests.exceptions.Timeout:
        _log.warning("[ImageService] 图生图请求超时")
        return None
    except Exception as exc:  # noqa: BLE001 - 兜底为 None，由上层记失败
        _log.error("[ImageService] 图生图调用失败: %s", exc, exc_info=True)
        return None


def _save_generated_image(image_data: str, *, prefix: str) -> tuple[str, str] | None:
    """保存生成结果到 image_lab 目录，返回 (url, 绝对路径)。"""
    upload_dir = get_image_lab_upload_dir()
    url = save_image_to_uploads(image_data, upload_dir, prefix=prefix)
    if not url:
        return None
    if url.startswith("/static/"):
        path = _static_url_to_path(url) or ""
    else:
        path = os.path.join(upload_dir, url)
    return url, path


# ----------------------------------------------------------------------------
# 统一生成入口（Web 路由 / Agent 工具共用）
# ----------------------------------------------------------------------------


def generate_image(
    prompt: str,
    *,
    config: dict | None = None,
    mode: str = "txt2img",
    reference_images: list[str] | None = None,
    strength: float = _DEFAULT_STRENGTH,
    style: str = "",
    negative_prompt: str = "",
    size: str = "",
    count: int = 1,
    source: str = "web",
    session_id: str = "",
    retry_of: str = "",
    record_history: bool = True,
) -> dict:
    """生成图片（文生图 / 图生图，支持批量），并落盘历史。

    Returns:
        dict: success / images / history_id / quota / error / elapsed_ms / params
    """
    started = time.time()
    mode = (mode or "txt2img").strip().lower()
    if mode not in ("txt2img", "img2img"):
        mode = "txt2img"

    prompt = (prompt or "").strip()
    if not prompt:
        return {"success": False, "error": "prompt 不能为空", "reason": "invalid_prompt"}

    try:
        size = normalize_image_size(size)
        strength = normalize_strength(strength)
        references = normalize_reference_images(reference_images)
    except ValueError as exc:
        return {"success": False, "error": str(exc), "reason": "invalid_parameter"}

    if mode == "img2img" and not references:
        return {
            "success": False,
            "error": "图生图需要至少一张参考图",
            "reason": "missing_reference",
        }

    quota_settings = get_image_quota_settings()
    max_count = quota_settings["max_images_per_request"]
    try:
        count = int(count or 1)
    except (TypeError, ValueError):
        count = 1
    if count < 1 or count > max_count:
        return {
            "success": False,
            "error": f"单次生成数量需在 1~{max_count} 之间",
            "reason": "invalid_count",
        }

    quota = check_image_quota(count)
    if not quota["allowed"]:
        return {
            "success": False,
            "error": f"今日生图配额不足：已用 {quota['used']}/{quota['limit']}，本次需 {count} 张",
            "reason": "quota_exceeded",
            "quota": quota,
        }

    resolved_config = config or get_image_generation_config()
    if not resolved_config:
        return {"success": False, "error": "未配置图片生成模型", "reason": "no_model_config"}

    style_key = normalize_style(style)
    results: list[dict] = []
    for index in range(count):
        item_started = time.time()
        try:
            if mode == "img2img":
                image_data = call_image_to_image(
                    prompt,
                    resolved_config,
                    reference_images=references,
                    strength=strength,
                    size=size,
                    negative_prompt=negative_prompt,
                    style=style_key,
                )
            else:
                image_data = call_image_generation(
                    prompt,
                    resolved_config,
                    size=size,
                    extra_keywords=[
                        part
                        for part in (style_prompt_parts(style_key)[0], negative_prompt)
                        if part
                    ]
                    or None,
                )
        except Exception as exc:  # noqa: BLE001 - 单张失败不中断批量
            _log.error("[ImageService] 第 %d 张生成异常: %s", index + 1, exc)
            image_data = None

        elapsed = int((time.time() - item_started) * 1000)
        if not image_data:
            results.append(
                {
                    "index": index,
                    "status": "failed",
                    "url": "",
                    "path": "",
                    "size": size,
                    "elapsed_ms": elapsed,
                    "error": "provider 未返回图片",
                }
            )
            continue

        saved = _save_generated_image(
            image_data, prefix="lab" if mode == "txt2img" else "lab_i2i"
        )
        if not saved:
            results.append(
                {
                    "index": index,
                    "status": "failed",
                    "url": "",
                    "path": "",
                    "size": size,
                    "elapsed_ms": elapsed,
                    "error": "图片保存失败",
                }
            )
            continue
        url, path = saved
        results.append(
            {
                "index": index,
                "status": "success",
                "url": url,
                "path": static_relative_path(path),
                "name": os.path.basename(path),
                "size": size,
                "elapsed_ms": elapsed,
                "error": "",
            }
        )

    success_count = sum(1 for item in results if item["status"] == "success")
    status = "success" if success_count == count else ("partial" if success_count else "failed")
    history_entry = {
        "id": _new_history_id(),
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "mode": mode,
        "status": status,
        "prompt": prompt,
        "negative_prompt": negative_prompt,
        "style": style_key,
        "size": size,
        "strength": round(strength, 2) if mode == "img2img" else None,
        "count": count,
        "reference_images": references,
        "provider": _image_provider(resolved_config),
        "model": resolved_config.get("model", ""),
        "source": source,
        "session_id": session_id,
        "retry_of": retry_of,
        "elapsed_ms": int((time.time() - started) * 1000),
        "results": results,
        "error": "" if success_count else (results[0].get("error") if results else ""),
    }
    if record_history:
        append_image_history(history_entry)

    images = [
        {
            "url": item["url"],
            "path": item["path"],
            "name": item.get("name", ""),
            "size": item["size"],
            "mode": mode,
            "index": item["index"],
        }
        for item in results
        if item["status"] == "success"
    ]
    return {
        "success": success_count > 0,
        "partial": 0 < success_count < count,
        "images": images,
        "history_id": history_entry["id"] if record_history else "",
        "entry": _decorate_history_entry(copy.deepcopy(history_entry)) if record_history else None,
        "mode": mode,
        "params": {
            "prompt": prompt,
            "negative_prompt": negative_prompt,
            "style": style_key,
            "size": size,
            "strength": strength,
            "count": count,
            "reference_images": references,
            "mode": mode,
        },
        "quota": check_image_quota(count),
        "elapsed_ms": history_entry["elapsed_ms"],
        "error": "" if success_count else (results[0].get("error") if results else "生成失败"),
    }


def retry_image_generation(entry_id: str, *, source: str = "web") -> dict:
    """按历史记录参数重试生成，返回 generate_image 的结果。"""
    entry = get_image_history_entry(entry_id)
    if not entry:
        return {"success": False, "error": "历史记录不存在", "reason": "not_found"}
    return generate_image(
        entry.get("prompt", ""),
        mode=entry.get("mode", "txt2img"),
        reference_images=entry.get("reference_images") or [],
        strength=entry.get("strength") or _DEFAULT_STRENGTH,
        style=entry.get("style", ""),
        negative_prompt=entry.get("negative_prompt", ""),
        size=entry.get("size", ""),
        count=entry.get("count", 1),
        source=source,
        session_id=entry.get("session_id", ""),
        retry_of=entry_id,
    )


def generate_image_tool(arguments: dict, context: dict | None = None) -> dict:
    """Agent 工具入口：generate_image。

    Args:
        arguments: 工具参数（prompt/mode/reference_images/strength/style/
            negative_prompt/size/count）。
        context: 调用上下文（可选，取 session_id）。

    Returns:
        工具结果字典（success/images/history_id/error...）。
    """
    arguments = arguments or {}
    context = context or {}
    result = generate_image(
        arguments.get("prompt", ""),
        mode=arguments.get("mode", "txt2img"),
        reference_images=arguments.get("reference_images") or [],
        strength=arguments.get("strength", _DEFAULT_STRENGTH),
        style=arguments.get("style", ""),
        negative_prompt=arguments.get("negative_prompt", ""),
        size=arguments.get("size", ""),
        count=arguments.get("count", 1),
        source="agent",
        session_id=str(context.get("session_id") or ""),
    )
    return result


# ============================================================================
# P2-10 生图实验室：模板 / 参数组合对比 / 任务队列
#
# 能力：
#   1. 模板：内置预设（立绘重绘 / 换装 / 表情差分 / 三视图 …）+ 用户自存模板，
#      落盘 data/web/image_lab_templates.json，可增删改；
#   2. 参数组合对比：styles × strengths × sizes × counts 笛卡尔积（上限 8 组），
#      也支持显式 variants 列表；先出计划再执行；
#   3. 任务队列：异步单 worker 队列，任务带进度（total/done/percent），
#      支持查询、列表、取消；
#   4. 结果分组返回：groups[] 每组含参数与图片，供前端按参数对比展示。
# ============================================================================

# 用户模板文件（可用 NBOT_IMAGE_TEMPLATES_PATH 覆盖，测试用）
_LAB_TEMPLATES_PATH_ENV = "NBOT_IMAGE_TEMPLATES_PATH"
# 单次对比最多几组参数
_MAX_COMPARE_GROUPS = 8
# 内存里最多保留多少个任务（旧的已完成任务被裁掉）
_MAX_LAB_TASKS = 60
# 模板名称长度上限
_TEMPLATE_NAME_MAX = 40
# 允许在对比里覆盖的参数键
_COMPARE_PARAM_KEYS = (
    "prompt",
    "mode",
    "reference_images",
    "strength",
    "style",
    "negative_prompt",
    "size",
    "count",
)

BUILTIN_IMAGE_LAB_TEMPLATES: list[dict] = [
    {
        "id": "builtin:style_restyle",
        "name": "立绘风格重绘",
        "description": "保留角色脸部与服装，按选定风格重绘立绘",
        "category": "img2img",
        "mode": "img2img",
        "prompt": (
            "keep the character's face, hairstyle and outfit unchanged, "
            "redraw the same character in the selected art style"
        ),
        "negative_prompt": "different character, extra limbs, distorted face",
        "style": "anime",
        "strength": 0.55,
        "size": "1024x1536",
        "count": 1,
    },
    {
        "id": "builtin:outfit_swap",
        "name": "换装",
        "description": "同一角色换一套服装，姿态与背景尽量保留",
        "category": "img2img",
        "mode": "img2img",
        "prompt": (
            "same character, keep pose and background, change the outfit to "
            "a casual modern street style outfit, detailed fabric folds"
        ),
        "negative_prompt": "different face, wrong proportions",
        "style": "none",
        "strength": 0.7,
        "size": "1024x1536",
        "count": 1,
    },
    {
        "id": "builtin:lineart_colorize",
        "name": "线稿上色",
        "description": "给线稿上色，保留线稿结构",
        "category": "img2img",
        "mode": "img2img",
        "prompt": (
            "colorize the line art, keep every line and composition, "
            "soft cel shading, harmonious palette"
        ),
        "negative_prompt": "photo, extra lines, changed pose",
        "style": "anime",
        "strength": 0.4,
        "size": "1024x1024",
        "count": 1,
    },
    {
        "id": "builtin:scene_swap",
        "name": "背景替换",
        "description": "保留主体，替换为指定场景背景",
        "category": "img2img",
        "mode": "img2img",
        "prompt": (
            "keep the subject exactly, replace the background with a cozy room, "
            "matching lighting and perspective"
        ),
        "negative_prompt": "changed subject, blurry subject",
        "style": "none",
        "strength": 0.75,
        "size": "1536x1024",
        "count": 1,
    },
    {
        "id": "builtin:emotion_sheet",
        "name": "表情差分 4 张",
        "description": "同一角色的四种表情批量出图",
        "category": "txt2img",
        "mode": "txt2img",
        "prompt": (
            "character expression sheet, same character, four expressions: "
            "happy, sad, angry, surprised, consistent art style, clean background"
        ),
        "negative_prompt": "different characters, inconsistent style",
        "style": "anime",
        "strength": 0.6,
        "size": "1024x1024",
        "count": 4,
    },
    {
        "id": "builtin:turnaround",
        "name": "三视图立绘",
        "description": "角色设定三视图，用于建模/立绘参考",
        "category": "txt2img",
        "mode": "txt2img",
        "prompt": (
            "character turnaround sheet, front view, side view and back view of "
            "the same character, full body, white background, consistent proportions"
        ),
        "negative_prompt": "different characters, cropped body",
        "style": "anime",
        "strength": 0.6,
        "size": "1536x1024",
        "count": 1,
    },
]

_TEMPLATE_LOCK = threading.RLock()


def _now_iso() -> str:
    """当前本地时间 ISO 字符串（秒精度）。"""
    return datetime.now().astimezone().isoformat(timespec="seconds")


def get_image_lab_templates_path() -> str:
    """用户模板文件路径。"""
    override = (os.environ.get(_LAB_TEMPLATES_PATH_ENV) or "").strip()
    if override:
        return override
    return os.path.join(_project_root(), "data", "web", "image_lab_templates.json")


def _load_user_templates() -> list[dict]:
    """读取用户模板列表；文件缺失/损坏时返回空列表。"""
    path = get_image_lab_templates_path()
    if not os.path.isfile(path):
        return []
    try:
        with open(path, encoding="utf-8") as handle:
            payload = json.load(handle)
    except (OSError, json.JSONDecodeError) as exc:
        _log.warning("[ImageService] 读取生图模板失败，按空模板处理: %s", exc)
        return []
    templates = payload.get("templates") if isinstance(payload, dict) else payload
    if not isinstance(templates, list):
        return []
    return [item for item in templates if isinstance(item, dict) and item.get("id")]


def _save_user_templates(templates: list[dict]) -> None:
    """原子写回用户模板文件。"""
    path = get_image_lab_templates_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = f"{path}.{os.getpid()}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as handle:
        json.dump({"version": 1, "templates": templates}, handle, ensure_ascii=False, indent=2)
    os.replace(tmp_path, path)


def list_image_lab_templates() -> dict:
    """列出内置 + 用户模板（内置在前）。"""
    with _TEMPLATE_LOCK:
        users = _load_user_templates()
    builtins = [dict(item, builtin=True) for item in BUILTIN_IMAGE_LAB_TEMPLATES]
    templates = builtins + users
    return {
        "templates": templates,
        "total": len(templates),
        "builtin_count": len(builtins),
    }


def get_image_lab_template(template_id: str) -> dict | None:
    """按 ID 取模板（内置或用户）。"""
    needle = (template_id or "").strip()
    if not needle:
        return None
    for item in BUILTIN_IMAGE_LAB_TEMPLATES:
        if item.get("id") == needle:
            return dict(item, builtin=True)
    with _TEMPLATE_LOCK:
        for item in _load_user_templates():
            if item.get("id") == needle:
                return dict(item, builtin=False)
    return None


def _normalize_reference_list(references) -> list[str]:
    """参考图列表轻量归一化：去空、去重、限量（不做可读性校验）。"""
    if references is None:
        return []
    if isinstance(references, str):
        references = [references]
    cleaned: list[str] = []
    for item in references:
        value = str(item or "").strip()
        if value and value not in cleaned:
            cleaned.append(value)
    return cleaned[:_MAX_REFERENCE_IMAGES]


def _coerce_template_count(value, fallback: int = 1) -> int:
    """模板张数归一化到 1~_MAX_IMAGES_PER_REQUEST。"""
    try:
        count = int(value)
    except (TypeError, ValueError):
        count = fallback
    return max(1, min(_MAX_IMAGES_PER_REQUEST, count))


def _normalize_template(payload: dict, *, template_id: str = "", builtin: bool = False) -> dict:
    """校验并归一化模板字典，非法时抛 ValueError。"""
    payload = payload or {}
    name = str(payload.get("name") or "").strip()
    if not name:
        raise ValueError("模板名称不能为空")
    if len(name) > _TEMPLATE_NAME_MAX:
        raise ValueError(f"模板名称过长（上限 {_TEMPLATE_NAME_MAX} 字）")
    mode = str(payload.get("mode") or "txt2img").strip().lower()
    if mode not in ("txt2img", "img2img"):
        raise ValueError("mode 只能是 txt2img 或 img2img")
    prompt = str(payload.get("prompt") or "").strip()
    if not prompt:
        raise ValueError("模板 prompt 不能为空")
    references = _normalize_reference_list(payload.get("reference_images"))
    if mode == "img2img" and not references:
        raise ValueError("图生图模板需要至少一张参考图")
    now = _now_iso()
    return {
        "id": template_id or f"tpl_{uuid.uuid4().hex[:10]}",
        "name": name,
        "description": str(payload.get("description") or "").strip()[:200],
        "category": (str(payload.get("category") or "custom").strip()[:20] or "custom"),
        "mode": mode,
        "prompt": prompt,
        "negative_prompt": str(payload.get("negative_prompt") or "").strip(),
        "style": normalize_style(payload.get("style", "")),
        "strength": normalize_strength(payload.get("strength", _DEFAULT_STRENGTH)),
        "size": normalize_image_size(payload.get("size", "")),
        "count": _coerce_template_count(payload.get("count", 1)),
        "reference_images": references,
        "builtin": builtin,
        "created_at": str(payload.get("created_at") or now),
        "updated_at": now,
    }


def save_image_lab_template(payload: dict) -> dict:
    """新建用户模板。"""
    template = _normalize_template(payload)
    with _TEMPLATE_LOCK:
        templates = _load_user_templates()
        if len(templates) >= 100:
            raise ValueError("用户模板最多 100 个，请先删除旧模板")
        templates.append(template)
        _save_user_templates(templates)
    return template


def update_image_lab_template(template_id: str, payload: dict) -> dict:
    """更新用户模板（内置模板不可改）。"""
    needle = (template_id or "").strip()
    if not needle:
        raise ValueError("模板 ID 不能为空")
    if any(item.get("id") == needle for item in BUILTIN_IMAGE_LAB_TEMPLATES):
        raise ValueError("内置模板不能修改，请另存为副本")
    with _TEMPLATE_LOCK:
        templates = _load_user_templates()
        target = next((item for item in templates if item.get("id") == needle), None)
        if target is None:
            raise KeyError(needle)
        merged = {**target, **(payload or {})}
        updated = _normalize_template(merged, template_id=needle)
        templates = [updated if item.get("id") == needle else item for item in templates]
        _save_user_templates(templates)
    return updated


def delete_image_lab_template(template_id: str) -> bool:
    """删除用户模板；内置模板抛 ValueError，不存在返回 False。"""
    needle = (template_id or "").strip()
    if any(item.get("id") == needle for item in BUILTIN_IMAGE_LAB_TEMPLATES):
        raise ValueError("内置模板不能删除")
    with _TEMPLATE_LOCK:
        templates = _load_user_templates()
        kept = [item for item in templates if item.get("id") != needle]
        if len(kept) == len(templates):
            return False
        _save_user_templates(kept)
    return True


# ----------------------------------------------------------------------------
# 参数组合对比：生成执行计划
# ----------------------------------------------------------------------------


def _axis_values(value) -> list:
    """把单值或列表归一化为列表（去掉空值）。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        items = [value]
    return [item for item in items if item not in (None, "")]


def _variant_label(params: dict) -> str:
    """对比组标签，例如 "anime · 0.60 · 1024x1024 · ×2"。"""
    parts: list[str] = []
    style = params.get("style") or "none"
    label = IMAGE_STYLE_PRESETS.get(style, {}).get("label") or style
    parts.append(str(label))
    if params.get("mode") == "img2img":
        parts.append(f"强度 {float(params.get('strength') or 0):.2f}")
    parts.append(str(params.get("size") or _DEFAULT_SIZE))
    try:
        count = int(params.get("count") or 1)
    except (TypeError, ValueError):
        count = 1
    if count > 1:
        parts.append(f"×{count}")
    return " · ".join(parts)


def build_compare_groups(
    base: dict,
    variations: dict | None = None,
    *,
    explicit: list | None = None,
    limit: int = _MAX_COMPARE_GROUPS,
) -> dict:
    """构建参数对比执行计划（不生成图片）。

    Args:
        base: 基础参数（prompt/mode/style/size/strength/count/reference_images）。
        variations: 轴式组合：{"styles": [...], "strengths": [...],
            "sizes": [...], "counts": [...]}。
        explicit: 显式覆盖列表，优先级高于 variations。
        limit: 最多几组。

    Returns:
        {"groups": [...], "total": n, "truncated": bool}
    """
    base = dict(base or {})
    overrides: list[dict] = []

    if explicit:
        for item in explicit:
            if isinstance(item, dict):
                overrides.append(
                    {key: value for key, value in item.items() if key in _COMPARE_PARAM_KEYS}
                )
    else:
        variations = variations or {}
        axes: list[tuple[str, list]] = []
        styles = _axis_values(variations.get("styles"))
        if styles:
            axes.append(("style", [normalize_style(str(item)) for item in styles]))
        strengths = _axis_values(variations.get("strengths"))
        if strengths:
            axes.append(("strength", [normalize_strength(item) for item in strengths]))
        sizes = _axis_values(variations.get("sizes"))
        if sizes:
            axes.append(("size", [normalize_image_size(str(item)) for item in sizes]))
        counts = _axis_values(variations.get("counts"))
        if counts:
            axes.append(("count", [_coerce_template_count(item) for item in counts]))
        if axes:
            for combo in itertools.product(*[values for _, values in axes]):
                overrides.append(
                    {key: value for (key, _), value in zip(axes, combo, strict=True)}
                )

    if not overrides:
        overrides = [{}]

    safe_limit = max(1, min(int(limit or _MAX_COMPARE_GROUPS), _MAX_COMPARE_GROUPS))
    truncated = len(overrides) > safe_limit
    overrides = overrides[:safe_limit]

    groups: list[dict] = []
    for index, override in enumerate(overrides):
        params = {**base, **override}
        params.pop("template_id", None)
        params["size"] = normalize_image_size(params.get("size", ""))
        params["strength"] = normalize_strength(params.get("strength", _DEFAULT_STRENGTH))
        params["style"] = normalize_style(params.get("style", ""))
        params["count"] = _coerce_template_count(params.get("count", 1))
        params["mode"] = "img2img" if params.get("mode") == "img2img" else "txt2img"
        params["reference_images"] = _normalize_reference_list(params.get("reference_images"))
        groups.append({
            "id": f"g{index + 1}",
            "label": _variant_label(params),
            "params": params,
            "status": "pending",
            "images": [],
            "history_id": "",
            "elapsed_ms": 0,
            "error": "",
        })
    return {"groups": groups, "total": len(groups), "truncated": truncated}


def _lab_base_params(payload: dict, *, allow_empty_prompt: bool = False) -> dict:
    """合并模板与请求参数，得到实验室基础参数。

    Args:
        payload: 请求体。
        allow_empty_prompt: 允许基础 prompt 为空（显式 variants 各自带 prompt 时用）。

    Raises:
        ValueError: 模板不存在或参数非法。
    """
    payload = payload or {}
    template_id = str(payload.get("template_id") or "").strip()
    template = None
    if template_id:
        template = get_image_lab_template(template_id)
        if template is None:
            raise ValueError(f"模板不存在：{template_id}")

    def _pick(key: str, default):
        value = payload.get(key)
        if value in (None, "", []):
            value = (template or {}).get(key, default)
        return value

    prompt = str(_pick("prompt", "") or "").strip()
    if not prompt and not allow_empty_prompt:
        raise ValueError("prompt 不能为空")
    mode = str(_pick("mode", "txt2img") or "txt2img").strip().lower()
    base = {
        "prompt": prompt,
        "mode": mode if mode in ("txt2img", "img2img") else "txt2img",
        "style": normalize_style(str(_pick("style", "none") or "none")),
        "negative_prompt": str(_pick("negative_prompt", "") or "").strip(),
        "size": normalize_image_size(str(_pick("size", "") or "")),
        "strength": normalize_strength(_pick("strength", _DEFAULT_STRENGTH)),
        "count": _coerce_template_count(_pick("count", 1)),
        "reference_images": _normalize_reference_list(_pick("reference_images", [])),
        "template_id": template_id,
    }
    return base


# ----------------------------------------------------------------------------
# 任务队列（异步生成 / 参数对比）
# ----------------------------------------------------------------------------

_LAB_TASK_LOCK = threading.RLock()
_LAB_TASKS: dict[str, dict] = {}
_LAB_TASK_QUEUE: "queue.Queue[str]" = queue.Queue()
_LAB_WORKER_STARTED = False


def _prune_lab_tasks_locked() -> None:
    """裁剪内存任务表：保留未完成任务 + 最近的已完成任务。"""
    if len(_LAB_TASKS) <= _MAX_LAB_TASKS:
        return
    finished = [
        task_id
        for task_id, task in _LAB_TASKS.items()
        if task.get("status") in ("succeeded", "partial", "failed", "canceled")
    ]
    finished.sort(key=lambda item: _LAB_TASKS[item].get("created_at", ""))
    for task_id in finished[: len(_LAB_TASKS) - _MAX_LAB_TASKS]:
        _LAB_TASKS.pop(task_id, None)


def _ensure_lab_worker_locked() -> None:
    """惰性启动后台 worker（调用方需持有 _LAB_TASK_LOCK）。"""
    global _LAB_WORKER_STARTED
    if _LAB_WORKER_STARTED:
        return
    thread = threading.Thread(
        target=_lab_worker_loop,
        name="image-lab-worker",
        daemon=True,
    )
    thread.start()
    _LAB_WORKER_STARTED = True


def _lab_worker_loop() -> None:
    """串行消费任务队列；单 worker 保证同一时刻只跑一组对比。"""
    while True:
        task_id = _LAB_TASK_QUEUE.get()
        try:
            _run_lab_task(task_id)
        except Exception as exc:  # noqa: BLE001 - worker 不能退出
            _log.error("[ImageService] 生图任务执行异常: %s", exc, exc_info=True)
            with _LAB_TASK_LOCK:
                task = _LAB_TASKS.get(task_id)
            if task is not None:
                task["status"] = "failed"
                task["error"] = str(exc)
                task["finished_at"] = _now_iso()
        finally:
            _LAB_TASK_QUEUE.task_done()


def _update_task_progress(task: dict, done: int, total: int) -> None:
    """更新任务进度。"""
    total = max(1, int(total))
    done = max(0, min(int(done), total))
    task["progress"] = {
        "total": total,
        "done": done,
        "percent": round(done * 100.0 / total, 1),
    }


def _finish_lab_task(task: dict, status: str) -> None:
    """收尾任务：写状态、结束时间，并把 groups 交给读方。"""
    task["status"] = status
    task["finished_at"] = _now_iso()
    total = task.get("progress", {}).get("total") or len(task.get("groups") or []) or 1
    done = len([g for g in task.get("groups") or [] if g.get("status") in ("succeeded", "failed")])
    _update_task_progress(task, done, total)
    task["image_count"] = sum(len(g.get("images") or []) for g in task.get("groups") or [])


def _run_lab_task(task_id: str) -> None:
    """执行一个实验室任务：逐组调用 generate_image。"""
    with _LAB_TASK_LOCK:
        task = _LAB_TASKS.get(task_id)
    if task is None:
        return
    if task.get("cancel_requested"):
        _finish_lab_task(task, "canceled")
        return

    task["status"] = "running"
    task["started_at"] = _now_iso()
    groups = task.get("groups") or []
    _update_task_progress(task, 0, len(groups) or 1)

    for index, group in enumerate(groups):
        if task.get("cancel_requested"):
            for pending in groups[index:]:
                pending["status"] = "canceled"
            _finish_lab_task(task, "canceled")
            return
        group["status"] = "running"
        params = group.get("params") or {}
        try:
            result = generate_image(
                params.get("prompt", ""),
                mode=params.get("mode", "txt2img"),
                reference_images=params.get("reference_images") or [],
                strength=params.get("strength", _DEFAULT_STRENGTH),
                style=params.get("style", ""),
                negative_prompt=params.get("negative_prompt", ""),
                size=params.get("size", ""),
                count=params.get("count", 1),
                source=str(task.get("source") or "lab"),
                session_id=str(task.get("session_id") or ""),
            )
        except Exception as exc:  # noqa: BLE001 - 单组失败不影响其他组
            _log.error("[ImageService] 对比组 %s 生成异常: %s", group.get("id"), exc)
            result = {"success": False, "error": str(exc), "images": []}

        succeeded = bool(result.get("success"))
        group.update({
            "status": "succeeded" if succeeded else "failed",
            "images": result.get("images") or [],
            "history_id": result.get("history_id", ""),
            "elapsed_ms": int(result.get("elapsed_ms") or 0),
            "error": "" if succeeded else (result.get("error") or "生成失败"),
        })
        _update_task_progress(task, index + 1, len(groups) or 1)

    succeeded_count = len([g for g in groups if g.get("status") == "succeeded"])
    if succeeded_count == len(groups) and groups:
        final_status = "succeeded"
    elif succeeded_count:
        final_status = "partial"
    else:
        final_status = "failed"
    if not task.get("error"):
        failed = [g for g in groups if g.get("status") == "failed"]
        task["error"] = failed[0].get("error", "") if failed and not succeeded_count else ""
    _finish_lab_task(task, final_status)


def submit_image_lab_task(payload: dict) -> dict:
    """提交实验室任务（异步）。

    Args:
        payload: template_id / prompt / mode / style / size / strength / count /
            reference_images / variations / variants / session_id。

    Returns:
        {"success": True, "task": {...}} 或 {"success": False, "error": ...,
        "reason": ...}
    """
    explicit_variants = (payload or {}).get("variants") or None
    try:
        base = _lab_base_params(payload, allow_empty_prompt=bool(explicit_variants))
        plan = build_compare_groups(
            base,
            (payload or {}).get("variations") or {},
            explicit=explicit_variants,
        )
    except (ValueError, KeyError) as exc:
        return {"success": False, "error": str(exc), "reason": "invalid_parameter"}

    groups = plan["groups"]
    for group in groups:
        params = group.get("params") or {}
        if not str(params.get("prompt") or "").strip():
            return {
                "success": False,
                "error": f"prompt 不能为空（{group.get('label')}）",
                "reason": "invalid_parameter",
            }
        if params.get("mode") == "img2img" and not params.get("reference_images"):
            return {
                "success": False,
                "error": f"图生图需要至少一张参考图（{group.get('label')}）",
                "reason": "missing_reference",
            }
    total_images = sum(int(group["params"]["count"]) for group in groups)
    quota = check_image_quota(total_images)
    if not quota["allowed"]:
        return {
            "success": False,
            "error": (
                f"今日生图配额不足：已用 {quota['used']}/{quota['limit']}，"
                f"本次需 {total_images} 张"
            ),
            "reason": "quota_exceeded",
            "quota": quota,
        }

    if not get_image_generation_config():
        return {"success": False, "error": "未配置图片生成模型", "reason": "no_model_config"}

    task = {
        "id": f"lab_{int(time.time())}_{uuid.uuid4().hex[:6]}",
        "kind": "compare" if (len(groups) > 1 or plan["truncated"]) else "generate",
        "status": "queued",
        "created_at": _now_iso(),
        "started_at": "",
        "finished_at": "",
        "template_id": base.get("template_id", ""),
        "params": {key: value for key, value in base.items() if key != "template_id"},
        "groups": groups,
        "progress": {"total": len(groups), "done": 0, "percent": 0.0},
        "truncated": plan["truncated"],
        "source": str((payload or {}).get("source") or "web_lab"),
        "session_id": str((payload or {}).get("session_id") or ""),
        "requested_images": total_images,
        "image_count": 0,
        "cancel_requested": False,
        "error": "",
    }

    with _LAB_TASK_LOCK:
        _LAB_TASKS[task["id"]] = task
        _prune_lab_tasks_locked()
        _ensure_lab_worker_locked()
    _LAB_TASK_QUEUE.put(task["id"])
    return {"success": True, "task": copy.deepcopy(task)}


def get_image_lab_task(task_id: str) -> dict | None:
    """按 ID 取任务快照。"""
    with _LAB_TASK_LOCK:
        task = _LAB_TASKS.get((task_id or "").strip())
        return copy.deepcopy(task) if task else None


def list_image_lab_tasks(limit: int = 20) -> dict:
    """任务列表（最新在前，返回摘要）。"""
    with _LAB_TASK_LOCK:
        tasks = list(_LAB_TASKS.values())
    tasks.sort(key=lambda item: item.get("created_at", ""), reverse=True)
    safe_limit = max(1, min(int(limit or 20), _MAX_LAB_TASKS))
    summaries = [
        {
            "id": task.get("id", ""),
            "kind": task.get("kind", ""),
            "status": task.get("status", ""),
            "created_at": task.get("created_at", ""),
            "finished_at": task.get("finished_at", ""),
            "progress": copy.deepcopy(task.get("progress") or {}),
            "prompt": str((task.get("params") or {}).get("prompt", ""))[:120],
            "group_count": len(task.get("groups") or []),
            "image_count": task.get("image_count", 0),
            "error": task.get("error", ""),
        }
        for task in tasks[:safe_limit]
    ]
    return {"tasks": summaries, "total": len(tasks)}


def cancel_image_lab_task(task_id: str) -> bool:
    """请求取消任务；已完成返回 False。"""
    with _LAB_TASK_LOCK:
        task = _LAB_TASKS.get((task_id or "").strip())
        if task is None:
            return False
        if task.get("status") in ("succeeded", "partial", "failed", "canceled"):
            return False
        task["cancel_requested"] = True
    return True


def clear_image_lab_tasks() -> None:
    """清空内存任务表（测试用）。"""
    with _LAB_TASK_LOCK:
        _LAB_TASKS.clear()

import configparser
import requests
import os
import base64
import json
import io
import logging
import mimetypes
import tempfile
from typing import Any, Dict, Optional
from PIL import Image
import imageio.v2 as imageio
from nbot.core import (
    repair_mojibake_text,
    resolve_chat_completion_url,
    response_json_utf8,
)
from nbot.core.model_proxy import model_proxy_request_kwargs
from nbot.core.protocols import get_protocol
from nbot.core.sampling import request_extra_body
from nbot.web.secure_store import read_secure_json, write_secure_json

config_parser = configparser.ConfigParser()
config_parser.read('config.ini', encoding='utf-8')

api_key = config_parser.get('ApiKey', 'api_key', fallback="")
base_url = config_parser.get('ApiKey', 'base_url', fallback="")
model = config_parser.get('ApiKey', 'model', fallback="")
MAX_HISTORY_LENGTH = config_parser.getint('chat', 'MAX_HISTORY_LENGTH', fallback=20)
pic_model = config_parser.get('pic', 'model', fallback="")
search_api_key = config_parser.get('search', 'api_key', fallback="")
search_api_url = config_parser.get('search', 'api_url', fallback="")
video_api = config_parser.get('video', 'api_key', fallback="")
provider_type = config_parser.get('ApiKey', 'provider_type', fallback="openai_compatible")
supports_tools = config_parser.getboolean('ApiKey', 'supports_tools', fallback=True)
supports_reasoning = config_parser.getboolean('ApiKey', 'supports_reasoning', fallback=True)
supports_stream = config_parser.getboolean('ApiKey', 'supports_stream', fallback=True)

_log = logging.getLogger(__name__)


def _resolve_oauth_runtime(account_id: str):
    """解析 OAuth 账号的运行时凭据。

    返回 (access_token, extra_headers, remove_headers)。
    - access_token: 最新有效的 OAuth access_token（必要时自动刷新）
    - extra_headers: 需要追加/覆盖到 HTTP 请求头的字段
    - remove_headers: 需要从 HTTP 请求头移除的字段

    OAuthManager 未初始化或账号已删除时返回 (None, {}, set())。
    """
    if not account_id:
        return None, {}, set()
    try:
        from nbot.core.oauth import get_oauth_manager
        manager = get_oauth_manager()
        cred = manager.resolve_credential(account_id)
        return cred.access_token, dict(cred.extra_headers), set(cred.remove_headers)
    except Exception as e:
        _log.error(f"OAuth runtime credential resolve failed: {e}")
        return None, {}, set()


def resolve_runtime_api_key(configured_api_key: str = "", provider: str = "") -> str:
    provider_name = (provider or "").strip().lower()
    if provider_name == "minimax":
        return (
            os.getenv("MINIMAX_API_KEY")
            or os.getenv("API_KEY")
            or configured_api_key
        )
    if provider_name in {"anthropic", "claude"}:
        return (
            os.getenv("ANTHROPIC_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider_name in {"google", "gemini"}:
        return (
            os.getenv("GEMINI_API_KEY")
            or os.getenv("GOOGLE_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    if provider_name in {"openai", "openai_compatible", "custom", "deepseek"}:
        return (
            os.getenv("OPENAI_API_KEY")
            or configured_api_key
            or os.getenv("API_KEY")
        )
    return configured_api_key or os.getenv("API_KEY")


def _load_shared_web_ai_config() -> dict:
    data_dir = os.path.join("data", "web")
    models_file = os.path.join(data_dir, "ai_models.json")
    config_file = os.path.join(data_dir, "ai_config.json")

    try:
        if os.path.exists(models_file):
            models_data, was_plaintext = read_secure_json(models_file, data_dir, {})
            if was_plaintext:
                write_secure_json(models_file, data_dir, models_data)
            active_model_id = models_data.get("active_model_id")
            for item in models_data.get("models", []):
                if item.get("id") == active_model_id and item.get("enabled", True):
                    return item
    except Exception:
        pass

    try:
        if os.path.exists(config_file):
            config, was_plaintext = read_secure_json(config_file, data_dir, {})
            if was_plaintext:
                write_secure_json(config_file, data_dir, config)
            return config if isinstance(config, dict) else {}
    except Exception:
        pass

    return {}


def get_runtime_ai_config() -> dict:
    shared = _load_shared_web_ai_config()
    effective = {
        "base_url": shared.get("base_url") or base_url,
        "model": shared.get("model") or model,
        "provider_type": shared.get("provider_type")
        or shared.get("provider")
        or provider_type
        or "openai_compatible",
        "append_base_url_path": shared.get("append_base_url_path", True),
        "proxy_url": shared.get("proxy_url", ""),
        "stream": shared.get("stream", True),
        "supports_tools": shared.get("supports_tools", supports_tools),
        "supports_reasoning": shared.get("supports_reasoning", supports_reasoning),
        "supports_stream": shared.get("supports_stream", supports_stream),
        "purpose": shared.get("purpose", "chat"),
        "stop": shared.get("stop", ""),
        # 采样参数：未配置时为 None，协议层会跳过这些字段
        "temperature": shared.get("temperature"),
        "top_p": shared.get("top_p"),
        "max_tokens": shared.get("max_tokens"),
        "token_limit_field": shared.get("token_limit_field", ""),
    }
    effective["api_key"] = resolve_runtime_api_key(
        shared.get("api_key") or api_key,
        effective["provider_type"],
    )
    effective["input_price"] = shared.get("input_price")
    effective["output_price"] = shared.get("output_price")
    return effective


def refresh_runtime_ai_config() -> dict:
    global api_key, base_url, model, provider_type
    global supports_tools, supports_reasoning, supports_stream

    effective = get_runtime_ai_config()
    api_key = effective["api_key"]
    base_url = effective["base_url"]
    model = effective["model"]
    provider_type = effective["provider_type"]
    supports_tools = bool(effective["supports_tools"])
    supports_reasoning = bool(effective["supports_reasoning"])
    supports_stream = bool(effective["supports_stream"])

    client = globals().get("ai_client")
    if client is not None:
        client.api_key = api_key
        client.base_url = base_url
        client.model = model
        client.provider_type = provider_type
        client.append_base_url_path = bool(effective.get("append_base_url_path", True))
        client.proxy_url = effective.get("proxy_url", "")
        client.stream_enabled = bool(effective.get("stream", True))
        client.supports_tools = supports_tools
        client.supports_reasoning = supports_reasoning
        client.supports_stream = supports_stream
        client.stop = effective.get("stop", "")
        client.temperature = effective.get("temperature")
        client.top_p = effective.get("top_p")
        client.max_tokens = effective.get("max_tokens")
        client.token_limit_field = effective.get("token_limit_field", "")

    return effective


def apply_model_config(config: dict) -> None:
    """Push a specific model config to the global AIClient singleton.

    Used by the failover wrapper to swap models without reloading
    from file. Accepts a config dict with the same shape as
    get_runtime_ai_config() returns.
    """
    global api_key, base_url, model, provider_type
    global supports_tools, supports_reasoning, supports_stream

    api_key = config.get("api_key", "")
    base_url = config.get("base_url", "")
    model = config.get("model", "")
    provider_type = config.get("provider_type", "openai_compatible")
    supports_tools = bool(config.get("supports_tools", True))
    supports_reasoning = bool(config.get("supports_reasoning", True))
    supports_stream = bool(config.get("supports_stream", True))

    client = globals().get("ai_client")
    if client is not None:
        client.api_key = api_key
        client.base_url = base_url
        client.model = model
        client.provider_type = provider_type
        client.append_base_url_path = bool(config.get("append_base_url_path", True))
        client.proxy_url = config.get("proxy_url", "")
        client.stream_enabled = bool(config.get("stream", True))
        client.supports_tools = supports_tools
        client.supports_reasoning = supports_reasoning
        client.supports_stream = supports_stream
        client.stop = config.get("stop", "")
        client.temperature = config.get("temperature")
        client.top_p = config.get("top_p")
        client.max_tokens = config.get("max_tokens")
        client.token_limit_field = config.get("token_limit_field", "")
        # OAuth 账号绑定（chat_completion 时解析 fresh access_token）
        client.oauth_account_id = config.get("oauth_account_id", "")


user_messages = {}
group_messages = {}

try:
    with open("saved_message/user_messages.json", "r", encoding="utf-8") as f:
        user_messages = json.load(f)
    with open("saved_message/group_messages.json", "r", encoding="utf-8") as f:
        group_messages = json.load(f)
except FileNotFoundError:
    os.makedirs("saved_message", exist_ok=True)


def _data_url_to_tempfile(data_url: str) -> Optional[str]:
    """将 data: URL 落盘为临时文件，供 STT 等本地处理使用。"""
    try:
        header, encoded = data_url.split(",", 1)
        mime = header[5:].split(";", 1)[0].strip() or "audio/mpeg"
        ext = mimetypes.guess_extension(mime) or ".mp3"
        with tempfile.NamedTemporaryFile(delete=False, suffix=ext) as tmp_file:
            tmp_file.write(base64.b64decode(encoded))
            return tmp_file.name
    except Exception as exc:
        _log.warning("[Audio] failed to decode data URL: %s", exc)
        return None


def _read_media_bytes(url: str) -> bytes:
    """读取媒体内容，支持 data URL、http(s) 与本地路径。"""
    if url.startswith("data:"):
        return base64.b64decode(url.split(",", 1)[1])
    if url.startswith(("http://", "https://")):
        res = requests.get(url, timeout=10)
        res.raise_for_status()
        return res.content
    path = url[7:] if url.startswith("file://") else url
    with open(path, "rb") as fh:
        return fh.read()


def _ensure_remote_media_url(url: str) -> str:
    """把本地路径转换为 data URL，保证视觉模型可访问。"""
    if url.startswith(("http://", "https://", "data:")):
        return url
    try:
        data = _read_media_bytes(url)
        mime = mimetypes.guess_type(url)[0] or "application/octet-stream"
        return f"data:{mime};base64," + base64.b64encode(data).decode("utf-8")
    except Exception:
        return url


def _is_gemini_model(provider_type: str, model_name: str) -> bool:
    """判断模型是否为 Gemini 系列。

    通过 provider_type（如 gemini_native）或模型名前缀（如 gemini-2.5-flash）判断。
    """
    pt = (provider_type or "").lower()
    mn = (model_name or "").lower()
    if "gemini" in pt and pt != "":
        return True
    # 常见 Gemini 模型名模式
    gemini_prefixes = ("gemini", "models/gemini")
    return any(mn.startswith(p) for p in gemini_prefixes)


class AIClient:
    def __init__(self, api_key: str, base_url: str, model: str, pic_model: str,
                 search_api_key: str, search_api_url: str, video_api: str,
                 provider_type: str = "openai_compatible",
                 append_base_url_path: bool = True,
                 stream_enabled: bool = True,
                 supports_tools: bool = True,
                 supports_reasoning: bool = True,
                 supports_stream: bool = True,
                 proxy_url: str = ""):
        self.api_key = api_key
        self.base_url = base_url
        self.model = model
        self.pic_model = pic_model
        self.search_api_key = search_api_key
        self.search_api_url = search_api_url
        self.video_api = video_api
        self.provider_type = provider_type or "openai_compatible"
        self.append_base_url_path = bool(append_base_url_path)
        self.stream_enabled = bool(stream_enabled)
        self.supports_tools = bool(supports_tools)
        self.supports_reasoning = bool(supports_reasoning)
        self.supports_stream = bool(supports_stream)
        self.proxy_url = proxy_url or ""
        # 采样参数（由 refresh_runtime_ai_config / apply_model_config 同步）
        self.stop: str = ""
        self.temperature = None
        self.top_p = None
        self.max_tokens = None
        self.token_limit_field: str = ""
        # OAuth 账号绑定（运行时由 _resolve_oauth_runtime 解析 fresh access_token）
        self.oauth_account_id: str = ""

    def sampling_config(self) -> dict:
        """当前客户端的采样参数快照，供协议层生成 extra_body。"""
        return {
            "stop": getattr(self, "stop", ""),
            "temperature": getattr(self, "temperature", None),
            "top_p": getattr(self, "top_p", None),
            "max_tokens": getattr(self, "max_tokens", None),
            "token_limit_field": getattr(self, "token_limit_field", ""),
        }

    @staticmethod
    def clean_response(content: str) -> str:
        if not content:
            return ""
        content = repair_mojibake_text(content)
        content = content.strip()
        if content.startswith("```json"):
            content = content[7:]
            if content.endswith("```"):
                content = content[:-3]
        elif content.startswith("```"):
            content = content[3:]
            if content.endswith("```"):
                content = content[:-3]
        return content.strip()

    def _build_completion_response(self, data, protocol, model_name, base_url, provider_type):
        """Parse API response and build the mock response object."""
        normalized = protocol.parse_response(
            data,
            model=model_name or "",
            base_url=base_url or "",
            provider_type=provider_type,
        )
        content = normalized.content
        usage = normalized.usage or {}
        prompt_tokens = usage.get("prompt_tokens", 0)
        completion_tokens = usage.get("completion_tokens", 0)
        total_tokens = usage.get("total_tokens", prompt_tokens + completion_tokens)

        Message = type("Message", (), {})
        Choice = type("Choice", (), {})
        Usage = type("Usage", (), {})
        Resp = type("Resp", (), {})
        msg_obj = Message()
        msg_obj.content = content
        choice_obj = Choice()
        choice_obj.message = msg_obj
        usage_obj = Usage()
        usage_obj.prompt_tokens = prompt_tokens
        usage_obj.completion_tokens = completion_tokens
        usage_obj.total_tokens = total_tokens
        resp_obj = Resp()
        resp_obj.choices = [choice_obj]
        resp_obj.usage = usage_obj
        return resp_obj

    def chat_completion(self, messages, model: str = None, stream: bool = False,
                        purpose: str = "chat"):
        """发送聊天补全请求，支持按 purpose 隔离故障转移。

        Args:
            messages: 消息列表
            model: 指定模型名，为 None 时走故障转移
            stream: 是否流式
            purpose: 模型用途 (chat/vision/video/tts/stt/embedding)，
                    决定故障转移时使用哪个模型队列
        """
        stream = bool(stream and self.supports_stream and self.stream_enabled)
        url_base = (self.base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")

        protocol = get_protocol(self.provider_type)
        model_name = model or self.model

        url = protocol.resolve_url(
            self.base_url,
            model=model_name or "",
            append_base_url_path=self.append_base_url_path,
            api_key=self.api_key,
        )
        headers = protocol.build_headers(self.api_key, stream=stream)

        # OAuth 运行时凭据注入：access_token + extra_headers + remove_headers
        if getattr(self, "oauth_account_id", ""):
            _oauth_token, _oauth_extra, _oauth_remove = _resolve_oauth_runtime(self.oauth_account_id)
            if _oauth_token:
                # 用 fresh access_token 重建 headers（确保 Authorization 用最新 token）
                headers = protocol.build_headers(_oauth_token, stream=stream)
                if _oauth_extra:
                    headers.update(_oauth_extra)
                if _oauth_remove:
                    for _h in _oauth_remove:
                        headers.pop(_h, None)
                # 同步更新 self.api_key 以供 payload 等下游使用
                self.api_key = _oauth_token

        payload = protocol.build_payload(
            model_name,
            messages,
            stream=stream,
            base_url=self.base_url,
            provider_type=self.provider_type,
            extra_body=request_extra_body(self.sampling_config()) or None,
        )

        if stream:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                stream=True,
                timeout=300,
                **model_proxy_request_kwargs(self.proxy_url),
            )
            resp.raise_for_status()
            return self._stream_response_generic(resp, protocol)

        # ---- 非流式调用：支持故障转移 ----
        # 有显式 model 参数时跳过 failover（调用方明确选择了模型）
        if model is not None:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=120,
                **model_proxy_request_kwargs(self.proxy_url),
            )
            resp.raise_for_status()
            data = response_json_utf8(resp)
            return self._build_completion_response(
                data, protocol, model_name, self.base_url, self.provider_type,
            )

        # 无显式 model：尝试故障转移（按 purpose 隔离队列）
        from nbot.core.failover import (
            get_failover_state, classify_http_error, _extract_status_code,
        )
        from nbot.web.utils.config_loader import get_model_configs_by_purpose

        failover = get_failover_state()
        model_configs = get_model_configs_by_purpose(purpose)

        if not model_configs or len(model_configs) <= 1:
            # 单模型或无配置：原逻辑 + 健康追踪
            _mid = model_configs[0].get("model_id", "") if model_configs else ""
            try:
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=120,
                    **model_proxy_request_kwargs(self.proxy_url),
                )
                resp.raise_for_status()
                data = response_json_utf8(resp)
                if _mid:
                    failover.record_success(_mid)
                return self._build_completion_response(
                    data, protocol, model_name, self.base_url, self.provider_type,
                )
            except Exception as e:
                if _mid:
                    failover.record_failure(_mid, _extract_status_code(e))
                raise

        # 多模型：按优先级遍历
        attempted = set()
        last_error = None
        for _ in range(len(model_configs)):
            cfg = failover.select_model(model_configs, exclude_ids=attempted)
            if cfg is None:
                break
            mid = cfg.get("model_id", "")
            mname = cfg.get("model", "")
            attempted.add(mid)

            cfg_pt = cfg.get("provider_type", "openai_compatible")
            cfg_protocol = get_protocol(cfg_pt)
            # OAuth 运行时凭据注入（failover 队列中的 OAuth 绑定模型）
            cfg_api_key = cfg.get("api_key", "")
            cfg_extra_headers = {}
            cfg_remove_headers = set()
            cfg_oauth_id = cfg.get("oauth_account_id", "")
            if cfg_oauth_id:
                _otoken, _oextra, _oremove = _resolve_oauth_runtime(cfg_oauth_id)
                if _otoken:
                    cfg_api_key = _otoken
                    cfg_extra_headers = _oextra
                    cfg_remove_headers = _oremove
            cfg_url = cfg_protocol.resolve_url(
                cfg.get("base_url", ""),
                model=mname or "",
                append_base_url_path=cfg.get("append_base_url_path", True),
                api_key=cfg_api_key,
            )
            cfg_headers = cfg_protocol.build_headers(cfg_api_key, stream=False)
            if cfg_extra_headers:
                cfg_headers.update(cfg_extra_headers)
            if cfg_remove_headers:
                for _h in cfg_remove_headers:
                    cfg_headers.pop(_h, None)
            cfg_payload = cfg_protocol.build_payload(
                mname,
                messages,
                stream=False,
                base_url=cfg.get("base_url", ""),
                provider_type=cfg_pt,
                extra_body=request_extra_body(cfg) or None,
            )
            try:
                # failover_timeout: 0 表示使用默认 120s
                request_timeout = cfg.get("failover_timeout", 0) or 120
                resp = requests.post(
                    cfg_url,
                    json=cfg_payload,
                    headers=cfg_headers,
                    timeout=request_timeout,
                    **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
                )
                resp.raise_for_status()
                data = response_json_utf8(resp)
                failover.record_success(mid)
                return self._build_completion_response(
                    data, cfg_protocol, mname, cfg.get("base_url", ""), cfg_pt,
                )
            except Exception as e:
                status = _extract_status_code(e)
                category = classify_http_error(status)
                if category == "config":
                    raise
                failover.record_failure(mid, status)
                last_error = e
                continue

        raise last_error or RuntimeError(f"All {purpose} models failed")

    def _stream_response_generic(self, resp, protocol):
        """处理流式响应，使用协议适配器解析chunk，返回一个生成器"""
        import json as _json

        for line in resp.iter_lines(chunk_size=1):
            if not line:
                continue

            line_text = line.decode('utf-8') if isinstance(line, bytes) else line

            if line_text.startswith('event: '):
                continue

            if not line_text.startswith('data: '):
                continue

            data_str = line_text[6:].strip()
            if data_str == '[DONE]':
                break

            try:
                data = _json.loads(data_str)
                parsed = protocol.parse_stream_chunk(data)
                if parsed and parsed.get("type") == "content":
                    yield repair_mojibake_text(parsed.get("content", ""))
            except _json.JSONDecodeError:
                continue


    def summarize_text(self, system_prompt: str, user_prompt: str, model: str = None) -> str:
        response = self.chat_completion(
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            model=model,
            stream=False,
        )
        # 记录 token 用量
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_UTILITY
            usage = getattr(response, "usage", None)
            if usage:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    model=model or self.model or "",
                    channel_type="utility",
                    source="utility",
                    purpose=PURPOSE_UTILITY,
                )
        except Exception:
            pass
        return self.clean_response(response.choices[0].message.content)

    def describe_image(self, image_url: str, text: str = None) -> Optional[str]:
        """图片识别，在 vision 模型队列内完成故障转移，不跨 purpose 回退。"""
        _url_preview = image_url[:80]
        _truncated = (
            "...(base64已省略)"
            if image_url.startswith("data:") and len(image_url) > 80
            else ""
        )
        print(f"[图片识别] 开始识别图片, URL: {_url_preview}{_truncated}")

        # 构建 vision 请求的消息体
        system_prompt = "请详细描述这张图片的内容。"
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": image_url}},
                    {"type": "text", "text": text or system_prompt}
                ]
            }
        ]

        # 获取 vision 模型队列，按优先级尝试（含 failover）
        from nbot.web.utils.config_loader import get_model_configs_by_purpose
        from nbot.core.failover import (
            get_failover_state, classify_http_error, _extract_status_code,
        )
        from nbot.core.model_adapter import (
            resolve_chat_completion_url, build_chat_completion_payload,
        )

        vision_configs = get_model_configs_by_purpose("vision")
        failover = get_failover_state()

        if vision_configs:
            # 有 vision 模型配置：在队列内按优先级遍历
            attempted = set()
            last_error = None
            for _ in range(len(vision_configs)):
                cfg = failover.select_model(vision_configs, exclude_ids=attempted)
                if cfg is None:
                    break
                mid = cfg.get("model_id", "")
                mname = cfg.get("model", "")
                attempted.add(mid)

                try:
                    url = resolve_chat_completion_url(
                        cfg.get("base_url", ""),
                        model=mname,
                        provider_type=cfg.get("provider_type", "openai_compatible"),
                        append_base_url_path=cfg.get("append_base_url_path", True),
                    )
                    headers = {
                        "Authorization": f"Bearer {cfg.get('api_key', '')}",
                        "Content-Type": "application/json"
                    }
                    payload = build_chat_completion_payload(
                        mname, messages,
                        base_url=cfg.get("base_url", ""),
                        provider_type=cfg.get("provider_type", "openai_compatible"),
                    )
                    request_timeout = cfg.get("failover_timeout", 0) or 60
                    response = requests.post(
                        url,
                        json=payload,
                        headers=headers,
                        timeout=request_timeout,
                        **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
                    )
                    response.raise_for_status()
                    resp_data = response_json_utf8(response)
                    result = self.clean_response(
                        resp_data["choices"][0]["message"]["content"]
                    )
                    failover.record_success(mid)
                    print(f"[图片识别] 识别成功(vision队列), 结果: {result[:100]}...")

                    # 记录 token 用量
                    try:
                        from nbot.core.token_stats import get_token_stats_manager, PURPOSE_VISION
                        usage_data = resp_data.get("usage", {})
                        if usage_data:
                            stats_mgr = get_token_stats_manager()
                            stats_mgr.record_usage(
                                prompt_tokens=usage_data.get("prompt_tokens", 0) or 0,
                                completion_tokens=usage_data.get("completion_tokens", 0) or 0,
                                total_tokens=usage_data.get("total_tokens", 0) or 0,
                                model=mname or "",
                                channel_type="vision",
                                source="vision",
                                purpose=PURPOSE_VISION,
                            )
                    except Exception:
                        pass

                    return result
                except Exception as e:
                    status = _extract_status_code(e)
                    category = classify_http_error(status)
                    if category == "config":
                        raise
                    failover.record_failure(mid, status)
                    last_error = e
                    continue

            # vision 队列全部失败
            print(f"[图片识别] vision模型队列全部失败({len(attempted)}个), 错误: {last_error}")
            return None

        # 无 vision 模型配置：使用 legacy 的 pic_model 作为单模型请求（不走 chat 队列）
        if self.pic_model:
            try:
                response = self.chat_completion(
                    messages=messages, model=self.pic_model, stream=False,
                )
                return self.clean_response(response.choices[0].message.content)
            except Exception as e:
                print(f"[图片识别] pic_model请求失败: {e}")
                return None

        print("[图片识别] 无可用模型配置")
        return None

    def describe_audio(self, audio_url: str) -> Optional[str]:
        """语音识别：把音频附件转写为文字，供消息预处理注入上下文。"""
        from nbot.services import stt as stt_service

        if not audio_url:
            return None

        temp_path = None
        try:
            if audio_url.startswith("data:"):
                temp_path = _data_url_to_tempfile(audio_url)
                if not temp_path:
                    return None
                text = stt_service.transcribe(temp_path)
            elif audio_url.startswith(("http://", "https://")):
                text = stt_service.transcribe_from_url(audio_url)
            else:
                path = audio_url[7:] if audio_url.startswith("file://") else audio_url
                if not os.path.exists(path):
                    print(f"[语音识别] 音频文件不存在: {path}")
                    return None
                text = stt_service.transcribe(path)

            if not text or not str(text).strip():
                return None
            result = str(text).strip()
            print(f"[语音识别] 转写成功: {result[:100]}...")
            return result
        except Exception as exc:
            print(f"[语音识别] 转写失败: {exc}")
            return None
        finally:
            if temp_path:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass

    def gif_to_mp4_data_url(self, image_url: str, fps: int = 10) -> str:
        try:
            buf_in = io.BytesIO(_read_media_bytes(image_url))
            frames = imageio.mimread(buf_in, format="gif")
            if not frames:
                return ""
            buf_out = io.BytesIO()
            imageio.mimsave(buf_out, frames, format="ffmpeg", fps=fps)
            mp4_bytes = buf_out.getvalue()
            if not mp4_bytes:
                return ""
            b64 = base64.b64encode(mp4_bytes).decode("utf-8")
            return "data:video/mp4;base64," + b64
        except Exception:
            return ""

    def describe_gif(self, image_url: str, max_frames: int = 10) -> Optional[str]:
        try:
            img = Image.open(io.BytesIO(_read_media_bytes(image_url)))
            total = getattr(img, "n_frames", 1)
            if total <= 1:
                return self.describe_image(
                    _ensure_remote_media_url(image_url),
                    "请描述这个图片的内容，仅作描述，不要分析内容",
                )

            content_list = []
            used = set()
            count = min(max_frames, total)
            for i in range(count):
                idx = int(i * total / count)
                if idx >= total:
                    idx = total - 1
                if idx in used:
                    continue
                used.add(idx)
                try:
                    img.seek(idx)
                    frame = img.convert("RGB")
                    buf = io.BytesIO()
                    frame.save(buf, format="PNG")
                    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
                    data_url = "data:image/png;base64," + b64
                    content_list.append({
                        "type": "image_url",
                        "image_url": {
                            "url": data_url
                        }
                    })
                except Exception:
                    continue

            if not content_list:
                return None

            content_list.append({
                "type": "text",
                "text": "以上是一个 GIF 动画的连续帧序列。请作为一个整体分析这个动画，描述其中发生的动作、情节以及角色的情绪。"
            })

            messages = [
                {
                    "role": "user",
                    "content": content_list
                }
            ]

            # 在 vision 模型队列内完成 GIF 帧序列的描述（不回退到 chat 队列）
            from nbot.web.utils.config_loader import get_model_configs_by_purpose
            from nbot.core.failover import (
                get_failover_state, classify_http_error, _extract_status_code,
            )
            from nbot.core.model_adapter import (
                resolve_chat_completion_url, build_chat_completion_payload,
            )

            vision_configs = get_model_configs_by_purpose("vision")
            failover = get_failover_state()

            if vision_configs:
                attempted = set()
                last_error = None
                for _ in range(len(vision_configs)):
                    cfg = failover.select_model(vision_configs, exclude_ids=attempted)
                    if cfg is None:
                        break
                    mid = cfg.get("model_id", "")
                    mname = cfg.get("model", "")
                    attempted.add(mid)

                    try:
                        url = resolve_chat_completion_url(
                            cfg.get("base_url", ""),
                            model=mname,
                            provider_type=cfg.get("provider_type", "openai_compatible"),
                            append_base_url_path=cfg.get("append_base_url_path", True),
                        )
                        headers = {
                            "Authorization": f"Bearer {cfg.get('api_key', '')}",
                            "Content-Type": "application/json"
                        }
                        payload = build_chat_completion_payload(
                            mname, messages,
                            base_url=cfg.get("base_url", ""),
                            provider_type=cfg.get("provider_type", "openai_compatible"),
                        )
                        request_timeout = cfg.get("failover_timeout", 0) or 60
                        response = requests.post(
                            url,
                            json=payload,
                            headers=headers,
                            timeout=request_timeout,
                            **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
                        )
                        response.raise_for_status()
                        result = self.clean_response(
                            response_json_utf8(response)["choices"][0]["message"]["content"]
                        )
                        failover.record_success(mid)
                        return result
                    except Exception as e:
                        status = _extract_status_code(e)
                        category = classify_http_error(status)
                        if category == "config":
                            raise
                        failover.record_failure(mid, status)
                        last_error = e
                        continue

                # vision 队列全部失败
                print(f"[GIF识别] vision模型队列全部失败({len(attempted)}个), 错误: {last_error}")
                return None

            # 无 vision 配置：尝试 pic_model 单模型请求
            if self.pic_model:
                try:
                    response = self.chat_completion(
                        messages=messages, model=self.pic_model, stream=False,
                    )
                    return self.clean_response(response.choices[0].message.content)
                except Exception:
                    return None

            return None
        except Exception:
            return None

    def describe_gif_as_video(self, image_url: str) -> Optional[str]:
        data_url = self.gif_to_mp4_data_url(image_url)
        if data_url:
            result = self.describe_video(data_url)
            if result:
                return result
        return self.describe_gif(image_url)

    def describe_webpage_html(self, html: str) -> Optional[str]:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"请描述这个网页的内容，仅分析网页的主体内容，忽略网页的其他内容和技术相关的细节；仅返回主体内容的描述：{html}"
                    }
                ]
            }
        ]
        response = self.chat_completion(messages=messages, stream=False)
        try:
            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_UTILITY
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=self.model or "",
                        channel_type="utility",
                        source="utility",
                        purpose=PURPOSE_UTILITY,
                    )
            except Exception:
                pass
            return self.clean_response(response.choices[0].message.content)
        except Exception:
            return None

    def analyze_json(self, content: str) -> str:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"请分析这个json字符串的内容；如果有链接，则还需列出最重要的一个链接，忽略其他链接：{content}"
                    }
                ]
            }
        ]
        response = self.chat_completion(messages=messages, stream=False)
        try:
            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_UTILITY
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=self.model or "",
                        channel_type="utility",
                        source="utility",
                        purpose=PURPOSE_UTILITY,
                    )
            except Exception:
                pass
            return self.clean_response(response.choices[0].message.content)
        except Exception:
            return ""

    def should_search(self, content: str) -> bool:
        messages = [
            {
                "role": "user",
                "content": [
                    {
                        "type": "text",
                        "text": f"请判断这个内容AI是否需要搜索才能获取准确且最新的回答；如果需要搜索，则只返回1；如果不需要搜索，则只返回0：{content}"
                    }
                ]
            }
        ]
        try:
            response = self.chat_completion(messages=messages, stream=False)
            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_DECISION
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=self.model or "",
                        channel_type="decision",
                        source="decision",
                        purpose=PURPOSE_DECISION,
                    )
            except Exception:
                pass
            content = self.clean_response(response.choices[0].message.content)
            print(f"[DEBUG] should_search API响应: {content}")
            return int(content) == 1
        except Exception as e:
            print(f"[DEBUG] should_search 错误: {e}")
            return False

    def should_reply(self, content: str) -> float:
        messages = [
            {
                "role": "system",
                "content": "你是一个对话助手，需要根据群聊上下文和机器人的人设来判断是否应该回复当前消息。请输出 0 到 1 之间的一个小数，表示'应该回复程度'：0 表示完全不应该回复，1 表示非常应该回复，只输出这个数字，不要输出其他内容。"
            },
            {
                "role": "user",
                "content": content
            }
        ]
        response = self.chat_completion(messages=messages, stream=False)
        try:
            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_DECISION
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=self.model or "",
                        channel_type="decision",
                        source="decision",
                        purpose=PURPOSE_DECISION,
                    )
            except Exception:
                pass
            score_str = self.clean_response(response.choices[0].message.content)
            score = float(score_str)
            if score < 0:
                score = 0.0
            if score > 1:
                score = 1.0
            return score
        except Exception:
            return 0.0

    def search(self, content: str) -> str:
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.search_api_key}"
        }
        data = {
            "query": content,
            "query_rewrite": True,
            "top_k": 6
        }
        try:
            response = requests.post(self.search_api_url, headers=headers, json=data)
            return str(response_json_utf8(response)["result"]["search_result"])
        except Exception:
            return ""

    def describe_video(self, video_url: str, text: str = None) -> Optional[str]:
        """视频识别，在 video 模型队列内完成故障转移，不跨 purpose 回退。

        对 Gemini 模型自动使用原生 inline_data 格式（而非 OpenAI video_url 格式），
        因为多数 OpenAI 兼容代理无法正确转发 video_url 中的视频内容。
        """
        _url_preview = video_url[:80]
        _truncated = (
            "...(base64已省略)"
            if video_url.startswith("data:") and len(video_url) > 80
            else ""
        )
        print(f"[视频识别] 开始识别视频, URL: {_url_preview}{_truncated}")

        system_prompt = "请分析这个视频的内容。"
        # OpenAI 格式的消息（用于非 Gemini 模型）
        openai_messages = [
            {
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": video_url}},
                    {"type": "text", "text": text or system_prompt}
                ]
            }
        ]

        # 获取 video 模型队列，按优先级尝试（含 failover）
        from nbot.web.utils.config_loader import get_model_configs_by_purpose
        from nbot.core.failover import (
            get_failover_state, classify_http_error, _extract_status_code,
        )

        video_configs = get_model_configs_by_purpose("video")
        failover = get_failover_state()

        if video_configs:
            attempted = set()
            last_error = None
            for _ in range(len(video_configs)):
                cfg = failover.select_model(video_configs, exclude_ids=attempted)
                if cfg is None:
                    break
                mid = cfg.get("model_id", "")
                mname = cfg.get("model", "")
                ptype = cfg.get("provider_type", "openai_compatible")
                attempted.add(mid)

                try:
                    # 判断是否为 Gemini 模型 → 使用原生 inline_data 格式
                    is_gemini = _is_gemini_model(ptype, mname)
                    if is_gemini:
                        result = self._call_gemini_video(video_url, text, system_prompt, cfg)
                    else:
                        result = self._call_openai_video(openai_messages, cfg)
                    if result is not None:
                        failover.record_success(mid)
                        print(f"[视频识别] 识别成功(video队列), 结果: {result[:100]}...")
                        return result
                except Exception as e:
                    status = _extract_status_code(e)
                    category = classify_http_error(status)
                    if category == "config":
                        raise
                    failover.record_failure(mid, status)
                    last_error = e
                    continue

            # video 队列全部失败
            print(f"[视频识别] video模型队列全部失败({len(attempted)}个), 错误: {last_error}")
            return None

        # 无 video 模型配置：使用 AIClient 自身属性作为单模型请求
        try:
            is_gemini = _is_gemini_model(self.provider_type, model)
            if is_gemini:
                cfg = {
                    "base_url": self.base_url,
                    "model": model,
                    "api_key": self.api_key,
                    "provider_type": self.provider_type,
                    "append_base_url_path": self.append_base_url_path,
                    "failover_timeout": 0,
                }
                result = self._call_gemini_video(video_url, text, system_prompt, cfg)
            else:
                url = resolve_chat_completion_url(
                    self.base_url,
                    model="zai-org/GLM-4.6V",
                    provider_type=self.provider_type,
                    append_base_url_path=self.append_base_url_path,
                )
                payload = {
                    "model": "zai-org/GLM-4.6V",
                    "messages": openai_messages
                }
                headers = {
                    "Authorization": f"Bearer {self.api_key}",
                    "Content-Type": "application/json"
                }
                response = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=120,
                    **model_proxy_request_kwargs(self.proxy_url),
                )
                response.raise_for_status()
                result = self.clean_response(
                    response_json_utf8(response)["choices"][0]["message"]["content"]
                )
            print(f"[视频识别] 识别成功(默认配置), 结果: {result[:100]}..." if result else "[视频识别] 默认配置返回空")
            return result
        except Exception as e:
            print(f"[视频识别] 默认配置请求失败: {e}")
            return None

    def _call_openai_video(self, messages: list, cfg: dict) -> Optional[str]:
        """使用 OpenAI 兼容格式发送视频识别请求。"""
        mname = cfg.get("model", "")
        url = resolve_chat_completion_url(
            cfg.get("base_url", ""),
            model=mname,
            provider_type=cfg.get("provider_type", "openai_compatible"),
            append_base_url_path=cfg.get("append_base_url_path", True),
        )
        payload = {"model": mname, "messages": messages}
        headers = {
            "Authorization": f"Bearer {cfg.get('api_key', '')}",
            "Content-Type": "application/json",
        }
        request_timeout = cfg.get("failover_timeout", 0) or 120
        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=request_timeout,
            **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
        )
        response.raise_for_status()
        resp_data = response_json_utf8(response)
        result = self.clean_response(
            resp_data["choices"][0]["message"]["content"]
        )

        # 记录 token 用量
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_VISION
            usage_data = resp_data.get("usage", {})
            if usage_data:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=usage_data.get("prompt_tokens", 0) or 0,
                    completion_tokens=usage_data.get("completion_tokens", 0) or 0,
                    total_tokens=usage_data.get("total_tokens", 0) or 0,
                    model=mname or "",
                    channel_type="vision",
                    source="vision",
                    purpose=PURPOSE_VISION,
                )
        except Exception:
            pass

        return result

    def _call_gemini_video(self, video_url: str, text: str, system_prompt: str, cfg: dict) -> Optional[str]:
        """使用 Gemini 原生 generateContent 格式发送视频识别请求。

        Gemini 原生格式使用 inline_data 而非 OpenAI 的 video_url：
          {"inline_data": {"mime_type": "video/mp4", "data": "<base64>"}}
        """
        from nbot.core.protocols.gemini_native import GeminiNativeProtocol

        api_key = cfg.get("api_key", "")
        base_url = (cfg.get("base_url", "") or "").rstrip("/")
        mname = cfg.get("model", "")

        # 从 data URL 中提取 MIME 类型和 base64 数据
        mime_type = "video/mp4"
        b64_data = ""
        if video_url.startswith("data:"):
            # data:video/mp4;base64,xxxxx
            header_part, _, b64_data = video_url.partition(",")
            if ";" in header_part:
                mime_type = header_part.split(";")[0].replace("data:", "")
        elif video_url.startswith(("http://", "https://")):
            # 外部 URL 使用 file_data 格式（Gemini 支持直接下载）
            b64_data = ""  # 不需要 base64

        protocol = GeminiNativeProtocol()

        # 构建 Gemini 原生 payload
        if b64_data:
            # 内嵌 base64 数据（<20MB 视频）
            contents = [{
                "parts": [
                    {"inline_data": {"mime_type": mime_type, "data": b64_data}},
                    {"text": text or system_prompt},
                ]
            }]
        elif video_url.startswith(("http://", "https://")):
            # 外部 URL（Gemini 会自动下载）
            contents = [{
                "parts": [
                    {"file_data": {"file_uri": video_url, "mime_type": mime_type}},
                    {"text": text or system_prompt},
                ]
            }]
        else:
            print(f"[视频识别] Gemini: 无法处理的视频URL格式: {video_url[:50]}")
            return None

        payload: Dict[str, Any] = {"contents": contents}

        # 解析 URL：支持多种 Gemini 端点格式
        if ":generateContent" in base_url or ":streamGenerateContent" in base_url:
            url = base_url.replace(":streamGenerateContent", ":generateContent")
        elif "/models/" in base_url:
            url = f"{base_url}:generateContent"
        elif base_url.endswith("/v1beta") or base_url.endswith("/v1"):
            url = f"{base_url}/models/{mname}:generateContent"
        else:
            url = f"{base_url}/v1beta/models/{mname}:generateContent"

        # Gemini 认证方式：API Key 通过 URL 参数或 x-goog-api-key 头
        if api_key:
            separator = "&" if "?" in url else "?"
            url = f"{url}{separator}key={api_key}"
        headers = {"Content-Type": "application/json"}

        request_timeout = cfg.get("failover_timeout", 0) or 120
        print(f"[视频识别] Gemini原生格式请求: {mname}, inline_data={bool(b64_data)}, "
              f"size={len(b64_data) if b64_data else 'URL'}")

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=request_timeout,
            **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
        )
        response.raise_for_status()
        data = response_json_utf8(response)

        # 解析 Gemini 响应格式: candidates[0].content.parts[0].text
        candidates = data.get("candidates", [])
        if not candidates:
            feedback = data.get("promptFeedback", {})
            block_reason = feedback.get("blockReason", "")
            if block_reason:
                return f"[Gemini 安全拦截: {block_reason}]"
            return None

        content_parts = candidates[0].get("content", {}).get("parts", [])
        result = "".join(p.get("text", "") for p in content_parts if "text" in p)

        # 记录 token 用量（Gemini 格式）
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_VISION
            usage_metadata = data.get("usageMetadata", {})
            if usage_metadata:
                stats_mgr = get_token_stats_manager()
                stats_mgr.record_usage(
                    prompt_tokens=usage_metadata.get("promptTokenCount", 0) or 0,
                    completion_tokens=usage_metadata.get("candidatesTokenCount", 0) or 0,
                    total_tokens=usage_metadata.get("totalTokenCount", 0) or 0,
                    model=mname or "",
                    channel_type="vision",
                    source="vision",
                    purpose=PURPOSE_VISION,
                )
        except Exception:
            pass

        return self.clean_response(result) if result else None


ai_client = AIClient(
    api_key=api_key,
    base_url=base_url,
    model=model,
    pic_model=pic_model,
    search_api_key=search_api_key,
    search_api_url=search_api_url,
    video_api=video_api,
    provider_type=provider_type,
    append_base_url_path=True,
    stream_enabled=True,
    supports_tools=supports_tools,
    supports_reasoning=supports_reasoning,
    supports_stream=supports_stream,
)

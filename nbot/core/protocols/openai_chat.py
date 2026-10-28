"""
OpenAI Chat Completions Protocol (v1/chat/completions)

Handles the standard OpenAI-compatible chat completions API used by
OpenAI, DeepSeek, SiliconFlow, and other compatible providers.
"""

from typing import Any, Callable, Dict, List, Optional

from nbot.core.model_adapter import (
    NormalizedModelResponse,
    _stringify_message_content,
    infer_provider_profile,
    normalize_chat_completion_data,
    normalize_messages_for_provider,
    repair_mojibake_text,
    sanitize_messages_for_chat_api,
)
from nbot.core.protocols.base import ModelProtocol
from nbot.core.sampling import (
    is_reasoning_model,
    normalize_max_tokens,
    resolve_token_limit,
    token_limit_field,
)


class OpenAIChatProtocol(ModelProtocol):
    """OpenAI Chat Completions protocol (v1/chat/completions)."""

    @property
    def name(self) -> str:
        return "openai_chat"

    @property
    def display_name(self) -> str:
        return "OpenAI Chat Completions"

    @property
    def url_suffix(self) -> str:
        return "/chat/completions"

    def resolve_url(
        self,
        base_url: str,
        model: str = "",
        *,
        append_base_url_path: bool = True,
        **opts: Any,
    ) -> str:
        url_base = (base_url or "").rstrip("/")
        if not url_base:
            raise ValueError("base_url 未配置")
        if not append_base_url_path:
            return url_base
        if "/chat/completions" in url_base or "/chatcompletion" in url_base:
            return url_base
        if url_base.endswith("/v1"):
            return f"{url_base}/chat/completions"
        return f"{url_base}/chat/completions"

    def build_headers(self, api_key: str, *, stream: bool = False) -> Dict[str, str]:
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        if stream:
            headers["Accept"] = "text/event-stream"
            headers["Cache-Control"] = "no-cache"
        return headers

    def build_payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = "auto",
        stream: bool = False,
        max_tokens: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        base_url: str = "",
        provider_type: str = "",
    ) -> Dict[str, Any]:
        profile = infer_provider_profile(base_url, model, provider_type)
        allow_tools = bool(tools and profile.supports_tools)
        messages = normalize_messages_for_provider(
            messages,
            base_url=base_url,
            model=model,
            provider_type=provider_type,
        )
        messages = sanitize_messages_for_chat_api(messages, allow_tools=allow_tools)
        payload: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "stream": stream,
        }
        if allow_tools:
            payload["tools"] = tools
            if tool_choice:
                payload["tool_choice"] = tool_choice
        extra = dict(extra_body) if extra_body else {}
        # prompt_caching 只是协议内部的缓存断点标记，不能透传给 OpenAI 兼容接口
        extra.pop("prompt_caching", None)
        stop = extra.pop("stop", None)
        token_limit = normalize_max_tokens(max_tokens) or resolve_token_limit(extra)
        limit_field = token_limit_field(model, extra)
        extra.pop("max_tokens", None)
        extra.pop("token_limit_field", None)
        if is_reasoning_model(model):
            # 推理系模型只接受默认温度，top_p 同样不生效
            extra.pop("temperature", None)
            extra.pop("top_p", None)
        payload.update(extra)
        if token_limit:
            payload[limit_field] = token_limit
        # 停止字符串：OpenAI 协议直接使用 stop
        if stop:
            payload["stop"] = stop
        return payload

    def parse_response(
        self,
        data: Dict[str, Any],
        *,
        model: str = "",
        base_url: str = "",
        provider_type: str = "",
        fallback_tool_parser: Optional[Callable] = None,
    ) -> NormalizedModelResponse:
        return normalize_chat_completion_data(
            data,
            base_url=base_url,
            model=model,
            provider_type=provider_type,
            fallback_tool_parser=fallback_tool_parser,
        )

    def parse_stream_chunk(
        self,
        chunk_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        choices = chunk_data.get("choices", [])
        if not choices:
            return None
        delta = choices[0].get("delta", {})
        raw_content = delta.get("content")
        if raw_content in (None, ""):
            raw_content = delta.get("text") or choices[0].get("text")
        content = _stringify_message_content(raw_content)
        if content:
            content = repair_mojibake_text(content)
            return {"type": "content", "content": content}
        return None

    def supports_tools(self) -> bool:
        return True

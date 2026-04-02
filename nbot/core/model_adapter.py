import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple


@dataclass
class ProviderProfile:
    name: str
    endpoint_mode: str = "openai_chat_completions"
    provider_type: str = "openai_compatible"
    supports_tools: bool = True
    supports_reasoning: bool = True
    supports_stream: bool = True
    reasoning_keys: Tuple[str, ...] = (
        "thinking_content",
        "reasoning_content",
        "reasoning",
    )


@dataclass
class NormalizedModelResponse:
    content: str = ""
    finish_reason: str = ""
    tool_calls: List[Dict[str, Any]] = field(default_factory=list)
    thinking_content: str = ""
    usage: Dict[str, Any] = field(default_factory=dict)
    raw_message: Dict[str, Any] = field(default_factory=dict)
    raw_data: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        data = {
            "content": self.content,
            "finish_reason": self.finish_reason,
        }
        if self.tool_calls:
            data["tool_calls"] = self.tool_calls
        if self.thinking_content:
            data["thinking_content"] = self.thinking_content
        if self.usage:
            data["usage"] = self.usage
        return data


def infer_provider_profile(
    base_url: str = "",
    model: str = "",
    provider_type: str = "",
) -> ProviderProfile:
    explicit = (provider_type or "").strip().lower()
    if explicit in {"openai", "openai_compatible", "custom", "deepseek"}:
        return ProviderProfile(name=explicit or "openai_compatible", provider_type=explicit or "openai_compatible")
    if explicit in {"siliconflow", "silicon"}:
        return ProviderProfile(name="siliconflow", provider_type="siliconflow")
    if explicit in {"minimax"}:
        return ProviderProfile(
            name="minimax",
            provider_type="minimax",
            endpoint_mode="raw",
        )
    if explicit in {"anthropic", "claude"}:
        return ProviderProfile(
            name="anthropic",
            provider_type="anthropic",
            endpoint_mode="raw",
            supports_tools=False,
        )
    if explicit in {"google", "gemini"}:
        return ProviderProfile(
            name="gemini",
            provider_type="google",
            endpoint_mode="raw",
            supports_tools=False,
        )

    combined = f"{base_url or ''} {model or ''}".lower()
    if "anthropic" in combined or "/v1/messages" in combined:
        return ProviderProfile(name="anthropic", provider_type="anthropic", endpoint_mode="raw", supports_tools=False)
    if "minimax" in combined:
        return ProviderProfile(name="minimax", provider_type="minimax", endpoint_mode="raw")
    if "silicon" in combined:
        return ProviderProfile(name="siliconflow", provider_type="siliconflow")
    return ProviderProfile(name="openai_compatible", provider_type="openai_compatible")


def resolve_chat_completion_url(
    base_url: str,
    provider: Optional[ProviderProfile] = None,
    *,
    model: str = "",
    provider_type: str = "",
) -> str:
    url_base = (base_url or "").rstrip("/")
    if not url_base:
        raise ValueError("base_url 未配置")

    provider = provider or infer_provider_profile(base_url, model, provider_type)
    if provider.endpoint_mode == "raw":
        return url_base
    if "/chat/completions" in url_base or "/chatcompletion" in url_base:
        return url_base
    if url_base.endswith("/v1"):
        return f"{url_base}/chat/completions"
    return f"{url_base}/chat/completions"


def build_chat_completion_payload(
    model: str,
    messages: List[Dict[str, Any]],
    *,
    tools: Optional[List[Dict[str, Any]]] = None,
    tool_choice: Optional[str] = "auto",
    stream: bool = False,
    extra_body: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    payload: Dict[str, Any] = {
        "model": model,
        "messages": messages,
        "stream": stream,
    }
    if tools:
        payload["tools"] = tools
        if tool_choice:
            payload["tool_choice"] = tool_choice
    if extra_body:
        payload.update(extra_body)
    return payload


def parse_tool_call_arguments(raw_arguments: Any) -> Dict[str, Any]:
    if isinstance(raw_arguments, dict):
        return raw_arguments
    if not raw_arguments:
        return {}
    if isinstance(raw_arguments, str):
        try:
            return json.loads(raw_arguments)
        except Exception:
            return {}
    return {}


def extract_reasoning_content(
    message: Dict[str, Any],
    profile: Optional[ProviderProfile] = None,
) -> str:
    profile = profile or ProviderProfile(name="openai_compatible")
    for key in profile.reasoning_keys:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
        if isinstance(value, list):
            chunks: List[str] = []
            for item in value:
                if isinstance(item, str):
                    chunks.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content")
                    if text:
                        chunks.append(str(text))
            joined = "\n".join(chunk for chunk in chunks if chunk)
            if joined.strip():
                return joined
    return ""


def parse_tool_calls(message: Dict[str, Any]) -> List[Dict[str, Any]]:
    parsed_calls: List[Dict[str, Any]] = []
    for tool_call in message.get("tool_calls") or []:
        function_block = tool_call.get("function", {}) if isinstance(tool_call, dict) else {}
        parsed_calls.append(
            {
                "id": tool_call.get("id", "") if isinstance(tool_call, dict) else "",
                "name": function_block.get("name"),
                "arguments": parse_tool_call_arguments(function_block.get("arguments")),
            }
        )
    return parsed_calls


def normalize_chat_completion_data(
    data: Dict[str, Any],
    *,
    base_url: str = "",
    model: str = "",
    provider_type: str = "",
    fallback_tool_parser: Optional[Callable[[str], List[Dict[str, Any]]]] = None,
) -> NormalizedModelResponse:
    profile = infer_provider_profile(base_url, model, provider_type)
    choices = data.get("choices") or []
    if not choices:
        base_resp = data.get("base_resp", {}) if isinstance(data, dict) else {}
        status_msg = base_resp.get("status_msg") or "API 返回空响应"
        raise ValueError(status_msg)

    choice = choices[0] or {}
    message = choice.get("message") or {}
    finish_reason = choice.get("finish_reason") or ""
    content = message.get("content") or ""
    tool_calls = parse_tool_calls(message)
    thinking_content = extract_reasoning_content(message, profile)

    if not tool_calls and fallback_tool_parser and "[TOOL_CALL]" in content and "[/TOOL_CALL]" in content:
        parsed_calls = fallback_tool_parser(content)
        if parsed_calls:
            tool_calls = parsed_calls

    usage = data.get("usage") or {}
    return NormalizedModelResponse(
        content=content,
        finish_reason=finish_reason,
        tool_calls=tool_calls,
        thinking_content=thinking_content,
        usage=usage,
        raw_message=message,
        raw_data=data,
    )

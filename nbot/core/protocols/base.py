"""
Base protocol interface and registry for model API adapters.

Each protocol implements ModelProtocol to handle:
- URL resolution
- Request header construction
- Payload building (messages -> API format)
- Response parsing (API format -> NormalizedModelResponse)
- Stream chunk parsing
"""

from abc import ABC, abstractmethod
from typing import Any, Callable, Dict, List, Optional

from nbot.core.model_adapter import NormalizedModelResponse


class ModelProtocol(ABC):
    """Unified interface for AI model API protocols."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Protocol identifier (e.g. 'openai_chat', 'anthropic_messages')."""
        ...

    @abstractmethod
    def resolve_url(
        self,
        base_url: str,
        model: str = "",
        *,
        append_base_url_path: bool = True,
        **opts: Any,
    ) -> str:
        """Build the full API endpoint URL."""
        ...

    @abstractmethod
    def build_headers(self, api_key: str, *, stream: bool = False) -> Dict[str, str]:
        """Build HTTP headers for the API request."""
        ...

    @abstractmethod
    def build_payload(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        tool_choice: Optional[str] = None,
        stream: bool = False,
        max_tokens: Optional[int] = None,
        extra_body: Optional[Dict[str, Any]] = None,
        **kwargs: Any,
    ) -> Dict[str, Any]:
        """Build the JSON payload for the API request.

        model 配置里的采样参数（temperature / top_p / max_tokens）通过
        ``extra_body`` 传入，由各协议翻译为自家字段名。
        """
        ...

    @abstractmethod
    def parse_response(
        self,
        data: Dict[str, Any],
        *,
        model: str = "",
        fallback_tool_parser: Optional[Callable] = None,
    ) -> NormalizedModelResponse:
        """Parse a non-streaming API response into NormalizedModelResponse."""
        ...

    @abstractmethod
    def parse_stream_chunk(
        self,
        chunk_data: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """Parse a streaming chunk. Returns None to skip, or a dict with 'type' key."""
        ...

    def supports_tools(self) -> bool:
        """Whether this protocol natively supports tool/function calling."""
        return True

    def supports_streaming(self) -> bool:
        """Whether this protocol supports streaming responses."""
        return True

    @property
    def display_name(self) -> str:
        """Human-readable protocol name for UI display."""
        return self.name

    @property
    def url_suffix(self) -> str:
        """The URL path suffix appended when append_base_url_path is True."""
        return ""


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_PROTOCOL_REGISTRY: Dict[str, type] = {}

# provider_type string -> protocol registry key
_PROVIDER_ALIASES: Dict[str, str] = {
    "openai_compatible": "openai_chat",
    "openai": "openai_chat",
    "custom": "openai_chat",
    "deepseek": "openai_chat",
    "siliconflow": "openai_chat",
    "silicon": "openai_chat",
    "zhipu": "openai_chat",
    "glm": "openai_chat",
    "minimax": "openai_chat",
    "grok": "openai_chat",
    "xai": "openai_chat",
    "qwen": "openai_chat",
    "dashscope": "openai_chat",
    "xiaomi": "openai_chat",
    "mimo": "openai_chat",
    "openai_responses": "openai_responses",
    "anthropic": "anthropic_messages",
    "claude": "anthropic_messages",
    "gemini": "gemini_native",
    "google": "gemini_native",
    "gemini_native": "gemini_native",
}


def register_protocol(key: str, protocol_cls: type) -> None:
    """Register a protocol class under a given key."""
    _PROTOCOL_REGISTRY[key] = protocol_cls


def get_protocol(provider_type: str = "") -> ModelProtocol:
    """Get a protocol instance for the given provider type.

    Falls back to OpenAI Chat Completions for unknown types.
    """
    normalized = (provider_type or "").strip().lower()
    key = _PROVIDER_ALIASES.get(normalized, "openai_chat")
    cls = _PROTOCOL_REGISTRY.get(key)
    if cls is None:
        _ensure_all_registered()
        cls = _PROTOCOL_REGISTRY.get(key, _PROTOCOL_REGISTRY.get("openai_chat"))
    return cls()


def list_protocols() -> List[Dict[str, str]]:
    """Return metadata for all registered protocols (for UI dropdowns).

    Each entry: { key, name, display_name, url_suffix, aliases }
    """
    _ensure_all_registered()
    # Collect unique protocol keys -> all aliases that map to them
    alias_map: Dict[str, List[str]] = {}
    for alias, key in _PROVIDER_ALIASES.items():
        alias_map.setdefault(key, []).append(alias)

    result: List[Dict[str, str]] = []
    seen: set = set()
    # Use alias order (first alias = canonical provider_type value)
    for alias, key in _PROVIDER_ALIASES.items():
        if key in seen:
            continue
        seen.add(key)
        cls = _PROTOCOL_REGISTRY.get(key)
        if cls is None:
            continue
        instance = cls()
        result.append({
            "key": alias,
            "protocol_key": key,
            "name": instance.display_name,
            "url_suffix": instance.url_suffix,
            "aliases": alias_map.get(key, []),
        })
    return result


def _ensure_all_registered() -> None:
    """Ensure all protocol classes are registered (lazy import)."""
    if _PROTOCOL_REGISTRY:
        return
    from nbot.core.protocols.openai_chat import OpenAIChatProtocol
    from nbot.core.protocols.openai_responses import OpenAIResponsesProtocol
    from nbot.core.protocols.anthropic_messages import AnthropicMessagesProtocol
    from nbot.core.protocols.gemini_native import GeminiNativeProtocol

    register_protocol("openai_chat", OpenAIChatProtocol)
    register_protocol("openai_responses", OpenAIResponsesProtocol)
    register_protocol("anthropic_messages", AnthropicMessagesProtocol)
    register_protocol("gemini_native", GeminiNativeProtocol)

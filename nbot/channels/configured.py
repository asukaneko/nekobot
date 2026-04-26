from typing import Any, Dict

from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope


class ConfiguredChannelAdapter(BaseChannelAdapter):
    """Generic adapter backed by a channel config record."""

    def __init__(self, channel_config: Dict[str, Any]):
        self.channel_config = dict(channel_config or {})
        self.channel_name = str(self.channel_config.get("id") or "custom").strip()

    def get_capabilities(self) -> ChannelCapabilities:
        capabilities = self.channel_config.get("capabilities") or {}
        return ChannelCapabilities(
            supports_stream=bool(capabilities.get("supports_stream", False)),
            supports_progress_updates=bool(
                capabilities.get("supports_progress_updates", False)
            ),
            supports_file_send=bool(capabilities.get("supports_file_send", False)),
            supports_stop=bool(capabilities.get("supports_stop", False)),
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        metadata.setdefault("channel_config", self.public_config())
        sender = kwargs.get("sender") or f"{self.channel_name}_user"
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=kwargs.get("conversation_id") or "",
            user_id=kwargs.get("user_id") or "",
            sender=sender,
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def public_config(self) -> Dict[str, Any]:
        return {
            "id": self.channel_config.get("id"),
            "name": self.channel_config.get("name"),
            "type": self.channel_config.get("type"),
            "transport": self.channel_config.get("transport"),
        }

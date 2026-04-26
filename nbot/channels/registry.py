from typing import Callable, Dict, List, Optional, Set, Type, Union

from nbot.channels.base import BaseChannelAdapter

AdapterFactory = Callable[[], BaseChannelAdapter]
Handler = Callable[..., object]


class ChannelRegistry:
    """Registry for channel adapters and optional agent handlers."""

    def __init__(self):
        self._adapter_factories: Dict[str, AdapterFactory] = {}
        self._handlers: Dict[str, Handler] = {}
        self._configured_channels: Set[str] = set()

    def register_adapter(
        self,
        channel: str,
        adapter: Union[Type[BaseChannelAdapter], AdapterFactory, BaseChannelAdapter],
    ) -> None:
        channel_name = self._normalize_channel(channel)

        if isinstance(adapter, BaseChannelAdapter):
            self._adapter_factories[channel_name] = lambda adapter=adapter: adapter
        elif isinstance(adapter, type):
            self._adapter_factories[channel_name] = adapter
        else:
            self._adapter_factories[channel_name] = adapter

    def get_adapter(self, channel: str) -> Optional[BaseChannelAdapter]:
        factory = self._adapter_factories.get(self._normalize_channel(channel))
        if not factory:
            return None
        return factory()

    def list_adapters(self) -> List[str]:
        return sorted(self._adapter_factories.keys())

    def unregister_adapter(self, channel: str) -> None:
        self._adapter_factories.pop(self._normalize_channel(channel), None)

    def register_handler(self, channel: str, handler: Handler) -> None:
        self._handlers[self._normalize_channel(channel)] = handler

    def get_handler(self, channel: str) -> Optional[Handler]:
        return self._handlers.get(self._normalize_channel(channel))

    def list_handlers(self) -> List[str]:
        return sorted(self._handlers.keys())

    def unregister_handler(self, channel: str) -> None:
        self._handlers.pop(self._normalize_channel(channel), None)

    @staticmethod
    def _normalize_channel(channel: str) -> str:
        return (channel or "").strip().lower()


channel_registry = ChannelRegistry()


def register_channel_adapter(
    channel: str,
    adapter: Union[Type[BaseChannelAdapter], AdapterFactory, BaseChannelAdapter],
) -> None:
    channel_registry.register_adapter(channel, adapter)


def get_channel_adapter(channel: str) -> Optional[BaseChannelAdapter]:
    return channel_registry.get_adapter(channel)


def unregister_channel_adapter(channel: str) -> None:
    channel_registry.unregister_adapter(channel)


def register_channel_handler(channel: str, handler: Handler) -> None:
    channel_registry.register_handler(channel, handler)


def get_channel_handler(channel: str) -> Optional[Handler]:
    return channel_registry.get_handler(channel)


def unregister_channel_handler(channel: str) -> None:
    channel_registry.unregister_handler(channel)


def register_configured_channel(channel_config: dict) -> None:
    from nbot.channels.configured import ConfiguredChannelAdapter

    if not channel_config or channel_config.get("builtin"):
        return
    channel_id = str(channel_config.get("id") or "").strip()
    if not channel_id:
        return
    channel_registry._configured_channels.add(channel_id)
    if channel_config.get("enabled") is False:
        channel_registry.unregister_adapter(channel_id)
        return
    if str(channel_config.get("type") or "").strip().lower() == "telegram":
        from nbot.channels.telegram import TelegramChannelAdapter

        channel_registry.register_adapter(channel_id, TelegramChannelAdapter)
        return
    channel_registry.register_adapter(
        channel_id,
        lambda channel_config=dict(channel_config): ConfiguredChannelAdapter(
            channel_config
        ),
    )


def sync_configured_channels(channel_configs: list) -> None:
    active_configured_ids = {
        str(channel_config.get("id") or "").strip()
        for channel_config in channel_configs or []
        if isinstance(channel_config, dict)
        and not channel_config.get("builtin")
        and str(channel_config.get("id") or "").strip()
    }
    for old_channel_id in list(channel_registry._configured_channels - active_configured_ids):
        channel_registry.unregister_adapter(old_channel_id)
        channel_registry._configured_channels.discard(old_channel_id)
    for channel_config in channel_configs or []:
        register_configured_channel(channel_config)

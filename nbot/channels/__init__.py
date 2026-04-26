from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope
from nbot.channels.qq import QQChannelAdapter
from nbot.channels.registry import (
    ChannelRegistry,
    channel_registry,
    get_channel_adapter,
    get_channel_handler,
    register_configured_channel,
    register_channel_adapter,
    register_channel_handler,
    sync_configured_channels,
    unregister_channel_adapter,
    unregister_channel_handler,
)
from nbot.channels.web import WebChannelAdapter

register_channel_adapter(QQChannelAdapter.channel_name, QQChannelAdapter)
register_channel_adapter(WebChannelAdapter.channel_name, WebChannelAdapter)

__all__ = [
    "BaseChannelAdapter",
    "ChannelCapabilities",
    "ChannelEnvelope",
    "ChannelRegistry",
    "QQChannelAdapter",
    "WebChannelAdapter",
    "channel_registry",
    "get_channel_adapter",
    "get_channel_handler",
    "register_configured_channel",
    "register_channel_adapter",
    "register_channel_handler",
    "sync_configured_channels",
    "unregister_channel_adapter",
    "unregister_channel_handler",
]

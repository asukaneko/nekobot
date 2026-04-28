# registry - 频道注册

## 概述

`registry.py` 提供频道适配器的注册和管理功能，维护频道适配器和处理器的映射关系。

## 核心类

### ChannelRegistry

频道注册中心，管理所有频道适配器。

```python
class ChannelRegistry:
    def __init__(self):
        self._adapter_factories: Dict[str, AdapterFactory] = {}
        self._handlers: Dict[str, Handler] = {}
        self._configured_channels: Set[str] = set()
```

## 主要方法

### 注册适配器

```python
from nbot.channels.registry import channel_registry

# 注册适配器类
channel_registry.register_adapter("my_channel", MyChannelAdapter)

# 注册适配器实例
adapter = MyChannelAdapter()
channel_registry.register_adapter("my_channel", adapter)

# 注册适配器工厂
channel_registry.register_adapter("my_channel", lambda: MyChannelAdapter())
```

### 获取适配器

```python
# 获取适配器实例
adapter = channel_registry.get_adapter("my_channel")
if adapter:
    print(f"适配器: {adapter.channel_name}")
```

### 注册处理器

```python
# 注册频道处理器
def my_handler(request: ChatRequest) -> ChatResponse:
    return ChatResponse(final_content="处理结果")

channel_registry.register_handler("my_channel", my_handler)
```

### 列出所有适配器

```python
adapters = channel_registry.list_adapters()
print(f"已注册频道: {adapters}")
# 输出: ['qq', 'telegram', 'web', 'my_channel']
```

## 便捷函数

```python
from nbot.channels.registry import (
    register_channel_adapter,
    get_channel_adapter,
    unregister_channel_adapter,
    register_channel_handler,
    get_channel_handler,
)

# 快速注册
register_channel_adapter("my_channel", MyChannelAdapter)

# 快速获取
adapter = get_channel_adapter("my_channel")

# 注册处理器
register_channel_handler("my_channel", my_handler)
```

## 配置频道同步

支持从配置自动注册频道：

```python
from nbot.channels.registry import (
    register_configured_channel,
    sync_configured_channels
)

# 注册单个配置频道
channel_config = {
    "id": "custom_qq",
    "name": "自定义QQ",
    "type": "qq",
    "enabled": True
}
register_configured_channel(channel_config)

# 同步所有配置
all_configs = [
    {"id": "ch_1", "type": "telegram", "enabled": True},
    {"id": "ch_2", "type": "web", "enabled": False},
]
sync_configured_channels(all_configs)
```

## 工作流程

```
1. 启动时加载配置
2. 调用 sync_configured_channels()
3. 注册所有启用的频道适配器
4. 收到消息时通过 registry 获取对应适配器
5. 适配器转换消息格式
6. 调用注册的处理器处理请求
```

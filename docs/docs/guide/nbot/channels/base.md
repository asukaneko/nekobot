# base - 频道基类

## 概述

`base.py` 定义了频道适配器的基类和接口，所有频道适配器（QQ、Web、Telegram）都需要继承 `BaseChannelAdapter`。

## 核心类

### BaseChannelAdapter

频道适配器的抽象基类。

```python
class BaseChannelAdapter(ABC):
    channel_name: str = "base"

    @abstractmethod
    def get_capabilities(self) -> ChannelCapabilities:
        """返回频道能力声明"""
        pass

    @abstractmethod
    def build_chat_request(self, **kwargs) -> ChatRequest:
        """构建 ChatRequest"""
        pass

    def normalize_inbound_message(self, content: str) -> str:
        """归一化输入消息"""
        return (content or "").strip()
```

### ChannelCapabilities

频道能力声明数据类。

```python
@dataclass
class ChannelCapabilities:
    supports_stream: bool = False              # 支持流式输出
    supports_progress_updates: bool = False    # 支持进度更新
    supports_file_send: bool = False           # 支持发送文件
    supports_stop: bool = False                # 支持停止生成
```

## 创建自定义适配器

```python
from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities
from nbot.core.chat_models import ChatRequest

class MyChannelAdapter(BaseChannelAdapter):
    channel_name = "my_channel"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=True,
            supports_file_send=True
        )

    def build_chat_request(self, **kwargs) -> ChatRequest:
        return ChatRequest(
            channel=self.channel_name,
            conversation_id=kwargs.get("conversation_id"),
            user_id=kwargs.get("user_id"),
            content=kwargs.get("content"),
            attachments=kwargs.get("attachments", []),
            metadata=kwargs.get("metadata", {})
        )

    async def send_message(self, conversation_id: str, content: str, **kwargs):
        """发送消息到频道"""
        # 实现具体的发送逻辑
        pass
```

## 能力声明说明

| 能力 | 说明 | 影响 |
|------|------|------|
| `supports_stream` | 支持流式输出 | 启用打字机效果 |
| `supports_progress_updates` | 支持进度更新 | 显示处理进度 |
| `supports_file_send` | 支持发送文件 | 可以发送工作区文件 |
| `supports_stop` | 支持停止生成 | 可以中断 AI 生成 |

## 生命周期

```
1. 接收原始消息
2. normalize_inbound_message() - 归一化
3. build_chat_request() - 构建请求
4. 传递给 AI 核心处理
5. 接收 ChatResponse
6. 根据频道能力格式化输出
7. 发送给用户
```

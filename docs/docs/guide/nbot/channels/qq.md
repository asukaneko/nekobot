# qq - QQ适配器

## 概述

`qq.py` 实现 QQ 频道的适配器，基于 NapCat/ncatbot 框架与 QQ 协议对接。

## QQChannelAdapter

```python
from nbot.channels.qq import QQChannelAdapter

adapter = QQChannelAdapter()
```

## 能力声明

```python
def get_capabilities(self) -> ChannelCapabilities:
    return ChannelCapabilities(
        supports_stream=False,           # QQ 不支持流式
        supports_progress_updates=True,  # 支持进度提示
        supports_file_send=True,         # 支持发送文件
        supports_stop=False              # 不支持停止
    )
```

## 消息处理

### 构建请求

```python
def build_chat_request(self, **kwargs) -> ChatRequest:
    """将 QQ 消息转换为 ChatRequest"""
    return ChatRequest(
        channel="qq",
        conversation_id=kwargs.get("group_id") or kwargs.get("user_id"),
        user_id=kwargs.get("user_id"),
        content=kwargs.get("content"),
        attachments=self._parse_attachments(kwargs),
        metadata={
            "message_id": kwargs.get("message_id"),
            "group_id": kwargs.get("group_id"),
            "is_group": kwargs.get("group_id") is not None
        }
    )
```

### 发送回复

```python
async def send_message(self, conversation_id: str, content: str, **kwargs):
    """发送消息到 QQ"""
    # 处理长消息分割
    # 支持文本、图片、@提及
```

## 特殊处理

### 长消息分割

QQ 消息有长度限制，适配器会自动分割长消息：

```python
max_length = 3000  # 单条消息最大长度
if len(content) > max_length:
    chunks = split_message(content, max_length)
    for chunk in chunks:
        await send_qq_message(chunk)
```

### 图片处理

```python
# 发送图片
await adapter.send_message(
    conversation_id="123456",
    content="",
    image="path/to/image.jpg"
)

# 发送网络图片
await adapter.send_message(
    conversation_id="123456",
    content="",
    image_url="https://example.com/image.jpg"
)
```

### 文件发送

```python
# 发送工作区文件
await adapter.send_file(
    conversation_id="123456",
    file_path="data/workspaces/private/xxx/file.txt"
)
```

## 事件处理

### 消息事件

```python
@bot.message_handler()
async def handle_message(msg):
    # 转换为 ChatRequest
    request = adapter.build_chat_request(
        user_id=msg.user_id,
        group_id=msg.group_id,
        content=msg.raw_message,
        message_id=msg.message_id
    )
    # 处理请求...
```

### 群聊 @ 处理

```python
def _is_at_me(self, message: str) -> bool:
    """检查是否 @ 机器人"""
    at_patterns = [f"@{self.bot_uin}", f"@{self.bot_name}"]
    return any(pattern in message for pattern in at_patterns)
```

## 配置

```python
# .env
BOT_UIN=123456789          # 机器人 QQ 号
ROOT=987654321             # 管理员 QQ 号
WS_URI=ws://localhost:3001 # NapCat WebSocket
TOKEN=your_token           # NapCat Token
```

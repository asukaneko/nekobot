# telegram - Telegram适配器

## 概述

`telegram.py` 实现 Telegram 频道的适配器，支持通过 Webhook 接收消息和发送回复。

## TelegramChannelAdapter

```python
from nbot.channels.telegram import TelegramChannelAdapter

adapter = TelegramChannelAdapter()
```

## 能力声明

```python
def get_capabilities(self) -> ChannelCapabilities:
    return ChannelCapabilities(
        supports_stream=False,           # Telegram 不支持流式
        supports_progress_updates=False, # 不支持进度更新
        supports_file_send=True,         # 支持发送文件
        supports_stop=False              # 不支持停止
    )
```

## Webhook 处理

### 接收消息

```python
async def handle_webhook(self, update: dict) -> ChatRequest:
    """处理 Telegram Webhook 更新"""
    message = update.get('message', {})
    
    return ChatRequest(
        channel="telegram",
        conversation_id=str(message.get('chat', {}).get('id')),
        user_id=str(message.get('from', {}).get('id')),
        content=message.get('text', ''),
        attachments=self._parse_attachments(message),
        metadata={
            "message_id": message.get('message_id'),
            "chat_type": message.get('chat', {}).get('type')
        }
    )
```

### 发送消息

```python
async def send_message(self, chat_id: str, content: str, **kwargs):
    """发送消息到 Telegram"""
    url = f"https://api.telegram.org/bot{self.token}/sendMessage"
    
    payload = {
        "chat_id": chat_id,
        "text": content,
        "parse_mode": "Markdown"
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            return await resp.json()
```

## 配置

```python
# 环境变量
TELEGRAM_BOT_TOKEN=123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11
TELEGRAM_WEBHOOK_SECRET=random-secret-string

# 频道配置
{
    "id": "telegram",
    "name": "Telegram",
    "type": "telegram",
    "transport": "webhook",
    "config": {
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "secret_token_env": "TELEGRAM_WEBHOOK_SECRET",
        "webhook_url": "https://example.com/api/channels/telegram/telegram/webhook"
    }
}
```

## 设置 Webhook

```python
async def set_webhook(self, webhook_url: str):
    """设置 Telegram Webhook"""
    url = f"https://api.telegram.org/bot{self.token}/setWebhook"
    
    payload = {
        "url": webhook_url,
        "secret_token": self.webhook_secret
    }
    
    async with aiohttp.ClientSession() as session:
        async with session.post(url, json=payload) as resp:
            result = await resp.json()
            return result.get('ok', False)
```

## 安全验证

```python
def verify_webhook_secret(self, request_headers: dict) -> bool:
    """验证 Webhook 请求的 Secret Token"""
    secret = request_headers.get('X-Telegram-Bot-Api-Secret-Token')
    return secret == self.webhook_secret
```

## 特殊功能

### Markdown 格式

Telegram 支持 Markdown 格式消息：

```python
content = """
*粗体文本*
_斜体文本_
`代码`
[链接](https://example.com)
"""

await adapter.send_message(
    chat_id="123456",
    content=content,
    parse_mode="Markdown"
)
```

### 发送图片

```python
await adapter.send_photo(
    chat_id="123456",
    photo="path/to/image.jpg",
    caption="图片描述"
)
```

### 发送文件

```python
await adapter.send_document(
    chat_id="123456",
    document="path/to/file.pdf",
    caption="文件描述"
)
```

## 命令处理

Telegram 支持 Bot 命令：

```python
def _is_command(self, text: str) -> bool:
    """检查是否为命令"""
    return text.startswith('/')

def _parse_command(self, text: str) -> tuple:
    """解析命令"""
    parts = text.split()
    command = parts[0][1:]  # 去掉 /
    args = parts[1:]
    return command, args
```

## 使用流程

```
1. 创建 Telegram Bot，获取 Token
2. 配置环境变量 TELEGRAM_BOT_TOKEN
3. 在 Web 后台添加 Telegram 频道
4. 设置 Webhook URL
5. Telegram 服务器向 Webhook 发送更新
6. 适配器处理更新并转换为 ChatRequest
7. AI 核心处理请求
8. 适配器发送回复到 Telegram
```

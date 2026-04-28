# 频道管理与接入

NekoBot 的频道层把 Web、QQ、Telegram 等入口统一成频道配置。每个频道可以声明自己的传输方式、能力和私有配置，后端通过 `ChannelRegistry` 注册适配器。

## 默认内置频道

- **`web`** - Web 控制台频道
- **`qq`** - NapCat / ncatbot QQ 频道

## Web 界面管理

进入 Web 控制台的"频道管理"页面后，可以：
- 新增、编辑、启用、禁用和删除自定义频道
- 内置频道只能编辑和启停，不能删除

频道配置会保存到 `data/channels.json`。

::: warning 安全提示
不要把 token、cookie、secret 等敏感值直接写进配置文件，推荐只写环境变量名，例如 `bot_token_env`。
:::

## 频道适配器架构

### 基类定义

```python
from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities

class MyChannelAdapter(BaseChannelAdapter):
    channel_name = "my_channel"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=True,           # 支持流式输出
            supports_progress_updates=True, # 支持进度更新
            supports_file_send=True,        # 支持发送文件
            supports_stop=True              # 支持停止生成
        )

    def build_chat_request(self, **kwargs):
        # 构建统一的 ChatRequest
        return super().build_chat_request(**kwargs)

    def normalize_inbound_message(self, content: str) -> str:
        # 归一化输入消息
        return (content or "").strip()
```

### 注册适配器

```python
from nbot.channels.registry import register_channel_adapter

register_channel_adapter("my_channel", MyChannelAdapter)
```

## Telegram 预设

频道管理页面提供了 "Telegram 预设" 按钮，会自动填充：

```json
{
  "id": "telegram",
  "name": "Telegram",
  "type": "telegram",
  "transport": "webhook",
  "config": {
    "bot_token_env": "TELEGRAM_BOT_TOKEN",
    "secret_token_env": "TELEGRAM_WEBHOOK_SECRET",
    "webhook_url": ""
  },
  "capabilities": {
    "supports_stream": false,
    "supports_progress_updates": false,
    "supports_file_send": false,
    "supports_stop": false
  }
}
```

### 环境变量

启动服务前设置：

```bash
export TELEGRAM_BOT_TOKEN="123456:telegram-bot-token"
export TELEGRAM_WEBHOOK_SECRET="random-long-secret"
```

Windows PowerShell：

```powershell
$env:TELEGRAM_BOT_TOKEN="123456:telegram-bot-token"
$env:TELEGRAM_WEBHOOK_SECRET="random-long-secret"
```

`TELEGRAM_WEBHOOK_SECRET` 会用于校验 Telegram 请求头 `X-Telegram-Bot-Api-Secret-Token`。如果配置了 secret，校验失败的请求会被拒绝。

### Webhook 地址

Telegram 需要能访问你的 Web 服务公网地址。频道创建后，页面会展示本地路径：

```
/api/channels/telegram/telegram/webhook
```

把它拼到公网域名后写入 `webhook_url`：

```json
{
  "bot_token_env": "TELEGRAM_BOT_TOKEN",
  "secret_token_env": "TELEGRAM_WEBHOOK_SECRET",
  "webhook_url": "https://example.com/api/channels/telegram/telegram/webhook"
}
```

保存频道后，点击频道卡片上的链接按钮即可调用 Telegram `setWebhook`。

也可以手动调用：

```http
POST /api/channels/telegram/telegram/set-webhook
Content-Type: application/json

{
  "webhook_url": "https://example.com/api/channels/telegram/telegram/webhook"
}
```

## 频道 API

所有管理 API 都需要 Web 登录态或 Bearer Token。Telegram webhook 回调路径例外，它通过 Telegram secret token 校验。

```http
GET /api/channels                    # 获取所有频道
GET /api/channels/presets            # 获取预设列表
POST /api/channels                   # 创建频道
POST /api/channels/presets/<preset_id>  # 从预设创建
PUT /api/channels/<channel_id>       # 更新频道
DELETE /api/channels/<channel_id>    # 删除频道
POST /api/channels/<channel_id>/toggle  # 启用/禁用频道
POST /api/channels/telegram/<channel_id>/webhook      # 处理 webhook
POST /api/channels/telegram/<channel_id>/set-webhook  # 设置 webhook
```

## 代码接入

### 注册自定义适配器

```python
from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities
from nbot.channels.registry import register_channel_adapter


class MyChannelAdapter(BaseChannelAdapter):
    channel_name = "my_channel"

    def get_capabilities(self):
        return ChannelCapabilities(supports_file_send=True)

    async def send_message(self, conversation_id, content, **kwargs):
        # 实现消息发送逻辑
        pass

    async def handle_incoming(self, data):
        # 处理 incoming 消息
        request = self.build_chat_request(
            conversation_id=data.get("chat_id"),
            content=data.get("text"),
            user_id=data.get("user_id"),
            sender=data.get("username")
        )
        return request


register_channel_adapter("my_channel", MyChannelAdapter)
```

### 从配置同步

从配置同步出来的自定义频道会自动注册为通用适配器。`type=telegram` 的频道会使用 Telegram 专用适配器。

## 安全建议

- 不要把真实 token 写进 `data/channels.json`
- Webhook 必须配置随机 secret，并确保公网入口只暴露必要路径
- 如果使用反向代理，确认它会转发 `X-Telegram-Bot-Api-Secret-Token` 请求头
- 频道管理 API 依赖 Web 登录认证，不建议直接暴露在公网

## 频道能力声明

每个频道可以声明自己的能力：

```python
@dataclass
class ChannelCapabilities:
    supports_stream: bool = False              # 支持流式输出
    supports_progress_updates: bool = False    # 支持进度更新
    supports_file_send: bool = False           # 支持发送文件
    supports_stop: bool = False                # 支持停止生成
```

这些能力会影响 AI 的行为：
- 如果 `supports_tools=true`，AI 会尝试调用工具
- 如果 `supports_stream=true`，会启用流式输出
- 如果 `supports_file_send=true`，AI 可以发送文件

# 新增频道指南

## 概述

Ncatbot 使用 **统一 AI 管道中间件**（`AIPipeline`）处理所有频道的 AI 请求。新增一个频道时，你只需要关注 **传输层**（怎么收消息、怎么发消息），管道会自动提供：

- 知识库检索（RAG）
- 工具调用（搜索、天气、命令执行、工作区操作等）
- 技能系统
- 工作区文件管理
- 会话历史管理
- `exec_command` 确认/拒绝流程

## 架构总览

```
外部平台 (Discord / 钉钉 / ... )
    │
    ▼
┌─────────────────────┐
│  ① 频道适配器        │  ChannelAdapter: parse → build_chat_request
│  channels/<name>.py │
└──────┬──────────────┘
       │  ChatRequest
       ▼
┌─────────────────────┐
│  ② 管道回调          │  XxxCallbacks(PipelineCallbacks)
│  services/xxx.py    │  → 覆写 3~6 个方法
└──────┬──────────────┘
       │
       ▼
┌─────────────────────┐
│  ③ 统一管道          │  AIPipeline.process()
│  core/ai_pipeline.py│  → 附件 → 知识库 → 上下文 → AI(工具循环/流式) → 回复
└──────┬──────────────┘
       │  PipelineResult
       ▼
┌─────────────────────┐
│  ④ 发送回复          │  callbacks.send_response()
└─────────────────────┘
```

## 第一步：创建频道适配器

在 `nbot/channels/` 下创建 `<name>.py`，继承 `BaseChannelAdapter`。

### 必须覆写

| 方法 | 说明 |
|------|------|
| `channel_name` | 频道唯一标识，如 `"discord"` |
| `get_capabilities()` | 声明传输层能力 |
| `build_envelope(**kwargs)` | 构建 `ChannelEnvelope`（会话 ID、发送者等） |

### 可选覆写

| 方法 | 说明 |
|------|------|
| `normalize_inbound_message(content)` | 归一化输入（去除 @提及、Markdown 等） |
| `parse_xxx(raw)` | 解析原始消息载荷的便捷方法 |

### 示例

```python
# nbot/channels/discord.py

from typing import Any, Dict, Optional
from nbot.channels.base import BaseChannelAdapter, ChannelCapabilities, ChannelEnvelope

class DiscordChannelAdapter(BaseChannelAdapter):
    channel_name = "discord"

    def get_capabilities(self) -> ChannelCapabilities:
        return ChannelCapabilities(
            supports_stream=False,
            supports_progress_updates=False,
            supports_file_send=False,
            supports_stop=False,
        )

    def build_envelope(self, **kwargs) -> ChannelEnvelope:
        metadata = dict(kwargs.get("metadata") or {})
        chat_id = metadata.get("channel_id") or kwargs.get("conversation_id", "")
        return ChannelEnvelope(
            channel=self.channel_name,
            conversation_id=f"discord:{chat_id}",
            user_id=kwargs.get("user_id", ""),
            sender=kwargs.get("sender", "discord_user"),
            attachments=list(kwargs.get("attachments") or []),
            metadata=metadata,
        )

    def parse_message(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """解析 Discord 消息事件 → 标准格式"""
        # 根据实际 API 格式调整
        msg = event.get("d", {})
        author = msg.get("author", {})
        content = msg.get("content", "")
        if not content:
            return None

        return {
            "channel_id": msg.get("channel_id"),
            "message_id": msg.get("id"),
            "user_id": author.get("id"),
            "sender": author.get("username", "discord_user"),
            "content": self.normalize_inbound_message(content),
            "metadata": {"guild_id": msg.get("guild_id")},
        }
```

### 能力声明说明

`ChannelCapabilities` 描述的是**传输层**能做什么，不是 AI 能力：

| 字段 | 含义 | 何时设为 `True` |
|------|------|-----------------|
| `supports_stream` | 传输层支持流式推送 | WebSocket 可实时推送 token |
| `supports_progress_updates` | 传输层可展示进度 | UI 有进度卡片等组件 |
| `supports_file_send` | 传输层可发送文件 | API 支持文件上传/发送 |
| `supports_stop` | 传输层可中断生成 | 有取消机制（如 SocketIO `stop` 事件） |

> 工具调用、知识库、工作区等是管道自动提供的，**不依赖能力声明**。

## 第二步：实现管道回调

在 `nbot/services/` 下创建 `xxx_service.py`，实现 `PipelineCallbacks` 子类。

### 最少覆写（3 个方法 = 全套 AI 能力）

```python
# nbot/services/discord_service.py

from typing import Any, Dict, List
from nbot.core.ai_pipeline import (
    PipelineContext, PipelineCallbacks, PipelineResult,
    AIPipeline, handle_tool_confirmation,
)

class DiscordCallbacks(PipelineCallbacks):

    def __init__(self, server, token, parsed):
        self.server = server
        self.token = token
        self.parsed = parsed

    # ---- 必须覆写 ----

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        """返回系统提示词。"""
        return str(
            getattr(self.server, "personality", {}).get("systemPrompt") or ""
        ).strip()

    def send_response(self, ctx: PipelineContext, message: Dict[str, Any]) -> None:
        """发送回复到 Discord。"""
        send_discord_message(
            self.token,
            self.parsed["channel_id"],
            message.get("content", ""),
        )

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        """提供 session_id 给工具和确认流程。"""
        return {
            "session_id": f"discord:{self.parsed['channel_id']}",
            "session_type": "discord",
        }
```

这 3 个方法就足够让频道拥有：**知识库检索 + 工具调用 + 工作区操作 + exec 确认流程**。

### 增强覆写（加会话历史）

```python
    def load_messages(self, ctx: PipelineContext) -> List[Dict[str, Any]]:
        """加载历史会话。"""
        messages = []
        system = self.get_system_prompt(ctx)
        if system:
            messages.append({"role": "system", "content": system})
        # 可从 session_store 或外部存储加载历史
        history = get_discord_history(self.parsed["channel_id"])
        messages.extend(history)
        return messages

    def save_assistant_message(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """保存 AI 回复到历史。"""
        save_discord_message(self.parsed["channel_id"], message)
```

### 全部可覆写方法

| 方法 | 默认行为 | 覆写场景 |
|------|---------|---------|
| `load_messages(ctx)` | 仅返回 system + 当前消息 | 需要会话持久化 |
| `get_system_prompt(ctx)` | 返回 `""` | **必须覆写** |
| `save_assistant_message(ctx, msg)` | 无操作 | 需要会话持久化 |
| `send_response(ctx, msg)` | 抛出 `NotImplementedError` | **必须覆写** |
| `build_model_call(ctx, tools)` | 标准 HTTP API 调用 | 使用自定义 API 端点 |
| `build_model_call_streaming(ctx, tools)` | 返回 `None` | 支持流式输出 |
| `on_stream_start/chunk/end(ctx, ...)` | 无操作 | 支持流式输出 |
| `get_progress_reporter(ctx)` | 返回 `NoOpProgressReporter` | 需要进度展示 |
| `on_confirmation_required(ctx, id, cmd)` | 无操作 | 自定义确认通知方式 |
| `check_confirmation(ctx, input)` | 返回 `None` | 自定义确认检测（通常用共用函数） |
| `search_knowledge(ctx, query)` | 返回 `""` | 自定义知识库检索逻辑 |
| `ensure_workspace(ctx)` | 返回 `""` | 启用工作区文件管理 |
| `get_workspace_context(ctx)` | 返回 `{}` | **必须覆写**（让工具能用） |
| `resolve_attachment_data(ctx, att)` | 返回 `None` | 平台特定的附件下载/解析 |
| `on_response_complete(ctx, result)` | 无操作 | 统计、通知、后处理 |

## 第三步：编写入口函数

```python
def answer_discord_event(server, channel_config, event):
    """Discord Webhook 入口。"""
    adapter = DiscordChannelAdapter()
    parsed = adapter.parse_message(event or {})
    if not parsed:
        return {"ok": True, "ignored": True}

    # ---- 确认/拒绝检测（共性问题，用共用函数一行搞定） ----
    content = handle_tool_confirmation(
        parsed["content"],
        f"discord:{parsed['channel_id']}",
        log_prefix="Discord",
    )

    # ---- 构建管道上下文 ----
    chat_request = adapter.build_chat_request(
        conversation_id=f"discord:{parsed['channel_id']}",
        user_id=parsed["user_id"],
        content=content,
        sender=parsed.get("sender", "discord_user"),
        metadata=parsed.get("metadata", {}),
    )

    ctx = PipelineContext(chat_request=chat_request, adapter=adapter)
    callbacks = DiscordCallbacks(server, token, parsed)

    # ---- 获取工具列表 ----
    tools = None
    try:
        from nbot.services.tools import get_enabled_tools
        tools = get_enabled_tools()
    except Exception:
        pass

    # ---- 运行管道 ----
    pipeline = AIPipeline()
    result = pipeline.process(ctx, callbacks, tools=tools)

    if result.error:
        return {"ok": False, "error": result.error}
    return {"ok": True, "result": result.final_content}
```

## 第四步：注册适配器

在 `nbot/channels/__init__.py` 中添加：

```python
from nbot.channels.discord import DiscordChannelAdapter
register_channel_adapter(DiscordChannelAdapter.channel_name, DiscordChannelAdapter)
```

## 第五步：挂载入口（按需）

### Webhook 模式

在 `nbot/web/routes/channels.py` 添加 webhook 路由：

```python
@channels_bp.route("/discord/<channel_id>/webhook", methods=["POST"])
def discord_webhook(channel_id):
    from nbot.services.discord_service import answer_discord_event
    return answer_discord_event(server, channel_config, request.json)
```

### WebSocket / 长连接模式

在 `nbot/services/` 下创建 `xxx_ws_service.py`，管理连接生命周期，收消息时调用入口函数。

### 预设注册

在 `nbot/web/routes/channels.py` 的 `PRESET_CHANNELS` 中添加预设，让用户可在 Web UI 界面中添加频道：

```python
PRESET_CHANNELS = [
    # ... 已有预设
    {
        "id": "discord",
        "name": "Discord",
        "type": "discord",
        "transport": "webhook",
        "config": {
            "bot_token": "",
            "webhook_url": "",
        },
    },
]
```

## 完整文件清单

新增一个频道需要创建/修改的文件：

| 文件 | 操作 | 行数估计 |
|------|------|---------|
| `nbot/channels/<name>.py` | **新建** - 适配器 | ~40 行 |
| `nbot/services/<name>_service.py` | **新建** - 回调 + 入口 | ~80 行 |
| `nbot/channels/__init__.py` | **修改** - 注册 | +2 行 |
| `nbot/web/routes/channels.py` | **修改** - 路由 + 预设 | +30 行 |

## 管道处理流程

```
入口函数
  │
  ├─ handle_tool_confirmation()   ← 确认/拒绝检测
  │
  └─ pipeline.process(ctx, callbacks)
       │
       ├─ Phase 1: 附件解析
       │    → callbacks.resolve_attachment_data()
       │
       ├─ Phase 2: 知识库检索
       │    → callbacks.search_knowledge() → inject_knowledge_context()
       │
       ├─ Phase 3: 上下文准备
       │    → callbacks.load_messages() → prepare_chat_context() → trim
       │
       ├─ Phase 4: AI 响应
       │    ├─ 有 tools → run_tool_loop_session()   ← 工具循环
       │    ├─ 有 streamer → 流式输出              ← 逐 token 推送
       │    └─ 其他 → callbacks.build_model_call()  ← 单次调用
       │
       └─ Phase 5: 结果组装
            → build_assistant_message()
            → callbacks.save_assistant_message()
            → callbacks.send_response()
            → callbacks.on_response_complete()
```

## 工具确认流程

`exec_command` 等危险工具需要用户确认后才能执行。管道共用函数 `handle_tool_confirmation()` 已处理此流程，所有频道统一使用：

```
用户: "运行 python --version"
  → AI 调用 exec_command
  → 返回确认请求: "⚠️ 命令需要确认: python --version"
  
用户: "确认"
  → handle_tool_confirmation() 检测关键词
  → execute_pending_command()
  → 生成系统消息: "[系统] 用户已确认执行命令 `python --version`"
  → 管道继续，AI 获得执行结果
```

确认/拒绝关键词：

| 确认 | 拒绝 |
|------|------|
| 确认、同意、确认执行、是、yes、y、ok、执行 | 取消、拒绝、否、不执行、no、n、cancel |

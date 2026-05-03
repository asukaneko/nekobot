# ai_pipeline - 统一 AI 管道中间件

## 概述

`ai_pipeline.py` 是 NekoBot 本轮架构改造的核心成果。它将**知识库检索、工具调用、工作区管理、附件解析、流式输出、进度报告**等通用能力抽入统一管道，所有频道只需提供轻量传输层回调即可享用全部 AI 能力。

**设计目的**：消除各频道 handler 中大量重复的 AI 处理逻辑。

## 架构对比

### 改造前

```
Web 频道:  trigger_ai_response() → 自己实现知识库/工具/流式/进度 (~2540 行)
QQ 频道:   _run_qq_chat_request() → 自己实现知识库/工具循环 (~1169 行)
飞书 WS:   _process_ai_response() → 简单 chat_completion，无工具/知识库 (~431 行)
Telegram:  answer_telegram_update() → 简单 chat_completion，无历史 (~121 行)
飞书 Hook: answer_feishu_event() → 简单 chat_completion (~280 行)
```

### 改造后

```
所有频道 → AIPipeline.process() → 统一处理
                │
                ├─ Phase 1: 附件解析
                ├─ Phase 2: 知识库检索
                ├─ Phase 3: 上下文准备
                ├─ Phase 4: AI 响应 (工具循环 / 流式 / 单次调用)
                └─ Phase 5: 结果组装 & 回复

每个频道只需实现 PipelineCallbacks (30~150 行)
```

## 核心类

### PipelineContext

贯穿管道的上下文数据类，承载输入 → 中间状态 → 输出。

```python
@dataclass
class PipelineContext:
    # === 输入 ===
    chat_request: ChatRequest              # 频道适配器构建的请求
    adapter: Any                           # 频道适配器实例

    # === 管道填充的中间状态 ===
    messages: List[Dict]                   # 完整的消息上下文
    knowledge_text: str                    # 检索到的知识库文本
    image_urls: List[str]                  # 图片 URL/Base64
    file_contents: List[str]               # 附件文本内容
    tool_context: Dict[str, Any]           # 工具上下文 (session_id 等)
    stop_event: Optional[threading.Event]  # 停止信号

    # === 结果 ===
    final_content: str                     # 最终回复文本
    stopped_prematurely: bool              # 是否被提前停止
    tool_trace: List[Dict]                 # 工具调用轨迹
    usage: Dict                            # token 用量
    error: Optional[str]                   # 错误信息
```

### PipelineResult

管道返回结果，可转为 `ChatResponse`。

```python
@dataclass
class PipelineResult:
    final_content: str
    assistant_message: Optional[Dict]
    tool_trace: List[Dict]        # 用于「继续」功能
    can_continue: bool
    stopped_prematurely: bool
    usage: Dict
    error: Optional[str]

    def to_chat_response(self) -> ChatResponse: ...
```

### PipelineCallbacks

频道需实现的回调基类。所有方法都有默认空实现，**最少只需覆写 3 个方法**。

```python
class PipelineCallbacks(ABC):
    # ---- 必须覆写 ----
    def get_system_prompt(self, ctx) -> str: ...
    def send_response(self, ctx, message) -> None: ...
    def get_workspace_context(self, ctx) -> Dict: ...

    # ---- 增强覆写 ----
    def load_messages(self, ctx) -> List[Dict]: ...
    def save_assistant_message(self, ctx, message) -> None: ...
    def search_knowledge(self, ctx, query) -> str: ...
    def build_model_call(self, ctx, tools) -> Callable: ...
    def build_model_call_streaming(self, ctx, tools) -> Optional[Callable]: ...
    def resolve_attachment_data(self, ctx, att) -> Optional[Dict]: ...
    def ensure_workspace(self, ctx) -> str: ...

    # ---- 流式回调 ----
    def on_stream_start(self, ctx, message) -> None: ...
    def on_stream_chunk(self, ctx, chunk, message_id) -> None: ...
    def on_stream_end(self, ctx, message_id) -> None: ...

    # ---- 进度 ----
    def get_progress_reporter(self, ctx) -> ProgressReporter: ...

    # ---- 确认 ----
    def on_confirmation_required(self, ctx, request_id, command) -> None: ...
    def check_confirmation(self, ctx, user_input) -> Optional[str]: ...

    # ---- 后处理 ----
    def on_response_complete(self, ctx, result) -> None: ...
```

### AIPipeline

管道主类，全局单例 `ai_pipeline`。

```python
class AIPipeline:
    def process(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        *,
        tools: Optional[List[Dict]] = None,    # None = 不使用工具
        max_tool_iterations: int = 50,
        max_context_chars: int = 100000,
    ) -> PipelineResult:
```

### ProgressReporter

抽象进度报告接口。Web 频道通过 `WebProgressReporter` 封装 `ProgressCardManager` 和 `TodoCardManager`，其他频道使用 `NoOpProgressReporter`。

```python
class ProgressReporter(ABC):
    def on_thinking_start(self, ctx) -> None: ...
    def on_thinking_content(self, ctx, content) -> None: ...
    def on_knowledge_start(self, ctx) -> None: ...
    def on_knowledge_done(self, ctx, retrieved) -> None: ...
    def on_tool_start(self, ctx, tool_name, arguments, thinking) -> None: ...
    def on_tool_done(self, ctx, tool_name, result, thinking) -> None: ...
    def on_tool_iteration(self, ctx, iteration) -> None: ...
    def on_attachment_start(self, ctx, count) -> None: ...
    def on_attachment_item(self, ctx, name, item_type) -> None: ...
    def on_attachment_item_done(self, ctx, name, success, preview) -> None: ...
    def on_waiting_confirmation(self, ctx, command, request_id) -> None: ...
    def on_done(self, ctx) -> None: ...
```

## 管道处理流程

```
pipeline.process(ctx, callbacks, tools=...)
│
├─ Phase 1: _phase_attachments()
│    → 遍历 ctx.chat_request.attachments
│    → 分类: 图片 / 文本 / 文档
│    → callbacks.resolve_attachment_data() 解析文件
│    → 填充 ctx.image_urls / ctx.file_contents
│
├─ Phase 2: _phase_knowledge()
│    → callbacks.search_knowledge(ctx, query)
│    → 设置 ctx.knowledge_text
│
├─ Phase 3: _phase_prepare_context()
│    → callbacks.load_messages() 加载历史
│    → 追加当前用户消息 + 附件内容
│    → prepare_chat_context() 修剪、注入知识
│    → 设置 ctx.messages / ctx.tool_call_history
│
├─ Phase 4: _phase_ai_response()
│    ├─ tools=None 且有流式 → _run_streaming() 逐 token 回调
│    ├─ tools 非空 → _run_tool_loop() 工具调用循环
│    │    → ToolLoopSession + ToolLoopHooks → ProgressReporter
│    │    → execute_tool() 执行工具
│    │    → require_confirmation → ToolLoopExit → 等待确认
│    └─ 其他 → _run_simple() 单次模型调用
│
└─ Phase 5: _phase_assemble_result()
     → adapter.build_assistant_message()
     → callbacks.save_assistant_message() 持久化
     → callbacks.send_response() 发送回复
     → callbacks.on_response_complete() 后处理
```

## 共用函数

### handle_tool_confirmation()

各频道入口处统一调用，处理 `exec_command` 等工具的确认/拒绝流程。

```python
def handle_tool_confirmation(
    content: str,
    session_id: str,
    *,
    log_prefix: str = "",
) -> str:
    """检测确认/拒绝关键词，执行或拒绝待处理命令。
    
    Returns:
        替换后的消息内容（原内容 或 确认/拒绝结果文本）
    """
```

确认关键词：`确认` `同意` `确认执行` `是` `yes` `y` `ok` `执行`

拒绝关键词：`取消` `拒绝` `否` `不执行` `no` `n` `cancel`

## 工具确认流程

```
用户: "运行 python --version"
  → AI 调用 exec_command
  → execute_tool() 返回 {"require_confirmation": True, "request_id": "xxx"}
  → 管道抛出 ToolLoopExit
  → 返回确认请求消息给用户

用户: "确认"
  → 频道入口调用 handle_tool_confirmation("确认", session_id)
  → 检测到确认关键词 → execute_pending_command("xxx")
  → 返回: "[系统] 用户已确认执行命令 `python --version`。\n\n执行结果:..."
  → content 被替换为执行结果
  → 管道继续，AI 获得执行结果并回复
```

## 各频道回调实现

| 频道 | 回调类 | 覆写方法数 | 特殊实现 |
|------|--------|-----------|---------|
| Web | `WebCallbacks` | 12 | `WebProgressReporter` 封装 ProgressCard/TodoCard；SocketIO 流式/确认推送 |
| QQ | `QQCallbacks` | 8 | `QQSessionStore` 历史加载；`check_confirmation` 关键词检测 |
| 飞书 WS | `FeishuChatCallbacks` | 6 | `WebSessionStore` 历史；`get_workspace_context` 启用工具 |
| 飞书 Webhook | `FeishuCallbacks` | 4 | 简单单轮（无历史持久化） |
| Telegram | `TelegramCallbacks` | 4 | 简单单轮（无历史持久化） |

## 使用示例

### 最简集成（Telegram/飞书 Webhook 风格）

```python
from nbot.core.ai_pipeline import (
    AIPipeline, PipelineContext, PipelineCallbacks, handle_tool_confirmation,
)

class MyCallbacks(PipelineCallbacks):
    def get_system_prompt(self, ctx):
        return "You are helpful."

    def send_response(self, ctx, msg):
        send_to_platform(msg["content"])

    def get_workspace_context(self, ctx):
        return {"session_id": self.chat_id, "session_type": "my_platform"}

def answer(server, raw_event):
    parsed = parse_event(raw_event)
    content = handle_tool_confirmation(parsed["content"], parsed["chat_id"])

    ctx = PipelineContext(
        chat_request=adapter.build_chat_request(content=content, ...),
        adapter=adapter,
    )
    callbacks = MyCallbacks(server, ...)

    pipeline = AIPipeline()
    result = pipeline.process(ctx, callbacks, tools=get_enabled_tools())
```

### 完整集成（Web/QQ 风格）

覆写 `load_messages`、`save_assistant_message` 实现会话持久化；覆写 `get_progress_reporter` 实现进度展示；覆写流式回调实现打字机效果。详见 [新增频道指南](/guide/nbot/channels/add-channel.md)。

## 关键设计决策

| 决策 | 原因 |
|------|------|
| `tools=None` 表示不使用工具（尝试流式） | 允许频道选择是否启用工具循环 |
| `tools=[]` 空列表 → 简单路径 | 已加载工具配置但无可用工具时不要报错 |
| 确认流程在管道入口处处理（不在管道内） | 确认需要中断当前消息并替换内容，必须提前拦截 |
| ProgressCard/TodoCard 保持 Web 专属 | 这些是 UI 组件，其他频道无对应概念 |
| `model_call` 有默认实现 | 简单频道无需关心 AI API 调用细节 |
| 管道不持有任何频道引用 | 通过回调完全解耦，新增频道不影响管道代码 |

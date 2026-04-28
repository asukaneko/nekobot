# chat_models - 聊天模型

## 概述

`chat_models.py` 定义了 NekoBot 统一 AI 核心的数据模型，包括 `ChatRequest` 和 `ChatResponse`，用于在不同频道间标准化消息传输。

## 核心类

### ChatRequest

统一的聊天请求模型，所有频道的输入都会转换为这个格式。

```python
@dataclass
class ChatRequest:
    channel: str                      # 频道标识 (qq/web/telegram)
    conversation_id: str              # 会话ID
    user_id: str                      # 用户ID
    content: str                      # 消息内容
    attachments: List[Attachment]     # 附件列表（图片、文件等）
    metadata: Dict[str, Any]          # 元数据（扩展信息）
    timestamp: datetime               # 时间戳
    reply_to: Optional[str]           # 回复的消息ID

@dataclass
class Attachment:
    type: str                         # 类型: image/file/audio/video
    url: Optional[str]                # URL地址
    content: Optional[bytes]          # 二进制内容
    mime_type: Optional[str]          # MIME类型
    filename: Optional[str]           # 文件名
```

### ChatResponse

统一的聊天响应模型，AI 的回复通过这个格式返回给频道。

```python
@dataclass
class ChatResponse:
    final_content: str                # 最终回复内容
    can_continue: bool                # 是否可以继续执行
    tool_trace: List[ToolCall]        # 工具调用记录
    thinking_cards: List[Dict]        # 思考过程卡片
    file_changes: List[Dict]          # 文件变更记录
    attachments: List[Attachment]     # 附件列表

@dataclass
class ToolCall:
    tool_name: str                    # 工具名称
    arguments: Dict[str, Any]         # 调用参数
    result: Any                       # 执行结果
    status: str                       # 状态: success/error/pending
```

## 使用示例

### 创建请求

```python
from nbot.core.chat_models import ChatRequest, Attachment

request = ChatRequest(
    channel="web",
    conversation_id="session_123",
    user_id="user_456",
    content="你好",
    attachments=[],
    metadata={"client_version": "1.0"}
)
```

### 创建响应

```python
from nbot.core.chat_models import ChatResponse

response = ChatResponse(
    final_content="你好！有什么可以帮助你的？",
    can_continue=False,
    tool_trace=[]
)
```

## 数据流向

```
QQ消息 → QQ适配器 → ChatRequest → AI核心 → ChatResponse → QQ适配器 → QQ回复
Web消息 → Web适配器 → ChatRequest → AI核心 → ChatResponse → Web适配器 → Web回复
Telegram消息 → Telegram适配器 → ChatRequest → AI核心 → ChatResponse → Telegram适配器 → Telegram回复
```

## 设计原则

1. **统一性** - 所有频道使用相同的数据模型
2. **扩展性** - 通过 metadata 和 attachments 支持扩展
3. **可追溯** - tool_trace 记录完整的工具调用链
4. **可恢复** - can_continue 支持中断后继续执行

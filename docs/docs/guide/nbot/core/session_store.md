# session_store - 会话存储

## 概述

`session_store.py` 负责管理用户和群组的会话数据，包括消息历史、上下文状态和临时数据。

## 核心类

### SessionStore

会话存储管理器，提供会话的读写操作。

```python
class SessionStore:
    def __init__(self, storage_path: str = "data/sessions"):
        self.storage_path = storage_path
        self.cache: Dict[str, Session] = {}  # 内存缓存
```

## 主要方法

### get_session(session_id: str) -> Session

获取或创建会话。

```python
from nbot.core.session_store import get_session_store

store = get_session_store()
session = store.get_session("session_123")
```

### save_message(session_id: str, message: Message)

保存消息到会话。

```python
message = {
    "role": "user",
    "content": "你好",
    "timestamp": datetime.now()
}
store.save_message("session_123", message)
```

### get_messages(session_id: str, limit: int = 20) -> List[Message]

获取会话的历史消息。

```python
messages = store.get_messages("session_123", limit=10)
for msg in messages:
    print(f"{msg['role']}: {msg['content']}")
```

### clear_session(session_id: str)

清空会话数据。

```python
store.clear_session("session_123")
```

## 会话数据结构

```python
@dataclass
class Session:
    id: str                           # 会话ID
    channel: str                      # 所属频道
    user_id: str                      # 用户ID
    messages: List[Message]           # 消息列表
    metadata: Dict[str, Any]          # 元数据
    created_at: datetime              # 创建时间
    updated_at: datetime              # 更新时间
    pending_task: Optional[Dict]      # 待继续的任务
```

## 存储方式

1. **内存缓存** - 活跃会话保存在内存中，快速访问
2. **文件持久化** - 定期保存到 JSON 文件
3. **自动清理** - 过期会话自动清理

## 使用示例

```python
from nbot.core.session_store import get_session_store

store = get_session_store()

# 保存用户消息
store.save_message("user_123", {
    "role": "user",
    "content": "你好"
})

# 保存助手回复
store.save_message("user_123", {
    "role": "assistant",
    "content": "你好！有什么可以帮助你的？"
})

# 获取完整对话历史
history = store.get_messages("user_123")
```

## 配置选项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| max_history | 最大历史消息数 | 20 |
| session_ttl | 会话过期时间 | 7天 |
| auto_save_interval | 自动保存间隔 | 5分钟 |

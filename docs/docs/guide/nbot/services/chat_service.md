# chat_service - 聊天服务

## 概述

`chat_service.py` 提供高层次的聊天服务接口，整合 AI 客户端、工具调用、会话管理等功能。

## ChatService 类

```python
from nbot.services.chat_service import ChatService

service = ChatService()
```

## 主要方法

### 处理消息

```python
async def process_message(
    self,
    session_id: str,
    content: str,
    user_id: str = None,
    attachments: list = None,
    context: dict = None
) -> ChatResponse:
    """处理用户消息"""
    pass
```

### 流式处理

```python
async def process_message_stream(
    self,
    session_id: str,
    content: str,
    user_id: str = None
) -> AsyncGenerator[str, None]:
    """流式处理消息"""
    async for chunk in service.process_message_stream(
        session_id="session_123",
        content="你好"
    ):
        print(chunk, end="")
```

### 继续执行

```python
async def continue_execution(
    self,
    session_id: str,
    tool_results: list
) -> ChatResponse:
    """继续执行中断的工具调用链"""
    pass
```

## 使用示例

### 基本对话

```python
from nbot.services.chat_service import ChatService

service = ChatService()

# 处理消息
response = await service.process_message(
    session_id="user_123",
    content="搜索今天的天气",
    user_id="user_123"
)

print(response.final_content)
```

### 带附件的消息

```python
response = await service.process_message(
    session_id="user_123",
    content="描述这张图片",
    attachments=[{
        "type": "image",
        "url": "https://example.com/image.jpg"
    }]
)
```

### 流式输出

```python
async for chunk in service.process_message_stream(
    session_id="user_123",
    content="写一段代码"
):
    # 实时输出到前端
    await websocket.send(chunk)
```

## 上下文管理

```python
# 获取会话上下文
context = service.get_context(session_id)

# 设置上下文变量
service.set_context(session_id, "key", "value")

# 清除上下文
service.clear_context(session_id)
```

## 工具调用流程

```
1. 用户发送消息
2. 构建 messages（包含历史记录）
3. 调用 AI 获取响应
4. 检查是否需要工具调用
5. 执行工具
6. 将工具结果加入 messages
7. 再次调用 AI
8. 返回最终响应
```

## 错误处理

```python
try:
    response = await service.process_message(...)
except AIServiceError as e:
    print(f"AI 服务错误: {e}")
except ToolExecutionError as e:
    print(f"工具执行错误: {e}")
except Exception as e:
    print(f"未知错误: {e}")
```

# agent_service - AI服务

## 概述

`agent_service.py` 是 NekoBot 的 AI 处理核心入口，负责协调频道适配器、AI 模型、工具调用和会话管理。

## 核心类

### AgentService

AI 服务的主类，处理所有聊天请求。

```python
class AgentService:
    def __init__(self):
        self.handlers: Dict[str, Handler] = {}      # 频道处理器
        self.tools: List[Dict] = []                  # 可用工具列表
        self.memory_enabled: bool = True             # 是否启用记忆
```

## 主要方法

### process(request: ChatRequest) -> ChatResponse

处理聊天请求的主入口。

```python
from nbot.core.agent_service import AgentService
from nbot.core.chat_models import ChatRequest

agent = AgentService()
request = ChatRequest(
    channel="web",
    conversation_id="session_123",
    user_id="user_456",
    content="搜索今天的天气"
)
response = agent.process(request)
print(response.final_content)
```

### register_handler(channel: str, handler: Handler)

注册频道处理器。

```python
def my_handler(request: ChatRequest) -> ChatResponse:
    # 自定义处理逻辑
    return ChatResponse(final_content="处理结果")

agent.register_handler("custom_channel", my_handler)
```

### 处理流程

1. **请求接收** - 接收 ChatRequest
2. **会话加载** - 从 SessionStore 加载历史消息
3. **提示词构建** - 组合系统提示词和用户消息
4. **AI调用** - 调用 AI 模型获取响应
5. **工具解析** - 解析并执行工具调用
6. **响应生成** - 构建 ChatResponse
7. **会话保存** - 保存更新后的会话

## 工具调用处理

```python
# AI 返回需要调用工具
if response.needs_tool_call:
    tool_result = execute_tool(
        tool_name=response.tool_name,
        arguments=response.tool_arguments
    )
    # 将工具结果加入上下文，再次调用 AI
    messages.append({
        "role": "tool",
        "content": tool_result
    })
```

## 继续执行机制

支持中断后继续执行未完成的工具调用链：

```python
# 检查是否有待继续的任务
pending_task = get_pending_task(request.conversation_id)
if pending_task:
    # 继续执行
    response = continue_execution(pending_task)
```

## 配置选项

| 配置项 | 说明 | 默认值 |
|--------|------|--------|
| max_iterations | 最大工具调用轮数 | 10 |
| timeout | 单次请求超时时间 | 300秒 |
| enable_streaming | 是否启用流式输出 | True |
| enable_tools | 是否启用工具调用 | True |

# web - Web适配器

## 概述

`web.py` 实现 Web 频道的适配器，处理 Web 后台的聊天请求，支持流式输出和完整的功能。

## WebChannelAdapter

```python
from nbot.channels.web import WebChannelAdapter

adapter = WebChannelAdapter()
```

## 能力声明

```python
def get_capabilities(self) -> ChannelCapabilities:
    return ChannelCapabilities(
        supports_stream=True,            # 支持流式输出
        supports_progress_updates=True,  # 支持进度更新
        supports_file_send=True,         # 支持发送文件
        supports_stop=True               # 支持停止生成
    )
```

## 消息处理

### 构建请求

```python
def build_chat_request(self, **kwargs) -> ChatRequest:
    """将 Web 消息转换为 ChatRequest"""
    return ChatRequest(
        channel="web",
        conversation_id=kwargs.get("session_id"),
        user_id=kwargs.get("user_id"),
        content=kwargs.get("content"),
        attachments=kwargs.get("attachments", []),
        metadata={
            "client_version": kwargs.get("version"),
            "browser": kwargs.get("user_agent")
        }
    )
```

## 流式输出

Web 适配器支持 Server-Sent Events (SSE) 流式输出：

```python
async def send_stream(self, conversation_id: str, stream_generator):
    """发送流式响应"""
    async for chunk in stream_generator:
        yield {
            "type": "content",
            "data": chunk
        }
```

## Socket.IO 集成

Web 适配器通过 Socket.IO 与前端实时通信：

```python
@socketio.on('chat_message')
async def handle_chat_message(data):
    # 构建 ChatRequest
    request = adapter.build_chat_request(
        session_id=data.get('session_id'),
        user_id=data.get('user_id'),
        content=data.get('content')
    )
    
    # 处理请求
    response = await agent_service.process(request)
    
    # 发送响应
    emit('chat_response', {
        'content': response.final_content,
        'thinking_cards': response.thinking_cards,
        'file_changes': response.file_changes
    })
```

## 文件上传处理

```python
async def handle_file_upload(self, session_id: str, file_data: bytes, filename: str):
    """处理文件上传"""
    # 保存到工作区
    workspace_path = workspace_manager.get_or_create(session_id)
    file_path = os.path.join(workspace_path, filename)
    
    with open(file_path, 'wb') as f:
        f.write(file_data)
    
    return {
        "success": True,
        "filename": filename,
        "path": file_path
    }
```

## 停止生成

```python
async def stop_generation(self, session_id: str):
    """停止当前生成"""
    # 设置停止标志
    self.stop_flags[session_id] = True
```

## 前端集成

```javascript
// 连接 Socket.IO
const socket = io();

// 发送消息
socket.emit('chat_message', {
    session_id: 'session_123',
    content: '你好'
});

// 接收流式响应
socket.on('chat_response', (data) => {
    if (data.type === 'stream') {
        appendContent(data.chunk);
    } else if (data.type === 'complete') {
        showThinkingCards(data.thinking_cards);
        showFileChanges(data.file_changes);
    }
});

// 停止生成
socket.emit('stop_generation', {
    session_id: 'session_123'
});
```

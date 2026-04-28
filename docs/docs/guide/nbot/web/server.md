# server - Web服务入口

## 概述

`server.py` 是 NekoBot Web 后台的服务入口，基于 Flask 和 Socket.IO 提供 Web 界面和 API 服务。

## WebServer 类

```python
from nbot.web.server import WebServer

server = WebServer(host="0.0.0.0", port=5000)
server.run()
```

## 主要功能

### 启动服务

```python
# 启动 Web 服务
server.run(debug=False, use_reloader=False)

# 获取实例
instance = WebServer.get_instance()
```

### 会话管理

```python
# 获取所有会话
sessions = server.sessions

# 获取特定会话
session = server.get_session("session_123")

# 创建新会话
session = server.create_session(user_id="user_123")
```

### 广播消息

```python
# 向所有客户端广播
server.broadcast("notification", {"message": "系统通知"})

# 向特定会话发送
server.emit_to_session("session_123", "message", {"content": "你好"})
```

## 路由注册

```python
from flask import Blueprint

# 创建蓝图
api_bp = Blueprint('api', __name__)

@api_bp.route('/api/chat', methods=['POST'])
def chat():
    # 处理聊天请求
    pass

# 注册蓝图
server.app.register_blueprint(api_bp)
```

## Socket.IO 事件

### 聊天事件

```python
@server.socketio.on('chat_message')
def handle_chat_message(data):
    session_id = data.get('session_id')
    content = data.get('content')
    
    # 处理消息
    response = process_message(session_id, content)
    
    # 发送响应
    emit('chat_response', {
        'content': response.final_content,
        'thinking_cards': response.thinking_cards
    })
```

### 文件上传

```python
@server.socketio.on('file_upload')
def handle_file_upload(data):
    session_id = data.get('session_id')
    file_data = data.get('file_data')
    filename = data.get('filename')
    
    # 保存文件到工作区
    result = workspace_manager.save_file(session_id, file_data, filename)
    
    emit('file_uploaded', result)
```

### 停止生成

```python
@server.socketio.on('stop_generation')
def handle_stop_generation(data):
    session_id = data.get('session_id')
    # 设置停止标志
    server.stop_flags[session_id] = True
```

## 配置

```python
# .env
WEB_PASSWORD=your_password    # Web 登录密码
WEB_HOST=0.0.0.0              # 监听地址
WEB_PORT=5000                 # 监听端口
```

## 启动模式

```python
# 仅 Web 模式
python bot.py --only-web

# QQ + Web 模式（默认）
python bot.py

# CLI + Web 模式
python bot.py --cli-and-web
```

## 前端集成

Web 服务提供静态文件服务：

```
nbot/web/
├── static/           # 静态资源
│   ├── css/
│   ├── js/
│   └── images/
└── templates/        # HTML 模板
    └── index.html
```

## API 列表

| 路径 | 方法 | 说明 |
|------|------|------|
| `/api/chat` | POST | 发送消息 |
| `/api/sessions` | GET | 获取会话列表 |
| `/api/sessions/<id>` | GET | 获取会话详情 |
| `/api/files` | POST | 上传文件 |
| `/api/files/<id>` | GET | 下载文件 |
| `/api/config` | GET/POST | 获取/更新配置 |

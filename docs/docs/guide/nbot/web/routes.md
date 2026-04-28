# routes - API路由

## 概述

`routes/` 目录包含 Web 后台的所有 API 路由定义，采用 Blueprint 模式组织。

## 路由结构

```
nbot/web/routes/
├── __init__.py
├── chat.py           # 聊天相关
├── sessions.py       # 会话管理
├── files.py          # 文件操作
├── config.py         # 配置管理
├── channels.py       # 频道管理
├── workflows.py      # 工作流
├── skills.py         # 技能管理
└── tools.py          # 工具配置
```

## 聊天路由 (chat.py)

```python
from flask import Blueprint, request, jsonify

bp = Blueprint('chat', __name__)

@bp.route('/api/chat', methods=['POST'])
def send_message():
    """发送消息"""
    data = request.json
    session_id = data.get('session_id')
    content = data.get('content')
    
    response = chat_service.process_message(session_id, content)
    return jsonify({
        "success": True,
        "content": response.final_content
    })

@bp.route('/api/chat/stream', methods=['POST'])
def send_message_stream():
    """流式发送消息"""
    def generate():
        for chunk in chat_service.process_message_stream(...):
            yield f"data: {chunk}\n\n"
    
    return Response(generate(), mimetype='text/event-stream')
```

## 会话路由 (sessions.py)

```python
@bp.route('/api/sessions', methods=['GET'])
def list_sessions():
    """获取会话列表"""
    sessions = session_store.list_sessions()
    return jsonify({"sessions": sessions})

@bp.route('/api/sessions/<session_id>', methods=['GET'])
def get_session(session_id):
    """获取会话详情"""
    session = session_store.get_session(session_id)
    return jsonify(session.to_dict())

@bp.route('/api/sessions/<session_id>', methods=['DELETE'])
def delete_session(session_id):
    """删除会话"""
    session_store.delete_session(session_id)
    return jsonify({"success": True})

@bp.route('/api/sessions/<session_id>/messages', methods=['GET'])
def get_messages(session_id):
    """获取会话消息"""
    messages = session_store.get_messages(session_id)
    return jsonify({"messages": messages})
```

## 文件路由 (files.py)

```python
@bp.route('/api/files', methods=['POST'])
def upload_file():
    """上传文件"""
    file = request.files['file']
    session_id = request.form.get('session_id')
    
    result = workspace_manager.save_uploaded_file(
        session_id, file.read(), file.filename
    )
    return jsonify(result)

@bp.route('/api/files/<path:filename>', methods=['GET'])
def download_file(filename):
    """下载文件"""
    return send_from_directory(
        workspace_manager.get_workspace(session_id),
        filename
    )

@bp.route('/api/workspaces/<session_id>/files', methods=['GET'])
def list_workspace_files(session_id):
    """列出工作区文件"""
    files = workspace_manager.list_files(session_id)
    return jsonify(files)
```

## 配置路由 (config.py)

```python
@bp.route('/api/config', methods=['GET'])
def get_config():
    """获取配置"""
    return jsonify({
        "ai_config": ai_config_loader.load(),
        "tools_config": tools_config_loader.load()
    })

@bp.route('/api/config/ai', methods=['POST'])
def update_ai_config():
    """更新 AI 配置"""
    config = request.json
    ai_config_loader.save(config)
    refresh_runtime_ai_config()
    return jsonify({"success": True})
```

## 频道路由 (channels.py)

```python
@bp.route('/api/channels', methods=['GET'])
def list_channels():
    """获取频道列表"""
    channels = channel_registry.list_channels()
    return jsonify({"channels": channels})

@bp.route('/api/channels', methods=['POST'])
def create_channel():
    """创建频道"""
    config = request.json
    channel_registry.register_configured_channel(config)
    return jsonify({"success": True})

@bp.route('/api/channels/<channel_id>', methods=['PUT'])
def update_channel(channel_id):
    """更新频道"""
    config = request.json
    channel_registry.update_channel(channel_id, config)
    return jsonify({"success": True})

@bp.route('/api/channels/<channel_id>', methods=['DELETE'])
def delete_channel(channel_id):
    """删除频道"""
    channel_registry.unregister_adapter(channel_id)
    return jsonify({"success": True})
```

## 工作流路由 (workflows.py)

```python
@bp.route('/api/workflows', methods=['GET'])
def list_workflows():
    """获取工作流列表"""
    workflows = workflow_engine.list_workflows()
    return jsonify({"workflows": workflows})

@bp.route('/api/workflows', methods=['POST'])
def create_workflow():
    """创建工作流"""
    workflow_data = request.json
    workflow = Workflow.from_dict(workflow_data)
    workflow_engine.register(workflow)
    return jsonify({"success": True, "id": workflow.id})

@bp.route('/api/workflows/<workflow_id>/execute', methods=['POST'])
def execute_workflow(workflow_id):
    """执行工作流"""
    context = request.json.get('context', {})
    result = workflow_engine.execute(workflow_id, context)
    return jsonify(result)
```

## 统一响应格式

```python
def success_response(data=None, message="操作成功"):
    return jsonify({
        "success": True,
        "message": message,
        "data": data
    })

def error_response(message="操作失败", code=400):
    return jsonify({
        "success": False,
        "error": message,
        "code": code
    }), code
```

## 认证中间件

```python
from functools import wraps

def require_auth(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        token = request.headers.get('Authorization')
        if not verify_token(token):
            return error_response("未授权", 401)
        return f(*args, **kwargs)
    return decorated_function

@bp.route('/api/protected', methods=['GET'])
@require_auth
def protected_route():
    return success_response()
```

# workspace - 工作区

## 概述

`workspace.py` 提供文件工作区管理，支持私有工作区（会话级别）和共享工作区（全局）。AI 可以通过工具调用读写工作区文件。

## 工作区类型

### 私有工作区 (private)

- 每个会话独立
- 路径: `data/workspaces/private/<session_id>/`
- 仅当前会话可访问

### 共享工作区 (shared)

- 所有会话共享
- 路径: `data/workspaces/_shared/`
- 所有会话都可访问

## 核心类

### WorkspaceManager

工作区管理器，提供文件操作接口。

```python
class WorkspaceManager:
    def create_file(self, session_id, filename, content)
    def read_file(self, session_id, filename)
    def edit_file(self, session_id, filename, old_content, new_content)
    def delete_file(self, session_id, filename)
    def list_files(self, session_id)
    def send_file(self, session_id, filename)
```

## 主要方法

### 创建文件

```python
from nbot.core.workspace import workspace_manager

result = workspace_manager.create_file(
    session_id="session_123",
    filename="hello.txt",
    content="Hello, World!"
)
# 返回: {"success": True, "path": "...", "filename": "hello.txt"}
```

### 读取文件

```python
result = workspace_manager.read_file(
    session_id="session_123",
    filename="hello.txt",
    start_line=1,      # 可选：起始行
    end_line=10        # 可选：结束行
)
# 返回: {"success": True, "content": "...", "path": "..."}
```

### 编辑文件

```python
result = workspace_manager.edit_file(
    session_id="session_123",
    filename="hello.txt",
    old_content="Hello",
    new_content="Hi"
)
# 返回: {"success": True, "diff_preview": "..."}
```

### 删除文件

```python
result = workspace_manager.delete_file(
    session_id="session_123",
    filename="hello.txt"
)
# 返回: {"success": True}
```

### 列出文件

```python
result = workspace_manager.list_files("session_123")
# 返回: {"success": True, "files": [{"name": "...", "size": ..., "modified": ...}]}
```

### 共享工作区操作

```python
# 创建共享文件
result = workspace_manager.create_shared_file("shared.txt", "共享内容")

# 读取共享文件
result = workspace_manager.read_shared_file("shared.txt")

# 列出共享文件
result = workspace_manager.list_shared_files()
```

## AI 工具集成

工作区通过工具调用与 AI 集成：

```python
# AI 可以调用的工作区工具
WORKSPACE_TOOLS = [
    "workspace_create_file",   # 创建文件
    "workspace_read_file",     # 读取文件
    "workspace_edit_file",     # 编辑文件
    "workspace_delete_file",   # 删除文件
    "workspace_list_files",    # 列出文件
    "workspace_send_file",     # 发送文件给用户
    "workspace_parse_file",    # 解析文件内容
    "workspace_file_info",     # 获取文件信息
]
```

## 文件变更追踪

每次文件操作都会记录变更信息：

```python
{
    "action": "created",           # created/modified/deleted
    "filename": "test.txt",
    "scope": "private",            # private/shared
    "before_preview": "...",       # 变更前内容预览
    "after_preview": "...",        # 变更后内容预览
    "diff_preview": "...",         # diff 对比
    "path": "/absolute/path"
}
```

## 使用示例

### 完整工作流程

```python
from nbot.core.workspace import workspace_manager

session_id = "user_123"

# 1. 创建代码文件
workspace_manager.create_file(
    session_id,
    "script.py",
    "print('Hello, World!')"
)

# 2. 读取文件内容
result = workspace_manager.read_file(session_id, "script.py")
print(result["content"])

# 3. 编辑文件
workspace_manager.edit_file(
    session_id,
    "script.py",
    "print('Hello, World!')",
    "print('Hello, NekoBot!')"
)

# 4. 列出所有文件
files = workspace_manager.list_files(session_id)
for f in files.get("files", []):
    print(f"{f['name']} - {f['size']} bytes")

# 5. 删除文件
workspace_manager.delete_file(session_id, "script.py")
```

## 支持的文件类型

- **文本文件** - .txt, .md, .json, .yaml, .py, .js, 等
- **代码文件** - 支持语法高亮和 diff 预览
- **文档文件** - PDF, DOCX, PPT, Excel（通过解析器）
- **图片文件** - 支持预览和发送

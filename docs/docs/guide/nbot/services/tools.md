# tools - 工具系统

## 概述

`tools.py` 提供 AI 可调用的工具系统，包括内置工具、工作区工具和动态工具。支持工具注册、执行和管理。

## 内置工具

### 新闻搜索

```python
result = ToolExecutor.search_news(
    query="科技",
    count=5,
    source="all"  # all, 36kr, ithome, huxiu, sspai
)
```

### 天气查询

```python
result = ToolExecutor.get_weather(city="北京")
# 返回: {"success": True, "weather": {...}}
```

### 网页搜索

```python
result = ToolExecutor.search_web(
    query="Python 教程",
    num_results=3
)
```

### 获取时间

```python
result = ToolExecutor.get_date_time()
# 返回: {"date": "2025-01-01", "time": "12:00:00", ...}
```

### HTTP 请求

```python
result = ToolExecutor.http_get("https://api.example.com/data")
```

### 图片理解

```python
result = ToolExecutor.understand_image(
    prompt="描述这张图片",
    image_source="https://example.com/image.jpg"
)
```

### 命令执行

```python
result = ToolExecutor.exec_command(
    command="ls -la",
    timeout=30
)
```

### 文件下载

```python
result = ToolExecutor.download_file(
    url="https://example.com/file.pdf",
    filename="document.pdf"
)
```

## 工作区工具

AI 可以通过工具调用操作工作区文件：

```python
# 创建文件
workspace_create_file(filename="test.txt", content="Hello", scope="private")

# 读取文件
workspace_read_file(filename="test.txt", scope="private")

# 编辑文件
workspace_edit_file(filename="test.txt", old_content="Hello", new_content="Hi")

# 删除文件
workspace_delete_file(filename="test.txt", scope="private")

# 列出文件
workspace_list_files(scope="all", recursive=True)

# 发送文件
workspace_send_file(filename="test.txt", scope="private")

# 解析文件
workspace_parse_file(filename="document.pdf", scope="private")

# 获取文件信息
workspace_file_info(filename="data.xlsx", scope="private")
```

## 工具注册

### 使用装饰器注册

```python
from nbot.services.tool_registry import register_tool

@register_tool("my_tool")
def my_tool(arguments: dict, context: dict = None) -> dict:
    """工具描述"""
    param = arguments.get("param")
    return {
        "success": True,
        "result": f"处理结果: {param}"
    }
```

### 工具定义格式

```python
TOOL_DEFINITION = {
    "type": "function",
    "function": {
        "name": "my_tool",
        "description": "工具描述",
        "parameters": {
            "type": "object",
            "properties": {
                "param": {
                    "type": "string",
                    "description": "参数说明"
                }
            },
            "required": ["param"]
        }
    }
}
```

## 工具执行

```python
from nbot.services.tools import execute_tool

result = execute_tool(
    tool_name="search_web",
    arguments={"query": "Python"},
    context={"session_id": "xxx", "user_id": "xxx"}
)
```

## 命令执行确认机制

对于非白名单命令，系统会要求用户确认：

```python
# 白名单命令直接执行
result = exec_command("ls -la")  # 立即执行

# 非白名单命令返回确认请求
result = exec_command("rm -rf /")  # 返回 require_confirmation: True

# 用户确认后执行
execute_pending_command(request_id)
```

### 白名单配置

```python
EXEC_WHITELIST = {
    'ls', 'cat', 'echo', 'pwd', 'whoami', 'date',
    'head', 'tail', 'wc', 'grep', 'find', 'ps',
    'ping', 'uname', 'hostname', 'which', 'file'
}
```

## 文件变更追踪

工作区工具会自动记录文件变更：

```python
{
    "action": "modified",      # created/modified/deleted
    "filename": "test.txt",
    "scope": "private",
    "before_preview": "...",
    "after_preview": "...",
    "diff_preview": "..."
}
```

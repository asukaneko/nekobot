# todo_tools - 待办工具

## 概述

`todo_tools.py` 提供待办事项管理工具，AI 可以通过工具调用创建、管理和追踪待办事项。

## 待办工具列表

```python
TODO_TOOLS = [
    "todo_create",      # 创建待办
    "todo_list",        # 列出待办
    "todo_update",      # 更新待办
    "todo_delete",      # 删除待办
    "todo_complete",    # 完成待办
]
```

## 工具详解

### 创建待办

```python
result = execute_todo_tool("todo_create", {
    "title": "完成文档编写",
    "description": "编写 API 文档",
    "priority": "high",  # low/medium/high
    "due_date": "2025-01-15",
    "tags": ["文档", "API"]
}, context={"session_id": "xxx"})
```

### 列出待办

```python
result = execute_todo_tool("todo_list", {
    "status": "pending",  # all/pending/completed
    "priority": "high",   # 可选筛选
    "tag": "文档"         # 可选筛选
})

# 返回
{
    "success": True,
    "todos": [
        {
            "id": "todo_1",
            "title": "完成文档编写",
            "status": "pending",
            "priority": "high",
            "created_at": "2025-01-01T12:00:00"
        }
    ]
}
```

### 更新待办

```python
result = execute_todo_tool("todo_update", {
    "todo_id": "todo_1",
    "title": "更新后的标题",
    "priority": "medium",
    "due_date": "2025-01-20"
})
```

### 完成待办

```python
result = execute_todo_tool("todo_complete", {
    "todo_id": "todo_1"
})

# 返回
{
    "success": True,
    "message": "待办已完成",
    "todo": {
        "id": "todo_1",
        "status": "completed",
        "completed_at": "2025-01-01T15:00:00"
    }
}
```

### 删除待办

```python
result = execute_todo_tool("todo_delete", {
    "todo_id": "todo_1"
})
```

## 待办数据结构

```python
@dataclass
class TodoItem:
    id: str
    title: str
    description: str
    status: str           # pending/completed
    priority: str         # low/medium/high
    due_date: Optional[str]
    tags: List[str]
    created_at: datetime
    completed_at: Optional[datetime]
    session_id: str       # 所属会话
```

## 存储

待办数据存储在 `data/todos/` 目录下，按会话分文件存储：

```
data/todos/
├── session_123.json
├── session_456.json
└── ...
```

## AI 集成示例

```python
# AI 可以主动创建待办来追踪任务
"""
用户：帮我规划项目

AI：我来为您创建项目规划待办事项
[调用 todo_create] 创建需求分析待办
[调用 todo_create] 创建设计文档待办
[调用 todo_create] 创建开发实现待办

已为您创建 3 个待办事项，您可以使用以下命令管理：
- 查看所有待办
- 标记待办完成
- 更新待办进度
"""
```

## Web 界面

Web 后台提供待办管理界面：

- 查看所有待办列表
- 按状态/优先级筛选
- 创建/编辑/删除待办
- 标记完成
- 查看待办统计

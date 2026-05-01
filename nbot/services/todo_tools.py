"""
Todo 工具模块 - 提供 AI 待办事项管理功能
用于复杂任务的管理和跟踪
"""
import logging
from typing import Dict, Any, List
from datetime import datetime

_log = logging.getLogger(__name__)


class TodoManager:
    """待办事项管理器（按会话隔离）"""
    
    def __init__(self):
        self._todos: Dict[str, List[Dict]] = {}  # session_id -> todo_list
        self._counter: Dict[str, int] = {}  # session_id -> id_counter
    
    def _get_session_todos(self, session_id: str) -> List[Dict]:
        """获取指定会话的待办事项列表"""
        if session_id not in self._todos:
            self._todos[session_id] = []
            self._counter[session_id] = 1
        return self._todos[session_id]
    
    def add_todo(self, session_id: str, content: str, priority: str = "medium") -> Dict:
        """添加待办事项"""
        todos = self._get_session_todos(session_id)
        todo_id = self._counter[session_id]
        self._counter[session_id] += 1
        
        todo = {
            "id": todo_id,
            "content": content,
            "status": "pending",
            "priority": priority,
            "created_at": datetime.now().isoformat(),
            "completed_at": None
        }
        todos.append(todo)
        
        _log.info(f"[Todo] Added todo {todo_id} for session {session_id[:8]}...")
        
        return {
            "success": True,
            "message": f"已添加待办事项 (ID: {todo_id})",
            "todo": todo
        }
    
    def list_todos(self, session_id: str, status: str = None) -> Dict:
        """列出待办事项"""
        todos = self._get_session_todos(session_id)
        
        if status:
            filtered = [t for t in todos if t["status"] == status]
        else:
            filtered = todos
        
        # 按优先级排序：high > medium > low
        priority_order = {"high": 0, "medium": 1, "low": 2}
        sorted_todos = sorted(filtered, key=lambda x: priority_order.get(x["priority"], 1))
        
        pending_count = len([t for t in todos if t["status"] == "pending"])
        completed_count = len([t for t in todos if t["status"] == "completed"])
        
        return {
            "success": True,
            "todos": sorted_todos,
            "total": len(todos),
            "pending": pending_count,
            "completed": completed_count
        }
    
    def complete_todo(self, session_id: str, todo_id: int) -> Dict:
        """完成待办事项"""
        todos = self._get_session_todos(session_id)
        
        for todo in todos:
            if todo["id"] == todo_id:
                if todo["status"] == "completed":
                    return {
                        "success": False,
                        "error": f"待办事项 {todo_id} 已经完成"
                    }
                
                todo["status"] = "completed"
                todo["completed_at"] = datetime.now().isoformat()
                
                _log.info(f"[Todo] Completed todo {todo_id} for session {session_id[:8]}...")
                
                return {
                    "success": True,
                    "message": f"已完成待办事项: {todo['content']}",
                    "todo": todo
                }
        
        return {
            "success": False,
            "error": f"未找到待办事项 ID: {todo_id}"
        }
    
    def delete_todo(self, session_id: str, todo_id: int) -> Dict:
        """删除待办事项"""
        todos = self._get_session_todos(session_id)
        
        for i, todo in enumerate(todos):
            if todo["id"] == todo_id:
                deleted = todos.pop(i)
                _log.info(f"[Todo] Deleted todo {todo_id} for session {session_id[:8]}...")
                return {
                    "success": True,
                    "message": f"已删除待办事项: {deleted['content']}",
                    "deleted_todo": deleted
                }
        
        return {
            "success": False,
            "error": f"未找到待办事项 ID: {todo_id}"
        }
    
    def clear_todos(self, session_id: str, status: str = None) -> Dict:
        """清空待办事项"""
        todos = self._get_session_todos(session_id)
        
        if status:
            original_count = len(todos)
            todos[:] = [t for t in todos if t["status"] != status]
            deleted_count = original_count - len(todos)
            _log.info(f"[Todo] Cleared {deleted_count} {status} todos for session {session_id[:8]}...")
            return {
                "success": True,
                "message": f"已清空 {deleted_count} 个{status}状态的待办事项"
            }
        else:
            count = len(todos)
            todos.clear()
            _log.info(f"[Todo] Cleared all {count} todos for session {session_id[:8]}...")
            return {
                "success": True,
                "message": f"已清空所有 {count} 个待办事项"
            }


# 全局 Todo 管理器实例
todo_manager = TodoManager()


# Todo 工具定义
TODO_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "todo_add",
            "description": "添加待办事项。当需要记录任务、创建待办清单时使用此工具。每个待办事项有唯一的ID，可以用于后续操作。建议在面对复杂任务、多步骤操作或需要跟踪进度时使用此工具来管理任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "待办事项的内容描述，应清晰具体"
                    },
                    "priority": {
                        "type": "string",
                        "description": "优先级：high（高）、medium（中）、low（低），默认为 medium。对于复杂任务中的关键步骤，建议使用 high 优先级",
                        "enum": ["high", "medium", "low"],
                        "default": "medium"
                    }
                },
                "required": ["content"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_list",
            "description": "列出当前会话的所有待办事项。可以按状态筛选，结果按优先级排序。在复杂任务执行过程中，可以使用此工具查看当前进度和剩余任务。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "筛选状态：pending（待完成）、completed（已完成），不填则显示全部",
                        "enum": ["pending", "completed"]
                    }
                }
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_complete",
            "description": "将指定的待办事项标记为已完成。当完成一个任务步骤后，使用此工具更新待办状态。",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {
                        "type": "integer",
                        "description": "待办事项的唯一ID"
                    }
                },
                "required": ["todo_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_delete",
            "description": "删除指定的待办事项。当某个任务不再需要或创建错误时，使用此工具删除。",
            "parameters": {
                "type": "object",
                "properties": {
                    "todo_id": {
                        "type": "integer",
                        "description": "待办事项的唯一ID"
                    }
                },
                "required": ["todo_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "todo_clear",
            "description": "清空待办事项。可以清空所有事项或只清空特定状态的事项。任务全部完成后，可以使用此工具清理待办列表。",
            "parameters": {
                "type": "object",
                "properties": {
                    "status": {
                        "type": "string",
                        "description": "要清空的状态：completed（只清空已完成的）、不填则清空所有",
                        "enum": ["completed"]
                    }
                }
            }
        }
    }
]


def execute_todo_tool(tool_name: str, arguments: Dict[str, Any], context: Dict = None) -> Dict[str, Any]:
    """
    执行 Todo 相关工具
    
    Args:
        tool_name: 工具名称
        arguments: 工具参数
        context: 上下文信息，包含 session_id
        
    Returns:
        工具执行结果
    """
    if not context or not context.get('session_id'):
        return {"success": False, "error": "缺少会话信息，无法管理待办事项"}
    
    session_id = context['session_id']
    
    try:
        if tool_name == "todo_add":
            content = arguments.get('content', '')
            priority = arguments.get('priority', 'medium')
            
            if not content:
                return {"success": False, "error": "待办事项内容不能为空"}
            
            if priority not in ['high', 'medium', 'low']:
                priority = 'medium'
            
            return todo_manager.add_todo(session_id, content, priority)
        
        elif tool_name == "todo_list":
            status = arguments.get('status')
            return todo_manager.list_todos(session_id, status)
        
        elif tool_name == "todo_complete":
            todo_id = arguments.get('todo_id')
            if todo_id is None:
                return {"success": False, "error": "请提供待办事项ID (todo_id)"}
            
            return todo_manager.complete_todo(session_id, int(todo_id))
        
        elif tool_name == "todo_delete":
            todo_id = arguments.get('todo_id')
            if todo_id is None:
                return {"success": False, "error": "请提供待办事项ID (todo_id)"}
            
            return todo_manager.delete_todo(session_id, int(todo_id))
        
        elif tool_name == "todo_clear":
            status = arguments.get('status')
            return todo_manager.clear_todos(session_id, status)
        
        else:
            return {"success": False, "error": f"未知的 Todo 工具: {tool_name}"}
    
    except Exception as e:
        _log.error(f"Todo tool error: {tool_name} - {e}")
        return {"success": False, "error": f"执行 Todo 工具时出错: {str(e)}"}

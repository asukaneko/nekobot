"""
Todo 卡片管理模块
用于在 Web 端显示待办事项列表和完成状态
"""

import logging
import uuid
from datetime import datetime
from typing import Dict, List, Optional, Any
from enum import Enum

_log = logging.getLogger(__name__)


class TodoStatus(Enum):
    """待办事项状态"""
    PENDING = "pending"
    COMPLETED = "completed"


class TodoCard:
    """Todo 卡片类 - 用于展示待办事项列表"""

    def __init__(self, session_id: str, parent_message_id: str,
                 socketio=None, manager=None, is_agent: bool = False):
        """
        初始化 Todo 卡片

        Args:
            session_id: 会话ID
            parent_message_id: 父消息ID（关联到用户消息）
            socketio: SocketIO 实例，用于发送实时更新
            manager: TodoCardManager 实例
            is_agent: 是否为 Agent 模式（向前端传递的状态标记）
        """
        self.card_id = str(uuid.uuid4())
        self.session_id = session_id
        self.parent_message_id = parent_message_id
        self.socketio = socketio
        self.manager = manager
        self.is_agent = is_agent
        self.todos: List[Dict[str, Any]] = []
        self.is_completed = False
        self.created_at = datetime.now().isoformat()

    def add_todo(self, todo_id: int, content: str, priority: str = "medium"):
        """
        添加待办事项到卡片

        Args:
            todo_id: 待办事项ID
            content: 待办事项内容
            priority: 优先级 (high/medium/low)
        """
        priority_config = {
            'high': {'icon': '🔴', 'label': '高优先级'},
            'medium': {'icon': '🟡', 'label': '中优先级'},
            'low': {'icon': '🟢', 'label': '低优先级'}
        }
        config = priority_config.get(priority, priority_config['medium'])

        self.todos.append({
            'id': todo_id,
            'content': content,
            'priority': priority,
            'priority_icon': config['icon'],
            'priority_label': config['label'],
            'status': TodoStatus.PENDING.value,
            'status_icon': '⭕',
            'created_at': datetime.now().isoformat(),
            'completed_at': None
        })
        self._emit_update()

    def complete_todo(self, todo_id: int):
        """
        标记待办事项为已完成

        Args:
            todo_id: 待办事项ID
        """
        for todo in self.todos:
            if todo['id'] == todo_id:
                todo['status'] = TodoStatus.COMPLETED.value
                todo['status_icon'] = '✅'
                todo['completed_at'] = datetime.now().isoformat()
                break
        self._emit_update()

    def delete_todo(self, todo_id: int):
        """
        从卡片中删除待办事项

        Args:
            todo_id: 待办事项ID
        """
        self.todos = [t for t in self.todos if t['id'] != todo_id]
        self._emit_update()

    def update_todos(self, todos: List[Dict[str, Any]]):
        """
        批量更新待办事项列表

        Args:
            todos: 待办事项列表
        """
        self.todos = []
        for todo in todos:
            priority = todo.get('priority', 'medium')
            status = todo.get('status', 'pending')
            priority_config = {
                'high': {'icon': '🔴', 'label': '高优先级'},
                'medium': {'icon': '🟡', 'label': '中优先级'},
                'low': {'icon': '🟢', 'label': '低优先级'}
            }
            config = priority_config.get(priority, priority_config['medium'])

            self.todos.append({
                'id': todo.get('id'),
                'content': todo.get('content', ''),
                'priority': priority,
                'priority_icon': config['icon'],
                'priority_label': config['label'],
                'status': status,
                'status_icon': '✅' if status == 'completed' else '⭕',
                'created_at': todo.get('created_at', datetime.now().isoformat()),
                'completed_at': todo.get('completed_at')
            })
        self._emit_update()

    def complete_all(self):
        """标记所有待办事项为已完成"""
        for todo in self.todos:
            if todo['status'] != TodoStatus.COMPLETED.value:
                todo['status'] = TodoStatus.COMPLETED.value
                todo['status_icon'] = '✅'
                todo['completed_at'] = datetime.now().isoformat()
        self.is_completed = True
        self._emit_update()

    def _emit_update(self):
        """发送进度更新到 Web 端，并保存到会话"""
        if not self.socketio or not self.session_id:
            return

        pending_count = len([t for t in self.todos if t['status'] == TodoStatus.PENDING.value])
        completed_count = len([t for t in self.todos if t['status'] == TodoStatus.COMPLETED.value])
        total_count = len(self.todos)

        if total_count == 0:
            content = '📋 待办事项'
        elif pending_count == 0:
            content = f'✅ 全部完成 ({completed_count}/{total_count})'
        else:
            content = f'📋 待办事项 ({completed_count}/{total_count})'

        card_message = {
            'id': self.card_id,
            'session_id': self.session_id,
            'parent_message_id': self.parent_message_id,
            'role': 'system',
            'type': 'todo_card',
            'content': content,
            'todos': self.todos.copy(),
            'summary': {
                'total': total_count,
                'pending': pending_count,
                'completed': completed_count
            },
            'timestamp': datetime.now().isoformat(),
            'is_complete': self.is_completed
        }

        try:
            # 发送实时更新
            self.socketio.emit('new_message', card_message, room=self.session_id)
            self.socketio.sleep(0)

            # 保存到会话（持久化）
            sessions = self.manager.get_sessions() if self.manager else None
            if sessions and self.session_id in sessions:
                session = sessions[self.session_id]
                if self.parent_message_id:
                    for msg in session.get('messages', []):
                        if msg.get('id') == self.parent_message_id:
                            if 'todo_cards' not in msg:
                                msg['todo_cards'] = []
                            # 查找是否已存在该卡片
                            existing_idx = None
                            for i, card in enumerate(msg['todo_cards']):
                                if card.get('id') == self.card_id:
                                    existing_idx = i
                                    break
                            if existing_idx is not None:
                                msg['todo_cards'][existing_idx] = card_message
                            else:
                                msg['todo_cards'].append(card_message)
                            break
        except Exception:
            pass  # 静默处理错误

    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（用于保存到消息）"""
        pending_count = len([t for t in self.todos if t['status'] == TodoStatus.PENDING.value])
        completed_count = len([t for t in self.todos if t['status'] == TodoStatus.COMPLETED.value])
        total_count = len(self.todos)

        if total_count == 0:
            content = '📋 待办事项'
        elif pending_count == 0:
            content = f'✅ 全部完成 ({completed_count}/{total_count})'
        else:
            content = f'📋 待办事项 ({completed_count}/{total_count})'

        return {
            'id': self.card_id,
            'session_id': self.session_id,
            'parent_message_id': self.parent_message_id,
            'role': 'system',
            'type': 'todo_card',
            'content': content,
            'todos': self.todos.copy(),
            'summary': {
                'total': total_count,
                'pending': pending_count,
                'completed': completed_count
            },
            'timestamp': datetime.now().isoformat(),
            'is_complete': self.is_completed,
            'is_agent': self.is_agent,
        }


class TodoCardManager:
    """Todo 卡片管理器（单例模式）"""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cards: Dict[str, TodoCard] = {}
            cls._instance._socketio = None
            cls._instance._sessions = None
        return cls._instance

    def set_socketio(self, socketio):
        """设置 SocketIO 实例"""
        self._socketio = socketio

    def set_sessions(self, sessions):
        """设置会话字典（用于持久化）"""
        self._sessions = sessions

    def get_sessions(self):
        """获取会话字典"""
        return self._sessions

    def create_card(self, session_id: str, parent_message_id: str,
                    is_agent: bool = False) -> TodoCard:
        """
        创建新的 Todo 卡片

        Args:
            session_id: 会话ID
            parent_message_id: 父消息ID
            is_agent: 是否为 Agent 模式（向前端传递的状态标记）

        Returns:
            TodoCard 实例
        """
        card = TodoCard(
            session_id=session_id,
            parent_message_id=parent_message_id,
            socketio=self._socketio,
            manager=self,
            is_agent=is_agent,
        )
        self._cards[card.card_id] = card
        return card

    def get_card(self, card_id: str) -> Optional[TodoCard]:
        """获取 Todo 卡片"""
        return self._cards.get(card_id)

    def remove_card(self, card_id: str):
        """移除 Todo 卡片"""
        if card_id in self._cards:
            del self._cards[card_id]

    def get_or_create_card(self, session_id: str, parent_message_id: str) -> TodoCard:
        """
        获取或创建 Todo 卡片
        如果已存在该 parent_message_id 对应的卡片，则返回现有卡片

        Args:
            session_id: 会话ID
            parent_message_id: 父消息ID

        Returns:
            TodoCard 实例
        """
        # 查找是否已存在该 parent_message_id 对应的卡片
        for card in self._cards.values():
            if card.session_id == session_id and card.parent_message_id == parent_message_id:
                return card
        # 不存在则创建新卡片
        return self.create_card(session_id, parent_message_id)

    def update_from_tool_result(self, card: Optional[TodoCard], tool_name: str, result: Dict[str, Any]) -> None:
        """将 todo_* 工具的执行结果同步到进度卡片。

        由 WebCallbacks.on_tool_done 调用；此前本方法缺失导致
        卡片链路被上层 try/except 吞掉。
        """
        if card is None or not isinstance(result, dict) or not result.get("success"):
            return
        tool_name = str(tool_name or "")
        if not tool_name.startswith("todo_"):
            return
        try:
            if tool_name == "todo_add" and isinstance(result.get("todo"), dict):
                todo = result["todo"]
                card.add_todo(
                    int(todo.get("id")),
                    str(todo.get("content") or ""),
                    str(todo.get("priority") or "medium"),
                )
            elif tool_name == "todo_complete" and isinstance(result.get("todo"), dict):
                card.complete_todo(int(result["todo"]["id"]))
            elif tool_name == "todo_delete" and isinstance(result.get("deleted_todo"), dict):
                card.delete_todo(int(result["deleted_todo"]["id"]))
            elif isinstance(result.get("todos"), list):
                card.update_todos(result["todos"])
        except Exception as exc:
            _log.debug("[TodoCard] update_from_tool_result failed: %s", exc)


# 全局 Todo 卡片管理器实例
todo_card_manager = TodoCardManager()

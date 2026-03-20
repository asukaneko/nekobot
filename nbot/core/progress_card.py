"""
进度卡片管理模块
用于在 Web 端显示 AI 处理进度，包括工具调用、图片识别、文件处理等
"""

import uuid
import json
from datetime import datetime
from typing import Dict, List, Optional, Callable, Any
from enum import Enum


class StepType(Enum):
    """步骤类型"""
    START = "start"
    THINKING = "thinking"
    TOOL = "tool"
    TOOL_DONE = "tool_done"
    IMAGE = "image"
    IMAGE_DONE = "image_done"
    FILE = "file"
    FILE_DONE = "file_done"
    UPLOAD = "upload"
    UPLOAD_DONE = "upload_done"
    DONE = "done"


STEP_CONFIG = {
    StepType.START: {'type': 'thinking', 'icon': '🤔', 'status': 'active'},
    StepType.THINKING: {'type': 'thinking', 'icon': '💭', 'status': 'active'},
    StepType.TOOL: {'type': 'tool', 'icon': '🔧', 'status': 'running'},
    StepType.TOOL_DONE: {'type': 'tool', 'icon': '🔧', 'status': 'done'},
    StepType.IMAGE: {'type': 'image', 'icon': '🖼️', 'status': 'running'},
    StepType.IMAGE_DONE: {'type': 'image', 'icon': '🖼️', 'status': 'done'},
    StepType.FILE: {'type': 'file', 'icon': '📄', 'status': 'running'},
    StepType.FILE_DONE: {'type': 'file', 'icon': '📄', 'status': 'done'},
    StepType.UPLOAD: {'type': 'upload', 'icon': '📤', 'status': 'running'},
    StepType.UPLOAD_DONE: {'type': 'upload', 'icon': '📤', 'status': 'done'},
    StepType.DONE: {'type': 'done', 'icon': '✅', 'status': 'done'},
}


class ProgressCard:
    """进度卡片类"""
    
    def __init__(self, session_id: str, parent_message_id: str, 
                 socketio=None, max_iterations: int = 30, manager=None):
        """
        初始化进度卡片
        
        Args:
            session_id: 会话ID
            parent_message_id: 父消息ID（关联到用户消息）
            socketio: SocketIO 实例，用于发送实时更新
            max_iterations: 最大迭代次数（用于显示进度）
            manager: ProgressCardManager 实例，用于获取 sessions
        """
        self.card_id = str(uuid.uuid4())
        self.session_id = session_id
        self.parent_message_id = parent_message_id
        self.socketio = socketio
        self.max_iterations = max_iterations
        self.manager = manager  # 用于获取 sessions
        self.steps: List[Dict[str, Any]] = []
        self.current_iteration = 0
        self.is_completed = False
        
    def update(self, step_type: StepType, step_name: str, 
               step_detail: str = None, step_result: Any = None):
        """
        更新进度卡片
        
        Args:
            step_type: 步骤类型
            step_name: 步骤名称
            step_detail: 步骤详情
            step_result: 步骤结果（用于完成状态）
        """
        if self.is_completed:
            return
            
        config = STEP_CONFIG.get(step_type)
        if not config:
            return
        
        # 如果是完成状态，更新已有步骤
        if step_type in [StepType.TOOL_DONE, StepType.IMAGE_DONE, 
                        StepType.FILE_DONE, StepType.UPLOAD_DONE]:
            base_type = config['type']
            # 找到最后一个匹配的 running 步骤并标记为完成
            for step in reversed(self.steps):
                if step['type'] == base_type and step['status'] == 'running':
                    step['status'] = 'done' if step_result is not False else 'error'
                    # 更新步骤名称为完成状态的名称
                    if step_name:
                        step['name'] = step_name
                    if step_result and isinstance(step_result, str):
                        step['result'] = step_result[:100]
                    break
        elif step_type == StepType.DONE:
            # 添加完成步骤前，将所有运行中的步骤标记为完成
            for step in self.steps:
                if step['status'] in ['active', 'running']:
                    step['status'] = 'done'
            
            # 添加完成步骤
            self.steps.append({
                'type': config['type'],
                'icon': config['icon'],
                'name': step_name,
                'status': config['status'],
                'detail': step_detail
            })
            self.is_completed = True
        else:
            # 添加新步骤前，将之前的运行中步骤（除了同类型的）标记为完成
            if step_type not in [StepType.THINKING, StepType.UPLOAD]:
                for step in reversed(self.steps):
                    if step['status'] in ['active', 'running']:
                        # 如果是 file 或 image 步骤，保留它们，直到明确的 _done 调用
                        if step['type'] in ['file', 'image']:
                            continue
                        step['status'] = 'done'
                        break
            
            # 添加新步骤
            self.steps.append({
                'type': config['type'],
                'icon': config['icon'],
                'name': step_name,
                'status': config['status'],
                'detail': step_detail
            })
        
        # 发送更新
        self._emit_update()
    
    def _emit_update(self):
        """发送进度更新到 Web 端，并保存到会话"""
        if not self.socketio or not self.session_id:
            return
            
        if self.is_completed:
            content = '✅ 处理完成'
        else:
            content = f'🔄 AI 正在处理... ({self.current_iteration + 1}/{self.max_iterations})'
        
        card_message = {
            'id': self.card_id,
            'session_id': self.session_id,
            'parent_message_id': self.parent_message_id,
            'role': 'system',
            'type': 'thinking_card',
            'content': content,
            'steps': self.steps.copy(),
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
                            if 'thinking_cards' not in msg:
                                msg['thinking_cards'] = []
                            # 查找是否已存在该卡片
                            existing_idx = None
                            for i, card in enumerate(msg['thinking_cards']):
                                if card.get('id') == self.card_id:
                                    existing_idx = i
                                    break
                            if existing_idx is not None:
                                msg['thinking_cards'][existing_idx] = card_message
                            else:
                                msg['thinking_cards'].append(card_message)
                            break
        except Exception:
            pass  # 静默处理错误
    
    def complete(self, final_message: str = '✅ 处理完成'):
        """标记进度为完成"""
        self.update(StepType.DONE, final_message)
    
    def increment_iteration(self):
        """增加迭代计数"""
        self.current_iteration += 1
        self._emit_update()
    
    def to_dict(self) -> Dict[str, Any]:
        """转换为字典格式（用于保存到消息）"""
        return {
            'id': self.card_id,
            'session_id': self.session_id,
            'parent_message_id': self.parent_message_id,
            'role': 'system',
            'type': 'thinking_card',
            'content': '✅ 处理完成' if self.is_completed else f'🔄 AI 正在处理... ({self.current_iteration + 1}/{self.max_iterations})',
            'steps': self.steps.copy(),
            'timestamp': datetime.now().isoformat(),
            'is_complete': self.is_completed
        }


class ProgressCardManager:
    """进度卡片管理器（单例模式）"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._cards: Dict[str, ProgressCard] = {}
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
                   max_iterations: int = 30) -> ProgressCard:
        """
        创建新的进度卡片
        
        Args:
            session_id: 会话ID
            parent_message_id: 父消息ID
            max_iterations: 最大迭代次数
            
        Returns:
            ProgressCard 实例
        """
        card = ProgressCard(
            session_id=session_id,
            parent_message_id=parent_message_id,
            socketio=self._socketio,
            max_iterations=max_iterations,
            manager=self
        )
        self._cards[card.card_id] = card
        return card
    
    def get_card(self, card_id: str) -> Optional[ProgressCard]:
        """获取进度卡片"""
        return self._cards.get(card_id)
    
    def remove_card(self, card_id: str):
        """移除进度卡片"""
        if card_id in self._cards:
            del self._cards[card_id]
    
    def complete_all(self, session_id: str):
        """完成会话中的所有进度卡片"""
        for card in list(self._cards.values()):
            if card.session_id == session_id and not card.is_completed:
                card.complete()


# 全局进度卡片管理器实例
progress_card_manager = ProgressCardManager()

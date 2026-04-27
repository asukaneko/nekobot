"""
统一消息管理模块

提供统一的消息格式和管理功能，支持：
- 统一的消息数据结构
- QQ 端和 Web 端消息统一管理
- 消息历史记录和检索
- 统一的存储格式
"""

import os
import json
import uuid
from typing import Optional, List, Dict, Any
from datetime import datetime
from dataclasses import dataclass, field, asdict
from nbot.web.persistence import is_web_visible_session
from nbot.web.sessions_db import load_sessions, save_sessions


@dataclass
class Message:
    """统一消息格式"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    role: str = "user"  # user, assistant, system
    content: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now().isoformat())
    sender: str = ""  # 发送者标识
    source: str = "unknown"  # qq_private, qq_group, web
    session_id: str = ""  # 会话 ID
    attachments: List[Dict] = field(default_factory=list)  # 附件列表
    metadata: Dict = field(default_factory=dict)  # 元数据
    
    def to_dict(self) -> Dict:
        """转换为字典"""
        return asdict(self)
    
    @classmethod
    def from_dict(cls, data: Dict) -> 'Message':
        """从字典创建"""
        return cls(
            id=data.get('id', str(uuid.uuid4())),
            role=data.get('role', 'user'),
            content=data.get('content', ''),
            timestamp=data.get('timestamp', datetime.now().isoformat()),
            sender=data.get('sender', ''),
            source=data.get('source', 'unknown'),
            session_id=data.get('session_id', ''),
            attachments=data.get('attachments', []),
            metadata=data.get('metadata', {})
        )
    
    @classmethod
    def create_user_message(cls, content: str, sender: str = "", source: str = "unknown",
                          session_id: str = "", attachments: List[Dict] = None, metadata: Dict = None) -> 'Message':
        """创建用户消息"""
        return cls(
            role="user",
            content=content,
            sender=sender,
            source=source,
            session_id=session_id,
            attachments=attachments or [],
            metadata=metadata or {}
        )
    
    @classmethod
    def create_assistant_message(cls, content: str, sender: str = "assistant",
                                source: str = "unknown", session_id: str = "", metadata: Dict = None) -> 'Message':
        """创建助手消息"""
        return cls(
            role="assistant",
            content=content,
            sender=sender,
            source=source,
            session_id=session_id,
            metadata=metadata or {}
        )
    
    @classmethod
    def create_system_message(cls, content: str) -> 'Message':
        """创建系统消息"""
        return cls(
            role="system",
            content=content
        )


class MessageManager:
    """消息管理器（单例模式）"""
    
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return
        self._initialized = True
        
        self.base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
        self.data_dir = os.path.join(self.base_dir, 'data')
        
        # 消息存储目录
        self.qq_private_dir = os.path.join(self.data_dir, 'qq', 'private')
        self.qq_group_dir = os.path.join(self.data_dir, 'qq', 'group')
        self.web_dir = os.path.join(self.data_dir, 'web')
        
        # 确保目录存在
        os.makedirs(self.qq_private_dir, exist_ok=True)
        os.makedirs(self.qq_group_dir, exist_ok=True)
        os.makedirs(self.web_dir, exist_ok=True)
        
        # 内存中的消息缓存
        self._qq_private_cache: Dict[str, List[Message]] = {}
        self._qq_group_cache: Dict[str, List[Message]] = {}
        self._web_cache: Dict[str, List[Message]] = {}
        
        self._load_all_messages()
    
    def _load_all_messages(self):
        """加载所有消息"""
        # 加载 QQ 私聊消息
        for filename in os.listdir(self.qq_private_dir):
            if filename.endswith('.json'):
                user_id = filename.replace('.json', '')
                filepath = os.path.join(self.qq_private_dir, filename)
                self._qq_private_cache[user_id] = self._load_messages_from_file(filepath)
        
        # 加载 QQ 群聊消息
        for filename in os.listdir(self.qq_group_dir):
            if filename.endswith('.json'):
                group_id = filename.replace('.json', '')
                filepath = os.path.join(self.qq_group_dir, filename)
                self._qq_group_cache[group_id] = self._load_messages_from_file(filepath)
        
        # 加载 Web 会话消息
        sessions_data = load_sessions(self.web_dir)
        try:
            for session_id, session in sessions_data.items():
                if not is_web_visible_session(session_id, session):
                    continue
                messages = session.get('messages', [])
                self._web_cache[session_id] = [Message.from_dict(m) for m in messages]
        except Exception as e:
                print(f"加载 Web 消息失败: {e}")
    
    def _load_messages_from_file(self, filepath: str) -> List[Message]:
        """从文件加载消息"""
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return [Message.from_dict(m) for m in data]
                elif isinstance(data, dict) and 'messages' in data:
                    return [Message.from_dict(m) for m in data['messages']]
        except Exception as e:
            print(f"加载消息失败 {filepath}: {e}")
        return []
    
    def _save_messages_to_file(self, filepath: str, messages: List[Message]):
        """保存消息到文件"""
        try:
            with open(filepath, 'w', encoding='utf-8') as f:
                json.dump([m.to_dict() for m in messages], f, ensure_ascii=False, indent=2)
        except Exception as e:
            print(f"保存消息失败 {filepath}: {e}")
    
    def _save_web_sessions(self):
        """保存 Web 会话"""
        try:
            existing_sessions = load_sessions(self.web_dir)
            sessions_data = {
                session_id: dict(session)
                for session_id, session in existing_sessions.items()
                if is_web_visible_session(session_id, session)
            }
            for session_id, messages in self._web_cache.items():
                existing = sessions_data.get(session_id, {})
                session = {
                    **existing,
                    'id': session_id,
                    'type': existing.get('type') or 'web',
                    'messages': [m.to_dict() for m in messages]
                }
                if is_web_visible_session(session_id, session):
                    sessions_data[session_id] = session
            save_sessions(self.web_dir, sessions_data)
        except Exception as e:
            print(f"保存 Web 会话失败: {e}")
    
    # ========== QQ 私聊消息 ==========
    
    def add_qq_private_message(self, user_id: str, message: Message) -> Message:
        """添加 QQ 私聊消息"""
        if user_id not in self._qq_private_cache:
            self._qq_private_cache[user_id] = []
        
        message.source = "qq_private"
        message.sender = user_id
        self._qq_private_cache[user_id].append(message)
        
        # 保存到文件
        filepath = os.path.join(self.qq_private_dir, f"{user_id}.json")
        self._save_messages_to_file(filepath, self._qq_private_cache[user_id])
        
        return message
    
    def get_qq_private_messages(self, user_id: str, limit: int = None) -> List[Message]:
        """获取 QQ 私聊消息"""
        messages = self._qq_private_cache.get(user_id, [])
        if limit:
            return messages[-limit:]
        return messages
    
    def clear_qq_private_messages(self, user_id: str) -> bool:
        """清空 QQ 私聊消息"""
        if user_id in self._qq_private_cache:
            self._qq_private_cache[user_id] = []
            filepath = os.path.join(self.qq_private_dir, f"{user_id}.json")
            self._save_messages_to_file(filepath, [])
            return True
        return False
    
    # ========== QQ 群聊消息 ==========
    
    def add_qq_group_message(self, group_id: str, message: Message) -> Message:
        """添加 QQ 群聊消息"""
        if group_id not in self._qq_group_cache:
            self._qq_group_cache[group_id] = []
        
        message.source = "qq_group"
        self._qq_group_cache[group_id].append(message)
        
        # 保存到文件
        filepath = os.path.join(self.qq_group_dir, f"{group_id}.json")
        self._save_messages_to_file(filepath, self._qq_group_cache[group_id])
        
        return message
    
    def get_qq_group_messages(self, group_id: str, limit: int = None) -> List[Message]:
        """获取 QQ 群聊消息"""
        messages = self._qq_group_cache.get(group_id, [])
        if limit:
            return messages[-limit:]
        return messages
    
    def clear_qq_group_messages(self, group_id: str) -> bool:
        """清空 QQ 群聊消息"""
        if group_id in self._qq_group_cache:
            self._qq_group_cache[group_id] = []
            filepath = os.path.join(self.qq_group_dir, f"{group_id}.json")
            self._save_messages_to_file(filepath, [])
            return True
        return False
    
    # ========== Web 会话消息 ==========
    
    def add_web_message(self, session_id: str, message: Message) -> Message:
        """添加 Web 会话消息"""
        if session_id not in self._web_cache:
            self._web_cache[session_id] = []
        
        message.source = "web"
        self._web_cache[session_id].append(message)
        
        # 保存到文件
        self._save_web_sessions()
        
        return message
    
    def get_web_messages(self, session_id: str, limit: int = None) -> List[Message]:
        """获取 Web 会话消息"""
        messages = self._web_cache.get(session_id, [])
        if limit:
            return messages[-limit:]
        return messages
    
    def clear_web_messages(self, session_id: str) -> bool:
        """清空 Web 会话消息"""
        if session_id in self._web_cache:
            self._web_cache[session_id] = []
            self._save_web_sessions()
            return True
        return False
    
    # ========== 兼容旧接口 ==========
    
    def add_message(self, source: str, target_id: str, role: str, content: str,
                  **kwargs) -> Dict:
        """兼容旧接口添加消息"""
        message = Message(
            role=role,
            content=content,
            sender=kwargs.get('sender', target_id),
            source=source,
            session_id=target_id,
            metadata=kwargs
        )
        
        if source == "qq_private":
            self.add_qq_private_message(target_id, message)
        elif source == "qq_group":
            self.add_qq_group_message(target_id, message)
        elif source == "web":
            self.add_web_message(target_id, message)
        
        return message.to_dict()
    
    def get_messages(self, source: str, target_id: str, limit: int = None) -> List[Dict]:
        """兼容旧接口获取消息"""
        if source == "qq_private":
            messages = self.get_qq_private_messages(target_id, limit)
        elif source == "qq_group":
            messages = self.get_qq_group_messages(target_id, limit)
        elif source == "web":
            messages = self.get_web_messages(target_id, limit)
        else:
            return []
        
        return [m.to_dict() for m in messages]
    
    # ========== 迁移工具 ==========
    
    def migrate_from_chat_service(self, user_messages: Dict, group_messages: Dict):
        """从旧的 chat_service 迁移数据"""
        # 迁移私聊消息
        for user_id, messages in user_messages.items():
            for msg in messages:
                if msg.get('role') == 'system':
                    continue
                message = Message.from_dict(msg)
                message.source = "qq_private"
                message.sender = user_id
                self.add_qq_private_message(user_id, message)
        
        # 迁移群聊消息
        for group_id, messages in group_messages.items():
            for msg in messages:
                if msg.get('role') == 'system':
                    continue
                message = Message.from_dict(msg)
                message.source = "qq_group"
                self.add_qq_group_message(group_id, message)
        
        print(f"迁移完成: {len(user_messages)} 私聊, {len(group_messages)} 群聊")


# 创建全局实例
message_manager = MessageManager()


# 便捷函数
def add_message(source: str, target_id: str, role: str, content: str, **kwargs) -> Dict:
    """添加消息"""
    return message_manager.add_message(source, target_id, role, content, **kwargs)


def get_messages(source: str, target_id: str, limit: int = None) -> List[Dict]:
    """获取消息"""
    return message_manager.get_messages(source, target_id, limit)


def create_message(role: str, content: str, **kwargs) -> Message:
    """创建消息"""
    return Message(role=role, content=content, **kwargs)


def migrate_messages(user_messages: Dict, group_messages: Dict):
    """迁移消息"""
    message_manager.migrate_from_chat_service(user_messages, group_messages)

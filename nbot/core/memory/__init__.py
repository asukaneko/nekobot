"""
Memory 长期记忆系统
支持用户和群组的长期记忆存储与检索
"""
import os
import json
import logging
from typing import Dict, Any, List, Optional
from dataclasses import dataclass, field
from datetime import datetime
from enum import Enum

_log = logging.getLogger(__name__)


class MemoryType(Enum):
    """记忆类型"""
    USER = "user"
    GROUP = "group"


class MemoryImportance(Enum):
    """记忆重要性等级"""
    LOW = 1      # 低：一般对话
    MEDIUM = 2   # 中：重要事件
    HIGH = 3     # 高：核心信息


@dataclass
class MemoryEntry:
    """记忆条目"""
    id: str
    content: str
    memory_type: str
    importance: int = MemoryImportance.LOW.value
    tags: List[str] = field(default_factory=list)
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "content": self.content,
            "memory_type": self.memory_type,
            "importance": self.importance,
            "tags": self.tags,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "metadata": self.metadata
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "MemoryEntry":
        return cls(**data)


class MemoryStore:
    """记忆存储"""

    def __init__(self, base_dir: str = "saved_message/memory"):
        self.base_dir = base_dir
        self.user_dir = os.path.join(base_dir, "user")
        self.group_dir = os.path.join(base_dir, "group")
        os.makedirs(self.user_dir, exist_ok=True)
        os.makedirs(self.group_dir, exist_ok=True)

    def _get_file_path(self, memory_type: MemoryType, entity_id: str) -> str:
        """获取记忆文件路径"""
        if memory_type == MemoryType.USER:
            return os.path.join(self.user_dir, f"{entity_id}.json")
        else:
            return os.path.join(self.group_dir, f"{entity_id}.json")

    def _load_memories(self, entity_id: str, memory_type: MemoryType) -> List[MemoryEntry]:
        """加载记忆"""
        file_path = self._get_file_path(memory_type, entity_id)
        if not os.path.exists(file_path):
            return []

        try:
            with open(file_path, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [MemoryEntry.from_dict(item) for item in data]
        except Exception as e:
            _log.error(f"Failed to load memories: {e}")
            return []

    def _save_memories(self, entity_id: str, memory_type: MemoryType, memories: List[MemoryEntry]):
        """保存记忆"""
        file_path = self._get_file_path(memory_type, entity_id)
        try:
            with open(file_path, "w", encoding="utf-8") as f:
                json.dump(
                    [m.to_dict() for m in memories],
                    f,
                    ensure_ascii=False,
                    indent=2
                )
        except Exception as e:
            _log.error(f"Failed to save memories: {e}")

    def add_memory(
        self,
        entity_id: str,
        content: str,
        memory_type: MemoryType,
        importance: int = MemoryImportance.LOW.value,
        tags: List[str] = None,
        metadata: Dict[str, Any] = None
    ) -> MemoryEntry:
        """添加记忆"""
        memories = self._load_memories(entity_id, memory_type)

        import uuid
        new_memory = MemoryEntry(
            id=str(uuid.uuid4()),
            content=content,
            memory_type=memory_type.value,
            importance=importance,
            tags=tags or [],
            metadata=metadata or {}
        )

        memories.append(new_memory)
        self._save_memories(entity_id, memory_type, memories)

        _log.info(f"Added memory for {memory_type.value}: {entity_id}")
        return new_memory

    def get_memories(
        self,
        entity_id: str,
        memory_type: MemoryType,
        importance: int = None,
        tags: List[str] = None,
        limit: int = 10
    ) -> List[MemoryEntry]:
        """获取记忆"""
        memories = self._load_memories(entity_id, memory_type)

        if importance:
            memories = [m for m in memories if m.importance >= importance]

        if tags:
            memories = [m for m in memories if any(tag in m.tags for tag in tags)]

        memories.sort(key=lambda x: (x.importance, x.created_at), reverse=True)
        return memories[:limit]

    def search_memories(
        self,
        entity_id: str,
        memory_type: MemoryType,
        query: str,
        limit: int = 5
    ) -> List[MemoryEntry]:
        """搜索记忆"""
        memories = self._load_memories(entity_id, memory_type)

        query_lower = query.lower()
        results = [
            m for m in memories
            if query_lower in m.content.lower() or any(query_lower in tag.lower() for tag in m.tags)
        ]

        results.sort(key=lambda x: x.importance, reverse=True)
        return results[:limit]

    def update_memory(
        self,
        entity_id: str,
        memory_type: MemoryType,
        memory_id: str,
        content: str = None,
        importance: int = None,
        tags: List[str] = None
    ) -> bool:
        """更新记忆"""
        memories = self._load_memories(entity_id, memory_type)

        for memory in memories:
            if memory.id == memory_id:
                if content:
                    memory.content = content
                if importance:
                    memory.importance = importance
                if tags:
                    memory.tags = tags
                memory.updated_at = datetime.now().isoformat()

                self._save_memories(entity_id, memory_type, memories)
                return True

        return False

    def delete_memory(
        self,
        entity_id: str,
        memory_type: MemoryType,
        memory_id: str
    ) -> bool:
        """删除记忆"""
        memories = self._load_memories(entity_id, memory_type)
        original_count = len(memories)
        memories = [m for m in memories if m.id != memory_id]

        if len(memories) < original_count:
            self._save_memories(entity_id, memory_type, memories)
            return True

        return False

    def get_memory_summary(
        self,
        entity_id: str,
        memory_type: MemoryType,
        max_length: int = 500
    ) -> str:
        """获取记忆摘要"""
        memories = self.get_memories(entity_id, memory_type, limit=20)

        if not memories:
            return ""

        summary = "【记忆摘要】\n"
        for memory in memories:
            importance_label = {
                1: "○",
                2: "●",
                3: "★"
            }.get(memory.importance, "○")

            summary += f"{importance_label} {memory.content[:100]}\n"

            if len(summary) > max_length:
                break

        return summary


class MemoryManager:
    """记忆管理器"""

    def __init__(self):
        self.store = MemoryStore()
        self._importance_keywords = {
            MemoryImportance.HIGH.value: [
                "喜欢", "讨厌", "爱好", "生日", "名字", "重要",
                "记住", "不要忘记", "最喜欢", "最讨厌"
            ],
            MemoryImportance.MEDIUM.value: [
                "之前", "上次", "告诉过", "说过", "聊过"
            ]
        }

    def _detect_importance(self, content: str) -> int:
        """自动检测重要性"""
        content_lower = content.lower()

        for importance, keywords in self._importance_keywords.items():
            for keyword in keywords:
                if keyword in content_lower:
                    return importance

        return MemoryImportance.LOW.value

    def _extract_tags(self, content: str) -> List[str]:
        """自动提取标签"""
        tags = []

        tag_patterns = {
            "漫画": ["漫画", "本子", "看漫画"],
            "小说": ["小说", "轻小说", "看书"],
            "音乐": ["音乐", "听歌", "歌曲"],
            "游戏": ["游戏", "玩游戏", "打游戏"],
            "技术": ["代码", "编程", "技术"],
            "日常": ["吃饭", "睡觉", "上班", "学习"]
        }

        for tag, keywords in tag_patterns.items():
            if any(kw in content for kw in keywords):
                tags.append(tag)

        return tags

    def remember(
        self,
        content: str,
        user_id: str = None,
        group_id: str = None,
        auto_detect: bool = True
    ) -> Optional[MemoryEntry]:
        """记录重要信息到长期记忆"""
        if not user_id and not group_id:
            return None

        entity_id = user_id or group_id
        memory_type = MemoryType.USER if user_id else MemoryType.GROUP

        importance = self._detect_importance(content) if auto_detect else MemoryImportance.LOW.value
        tags = self._extract_tags(content) if auto_detect else []

        return self.store.add_memory(
            entity_id=entity_id,
            content=content,
            memory_type=memory_type,
            importance=importance,
            tags=tags,
            metadata={"auto_detected": auto_detect}
        )

    def recall(
        self,
        user_id: str = None,
        group_id: str = None,
        query: str = None,
        limit: int = 5
    ) -> str:
        """检索记忆"""
        if not user_id and not group_id:
            return ""

        entity_id = user_id or group_id
        memory_type = MemoryType.USER if user_id else MemoryType.GROUP

        if query:
            memories = self.store.search_memories(entity_id, memory_type, query, limit)
        else:
            memories = self.store.get_memories(entity_id, memory_type, limit=limit)

        if not memories:
            return ""

        result = "【相关记忆】\n"
        for memory in memories:
            result += f"- {memory.content[:80]}\n"

        return result

    def get_user_profile_summary(self, user_id: str) -> str:
        """获取用户画像摘要"""
        return self.store.get_memory_summary(user_id, MemoryType.USER)


memory_manager: Optional[MemoryManager] = None


def get_memory_manager() -> MemoryManager:
    """获取记忆管理器单例"""
    global memory_manager
    if memory_manager is None:
        memory_manager = MemoryManager()
    return memory_manager

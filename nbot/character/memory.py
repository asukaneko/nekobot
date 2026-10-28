"""
角色记忆服务

第一版使用 PromptManagerMemoryAdapter 包装旧 auto_memory 系统，
保持与现有 data/memories.json 的兼容。
后续逐步迁移到独立的角色记忆存储。
"""

import logging
import re
import uuid
from datetime import datetime
from typing import Any, Dict, List, Optional

from nbot.character.models import CharacterMemory, CharacterTurnContext

_log = logging.getLogger(__name__)

_TERM_RE = re.compile(r"[a-zA-Z0-9_]{2,}|[\u3400-\u9fff\uf900-\ufaff]{2,}")


def _extract_query_terms(query: str) -> list[str]:
    """从查询中提取匹配词：拉丁词元 + CJK 二元组（滑动窗口）。"""
    terms: list[str] = []
    for token in _TERM_RE.findall(str(query or "")):
        if token[0].isascii():
            terms.append(token.lower())
        elif len(token) == 2:
            terms.append(token)
        else:
            terms.extend(token[i:i + 2] for i in range(len(token) - 1))
    return terms


def _coerce_importance(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


class PromptManagerMemoryAdapter:
    """旧 PromptManager 记忆系统的适配器

    包装 nbot.core.prompt 中的 PromptManager，
    提供按 character_id + target_id 检索记忆的能力。
    """

    def __init__(self):
        self._prompt_manager = None

    @property
    def prompt_manager(self):
        if self._prompt_manager is None:
            from nbot.core.prompt import prompt_manager
            self._prompt_manager = prompt_manager
        return self._prompt_manager

    def search(
        self,
        character_id: str = "",
        target_id: str = "",
        query: str = "",
        limit: int = 8,
    ) -> List[CharacterMemory]:
        """检索相关记忆

        Args:
            character_id: 角色ID
            target_id: 目标用户ID
            query: 查询文本
            limit: 最大返回数量

        Returns:
            CharacterMemory 列表
        """
        try:
            raw_memories = self.prompt_manager.get_memories(
                target_id=target_id or None,
                character_name=character_id or None,
            )

            # query 非空时按相关性排序（命中词元数），否则保持原序
            terms = _extract_query_terms(str(query or ""))
            if terms:
                def _score(mem: dict) -> int:
                    text = " ".join(
                        str(mem.get(k) or "") for k in ("title", "summary", "content")
                    ).lower()
                    return sum(1 for term in terms if term in text)

                raw_memories = sorted(
                    raw_memories,
                    key=lambda mem: (_score(mem), _coerce_importance(mem.get("importance", 0))),
                    reverse=True,
                )

            memories = []
            for mem in raw_memories[:limit]:
                memories.append(CharacterMemory(
                    id=mem.get("id", str(uuid.uuid4())),
                    character_id=character_id,
                    target_id=target_id,
                    type=mem.get("type", "long"),
                    title=mem.get("title", ""),
                    summary=mem.get("summary", ""),
                    content=mem.get("content", ""),
                    importance=5,
                    created_at=mem.get("created_at", ""),
                ))

            return memories
        except Exception as exc:
            _log.warning("[MemoryAdapter] 检索记忆失败: %s", exc)
            return []

    def save(
        self,
        character_id: str,
        target_id: str,
        title: str,
        content: str,
        summary: str = "",
        mem_type: str = "long",
    ) -> bool:
        """保存记忆

        Args:
            character_id: 角色ID
            target_id: 目标用户ID
            title: 记忆标题
            content: 记忆内容
            summary: 记忆摘要
            mem_type: 记忆类型

        Returns:
            是否保存成功
        """
        try:
            return self.prompt_manager.add_memory(
                title=title,
                content=content,
                target_id=target_id,
                summary=summary,
                mem_type=mem_type,
                character_name=character_id,
            )
        except Exception as exc:
            _log.warning("[MemoryAdapter] 保存记忆失败: %s", exc)
            return False

    def delete(self, memory_id: str) -> bool:
        """删除记忆"""
        try:
            return self.prompt_manager.delete_memory(memory_id)
        except Exception as exc:
            _log.warning("[MemoryAdapter] 删除记忆失败: %s", exc)
            return False

    def extract_and_save_if_needed(
        self,
        chat_request,
        result,
        turn_context: CharacterTurnContext,
    ) -> None:
        """记忆抽取（委托给旧 auto_memory 系统）

        第一版不在此处实现独立抽取，继续使用 Pipeline 中的 auto_memory 阶段。
        后续版本在此实现按角色+用户的记忆抽取。
        """
        pass

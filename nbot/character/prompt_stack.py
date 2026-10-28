"""
动态提示词栈

允许任意模块在任意阶段注册提示词注入项，
最终统一合成为本轮顶部 system/developer prompt。

核心原则：
- 动态注入只在本轮请求生效，不写入历史消息
- 按优先级排序合成
- 可调试、可观察
"""

import logging
import re
from dataclasses import dataclass
from typing import Any, Literal

_log = logging.getLogger(__name__)

_DYNAMIC_SECTION_KEYS = (
    "character.runtime_state",
    "character.relationship",
    "character.reaction_plan",
    "character.memories",
    "character.memories_legacy",
    "character.image_capability",
    "character.personality_evolution",
    "output.bubble_split",
    "output.inner_monologue",
    "real_time.continuity",
    "character.circadian",
    "character.timeline",
    "character.life_sim",
    "plot.real_time_sync",
    "knowledge.rag",
    "world_book",
    "world_book.before_char",
    "world_book.after_char",
    "memory_fs.context",
    "memory_fs_context",
)


_LEGACY_RUNTIME_SECTION_MARKERS = (
    "【角色当前状态】",
    "【与用户的关系】",
    "与用户的初始关系",
    "銆愯鑹插綋鍓嶇姸鎬併€",
    "銆愪笌鐢ㄦ埛鐨勫叧绯",
    "涓庣敤鎴风殑鍒濆鍏崇郴",
)


def _strip_legacy_inline_sections(prompt: str) -> str:
    lines = prompt.splitlines()
    kept = []
    skipping = False

    for line in lines:
        stripped = line.strip()
        if any(marker in stripped for marker in _LEGACY_RUNTIME_SECTION_MARKERS):
            skipping = True
            continue
        if skipping and (
            stripped.startswith("## ")
            or stripped.startswith("【")
            or stripped.startswith("銆?")
        ):
            skipping = False
        if not skipping:
            kept.append(line)

    return "\n".join(kept)


def strip_dynamic_prompt_sections(prompt: str) -> str:
    """Remove runtime PromptStack sections from a persisted base prompt."""
    if not prompt:
        return ""

    cleaned = prompt
    # 兼容两种 section 标题风格：
    # - ## {key}：PromptStack.render() 默认输出
    # - [{key}]：MemoryFS.build_prompt_context() 注入的 timeline / life_sim
    # 两种风格都需要在 strip 时被正确移除，避免 base_prompt 中残留过时 section
    section_break = r"(?=\n{2,}(?:## |\[)|\Z)"
    for key in _DYNAMIC_SECTION_KEYS:
        escaped_key = re.escape(key)
        # ## {key} 风格
        cleaned = re.sub(
            rf"(?:\n{{2,}}|\A)## {escaped_key}\n.*?{section_break}",
            "\n\n",
            cleaned,
            flags=re.DOTALL,
        )
        # [{key}] 风格
        cleaned = re.sub(
            rf"(?:\n{{2,}}|\A)\[{escaped_key}\]\n.*?{section_break}",
            "\n\n",
            cleaned,
            flags=re.DOTALL,
        )

    # Older compiled profile prompts embedded initial runtime values directly.
    cleaned = re.sub(
        r"(?:\n{2,}|\A)【角色当前状态】\n.*?(?=\n{2,}(?:## |【)|\Z)",
        "\n\n",
        cleaned,
        flags=re.DOTALL,
    )
    cleaned = _strip_legacy_inline_sections(cleaned)
    return re.sub(r"\n{3,}", "\n\n", cleaned).strip()

PromptRole = Literal["system", "developer"]
PromptScope = Literal["global", "session", "turn"]


@dataclass
class PromptInjection:
    """单条提示词注入项"""

    key: str
    content: str
    priority: int = 100
    role: PromptRole = "system"
    scope: PromptScope = "turn"
    enabled: bool = True
    # before=渲染在 base_prompt 之前（如世界书 before_char），after=之后
    position: str = "after"

    def to_dict(self) -> dict[str, Any]:
        return {
            "key": self.key,
            "content": self.content[:200] + "..." if len(self.content) > 200 else self.content,
            "priority": self.priority,
            "role": self.role,
            "scope": self.scope,
            "enabled": self.enabled,
            "position": self.position,
        }


class PromptStack:
    """动态提示词栈，收集并合成所有注入项"""

    # 推荐优先级常量
    PRIORITY_SAFETY = 10
    PRIORITY_BUBBLE_SPLIT = 15  # 气泡分隔提示词（高优先级，紧随安全规则）
    PRIORITY_BEHAVIOR = 20
    PRIORITY_CHARACTER_PROFILE = 30
    PRIORITY_CHARACTER_STATE = 40
    PRIORITY_CHARACTER_RELATIONSHIP = 50
    PRIORITY_CHARACTER_MEMORIES = 60
    PRIORITY_KNOWLEDGE_RAG = 70
    PRIORITY_TOOL_INSTRUCTIONS = 80
    PRIORITY_REACTION_PLAN = 55
    PRIORITY_WORLD_BOOK = 65

    def __init__(self):
        self.items: list[PromptInjection] = []

    def add(
        self,
        key: str,
        content: str,
        priority: int = 100,
        role: str = "system",
        scope: str = "turn",
        position: str = "after",
    ) -> None:
        """注册一条提示词注入项

        Args:
            key: 注入项标识，用于调试和去重
            content: 注入内容
            priority: 优先级，数值越小越靠前
            role: 消息角色 (system / developer)
            scope: 作用域 (global / session / turn)
            position: 相对 base_prompt 的渲染位置 (after / before)
        """
        if not content or not content.strip():
            return

        # 同 key 则替换
        for i, item in enumerate(self.items):
            if item.key == key:
                self.items[i] = PromptInjection(
                    key=key,
                    content=content.strip(),
                    priority=priority,
                    role=role,
                    scope=scope,
                    position=position,
                )
                return

        self.items.append(PromptInjection(
            key=key,
            content=content.strip(),
            priority=priority,
            role=role,
            scope=scope,
            position=position,
        ))

    def remove(self, key: str) -> bool:
        """移除指定 key 的注入项"""
        before = len(self.items)
        self.items = [item for item in self.items if item.key != key]
        return len(self.items) < before

    def get(self, key: str) -> PromptInjection | None:
        """获取指定 key 的注入项"""
        for item in self.items:
            if item.key == key:
                return item
        return None

    def render(self, base_prompt: str = "") -> str:
        """合成所有注入项为最终的 system prompt

        Args:
            base_prompt: 基础系统提示词（通常来自角色卡编译结果）

        Returns:
            合成后的完整系统提示词
        """
        base_prompt = strip_dynamic_prompt_sections(base_prompt)

        before_items = sorted(
            (i for i in self.items if i.enabled and i.content.strip() and i.position == "before"),
            key=lambda x: x.priority,
        )
        after_items = sorted(
            (i for i in self.items if i.enabled and i.content.strip() and i.position != "before"),
            key=lambda x: x.priority,
        )

        parts = []
        for item in before_items:
            parts.append(f"## {item.key}\n{item.content.strip()}")
        if base_prompt and base_prompt.strip():
            parts.append(base_prompt.strip())
        for item in after_items:
            parts.append(f"## {item.key}\n{item.content.strip()}")

        return "\n\n".join(parts).strip()

    def render_debug(self) -> list[dict[str, Any]]:
        """返回调试信息，展示本轮所有注入项"""
        return [
            item.to_dict()
            for item in sorted(self.items, key=lambda x: x.priority)
        ]

    def clear_scope(self, scope: str) -> int:
        """清除指定作用域的所有注入项，返回清除数量"""
        before = len(self.items)
        self.items = [item for item in self.items if item.scope != scope]
        return before - len(self.items)

    @property
    def keys(self) -> list[str]:
        """返回所有已注册的 key 列表"""
        return [item.key for item in self.items]


def split_system_prompt(messages: list[dict[str, Any]]) -> tuple:
    """从消息列表中分离 system prompt 和历史消息

    Args:
        messages: 完整消息列表

    Returns:
        (system_prompt_text, history_messages) 元组
    """
    system_parts = []
    history = []

    for msg in messages:
        role = msg.get("role", "")
        if role == "system":
            content = msg.get("content", "")
            if content:
                system_parts.append(content)
        else:
            history.append(msg)

    system_prompt = "\n\n".join(system_parts).strip()
    return system_prompt, history

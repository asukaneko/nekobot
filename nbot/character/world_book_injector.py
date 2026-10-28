"""
世界书 PromptStack 注入器

将命中的世界书条目格式化并注入 PromptStack。

注入位置（WorldBookEntry.position）：
- ""（默认）        → world_book 槽（priority 65）
- before_char       → 角色基础提示词之前
- after_char        → 角色基础提示词之后
- before_an/after_an→ 距对话末尾 depth 条处的锚点前/后（由管道插入消息序列）
- at_depth          → 按 depth 插入消息序列（由管道插入）

depth 类条目在不支持消息级插入的通道走默认槽兜底（include_depth_entries=True）。
"""

import logging
from typing import Dict, List, Tuple

from nbot.character.models import WorldBookEntry
from nbot.character.prompt_stack import PromptStack

_log = logging.getLogger(__name__)

MAX_ENTRY_CHARS = 2000
MAX_TOTAL_CHARS = 3000

# 需要插入消息序列的位置（由 AI 管道处理，而非 PromptStack）
DEPTH_POSITIONS = ("at_depth", "before_an", "after_an")
VALID_POSITIONS = ("", "before_char", "after_char", "before_an", "after_an", "at_depth")

# AN 锚点默认深度（对齐 SillyTavern 默认 Author's Note depth）
DEFAULT_AN_DEPTH = 4

_POSITION_LABELS = {
    "before_char": "世界观设定（角色设定之前）",
    "after_char": "世界观设定（角色设定之后）",
    "before_an": "世界观设定（对话末尾锚点前）",
    "after_an": "世界观设定（对话末尾锚点后）",
    "at_depth": "世界观设定（按深度插入）",
}


def normalize_position(value: str) -> str:
    """归一化注入位置，非法值回退为默认槽。"""
    value = str(value or "").strip().lower()
    return value if value in VALID_POSITIONS else ""


def normalize_depth(value, position: str = "") -> int:
    """归一化深度值；非 depth 类位置返回 -1 表示不适用。"""
    if position not in DEPTH_POSITIONS:
        return -1
    try:
        depth = int(value)
    except (TypeError, ValueError):
        depth = DEFAULT_AN_DEPTH
    return max(0, depth)


def split_entries_by_position(
    entries: List[WorldBookEntry],
) -> Dict[str, List[WorldBookEntry]]:
    """按注入位置分组，组内保持传入顺序（匹配得分序）。"""
    groups: Dict[str, List[WorldBookEntry]] = {}
    for entry in entries:
        position = normalize_position(getattr(entry, "position", ""))
        groups.setdefault(position, []).append(entry)
    return groups


def format_entries(
    entries: List[WorldBookEntry],
    max_total_chars: int = MAX_TOTAL_CHARS,
    header: str = "以下是在当前对话中触发的世界观设定：",
) -> Tuple[str, int]:
    """将条目格式化为单段注入文本，返回 (内容, 实际字符数)。"""
    sections = []
    total_chars = 0

    for entry in entries:
        content = entry.content.strip()
        if not content:
            continue

        # 截断单条过长内容
        if len(content) > MAX_ENTRY_CHARS:
            content = content[:MAX_ENTRY_CHARS] + "..."

        section = f"【{entry.name}】\n{content}" if entry.name else content
        section_chars = len(section)

        if total_chars + section_chars > max_total_chars:
            _log.debug(
                "[WorldBookInjector] skip entry %s (%d chars), total %d/%d",
                entry.name, section_chars, total_chars, max_total_chars,
            )
            continue

        sections.append(section)
        total_chars += section_chars

    if not sections:
        return "", 0
    return header + "\n\n" + "\n\n".join(sections), total_chars


def build_depth_insertions(
    entries: List[WorldBookEntry],
    max_total_chars: int = MAX_TOTAL_CHARS,
) -> List[Tuple[int, str]]:
    """构建需要插入消息序列的深度注入。

    Returns:
        [(depth, content)]，depth 相同的条目合并为一条（before_an 在 after_an 之前）。
    """
    groups = split_entries_by_position(entries)
    merged: Dict[Tuple[str, int], List[WorldBookEntry]] = {}
    for position in ("before_an", "after_an", "at_depth"):
        for entry in groups.get(position, []):
            depth = normalize_depth(getattr(entry, "depth", DEFAULT_AN_DEPTH), position)
            merged.setdefault((position, depth), []).append(entry)

    insertions: Dict[int, List[str]] = {}
    for (position, depth), group in merged.items():
        content, _ = format_entries(
            group,
            max_total_chars=max_total_chars,
            header=f"以下是{_POSITION_LABELS[position]}（深度 {depth}）：",
        )
        if content:
            insertions.setdefault(depth, []).append(content)

    return [(depth, "\n\n".join(parts)) for depth, parts in sorted(insertions.items())]


def inject_world_book(
    stack: PromptStack,
    entries: List[WorldBookEntry],
    max_total_chars: int = MAX_TOTAL_CHARS,
    include_depth_entries: bool = True,
) -> None:
    """将命中的世界书条目注入 PromptStack

    Args:
        stack: 当前轮的 PromptStack
        entries: 命中的条目列表（已按 priority 排序）
        max_total_chars: 注入内容总字符上限
        include_depth_entries: True 时 depth 类条目并入默认槽（兜底）；
            False 时跳过，由调用方按深度插入消息序列。
    """
    if not entries:
        return

    groups = split_entries_by_position(entries)

    # before_char / after_char：相对角色基础提示词的独立段
    for position, key, priority, stack_position in (
        ("before_char", "world_book.before_char", PromptStack.PRIORITY_CHARACTER_PROFILE - 1, "before"),
        ("after_char", "world_book.after_char", PromptStack.PRIORITY_CHARACTER_PROFILE + 1, "after"),
    ):
        group = groups.get(position, [])
        if not group:
            continue
        content, total_chars = format_entries(
            group,
            max_total_chars=max_total_chars,
            header=f"以下是{_POSITION_LABELS[position]}：",
        )
        if content:
            stack.add(key, content, priority=priority, position=stack_position)
            _log.debug(
                "[WorldBookInjector] injected %d entries at %s, %d chars",
                len(group), position, total_chars,
            )

    # 默认槽：默认位置 + （可选）depth 类兜底
    default_entries = list(groups.get("", []))
    if include_depth_entries:
        for position in DEPTH_POSITIONS:
            default_entries.extend(groups.get(position, []))
    if not default_entries:
        return

    content, total_chars = format_entries(default_entries, max_total_chars=max_total_chars)
    if not content:
        return

    stack.add(
        "world_book",
        content,
        priority=PromptStack.PRIORITY_WORLD_BOOK,
        scope="turn",
    )

    _log.debug(
        "[WorldBookInjector] injected %d default-slot entries, %d chars",
        len(default_entries), total_chars,
    )

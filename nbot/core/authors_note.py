"""Author's Note（作者注记）

对标 SillyTavern 的 Author's Note：一段附加指令，按"距对话末尾第 N 条"插入
消息序列，用来在不改角色卡的前提下临时调整语气、推进情节或强调规则。

与 P1-2 的世界书 ``before_an/after_an`` 锚点复用同一套深度插入语义，
但来源独立（会话级配置，可被全局默认值兜底）。

配置结构::

    {
        "enabled": true,
        "text": "（提醒：最近角色话变少了，请更主动一些）",
        "depth": 4,              # 距对话末尾第 4 条（0 = 最末尾）
        "role": "system",        # system | user | assistant
        "position": "before_an"  # before_an | after_an（同一深度时的先后）
    }
"""

from typing import Any, Dict, Optional, Tuple

DEFAULT_DEPTH = 4
MAX_DEPTH = 100
VALID_ROLES = ("system", "user", "assistant")
VALID_POSITIONS = ("before_an", "after_an")


def _as_int(value: Any, default: int = DEFAULT_DEPTH) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, min(MAX_DEPTH, number))


def normalize_authors_note(raw: Any) -> Dict[str, Any]:
    """归一化 Author's Note 配置；无效/空文本返回空字典。"""
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        return {}

    text = str(raw.get("text") or raw.get("content") or "").strip()
    if not text:
        return {}

    role = str(raw.get("role") or "system").strip().lower()
    if role not in VALID_ROLES:
        role = "system"

    position = str(raw.get("position") or "before_an").strip().lower()
    if position not in VALID_POSITIONS:
        position = "before_an"

    return {
        "enabled": bool(raw.get("enabled", True)),
        "text": text[:2000],
        "depth": _as_int(raw.get("depth", DEFAULT_DEPTH)),
        "role": role,
        "position": position,
    }


def resolve_authors_note(
    session_note: Any,
    default_note: Any = None,
) -> Dict[str, Any]:
    """会话配置优先，其次全局默认；两者都无有效配置时返回空字典。"""
    resolved = normalize_authors_note(session_note)
    if resolved:
        return resolved
    return normalize_authors_note(default_note)


def build_insertion(
    note: Any,
    expander=None,
) -> Optional[Tuple[int, str, str]]:
    """构建插入项，返回 ``(depth, role, content)``；未启用或无文本返回 None。

    Args:
        note: 归一化前后的配置均可。
        expander: 可选 ``callable(text) -> text``，用于展开宏。
    """
    normalized = normalize_authors_note(note)
    if not normalized or not normalized.get("enabled"):
        return None

    content = normalized["text"]
    if expander is not None:
        try:
            content = expander(content)
        except Exception:  # pragma: no cover - 宏展开失败不影响主流程
            content = normalized["text"]
    content = (content or "").strip()
    if not content:
        return None

    return normalized["depth"], normalized["role"], content


def insertion_index(message_count: int, depth: int) -> int:
    """把"距末尾第 N 条"换算成列表下标（不早于 system 之后）。"""
    return max(1, int(message_count) - int(depth))

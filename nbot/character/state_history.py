"""角色状态快照历史

``states.json`` / ``relationships.json`` 只保留最新值（覆盖式），无法回看"好感是怎么涨上来的"。
本模块按 ``character_id::scope_id`` 追加状态快照，支持：

- 时间线回看（Web 会话已有逐条消息快照，本模块补齐 QQ/TG/飞书等频道态）
- 差分（两次快照之间的数值变化）
- 恢复（把状态/关系回滚到某个快照；恢复范围只有状态与六维关系）

存储：``data/character/state_history.json``，结构 ``{key: [snapshot, ...]}``。
每个 key 上限默认 200 条，超出后**按上限内合并**而不是直接丢最旧：
始终保留首条（基线）与末条（当前值），并优先保留手动修改/恢复/评估
以及数值大幅变化的"关键变化点"，其余按变化幅度与时间新旧取舍。
"""

import logging
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from nbot.character.storage.json_store import JsonStore

_log = logging.getLogger(__name__)

DEFAULT_MAX_SNAPSHOTS = 200
RELATIONSHIP_FIELDS = (
    "affection",
    "trust",
    "familiarity",
    "dependency",
    "security",
    "jealousy",
)
STATE_FIELDS = ("mood", "mood_intensity", "energy")

# 这些来源的快照代表明确的用户/系统决策，合并时优先保留
KEY_SOURCES = frozenset({"manual", "restore", "assessment", "eval", "evaluation"})
# 单字段变化达到该幅度即视为关键变化点
KEY_DELTA_THRESHOLD = 5.0
MOOD_CHANGE_WEIGHT = 2.0
KEY_SOURCE_WEIGHT = 6.0
KEY_DELTA_WEIGHT = 3.0


def _key(character_id: str, scope_id: str) -> str:
    return f"{str(character_id or '').strip()}::{str(scope_id or '').strip()}"


def _num(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _change_score(previous: Dict[str, Any], current: Dict[str, Any]) -> float:
    """给一条快照打分：变化越大、来源越关键，分数越高。"""
    score = 0.0
    for field in RELATIONSHIP_FIELDS:
        if field not in previous and field not in current:
            continue
        delta = abs(_num(current.get(field)) - _num(previous.get(field)))
        score += delta
        if delta >= KEY_DELTA_THRESHOLD:
            score += KEY_DELTA_WEIGHT
    old_mood = str(previous.get("mood", ""))
    new_mood = str(current.get("mood", ""))
    if old_mood != new_mood:
        score += MOOD_CHANGE_WEIGHT
    for field in ("mood_intensity", "energy"):
        if field not in previous and field not in current:
            continue
        score += abs(_num(current.get(field)) - _num(previous.get(field)))
    if str(current.get("source") or "") in KEY_SOURCES:
        score += KEY_SOURCE_WEIGHT * 10
    return score


def compact_history(
    history: List[Dict[str, Any]],
    limit: int = DEFAULT_MAX_SNAPSHOTS,
) -> List[Dict[str, Any]]:
    """把超限快照压缩到 limit 条：保留首/末与关键变化点，其余按分数取舍。

    - 首条（基线）与末条（当前值）一定保留；
    - 手动修改 / 恢复 / 评估类来源优先保留；
    - 其余按"相对上一条的变化幅度 + 时间新旧"排序，取前若干条后按时间还原。
    """
    limit = max(2, int(limit or 2))
    if len(history) <= limit:
        return list(history)

    head = history[0]
    tail = history[-1]
    interior = history[1:-1]
    slots = max(0, limit - 2)
    if slots == 0:
        return [head, tail]

    ranked: List[Tuple[float, int, Dict[str, Any]]] = []
    for offset, item in enumerate(interior):
        previous = history[offset]  # interior[i] 对应 history[i + 1]
        ranked.append((_change_score(previous, item), offset, item))
    # 分数降序；同分时保留更近的快照
    ranked.sort(key=lambda entry: (entry[0], entry[1]), reverse=True)
    keep_offsets = {offset for _, offset, _ in ranked[:slots]}

    kept = [head]
    for offset, item in enumerate(interior):
        if offset in keep_offsets:
            kept.append(item)
    kept.append(tail)
    return kept


class StateHistoryStore:
    """按会话（scope）追加式保存角色状态快照。"""

    def __init__(self, base_dir: str, max_snapshots: int = DEFAULT_MAX_SNAPSHOTS):
        import os

        self._store = JsonStore(os.path.join(base_dir, "data", "character", "state_history.json"))
        self.max_snapshots = max(10, int(max_snapshots))

    # ---- 写入 ----

    def record(
        self,
        character_id: str,
        scope_id: str,
        state: Any = None,
        relationship: Any = None,
        *,
        source: str = "turn",
        reason: str = "",
        message_id: str = "",
        force: bool = False,
    ) -> Optional[Dict[str, Any]]:
        """追加一条快照；与上一条完全相同时跳过（force=True 可强制写入）。"""
        character_id = str(character_id or "").strip()
        scope_id = str(scope_id or "").strip()
        if not character_id or not scope_id:
            return None

        snapshot = self._build_snapshot(state, relationship, source=source, reason=reason,
                                        message_id=message_id)
        if not snapshot:
            return None

        key = _key(character_id, scope_id)
        history = self._load(key)
        if not force and history and self._same_state(history[-1], snapshot):
            return history[-1]

        history.append(snapshot)
        if len(history) > self.max_snapshots:
            # 上限内合并：保留首/末与关键变化点，而不是直接丢最旧
            history = compact_history(history, self.max_snapshots)
        self._store.set(key, history)
        return snapshot

    @staticmethod
    def _build_snapshot(state: Any, relationship: Any, *, source: str, reason: str,
                        message_id: str) -> Dict[str, Any]:
        if state is None and relationship is None:
            return {}
        snapshot: Dict[str, Any] = {
            "ts": datetime.now().astimezone().isoformat(),
            "source": str(source or "turn"),
            "reason": str(reason or "")[:200],
            "message_id": str(message_id or ""),
        }
        for field in STATE_FIELDS:
            value = getattr(state, field, None) if state is not None else None
            if value is None and isinstance(state, dict):
                value = state.get(field)
            if value is not None:
                snapshot[field] = value
        for field in RELATIONSHIP_FIELDS:
            value = getattr(relationship, field, None) if relationship is not None else None
            if value is None and isinstance(relationship, dict):
                value = relationship.get(field)
            if value is not None:
                snapshot[field] = value
        return snapshot

    @staticmethod
    def _same_state(previous: Dict[str, Any], current: Dict[str, Any]) -> bool:
        fields = set(STATE_FIELDS) | set(RELATIONSHIP_FIELDS)
        for field in fields:
            if field not in previous and field not in current:
                continue
            if field == "mood":
                if str(previous.get("mood", "")) != str(current.get("mood", "")):
                    return False
                continue
            if abs(_num(previous.get(field)) - _num(current.get(field))) > 1e-6:
                return False
        return True

    # ---- 读取 ----

    def _load(self, key: str) -> List[Dict[str, Any]]:
        data = self._store.get(key)
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []

    def list_snapshots(
        self,
        character_id: str,
        scope_id: str,
        limit: int = 50,
        since: str = "",
    ) -> List[Dict[str, Any]]:
        """返回最近的快照（按时间升序），limit<=0 表示全部。"""
        history = self._load(_key(character_id, scope_id))
        if since:
            history = [item for item in history if str(item.get("ts") or "") >= since]
        limit = int(limit or 0)
        if limit > 0:
            history = history[-limit:]
        return history

    def list_scopes(self, character_id: str) -> List[Dict[str, Any]]:
        """列出该角色有历史记录的 scope 及条数。"""
        prefix = f"{str(character_id or '').strip()}::"
        result = []
        for key, value in self._store.list_all().items():
            if not key.startswith(prefix) or not isinstance(value, list) or not value:
                continue
            result.append({
                "scope_id": key[len(prefix):],
                "count": len(value),
                "last_ts": str((value[-1] or {}).get("ts") or ""),
            })
        result.sort(key=lambda item: item["last_ts"], reverse=True)
        return result

    def trend(
        self,
        character_id: str,
        scope_id: str,
        limit: int = 100,
        fields: Optional[List[str]] = None,
    ) -> Dict[str, Any]:
        """返回趋势序列（供折线图）：``{fields: [...], points: [{ts, ...}]}``。"""
        snapshot_list = self.list_snapshots(character_id, scope_id, limit=limit)
        wanted = [f for f in (fields or list(RELATIONSHIP_FIELDS) + list(STATE_FIELDS)) if f]
        points = []
        for item in snapshot_list:
            point = {"ts": item.get("ts", ""), "source": item.get("source", "")}
            for field in wanted:
                if field in item:
                    point[field] = item[field]
            points.append(point)
        return {"fields": wanted, "points": points, "count": len(points)}

    # ---- 维护 ----

    def compact(self, character_id: str, scope_id: str) -> Dict[str, Any]:
        """对单个 key 做一次上限内合并（用于历史遗留的超限数据）。"""
        key = _key(character_id, scope_id)
        history = self._load(key)
        before = len(history)
        if before <= self.max_snapshots:
            return {"compact": False, "before": before, "after": before}
        merged = compact_history(history, self.max_snapshots)
        self._store.set(key, merged)
        return {"compact": True, "before": before, "after": len(merged)}

    def clear(self, character_id: str, scope_id: str) -> bool:
        return self._store.delete(_key(character_id, scope_id))


def snapshot_diff(before: Dict[str, Any], after: Dict[str, Any]) -> List[Dict[str, Any]]:
    """计算两次快照的差异，返回 [{field, before, after, delta}]。"""
    changes = []
    for field in list(RELATIONSHIP_FIELDS) + list(STATE_FIELDS):
        if field not in before and field not in after:
            continue
        if field == "mood":
            old, new = str(before.get("mood", "")), str(after.get("mood", ""))
            if old != new:
                changes.append({"field": field, "before": old, "after": new})
            continue
        old, new = _num(before.get(field)), _num(after.get(field))
        if abs(new - old) > 1e-6:
            changes.append({
                "field": field,
                "before": round(old, 2),
                "after": round(new, 2),
                "delta": round(new - old, 2),
            })
    return changes

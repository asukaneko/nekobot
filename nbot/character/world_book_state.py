"""世界书注入状态：冷却轮数与单会话注入次数限制的持久化。

按「角色 + 会话（scope）」维度记录每个条目最近一次注入的轮次与累计次数，
用于执行 WorldBookEntry.cooldown_turns / max_injections_per_session。
"""

import json
import logging
import os
import threading
from typing import Any, Dict, List, Optional

_log = logging.getLogger(__name__)

_DEFAULT_PATH = os.path.join("data", "character", "world_book_state.json")


class WorldBookStateStore:
    def __init__(self, path: str = ""):
        self.path = path or _DEFAULT_PATH
        self._lock = threading.Lock()
        self._data = self._load()

    def begin_turn(self, scope_key: str) -> int:
        """推进并返回该 scope 的轮次计数。"""
        with self._lock:
            scope = self._scope(scope_key)
            scope["turn"] = int(scope.get("turn", 0)) + 1
            self._save_locked()
            return scope["turn"]

    def filter_and_record(
        self,
        scope_key: str,
        results: List[Any],
        turn: int,
    ) -> List[Any]:
        """按冷却与次数限制过滤匹配结果，并记录本轮实际注入的条目。"""
        with self._lock:
            scope = self._scope(scope_key)
            records: Dict[str, Dict[str, int]] = scope.setdefault("entries", {})
            kept = []
            for result in results:
                entry = getattr(result, "entry", None)
                if entry is None:
                    kept.append(result)
                    continue
                # 常驻条目不受冷却与次数限制
                if "always" in (getattr(result, "trigger_sources", None) or []):
                    kept.append(result)
                    continue

                entry_key = str(entry.id or entry.name or "")
                if not entry_key:
                    kept.append(result)
                    continue

                record = records.get(entry_key) or {}
                count = int(record.get("count", 0))
                last_turn = int(record.get("last_turn", 0))

                if entry.max_injections_per_session and count >= entry.max_injections_per_session:
                    continue
                if entry.cooldown_turns and last_turn and turn - last_turn <= entry.cooldown_turns:
                    continue

                records[entry_key] = {"count": count + 1, "last_turn": turn}
                kept.append(result)

            scope["entries"] = records
            self._save_locked()
            return kept

    def reset_scope(self, scope_key: str) -> None:
        with self._lock:
            self._data.setdefault("scopes", {}).pop(scope_key, None)
            self._save_locked()

    def _scope(self, scope_key: str) -> Dict[str, Any]:
        scopes = self._data.setdefault("scopes", {})
        return scopes.setdefault(str(scope_key or "default"), {})

    def _load(self) -> Dict[str, Any]:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {"scopes": {}}
        except (OSError, ValueError):
            return {"scopes": {}}

    def _save_locked(self) -> None:
        try:
            directory = os.path.dirname(self.path)
            if directory:
                os.makedirs(directory, exist_ok=True)
            tmp_path = self.path + ".tmp"
            with open(tmp_path, "w", encoding="utf-8") as fh:
                json.dump(self._data, fh, ensure_ascii=False, indent=2)
            os.replace(tmp_path, self.path)
        except OSError as exc:
            _log.warning("[WorldBookState] save failed: %s", exc)


_store: Optional[WorldBookStateStore] = None
_store_lock = threading.Lock()


def get_world_book_state_store(path: str = "") -> WorldBookStateStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = WorldBookStateStore(path)
    return _store

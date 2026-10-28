"""成就管理器：线程安全的进度追踪、解锁与持久化。"""

from __future__ import annotations

import json
import os
import threading
from datetime import datetime

from nbot.achievements.definitions import get_achievement_defs
from nbot.achievements.models import AchievementDef


class AchievementManager:
    """管理成就进度、解锁状态与增量计数器。

    持久化文件：<data_dir>/achievements.json
    结构：
        {
          "counters": {"messages": 0, "tokens": 0, "cost": 0.0},
          "achievements": {"<id>": {"progress": 0.0, "unlocked": false, "unlocked_at": ""}},
          "last_refresh_at": "..."
        }
    """

    def __init__(self, data_dir: str):
        self._data_dir = data_dir
        self._file_path = os.path.join(data_dir, "achievements.json")
        self._lock = threading.Lock()
        self._counters: dict[str, float] = {
            "messages": 0.0,
            "tokens": 0.0,
            "cost": 0.0,
        }
        self._states: dict[str, dict] = {}
        self._load()

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load(self):
        os.makedirs(self._data_dir, exist_ok=True)
        try:
            if os.path.exists(self._file_path):
                with open(self._file_path, encoding="utf-8") as f:
                    data = json.load(f)
                counters = data.get("counters") or {}
                self._counters.update({k: float(v) for k, v in counters.items()})
                self._states = data.get("achievements") or {}
        except Exception:
            self._states = {}
        # 保证所有定义都有状态条目
        for d in get_achievement_defs():
            self._states.setdefault(d.id, {"progress": 0.0, "unlocked": False, "unlocked_at": ""})

    def _save(self):
        try:
            with open(self._file_path, "w", encoding="utf-8") as f:
                json.dump({
                    "counters": self._counters,
                    "achievements": self._states,
                    "last_refresh_at": datetime.now().isoformat(timespec="seconds"),
                }, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ------------------------------------------------------------------
    # 增量计数器
    # ------------------------------------------------------------------

    def record(self, counter: str, amount: float):
        """递增一个计数器（messages / tokens / cost）。"""
        if amount <= 0:
            return
        with self._lock:
            self._counters[counter] = float(self._counters.get(counter, 0.0)) + float(amount)
            newly = self._recheck()
            self._save()
        return newly

    # ------------------------------------------------------------------
    # 刷新 / 解锁
    # ------------------------------------------------------------------

    def _progress_for(self, d: AchievementDef, snapshot: dict) -> float:
        """计算某个成就的当前进度值。"""
        if d.stat_key in self._counters:
            return float(self._counters[d.stat_key])
        return float(snapshot.get(d.stat_key, 0.0) or 0.0)

    def refresh(self, snapshot: dict) -> list[str]:
        """根据快照（外部统计）重算进度并解锁。

        Args:
            snapshot: 形如 {"characters": 3, "worldbooks": 2, ...} 的外部统计。

        Returns:
            本次新解锁的成就 id 列表。
        """
        now = datetime.now().isoformat(timespec="seconds")
        newly_unlocked: list[str] = []
        with self._lock:
            for d in get_achievement_defs():
                state = self._states.setdefault(
                    d.id, {"progress": 0.0, "unlocked": False, "unlocked_at": ""}
                )
                if state.get("unlocked"):
                    continue
                progress = self._progress_for(d, snapshot)
                state["progress"] = min(progress, d.target)
                if progress >= d.target:
                    state["unlocked"] = True
                    state["unlocked_at"] = state.get("unlocked_at") or now
                    state["progress"] = float(d.target)
                    newly_unlocked.append(d.id)
            self._save()
        return newly_unlocked

    def _recheck(self) -> list[str]:
        """在计数器变化后重算（仅基于计数器，不含快照）。"""
        now = datetime.now().isoformat(timespec="seconds")
        newly: list[str] = []
        for d in get_achievement_defs():
            if d.stat_key not in self._counters:
                continue
            state = self._states.setdefault(
                d.id, {"progress": 0.0, "unlocked": False, "unlocked_at": ""}
            )
            if state.get("unlocked"):
                continue
            value = float(self._counters[d.stat_key])
            state["progress"] = min(value, d.target)
            if value >= d.target:
                state["unlocked"] = True
                state["unlocked_at"] = now
                state["progress"] = float(d.target)
                newly.append(d.id)
        return newly

    # ------------------------------------------------------------------
    # 查询
    # ------------------------------------------------------------------

    def list_all(self) -> list[dict]:
        """返回带进度与状态的完整成就列表。"""
        result = []
        for d in get_achievement_defs():
            state = self._states.get(d.id) or {"progress": 0.0, "unlocked": False, "unlocked_at": ""}
            item = d.to_dict()
            item["progress"] = round(min(float(state.get("progress", 0)), d.target), 2)
            item["unlocked"] = bool(state.get("unlocked"))
            item["unlocked_at"] = state.get("unlocked_at") or ""
            item["percent"] = round(min(float(state.get("progress", 0)) / d.target, 1.0) * 100, 1)
            result.append(item)
        return result

    def get_counters(self) -> dict:
        with self._lock:
            return dict(self._counters)

    def summary(self) -> dict:
        items = self.list_all()
        unlocked = [i for i in items if i["unlocked"]]
        return {
            "total": len(items),
            "unlocked": len(unlocked),
            "percent": round(len(unlocked) / len(items) * 100, 1) if items else 0.0,
            "counters": self.get_counters(),
        }

    def reset(self):
        with self._lock:
            self._states = {}
            self._counters = {"messages": 0.0, "tokens": 0.0, "cost": 0.0}
            for d in get_achievement_defs():
                self._states[d.id] = {"progress": 0.0, "unlocked": False, "unlocked_at": ""}
            self._save()

    def apply_restored(self, data: dict):
        """导入备份时恢复计数与解锁状态。"""
        with self._lock:
            self._counters = {k: float(v) for k, v in (data.get("counters") or {}).items()}
            self._states = data.get("achievements") or {}
            for d in get_achievement_defs():
                self._states.setdefault(d.id, {"progress": 0.0, "unlocked": False, "unlocked_at": ""})
            self._save()


_manager: AchievementManager | None = None
_manager_lock = threading.Lock()


def get_achievement_manager(data_dir: str = "data/web") -> AchievementManager:
    global _manager
    if _manager is None:
        with _manager_lock:
            if _manager is None:
                _manager = AchievementManager(data_dir)
    return _manager

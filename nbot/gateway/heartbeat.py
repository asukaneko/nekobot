import json
import os
from datetime import datetime, timedelta
from typing import Any, Callable, Optional


_global_session_heartbeat_manager = None

# 二进制指数退避：会话闲置越久，心跳间隔越长（最多 2^MAX_BACKOFF 倍基础间隔）
MAX_BACKOFF = 4
# life_sim 有效间隔硬上限：8 小时（无论基础间隔多大，退避不超过此值）
MAX_LIFE_SIM_INTERVAL_MINUTES = 480


class SessionHeartbeatManager:
    def __init__(
        self,
        *,
        data_dir: str,
        gateway_getter: Callable[[], Any],
        executor: Callable[..., Any],
        activity_getter: Optional[Callable[[str], Optional[datetime]]] = None,
    ):
        self.data_dir = data_dir
        self.gateway_getter = gateway_getter
        self.executor = executor
        # 查询会话最后用户活动时间（用于指数退避判定）
        self.activity_getter = activity_getter
        self._configs = self._load()

    @property
    def storage_path(self) -> str:
        return os.path.join(self.data_dir, "session_heartbeats.json")

    def _default_config(self, session_id: str) -> dict[str, Any]:
        return {
            "session_id": session_id,
            "target_session_id": session_id,
            "enabled": False,
            "interval_minutes": 60,
            "content_file": "heartbeat.md",
            "last_run": None,
            "next_run": None,
            "last_trace_id": "",
            "last_gateway_status": "",
            "silent": False,
            "backoff_count": 0,
            "last_user_activity": None,
        }

    def _normalize_config(self, session_id: str, config: Optional[dict[str, Any]]) -> dict[str, Any]:
        normalized = self._default_config(session_id)
        normalized.update(config or {})
        normalized["session_id"] = session_id
        normalized["target_session_id"] = (
            str(normalized.get("target_session_id") or session_id).strip() or session_id
        )
        normalized["enabled"] = bool(normalized.get("enabled", False))
        try:
            normalized["interval_minutes"] = max(
                1, int(normalized.get("interval_minutes", 60) or 60)
            )
        except Exception:
            normalized["interval_minutes"] = 60
        normalized["content_file"] = (
            str(normalized.get("content_file") or "heartbeat.md").strip() or "heartbeat.md"
        )
        normalized["last_trace_id"] = str(normalized.get("last_trace_id") or "")
        normalized["last_gateway_status"] = str(normalized.get("last_gateway_status") or "")
        normalized["silent"] = bool(normalized.get("silent", False))
        try:
            normalized["backoff_count"] = max(0, int(normalized.get("backoff_count", 0) or 0))
        except Exception:
            normalized["backoff_count"] = 0
        return normalized

    def _load(self) -> dict[str, dict[str, Any]]:
        try:
            if os.path.exists(self.storage_path):
                with open(self.storage_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return {
                        str(session_id): self._normalize_config(str(session_id), config)
                        for session_id, config in data.items()
                        if isinstance(config, dict)
                    }
        except Exception:
            pass
        return {}

    def _save(self) -> None:
        os.makedirs(self.data_dir, exist_ok=True)
        with open(self.storage_path, "w", encoding="utf-8") as f:
            json.dump(self._configs, f, ensure_ascii=False, indent=2)

    def get_config(self, session_id: str) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            return {}
        config = self._configs.get(session_id)
        if config is None:
            config = self._default_config(session_id)
        return dict(config)

    def set_config(self, session_id: str, config: Optional[dict[str, Any]]) -> dict[str, Any]:
        session_id = str(session_id or "").strip()
        if not session_id:
            raise ValueError("session_id is required")
        merged = self._normalize_config(session_id, {**self.get_config(session_id), **(config or {})})
        self._configs[session_id] = merged
        self._save()
        return dict(merged)

    def disable(self, session_id: str) -> dict[str, Any]:
        return self.set_config(session_id, {"enabled": False, "next_run": None})

    def list_configs(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._configs.values()]

    def list_enabled_configs(self) -> list[dict[str, Any]]:
        return [dict(item) for item in self._configs.values() if item.get("enabled")]

    def any_enabled(self) -> bool:
        return any(item.get("enabled") for item in self._configs.values())

    @staticmethod
    def _is_life_sim_config(config: dict[str, Any]) -> bool:
        session_id = str(config.get("session_id") or "")
        return bool(config.get("life_sim")) or session_id.endswith("__life_sim")

    @staticmethod
    def _is_proactive_config(config: dict[str, Any]) -> bool:
        session_id = str(config.get("session_id") or "")
        return bool(config.get("proactive_chat")) or session_id.endswith("__proactive")

    def _is_due(self, config: dict[str, Any], now: datetime) -> bool:
        if not config.get("enabled"):
            return False

        baseline = self._parse_dt(config.get("last_run"))
        if self._is_proactive_config(config) and self.activity_getter:
            target_sid = str(config.get("target_session_id") or config.get("session_id") or "")
            try:
                last_user_activity = self.activity_getter(target_sid)
            except Exception:
                last_user_activity = None
            last_user_activity = self._parse_dt(last_user_activity)
            if last_user_activity and (baseline is None or last_user_activity > baseline):
                baseline = last_user_activity

        if baseline is None:
            return True

        base = int(config.get("interval_minutes", 60) or 60)
        # 只有“同步现实时间”的 life simulation 使用指数退避。
        backoff = 0
        if self._is_life_sim_config(config):
            backoff = min(int(config.get("backoff_count", 0) or 0), MAX_BACKOFF)
        effective_interval = min(base * (2 ** backoff), MAX_LIFE_SIM_INTERVAL_MINUTES)
        due_at = baseline + timedelta(minutes=effective_interval)
        normalized_now = self._parse_dt(now) or now
        return due_at <= normalized_now

    async def execute_due_sessions(self) -> list[Any]:
        now = datetime.now()
        results = []
        for config in self.list_enabled_configs():
            if self._is_due(config, now):
                results.append(await self.execute_session(config["session_id"]))
        return results

    async def execute_session(
        self,
        session_id: str,
        *,
        force: bool = False,
        trigger_source: str = "system",
    ):
        config = self.get_config(session_id)
        if not config:
            raise ValueError("session heartbeat config not found")

        gateway = self.gateway_getter() if self.gateway_getter else None
        if gateway:
            result = await gateway.submit_internal_task(
                task_kind="heartbeat",
                task_id=f"heartbeat:{session_id}",
                task_name="heartbeat",
                handler=lambda: self.executor(session_id, dict(config), force=force),
                trigger_source=trigger_source,
                conversation_id=session_id,
                metadata={"target_session_id": config.get("target_session_id", session_id)},
            )
        else:
            direct_result = self.executor(session_id, dict(config), force=force)
            if hasattr(direct_result, "__await__"):
                direct_result = await direct_result
            result = direct_result

        if hasattr(result, "trace_id"):
            updated = self.get_config(session_id)
            now = datetime.now()
            updated["last_run"] = now.isoformat()
            updated["next_run"] = None
            updated["last_trace_id"] = getattr(result, "trace_id", "") or ""
            updated["last_gateway_status"] = getattr(result, "status", "") or ""
            target_sid = str(updated.get("target_session_id") or session_id)
            prev_user_activity = updated.get("last_user_activity")
            current_user_activity = None
            if self.activity_getter:
                try:
                    current_user_activity = self.activity_getter(target_sid)
                except Exception:
                    current_user_activity = None
            current_user_activity = self._parse_dt(current_user_activity)
            if not self._is_life_sim_config(updated):
                updated["backoff_count"] = 0
            else:
                # life simulation 在用户重新活跃时重置退避，否则递增。
                if (
                    current_user_activity
                    and (
                        not prev_user_activity
                        or current_user_activity > self._parse_dt(prev_user_activity)
                    )
                ):
                    updated["backoff_count"] = 0
                else:
                    updated["backoff_count"] = min(
                        int(updated.get("backoff_count", 0) or 0) + 1,
                        MAX_BACKOFF,
                    )
            updated["last_user_activity"] = (
                current_user_activity.isoformat() if current_user_activity else None
            )
            self.set_config(session_id, updated)
        return result

    @staticmethod
    def _parse_dt(value):
        if not value:
            return None
        try:
            text = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            return None


def get_session_heartbeat_manager() -> "SessionHeartbeatManager | None":
    return _global_session_heartbeat_manager


def set_session_heartbeat_manager(manager: "SessionHeartbeatManager | None") -> None:
    global _global_session_heartbeat_manager
    _global_session_heartbeat_manager = manager

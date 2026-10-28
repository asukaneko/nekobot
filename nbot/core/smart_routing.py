"""
智能模型路由

在既有故障转移（priority 队列）之上，提供基于成本/质量/速度/上下文的
自动打分排序、日预算控制、决策日志与可解释路由分析，并支持 A/B 测试分流。

设置存储：<data_dir>/settings.json 的 "smart_routing" 键。
决策日志：<data_dir>/routing_logs.json。
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import threading
import uuid
from datetime import datetime

_log = logging.getLogger(__name__)

DEFAULT_SETTINGS = {
    "enabled": False,
    "daily_budget": 0.0,              # 日预算（元），0 = 不限
    "budget_warning_pct": 80.0,       # 预算告警阈值（%）
    "weights": {
        "cost": 0.35,
        "quality": 0.30,
        "speed": 0.15,
        "priority": 0.20,
    },
    "ab_test": {
        "enabled": False,
        "control_model_id": "",
        "experiment_model_id": "",
        "ratio": 50,
    },
}

# 估算时假设 input:output token 比例
_IN_OUT_RATIO = (0.75, 0.25)

# 速度分的满分线：实测 50 tok/s 及以上记为满分
_TARGET_TOK_PER_SEC = 50.0


def _clamp01(value: float) -> float:
    return max(0.0, min(1.0, value))


def _est_cost_per_1k(config: dict) -> float:
    """估算每 1K token 的平均成本（元），基于配置价格，缺省时返回 0。"""
    in_price = config.get("input_price")
    out_price = config.get("output_price")
    if in_price is None or out_price is None:
        return 0.0
    try:
        in_price = float(in_price)
        out_price = float(out_price)
    except (TypeError, ValueError):
        return 0.0
    return (in_price * _IN_OUT_RATIO[0] + out_price * _IN_OUT_RATIO[1]) / 1000.0


def _cost_score(cost_per_1k: float) -> float:
    """成本分：越便宜分越高。0.001 元/1K 为满分，1 元/1K 以上为 0 分。"""
    if cost_per_1k <= 0:
        return 0.7  # 未知价格给中性偏高分，避免误伤
    return _clamp01(1.0 - math.log10(max(cost_per_1k, 0.00001) / 0.001) / 3.0)


def _quality_from_analytics(analytics: dict, model_id: str) -> float:
    """质量分：基于决策日志里的历史成功率（analytics 由调用方一次性取好）。"""
    stat = ((analytics or {}).get("models") or {}).get(model_id) or {}
    calls = int(stat.get("calls") or 0)
    if calls >= 2:
        return _clamp01(float(stat.get("success_rate") or 0.5))
    return 0.5


def _quality_score(model_id: str, data_dir: str) -> float:
    """质量分（单模型入口，内部一次性读取决策日志）。"""
    try:
        return _quality_from_analytics(_aggregate_analytics(data_dir), model_id)
    except Exception:
        return 0.5


def _speed_kpis(data_dir: str = "") -> dict[str, float]:
    """汇总最近 7 天每个模型的实测输出速度（tok/s），键为**模型名**。

    ``token_stats`` 明细里的 ``model`` 存的是模型名（如 ``deepseek-chat``），
    不是模型配置 id；调用方按「模型名 → 模型 id」顺序双查。
    """
    try:
        from nbot.core.token_stats import get_token_stats_manager
        records = get_token_stats_manager().get_records("7d") or []
    except Exception:
        return {}

    buckets: dict[str, list[float]] = {}
    for record in records:
        name = str(record.get("model") or "").strip()
        if not name:
            continue
        try:
            duration_ms = float(record.get("duration_ms") or 0)
            output_tokens = float(record.get("output") or 0)
        except (TypeError, ValueError):
            continue
        if duration_ms <= 0 or output_tokens <= 0:
            continue
        buckets.setdefault(name, []).append(output_tokens / (duration_ms / 1000.0))
    return {name: sum(values) / len(values) for name, values in buckets.items() if values}


def _speed_score(tps_map: dict[str, float], model_id: str, model_name: str) -> float:
    """速度分：实测 tok/s ÷ 目标值（50 tok/s 满分）；无实测数据给中性 0.5。"""
    tps = 0.0
    for key in (model_name, model_id):
        if key:
            value = (tps_map or {}).get(key)
            if value:
                tps = float(value)
                break
    if tps <= 0:
        return 0.5
    return _clamp01(tps / _TARGET_TOK_PER_SEC)


def ab_group_for(session_id: str, ratio: int) -> str:
    """基于会话 ID 的确定性分流。"""
    if not session_id:
        return "control"
    h = int(hashlib.md5(str(session_id).encode("utf-8")).hexdigest(), 16)
    return "experiment" if (h % 100) < int(ratio) else "control"


class SmartRouter:
    """智能路由核心。"""

    def __init__(self, data_dir: str = "data/web"):
        self._data_dir = data_dir
        self._log_file = os.path.join(data_dir, "routing_logs.json")
        self._lock = threading.Lock()
        self._settings: dict = {}
        self.load_settings()

    # ------------------------------------------------------------------
    # 设置
    # ------------------------------------------------------------------

    def load_settings(self) -> dict:
        merged = dict(DEFAULT_SETTINGS)
        try:
            settings_path = os.path.join(self._data_dir, "settings.json")
            if os.path.exists(settings_path):
                with open(settings_path, encoding="utf-8") as f:
                    data = json.load(f)
                sr = (data.get("smart_routing") or {}) if isinstance(data, dict) else {}
                if isinstance(sr, dict):
                    for key in merged:
                        if key in sr:
                            merged[key] = sr[key]
                    merged["weights"] = {**merged["weights"], **((sr.get("weights") or {}) if isinstance(sr.get("weights"), dict) else {})}
                    merged["ab_test"] = {**merged["ab_test"], **((sr.get("ab_test") or {}) if isinstance(sr.get("ab_test"), dict) else {})}
        except Exception:
            pass
        self._settings = merged
        return self._settings

    def update_settings(self, payload: dict):
        """保存设置（由 web 路由调用，写入 settings.json 的 smart_routing 键）。"""
        current = dict(self._settings)
        if "enabled" in payload:
            current["enabled"] = bool(payload["enabled"])
        if "daily_budget" in payload:
            try:
                current["daily_budget"] = max(0.0, float(payload["daily_budget"]))
            except (TypeError, ValueError):
                pass
        if "budget_warning_pct" in payload:
            try:
                current["budget_warning_pct"] = max(0.0, min(100.0, float(payload["budget_warning_pct"])))
            except (TypeError, ValueError):
                pass
        if isinstance(payload.get("weights"), dict):
            w = dict(current.get("weights", {}))
            for key, val in payload["weights"].items():
                try:
                    w[key] = max(0.0, float(val))
                except (TypeError, ValueError):
                    pass
            current["weights"] = w
        if isinstance(payload.get("ab_test"), dict):
            ab = dict(current.get("ab_test", {}))
            ab.update({k: payload["ab_test"][k] for k in ("enabled", "control_model_id", "experiment_model_id") if k in payload["ab_test"]})
            try:
                ab["ratio"] = max(0, min(100, int(payload["ab_test"].get("ratio", ab.get("ratio", 50)))))
            except (TypeError, ValueError):
                pass
            current["ab_test"] = ab
        self._settings = current
        self._persist_settings(current)
        return current

    def _persist_settings(self, settings: dict):
        """写入 settings.json 的 smart_routing 键。"""
        try:
            settings_path = os.path.join(self._data_dir, "settings.json")
            os.makedirs(self._data_dir, exist_ok=True)
            data = {}
            if os.path.exists(settings_path):
                with open(settings_path, encoding="utf-8") as f:
                    data = json.load(f)
            if not isinstance(data, dict):
                data = {}
            data["smart_routing"] = settings
            with open(settings_path, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    def settings_dict(self) -> dict:
        return self._settings

    # ------------------------------------------------------------------
    # 预算
    # ------------------------------------------------------------------

    def budget_status(self) -> dict:
        budget = float(self._settings.get("daily_budget") or 0.0)
        today_cost = 0.0
        try:
            from nbot.core.token_stats import get_token_stats_manager
            mgr = get_token_stats_manager()
            for r in (mgr.get_records("today") or []):
                today_cost += float(r.get("cost") or 0.0)
        except Exception:
            pass
        if budget <= 0:
            return {
                "budget": 0.0, "used": round(today_cost, 2),
                "percent": 0.0, "remaining": 0.0, "exhausted": False,
                "pressure": 0.0,
            }
        percent = _clamp01(today_cost / budget) * 100.0
        return {
            "budget": budget,
            "used": round(today_cost, 2),
            "percent": round(percent, 1),
            "remaining": round(max(0.0, budget - today_cost), 2),
            "exhausted": today_cost >= budget,
            "pressure": _clamp01(today_cost / budget),
        }

    # ------------------------------------------------------------------
    # 打分
    # ------------------------------------------------------------------

    def score_model(
        self,
        config: dict,
        *,
        pressure: float = 0.0,
        needed_context: int = 16000,
        has_tools: bool = False,
        analytics: dict | None = None,
        tps_map: dict | None = None,
    ) -> dict:
        """给单个模型配置打分。

        ``analytics`` / ``tps_map`` 可由调用方一次性取好复用，避免逐个模型重读
        决策日志与 token 统计；不传时按单模型入口自行取数。
        """
        weights = self._settings.get("weights", DEFAULT_SETTINGS["weights"])
        cost_w = float(weights.get("cost", 0.35))
        qual_w = float(weights.get("quality", 0.30))
        speed_w = float(weights.get("speed", 0.15))
        prio_w = float(weights.get("priority", 0.20))

        # 预算压力提高成本权重
        effective_cost_w = cost_w + pressure * 0.4
        total_w = effective_cost_w + qual_w + speed_w + prio_w

        model_id = config.get("model_id", "")
        model_name = config.get("model", "")

        if analytics is None:
            analytics = _aggregate_analytics(self._data_dir)
        if tps_map is None:
            tps_map = _speed_kpis(self._data_dir)

        cost_per_1k = _est_cost_per_1k(config)
        components = {
            "cost": round(_cost_score(cost_per_1k) if cost_per_1k > 0 else 0.5, 4),
            "quality": round(_quality_from_analytics(analytics, model_id), 4),
            "speed": round(_speed_score(tps_map, model_id, model_name), 4),
            "context": _clamp01(float(config.get("max_context_length") or 0) / max(needed_context, 1)),
            "tools": 1.0 if (config.get("supports_tools", True) or not has_tools) else 0.0,
            "priority": _clamp01(1.0 - (float(config.get("priority") or 0)) / 100.0),
        }
        score = (
            components["cost"] * effective_cost_w
            + components["quality"] * qual_w
            + components["speed"] * speed_w
            + components["priority"] * prio_w
        ) / total_w

        return {
            "model_id": model_id,
            "model": model_name,
            "score": round(score, 4),
            "components": components,
            "cost_per_1k": round(cost_per_1k, 6),
        }

    def route(
        self,
        model_configs: list,
        *,
        purpose: str = "",
        session_id: str = "",
        has_tools: bool = False,
        approx_context: int = 16000,
    ) -> list:
        """对模型队列进行智能排序；未启用或配置异常时原样返回。"""
        ranked, _ = self.route_with_scores(
            model_configs,
            purpose=purpose,
            session_id=session_id,
            has_tools=has_tools,
            approx_context=approx_context,
        )
        return ranked

    def route_with_scores(
        self,
        model_configs: list,
        *,
        purpose: str = "",
        session_id: str = "",
        has_tools: bool = False,
        approx_context: int = 16000,
    ) -> tuple[list, list]:
        """返回 (排序后的配置列表, 打分快照列表)。未启用时返回原列表 + 空快照。"""
        if not self._settings.get("enabled"):
            return model_configs, []
        configs = [c for c in (model_configs or []) if c.get("model_id")]
        if not configs:
            return model_configs, []

        status = self.budget_status()
        pressure = status.get("pressure", 0.0)
        # 决策日志与 token 统计各读一次，所有候选共用
        analytics = self.analytics()
        tps_map = _speed_kpis(self._data_dir)

        scored = [
            (
                self.score_model(
                    c,
                    pressure=pressure,
                    needed_context=approx_context,
                    has_tools=has_tools,
                    analytics=analytics,
                    tps_map=tps_map,
                ),
                c,
            )
            for c in configs
        ]
        scored.sort(key=lambda item: item[0]["score"], reverse=True)

        # A/B 测试：实验组优先使用实验模型
        ab = self._settings.get("ab_test", {})
        if ab.get("enabled"):
            group = ab_group_for(session_id, ab.get("ratio", 50))
            if group == "experiment":
                exp_id = ab.get("experiment_model_id", "")
                if exp_id:
                    scored.sort(
                        key=lambda item: (item[0]["model_id"] != exp_id, -item[0]["score"])
                    )
            elif ab.get("control_model_id"):
                ctrl_id = ab.get("control_model_id", "")
                if ctrl_id:
                    scored.sort(
                        key=lambda item: (item[0]["model_id"] != ctrl_id, -item[0]["score"])
                    )

        candidates_snapshot = [s for s, _ in scored]
        return [c for _, c in scored], candidates_snapshot

    # ------------------------------------------------------------------
    # 决策日志
    # ------------------------------------------------------------------

    def record_decision(
        self,
        *,
        session_id: str,
        purpose: str,
        candidates: list,
        selected_id: str,
        selected_name: str,
        reason: str,
        ab_group: str = "",
    ) -> str:
        decision = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now().isoformat(timespec="seconds"),
            "session_id": str(session_id or ""),
            "purpose": purpose,
            "ab_group": ab_group,
            "candidates": candidates,
            "selected_id": selected_id,
            "selected_name": selected_name,
            "reason": reason,
            "result": {"success": None, "duration_ms": None, "ttft_ms": None},
        }
        with self._lock:
            data = self._load_log()
            data["decisions"].append(decision)
            self._cap_and_save(data)
        return decision["id"]

    def backfill_decision(self, decision_id: str, **fields) -> bool:
        with self._lock:
            data = self._load_log()
            for d in data["decisions"]:
                if d.get("id") == decision_id:
                    d.setdefault("result", {}).update(fields)
                    self._cap_and_save(data)
                    return True
        return False

    def decision_logs(self, limit: int = 200) -> list:
        with self._lock:
            data = self._load_log()
            return list(data["decisions"][-limit:])

    def clear_logs(self):
        with self._lock:
            self._save_log({"decisions": []})

    def analytics(self) -> dict:
        with self._lock:
            data = self._load_log()
            return _aggregate_from_decisions(data["decisions"])

    # ------------------------------------------------------------------
    # 持久化
    # ------------------------------------------------------------------

    def _load_log(self) -> dict:
        try:
            if os.path.exists(self._log_file):
                with open(self._log_file, encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict) and isinstance(data.get("decisions"), list):
                    return data
        except Exception:
            pass
        return {"decisions": []}

    def _cap_and_save(self, data: dict):
        # 最多保留 3000 条决策
        data["decisions"] = data["decisions"][-3000:]
        self._save_log(data)

    def _save_log(self, data: dict):
        os.makedirs(self._data_dir, exist_ok=True)
        try:
            with open(self._log_file, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
        except Exception:
            pass


def _aggregate_analytics(data_dir: str) -> dict:
    """聚合决策日志统计：直接读一次 ``routing_logs.json`` 再分组。

    不走 :func:`get_smart_router`，避免被固定到某个 ``data_dir`` 的单例，
    也避免逐个模型重复读盘。
    """
    path = os.path.join(data_dir or "data/web", "routing_logs.json")
    decisions: list = []
    try:
        if os.path.exists(path):
            with open(path, encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict) and isinstance(data.get("decisions"), list):
                decisions = data["decisions"]
    except Exception:
        decisions = []
    return _aggregate_from_decisions(decisions)


def _aggregate_from_decisions(decisions: list) -> dict:
    models: dict[str, dict] = {}
    total_calls = 0
    success_calls = 0
    for d in decisions:
        selected_id = d.get("selected_id", "")
        if not selected_id:
            continue
        total_calls += 1
        stat = models.setdefault(selected_id, {
            "model_id": selected_id,
            "model": d.get("selected_name", selected_id),
            "calls": 0, "success": 0, "failed": 0,
            "score_sum": 0.0, "selection": 0, "cost_est_sum": 0.0,
        })
        stat["calls"] += 1
        stat["selection"] += 1
        result = d.get("result") or {}
        if result.get("success") is True:
            stat["success"] += 1
            success_calls += 1
        elif result.get("success") is False:
            stat["failed"] += 1
        # 取该次选中的候选分数
        selected_candidate = next(
            (c for c in (d.get("candidates") or []) if c.get("model_id") == selected_id), {}
        )
        stat["score_sum"] += float(selected_candidate.get("score") or 0)
        stat["cost_est_sum"] += float(selected_candidate.get("cost_per_1k") or 0)

    result_models = {}
    for mid, stat in models.items():
        calls = max(stat["calls"], 1)
        result_models[mid] = {
            "model_id": mid,
            "model": stat["model"],
            "calls": stat["calls"],
            "success": stat["success"],
            "failed": stat["failed"],
            "success_rate": round(stat["success"] / calls, 4),
            "avg_score": round(stat["score_sum"] / calls, 4),
            "selection_rate": round(stat["selection"] / max(total_calls, 1), 4),
            "avg_cost_per_1k": round(stat["cost_est_sum"] / calls, 6),
        }
    return {
        "total_decisions": total_calls,
        "success_rate": round(success_calls / max(total_calls, 1), 4),
        "models": result_models,
    }


_routers: dict[str, SmartRouter] = {}
_router_lock = threading.Lock()


def get_smart_router(data_dir: str = "data/web") -> SmartRouter:
    """按 ``data_dir``（绝对路径）分键缓存路由器实例。

    旧的全局单例在第一次调用后忽略后续 ``data_dir``：AI 管线用默认值、
    Web 路由用 ``server.data_dir`` 时会读到不同的 ``settings.json``。
    """
    key = os.path.abspath(str(data_dir or "data/web"))
    router = _routers.get(key)
    if router is None:
        with _router_lock:
            router = _routers.get(key)
            if router is None:
                router = SmartRouter(key)
                _routers[key] = router
    return router


def reset_smart_routers() -> None:
    """清空路由器缓存（测试 / 数据目录切换时用）。"""
    with _router_lock:
        _routers.clear()


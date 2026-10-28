"""智能路由：速度分对齐 token_stats 的模型名、路由只读一次统计、单例按目录分键。"""

import json

import pytest

from nbot.core import smart_routing as sr


class _FakeStats:
    """假 token 统计管理器（只提供 get_records）。"""

    def __init__(self, records):
        self._records = list(records)
        self.calls = 0
        self.ranges = []

    def get_records(self, range_key="7d"):
        self.calls += 1
        self.ranges.append(range_key)
        return list(self._records)


def _patch_stats(monkeypatch, records) -> _FakeStats:
    fake = _FakeStats(records)
    monkeypatch.setattr(
        "nbot.core.token_stats.get_token_stats_manager", lambda *a, **k: fake
    )
    return fake


def _router(tmp_path, **settings) -> sr.SmartRouter:
    router = sr.SmartRouter(str(tmp_path))
    router._settings.update(settings)
    return router


SPEED_RECORDS = [
    # 快模型：50 tok/s 与 15 tok/s 混合 → 平均 32.5
    {"model": "fast-model", "output": 500, "duration_ms": 10000},
    {"model": "fast-model", "output": 150, "duration_ms": 10000},
    # 慢模型：5 tok/s
    {"model": "slow-model", "output": 50, "duration_ms": 10000},
    # 缺 duration 的记录不参与
    {"model": "no-duration", "output": 100, "duration_ms": 0},
]


def _config(model_id, model_name, **extra):
    config = {
        "model_id": model_id,
        "model": model_name,
        "max_context_length": 200000,
        "priority": 0,
        "supports_tools": True,
    }
    config.update(extra)
    return config


# ---------------------------------------------------------------------------
# 速度分
# ---------------------------------------------------------------------------


def test_speed_kpis_indexes_by_model_name(monkeypatch, tmp_path):
    _patch_stats(monkeypatch, SPEED_RECORDS)
    kpis = sr._speed_kpis(str(tmp_path))
    assert kpis["fast-model"] == pytest.approx(32.5)
    assert kpis["slow-model"] == pytest.approx(5.0)
    assert "no-duration" not in kpis


def test_speed_kpis_without_stats_returns_empty(monkeypatch, tmp_path):
    def boom(*_a, **_k):
        raise RuntimeError("stats unavailable")

    monkeypatch.setattr("nbot.core.token_stats.get_token_stats_manager", boom)
    assert sr._speed_kpis(str(tmp_path)) == {}


def test_speed_score_reflects_measured_tps(monkeypatch, tmp_path):
    _patch_stats(monkeypatch, SPEED_RECORDS)
    tps_map = sr._speed_kpis(str(tmp_path))

    fast = sr._speed_score(tps_map, "cfg-fast", "fast-model")
    slow = sr._speed_score(tps_map, "cfg-slow", "slow-model")
    assert fast == pytest.approx(0.65)  # 32.5 / 50
    assert slow == pytest.approx(0.1)  # 5 / 50
    assert fast > slow

    # 没有实测数据 → 中性 0.5
    assert sr._speed_score(tps_map, "cfg-unknown", "unknown-model") == 0.5
    assert sr._speed_score({}, "cfg", "name") == 0.5


def test_speed_component_uses_model_name_not_id(monkeypatch, tmp_path):
    """回归：token_stats 存的是模型名，用 model_id 匹配会永远落空。"""
    _patch_stats(monkeypatch, SPEED_RECORDS)
    router = _router(tmp_path, enabled=True)
    scored = router.score_model(_config("cfg-fast", "fast-model"))
    assert scored["components"]["speed"] == pytest.approx(0.65)


# ---------------------------------------------------------------------------
# 路由
# ---------------------------------------------------------------------------


def test_route_orders_fast_model_first_and_reads_stats_once(monkeypatch, tmp_path):
    fake = _patch_stats(monkeypatch, SPEED_RECORDS)
    router = _router(tmp_path, enabled=True)

    analytics_calls = {"count": 0}
    original_analytics = router.analytics

    def counting_analytics():
        analytics_calls["count"] += 1
        return original_analytics()

    monkeypatch.setattr(router, "analytics", counting_analytics)

    ranked, snapshot = router.route_with_scores([
        _config("cfg-slow", "slow-model"),
        _config("cfg-fast", "fast-model"),
    ])
    assert [item["model"] for item in ranked] == ["fast-model", "slow-model"]
    assert len(snapshot) == 2
    # 两个模型也只读一次：速度统计 1 次（7d）+ 预算 1 次（today）
    assert fake.ranges.count("7d") == 1
    assert fake.ranges.count("today") == 1
    assert analytics_calls["count"] == 1


def test_route_disabled_returns_configs_untouched(tmp_path):
    router = _router(tmp_path, enabled=False)
    configs = [_config("cfg-a", "a"), _config("cfg-b", "b")]
    ranked, snapshot = router.route_with_scores(configs)
    assert ranked is configs
    assert snapshot == []


def test_route_ignores_configs_without_model_id(tmp_path):
    router = _router(tmp_path, enabled=True)
    configs = [{"model": "no-id"}]
    ranked, snapshot = router.route_with_scores(configs)
    assert ranked is configs
    assert snapshot == []


def test_budget_pressure_boosts_cost_weight(monkeypatch, tmp_path):
    _patch_stats(monkeypatch, [])
    router = _router(tmp_path, enabled=True, daily_budget=1.0)
    monkeypatch.setattr(
        router,
        "budget_status",
        lambda: {"budget": 1.0, "used": 1.0, "percent": 100.0,
                 "remaining": 0.0, "exhausted": True, "pressure": 1.0},
    )
    cheap = _config("cfg-cheap", "cheap", input_price=0.1, output_price=0.1)
    pricey = _config("cfg-pricey", "pricey", input_price=100.0, output_price=100.0)
    ranked, _snapshot = router.route_with_scores([pricey, cheap])
    assert ranked[0]["model"] == "cheap"


def test_ab_group_is_deterministic(tmp_path):
    assert sr.ab_group_for("", 50) == "control"
    first = sr.ab_group_for("session-abc", 50)
    assert first in ("control", "experiment")
    assert sr.ab_group_for("session-abc", 50) == first
    assert sr.ab_group_for("session-abc", 0) == "control"
    assert sr.ab_group_for("session-abc", 100) == "experiment"


# ---------------------------------------------------------------------------
# 统计与单例
# ---------------------------------------------------------------------------


def test_aggregate_analytics_reads_log_once_without_singleton(tmp_path, monkeypatch):
    router = _router(tmp_path)
    decision_id = router.record_decision(
        session_id="conv-1",
        purpose="chat",
        candidates=[{"model_id": "m1", "score": 0.8, "cost_per_1k": 0.001}],
        selected_id="m1",
        selected_name="模型一",
        reason="test",
    )
    router.backfill_decision(decision_id, success=True, duration_ms=1234)

    def _forbidden(*_a, **_k):
        raise AssertionError("_aggregate_analytics 不应走 get_smart_router 单例")

    monkeypatch.setattr(sr, "get_smart_router", _forbidden)

    analytics = sr._aggregate_analytics(str(tmp_path))
    assert analytics["total_decisions"] == 1
    assert analytics["models"]["m1"]["success_rate"] == 1.0
    assert analytics["models"]["m1"]["avg_score"] == pytest.approx(0.8)


def test_aggregate_analytics_tolerates_missing_file(tmp_path):
    analytics = sr._aggregate_analytics(str(tmp_path / "missing"))
    assert analytics == {"total_decisions": 0, "success_rate": 0.0, "models": {}}


def test_get_smart_router_caches_per_data_dir(tmp_path):
    sr.reset_smart_routers()
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    for folder, enabled in ((dir_a, True), (dir_b, False)):
        (folder / "settings.json").write_text(
            json.dumps({"smart_routing": {"enabled": enabled, "daily_budget": 5.0}}),
            encoding="utf-8",
        )

    router_a = sr.get_smart_router(str(dir_a))
    router_b = sr.get_smart_router(str(dir_b))
    assert router_a is not router_b
    assert router_a.settings_dict()["enabled"] is True
    assert router_b.settings_dict()["enabled"] is False
    # 同目录复用同一实例
    assert sr.get_smart_router(str(dir_a)) is router_a
    sr.reset_smart_routers()


def test_settings_persist_round_trip(tmp_path):
    router = _router(tmp_path)
    updated = router.update_settings({
        "enabled": True,
        "daily_budget": 12.5,
        "weights": {"cost": 0.5, "quality": "bad"},
        "ab_test": {"enabled": True, "ratio": 250},
    })
    assert updated["enabled"] is True
    assert updated["daily_budget"] == 12.5
    assert updated["weights"]["cost"] == 0.5
    assert updated["ab_test"]["ratio"] == 100  # 夹到 0~100

    payload = json.loads((tmp_path / "settings.json").read_text(encoding="utf-8"))
    assert payload["smart_routing"]["daily_budget"] == 12.5

    reloaded = sr.SmartRouter(str(tmp_path))
    assert reloaded.settings_dict()["enabled"] is True

"""Tests for the achievement system and smart routing (new features)."""

from nbot.achievements.definitions import get_achievement_defs
from nbot.achievements.manager import AchievementManager
from nbot.core.smart_routing import SmartRouter, ab_group_for


def test_achievement_manager_unlock_on_counter(tmp_path):
    mgr = AchievementManager(str(tmp_path))
    newly = mgr.record("messages", 1)
    assert "msg_first" in newly
    assert "msg_10" not in newly
    newly = mgr.record("messages", 9)
    assert "msg_10" in newly
    summary = mgr.summary()
    assert summary["unlocked"] >= 2


def test_achievement_manager_refresh_from_snapshot(tmp_path):
    mgr = AchievementManager(str(tmp_path))
    newly = mgr.refresh({"characters": 6, "worldbooks": 1, "affection_max": 95})
    assert "char_first" in newly
    assert "char_5" in newly
    assert "char_10" not in newly
    assert "wb_first" in newly
    assert "affection_90" in newly
    assert "affection_100" not in newly


def test_achievement_defs_unique_ids():
    ids = [d.id for d in get_achievement_defs()]
    assert len(ids) == len(set(ids))
    assert len(ids) >= 30


def test_ab_group_for_is_deterministic():
    a = ab_group_for("session-abc", 50)
    b = ab_group_for("session-abc", 50)
    assert a == b


def test_smart_router_disabled_returns_original_order(tmp_path):
    router = SmartRouter(str(tmp_path))
    configs = [
        {"model_id": "m1", "model": "model-a", "input_price": 10.0, "output_price": 40.0, "priority": 1},
        {"model_id": "m2", "model": "model-b", "input_price": 0.2, "output_price": 0.8, "priority": 2},
    ]
    assert router.route(configs, session_id="s1") == configs


def test_smart_router_ranks_cheaper_model_first(tmp_path):
    router = SmartRouter(str(tmp_path))
    router.update_settings({"enabled": True})
    configs = [
        {"model_id": "expensive", "model": "exp", "input_price": 100.0, "output_price": 400.0, "priority": 1, "max_context_length": 128000},
        {"model_id": "cheap", "model": "cheap", "input_price": 0.1, "output_price": 0.4, "priority": 2, "max_context_length": 32000},
    ]
    ranked = router.route(configs, session_id="s1")
    assert ranked[0]["model_id"] == "cheap"


def test_smart_router_decision_log_and_analytics(tmp_path):
    router = SmartRouter(str(tmp_path))
    router.update_settings({"enabled": True})
    configs = [
        {"model_id": "m1", "model": "model-a", "input_price": 1.0, "output_price": 4.0, "priority": 1, "max_context_length": 128000},
    ]
    ranked, candidates = router.route_with_scores(configs, session_id="s1")
    did = router.record_decision(
        session_id="s1", purpose="chat", candidates=candidates,
        selected_id="m1", selected_name="model-a", reason="smart_route",
    )
    router.backfill_decision(did, success=True, duration_ms=120)
    analytics = router.analytics()
    assert analytics["total_decisions"] == 1
    assert analytics["models"]["m1"]["success_rate"] == 1.0
    logs = router.decision_logs()
    assert logs[0]["id"] == did


def test_smart_router_persists(tmp_path):
    router = SmartRouter(str(tmp_path))
    router.update_settings({"enabled": True, "daily_budget": 50.0})
    router2 = SmartRouter(str(tmp_path))
    assert router2.settings_dict()["enabled"] is True
    assert router2.settings_dict()["daily_budget"] == 50.0


def test_budget_status_returns_shape(tmp_path):
    router = SmartRouter(str(tmp_path))
    router.update_settings({"enabled": True, "daily_budget": 100.0})
    status = router.budget_status()
    assert status["budget"] == 100.0
    assert "used" in status and "percent" in status and "pressure" in status

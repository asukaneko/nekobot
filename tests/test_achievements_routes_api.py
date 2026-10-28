"""Tests for the new web APIs (achievements / smart-routing / review settings)."""
from types import SimpleNamespace

from flask import Flask

from nbot.web.routes.achievements import register_achievement_routes
from nbot.web.routes.review import register_review_routes
from nbot.web.routes.smart_routing import register_smart_routing_routes


def _make_server(tmp_path):
    return SimpleNamespace(
        data_dir=str(tmp_path),
        settings={},
        sessions={},
        _save_data=lambda key: None,
        ai_models=[],
        active_models_by_purpose={},
        hook_runtime=None,
    )


def _make_app(server):
    app = Flask(__name__)
    register_achievement_routes(app, server)
    register_smart_routing_routes(app, server)
    register_review_routes(app, server)
    return app


def test_achievements_api_returns_list(tmp_path):
    server = _make_server(tmp_path)
    app = _make_app(server)
    client = app.test_client()

    resp = client.get("/api/achievements")
    assert resp.status_code == 200
    data = resp.get_json()
    assert len(data["achievements"]) >= 30
    assert data["summary"]["total"] >= 30


def test_achievements_refresh_unlocks_from_snapshot(tmp_path):
    server = _make_server(tmp_path)
    app = _make_app(server)
    client = app.test_client()

    # 造一个角色卡，让 char_first 解锁
    import json
    import os
    presets_path = os.path.join(str(tmp_path), "custom_personality_presets.json")
    with open(presets_path, "w", encoding="utf-8") as f:
        json.dump([{"id": "c1", "name": "test"}], f)

    resp = client.post("/api/achievements/refresh")
    data = resp.get_json()
    assert data["ok"] is True
    assert "char_first" in data["newly_unlocked"]


def test_smart_routing_settings_and_budget(tmp_path):
    server = _make_server(tmp_path)
    app = _make_app(server)
    client = app.test_client()

    resp = client.post("/api/smart-routing/settings", json={"enabled": True, "daily_budget": 100.0})
    assert resp.status_code == 200
    assert resp.get_json()["settings"]["enabled"] is True

    resp = client.get("/api/smart-routing/settings")
    assert resp.get_json()["settings"]["daily_budget"] == 100.0

    resp = client.get("/api/smart-routing/budget")
    assert resp.status_code == 200
    assert "budget" in resp.get_json()

    resp = client.get("/api/smart-routing/analytics")
    assert resp.status_code == 200

    resp = client.get("/api/smart-routing/logs")
    assert resp.status_code == 200


def test_review_settings_api(tmp_path):
    server = _make_server(tmp_path)
    app = _make_app(server)
    client = app.test_client()

    resp = client.get("/api/review/settings")
    assert resp.status_code == 200
    assert resp.get_json()["mode"] in ("rule", "llm")

    resp = client.post("/api/review/settings", json={"mode": "llm"})
    assert resp.status_code == 200
    assert resp.get_json()["mode"] == "llm"

    resp = client.post("/api/review/settings", json={"mode": "invalid"})
    assert resp.status_code == 400

"""世界事件分页（event_limit / before_sequence）与角色状态骨架。"""

import tempfile

from flask import Flask

from nbot.web.routes.worlds import register_world_routes
from nbot.world import WorldStore


def _world_with_events(tmpdir, count=5):
    store = WorldStore(tmpdir)
    bundle = store.create_world({"name": "分页世界"})
    world_id = bundle["world"]["world_id"]
    node_id = bundle["current_node"]["node_id"]
    for index in range(count):
        store.add_event(world_id, node_id, "world_event", f"第 {index} 件事")
    return store, world_id, node_id


def _client(tmpdir):
    app = Flask(__name__)
    server = type("Server", (), {"data_dir": tmpdir, "world_store": None})()
    register_world_routes(app, server)
    return app.test_client()


def test_bundle_returns_recent_events_with_limit():
    with tempfile.TemporaryDirectory() as tmpdir:
        _world_with_events(tmpdir)
        client = _client(tmpdir)
        world_id = client.get("/api/worlds").get_json()["worlds"][0]["world_id"]

        data = client.get(f"/api/worlds/{world_id}?event_limit=2").get_json()

        assert len(data["events"]) == 2
        assert [item["sequence"] for item in data["events"]] == [3, 4]
        assert data["event_total"] == 5
        assert data["has_more"] is True


def test_bundle_supports_sequence_cursor():
    with tempfile.TemporaryDirectory() as tmpdir:
        _world_with_events(tmpdir)
        client = _client(tmpdir)
        world_id = client.get("/api/worlds").get_json()["worlds"][0]["world_id"]

        data = client.get(
            f"/api/worlds/{world_id}?event_limit=2&before_sequence=3"
        ).get_json()

        assert [item["sequence"] for item in data["events"]] == [1, 2]
        assert data["event_total"] == 5
        assert data["has_more"] is True


def test_bundle_without_params_returns_all_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        _world_with_events(tmpdir)
        client = _client(tmpdir)
        world_id = client.get("/api/worlds").get_json()["worlds"][0]["world_id"]

        data = client.get(f"/api/worlds/{world_id}").get_json()

        assert len(data["events"]) == 5
        assert data["has_more"] is False


def test_invalid_paging_param_is_ignored():
    with tempfile.TemporaryDirectory() as tmpdir:
        _world_with_events(tmpdir)
        client = _client(tmpdir)
        world_id = client.get("/api/worlds").get_json()["worlds"][0]["world_id"]

        data = client.get(f"/api/worlds/{world_id}?event_limit=abc").get_json()

        assert len(data["events"]) == 5


def test_upsert_character_creates_state_skeleton():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        bundle = store.create_world({"name": "角色骨架世界"})
        world_id = bundle["world"]["world_id"]

        character = store.upsert_character(world_id, {"name": "露娜", "role": "守夜人"})
        reloaded = store.get_world_bundle(world_id)
        bucket = reloaded["current_node"]["state"]["characters"]

        assert len(bucket) == 1
        entry = bucket[character["character_id"]]
        assert entry["name"] == "露娜"
        assert entry["memory"] == []
        assert entry["mood"] == ""
        assert entry["affection"] == 50
        assert entry["trust"] == 50

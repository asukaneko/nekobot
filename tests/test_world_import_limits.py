"""世界导入的存储侧限制与文本清洗（安全审查 #13 / #15）。"""

import tempfile

from flask import Flask

from nbot.web.routes.worlds import register_world_routes
from nbot.world import WorldStore
from nbot.world.store import (
    MAX_IMPORT_CHARACTERS,
    MAX_IMPORT_EVENTS,
    MAX_IMPORT_NODES,
    MAX_NAME_CHARS,
    MAX_TEXT_CHARS,
    clean_text,
)


def _nodes(count):
    return [{"node_id": f"n{i}", "title": f"节点{i}"} for i in range(count)]


def _import_payload(nodes=None, events=None, characters=None, assets=None):
    return {
        "world": {"name": "限制测试"},
        "nodes": nodes or [{"node_id": "n0", "title": "主线"}],
        "events": events or [],
        "characters": characters or [],
        "assets": assets or [],
    }


def test_import_rejects_too_many_nodes():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        try:
            store.import_world(_import_payload(nodes=_nodes(MAX_IMPORT_NODES + 1)))
        except ValueError as exc:
            assert "节点" in str(exc) and str(MAX_IMPORT_NODES) in str(exc)
        else:  # pragma: no cover
            raise AssertionError("超限节点数应当被拒绝")


def test_import_rejects_too_many_events():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        events = [{"node_id": "n0", "content": "x"} for _ in range(MAX_IMPORT_EVENTS + 1)]
        try:
            store.import_world(_import_payload(events=events))
        except ValueError as exc:
            assert "事件" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("超限事件数应当被拒绝")


def test_import_rejects_too_many_characters():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        characters = [{"name": f"角色{i}"} for i in range(MAX_IMPORT_CHARACTERS + 1)]
        try:
            store.import_world(_import_payload(characters=characters))
        except ValueError as exc:
            assert "角色" in str(exc)
        else:  # pragma: no cover
            raise AssertionError("超限角色数应当被拒绝")


def test_import_route_returns_400_with_reason():
    with tempfile.TemporaryDirectory() as tmpdir:
        app = Flask(__name__)
        server = type("Server", (), {"data_dir": tmpdir, "world_store": None})()
        register_world_routes(app, server)
        client = app.test_client()

        response = client.post(
            "/api/worlds/import", json=_import_payload(nodes=_nodes(MAX_IMPORT_NODES + 1))
        )
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["success"] is False
        assert "上限" in payload["error"]


def test_import_sanitizes_control_chars_and_truncates():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        payload = _import_payload(
            nodes=[
                {
                    "node_id": "n0",
                    "title": "主线\x00\x07标题",
                    "summary": "长" * (MAX_TEXT_CHARS + 500),
                }
            ],
            events=[{"node_id": "n0", "type": "world_event", "content": "事件\x1b正文"}],
        )
        payload["world"]["description"] = "描述\x00内容"
        bundle = store.import_world(payload)

        assert bundle["world"]["description"] == "描述内容"
        node = bundle["current_node"]
        assert node["title"] == "主线标题"
        assert len(node["summary"]) == MAX_TEXT_CHARS
        assert bundle["events"][0]["content"] == "事件正文"


def test_import_normal_payload_still_works():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        bundle = store.import_world(
            _import_payload(
                nodes=[{"node_id": "n0", "title": "主线"}, {"node_id": "n1", "title": "支线"}],
                events=[{"node_id": "n0", "content": "一件事"}],
            )
        )
        assert bundle["counts"]["node_count"] == 2
        assert bundle["counts"]["event_count"] == 1
        assert bundle["world"]["name"] == "限制测试"


def test_clean_text_strips_controls_and_truncates():
    assert clean_text("a\x00b\x07c") == "abc"
    assert clean_text("x" * (MAX_NAME_CHARS + 10), MAX_NAME_CHARS) == "x" * MAX_NAME_CHARS
    assert clean_text(None) == ""
    assert clean_text(42) == "42"
    assert clean_text("tab\tkeep\nnewline") == "tab\tkeep\nnewline"


def test_create_and_update_also_sanitize():
    with tempfile.TemporaryDirectory() as tmpdir:
        store = WorldStore(tmpdir)
        bundle = store.create_world({"name": "世\x00界", "description": "描\x1f述"})
        world_id = bundle["world"]["world_id"]
        assert bundle["world"]["name"] == "世界"
        assert bundle["world"]["description"] == "描述"

        updated = store.update_world(world_id, {"description": "新\x00描述"})
        assert updated["world"]["description"] == "新描述"

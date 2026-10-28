"""状态快照历史（时间线 / 差分 / 恢复）测试"""

import os

import pytest
from flask import Flask

from nbot.character.models import CharacterState, RelationshipState
from nbot.character.state_history import StateHistoryStore, snapshot_diff
from nbot.web.routes.characters import register_character_routes


def _state(mood="开心", energy=70, mood_intensity=0.6):
    return CharacterState(
        character_id="小春",
        scope_id="qq:group:123",
        mood=mood,
        energy=energy,
        mood_intensity=mood_intensity,
    )


def _rel(affection=50, trust=50):
    return RelationshipState(
        character_id="小春",
        target_id="qq:user:42",
        affection=affection,
        trust=trust,
    )


def test_record_and_list(tmp_path):
    store = StateHistoryStore(str(tmp_path))
    store.record("小春", "qq:group:123", _state(), _rel(), source="turn")
    store.record("小春", "qq:group:123", _state(energy=60), _rel(affection=55), source="turn")

    snaps = store.list_snapshots("小春", "qq:group:123")
    assert len(snaps) == 2
    assert snaps[0]["energy"] == 70
    assert snaps[1]["affection"] == 55
    assert all(item["ts"] for item in snaps)


def test_record_skips_duplicates(tmp_path):
    store = StateHistoryStore(str(tmp_path))
    store.record("小春", "s1", _state(), _rel(), source="turn")
    store.record("小春", "s1", _state(), _rel(), source="turn")
    assert len(store.list_snapshots("小春", "s1")) == 1

    store.record("小春", "s1", _state(), _rel(), source="turn", force=True)
    assert len(store.list_snapshots("小春", "s1")) == 2


def test_record_ignores_missing_ids(tmp_path):
    store = StateHistoryStore(str(tmp_path))
    assert store.record("", "s1", _state(), _rel()) is None
    assert store.record("小春", "", _state(), _rel()) is None
    assert store.record("小春", "s1", None, None) is None


def test_cap_keeps_latest(tmp_path):
    store = StateHistoryStore(str(tmp_path), max_snapshots=12)
    for index in range(20):
        store.record("小春", "s1", _state(energy=index), _rel(), source="turn", force=True)
    snaps = store.list_snapshots("小春", "s1", limit=0)
    assert len(snaps) == 12
    assert snaps[-1]["energy"] == 19


def test_list_scopes_and_trend(tmp_path):
    store = StateHistoryStore(str(tmp_path))
    store.record("小春", "qq:group:1", _state(), _rel(), source="turn")
    store.record("小春", "qq:private:2", _state(), _rel(), source="turn")

    scopes = store.list_scopes("小春")
    assert {item["scope_id"] for item in scopes} == {"qq:group:1", "qq:private:2"}
    assert all(item["count"] == 1 for item in scopes)

    trend = store.trend("小春", "qq:group:1", limit=10, fields=["affection", "energy"])
    assert trend["fields"] == ["affection", "energy"]
    assert trend["count"] == 1
    assert trend["points"][0]["affection"] == 50


def test_snapshot_diff():
    before = {"mood": "开心", "energy": 70, "affection": 50, "trust": 50}
    after = {"mood": "害羞", "energy": 65, "affection": 53, "trust": 50}
    changes = snapshot_diff(before, after)
    fields = {item["field"]: item for item in changes}
    assert set(fields) == {"mood", "energy", "affection"}
    assert fields["mood"]["before"] == "开心"
    assert fields["affection"]["delta"] == 3
    assert fields["energy"]["delta"] == -5


def test_clear(tmp_path):
    store = StateHistoryStore(str(tmp_path))
    store.record("小春", "s1", _state(), _rel())
    assert store.clear("小春", "s1") is True
    assert store.list_snapshots("小春", "s1") == []


class _StubServer:
    def __init__(self, base_dir):
        self.base_dir = str(base_dir)


@pytest.fixture()
def client(tmp_path):
    app = Flask(__name__)
    register_character_routes(app, _StubServer(tmp_path))
    app.config["TESTING"] = True
    with app.test_client() as test_client:
        yield test_client


def test_history_routes_roundtrip(client, tmp_path):
    scope = "qq:group:123"
    res = client.put(
        f"/api/characters/小春/state?scope_id={scope}",
        json={"mood": "害羞", "energy": 66, "mood_intensity": 0.7},
    )
    assert res.status_code == 200
    assert res.get_json()["state"]["mood"] == "害羞"

    res = client.get(f"/api/characters/小春/state/history?scope_id={scope}")
    body = res.get_json()
    assert body["success"] is True
    assert body["count"] == 1
    assert body["snapshots"][0]["source"] == "manual"
    assert body["snapshots"][0]["mood"] == "害羞"

    res = client.get("/api/characters/小春/state/history/scopes")
    scopes = res.get_json()["scopes"]
    assert scopes and scopes[0]["scope_id"] == scope

    res = client.get(f"/api/characters/小春/state/trend?scope_id={scope}")
    trend = res.get_json()
    assert trend["success"] is True
    assert trend["points"][0]["energy"] == 66


def test_restore_route(client):
    scope = "qq:group:123"
    client.put(f"/api/characters/小春/state?scope_id={scope}", json={"mood": "开心", "energy": 80})
    client.put(f"/api/characters/小春/state?scope_id={scope}", json={"mood": "生气", "energy": 40})

    res = client.post(
        "/api/characters/小春/state/restore",
        json={"scope_id": scope, "index": 0},
    )
    body = res.get_json()
    assert res.status_code == 200
    assert body["restored"]["state"]["mood"] == "开心"
    assert body["restored"]["state"]["energy"] == 80

    history = client.get(f"/api/characters/小春/state/history?scope_id={scope}").get_json()
    assert history["snapshots"][-1]["source"] == "restore"


def test_restore_route_errors(client):
    assert client.post("/api/characters/小春/state/restore", json={}).status_code == 400
    res = client.post(
        "/api/characters/小春/state/restore", json={"scope_id": "unknown:1"}
    )
    assert res.status_code == 404
    client.put("/api/characters/小春/state?scope_id=s1", json={"mood": "开心"})
    res = client.post("/api/characters/小春/state/restore", json={"scope_id": "s1", "index": 99})
    assert res.status_code == 400


def test_history_requires_scope(client):
    assert client.get("/api/characters/小春/state/history").status_code == 400
    assert client.get("/api/characters/小春/state/trend").status_code == 400

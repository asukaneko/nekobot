"""用户人设（persona）：归一化、存储、会话回退与 system prompt 注入。"""

import pytest
from flask import Flask

from nbot.core.ai_pipeline import AIPipeline
from nbot.core.personas import (
    MAX_PERSONAS,
    PersonaStore,
    build_persona_block,
    normalize_persona,
    session_persona,
)
from nbot.web.routes import personas as persona_routes


# ---------------------------------------------------------------------------
# 归一化与渲染
# ---------------------------------------------------------------------------


def test_normalize_persona_requires_content():
    assert normalize_persona(None) is None
    assert normalize_persona({}) is None
    assert normalize_persona({"name": "  ", "description": "  "}) is None


def test_normalize_persona_defaults_and_truncation():
    persona = normalize_persona({"description": "一个普通上班族"})
    assert persona["name"] == "我"
    assert persona["id"]
    assert persona["created_at"] and persona["updated_at"]

    long_persona = normalize_persona({"name": "名" * 100, "description": "设" * 5000})
    assert len(long_persona["name"]) == 40
    assert len(long_persona["description"]) == 2000


def test_normalize_persona_keeps_explicit_id():
    persona = normalize_persona({"id": "abc", "name": "阿强"})
    assert persona["id"] == "abc"
    assert normalize_persona({"name": "阿强"}, persona_id="xyz")["id"] == "xyz"


def test_build_persona_block_content():
    block = build_persona_block({"name": "旅行者", "pronouns": "他", "description": "常年在路上"})
    assert block.startswith("【用户设定】")
    assert "旅行者" in block
    assert "他" in block
    assert "常年在路上" in block
    assert "{{user}}" not in block
    assert build_persona_block({}) == ""
    assert build_persona_block(None) == ""


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _store(tmp_path) -> PersonaStore:
    return PersonaStore(base_dir=str(tmp_path))


def test_store_save_list_and_default(tmp_path):
    store = _store(tmp_path)
    assert store.list() == []
    first = store.save({"name": "旅行者", "description": "到处跑"})
    second = store.save({"name": "学者", "description": "爱读书"})
    assert [p["name"] for p in store.list()] == ["旅行者", "学者"]
    assert store.default_id() == first["id"]
    assert store.default()["id"] == first["id"]
    assert store.set_default(second["id"]) is True
    assert store.default()["id"] == second["id"]


def test_store_update_keeps_created_at(tmp_path):
    store = _store(tmp_path)
    created = store.save({"name": "旅行者"})
    store.save({"id": created["id"], "name": "旅行者二号"})
    updated = store.get(created["id"])
    assert updated["name"] == "旅行者二号"
    assert updated["created_at"] == created["created_at"]


def test_store_delete_moves_default(tmp_path):
    store = _store(tmp_path)
    first = store.save({"name": "A"})
    second = store.save({"name": "B"})
    assert store.delete(first["id"]) is True
    assert store.delete(first["id"]) is False
    assert store.default()["id"] == second["id"]
    assert store.delete(second["id"]) is True
    assert store.list() == []


def test_store_rejects_empty_and_respects_limit(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.save({"name": "", "description": ""})
    for index in range(MAX_PERSONAS):
        store.save({"name": f"P{index}"})
    with pytest.raises(ValueError):
        store.save({"name": "overflow"})


def test_store_import_merge_and_overwrite(tmp_path):
    store = _store(tmp_path)
    result = store.import_personas([
        {"name": "旅行者", "description": "到处跑"},
        {"name": "学者", "description": "爱读书"},
        {"name": ""},
    ])
    assert result == {"added": 2, "updated": 0, "skipped": 1}

    merge_again = store.import_personas([{"name": "旅行者", "description": "到处跑"}])
    assert merge_again == {"added": 0, "updated": 0, "skipped": 1}

    overwrite = store.import_personas(
        [{"name": "学者", "description": "换了设定"}], mode="overwrite"
    )
    assert overwrite["updated"] == 1
    assert store.list()[1]["description"] == "换了设定"


def test_session_persona_resolution(tmp_path):
    store = _store(tmp_path)
    first = store.save({"name": "旅行者"})
    second = store.save({"name": "学者"})
    store.set_default(second["id"])

    assert session_persona({"persona_id": first["id"]}, store)["name"] == "旅行者"
    assert session_persona({"persona_id": "missing"}, store)["name"] == "学者"
    assert session_persona({}, store)["name"] == "学者"
    assert session_persona(None, store) is None


# ---------------------------------------------------------------------------
# system prompt 注入
# ---------------------------------------------------------------------------


class _StubCtx:
    def __init__(self, metadata=None):
        self.metadata = dict(metadata or {})


class _StubPipeline:
    def _expand_prompt_macros(self, ctx, text):
        return text.replace("{{user}}", "旅行者")


def test_apply_user_persona_appends_to_system_message():
    messages = [
        {"role": "system", "content": "BASE"},
        {"role": "user", "content": "hi"},
    ]
    out = AIPipeline._apply_user_persona(
        _StubPipeline(),
        _StubCtx({"user_persona": {"name": "{{user}}", "description": "到处跑"}}),
        messages,
    )
    assert out[0]["role"] == "system"
    assert out[0]["content"].startswith("BASE")
    assert "【用户设定】" in out[0]["content"]
    assert "旅行者" in out[0]["content"]
    assert len(out) == 2
    assert messages[0]["content"] == "BASE"  # 原列表未被就地修改


def test_apply_user_persona_without_persona_is_noop():
    messages = [{"role": "system", "content": "BASE"}]
    out = AIPipeline._apply_user_persona(_StubPipeline(), _StubCtx(), messages)
    assert out is messages


def test_apply_user_persona_inserts_system_when_missing():
    out = AIPipeline._apply_user_persona(
        _StubPipeline(),
        _StubCtx({"user_persona": {"name": "旅行者"}}),
        [{"role": "user", "content": "hi"}],
    )
    assert out[0]["role"] == "system"
    assert "【用户设定】" in out[0]["content"]
    assert len(out) == 2


def test_apply_user_persona_records_debug_entry():
    ctx = _StubCtx({"user_persona": {"name": "旅行者"}})
    AIPipeline._apply_user_persona(_StubPipeline(), ctx, [{"role": "system", "content": "B"}])
    entries = ctx.metadata["prompt_stack_debug"]
    assert entries[0]["key"] == "user_persona"
    assert entries[0]["chars"] > 0


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------


class _StubServer:
    ai_models = []
    active_model_id = None

    def _save_data(self, name):
        return None


@pytest.fixture()
def persona_client(tmp_path, monkeypatch):
    store = PersonaStore(base_dir=str(tmp_path))
    monkeypatch.setattr(persona_routes, "get_persona_store", lambda *a, **k: store)
    app = Flask(__name__)
    persona_routes.register_persona_routes(app, _StubServer())
    return app.test_client(), store


def test_routes_crud_flow(persona_client):
    client, store = persona_client

    created = client.post("/api/personas", json={"name": "旅行者", "description": "到处跑"})
    assert created.status_code == 200
    payload = created.get_json()
    persona_id = payload["persona"]["id"]
    assert payload["default_id"] == persona_id
    assert payload["count"] == 1

    listed = client.get("/api/personas").get_json()
    assert listed["count"] == 1
    assert listed["limit"] == MAX_PERSONAS

    updated = client.put(f"/api/personas/{persona_id}", json={"name": "旅行者二号"})
    assert updated.status_code == 200
    assert updated.get_json()["persona"]["name"] == "旅行者二号"

    preview = client.get(f"/api/personas/{persona_id}/preview").get_json()
    assert "【用户设定】" in preview["prompt_block"]

    second = client.post("/api/personas", json={"name": "学者"}).get_json()["persona"]["id"]
    assert client.post(f"/api/personas/{second}/default").get_json()["default_id"] == second
    assert client.delete(f"/api/personas/{second}").status_code == 200
    assert store.default_id() == persona_id


def test_routes_errors(persona_client):
    client, _store = persona_client
    assert client.post("/api/personas", json={}).status_code == 400
    assert client.put("/api/personas/missing", json={"name": "x"}).status_code == 404
    assert client.delete("/api/personas/missing").status_code == 404
    assert client.get("/api/personas/missing/preview").status_code == 404
    assert client.post("/api/personas/import", json={"personas": "nope"}).status_code == 400


def test_routes_export_import(persona_client):
    client, store = persona_client
    client.post("/api/personas", json={"name": "旅行者", "description": "到处跑"})
    exported = client.get("/api/personas/export").get_json()
    assert exported["version"] == 1
    assert len(exported["personas"]) == 1

    imported = client.post(
        "/api/personas/import",
        json={"personas": [{"name": "学者", "description": "爱读书"}], "mode": "merge"},
    ).get_json()
    assert imported["added"] == 1
    assert imported["count"] == 2

"""Prompt Manager：preset 模型、酒馆互转、组装与会话绑定、路由。"""

import sys

import pytest
from flask import Flask

from nbot.core.ai_pipeline import AIPipeline
from nbot.core.prompt_manager import (
    MAX_DEPTH,
    MAX_PRESETS,
    PromptPresetStore,
    apply_preset_base,
    apply_preset_messages,
    build_preset_injection,
    normalize_preset,
    normalize_section,
    normalize_sections,
    parse_tavern_preset,
    preset_to_tavern,
    resolve_session_preset,
)
from nbot.web.routes import prompt_manager as preset_routes

# 注意：``nbot.core.__init__`` 里 ``prompt_manager`` 这个名字已被旧提示词实例占用，
# ``from nbot.core import prompt_manager`` 拿到的是实例，必须按子模块取。
pm = sys.modules["nbot.core.prompt_manager"]


TAVERN_SAMPLE = {
    "name": "我的酒馆预设",
    "temperature": 0.65,
    "top_p": 0.9,
    "prompts": [
        {
            "identifier": "main",
            "name": "Main Prompt",
            "system_prompt": True,
            "role": "system",
            "content": "你是 {{char}}，请保持角色。",
            "marker": False,
            "enabled": True,
            "injection_position": 0,
        },
        {
            "identifier": "worldInfoBefore",
            "name": "World Info (before)",
            "system_prompt": True,
            "role": "system",
            "content": "",
            "marker": True,
        },
        {
            "identifier": "charDescription",
            "name": "Char Description",
            "role": "system",
            "content": "",
            "marker": True,
        },
        {
            "identifier": "jailbreak",
            "name": "Jailbreak",
            "role": "system",
            "content": "更主动一点。",
            "injection_position": 1,
            "injection_depth": 3,
            "enabled": True,
        },
        {
            "identifier": "chatHistory",
            "name": "Chat History",
            "role": "system",
            "content": "",
            "marker": True,
        },
    ],
    "prompt_order": [
        {
            "character_id": 100000,
            "order": [
                {"identifier": "main", "enabled": True},
                {"identifier": "worldInfoBefore", "enabled": True},
                {"identifier": "charDescription", "enabled": True},
                {"identifier": "jailbreak", "enabled": False},
                {"identifier": "chatHistory", "enabled": False},
            ],
        }
    ],
}


# ---------------------------------------------------------------------------
# 归一化
# ---------------------------------------------------------------------------


def test_normalize_section_drops_empty_non_marker():
    assert normalize_section(None) is None
    assert normalize_section({}) is None
    assert normalize_section({"content": "   "}) is None
    assert normalize_section({"content": "", "marker": True})["marker"] is True


def test_normalize_section_defaults_and_clamps():
    section = normalize_section({"content": "  hi  "})
    assert section["role"] == "system"
    assert section["position"] == "end"
    assert section["enabled"] is True
    assert section["content"] == "hi"
    assert section["name"]

    deep = normalize_section({
        "content": "x",
        "role": "USER",
        "position": "DEPTH",
        "depth": 999,
    })
    assert deep["role"] == "user"
    assert deep["position"] == "depth"
    assert deep["depth"] == MAX_DEPTH

    fallback = normalize_section({"content": "x", "role": "tool", "position": "nowhere"})
    assert fallback["role"] == "system"
    assert fallback["position"] == "end"


def test_normalize_sections_sorts_and_dedupes_ids():
    sections = normalize_sections([
        {"id": "b", "content": "B", "order": 5},
        {"id": "a", "content": "A", "order": 1},
        {"id": "a", "content": "A2", "order": 3},
        {"content": "C", "order": 2},
    ])
    assert [s["content"] for s in sections] == ["A", "C", "A2", "B"]
    assert [s["order"] for s in sections] == [0, 1, 2, 3]
    assert len({s["id"] for s in sections}) == 4


def test_normalize_preset_shape():
    preset = normalize_preset({"name": " 预设 ", "sections": [{"content": "x"}]})
    assert preset["name"] == "预设"
    assert preset["source"] == "native"
    assert preset["id"] and preset["created_at"] and preset["updated_at"]
    assert preset["sections"][0]["content"] == "x"
    assert normalize_preset(None) is None
    assert normalize_preset({"name": "x"}, preset_id="fixed")["id"] == "fixed"


# ---------------------------------------------------------------------------
# 酒馆互转
# ---------------------------------------------------------------------------


def test_parse_tavern_preset_maps_order_and_depth():
    preset, warnings = parse_tavern_preset(TAVERN_SAMPLE)
    assert warnings == []
    sections = preset["sections"]
    assert [s["id"] for s in sections] == [
        "main",
        "worldInfoBefore",
        "charDescription",
        "jailbreak",
        "chatHistory",
    ]
    assert [s["order"] for s in sections] == [0, 1, 2, 3, 4]
    # prompt_order 里 jailbreak / chatHistory 被关掉
    assert [s["enabled"] for s in sections] == [True, True, True, False, False]
    jailbreak = sections[3]
    assert jailbreak["position"] == "depth"
    assert jailbreak["depth"] == 3
    assert preset["source"] == "tavern"
    assert preset["tavern"]["temperature"] == 0.65


def test_parse_tavern_preset_requires_prompts():
    with pytest.raises(ValueError):
        parse_tavern_preset({"name": "x"})
    with pytest.raises(ValueError):
        parse_tavern_preset([1, 2, 3])

    preset, warnings = parse_tavern_preset({"name": "空", "prompts": []})
    assert preset["sections"] == []
    assert warnings


def test_tavern_round_trip_keeps_sections_and_scalars():
    preset, _ = parse_tavern_preset(TAVERN_SAMPLE)
    exported = preset_to_tavern(preset)
    assert exported["name"] == "我的酒馆预设"
    assert exported["temperature"] == 0.65
    assert exported["top_p"] == 0.9
    assert [p["identifier"] for p in exported["prompts"]] == [
        "main",
        "worldInfoBefore",
        "charDescription",
        "jailbreak",
        "chatHistory",
    ]
    jailbreak = exported["prompts"][3]
    assert jailbreak["injection_position"] == 1
    assert jailbreak["injection_depth"] == 3
    assert exported["prompt_order"][0]["order"][3] == {
        "identifier": "jailbreak",
        "enabled": False,
    }

    again, _ = parse_tavern_preset(exported)
    assert [s["content"] for s in again["sections"]] == [
        s["content"] for s in preset["sections"]
    ]
    assert [s["position"] for s in again["sections"]] == [
        s["position"] for s in preset["sections"]
    ]


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------


def _preset(sections):
    return normalize_preset({"name": "t", "sections": sections})


def test_build_injection_splits_start_end_top_depth():
    preset = _preset([
        {"id": "s1", "content": "START", "position": "start", "order": 0},
        {"id": "s2", "content": "END", "position": "end", "order": 1},
        {"id": "s3", "content": "USER-TOP", "role": "user", "position": "end", "order": 2},
        {"id": "s4", "content": "DEEP", "position": "depth", "depth": 2, "order": 3},
        {"id": "s5", "content": "OFF", "enabled": False, "order": 4},
    ])
    injection = build_preset_injection(preset)
    assert injection["start"] == "START"
    assert injection["end"] == "END"
    assert [m["content"] for m in injection["top_messages"]] == ["USER-TOP"]
    assert [d["content"] for d in injection["depth"]] == ["DEEP"]
    assert len(injection["debug"]) == 4
    assert injection["empty"] is False
    assert injection["chars"] == len("STARTENDUSER-TOPDEEP")


def test_build_injection_expander_and_empty():
    preset = _preset([{"id": "a", "content": "你好 {{user}}"}])
    injection = build_preset_injection(preset, expander=lambda text: text.replace("{{user}}", "阿强"))
    assert "阿强" in injection["end"]
    assert build_preset_injection(None)["empty"] is True
    assert build_preset_injection(_preset([]))["empty"] is True
    assert build_preset_injection(_preset([{"content": " ", "marker": True}]))["empty"] is True


def test_apply_preset_base_orders_start_and_end():
    preset = _preset([
        {"id": "s", "content": "S", "position": "start", "order": 0},
        {"id": "e", "content": "E", "position": "end", "order": 1},
    ])
    assert apply_preset_base(preset, "BASE") == "S\n\nBASE\n\nE"
    assert apply_preset_base(preset, "") == "S\n\nE"
    assert apply_preset_base(None, "BASE") == "BASE"
    assert apply_preset_base(_preset([]), "BASE") == "BASE"


def test_apply_preset_messages_inserts_top_and_depth():
    preset = _preset([
        {"id": "u", "content": "TOP-USER", "role": "user", "position": "end", "order": 0},
        {"id": "d", "content": "DEEP", "position": "depth", "depth": 1, "order": 1},
    ])
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1"},
        {"role": "assistant", "content": "a1"},
    ]
    out = apply_preset_messages(preset, messages)
    assert [m["content"] for m in out] == ["SYS", "TOP-USER", "u1", "DEEP", "a1"]
    assert messages[1]["content"] == "u1"  # 原列表未被就地修改
    assert apply_preset_messages(None, messages) is messages


def test_apply_preset_messages_depth_zero_goes_to_end():
    preset = _preset([{"id": "d", "content": "TAIL", "position": "depth", "depth": 0}])
    out = apply_preset_messages(preset, [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "u1"},
    ])
    assert out[-1]["content"] == "TAIL"


def test_apply_preset_messages_multiple_depths_keep_order():
    preset = _preset([
        {"id": "a", "content": "D2", "position": "depth", "depth": 2, "order": 0},
        {"id": "b", "content": "D5", "position": "depth", "depth": 5, "order": 1},
    ])
    messages = [{"role": "system", "content": "SYS"}] + [
        {"role": "user" if index % 2 else "assistant", "content": f"m{index}"}
        for index in range(8)
    ]
    out = apply_preset_messages(preset, messages)
    assert len(out) == len(messages) + 2
    # depth=2 表示插在"距末尾第 2 条"之前，即 m6 之前；depth=5 落在 m2 之后
    assert out[-3]["content"] == "D2"
    assert out[4]["content"] == "D5"


# ---------------------------------------------------------------------------
# 存储
# ---------------------------------------------------------------------------


def _store(tmp_path):
    return PromptPresetStore(base_dir=str(tmp_path))


def test_store_crud_and_default(tmp_path):
    store = _store(tmp_path)
    assert store.list() == []
    first = store.save({"name": "A", "sections": [{"content": "a"}]})
    second = store.save({"name": "B", "sections": [{"content": "b"}]})
    assert [p["name"] for p in store.list()] == ["A", "B"]
    assert store.default_id() == first["id"]
    assert store.set_default(second["id"]) is True
    assert store.default()["id"] == second["id"]

    updated = store.save({"id": first["id"], "name": "A2", "sections": [{"content": "a"}]})
    assert updated["created_at"] == first["created_at"]
    assert store.get(first["id"])["name"] == "A2"
    assert store.get("missing") is None


def test_store_rejects_empty_name_and_limit(tmp_path):
    store = _store(tmp_path)
    with pytest.raises(ValueError):
        store.save({"name": "  "})
    with pytest.raises(ValueError):
        store.save("nope")
    for index in range(MAX_PRESETS):
        store.save({"name": f"P{index}"})
    with pytest.raises(ValueError):
        store.save({"name": "overflow"})


def test_store_delete_moves_default_and_clears_bindings(tmp_path):
    store = _store(tmp_path)
    first = store.save({"name": "A"})
    second = store.save({"name": "B"})
    assert store.bind_session("s1", first["id"]) is True
    assert store.bind_session("s2", second["id"]) is True
    assert store.delete(first["id"]) is True
    assert store.delete(first["id"]) is False
    assert store.default_id() == second["id"]
    assert store.bindings() == {"s2": second["id"]}
    assert store.delete(second["id"]) is True
    assert store.list() == [] and store.bindings() == {}


def test_store_binding_validates_preset(tmp_path):
    store = _store(tmp_path)
    preset = store.save({"name": "A"})
    assert store.bind_session("", preset["id"]) is False
    assert store.bind_session("s1", "missing") is False
    assert store.bound_preset_id("s1") == ""
    assert store.bind_session("s1", preset["id"]) is True
    assert store.bound_preset_id("s1") == preset["id"]
    assert store.bind_session("s1", "") is True
    assert store.bound_preset_id("s1") == ""


def test_store_reorder_and_ensure_seed(tmp_path):
    store = _store(tmp_path)
    preset = store.save({
        "name": "A",
        "sections": [
            {"id": "x", "content": "X", "order": 0},
            {"id": "y", "content": "Y", "order": 1},
            {"id": "z", "content": "Z", "order": 2},
        ],
    })
    reordered = store.reorder(preset["id"], ["z", "x"])
    assert [s["id"] for s in reordered["sections"]] == ["z", "x", "y"]
    assert [s["order"] for s in reordered["sections"]] == [0, 1, 2]
    assert store.reorder("missing", ["x"]) is None
    assert store.reorder(preset["id"], "nope") is None

    empty = _store(tmp_path / "seed")
    seeded = empty.ensure_seed()
    assert seeded["name"] == "默认 preset"
    assert empty.ensure_seed() is None
    assert len(empty.list()) == 1


def test_store_import_merge_and_overwrite(tmp_path):
    store = _store(tmp_path)
    first = store.import_preset(TAVERN_SAMPLE, mode="merge")
    assert first["added"] == 1 and first["skipped"] == 0
    again = store.import_preset(TAVERN_SAMPLE, mode="merge")
    assert again["added"] == 0 and again["skipped"] == 1

    overwrite = store.import_preset(TAVERN_SAMPLE, mode="overwrite")
    assert overwrite["updated"] == 1

    native = store.import_preset({
        "name": "原生",
        "sections": [{"content": "hello"}],
    })
    assert native["added"] == 1
    assert native["preset"]["source"] == "native"

    with pytest.raises(ValueError):
        store.import_preset({"name": "bad"})
    # 兼容 {"preset": {...}} 包装
    wrapped = store.import_preset({"preset": {"name": "包裹", "sections": [{"content": "x"}]}})
    assert wrapped["added"] == 1


def test_store_export_formats(tmp_path):
    store = _store(tmp_path)
    preset = store.save({"name": "A", "sections": [{"content": "x"}]})
    native = store.export_preset(preset["id"])
    assert native["version"] == 1
    assert native["sections"][0]["content"] == "x"
    tavern = store.export_preset(preset["id"], fmt="tavern")
    assert tavern["prompts"][0]["content"] == "x"
    assert store.export_preset("missing") is None


# ---------------------------------------------------------------------------
# 会话解析（零行为变化）
# ---------------------------------------------------------------------------


def test_resolve_session_preset_returns_none_when_unbound(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(pm, "_STORE", store)
    assert resolve_session_preset("s1") is None
    assert resolve_session_preset("") is None

    preset = store.save({"name": "A", "sections": [{"content": "x"}]})
    store.bind_session("s1", preset["id"])
    assert resolve_session_preset("s1")["id"] == preset["id"]
    assert resolve_session_preset("s2") is None


def test_resolve_session_preset_swallows_store_errors(monkeypatch):
    class _Broken:
        def bound_preset_id(self, session_id):
            raise RuntimeError("boom")

    monkeypatch.setattr(pm, "_STORE", _Broken())
    assert resolve_session_preset("s1") is None


class _StubCtx:
    def __init__(self, metadata=None):
        self.metadata = dict(metadata or {})


class _StubPipeline:
    def _expand_prompt_macros(self, ctx, text):
        return text.replace("{{user}}", "阿强")


def test_pipeline_apply_prompt_preset_noop_without_preset():
    messages = [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}]
    ctx = _StubCtx()
    assert AIPipeline._apply_prompt_preset(_StubPipeline(), ctx, None, messages) is messages
    assert "prompt_stack_debug" not in ctx.metadata


def test_pipeline_apply_prompt_preset_inserts_and_logs():
    preset = _preset([
        {"id": "u", "content": "你好 {{user}}", "role": "user", "position": "end"},
    ])
    ctx = _StubCtx()
    out = AIPipeline._apply_prompt_preset(
        _StubPipeline(),
        ctx,
        preset,
        [{"role": "system", "content": "SYS"}, {"role": "user", "content": "hi"}],
    )
    assert [m["content"] for m in out] == ["SYS", "你好 阿强", "hi"]
    assert ctx.metadata["prompt_stack_debug"][0]["key"] == "preset.u"


# ---------------------------------------------------------------------------
# HTTP 路由
# ---------------------------------------------------------------------------


class _StubServer:
    ai_models = []
    active_model_id = None

    def _save_data(self, name):
        return None


@pytest.fixture()
def preset_client(tmp_path, monkeypatch):
    store = _store(tmp_path)
    monkeypatch.setattr(pm, "_STORE", store)
    app = Flask(__name__)
    preset_routes.register_prompt_preset_routes(app, _StubServer())
    return app.test_client(), store


def test_routes_crud_flow(preset_client):
    client, store = preset_client

    listed = client.get("/api/prompt-presets").get_json()
    assert listed["success"] is True
    assert listed["count"] == 1  # ensure_seed 落了一个默认 preset
    assert listed["limit"] == MAX_PRESETS
    assert listed["schema"]["roles"] == ["system", "user", "assistant"]

    created = client.post("/api/prompt-presets", json={
        "name": "我的 preset",
        "sections": [{"content": "hello", "position": "start"}],
    })
    assert created.status_code == 200
    payload = created.get_json()
    preset_id = payload["preset"]["id"]
    assert payload["summary"]["sections"] == 1

    detail = client.get(f"/api/prompt-presets/{preset_id}").get_json()
    assert detail["preset"]["name"] == "我的 preset"

    updated = client.put(f"/api/prompt-presets/{preset_id}", json={
        "name": "改名",
        "sections": [{"content": "a"}, {"content": "b"}],
    })
    assert updated.status_code == 200
    assert updated.get_json()["summary"]["sections"] == 2

    reordered = client.post(
        f"/api/prompt-presets/{preset_id}/reorder",
        json={"order": [updated.get_json()["preset"]["sections"][1]["id"]]},
    )
    assert reordered.status_code == 200
    assert reordered.get_json()["preset"]["sections"][0]["content"] == "b"

    assert client.post(f"/api/prompt-presets/{preset_id}/default").get_json()["default_id"] == preset_id
    assert client.delete(f"/api/prompt-presets/{preset_id}").status_code == 200
    assert store.get(preset_id) is None


def test_routes_preview(preset_client):
    client, _store = preset_client
    preset_id = client.post("/api/prompt-presets", json={
        "name": "P",
        "sections": [
            {"id": "start", "content": "S", "position": "start", "order": 0},
            {"id": "end", "content": "E", "position": "end", "order": 1},
            {"id": "deep", "content": "D", "position": "depth", "depth": 1, "order": 2},
        ],
    }).get_json()["preset"]["id"]

    preview = client.post(
        f"/api/prompt-presets/{preset_id}/preview",
        json={"base_prompt": "CARD", "messages": [{"role": "user", "content": "u1"}]},
    ).get_json()
    assert preview["system_prompt"] == "S\n\nCARD\n\nE"
    assert preview["depth_insertions"][0]["content"] == "D"
    # depth 段落在预览里也会作为独立消息插入
    assert [m["role"] for m in preview["messages"]] == ["system", "system", "user"]
    assert preview["summary"]["has_depth_sections"] is True


def test_routes_preview_expands_macros_locally_and_keeps_same_depth_order(preset_client):
    client, _store = preset_client
    preset_id = client.post("/api/prompt-presets", json={
        "name": "ordered",
        "sections": [
            {"id": "counter", "content": "{{addvar::counter::1}}counter={{getvar::counter}}"},
            {"id": "a", "content": "A", "position": "depth", "depth": 1, "order": 1},
            {"id": "b", "content": "B", "position": "depth", "depth": 1, "order": 2},
        ],
    }).get_json()["preset"]["id"]
    for _ in range(2):
        preview = client.post(f"/api/prompt-presets/{preset_id}/preview", json={}).get_json()
        assert [item["content"] for item in preview["messages"]] == [
            "counter=1", "A", "B", "（示例）你好呀",
        ]


def test_routes_import_export_and_binding(preset_client):
    client, _store = preset_client

    imported = client.post("/api/prompt-presets/import", json=TAVERN_SAMPLE).get_json()
    assert imported["added"] == 1
    preset_id = imported["imported"]["id"]
    assert imported["count"] == 1

    exported = client.get(f"/api/prompt-presets/{preset_id}/export?format=tavern").get_json()
    assert exported["preset"]["temperature"] == 0.65
    assert exported["preset"]["prompts"][0]["identifier"] == "main"

    bound = client.put("/api/prompt-presets/session-binding", json={
        "session_id": "s1",
        "preset_id": preset_id,
    })
    assert bound.status_code == 200
    assert bound.get_json()["preset_id"] == preset_id

    queried = client.get("/api/prompt-presets/session-binding?session_id=s1").get_json()
    assert queried["preset_id"] == preset_id
    assert queried["preset"]["name"] == "我的酒馆预设"

    unbound = client.put("/api/prompt-presets/session-binding", json={
        "session_id": "s1",
        "preset_id": "",
    }).get_json()
    assert unbound["preset_id"] == ""


def test_routes_list_session_bindings(preset_client):
    client, _store = preset_client
    first = client.post("/api/prompt-presets", json={"name": "P1"}).get_json()["preset"]["id"]
    second = client.post("/api/prompt-presets", json={"name": "P2"}).get_json()["preset"]["id"]
    client.put("/api/prompt-presets/session-binding", json={"session_id": "s1", "preset_id": first})
    client.put("/api/prompt-presets/session-binding", json={"session_id": "s2", "preset_id": second})

    listed = client.get("/api/prompt-presets/session-bindings").get_json()
    assert listed["count"] == 2
    assert listed["bindings"] == {"s1": first, "s2": second}
    assert listed["preset_names"][first] == "P1"

    # 删除 preset 后悬空绑定被过滤
    client.delete(f"/api/prompt-presets/{second}")
    listed = client.get("/api/prompt-presets/session-bindings").get_json()
    assert listed["bindings"] == {"s1": first}


def test_routes_errors(preset_client):
    client, _store = preset_client
    assert client.post("/api/prompt-presets", json={"name": ""}).status_code == 400
    assert client.put("/api/prompt-presets/missing", json={"name": "x"}).status_code == 404
    assert client.delete("/api/prompt-presets/missing").status_code == 404
    assert client.get("/api/prompt-presets/missing").status_code == 404
    assert client.get("/api/prompt-presets/missing/preview").status_code == 404
    assert client.get("/api/prompt-presets/missing/export").status_code == 404
    assert client.post("/api/prompt-presets/missing/reorder", json={"order": []}).status_code == 404
    assert client.post("/api/prompt-presets/import", json={"name": "x"}).status_code == 400
    assert client.put("/api/prompt-presets/session-binding", json={}).status_code == 400
    assert client.put(
        "/api/prompt-presets/session-binding",
        json={"session_id": "s1", "preset_id": "missing"},
    ).status_code == 400

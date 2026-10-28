"""会话级模型覆盖：配置归一化与候选队列重排"""

from nbot.web.session_model import (
    describe_model_override,
    normalize_model_override,
    resolve_model_configs,
)

DEFAULTS = [
    {"id": "m1", "name": "默认模型", "model": "gpt-x", "purpose": "chat"},
    {"id": "m2", "name": "备用模型", "model": "claude-y", "purpose": "chat"},
]
ALL = DEFAULTS + [
    {"id": "m3", "name": "第三模型", "model": "qwen-z", "purpose": "chat"},
    {"id": "v1", "name": "视觉模型", "model": "vision-1", "purpose": "vision"},
]


def test_normalize_accepts_dict_and_string():
    assert normalize_model_override({"enabled": True, "model_id": "m3"}) == {
        "enabled": True,
        "model_id": "m3",
    }
    assert normalize_model_override("m3") == {"enabled": True, "model_id": "m3"}
    assert normalize_model_override({"model": "m3"}) == {"enabled": True, "model_id": "m3"}


def test_normalize_rejects_empty():
    assert normalize_model_override(None) == {}
    assert normalize_model_override({}) == {}
    assert normalize_model_override({"enabled": True}) == {}
    assert normalize_model_override("   ") == {}
    assert normalize_model_override(123) == {}


def test_normalize_caps_length():
    long_id = "x" * 500
    assert len(normalize_model_override({"model_id": long_id})["model_id"]) == 120


def test_resolve_without_override_returns_defaults():
    assert resolve_model_configs(None, DEFAULTS, ALL) == DEFAULTS
    assert resolve_model_configs({}, DEFAULTS, ALL) == DEFAULTS
    assert resolve_model_configs({"model_override": {"enabled": False, "model_id": "m3"}}, DEFAULTS, ALL) == DEFAULTS


def test_resolve_puts_override_first_and_keeps_fallback():
    session = {"model_override": {"enabled": True, "model_id": "m3"}}
    resolved = resolve_model_configs(session, DEFAULTS, ALL)
    assert [cfg["id"] for cfg in resolved] == ["m3", "m1", "m2"]


def test_resolve_without_fallback():
    session = {"model_override": {"enabled": True, "model_id": "m3"}}
    resolved = resolve_model_configs(session, DEFAULTS, ALL, keep_fallback=False)
    assert [cfg["id"] for cfg in resolved] == ["m3"]


def test_resolve_unknown_model_falls_back():
    session = {"model_override": {"enabled": True, "model_id": "deleted-model"}}
    assert resolve_model_configs(session, DEFAULTS, ALL) == DEFAULTS


def test_resolve_override_same_as_first_no_duplicate():
    session = {"model_override": {"enabled": True, "model_id": "m1"}}
    resolved = resolve_model_configs(session, DEFAULTS, ALL)
    assert [cfg["id"] for cfg in resolved] == ["m1", "m2"]


def test_resolve_handles_missing_all_configs():
    session = {"model_override": {"enabled": True, "model_id": "m3"}}
    assert resolve_model_configs(session, DEFAULTS, None) == DEFAULTS
    assert resolve_model_configs(session, DEFAULTS, []) == DEFAULTS


def test_resolve_ignores_non_dict_entries():
    session = {"model_override": {"enabled": True, "model_id": "m2"}}
    resolved = resolve_model_configs(session, [None, DEFAULTS[0], "bad"], ALL)
    assert [cfg["id"] for cfg in resolved] == ["m2", "m1"]


def test_describe():
    assert describe_model_override({}) == "默认模型队列"
    assert "m3" in describe_model_override({"model_override": {"model_id": "m3"}})

"""manifest 解析与设置归一化测试。"""

import json
import os

import pytest

from nbot.plugins import safety
from nbot.plugins.manifest import (
    ManifestError,
    load_manifest,
    manifest_summary,
    normalize_settings,
    parse_manifest,
)


def _base(**overrides):
    data = {"id": "daily-news", "name": "每日新闻", "version": "1.2.0"}
    data.update(overrides)
    return data


def test_parse_minimal_manifest():
    manifest = parse_manifest(_base())
    assert manifest.id == "daily-news"
    assert manifest.version == "1.2.0"
    assert manifest.api_version == "1"
    assert manifest.entry == "main.py"
    assert manifest.permissions == ()


@pytest.mark.parametrize(
    "plugin_id",
    ["Demo", "a", "1abc-not-bad!", "bad id", "x" * 33, "-leading"],
)
def test_invalid_plugin_id_rejected(plugin_id):
    with pytest.raises(ManifestError):
        parse_manifest(_base(id=plugin_id))


@pytest.mark.parametrize("version", ["1.0", "v1.0.0", "1.0.0.0", ""])
def test_invalid_version_rejected(version):
    with pytest.raises(ManifestError):
        parse_manifest(_base(version=version))


def test_unknown_permission_fails_closed():
    with pytest.raises(ManifestError):
        parse_manifest(_base(permissions=["root"]))


def test_api_version_mismatch_rejected():
    with pytest.raises(ManifestError):
        parse_manifest(_base(api_version="2"))


def test_ui_page_requires_permission():
    with pytest.raises(ManifestError):
        parse_manifest(_base(ui={"page": "web/index.html"}))
    manifest = parse_manifest(
        _base(ui={"page": "web/index.html"}, permissions=["ui.page"])
    )
    assert manifest.ui["page"] == "web/index.html"


def test_entry_escape_rejected():
    with pytest.raises(ManifestError):
        parse_manifest(_base(entry="../evil.py"))


def test_actions_require_tool_permission():
    actions = [{"name": "refresh", "description": "刷新"}]
    with pytest.raises(ManifestError):
        parse_manifest(_base(actions=actions))
    manifest = parse_manifest(_base(actions=actions, permissions=["tool.register"]))
    assert manifest.actions[0]["name"] == "refresh"


def test_settings_schema_defaults_and_types():
    manifest = parse_manifest(
        _base(
            settings_schema={
                "greeting": {"type": "string", "default": "hi"},
                "retries": {"type": "number", "default": 3},
                "enabled": {"type": "boolean", "default": True},
                "mode": {
                    "type": "select",
                    "options": [{"value": "a", "label": "A"}, {"value": "b"}],
                    "default": "a",
                },
            }
        )
    )
    assert normalize_settings(manifest, {}) == {
        "greeting": "hi",
        "retries": 3,
        "enabled": True,
        "mode": "a",
    }
    assert normalize_settings(manifest, {"retries": "5", "enabled": "false"}) == {
        "greeting": "hi",
        "retries": 5,
        "enabled": False,
        "mode": "a",
    }


def test_settings_reject_unknown_key_and_bad_value():
    manifest = parse_manifest(
        _base(settings_schema={"greeting": {"type": "string", "default": ""}})
    )
    with pytest.raises(ManifestError):
        normalize_settings(manifest, {"other": 1})
    with pytest.raises(ManifestError):
        normalize_settings(manifest, {"greeting": ["not", "a", "string"]})


def test_load_manifest_reads_file(tmp_path):
    (tmp_path / "plugin.json").write_text(
        json.dumps(_base()), encoding="utf-8"
    )
    manifest = load_manifest(str(tmp_path))
    assert manifest.plugin_dir == str(tmp_path)
    assert manifest.entry_path == os.path.join(str(tmp_path), "main.py")


def test_load_manifest_rejects_oversize(tmp_path):
    payload = _base(description="x" * (70 * 1024))
    (tmp_path / "plugin.json").write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(ManifestError):
        load_manifest(str(tmp_path))


def test_manifest_summary_contains_effective_permissions():
    manifest = parse_manifest(_base(permissions=["kv", "net:api.example.com"]))
    summary = manifest_summary(manifest, effective_permissions=["kv"])
    assert summary["declared_permissions"] == ["kv", "net:api.example.com"]
    assert summary["effective_permissions"] == ["kv"]
    assert summary["settings_schema"] == {}


def test_safety_resolve_inside_blocks_escape(tmp_path):
    base = tmp_path / "plugin"
    base.mkdir()
    assert safety.resolve_inside(str(base), "a/b.txt") == str(base / "a" / "b.txt")
    assert safety.resolve_inside(str(base), ".") == str(base)
    assert safety.resolve_inside(str(base), "") == str(base)
    for bad in ("../x", "a/../../b", "/etc/passwd", "C:/win"):
        with pytest.raises(safety.UnsafePathError):
            safety.resolve_inside(str(base), bad)


def test_safety_suffix_whitelist():
    assert safety.is_allowed_suffix("main.py")
    assert safety.is_allowed_suffix("web/index.html")
    assert safety.is_allowed_suffix("LICENSE")
    assert not safety.is_allowed_suffix("payload.exe")
    assert not safety.is_allowed_suffix(".gitignore")

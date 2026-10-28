"""插件运行时：安装、启停、重载、卸载、状态与安全校验测试。"""

import io
import json
import os
import zipfile

import pytest

from nbot.plugins.manifest import ManifestError
from nbot.plugins.permissions import PermissionFormatError
from nbot.plugins.registry import PluginRegistry
from nbot.plugins.runtime import (
    PluginInstallError,
    PluginLoadError,
    PluginNotFound,
    PluginRuntime,
)


class FakeToolApi:
    def __init__(self):
        self.tools = {}

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        if name in self.tools:
            return False
        self.tools[name] = {"source": source, "read_only": read_only}
        return True

    def unregister_external_tool(self, name, *, source):
        entry = self.tools.get(name)
        if entry is None or entry["source"] != source:
            return False
        del self.tools[name]
        return True

    def unregister_source(self, source):
        names = [name for name, entry in self.tools.items() if entry["source"] == source]
        for name in names:
            del self.tools[name]
        return len(names)


PLUGIN_MAIN = """\
calls = []

def register(ctx):
    calls.append(["register", ctx.plugin_id, dict(ctx.settings)])

def unregister(ctx):
    calls.append(["unregister", ctx.plugin_id])
"""


def build_zip(
    plugin_id="demo",
    version="1.0.0",
    main=PLUGIN_MAIN,
    manifest_extra=None,
    files=None,
    prefix="",
):
    manifest = {
        "id": plugin_id,
        "name": "Demo",
        "version": version,
        "entry": "main.py",
        "permissions": ["kv"],
        "settings_schema": {"greeting": {"type": "string", "default": "hi"}},
    }
    manifest.update(manifest_extra or {})
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{prefix}plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(f"{prefix}main.py", main)
        for name, content in (files or {}).items():
            archive.writestr(f"{prefix}{name}", content)
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolate_plugin_use_tool(monkeypatch):
    """避免测试把 plugin_use 注册进真实的外部工具表。"""
    from nbot.plugins import plugin_use

    class _NoopApi:
        def register_external_tool(self, *args, **kwargs):
            return True

        def unregister_external_tool(self, *args, **kwargs):
            return False

        def unregister_source(self, *args, **kwargs):
            return 0

    monkeypatch.setattr(plugin_use, "_tool_api", lambda: _NoopApi())


@pytest.fixture
def rt(tmp_path):
    return PluginRuntime(
        plugins_root=str(tmp_path / "plugins"),
        state_path=str(tmp_path / "plugins.json"),
        registry=PluginRegistry(tool_api=FakeToolApi()),
    )


def test_install_from_zip(rt, tmp_path):
    detail = rt.install_from_zip(build_zip())
    assert detail["id"] == "demo"
    assert detail["version"] == "1.0.0"
    assert detail["enabled"] is False
    assert detail["loaded"] is False
    assert detail["settings"] == {"greeting": "hi"}
    assert os.path.isfile(os.path.join(str(tmp_path / "plugins"), "demo", "plugin.json"))
    state = json.loads((tmp_path / "plugins.json").read_text(encoding="utf-8"))
    assert state["plugins"]["demo"]["version"] == "1.0.0"


def test_install_single_nested_folder(rt):
    detail = rt.install_from_zip(build_zip(prefix="demo-1.0.0/"))
    assert detail["id"] == "demo"


def test_install_multiple_candidates_rejected(rt):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("a/plugin.json", json.dumps({"id": "a", "name": "A", "version": "1.0.0"}))
        archive.writestr("b/plugin.json", json.dumps({"id": "b", "name": "B", "version": "1.0.0"}))
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(buffer.getvalue())


def test_install_duplicate_requires_overwrite(rt):
    rt.install_from_zip(build_zip())
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(build_zip(version="1.1.0"))
    detail = rt.install_from_zip(build_zip(version="1.1.0"), overwrite=True)
    assert detail["version"] == "1.1.0"


def test_install_overwrite_keeps_settings(rt):
    rt.install_from_zip(build_zip())
    rt.update_settings("demo", {"greeting": "hello"})
    detail = rt.install_from_zip(build_zip(version="2.0.0"), overwrite=True)
    assert detail["settings"] == {"greeting": "hello"}


def test_install_rejects_path_traversal(rt):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps({"id": "demo", "name": "D", "version": "1.0.0"}))
        archive.writestr("../evil.py", "print(1)")
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(buffer.getvalue())
    assert not os.path.exists("evil.py")


def test_install_rejects_disallowed_suffix(rt):
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(build_zip(files={"payload.exe": b"MZ"}))


def test_install_rejects_symlink_entry(rt):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps({"id": "demo", "name": "D", "version": "1.0.0"}))
        info = zipfile.ZipInfo("link.py")
        info.external_attr = (0o120777 << 16)
        archive.writestr(info, "target")
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(buffer.getvalue())


def test_install_rejects_unknown_permission(rt):
    with pytest.raises(PluginInstallError) as excinfo:
        rt.install_from_zip(build_zip(manifest_extra={"permissions": ["root"]}))
    assert "权限" in str(excinfo.value)


def test_install_rejects_broken_manifest(rt):
    with pytest.raises(PluginInstallError):
        rt.install_from_zip(
            build_zip(manifest_extra={"api_version": "9"})
        )


def test_enable_and_disable_lifecycle(rt):
    rt.install_from_zip(build_zip())
    detail = rt.enable("demo")
    assert detail["enabled"] is True
    assert detail["loaded"] is True

    module = rt._modules["demo"]
    assert module.calls == [["register", "demo", {"greeting": "hi"}]]

    detail = rt.disable("demo")
    assert detail["enabled"] is False
    assert detail["loaded"] is False
    assert module.calls[-1] == ["unregister", "demo"]
    assert "demo" not in rt._modules


def test_enable_failure_rolls_back(rt):
    broken = "def register(ctx):\n    raise RuntimeError('boom')\n"
    rt.install_from_zip(build_zip(main=broken))
    with pytest.raises(PluginLoadError) as excinfo:
        rt.enable("demo")
    assert "boom" in str(excinfo.value)
    assert "demo" not in rt._modules
    detail = rt.get_plugin("demo")
    assert detail["enabled"] is False
    assert "boom" in (detail["last_error"] or "")


def test_enable_missing_register_rejected(rt):
    rt.install_from_zip(build_zip(main="value = 1\n"))
    with pytest.raises(PluginLoadError):
        rt.enable("demo")


def test_reload_picks_up_new_code(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    new_main = "version_marker = 2\ncalls = []\n\ndef register(ctx):\n    calls.append(['register', 2])\n"
    with open(os.path.join(rt.plugins_root, "demo", "main.py"), "w", encoding="utf-8") as handle:
        handle.write(new_main)
    detail = rt.reload("demo")
    assert detail["loaded"] is True
    module = rt._modules["demo"]
    assert module.version_marker == 2
    assert module.calls == [["register", 2]]


def test_reload_failure_restores_old_module(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    with open(os.path.join(rt.plugins_root, "demo", "main.py"), "w", encoding="utf-8") as handle:
        handle.write("def register(ctx):\n    raise RuntimeError('bad reload')\n")
    with pytest.raises(PluginLoadError):
        rt.reload("demo")
    # 旧模块已回滚，插件仍在运行
    assert rt._modules["demo"].calls[0][0] == "register"


def test_reload_invalid_manifest_keeps_state_file(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    with open(os.path.join(rt.plugins_root, "demo", "plugin.json"), "w", encoding="utf-8") as handle:
        handle.write("{not json")
    with pytest.raises(PluginLoadError):
        rt.reload("demo")
    assert rt.get_plugin("demo")["enabled"] is False


def test_uninstall_removes_dir_and_state(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    result = rt.uninstall("demo")
    assert result["removed"] is True
    assert not os.path.isdir(os.path.join(rt.plugins_root, "demo"))
    with open(rt.state_path, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
    assert "demo" not in saved["plugins"]
    with pytest.raises(PluginNotFound):
        rt.get_plugin("demo")


def test_uninstall_keep_state(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    rt.uninstall("demo", keep_state=True)
    with open(rt.state_path, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["plugins"]["demo"]["enabled"] is False
    assert not os.path.isdir(os.path.join(rt.plugins_root, "demo"))


def test_uninstall_delete_data(rt):
    rt.install_from_zip(build_zip())
    data_dir = os.path.join(rt.plugins_root, ".data", "demo")
    os.makedirs(data_dir, exist_ok=True)
    with open(os.path.join(data_dir, "kv.json"), "w", encoding="utf-8") as handle:
        handle.write("{}")

    rt.uninstall("demo")
    assert os.path.isdir(data_dir)  # 默认保留插件数据

    rt.install_from_zip(build_zip())
    result = rt.uninstall("demo", delete_data=True)
    assert result["data_removed"] is True
    assert not os.path.isdir(data_dir)


def test_update_settings_and_validation(rt):
    rt.install_from_zip(build_zip())
    rt.enable("demo")
    detail = rt.update_settings("demo", {"greeting": "yo"})
    assert detail["settings"] == {"greeting": "yo"}
    assert rt._contexts["demo"].settings == {"greeting": "yo"}
    with pytest.raises(ManifestError):
        rt.update_settings("demo", {"unknown": 1})


def test_update_permissions_only_tightens(rt):
    rt.install_from_zip(build_zip(manifest_extra={"permissions": ["kv", "fs:plugin"]}))
    rt.enable("demo")
    detail = rt.update_permissions("demo", ["kv"])
    assert detail["effective_permissions"] == ["kv"]
    assert rt._contexts["demo"].guard.effective == ("kv",)
    with pytest.raises(PermissionFormatError):
        rt.update_permissions("demo", ["kv", "net:api.example.com"])
    detail = rt.update_permissions("demo", None)
    assert detail["effective_permissions"] == ["kv", "fs:plugin"]


def test_state_persists_and_bootstraps(rt, tmp_path):
    rt.install_from_zip(build_zip())
    rt.enable("demo")

    second = PluginRuntime(
        plugins_root=str(tmp_path / "plugins"),
        state_path=str(tmp_path / "plugins.json"),
        registry=PluginRegistry(tool_api=FakeToolApi()),
    )
    listing = second.list_plugins()
    ids = [item["id"] for item in listing["plugins"]]
    assert ids == ["demo"]
    assert second._modules["demo"].calls[0][0] == "register"


def test_invalid_manifest_dir_reports_error(rt, tmp_path):
    broken_dir = tmp_path / "plugins" / "broken"
    broken_dir.mkdir(parents=True)
    (broken_dir / "plugin.json").write_text(json.dumps({"id": "Broken"}), encoding="utf-8")
    listing = rt.list_plugins()
    assert listing["plugins"] == []
    assert listing["errors"][0]["id"] == "broken"


def test_missing_plugin_raises(rt):
    with pytest.raises(PluginNotFound):
        rt.get_plugin("nope")
    with pytest.raises(PluginNotFound):
        rt.enable("nope")


def test_detail_contains_permission_display(rt):
    rt.install_from_zip(build_zip(manifest_extra={"permissions": ["kv", "net:api.example.com"]}))
    detail = rt.get_plugin("demo")
    names = [item["name"] for item in detail["permissions"]]
    assert names == ["kv", "net:api.example.com"]
    assert detail["resources"] == {"tools": [], "events": []}

"""plugin_use 元工具测试：动态描述、注册/注销时机与四种 action。"""

import io
import json
import zipfile

import pytest

from nbot.plugins import plugin_use
from nbot.plugins.registry import PluginRegistry
from nbot.plugins.runtime import PluginRuntime


class FakeToolApi:
    def __init__(self):
        self.tools = {}
        self.calls = []

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        self.calls.append(("register", name, source, read_only))
        if name in self.tools:
            return False
        self.tools[name] = {"spec": spec, "handler": handler, "source": source}
        return True

    def unregister_external_tool(self, name, *, source):
        self.calls.append(("unregister", name, source))
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


PLUGIN_MAIN = """
def register(ctx):
    ctx.register_action("refresh", lambda args: {"action": "refresh", "arguments": args})
"""


def build_zip(plugin_id="demo"):
    manifest = {
        "id": plugin_id,
        "name": "Demo",
        "version": "1.0.0",
        "description": "演示插件",
        "entry": "main.py",
        "ui": {"page": "web/index.html"},
        "permissions": ["tool.register", "ui.page"],
        "actions": [{"name": "refresh", "description": "刷新"}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("main.py", PLUGIN_MAIN)
        archive.writestr("web/index.html", "<html></html>")
    return buffer.getvalue()


@pytest.fixture
def runtime(tmp_path):
    return PluginRuntime(
        plugins_root=str(tmp_path / "plugins"),
        state_path=str(tmp_path / "plugins.json"),
        registry=PluginRegistry(tool_api=FakeToolApi()),
    )


@pytest.fixture
def fake_api(monkeypatch):
    api = FakeToolApi()
    monkeypatch.setattr(plugin_use, "_tool_api", lambda: api)
    return api


def test_refresh_without_plugins_does_not_register(runtime, fake_api):
    assert plugin_use.refresh_plugin_use_tool(runtime) is False
    assert "plugin_use" not in fake_api.tools


def test_refresh_unregisters_when_no_plugin_enabled(runtime, fake_api):
    fake_api.tools["plugin_use"] = {"source": plugin_use.SOURCE, "spec": {}, "handler": None}
    assert plugin_use.refresh_plugin_use_tool(runtime) is False
    assert "plugin_use" not in fake_api.tools


def test_enable_registers_tool_with_dynamic_description(runtime, fake_api, monkeypatch):
    monkeypatch.setattr(plugin_use, "get_plugin_runtime", lambda: runtime)
    runtime.install_from_zip(build_zip())
    runtime.enable("demo")

    assert "plugin_use" in fake_api.tools
    spec = fake_api.tools["plugin_use"]["spec"]
    assert "demo" in spec["description"]
    assert "refresh" in spec["description"]
    assert spec["parameters"]["properties"]["plugin_id"]["enum"] == ["demo"]
    assert fake_api.tools["plugin_use"]["source"] == "plugin:core"


def test_disable_removes_tool(runtime, fake_api):
    runtime.install_from_zip(build_zip())
    runtime.enable("demo")
    assert "plugin_use" in fake_api.tools
    runtime.disable("demo")
    assert "plugin_use" not in fake_api.tools


def test_handler_list_and_describe(runtime, fake_api, monkeypatch):
    monkeypatch.setattr(plugin_use, "get_plugin_runtime", lambda: runtime)
    runtime.install_from_zip(build_zip())
    runtime.enable("demo")

    listed = plugin_use.handle_plugin_use({"action": "list"})
    assert listed["success"] is True
    assert "demo" in listed["content"]

    described = plugin_use.handle_plugin_use({"action": "describe", "plugin_id": "demo"})
    info = json.loads(described["content"])
    assert info["actions"] == ["refresh"]
    assert info["enabled"] is True


def test_handler_call_and_ui(runtime, fake_api, monkeypatch):
    monkeypatch.setattr(plugin_use, "get_plugin_runtime", lambda: runtime)
    runtime.install_from_zip(build_zip())
    runtime.enable("demo")

    called = plugin_use.handle_plugin_use(
        {
            "action": "call",
            "plugin_id": "demo",
            "params": {"action": "refresh", "arguments": {"x": 1}},
        }
    )
    assert json.loads(called["content"]) == {"action": "refresh", "arguments": {"x": 1}}

    ui = plugin_use.handle_plugin_use({"action": "ui", "plugin_id": "demo"})
    assert ui["content"] == "/api/plugins/demo/ui/web/index.html"


def test_handler_errors(runtime, fake_api, monkeypatch):
    monkeypatch.setattr(plugin_use, "get_plugin_runtime", lambda: runtime)
    runtime.install_from_zip(build_zip())

    assert plugin_use.handle_plugin_use({"action": "describe"})["success"] is False
    assert plugin_use.handle_plugin_use({"action": "describe", "plugin_id": "nope"})[
        "success"
    ] is False
    assert plugin_use.handle_plugin_use({"action": "nope", "plugin_id": "demo"})[
        "success"
    ] is False
    assert plugin_use.handle_plugin_use(
        {"action": "call", "plugin_id": "demo", "params": {}}
    )["success"] is False
    assert plugin_use.handle_plugin_use({"action": "ui", "plugin_id": "demo"})[
        "success"
    ] is False

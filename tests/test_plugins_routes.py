"""插件管理路由测试（Flask test client，不启动服务器）。"""

import io
import json
import os
import zipfile

import pytest
from flask import Flask

from nbot.plugins import runtime as runtime_module
from nbot.plugins.registry import PluginRegistry
from nbot.plugins.runtime import PluginRuntime
from nbot.web.routes.plugins import register_plugin_routes


class FakeToolApi:
    def __init__(self):
        self.tools = {}

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        self.tools[name] = {"source": source}
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


def build_zip(plugin_id="demo", version="1.0.0"):
    manifest = {
        "id": plugin_id,
        "name": "Demo",
        "version": version,
        "entry": "main.py",
        "permissions": ["kv", "fs:plugin"],
        "settings_schema": {"greeting": {"type": "string", "default": "hi"}},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr(
            "main.py",
            "calls = []\n\ndef register(ctx):\n    calls.append(ctx.plugin_id)\n",
        )
    return buffer.getvalue()


@pytest.fixture(autouse=True)
def _isolate_plugin_use_tool(monkeypatch):
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
def api(tmp_path, monkeypatch):
    runtime = PluginRuntime(
        plugins_root=str(tmp_path / "plugins"),
        state_path=str(tmp_path / "plugins.json"),
        registry=PluginRegistry(tool_api=FakeToolApi()),
    )
    monkeypatch.setattr(runtime_module, "_runtime", None)
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
    app = Flask(__name__)
    register_plugin_routes(app, server=None)
    app.config.update(TESTING=True)
    return app.test_client(), runtime


def _install(client, blob=None, **data):
    return client.post(
        "/api/plugins/install",
        data={"file": (io.BytesIO(blob or build_zip()), "demo.zip"), **data},
        content_type="multipart/form-data",
    )


def test_list_empty(api):
    client, _ = api
    response = client.get("/api/plugins")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["success"] is True
    assert payload["plugins"] == []


def test_install_enable_disable_flow(api):
    client, runtime = api
    response = _install(client)
    assert response.status_code == 200
    plugin = response.get_json()["plugin"]
    assert plugin["id"] == "demo"
    assert plugin["enabled"] is False

    response = client.post("/api/plugins/demo/enable")
    assert response.status_code == 200
    assert response.get_json()["plugin"]["loaded"] is True
    assert runtime._modules["demo"].calls == ["demo"]

    response = client.post("/api/plugins/demo/disable")
    assert response.status_code == 200
    assert response.get_json()["plugin"]["enabled"] is False


def test_install_duplicate_returns_400(api):
    client, _ = api
    _install(client)
    response = _install(client)
    assert response.status_code == 400
    assert response.get_json()["success"] is False


def test_install_requires_file(api):
    client, _ = api
    response = client.post("/api/plugins/install", data={}, content_type="multipart/form-data")
    assert response.status_code == 400


def test_install_bad_zip_returns_400(api):
    client, _ = api
    response = _install(client, blob=b"not a zip")
    assert response.status_code == 400


def test_get_unknown_plugin_returns_404(api):
    client, _ = api
    response = client.get("/api/plugins/nope")
    assert response.status_code == 404


def test_update_settings(api):
    client, _ = api
    _install(client)
    response = client.put("/api/plugins/demo/settings", json={"settings": {"greeting": "yo"}})
    assert response.status_code == 200
    assert response.get_json()["plugin"]["settings"] == {"greeting": "yo"}

    response = client.put("/api/plugins/demo/settings", json={"settings": {"bad": 1}})
    assert response.status_code == 400


def test_update_permissions(api):
    client, _ = api
    _install(client)
    response = client.put("/api/plugins/demo/permissions", json={"permissions": ["kv"]})
    assert response.status_code == 200
    assert response.get_json()["plugin"]["effective_permissions"] == ["kv"]

    response = client.put(
        "/api/plugins/demo/permissions", json={"permissions": ["net:api.example.com"]}
    )
    assert response.status_code == 400


def test_uninstall_with_keep_state(api):
    client, runtime = api
    _install(client)
    client.post("/api/plugins/demo/enable")
    response = client.delete("/api/plugins/demo?keep_state=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["removed"] is True
    assert payload["state_kept"] is True
    with open(runtime.state_path, "r", encoding="utf-8") as handle:
        saved = json.load(handle)
    assert saved["plugins"]["demo"]["enabled"] is False
    assert client.get("/api/plugins/demo").status_code == 404


def test_uninstall_delete_data(api):
    client, runtime = api
    _install(client)
    client.post("/api/plugins/demo/enable")
    os.makedirs(os.path.join(runtime.plugins_root, ".data", "demo"), exist_ok=True)
    response = client.delete("/api/plugins/demo?delete_data=1")
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["data_removed"] is True
    assert not os.path.isdir(os.path.join(runtime.plugins_root, ".data", "demo"))


def test_reload_endpoint(api):
    client, _ = api
    _install(client)
    client.post("/api/plugins/demo/enable")
    response = client.post("/api/plugins/demo/reload")
    assert response.status_code == 200
    assert response.get_json()["plugin"]["loaded"] is True

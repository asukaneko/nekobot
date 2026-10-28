"""插件页面与桥路由测试：静态资源、CSP、路径安全、桥白名单与限流。"""

import io
import json
import zipfile

import pytest
from flask import Flask

from nbot.plugins import runtime as runtime_module
from nbot.plugins.registry import PluginRegistry
from nbot.plugins.runtime import PluginRuntime
from nbot.web.plugin_host import BRIDGE_RATE_LIMIT, _reset_rate_limit
from nbot.web.routes.plugins import register_plugin_routes


class FakeToolApi:
    def __init__(self):
        self.tools = {}

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        if name in self.tools:
            return False
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


PLUGIN_MAIN = """
def register(ctx):
    def refresh(args):
        return {"action": "refresh", "arguments": args}

    ctx.register_action("refresh", refresh)
"""

INDEX_HTML = """<!doctype html>
<html><head><title>Demo</title></head><body><script src="./app.js"></script></body></html>
"""


def build_zip(plugin_id="demo", permissions=None):
    manifest = {
        "id": plugin_id,
        "name": "Demo",
        "version": "1.0.0",
        "entry": "main.py",
        "ui": {"page": "web/index.html", "title": "Demo"},
        "permissions": permissions
        if permissions is not None
        else ["ui.page", "kv", "tool.register"],
        "actions": [{"name": "refresh", "description": "刷新"}],
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("plugin.json", json.dumps(manifest, ensure_ascii=False))
        archive.writestr("main.py", PLUGIN_MAIN)
        archive.writestr("web/index.html", INDEX_HTML)
        archive.writestr("web/app.js", "console.log('demo');")
        archive.writestr("web/style.css", "body{color:#000;}")
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
def client(tmp_path, monkeypatch):
    _reset_rate_limit()
    runtime = PluginRuntime(
        plugins_root=str(tmp_path / "plugins"),
        state_path=str(tmp_path / "plugins.json"),
        registry=PluginRegistry(tool_api=FakeToolApi()),
    )
    monkeypatch.setattr(runtime_module, "_runtime", runtime)
    app = Flask(__name__)
    register_plugin_routes(app, server=None)
    app.config.update(TESTING=True)
    return app.test_client(), runtime


def _install_and_enable(client):
    client.post(
        "/api/plugins/install",
        data={"file": (io.BytesIO(build_zip()), "demo.zip")},
        content_type="multipart/form-data",
    )
    client.post("/api/plugins/demo/enable")


def test_page_served_with_csp(client):
    http, _ = client
    _install_and_enable(http)
    response = http.get("/api/plugins/demo/ui/", follow_redirects=True)
    assert response.status_code == 200
    assert b"<title>Demo</title>" in response.data
    csp = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in csp
    assert "script-src 'self'" in csp
    assert "connect-src 'none'" in csp
    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


def test_asset_served_and_python_blocked(client):
    http, _ = client
    _install_and_enable(http)
    script = http.get("/api/plugins/demo/ui/web/app.js")
    assert script.status_code == 200
    assert b"console.log" in script.data

    assert http.get("/api/plugins/demo/ui/main.py").status_code == 404
    assert http.get("/api/plugins/demo/ui/bootstrap.py").status_code == 404


def test_signed_asset_url_loads_relative_script_without_login_cookie(client):
    http, _ = client
    _install_and_enable(http)
    entry = http.get("/api/plugins/demo/ui/")
    assert entry.status_code == 302
    signed_page = entry.headers["Location"]
    assert signed_page.startswith("/plugin-assets/demo/")
    anonymous = http.application.test_client(use_cookies=False)
    script_path = signed_page.rsplit("/", 1)[0] + "/app.js"
    response = anonymous.get(script_path)
    assert response.status_code == 200
    assert b"console.log" in response.data
    assert response.headers["Access-Control-Allow-Origin"] == "*"
    assert anonymous.get(script_path.replace("/demo/", "/other/", 1)).status_code == 403
    assert anonymous.get("/plugin-assets/demo/invalid/web/app.js").status_code == 403


def test_signed_asset_url_respects_disable_and_expiration(client, monkeypatch):
    http, runtime = client
    _install_and_enable(http)
    location = http.get("/api/plugins/demo/ui/").headers["Location"]
    runtime.disable("demo")
    assert http.get(location).status_code == 404
    runtime.enable("demo")
    monkeypatch.setattr("nbot.web.plugin_host.UI_ASSET_TOKEN_MAX_AGE", -1)
    assert http.get(location).status_code == 403


def test_asset_traversal_blocked(client):
    http, _ = client
    _install_and_enable(http)
    response = http.get("/api/plugins/demo/ui/..%2Fmain.py")
    assert response.status_code == 404


def test_page_requires_enabled_plugin(client):
    http, _ = client
    http.post(
        "/api/plugins/install",
        data={"file": (io.BytesIO(build_zip()), "demo.zip")},
        content_type="multipart/form-data",
    )
    assert http.get("/api/plugins/demo/ui/").status_code == 404


def test_page_requires_ui_permission(client):
    http, runtime = client
    _install_and_enable(http)
    runtime.update_permissions("demo", [])
    assert http.get("/api/plugins/demo/ui/").status_code == 403


def test_bridge_rejects_unknown_method(client):
    http, _ = client
    _install_and_enable(http)
    response = http.post(
        "/api/plugins/demo/bridge",
        json={"id": 1, "method": "shell.exec", "params": {}},
    )
    assert response.status_code == 400
    assert response.get_json()["ok"] is False


def test_bridge_kv_roundtrip(client):
    http, _ = client
    _install_and_enable(http)
    saved = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "a", "method": "kv.set", "params": {"key": "k", "value": 5}},
    )
    assert saved.status_code == 200
    assert saved.get_json() == {"ok": True, "id": "a", "result": {"saved": True}}

    got = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "b", "method": "kv.get", "params": {"key": "k"}},
    )
    assert got.get_json()["result"] == 5


def test_bridge_tool_invoke(client):
    http, _ = client
    _install_and_enable(http)
    response = http.post(
        "/api/plugins/demo/bridge",
        json={
            "id": "c",
            "method": "tool.invoke",
            "params": {"action": "refresh", "arguments": {"q": 1}},
        },
    )
    assert response.status_code == 200
    assert response.get_json()["result"] == {
        "action": "refresh",
        "arguments": {"q": 1},
    }


def test_bridge_unknown_action_returns_400(client):
    http, _ = client
    _install_and_enable(http)
    response = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "d", "method": "tool.invoke", "params": {"action": "missing"}},
    )
    assert response.status_code == 400


def test_bridge_ui_resize(client):
    http, _ = client
    _install_and_enable(http)
    ok = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "r", "method": "ui.resize", "params": {"height": 640}},
    )
    assert ok.status_code == 200
    assert ok.get_json()["result"] == {"notified": True, "height": 640}

    too_small = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "r2", "method": "ui.resize", "params": {"height": 20}},
    )
    assert too_small.status_code == 400

    invalid = http.post(
        "/api/plugins/demo/bridge",
        json={"id": "r3", "method": "ui.resize", "params": {"height": "tall"}},
    )
    assert invalid.status_code == 400


def test_bridge_rate_limit(client):
    http, _ = client
    _install_and_enable(http)
    payload = {"id": 1, "method": "kv.list", "params": {}}
    for _ in range(BRIDGE_RATE_LIMIT):
        assert http.post("/api/plugins/demo/bridge", json=payload).status_code == 200
    blocked = http.post("/api/plugins/demo/bridge", json=payload)
    assert blocked.status_code == 429


def test_bridge_origin_mismatch(client):
    http, _ = client
    _install_and_enable(http)
    response = http.post(
        "/api/plugins/demo/bridge",
        json={"id": 1, "method": "kv.list", "params": {}},
        headers={"Origin": "https://evil.example.com"},
    )
    assert response.status_code == 403


def test_bridge_requires_loaded_plugin(client):
    http, _ = client
    http.post(
        "/api/plugins/install",
        data={"file": (io.BytesIO(build_zip()), "demo.zip")},
        content_type="multipart/form-data",
    )
    response = http.post(
        "/api/plugins/demo/bridge",
        json={"id": 1, "method": "kv.list", "params": {}},
    )
    assert response.status_code == 404

"""宿主 API 测试：kv / fs / workspace / http / events / tools / session / message / log。"""

import json
import urllib.error
from email.message import Message

import pytest

from nbot.plugins import host as host_module
from nbot.plugins import safety
from nbot.plugins.host import (
    KV_MAX_BYTES,
    HostAPIError,
    HostServices,
    PluginHost,
)
from nbot.plugins.manifest import parse_manifest
from nbot.plugins.permissions import PermissionDenied, PermissionGuard, PluginSuspended
from nbot.plugins.registry import PluginRegistry


class FakeToolApi:
    def __init__(self):
        self.tools = {}

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        if name in self.tools:
            return False
        self.tools[name] = {"source": source, "handler": handler, "spec": spec}
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


def make_host(tmp_path, permissions=("kv", "fs:plugin"), services=None, override=None):
    plugin_dir = tmp_path / "plugin"
    plugin_dir.mkdir(parents=True, exist_ok=True)
    (plugin_dir / "main.py").write_text("", encoding="utf-8")
    manifest = parse_manifest(
        {
            "id": "demo",
            "name": "Demo",
            "version": "1.0.0",
            "permissions": list(permissions),
        },
        plugin_dir=str(plugin_dir),
    )
    guard = PermissionGuard("demo", manifest.permissions, override)
    registry = PluginRegistry(tool_api=FakeToolApi())
    host = PluginHost(
        plugin_id="demo",
        manifest=manifest,
        settings={},
        guard=guard,
        registry=registry,
        plugin_dir=str(plugin_dir),
        data_dir=str(tmp_path / "data"),
        services=services,
    )
    return host, plugin_dir, registry


def test_kv_roundtrip_and_persistence(tmp_path):
    host, _, _ = make_host(tmp_path)
    host.api.kv.set("greeting", {"text": "hi"})
    assert host.api.kv.get("greeting") == {"text": "hi"}
    assert host.api.kv.list() == ["greeting"]

    second, _, _ = make_host(tmp_path)
    assert second.api.kv.get("greeting") == {"text": "hi"}
    assert second.api.kv.delete("greeting") is True
    assert second.api.kv.get("greeting", "fallback") == "fallback"


def test_kv_rejects_oversize_value(tmp_path):
    host, _, _ = make_host(tmp_path)
    with pytest.raises(HostAPIError):
        host.api.kv.set("blob", "x" * (KV_MAX_BYTES + 10))


def test_kv_requires_permission(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("fs:plugin",))
    with pytest.raises(PermissionDenied):
        host.api.kv.get("anything")
    assert host.guard.violations == 1


def test_kv_suspends_after_repeated_violations(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=())
    for _ in range(4):
        with pytest.raises(PermissionDenied):
            host.api.kv.set("k", 1)
    with pytest.raises(PluginSuspended):
        host.api.kv.set("k", 1)


def test_fs_read_write_list_delete(tmp_path):
    host, plugin_dir, _ = make_host(tmp_path)
    assert host.api.fs.write_text("notes/a.txt", "hello") == 5
    assert (plugin_dir / "notes" / "a.txt").read_text(encoding="utf-8") == "hello"
    assert host.api.fs.read_text("notes/a.txt") == "hello"
    assert "a.txt" in host.api.fs.list("notes")
    assert host.api.fs.delete("notes/a.txt") is True
    assert host.api.fs.delete("notes/a.txt") is False


def test_fs_blocks_escape_and_root_delete(tmp_path):
    host, _, _ = make_host(tmp_path)
    with pytest.raises(safety.UnsafePathError):
        host.api.fs.read_text("../secret.txt")
    with pytest.raises(HostAPIError):
        host.api.fs.delete(".")


def test_fs_requires_permission(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("kv",))
    with pytest.raises(PermissionDenied):
        host.api.fs.write_text("a.txt", "x")


def test_workspace_requires_resolver(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("fs:workspace",))
    with pytest.raises(HostAPIError):
        host.api.fs.workspace_write_text("session-1", "a.txt", "x")


def test_workspace_scoped_to_plugin_subdir(tmp_path):
    workspace = tmp_path / "ws"
    workspace.mkdir()
    services = HostServices(workspace_resolver=lambda sid: str(workspace) if sid == "s1" else None)
    host, _, _ = make_host(tmp_path, permissions=("fs:workspace",), services=services)
    host.api.fs.workspace_write_text("s1", "a.txt", "data")
    assert (workspace / "plugins" / "demo" / "a.txt").read_text(encoding="utf-8") == "data"
    assert host.api.fs.workspace_list("s1") == ["a.txt"]
    with pytest.raises(safety.UnsafePathError):
        host.api.fs.workspace_read_text("s1", "../../escape.txt")


def test_http_requires_declared_host(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("kv",))
    with pytest.raises(PermissionDenied):
        host.api.http.request("GET", "https://api.example.com/ping")


def test_http_rejects_non_http_scheme(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("net:api.example.com",))
    with pytest.raises(HostAPIError):
        host.api.http.request("GET", "file:///etc/passwd")


def test_http_blocks_private_host_without_exact_permission(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("net:*.internal",))
    with pytest.raises((PermissionDenied, HostAPIError)):
        host.api.http.request("GET", "http://127.0.0.1:8080/status")


class _FakeResponse:
    def __init__(self, status=200, body=b'{"ok": true}', headers=None):
        self.status = status
        self._body = body
        self.headers = headers or {"Content-Type": "application/json"}

    def read(self, limit=-1):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(url, code, location=None):
    headers = Message()
    if location:
        headers["Location"] = location
    return urllib.error.HTTPError(url, code, "redirect", headers, None)


def _resolver(mapping):
    def _resolve(host, port):
        return [mapping.get(host, "93.184.216.34")]

    return _resolve


def test_http_success_via_fake_open(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:api.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"api.example.com": "93.184.216.34"})
    )
    monkeypatch.setattr(host_module, "_open_url", lambda request, timeout: _FakeResponse())
    result = host.api.http.request("GET", "https://api.example.com/ping")
    assert result["status"] == 200
    assert json.loads(result["body"]) == {"ok": True}
    assert result["truncated"] is False


@pytest.mark.parametrize(
    "ip",
    [
        "127.0.0.1",
        "10.1.2.3",
        "172.16.0.9",
        "192.168.1.5",
        "169.254.1.1",
        "0.0.0.0",
        "::1",
        "fc00::1",
        "::ffff:127.0.0.1",
        "not-an-ip",
    ],
)
def test_private_address_matrix(ip):
    assert host_module._is_private_address(ip) is True


@pytest.mark.parametrize("ip", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_address_matrix(ip):
    assert host_module._is_private_address(ip) is False


def test_http_rejects_dns_rebinding_to_loopback(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:*.nip.io",))
    monkeypatch.setattr(
        host_module,
        "_resolve_host_ips",
        _resolver({"127.0.0.1.nip.io": "127.0.0.1"}),
    )
    with pytest.raises(HostAPIError) as excinfo:
        host.api.http.request("GET", "http://127.0.0.1.nip.io/metrics")
    assert "内网" in str(excinfo.value)


@pytest.mark.parametrize("address", ["10.1.2.3", "192.168.1.5", "169.254.1.1", "127.0.0.1"])
def test_http_rejects_private_ranges(tmp_path, monkeypatch, address):
    host, _, _ = make_host(tmp_path, permissions=("net:*.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"svc.example.com": address})
    )
    with pytest.raises(HostAPIError) as excinfo:
        host.api.http.request("GET", "http://svc.example.com/x")
    assert "内网" in str(excinfo.value)


def test_http_ipv6_loopback_rejected(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("net:*.example.com",))
    with pytest.raises((PermissionDenied, HostAPIError)):
        host.api.http.request("GET", "http://[::1]/x")


def test_http_allows_explicit_private_ip_declaration(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:10.1.2.3",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"10.1.2.3": "10.1.2.3"})
    )
    monkeypatch.setattr(host_module, "_open_url", lambda request, timeout: _FakeResponse())
    result = host.api.http.request("GET", "http://10.1.2.3/health")
    assert result["status"] == 200


def test_redirect_to_loopback_rejected(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:public.example.com",))
    monkeypatch.setattr(
        host_module,
        "_resolve_host_ips",
        _resolver({"public.example.com": "93.184.216.34", "127.0.0.1": "127.0.0.1"}),
    )
    calls = []

    def _fake_open(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request.full_url, 302, "http://127.0.0.1/secret")

    monkeypatch.setattr(host_module, "_open_url", _fake_open)
    with pytest.raises((PermissionDenied, HostAPIError)):
        host.api.http.request("GET", "https://public.example.com/start")
    assert calls == ["https://public.example.com/start"]


def test_redirect_to_unlisted_host_rejected(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:public.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"public.example.com": "93.184.216.34"})
    )
    monkeypatch.setattr(
        host_module,
        "_open_url",
        lambda request, timeout: (_ for _ in ()).throw(
            _http_error(request.full_url, 302, "https://other.example.com/x")
        ),
    )
    with pytest.raises(PermissionDenied):
        host.api.http.request("GET", "https://public.example.com/start")


def test_redirect_same_host_followed(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:public.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"public.example.com": "93.184.216.34"})
    )
    calls = []

    def _fake_open(request, timeout):
        calls.append(request.full_url)
        if len(calls) == 1:
            raise _http_error(request.full_url, 302, "/next")
        return _FakeResponse(200, b"final")

    monkeypatch.setattr(host_module, "_open_url", _fake_open)
    result = host.api.http.request("GET", "https://public.example.com/start")
    assert result["status"] == 200
    assert result["body"] == "final"
    assert calls == ["https://public.example.com/start", "https://public.example.com/next"]


def test_redirect_https_downgrade_rejected(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:public.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"public.example.com": "93.184.216.34"})
    )
    monkeypatch.setattr(
        host_module,
        "_open_url",
        lambda request, timeout: (_ for _ in ()).throw(
            _http_error(request.full_url, 302, "http://public.example.com/x")
        ),
    )
    with pytest.raises(HostAPIError) as excinfo:
        host.api.http.request("GET", "https://public.example.com/start")
    assert "https" in str(excinfo.value)


def test_redirect_limit(tmp_path, monkeypatch):
    host, _, _ = make_host(tmp_path, permissions=("net:public.example.com",))
    monkeypatch.setattr(
        host_module, "_resolve_host_ips", _resolver({"public.example.com": "93.184.216.34"})
    )
    calls = []

    def _fake_open(request, timeout):
        calls.append(request.full_url)
        raise _http_error(request.full_url, 302, "/again")

    monkeypatch.setattr(host_module, "_open_url", _fake_open)
    with pytest.raises(HostAPIError) as excinfo:
        host.api.http.request("GET", "https://public.example.com/start")
    assert "重定向" in str(excinfo.value)
    assert len(calls) == host_module.HTTP_MAX_REDIRECTS + 1


class FakeBus:
    def __init__(self):
        self.subscriptions = []
        self.emitted = []

    def subscribe(self, pattern, callback, *, name="", once=False):
        self.subscriptions.append((pattern, callback, name))
        return f"sub-{pattern}"

    def unsubscribe(self, pattern, subscription=None):
        self.subscriptions = [item for item in self.subscriptions if item[0] != pattern]
        return True

    async def emit(self, event):
        self.emitted.append(event)
        return 1


def test_events_subscribe_requires_declared_event(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("kv",))
    with pytest.raises(PermissionDenied):
        host.api.events.subscribe("reply.before_send", lambda payload: None)


def test_events_subscribe_and_forward(tmp_path):
    bus = FakeBus()
    services = HostServices(event_bus=bus)
    host, _, _ = make_host(
        tmp_path, permissions=("event:reply.before_send",), services=services
    )
    received = []
    host.api.events.subscribe("reply.before_send", received.append)
    assert bus.subscriptions[0][0] == "reply.before_send"
    assert host.registry.get_event_handlers("reply.before_send")

    callback = bus.subscriptions[0][1]
    callback({"type": "reply.before_send", "payload": {"text": "hi"}})
    assert received == [{"type": "reply.before_send", "payload": {"text": "hi"}}]

    host.api.events.unsubscribe("reply.before_send")
    assert bus.subscriptions == []


def test_events_emit_only_own_namespace(tmp_path):
    bus = FakeBus()
    host, _, _ = make_host(tmp_path, permissions=("kv",), services=HostServices(event_bus=bus))
    with pytest.raises(HostAPIError):
        host.api.events.emit("conversation.before_receive", {})

    count = host.api.events.emit("plugin.demo.custom", {"value": 1})
    assert count == 1
    assert bus.emitted[0].type == "plugin.demo.custom"
    assert bus.emitted[0].source == "plugin:demo"


def test_events_emit_without_bus(tmp_path):
    host, _, _ = make_host(tmp_path)
    with pytest.raises(HostAPIError):
        host.api.events.emit("plugin.demo.custom", {})


def test_tools_register_enforces_prefix(tmp_path):
    host, _, registry = make_host(tmp_path, permissions=("tool.register",))
    handler = lambda args: {"ok": True}
    with pytest.raises(HostAPIError):
        host.api.tools.register("ping", description="x", handler=handler)
    assert host.api.tools.register("plugin_demo_ping", description="ping", handler=handler)
    assert registry.plugin_tool_names("demo") == ["plugin_demo_ping"]
    assert host.api.tools.unregister("plugin_demo_ping") is True


def test_session_read_requires_service_and_permission(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("kv",))
    with pytest.raises(PermissionDenied):
        host.api.session.get_messages("s1")

    services = HostServices(
        session_reader=lambda sid: {
            "messages": [{"role": "user", "content": f"m{i}"} for i in range(240)]
        }
    )
    host, _, _ = make_host(
        tmp_path, permissions=("host:session.read",), services=services
    )
    messages = host.api.session.get_messages("s1", limit=999)
    assert len(messages) == 200
    assert messages[-1]["content"] == "m239"


def test_message_send_validation(tmp_path):
    sent = []
    services = HostServices(message_sender=lambda c, t, x: sent.append((c, t, x)) or True)
    host, _, _ = make_host(tmp_path, permissions=("host:message.send",), services=services)
    assert host.api.message.send("web", "session-1", "hello") is True
    assert sent == [("web", "session-1", "hello")]
    with pytest.raises(HostAPIError):
        host.api.message.send("web", "session-1", "")
    with pytest.raises(HostAPIError):
        host.api.message.send("web", "session-1", "x" * 5000)


def test_log_writes_to_plugin_dir(tmp_path):
    host, _, _ = make_host(tmp_path)
    host.api.log.info("hello %s", "world")
    log_path = tmp_path / "data" / "demo" / "plugin.log"
    assert log_path.is_file()
    assert "hello world" in log_path.read_text(encoding="utf-8")


def test_guard_override_hides_capability(tmp_path):
    host, _, _ = make_host(tmp_path, permissions=("kv",), override=[])
    with pytest.raises(PermissionDenied):
        host.api.kv.set("k", "v")


def test_run_coroutine_helper_runs_async_emit(tmp_path):
    from nbot.plugins.host import _run_coroutine

    async def _value():
        return 7

    assert _run_coroutine(_value()) == 7

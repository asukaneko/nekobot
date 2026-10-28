"""P2-13 Realtime 实时语音（WebSocket 代理）测试。"""

import base64
import json
import time
import types

import pytest
from flask import Flask
from flask_socketio import SocketIO

from nbot.web import realtime_voice as rv


class FakeWebSocket:
    """最小化的厂商 WebSocket 替身。"""

    def __init__(self, incoming=None):
        self.sent = []
        self.incoming = list(incoming or [])
        self.closed = False
        self.pings = 0

    def send(self, payload):
        self.sent.append(json.loads(payload))

    def recv(self):
        if self.incoming:
            return self.incoming.pop(0)
        time.sleep(0.01)
        raise rv.websocket.WebSocketTimeoutException()

    def ping(self):
        self.pings += 1

    def close(self):
        self.closed = True

    def settimeout(self, _timeout):
        return None


def wait_for(predicate, timeout=3.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.02)
    return predicate()


def _config(**overrides):
    config = {
        "provider": "qwen_omni",
        "provider_label": "通义 Qwen-Omni Realtime",
        "model": "qwen3-omni-flash-realtime",
        "voice": "Cherry",
        "instructions": "你是测试助手",
        "sample_rate": 24000,
        "url": "wss://example.test/realtime?model=test",
        "headers": ["Authorization: Bearer test-key"],
        "api_key": "test-key",
        "protocol": "openai_realtime",
        "proxy_url": "",
    }
    config.update(overrides)
    return config


def test_provider_build_url_and_headers():
    provider = rv.REALTIME_PROVIDERS["qwen_omni"]
    url = provider.build_url("qwen3-omni-flash-realtime")
    assert url.startswith("wss://dashscope.aliyuncs.com/api-ws/v1/realtime?model=")
    assert provider.auth_headers("k") == ["Authorization: Bearer k"]

    openai = rv.REALTIME_PROVIDERS["openai_realtime"]
    headers = openai.auth_headers("sk")
    assert "Authorization: Bearer sk" in headers
    assert "OpenAI-Beta: realtime=v1" in headers

    custom = rv.REALTIME_PROVIDERS["custom_realtime"]
    with pytest.raises(rv.RealtimeConfigError):
        custom.build_url("x")


def test_build_url_upgrades_https_and_keeps_query():
    provider = rv.REALTIME_PROVIDERS["custom_realtime"]
    assert provider.build_url("m", "https://api.test/v1/realtime") == (
        "wss://api.test/v1/realtime?model=m"
    )
    assert provider.build_url("m", "wss://api.test/v1/realtime?token=1") == (
        "wss://api.test/v1/realtime?token=1&model=m"
    )


def test_resolve_config_from_env(monkeypatch):
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    monkeypatch.setattr(rv, "_find_realtime_model", lambda provider_id: {})
    config = rv.resolve_realtime_config("qwen_omni")
    assert config["api_key"] == "env-key"
    assert config["key_source"] == "DASHSCOPE_API_KEY"
    assert config["sample_rate"] == 24000
    assert config["voice"] == "Cherry"


def test_resolve_config_requires_key(monkeypatch):
    monkeypatch.delenv("DASHSCOPE_API_KEY", raising=False)
    monkeypatch.delenv("QWEN_API_KEY", raising=False)
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    monkeypatch.setattr(rv, "_find_realtime_model", lambda provider_id: {})
    with pytest.raises(rv.RealtimeConfigError):
        rv.resolve_realtime_config("qwen_omni")
    # require_key=False 时允许只取元数据
    assert rv.resolve_realtime_config("qwen_omni", require_key=False)["api_key"] == ""


def test_resolve_config_rejects_unknown_provider(monkeypatch):
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    with pytest.raises(rv.RealtimeConfigError):
        rv.resolve_realtime_config("nope_provider")


def test_session_sends_session_update_and_relays_audio(monkeypatch):
    fake = FakeWebSocket([
        json.dumps({"type": "session.created", "session": {"id": "s1"}}),
        json.dumps({"type": "response.audio.delta", "delta": "QUJD"}),
        json.dumps({
            "type": "conversation.item.input_audio_transcription.completed",
            "transcript": "你好",
        }),
    ])
    monkeypatch.setattr(rv.websocket, "create_connection", lambda *a, **k: fake)
    events = []
    session = rv.RealtimeVoiceSession(emit=lambda e, p: events.append((e, p)), config=_config())
    session.start()
    try:
        assert wait_for(lambda: any(name == "audio" for name, _ in events))
        names = [name for name, _ in events]
        assert "ready" in names
        assert "session" in names
        assert "user_transcript" in names
        assert fake.sent[0]["type"] == "session.update"
        body = fake.sent[0]["session"]
        assert body["voice"] == "Cherry"
        assert body["input_audio_format"] == "pcm16"
        assert body["turn_detection"]["type"] == "server_vad"
        assert body["instructions"] == "你是测试助手"
    finally:
        session.stop()
    assert session.stats.state == "closed"
    assert any(name == "closed" for name, _ in events)


def test_session_send_audio_and_interrupt(monkeypatch):
    fake = FakeWebSocket([json.dumps({"type": "response.created", "response": {"id": "r1"}})])
    monkeypatch.setattr(rv.websocket, "create_connection", lambda *a, **k: fake)
    events = []
    session = rv.RealtimeVoiceSession(emit=lambda e, p: events.append((e, p)), config=_config())
    session.start()
    try:
        assert wait_for(lambda: any(name == "response_started" for name, _ in events))
        pcm = b"\x00\x01" * 240
        assert session.send_audio(base64.b64encode(pcm).decode()) is True
        assert wait_for(
            lambda: any(
                item.get("type") == "input_audio_buffer.append" for item in fake.sent
            )
        )
        session.interrupt()
        assert wait_for(
            lambda: any(item.get("type") == "response.cancel" for item in fake.sent)
        )
        assert any(name == "interrupted" for name, _ in events)
    finally:
        session.stop()


def test_send_audio_rejects_bad_input(monkeypatch):
    session = rv.RealtimeVoiceSession(emit=lambda e, p: None, config=_config())
    assert session.send_audio("") is False
    assert session.send_audio("!!!not-base64!!!") is False
    long_audio = "A" * (rv.MAX_AUDIO_CHUNK_CHARS + 10)
    assert session.send_audio(long_audio) is False
    # 奇数字节自动截断为偶数
    odd = base64.b64encode(b"\x00\x01\x02").decode()
    assert session.send_audio(odd) is True
    queued = session._outbound[-1]
    assert len(base64.b64decode(queued["audio"])) == 2


def test_outbound_queue_drops_oldest(monkeypatch):
    session = rv.RealtimeVoiceSession(emit=lambda e, p: None, config=_config())
    chunk = base64.b64encode(b"\x00\x01").decode()
    for _ in range(rv.MAX_OUTBOUND_CHUNKS + 3):
        session.send_audio(chunk)
    assert len(session._outbound) == rv.MAX_OUTBOUND_CHUNKS
    assert session._dropped_chunks == 3


def test_session_reconnects_after_connection_error(monkeypatch):
    fake = FakeWebSocket()
    attempts = {"n": 0}

    def fake_create(*args, **kwargs):
        attempts["n"] += 1
        if attempts["n"] == 1:
            raise ConnectionError("boom")
        return fake

    monkeypatch.setattr(rv.websocket, "create_connection", fake_create)
    monkeypatch.setattr(rv, "RECONNECT_BASE_DELAY", 0.01)
    events = []
    session = rv.RealtimeVoiceSession(emit=lambda e, p: events.append((e, p)), config=_config())
    session.start()
    try:
        assert wait_for(lambda: any(name == "ready" for name, _ in events))
        names = [name for name, _ in events]
        assert "reconnecting" in names
        assert "error" in names
        assert session.stats.reconnects == 1
    finally:
        session.stop()


def test_session_gives_up_after_max_attempts(monkeypatch):
    def always_fail(*args, **kwargs):
        raise ConnectionError("nope")

    monkeypatch.setattr(rv.websocket, "create_connection", always_fail)
    monkeypatch.setattr(rv, "RECONNECT_BASE_DELAY", 0.01)
    monkeypatch.setattr(rv, "MAX_RECONNECT_ATTEMPTS", 2)
    events = []
    session = rv.RealtimeVoiceSession(emit=lambda e, p: events.append((e, p)), config=_config())
    session.start()
    assert wait_for(lambda: session.stats.state == "failed", timeout=5)
    fatal = [payload for name, payload in events if name == "error" and payload.get("retryable") is False]
    assert fatal


def test_map_vendor_events():
    assert rv._map_vendor_event("response.audio.delta", {"delta": "AA"}) == (
        "audio",
        {"delta": "AA", "encoding": "pcm16"},
    )
    assert rv._map_vendor_event("response.audio.delta", {"delta": ""}) is None
    done = rv._map_vendor_event("response.done", {"response": {"status": "completed"}})
    assert done == ("response_done", {"status": "completed", "usage": {}})
    err = rv._map_vendor_event("error", {"error": {"message": "bad", "code": "x"}})
    assert err[0] == "error" and err[1]["message"] == "bad"
    assert rv._map_vendor_event("unknown.event", {}) is None


def test_manager_start_and_end(monkeypatch):
    monkeypatch.setattr(rv.websocket, "create_connection", lambda *a, **k: FakeWebSocket())
    manager = rv.RealtimeVoiceManager()
    emitted = []
    stats = manager.start(
        "sid-1", _config(), emit=lambda e, p: emitted.append((e, p)), session_id="s1"
    )
    assert stats["session_id"] == "s1"
    assert manager.stats()["active_sessions"] == 1
    assert manager.get("sid-1") is not None
    assert manager.end("sid-1") is True
    assert manager.stats()["active_sessions"] == 0
    assert manager.end("sid-1") is False


def test_proxy_kwargs_from_proxy_url():
    session = rv.RealtimeVoiceSession(
        emit=lambda e, p: None, config=_config(proxy_url="http://user:pass@127.0.0.1:7890")
    )
    kwargs = session._proxy_kwargs()
    assert kwargs["http_proxy_host"] == "127.0.0.1"
    assert kwargs["http_proxy_port"] == 7890
    assert kwargs["http_proxy_auth"] == ("user", "pass")


def _socket_server():
    app = Flask(__name__)
    socketio = SocketIO(app, async_mode="threading")
    server = types.SimpleNamespace(
        socketio=socketio,
        active_connections={},
        data_dir="",
        _validate_login_token=lambda token: "tester" if token == "good-token" else None,
    )
    rv.register_realtime_voice(server)
    return app, socketio, server


def test_namespace_rejects_unauthenticated(monkeypatch):
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    app, socketio, _server = _socket_server()
    client = socketio.test_client(
        app, namespace=rv.REALTIME_NAMESPACE, auth={"token": "bad-token"}
    )
    assert client.is_connected(rv.REALTIME_NAMESPACE) is False


def test_namespace_start_reports_config_error(monkeypatch):
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    app, socketio, _server = _socket_server()
    client = socketio.test_client(
        app, namespace=rv.REALTIME_NAMESPACE, auth={"token": "good-token"}
    )
    assert client.is_connected(rv.REALTIME_NAMESPACE) is True
    client.emit(
        "realtime.start", {"provider": "not_a_provider"}, namespace=rv.REALTIME_NAMESPACE
    )
    received = client.get_received(rv.REALTIME_NAMESPACE)
    names = [item["name"] for item in received]
    assert "error" in names
    client.disconnect(namespace=rv.REALTIME_NAMESPACE)


def test_realtime_http_routes(monkeypatch):
    monkeypatch.setattr(rv, "_load_settings_section", lambda: {})
    monkeypatch.setattr(rv, "_find_realtime_model", lambda provider_id: {})
    monkeypatch.setenv("DASHSCOPE_API_KEY", "env-key")

    app = Flask(__name__)
    server = types.SimpleNamespace(socketio=None, data_dir="")
    from nbot.web.routes.voice import _register_realtime_routes

    _register_realtime_routes(app, server)
    client = app.test_client()

    providers = client.get("/api/realtime/providers").get_json()
    assert providers["success"] is True
    ids = [item["id"] for item in providers["providers"]]
    assert "qwen_omni" in ids
    assert providers["namespace"] == rv.REALTIME_NAMESPACE
    assert providers["audio"]["format"] == "pcm16"

    status = client.get("/api/realtime/status").get_json()
    assert status["success"] is True
    assert status["active_sessions"] == 0

"""MCP Bridge：单 server 并发、超时取消、断连与懒重连测试。"""

import asyncio
import threading
import time

import pytest

from nbot.services import mcp_bridge as mb


class FakeClient:
    """假 MCP 客户端：记录并发峰值、取消次数、连接次数。"""

    instances: list["FakeClient"] = []
    delay = 0.2

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.active = 0
        self.max_active = 0
        self.cancelled = 0
        self.closed = False
        self.connected = False
        self._lock = threading.Lock()
        FakeClient.instances.append(self)

    async def connect(self):
        self.connected = True

    async def list_tools(self):
        return [
            {
                "name": "echo",
                "description": "",
                "input_schema": {},
                "annotations": {"readOnlyHint": True},
            }
        ]

    async def close(self):
        self.closed = True

    async def call_tool(self, name, arguments=None):
        with self._lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
        try:
            await asyncio.sleep(FakeClient.delay)
            return {"echo": arguments, "tool": name}
        except asyncio.CancelledError:
            with self._lock:
                self.cancelled += 1
            raise
        finally:
            with self._lock:
                self.active -= 1


@pytest.fixture()
def bridge(monkeypatch):
    FakeClient.instances = []
    FakeClient.delay = 0.2
    monkeypatch.setattr(mb, "MCPRemoteClient", FakeClient)
    instance = mb.MCPBridge()
    instance.run_async(instance.connect_server("srv-1234", {"transport": "streamable-http", "url": "http://x"}))
    yield instance
    instance.run_async(instance.disconnect_all())


def _connect_count() -> int:
    return len(FakeClient.instances)


def test_connect_stores_config_and_readonly_hints(bridge):
    assert bridge.is_connected("srv-1234")
    assert bridge._configs["srv-1234"]["url"] == "http://x"
    assert bridge.is_tool_read_only(
        mb._make_tool_name("srv-1234", "echo")
    ) is True
    assert bridge.is_tool_read_only(mb._make_tool_name("srv-1234", "missing")) is False
    assert bridge.is_tool_read_only("not-an-mcp-name") is False


def test_same_server_calls_run_concurrently(bridge):
    async def _gather():
        return await asyncio.gather(
            *[bridge.execute("srv-1234", "echo", {"i": i}) for i in range(3)]
        )

    results = bridge.run_async(_gather(), timeout=10)

    assert len(results) == 3
    assert FakeClient.instances[0].max_active >= 2


def test_concurrency_is_capped_per_server(bridge):
    bridge.set_max_concurrency(2)

    async def _gather():
        return await asyncio.gather(
            *[bridge.execute("srv-1234", "echo", {"i": i}) for i in range(6)]
        )

    bridge.run_async(_gather(), timeout=20)

    assert FakeClient.instances[0].max_active <= 2
    assert FakeClient.instances[0].max_active >= 2


def test_call_timeout_returns_error_and_drops_client(bridge):
    FakeClient.delay = 1.0

    result = bridge.run_async(
        bridge.execute("srv-1234", "echo", {}, timeout=0.1), timeout=10
    )

    assert result["timeout"] is True
    assert "超时" in result["error"]
    client = FakeClient.instances[0]
    deadline = time.time() + 2
    while not client.cancelled and time.time() < deadline:
        time.sleep(0.01)
    assert client.cancelled == 1
    assert bridge.is_connected("srv-1234") is False
    # 工具缓存与配置保留，便于懒重连
    assert bridge.get_server_tools("srv-1234")
    assert "srv-1234" in bridge._configs


def test_lazy_reconnect_after_drop(bridge):
    FakeClient.delay = 1.0
    bridge.run_async(bridge.execute("srv-1234", "echo", {}, timeout=0.1), timeout=10)
    assert bridge.is_connected("srv-1234") is False

    FakeClient.delay = 0.01
    result = bridge.run_async(
        bridge.execute("srv-1234", "echo", {"hi": 1}), timeout=10
    )

    assert result["echo"] == {"hi": 1}
    assert bridge.is_connected("srv-1234") is True
    assert _connect_count() >= 2


def test_run_async_timeout_cancels_remote_coroutine(bridge):
    FakeClient.delay = 1.0

    with pytest.raises(mb.MCPCallTimeout):
        bridge.run_async(bridge.execute("srv-1234", "echo", {}), timeout=0.1)

    client = FakeClient.instances[0]
    deadline = time.time() + 2
    while not client.cancelled and time.time() < deadline:
        time.sleep(0.01)
    assert client.cancelled == 1


def test_status_reports_last_error(bridge):
    FakeClient.delay = 1.0
    bridge.run_async(bridge.execute("srv-1234", "echo", {}, timeout=0.1), timeout=10)

    status = bridge.get_status()["srv-1234"]

    assert status["connected"] is False
    assert "超时" in status["last_error"]
    assert status["concurrency"] >= 1


def test_disconnect_forgets_config(bridge):
    bridge.run_async(bridge.disconnect_server("srv-1234"))

    assert bridge.is_connected("srv-1234") is False
    assert "srv-1234" not in bridge._configs


def test_set_max_concurrency_rejects_invalid(bridge):
    with pytest.raises(ValueError):
        bridge.set_max_concurrency(0)

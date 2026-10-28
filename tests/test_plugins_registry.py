"""插件注册表：按 source 隔离与批量注销测试。"""

from nbot.plugins.registry import PluginRegistry, plugin_source


class FakeToolApi:
    """模拟 nbot.services.tools 的外部工具接口。"""

    def __init__(self):
        self.tools = {}
        self.unregister_calls = []

    def register_external_tool(self, name, spec, handler, *, source, read_only=False):
        if name in self.tools:
            return False
        self.tools[name] = {
            "spec": spec,
            "handler": handler,
            "source": source,
            "read_only": read_only,
        }
        return True

    def unregister_external_tool(self, name, *, source):
        entry = self.tools.get(name)
        if entry is None or entry["source"] != source:
            return False
        del self.tools[name]
        return True

    def unregister_source(self, source):
        self.unregister_calls.append(source)
        names = [name for name, entry in self.tools.items() if entry["source"] == source]
        for name in names:
            del self.tools[name]
        return len(names)


def _handler(args):
    return {"ok": True}


def test_plugin_source_format():
    assert plugin_source("demo") == "plugin:demo"
    assert plugin_source("daily-news") == "plugin:daily-news"


def test_register_tool_delegates_with_source():
    api = FakeToolApi()
    registry = PluginRegistry(tool_api=api)
    assert registry.register_tool("demo", "plugin_demo_ping", {"description": "p"}, _handler)
    entry = api.tools["plugin_demo_ping"]
    assert entry["source"] == "plugin:demo"
    assert registry.plugin_tool_names("demo") == ["plugin_demo_ping"]
    assert registry.plugin_summary("demo") == {
        "tools": ["plugin_demo_ping"],
        "events": [],
    }


def test_duplicate_tool_name_rejected():
    api = FakeToolApi()
    registry = PluginRegistry(tool_api=api)
    assert registry.register_tool("demo", "plugin_demo_ping", {"description": "p"}, _handler)
    assert not registry.register_tool("demo", "plugin_demo_ping", {"description": "p"}, _handler)
    assert registry.plugin_tool_names("demo") == ["plugin_demo_ping"]


def test_events_are_isolated_per_plugin():
    registry = PluginRegistry(tool_api=FakeToolApi())

    def first(payload):
        return "first"

    def second(payload):
        return "second"

    assert registry.register_event("demo", "reply.before_send", first)
    assert registry.register_event("other", "reply.before_send", second)
    assert not registry.register_event("demo", "reply.before_send", first)

    handlers = registry.get_event_handlers("reply.before_send")
    assert handlers == [first, second]
    assert registry.plugin_event_names("demo") == ["reply.before_send"]

    removed = registry.unregister_event("demo", "reply.before_send")
    assert removed == 1
    assert registry.get_event_handlers("reply.before_send") == [second]
    assert registry.unregister_event("demo", "reply.before_send") == 0


def test_unregister_source_reclaims_tools_and_events():
    api = FakeToolApi()
    registry = PluginRegistry(tool_api=api)
    registry.register_tool("demo", "plugin_demo_a", {"description": "a"}, _handler)
    registry.register_tool("demo", "plugin_demo_b", {"description": "b"}, _handler)
    registry.register_tool("other", "plugin_other_a", {"description": "a"}, _handler)
    registry.register_event("demo", "reply.before_send", _handler)
    registry.register_event("demo", "reply.after_send", _handler)

    result = registry.unregister_source("demo")
    assert result == {"tools": 2, "events": 2, "event_names": 2}
    assert sorted(api.tools) == ["plugin_other_a"]
    assert api.unregister_calls == ["plugin:demo"]
    assert registry.plugin_summary("demo") == {"tools": [], "events": []}
    assert registry.get_event_handlers("reply.before_send") == []


def test_registry_without_tool_api_returns_false(monkeypatch):
    registry = PluginRegistry()

    def broken():
        raise RuntimeError("工具注册接口不可用")

    monkeypatch.setattr(registry, "_api", broken)
    assert not registry.register_tool("demo", "plugin_demo_ping", {}, _handler)
    assert registry.plugin_tool_names("demo") == []

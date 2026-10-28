"""权限解析、有效权限与 guard 越权计数测试。"""

import pytest

from nbot.plugins.permissions import (
    VIOLATION_LIMIT,
    PermissionDenied,
    PermissionFormatError,
    PermissionGuard,
    PluginSuspended,
    effective_permissions,
    parse_permission,
    permission_covers,
    permissions_for_display,
)


@pytest.mark.parametrize(
    "text,kind,arg",
    [
        ("kv", "kv", ""),
        ("fs:plugin", "fs:plugin", ""),
        ("fs:workspace", "fs:workspace", ""),
        ("host:session.read", "host", ""),
        ("host:message.send", "host", ""),
        ("tool.register", "tool", ""),
        ("ui.page", "ui", ""),
        ("net:api.example.com", "net", "api.example.com"),
        ("net:*.example.com", "net", "*.example.com"),
        ("event:conversation.before_receive", "event", "conversation.before_receive"),
        ("event:conversation.*", "event", "conversation.*"),
    ],
)
def test_parse_permission(text, kind, arg):
    parsed = parse_permission(text)
    assert (parsed.kind, parsed.arg, parsed.text) == (kind, arg, text)


@pytest.mark.parametrize(
    "text",
    ["", "root", "net:", "net:*", "net:a.*.b", "event:", "event:*", "fs:/etc", "kv:all"],
)
def test_parse_permission_rejects_invalid(text):
    with pytest.raises(PermissionFormatError):
        parse_permission(text)


def test_permission_covers():
    assert permission_covers("net:*.example.com", "net:api.example.com")
    assert permission_covers("net:api.example.com", "net:api.example.com")
    assert not permission_covers("net:api.example.com", "net:other.example.com")
    assert permission_covers("event:conversation.*", "event:conversation.before_receive")
    assert not permission_covers("kv", "fs:plugin")
    assert not permission_covers("fs:plugin", "fs:workspace")


def test_effective_permissions_intersection():
    declared = ["kv", "net:api.example.com", "event:reply.before_send"]
    assert effective_permissions(declared) == tuple(declared)
    assert effective_permissions(declared, ["kv"]) == ("kv",)
    with pytest.raises(PermissionFormatError):
        effective_permissions(declared, ["fs:plugin"])


def test_guard_allows_and_denies():
    guard = PermissionGuard("demo", ["kv", "net:*.example.com"], ["kv"])
    assert guard.effective == ("kv",)
    assert guard.allows("kv")
    assert not guard.allows("net:api.example.com")

    with pytest.raises(PermissionDenied):
        guard.require("net:api.example.com")
    assert guard.violations == 1


def test_guard_suspends_after_limit():
    guard = PermissionGuard("demo", ["kv"])
    for _ in range(VIOLATION_LIMIT - 1):
        with pytest.raises(PermissionDenied):
            guard.require("fs:plugin")
    with pytest.raises(PluginSuspended) as excinfo:
        guard.require("fs:plugin")
    assert excinfo.value.plugin_id == "demo"
    assert guard.violations == VIOLATION_LIMIT


def test_guard_apply_override_and_reset():
    guard = PermissionGuard("demo", ["kv", "fs:plugin"])
    assert guard.effective == ("kv", "fs:plugin")
    guard.apply_override(["fs:plugin"])
    assert guard.effective == ("fs:plugin",)
    assert not guard.allows("kv")
    guard.reset_violations()
    assert guard.violations == 0
    guard.apply_override(None)
    assert guard.effective == ("kv", "fs:plugin")


def test_permissions_for_display():
    items = permissions_for_display(["kv", "net:api.example.com"])
    assert items[0]["name"] == "kv"
    assert "api.example.com" in items[1]["label"]

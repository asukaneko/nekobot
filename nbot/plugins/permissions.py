"""插件权限：解析、有效权限计算与运行时校验。

有效权限 = manifest 声明 ∩ 管理员策略（策略只能收紧，不能放宽）。
越权调用会计数并写入审计日志，累计超过 :data:`VIOLATION_LIMIT` 次触发
:class:`PluginSuspended`，由运行时负责停用插件。
"""

import logging
import re
from dataclasses import dataclass
from typing import Iterable, List, Optional, Sequence, Tuple

_log = logging.getLogger(__name__)
_audit = logging.getLogger("nbot.plugins.audit")

#: 同一插件允许的越权次数，超过后停用
VIOLATION_LIMIT = 5

#: 无参数权限
SIMPLE_PERMISSIONS = frozenset(
    {
        "kv",
        "fs:plugin",
        "fs:workspace",
        "host:session.read",
        "host:message.send",
        "tool.register",
        "ui.page",
    }
)

#: 出网白名单主机（支持 ``*.example.com`` 前缀通配，禁止裸 ``*``）
NET_HOST_PATTERN = re.compile(r"^(\*\.)?[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?$")
#: 事件名（允许末尾 ``.*`` 通配整族事件）
EVENT_NAME_PATTERN = re.compile(r"^[a-z][a-z0-9_.]{0,62}(?:\.\*)?$")

PERMISSION_LABELS = {
    "kv": "插件私有存储",
    "fs:plugin": "读写插件自身目录",
    "fs:workspace": "读写会话工作区子目录",
    "host:session.read": "读取会话消息",
    "host:message.send": "主动发送消息",
    "tool.register": "注册 LLM 工具与动作",
    "ui.page": "提供插件页面",
    "net": "访问外部网络",
    "event": "订阅宿主事件",
}

_KIND_BY_SIMPLE = {
    "kv": "kv",
    "fs:plugin": "fs:plugin",
    "fs:workspace": "fs:workspace",
    "host:session.read": "host",
    "host:message.send": "host",
    "tool.register": "tool",
    "ui.page": "ui",
}


class PermissionFormatError(ValueError):
    """权限字符串无法解析或不被支持。"""


class PermissionDenied(PermissionError):
    """插件调用了未授权的宿主能力。"""

    def __init__(self, plugin_id: str, request: str, message: str = ""):
        self.plugin_id = str(plugin_id or "")
        self.request = str(request or "")
        text = message or f"插件 {self.plugin_id} 缺少权限: {self.request}"
        super().__init__(text)


class PluginSuspended(RuntimeError):
    """插件越权次数超限，需要停用。"""

    def __init__(self, plugin_id: str, violations: int):
        self.plugin_id = str(plugin_id or "")
        self.violations = int(violations)
        super().__init__(f"插件 {self.plugin_id} 越权 {violations} 次，已触发自动停用")


@dataclass(frozen=True)
class Permission:
    """解析后的权限项。"""

    text: str
    kind: str
    arg: str = ""

    @property
    def scope(self) -> str:
        return f"{self.kind}:{self.arg}" if self.arg else self.kind


def parse_permission(text: str) -> Permission:
    """解析权限字符串，未知或非法格式抛 :class:`PermissionFormatError`。"""
    if not isinstance(text, str):
        raise PermissionFormatError(f"权限必须是字符串: {text!r}")
    raw = text.strip()
    if not raw:
        raise PermissionFormatError("权限不能为空")
    if raw in SIMPLE_PERMISSIONS:
        return Permission(raw, _KIND_BY_SIMPLE[raw])
    if raw.startswith("net:"):
        host = raw[4:].strip().lower()
        if not host:
            raise PermissionFormatError("net 权限缺少主机名")
        if host == "*" or "*" in host.replace("*.", "", 1):
            raise PermissionFormatError(f"不允许全通配出网权限: {raw}")
        if not NET_HOST_PATTERN.match(host):
            raise PermissionFormatError(f"出网主机名非法: {host}")
        return Permission(f"net:{host}", "net", host)
    if raw.startswith("event:"):
        name = raw[6:].strip()
        if not EVENT_NAME_PATTERN.match(name):
            raise PermissionFormatError(f"事件名非法: {name!r}")
        return Permission(f"event:{name}", "event", name)
    raise PermissionFormatError(f"未知权限: {raw}")


def _covers(granted: Permission, request: Permission) -> bool:
    if granted.text == request.text:
        return True
    if granted.kind != request.kind:
        return False
    if granted.kind == "net":
        if granted.arg.startswith("*."):
            return request.arg.endswith(granted.arg[1:])
        return False
    if granted.kind == "event":
        # event:conversation.* 覆盖 conversation.before_receive
        if granted.arg.endswith(".*"):
            return request.arg.startswith(granted.arg[:-1])
        return False
    return False


def permission_covers(granted: str, request: str) -> bool:
    """判断 ``granted`` 是否覆盖 ``request``（两者都是权限字符串）。"""
    return _covers(parse_permission(granted), parse_permission(request))


def effective_permissions(
    declared: Iterable[str], override: Optional[Iterable[str]] = None
) -> Tuple[str, ...]:
    """计算有效权限：override 只能从 declared 里收紧，不能新增。"""
    declared_list = [parse_permission(item).text for item in declared or ()]
    if override is None:
        return tuple(declared_list)

    declared_set = set(declared_list)
    result: List[str] = []
    for item in override or ():
        parsed = parse_permission(item)
        if parsed.text not in declared_set:
            raise PermissionFormatError(
                f"策略权限 {parsed.text} 不在插件声明的权限内，不能放宽"
            )
        if parsed.text not in result:
            result.append(parsed.text)
    return tuple(result)


def describe_permission(text: str) -> str:
    """给面板用的权限说明。"""
    try:
        parsed = parse_permission(text)
    except PermissionFormatError:
        return str(text)
    label = PERMISSION_LABELS.get(parsed.kind, parsed.kind)
    if parsed.kind in ("net", "event"):
        return f"{label}: {parsed.arg}"
    return label


class PermissionGuard:
    """单个插件的运行时权限守卫。"""

    def __init__(
        self,
        plugin_id: str,
        declared: Iterable[str],
        override: Optional[Iterable[str]] = None,
    ):
        self.plugin_id = str(plugin_id or "")
        self.declared: Tuple[str, ...] = tuple(
            parse_permission(item).text for item in declared or ()
        )
        self._override: Optional[Tuple[str, ...]] = None
        self._effective: Tuple[str, ...] = ()
        self.violations = 0
        self.apply_override(override)

    @property
    def effective(self) -> Tuple[str, ...]:
        return self._effective

    @property
    def override(self) -> Optional[Tuple[str, ...]]:
        return self._override

    def apply_override(self, override: Optional[Iterable[str]]) -> Tuple[str, ...]:
        """设置管理员策略（只收紧），返回新的有效权限。"""
        if override is None:
            self._override = None
            self._effective = effective_permissions(self.declared)
            return self._effective
        normalized = effective_permissions(self.declared, override)
        self._override = normalized
        self._effective = normalized
        return self._effective

    def allows(self, request: str) -> bool:
        """判断请求是否被授权（不计数、不抛错）。"""
        try:
            parsed = parse_permission(request)
        except PermissionFormatError:
            return False
        for granted_text in self._effective:
            if _covers(parse_permission(granted_text), parsed):
                return True
        return False

    def require(self, request: str) -> None:
        """请求权限，未授权时计数并抛错；超限抛 :class:`PluginSuspended`。"""
        if self.allows(request):
            return
        self.violations += 1
        _audit.warning(
            "[Plugins] 越权调用 plugin=%s request=%s violations=%s/%s",
            self.plugin_id,
            request,
            self.violations,
            VIOLATION_LIMIT,
        )
        if self.violations >= VIOLATION_LIMIT:
            raise PluginSuspended(self.plugin_id, self.violations)
        raise PermissionDenied(self.plugin_id, request)

    def reset_violations(self) -> None:
        self.violations = 0


def permissions_for_display(permissions: Sequence[str]) -> List[dict]:
    """把权限列表转成带说明的结构，供 Web 面板展示。"""
    return [
        {"name": text, "label": describe_permission(text)} for text in permissions or ()
    ]

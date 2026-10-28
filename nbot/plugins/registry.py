"""插件资源注册表：按 source（``plugin:<id>``）隔离工具与事件订阅。

运行时在启用插件时通过本注册表登记能力，停用/重载/卸载时调用
:meth:`PluginRegistry.unregister_source` 一次性回收，避免残留。
"""

import logging
import threading
from typing import Any, Callable, Dict, Iterable, List, Optional

_log = logging.getLogger(__name__)

#: 插件工具来源前缀，与 :func:`nbot.services.tools.unregister_source` 对齐
PLUGIN_SOURCE_PREFIX = "plugin:"


def plugin_source(plugin_id: str) -> str:
    """插件在外部工具注册表中的 source 标识。"""
    return f"{PLUGIN_SOURCE_PREFIX}{plugin_id}"


class PluginRegistry:
    """插件工具与事件订阅的注册表。"""

    def __init__(self, tool_api: Any = None):
        self._lock = threading.RLock()
        self._tool_api = tool_api
        self._tools: Dict[str, Dict[str, Dict[str, Any]]] = {}
        self._events: Dict[str, Dict[str, List[Callable[..., Any]]]] = {}

    # -- 内部 -------------------------------------------------------------

    def _api(self) -> Any:
        if self._tool_api is not None:
            return self._tool_api
        try:
            from nbot.services import tools as tool_api

            self._tool_api = tool_api
        except Exception as exc:  # 工具模块不可用时插件降级为「无工具」
            _log.error("[Plugins] 无法加载工具注册接口: %s", exc)
            raise RuntimeError(f"工具注册接口不可用: {exc}") from exc
        return self._tool_api

    @staticmethod
    def _plugin_id(plugin_id: str) -> str:
        pid = str(plugin_id or "").strip()
        if not pid:
            raise ValueError("plugin_id 不能为空")
        return pid

    # -- 工具 -------------------------------------------------------------

    def register_tool(
        self,
        plugin_id: str,
        name: str,
        spec: Dict[str, Any],
        handler: Callable[..., Any],
        *,
        read_only: bool = False,
    ) -> bool:
        """把插件工具注册进外部工具表；同名或非法时返回 False。"""
        pid = self._plugin_id(plugin_id)
        source = plugin_source(pid)
        try:
            api = self._api()
        except RuntimeError:
            return False
        with self._lock:
            ok = bool(
                api.register_external_tool(
                    name,
                    spec,
                    handler,
                    source=source,
                    read_only=read_only,
                )
            )
            if ok:
                self._tools.setdefault(pid, {})[str(name)] = {
                    "name": str(name),
                    "source": source,
                    "read_only": bool(read_only),
                }
            return ok

    def unregister_tool(self, plugin_id: str, name: str) -> bool:
        """注销插件单个工具。"""
        pid = self._plugin_id(plugin_id)
        source = plugin_source(pid)
        with self._lock:
            entry = self._tools.get(pid, {}).pop(str(name), None)
            if entry is None:
                return False
            try:
                self._api().unregister_external_tool(str(name), source=source)
            except Exception as exc:
                _log.warning("[Plugins] 注销工具 %s 失败: %s", name, exc)
                return False
            return True

    # -- 事件 -------------------------------------------------------------

    def register_event(
        self, plugin_id: str, event_name: str, handler: Callable[..., Any]
    ) -> bool:
        """登记插件对某个事件的订阅（权限校验由运行时负责）。"""
        pid = self._plugin_id(plugin_id)
        name = str(event_name or "").strip()
        if not name:
            raise ValueError("event_name 不能为空")
        if not callable(handler):
            raise ValueError("handler 必须可调用")
        with self._lock:
            handlers = self._events.setdefault(pid, {}).setdefault(name, [])
            if handler in handlers:
                return False
            handlers.append(handler)
            return True

    def unregister_event(
        self, plugin_id: str, event_name: str, handler: Optional[Callable[..., Any]] = None
    ) -> int:
        """注销事件订阅；不传 handler 时清空该事件的全部订阅，返回移除数量。"""
        pid = self._plugin_id(plugin_id)
        name = str(event_name or "").strip()
        with self._lock:
            handlers = self._events.get(pid, {}).get(name)
            if not handlers:
                return 0
            if handler is None:
                removed = len(handlers)
                self._events[pid].pop(name, None)
                return removed
            if handler not in handlers:
                return 0
            handlers.remove(handler)
            if not handlers:
                self._events[pid].pop(name, None)
            return 1

    def get_event_handlers(self, event_name: str) -> List[Callable[..., Any]]:
        """取出某事件的全部订阅（浅拷贝，避免调用方改动注册表）。"""
        name = str(event_name or "").strip()
        with self._lock:
            handler_lists = [
                handlers[name] for handlers in self._events.values() if name in handlers
            ]
            return [handler for handlers in handler_lists for handler in list(handlers)]

    # -- 查询与回收 -------------------------------------------------------

    def plugin_tool_names(self, plugin_id: str) -> List[str]:
        pid = self._plugin_id(plugin_id)
        with self._lock:
            return sorted(self._tools.get(pid, {}))

    def plugin_event_names(self, plugin_id: str) -> List[str]:
        pid = self._plugin_id(plugin_id)
        with self._lock:
            return sorted(self._events.get(pid, {}))

    def plugin_summary(self, plugin_id: str) -> Dict[str, Any]:
        pid = self._plugin_id(plugin_id)
        return {
            "tools": self.plugin_tool_names(pid),
            "events": self.plugin_event_names(pid),
        }

    def unregister_source(self, plugin_id: str) -> Dict[str, int]:
        """回收插件的全部工具与事件订阅，返回回收数量统计。"""
        pid = self._plugin_id(plugin_id)
        source = plugin_source(pid)
        with self._lock:
            removed_tools = 0
            try:
                removed_tools = int(self._api().unregister_source(source) or 0)
            except Exception as exc:
                _log.warning("[Plugins] 批量注销工具失败 plugin=%s: %s", pid, exc)
            self._tools.pop(pid, None)

            event_names = self._events.pop(pid, {})
            removed_events = sum(len(handlers) for handlers in event_names.values())
        return {
            "tools": removed_tools,
            "events": removed_events,
            "event_names": len(event_names),
        }

    def loaded_plugin_ids(self) -> Iterable[str]:
        """当前登记过工具或事件的插件 id。"""
        with self._lock:
            return sorted(set(self._tools) | set(self._events))

    def clear(self) -> None:
        """清空注册表（仅供测试）。"""
        with self._lock:
            self._tools.clear()
            self._events.clear()

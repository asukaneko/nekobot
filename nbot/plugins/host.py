"""插件宿主 API：白名单能力、权限校验与资源限额。

插件通过 ``register(ctx)`` 拿到 :class:`PluginContext`，只能用 ``ctx.api`` 暴露的能力：

    ctx.api.kv.get/set/delete/list
    ctx.api.fs.read_text/write_text/list/delete
    ctx.api.fs.workspace_read_text/workspace_write_text/workspace_list
    ctx.api.http.request
    ctx.api.events.subscribe/unsubscribe/emit
    ctx.api.tools.register
    ctx.api.session.get_messages
    ctx.api.message.send
    ctx.api.log.info/warn/error/debug

每次调用都走 :class:`nbot.plugins.permissions.PermissionGuard`，越权会计数并抛
``PermissionDenied`` / ``PluginSuspended``；插件拿不到 ``nbot.services.*``、
``web.server`` 等内部引用。
"""

import ipaddress
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from nbot.plugins import safety
from nbot.plugins.manifest import PluginManifest
from nbot.plugins.permissions import PermissionGuard, parse_permission
from nbot.plugins.registry import PluginRegistry

_log = logging.getLogger(__name__)
_audit = logging.getLogger("nbot.plugins.audit")

#: 插件 KV 总大小与键数上限
KV_MAX_BYTES = 1024 * 1024
KV_MAX_KEYS = 200
#: 单次文件读写上限
FS_MAX_BYTES = 1024 * 1024
#: 出网限额
HTTP_TIMEOUT_SECONDS = 10
HTTP_MAX_BYTES = 1024 * 1024
HTTP_MAX_HEADERS = 40
#: 会话读取上限
SESSION_MESSAGE_LIMIT = 200
#: 主动发消息长度上限
MESSAGE_MAX_CHARS = 4000
#: 事件回调 / 动作调用超时
EVENT_TIMEOUT_SECONDS = 2
ACTION_TIMEOUT_SECONDS = 10
#: 插件日志单文件上限
LOG_MAX_BYTES = 5 * 1024 * 1024

#: 出网重定向上限（逐跳重新校验，绝不自动跟随）
HTTP_MAX_REDIRECTS = 3
_REDIRECT_CODES = (301, 302, 303, 307, 308)


class HostAPIError(RuntimeError):
    """宿主 API 调用失败（参数非法、服务未接线、远端错误）。"""


class _NoRedirectHandler(urllib.request.HTTPRedirectHandler):
    """禁止自动跟随重定向：每一跳都由宿主重新做白名单与私网校验。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open_url(request: urllib.request.Request, timeout: float) -> Any:
    """用「不跟随重定向」的 opener 发请求（测试可替换）。"""
    opener = urllib.request.build_opener(_NoRedirectHandler)
    return opener.open(request, timeout=timeout)


def _resolve_host_ips(host: str, port: int) -> List[str]:
    """把主机名解析成 IP 列表；DNS 重绑定防护以解析结果为准。"""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError, OSError) as exc:
        raise HostAPIError(f"无法解析主机名: {host}") from exc
    ips: List[str] = []
    for info in infos:
        address = str(info[4][0])
        if address not in ips:
            ips.append(address)
    if not ips:
        raise HostAPIError(f"无法解析主机名: {host}")
    return ips


def _is_private_address(ip: str) -> bool:
    """私网 / 回环 / 链路本地 / 保留地址判定（IPv4 + IPv6，IPv4-mapped 归一）。"""
    try:
        address = ipaddress.ip_address(str(ip))
    except ValueError:
        return True
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return bool(
        address.is_private
        or address.is_loopback
        or address.is_link_local
        or address.is_reserved
        or address.is_multicast
        or address.is_unspecified
    )


@dataclass
class HostServices:
    """宿主服务注入点；未接线时对应 API 会抛 :class:`HostAPIError`。"""

    session_reader: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None
    message_sender: Optional[Callable[[str, str, str], bool]] = None
    workspace_resolver: Optional[Callable[[str], Optional[str]]] = None
    event_bus: Any = None


def run_with_timeout(func: Callable[..., Any], *args: Any, timeout: float) -> Any:
    """在线程里执行插件回调并加超时，避免单个插件卡死宿主。"""
    executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="plugin-call")
    try:
        future = executor.submit(func, *args)
        return future.result(timeout=timeout)
    except FuturesTimeout as exc:
        raise HostAPIError(f"插件回调超时（{timeout}s）") from exc
    finally:
        executor.shutdown(wait=False)


class _KVFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def get(self, key: str, default: Any = None) -> Any:
        return self._host.kv_get(key, default)

    def set(self, key: str, value: Any) -> None:
        self._host.kv_set(key, value)

    def delete(self, key: str) -> bool:
        return self._host.kv_delete(key)

    def list(self) -> List[str]:
        return self._host.kv_list()


class _FSFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def read_text(self, relative: str, encoding: str = "utf-8") -> str:
        return self._host.fs_read_text(relative, encoding=encoding)

    def write_text(self, relative: str, text: str) -> int:
        return self._host.fs_write_text(relative, text)

    def list(self, relative: str = ".") -> List[str]:
        return self._host.fs_list(relative)

    def delete(self, relative: str) -> bool:
        return self._host.fs_delete(relative)

    def workspace_read_text(self, session_id: str, relative: str) -> str:
        return self._host.workspace_read_text(session_id, relative)

    def workspace_write_text(self, session_id: str, relative: str, text: str) -> int:
        return self._host.workspace_write_text(session_id, relative, text)

    def workspace_list(self, session_id: str, relative: str = ".") -> List[str]:
        return self._host.workspace_list(session_id, relative)


class _HTTPFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Any = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        return self._host.http_request(method, url, headers=headers, body=body, timeout=timeout)


class _EventsFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def subscribe(self, event_name: str, handler: Callable[..., Any]) -> bool:
        return self._host.events_subscribe(event_name, handler)

    def unsubscribe(self, event_name: str, handler: Optional[Callable[..., Any]] = None) -> int:
        return self._host.events_unsubscribe(event_name, handler)

    def emit(self, event_name: str, payload: Optional[Dict[str, Any]] = None) -> int:
        return self._host.events_emit(event_name, payload)


class _ToolsFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def register(
        self,
        name: str,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        handler: Optional[Callable[..., Any]] = None,
        *,
        read_only: bool = False,
    ) -> bool:
        return self._host.tools_register(
            name,
            description=description,
            parameters=parameters,
            handler=handler,
            read_only=read_only,
        )

    def unregister(self, name: str) -> bool:
        return self._host.tools_unregister(name)


class _SessionFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def get_messages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        return self._host.session_get_messages(session_id, limit=limit)


class _MessageFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def send(self, channel: str, target: str, text: str) -> bool:
        return self._host.message_send(channel, target, text)


class _LogFacade:
    def __init__(self, host: "PluginHost"):
        self._host = host

    def info(self, message: str, *args: Any) -> None:
        self._host.log_write("info", message, args)

    def warn(self, message: str, *args: Any) -> None:
        self._host.log_write("warning", message, args)

    def error(self, message: str, *args: Any) -> None:
        self._host.log_write("error", message, args)

    def debug(self, message: str, *args: Any) -> None:
        self._host.log_write("debug", message, args)


class HostAPI:
    """``ctx.api`` 门面：只暴露白名单能力。"""

    def __init__(self, host: "PluginHost"):
        self.kv = _KVFacade(host)
        self.fs = _FSFacade(host)
        self.http = _HTTPFacade(host)
        self.events = _EventsFacade(host)
        self.tools = _ToolsFacade(host)
        self.session = _SessionFacade(host)
        self.message = _MessageFacade(host)
        self.log = _LogFacade(host)


class PluginHost:
    """单个插件的宿主实现。"""

    def __init__(
        self,
        *,
        plugin_id: str,
        manifest: PluginManifest,
        settings: Dict[str, Any],
        guard: PermissionGuard,
        registry: PluginRegistry,
        plugin_dir: str,
        data_dir: str,
        services: Optional[HostServices] = None,
        logger: Optional[logging.Logger] = None,
    ):
        self.plugin_id = plugin_id
        self.manifest = manifest
        self.settings = settings
        self.guard = guard
        self.registry = registry
        self.plugin_dir = os.path.abspath(plugin_dir)
        self.data_dir = os.path.abspath(data_dir)
        self.services = services or HostServices()
        self.log = logger or logging.getLogger(f"nbot.plugin.{plugin_id}")
        self.api = HostAPI(self)
        self._kv_lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._bus_subscriptions: List[Any] = []

    # -- 路径 -------------------------------------------------------------

    def _data_root(self) -> str:
        path = os.path.join(self.data_dir, self.plugin_id)
        os.makedirs(path, exist_ok=True)
        return path

    def _kv_file(self) -> str:
        return os.path.join(self._data_root(), "kv.json")

    def _log_file(self) -> str:
        return os.path.join(self._data_root(), "plugin.log")

    def workspace_root(self, session_id: str) -> str:
        """会话工作区下的 ``plugins/<id>/`` 目录。"""
        resolver = self.services.workspace_resolver
        if resolver is None:
            raise HostAPIError("工作区服务未接线，无法使用 fs:workspace")
        base = resolver(str(session_id or ""))
        if not base:
            raise HostAPIError(f"会话 {session_id} 没有工作区")
        root = os.path.join(os.path.abspath(base), "plugins", self.plugin_id)
        os.makedirs(root, exist_ok=True)
        return root

    # -- KV ---------------------------------------------------------------

    def _kv_load(self) -> Dict[str, Any]:
        try:
            with open(self._kv_file(), "r", encoding="utf-8") as handle:
                data = json.load(handle)
        except FileNotFoundError:
            return {}
        except (OSError, json.JSONDecodeError) as exc:
            _log.warning("[Plugins] 读取 KV 失败 plugin=%s: %s", self.plugin_id, exc)
            return {}
        return data if isinstance(data, dict) else {}

    def _kv_save(self, data: Dict[str, Any]) -> None:
        path = self._kv_file()
        tmp_path = f"{path}.{int(time.time() * 1000)}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as handle:
                json.dump(data, handle, ensure_ascii=False)
            os.replace(tmp_path, path)
        except OSError as exc:
            raise HostAPIError(f"写入 KV 失败: {exc}") from exc
        finally:
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    @staticmethod
    def _require_key(key: str) -> str:
        text = str(key or "").strip()
        if not text or len(text) > 128:
            raise HostAPIError("KV 键必须为 1-128 字符")
        return text

    def kv_get(self, key: str, default: Any = None) -> Any:
        self.guard.require("kv")
        name = self._require_key(key)
        with self._kv_lock:
            return self._kv_load().get(name, default)

    def kv_set(self, key: str, value: Any) -> None:
        self.guard.require("kv")
        name = self._require_key(key)
        with self._kv_lock:
            data = self._kv_load()
            data[name] = value
            if len(data) > KV_MAX_KEYS:
                raise HostAPIError(f"KV 键数超过上限（{KV_MAX_KEYS}）")
            try:
                payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
            except (TypeError, ValueError) as exc:
                raise HostAPIError(f"KV 值无法序列化为 JSON: {exc}") from exc
            if len(payload) > KV_MAX_BYTES:
                raise HostAPIError(f"KV 总大小超过 {KV_MAX_BYTES // 1024}KB 上限")
            self._kv_save(data)

    def kv_delete(self, key: str) -> bool:
        self.guard.require("kv")
        name = self._require_key(key)
        with self._kv_lock:
            data = self._kv_load()
            if name not in data:
                return False
            del data[name]
            self._kv_save(data)
            return True

    def kv_list(self) -> List[str]:
        self.guard.require("kv")
        with self._kv_lock:
            return sorted(self._kv_load())

    # -- 文件 -------------------------------------------------------------

    def _check_size(self, payload: bytes, label: str) -> None:
        if len(payload) > FS_MAX_BYTES:
            raise HostAPIError(f"{label} 超过 {FS_MAX_BYTES // 1024}KB 上限")

    def fs_read_text(self, relative: str, encoding: str = "utf-8") -> str:
        self.guard.require("fs:plugin")
        path = safety.resolve_inside(self.plugin_dir, relative)
        if not os.path.isfile(path):
            raise HostAPIError(f"文件不存在: {relative}")
        size = os.path.getsize(path)
        if size > FS_MAX_BYTES:
            raise HostAPIError(f"文件超过 {FS_MAX_BYTES // 1024}KB 上限: {relative}")
        with open(path, "r", encoding=encoding, errors="replace") as handle:
            return handle.read()

    def fs_write_text(self, relative: str, text: str) -> int:
        self.guard.require("fs:plugin")
        payload = str(text or "").encode("utf-8")
        self._check_size(payload, "写入内容")
        path = safety.resolve_inside(self.plugin_dir, relative)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return len(payload)

    def fs_list(self, relative: str = ".") -> List[str]:
        self.guard.require("fs:plugin")
        path = safety.resolve_inside(self.plugin_dir, relative)
        if not os.path.isdir(path):
            raise HostAPIError(f"目录不存在: {relative}")
        entries: List[str] = []
        for name in sorted(os.listdir(path)):
            if name.startswith("."):
                continue
            entries.append(name)
            if len(entries) >= 500:
                break
        return entries

    def fs_delete(self, relative: str) -> bool:
        self.guard.require("fs:plugin")
        path = safety.resolve_inside(self.plugin_dir, relative)
        if os.path.normcase(os.path.realpath(path)) == os.path.normcase(
            os.path.realpath(self.plugin_dir)
        ):
            raise HostAPIError("不允许删除插件根目录")
        if os.path.isfile(path):
            os.remove(path)
            return True
        if os.path.isdir(path):
            for root, dirs, files in os.walk(path, topdown=False):
                for name in files:
                    os.remove(os.path.join(root, name))
                for name in dirs:
                    os.rmdir(os.path.join(root, name))
            os.rmdir(path)
            return True
        return False

    def _workspace_path(self, session_id: str, relative: str) -> str:
        self.guard.require("fs:workspace")
        return safety.resolve_inside(self.workspace_root(session_id), relative)

    def workspace_read_text(self, session_id: str, relative: str) -> str:
        path = self._workspace_path(session_id, relative)
        if not os.path.isfile(path):
            raise HostAPIError(f"文件不存在: {relative}")
        if os.path.getsize(path) > FS_MAX_BYTES:
            raise HostAPIError(f"文件超过 {FS_MAX_BYTES // 1024}KB 上限: {relative}")
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()

    def workspace_write_text(self, session_id: str, relative: str, text: str) -> int:
        path = self._workspace_path(session_id, relative)
        payload = str(text or "").encode("utf-8")
        self._check_size(payload, "写入内容")
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as handle:
            handle.write(payload)
        return len(payload)

    def workspace_list(self, session_id: str, relative: str = ".") -> List[str]:
        path = self._workspace_path(session_id, relative)
        if not os.path.isdir(path):
            raise HostAPIError(f"目录不存在: {relative}")
        return sorted(name for name in os.listdir(path) if not name.startswith("."))[:500]

    # -- 网络 -------------------------------------------------------------

    def _validate_http_target(self, url: str) -> str:
        """校验单个请求目标：协议、白名单主机、解析后的 IP 是否为私网。

        返回主机名；任一检查失败直接抛错（fail-closed）。
        """
        parsed = urllib.parse.urlsplit(str(url or "").strip())
        if parsed.scheme not in ("http", "https") or not parsed.hostname:
            raise HostAPIError("只允许 http/https 且必须带主机名")
        host = parsed.hostname.lower()
        try:
            port = parsed.port or (443 if parsed.scheme == "https" else 80)
        except ValueError as exc:
            raise HostAPIError(f"URL 端口非法: {url}") from exc

        self.guard.require(f"net:{host}")
        for address in _resolve_host_ips(host, port):
            # 私网地址只有在 manifest 里显式声明了这个 IP 时才放行（防 DNS 重绑定）
            if _is_private_address(address) and f"net:{address}" not in self.guard.effective:
                raise HostAPIError(f"不允许访问内网地址: {host} -> {address}")
        return host

    def http_request(
        self,
        method: str,
        url: str,
        headers: Optional[Dict[str, str]] = None,
        body: Any = None,
        timeout: float = HTTP_TIMEOUT_SECONDS,
    ) -> Dict[str, Any]:
        """白名单出网请求：手动逐跳处理重定向，每跳重新校验，响应截断到 1MB。"""
        verb = str(method or "GET").upper()
        if verb not in ("GET", "POST", "PUT", "PATCH", "DELETE", "HEAD"):
            raise HostAPIError(f"不支持的 HTTP 方法: {verb}")
        clean_headers: Dict[str, str] = {}
        for key, value in (headers or {}).items():
            if len(clean_headers) >= HTTP_MAX_HEADERS:
                raise HostAPIError("请求头数量超过上限")
            clean_headers[str(key)] = str(value)
        data: Optional[bytes] = None
        if body is not None:
            data = body.encode("utf-8") if isinstance(body, str) else bytes(body)
            self._check_size(data, "请求体")

        limit = min(max(1.0, float(timeout or HTTP_TIMEOUT_SECONDS)), HTTP_TIMEOUT_SECONDS)
        current_url = str(url or "").strip()
        current_method = verb
        current_data = data
        redirects = 0

        while True:
            self._validate_http_target(current_url)
            request = urllib.request.Request(
                current_url, data=current_data, headers=clean_headers, method=current_method
            )
            try:
                with _open_url(request, limit) as response:
                    status = int(getattr(response, "status", 0) or 0)
                    raw = response.read(HTTP_MAX_BYTES + 1)
                    response_headers = dict(response.headers.items())
            except urllib.error.HTTPError as exc:
                if exc.code in _REDIRECT_CODES:
                    if redirects >= HTTP_MAX_REDIRECTS:
                        raise HostAPIError(
                            f"重定向次数超过上限（{HTTP_MAX_REDIRECTS}）"
                        ) from exc
                    location = exc.headers.get("Location") if exc.headers else None
                    if not location:
                        raise HostAPIError("重定向响应缺少 Location") from exc
                    next_url = urllib.parse.urljoin(current_url, location)
                    if (
                        urllib.parse.urlsplit(current_url).scheme == "https"
                        and urllib.parse.urlsplit(next_url).scheme != "https"
                    ):
                        raise HostAPIError("拒绝从 https 重定向到非 https") from exc
                    if exc.code == 303 or (
                        exc.code in (301, 302) and current_method not in ("GET", "HEAD")
                    ):
                        current_method = "GET"
                        current_data = None
                    redirects += 1
                    current_url = next_url
                    continue
                status = int(exc.code or 0)
                raw = exc.read(HTTP_MAX_BYTES + 1) if hasattr(exc, "read") else b""
                response_headers = dict(getattr(exc, "headers", {}) or {})
            except (urllib.error.URLError, OSError, ValueError) as exc:
                raise HostAPIError(f"请求失败: {exc}") from exc
            break

        truncated = len(raw) > HTTP_MAX_BYTES
        payload = raw[:HTTP_MAX_BYTES]
        return {
            "status": status,
            "headers": response_headers,
            "body": payload.decode("utf-8", errors="replace"),
            "truncated": truncated,
        }

    # -- 事件 -------------------------------------------------------------

    def events_subscribe(self, event_name: str, handler: Callable[..., Any]) -> bool:
        name = str(event_name or "").strip()
        parse_permission(f"event:{name}")
        self.guard.require(f"event:{name}")
        if not callable(handler):
            raise HostAPIError("handler 必须可调用")
        self.registry.register_event(self.plugin_id, name, handler)

        bus = self.services.event_bus
        if bus is not None:
            def _forward(event: Any) -> None:
                if isinstance(event, dict):
                    payload = dict(event)
                elif hasattr(event, "to_dict"):
                    payload = event.to_dict()
                else:
                    payload = {"type": getattr(event, "type", "")}
                try:
                    run_with_timeout(handler, payload, timeout=EVENT_TIMEOUT_SECONDS)
                except Exception as exc:  # 插件回调异常不能打断事件总线
                    _log.warning(
                        "[Plugins] 事件回调失败 plugin=%s event=%s: %s",
                        self.plugin_id,
                        name,
                        exc,
                    )

            subscription = bus.subscribe(name, _forward, name=f"plugin:{self.plugin_id}")
            self._bus_subscriptions.append((name, subscription))
        return True

    def events_unsubscribe(
        self, event_name: str, handler: Optional[Callable[..., Any]] = None
    ) -> int:
        name = str(event_name or "").strip()
        removed = self.registry.unregister_event(self.plugin_id, name, handler)
        bus = self.services.event_bus
        if bus is not None:
            remaining = [
                (topic, sub) for topic, sub in self._bus_subscriptions if topic == name
            ]
            self._bus_subscriptions = [
                (topic, sub) for topic, sub in self._bus_subscriptions if topic != name
            ]
            for topic, subscription in remaining:
                try:
                    bus.unsubscribe(topic, subscription)
                except Exception as exc:
                    _log.debug("[Plugins] 取消订阅失败 %s: %s", name, exc)
        return removed

    def events_emit(self, event_name: str, payload: Optional[Dict[str, Any]] = None) -> int:
        """插件只能在自己的 ``plugin.<id>.*`` 命名空间发事件。"""
        name = str(event_name or "").strip()
        prefix = f"plugin.{self.plugin_id}."
        if not name.startswith(prefix):
            raise HostAPIError(f"插件只能发 {prefix}* 命名空间的事件")
        bus = self.services.event_bus
        if bus is None:
            raise HostAPIError("事件总线未接线，无法 emit")
        from nbot.hooks.models import RuntimeEvent

        event = RuntimeEvent(
            type=name,
            source=f"plugin:{self.plugin_id}",
            payload=dict(payload or {}),
        )
        try:
            return int(_run_coroutine(bus.emit(event)) or 0)
        except Exception as exc:
            raise HostAPIError(f"事件发送失败: {exc}") from exc

    def close(self) -> None:
        """停用插件时回收总线订阅。"""
        bus = self.services.event_bus
        if bus is not None:
            for topic, subscription in self._bus_subscriptions:
                try:
                    bus.unsubscribe(topic, subscription)
                except Exception:
                    pass
        self._bus_subscriptions = []

    # -- 工具 -------------------------------------------------------------

    def tools_register(
        self,
        name: str,
        description: str = "",
        parameters: Optional[Dict[str, Any]] = None,
        handler: Optional[Callable[..., Any]] = None,
        *,
        read_only: bool = False,
    ) -> bool:
        self.guard.require("tool.register")
        tool_name = str(name or "").strip()
        expected_prefix = f"plugin_{self.plugin_id.replace('-', '_')}_"
        if not tool_name.startswith(expected_prefix):
            raise HostAPIError(f"插件工具名必须以 {expected_prefix} 开头")
        if not callable(handler):
            raise HostAPIError("工具 handler 必须可调用")
        spec = {
            "description": str(description or "")[:200],
            "parameters": parameters
            if isinstance(parameters, dict)
            else {"type": "object", "properties": {}},
        }
        return self.registry.register_tool(
            self.plugin_id, tool_name, spec, handler, read_only=read_only
        )

    def tools_unregister(self, name: str) -> bool:
        return self.registry.unregister_tool(self.plugin_id, str(name or "").strip())

    # -- 会话与消息 -------------------------------------------------------

    def session_get_messages(self, session_id: str, limit: int = 50) -> List[Dict[str, Any]]:
        self.guard.require("host:session.read")
        reader = self.services.session_reader
        if reader is None:
            raise HostAPIError("会话服务未接线，无法读取消息")
        sid = str(session_id or "").strip()
        if not sid:
            raise HostAPIError("session_id 不能为空")
        session = reader(sid) or {}
        if not isinstance(session, dict):
            raise HostAPIError(f"会话 {sid} 数据格式异常")
        messages = session.get("messages")
        if not isinstance(messages, list):
            return []
        max_count = min(max(1, int(limit or 50)), SESSION_MESSAGE_LIMIT)
        result: List[Dict[str, Any]] = []
        for item in messages[-max_count:]:
            if not isinstance(item, dict):
                continue
            message = {
                "role": str(item.get("role") or ""),
                "content": str(item.get("content") or "")[:4000],
            }
            if item.get("timestamp"):
                message["timestamp"] = item.get("timestamp")
            result.append(message)
        return result

    def message_send(self, channel: str, target: str, text: str) -> bool:
        self.guard.require("host:message.send")
        sender = self.services.message_sender
        if sender is None:
            raise HostAPIError("消息发送未接线")
        content = str(text or "")
        if not content.strip():
            raise HostAPIError("消息内容不能为空")
        if len(content) > MESSAGE_MAX_CHARS:
            raise HostAPIError(f"消息超过 {MESSAGE_MAX_CHARS} 字符上限")
        return bool(sender(str(channel or "").strip(), str(target or "").strip(), content))

    # -- 日志 -------------------------------------------------------------

    def log_write(self, level: str, message: str, args: Any = ()) -> None:
        text = str(message)
        if args:
            try:
                text = text % tuple(args)
            except (TypeError, ValueError):
                text = f"{text} {' '.join(str(item) for item in args)}"
        self.log.log(getattr(logging, str(level).upper(), logging.INFO), text)
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} [{level}] {text}\n"
        with self._log_lock:
            path = self._log_file()
            try:
                if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
                    os.replace(path, f"{path}.1")
                with open(path, "a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass


def _run_coroutine(awaitable: Any) -> Any:
    """在同步上下文里跑协程（已有事件循环时换线程）。"""
    import asyncio

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    box: Dict[str, Any] = {}

    def _target() -> None:
        try:
            box["value"] = asyncio.run(awaitable)
        except BaseException as exc:  # noqa: BLE001 - 需把异常带回调用线程
            box["error"] = exc

    thread = threading.Thread(target=_target, daemon=True, name="plugin-emit")
    thread.start()
    thread.join()
    if "error" in box:
        raise box["error"]
    return box.get("value")


@dataclass
class PluginContext:
    """传给插件 ``register(ctx)`` 的上下文。"""

    plugin_id: str
    manifest: PluginManifest
    settings: Dict[str, Any] = field(default_factory=dict)
    guard: Optional[PermissionGuard] = None
    registry: Optional[PluginRegistry] = None
    log: Optional[logging.Logger] = None
    api: Any = None
    host: Optional[PluginHost] = None
    actions: Dict[str, Callable[..., Any]] = field(default_factory=dict)

    @property
    def source(self) -> str:
        return f"plugin:{self.plugin_id}"

    def register_action(self, name: str, handler: Callable[..., Any]) -> None:
        """登记插件动作，供 ``plugin_use(action="call")`` 调用。"""
        action_name = str(name or "").strip()
        if not action_name:
            raise HostAPIError("动作名不能为空")
        if not callable(handler):
            raise HostAPIError("动作 handler 必须可调用")
        self.actions[action_name] = handler

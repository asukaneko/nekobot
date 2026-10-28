"""插件页面宿主：静态资源、CSP 与 postMessage 桥的后端转发。

前端壳（iframe 容器、插件页外壳）由「界面」负责；这里只提供：

- ``GET /api/plugins/<id>/ui/<path>``：插件目录内静态资源（后缀白名单 + CSP + no-store）；
- ``POST /api/plugins/<id>/bridge``：面板页面转发的桥请求，白名单方法 + 鉴权（沿用路由层）
  + 权限校验 + 每分钟 30 次限流。

iframe 使用 ``sandbox="allow-scripts allow-forms"``（不给 ``allow-same-origin``），
页面无法直接带 cookie 请求后端，所有数据必须经 ``postMessage`` 交给面板再转发到桥。
"""

import logging
import mimetypes
import os
import secrets
import threading
import time
from collections import deque
from typing import Any, Deque, Dict

from flask import jsonify, redirect, request, send_file, url_for
from itsdangerous import BadSignature, URLSafeTimedSerializer

from nbot.plugins import safety
from nbot.plugins.host import HostAPIError
from nbot.plugins.permissions import PermissionDenied, PluginSuspended
from nbot.plugins.runtime import PluginError, PluginNotFound, get_plugin_runtime

_log = logging.getLogger(__name__)

#: 桥允许的方法（全部转后端做鉴权与权限校验）
BRIDGE_METHODS = frozenset(
    {
        "kv.get",
        "kv.set",
        "kv.delete",
        "kv.list",
        "http.request",
        "session.get_messages",
        "tool.invoke",
        "ui.notify",
        "ui.resize",
    }
)
#: 桥限流：每插件 + 来源地址每分钟次数
BRIDGE_RATE_LIMIT = 30
BRIDGE_RATE_WINDOW = 60.0
UI_ASSET_TOKEN_MAX_AGE = 3600
#: 插件页面 CSP：禁止内联脚本与外部直连，只允许同源静态资源
PLUGIN_CSP = (
    "default-src 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self'; "
    "connect-src 'none'; "
    "base-uri 'none'; "
    "form-action 'none'; "
    "frame-ancestors 'self'"
)

ASSET_MIME_FALLBACK = "application/octet-stream"
_FORBIDDEN_ASSET_SUFFIXES = (".py", ".pyc", ".pyo")

_rate_lock = threading.Lock()
_rate_buckets: Dict[str, Deque[float]] = {}


def _allow_request(key: str) -> bool:
    """滑动窗口限流。"""
    now = time.time()
    with _rate_lock:
        bucket = _rate_buckets.setdefault(key, deque())
        while bucket and now - bucket[0] > BRIDGE_RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= BRIDGE_RATE_LIMIT:
            return False
        bucket.append(now)
        return True


def _reset_rate_limit() -> None:
    """清空限流状态（测试用）。"""
    with _rate_lock:
        _rate_buckets.clear()


def _asset_allowed(relative: str) -> bool:
    lowered = relative.lower()
    if lowered.endswith(_FORBIDDEN_ASSET_SUFFIXES):
        return False
    return safety.is_allowed_suffix(relative)


def _plugin_page_or_error(runtime, plugin_id: str):
    """校验插件已启用、声明页面且有效权限包含 ui.page。"""
    detail = runtime.get_plugin(plugin_id)
    if not detail.get("loaded"):
        raise PluginNotFound(f"插件 {plugin_id} 未启用")
    page = (detail.get("ui") or {}).get("page")
    if not page:
        raise PluginNotFound(f"插件 {plugin_id} 没有提供页面")
    if "ui.page" not in (detail.get("effective_permissions") or []):
        raise PermissionDenied(plugin_id, "ui.page")
    return detail, page


def _bridge_dispatch(runtime, host, plugin_id: str, method: str, params: Dict[str, Any]) -> Any:
    """执行桥方法（全部经宿主 API，天然带权限校验）。"""
    if method == "kv.get":
        return host.api.kv.get(params.get("key", ""), params.get("default"))
    if method == "kv.set":
        host.api.kv.set(params.get("key", ""), params.get("value"))
        return {"saved": True}
    if method == "kv.delete":
        return {"deleted": host.api.kv.delete(params.get("key", ""))}
    if method == "kv.list":
        return {"keys": host.api.kv.list()}
    if method == "http.request":
        return host.api.http.request(
            str(params.get("method") or "GET"),
            str(params.get("url") or ""),
            headers=params.get("headers") or {},
            body=params.get("body"),
            timeout=float(params.get("timeout") or 10),
        )
    if method == "session.get_messages":
        return host.api.session.get_messages(
            str(params.get("session_id") or ""),
            limit=int(params.get("limit") or 50),
        )
    if method == "tool.invoke":
        action_name = str(params.get("action") or "").strip()
        if not action_name:
            raise HostAPIError("tool.invoke 缺少 action")
        arguments = params.get("arguments") or {}
        if not isinstance(arguments, dict):
            raise HostAPIError("tool.invoke 的 arguments 必须是对象")
        return runtime.invoke_action(plugin_id, action_name, arguments)
    if method == "ui.notify":
        return {"notified": True, "message": str(params.get("message") or "")[:200]}
    if method == "ui.resize":
        # 插件页长内容在固定高度 iframe 里会出双滚动条，由壳按 height 调整 iframe
        try:
            height = int(params.get("height"))
        except (TypeError, ValueError) as exc:
            raise HostAPIError("ui.resize 需要整数 height") from exc
        if height < 80 or height > 2000:
            raise HostAPIError("ui.resize 的 height 必须在 80-2000 之间")
        return {"notified": True, "height": height}
    raise HostAPIError(f"桥方法不在白名单: {method}")


def _bridge_error(payload_id: Any, exc: Exception):
    if isinstance(exc, PermissionDenied):
        return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 403
    if isinstance(exc, PluginSuspended):
        return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 403
    if isinstance(exc, PluginNotFound):
        return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 404
    if isinstance(exc, (HostAPIError, PluginError, ValueError)):
        return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 400
    _log.error("[Plugins] 桥请求失败: %s", exc, exc_info=True)
    return jsonify({"ok": False, "id": payload_id, "error": f"桥请求失败: {exc}"}), 500


def register_plugin_ui_routes(app, server=None):
    """注册插件页面与桥路由。"""
    runtime = get_plugin_runtime()
    asset_tokens = URLSafeTimedSerializer(
        app.config.get("SECRET_KEY") or secrets.token_urlsafe(32),
        salt="nbot-plugin-ui-assets-v1",
    )

    def _serve_asset(plugin_id: str, asset_path: str):
        try:
            detail, page = _plugin_page_or_error(runtime, plugin_id)
        except PluginNotFound as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except PermissionDenied as exc:
            return jsonify({"success": False, "error": str(exc)}), 403

        relative = asset_path or page
        if not _asset_allowed(relative):
            return jsonify({"success": False, "error": "不允许的资源类型"}), 404
        try:
            absolute = safety.resolve_inside(detail["path"], relative)
        except safety.UnsafePathError:
            return jsonify({"success": False, "error": "资源路径非法"}), 404
        if not os.path.isfile(absolute):
            return jsonify({"success": False, "error": "资源不存在"}), 404

        mimetype = mimetypes.guess_type(absolute)[0] or ASSET_MIME_FALLBACK
        response = send_file(absolute, mimetype=mimetype, conditional=True)
        response.headers["Content-Security-Policy"] = PLUGIN_CSP
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Cache-Control"] = "no-store"
        response.headers["Access-Control-Allow-Origin"] = "*"
        return response

    @app.route("/api/plugins/<plugin_id>/ui/", defaults={"asset_path": ""})
    @app.route("/api/plugins/<plugin_id>/ui/<path:asset_path>")
    def plugin_ui_asset(plugin_id: str, asset_path: str = ""):
        try:
            _detail, page = _plugin_page_or_error(runtime, plugin_id)
        except PluginNotFound as exc:
            return jsonify({"success": False, "error": str(exc)}), 404
        except PermissionDenied as exc:
            return jsonify({"success": False, "error": str(exc)}), 403
        relative = asset_path or page
        if relative != page:
            return _serve_asset(plugin_id, relative)
        # opaque iframe 的子资源不携带登录 Cookie；令牌只授权当前插件的静态资源。
        token = asset_tokens.dumps({"plugin_id": plugin_id})
        response = redirect(url_for(
            "plugin_ui_asset_token", plugin_id=plugin_id, asset_token=token, asset_path=page,
        ))
        response.headers["Cache-Control"] = "no-store"
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    @app.route("/plugin-assets/<plugin_id>/<asset_token>/<path:asset_path>")
    def plugin_ui_asset_token(plugin_id: str, asset_token: str, asset_path: str):
        try:
            claims = asset_tokens.loads(asset_token, max_age=UI_ASSET_TOKEN_MAX_AGE)
        except BadSignature:
            return jsonify({"success": False, "error": "插件资源地址已失效"}), 403
        if not isinstance(claims, dict) or claims.get("plugin_id") != plugin_id:
            return jsonify({"success": False, "error": "插件资源地址不匹配"}), 403
        return _serve_asset(plugin_id, asset_path)

    @app.route("/api/plugins/<plugin_id>/bridge", methods=["POST"])
    def plugin_bridge(plugin_id: str):
        origin = request.headers.get("Origin")
        if origin:
            expected = request.host_url.rstrip("/")
            if origin.rstrip("/") != expected:
                return jsonify({"ok": False, "error": "来源不被允许"}), 403

        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({"ok": False, "error": "请求体必须是 JSON 对象"}), 400
        payload_id = payload.get("id")
        method = str(payload.get("method") or "").strip()
        params = payload.get("params") or {}
        if method not in BRIDGE_METHODS:
            return jsonify({"ok": False, "id": payload_id, "error": "桥方法不在白名单"}), 400
        if not isinstance(params, dict):
            return jsonify({"ok": False, "id": payload_id, "error": "params 必须是对象"}), 400

        try:
            _plugin_page_or_error(runtime, plugin_id)
        except PluginNotFound as exc:
            return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 404
        except PermissionDenied as exc:
            return jsonify({"ok": False, "id": payload_id, "error": str(exc)}), 403

        host = runtime.host_for(plugin_id)
        if host is None:
            return jsonify({"ok": False, "id": payload_id, "error": "插件未加载"}), 409

        limit_key = f"{plugin_id}:{request.remote_addr or 'local'}"
        if not _allow_request(limit_key):
            return jsonify({"ok": False, "id": payload_id, "error": "桥请求过于频繁"}), 429

        try:
            result = _bridge_dispatch(runtime, host, plugin_id, method, params)
        except PluginSuspended as exc:
            _log.warning("[Plugins] 插件越权超限，自动停用: %s", plugin_id)
            try:
                runtime.disable(plugin_id)
            except Exception as disable_exc:
                _log.error("[Plugins] 自动停用 %s 失败: %s", plugin_id, disable_exc)
            return _bridge_error(payload_id, exc)
        except Exception as exc:  # noqa: BLE001 - 统一转桥错误
            return _bridge_error(payload_id, exc)

        return jsonify({"ok": True, "id": payload_id, "result": result})

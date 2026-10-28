"""插件管理路由：列表 / 详情 / ZIP 安装 / 启停 / 重载 / 卸载 / 设置 / 权限收紧。

宿主 API、iframe 沙箱与 ``plugin_use`` 工具在第二波接入；本模块只做管理面。
"""

import logging
from datetime import datetime
from typing import Any, Dict, Optional, Tuple

from flask import jsonify, request

from nbot.plugins import safety
from nbot.plugins.host import HostAPIError, HostServices
from nbot.plugins.manifest import ManifestError
from nbot.plugins.permissions import PermissionFormatError
from nbot.plugins.runtime import (
    PluginError,
    PluginInstallError,
    PluginLoadError,
    PluginNotFound,
    PluginStateError,
    get_plugin_runtime,
)
from nbot.web.plugin_host import register_plugin_ui_routes

_log = logging.getLogger(__name__)


def _hook_event_bus() -> Optional[Any]:
    """取 HookManager 的对话事件总线（best-effort，失败则插件事件只进注册表）。"""
    try:
        from nbot.hooks.manager import get_hook_manager

        return getattr(get_hook_manager(), "_event_bus", None)
    except Exception as exc:  # 未初始化时忽略
        _log.debug("[Plugins] 事件总线不可用: %s", exc)
        return None


def _build_message_sender(session_store: Any):
    """插件主动发消息：当前只支持 Web 会话，其它频道明确报错。"""
    if session_store is None:
        return None

    def _send(channel: str, target: str, text: str) -> bool:
        if channel not in ("", "web", "web_session", "session"):
            raise HostAPIError(f"暂不支持向 {channel} 频道发送消息")
        if not target:
            raise HostAPIError("消息目标会话不能为空")
        if session_store.get_session(target) is None:
            raise HostAPIError(f"会话不存在: {target}")
        session_store.append_message(
            target,
            {
                "role": "assistant",
                "content": text,
                "timestamp": datetime.now().isoformat(),
                "sender": "AI",
                "source": "plugin",
            },
        )
        return True

    return _send


def configure_plugin_services(runtime, server) -> None:
    """把 Web 面板的会话 / 工作区 / 事件总线注入插件宿主动 API。"""
    if server is None:
        return
    session_store = getattr(server, "session_store", None)
    workspace_manager = getattr(server, "workspace_manager", None)
    services = HostServices(
        session_reader=(
            (lambda sid: session_store.get_session(sid)) if session_store is not None else None
        ),
        message_sender=_build_message_sender(session_store),
        workspace_resolver=(
            (lambda sid: workspace_manager.get_workspace(sid))
            if workspace_manager is not None
            else None
        ),
        event_bus=_hook_event_bus(),
    )
    runtime.configure_services(services)


def _error(message: str, status: int = 400):
    return jsonify({"success": False, "error": message}), status


def _failure(exc: Exception) -> Tuple[Any, int]:
    """把运行时异常映射成 HTTP 状态码。"""
    if isinstance(exc, PluginNotFound):
        return _error(str(exc), 404)
    if isinstance(
        exc,
        (PluginInstallError, PluginLoadError, ManifestError, PermissionFormatError),
    ):
        return _error(str(exc), 400)
    if isinstance(exc, PluginStateError):
        return _error(str(exc), 500)
    if isinstance(exc, PluginError):
        return _error(str(exc), 400)
    _log.error("[Plugins] 未处理异常: %s", exc, exc_info=True)
    return _error(f"插件操作失败: {exc}", 500)


def register_plugin_routes(app, server=None):
    """注册插件管理路由（含插件页面与 postMessage 桥）。"""
    runtime = get_plugin_runtime()
    configure_plugin_services(runtime, server)
    register_plugin_ui_routes(app, server)

    @app.route("/api/plugins", methods=["GET"])
    def list_plugins():
        try:
            payload = runtime.list_plugins()
        except Exception as exc:  # noqa: BLE001 - 统一转 HTTP 错误
            return _failure(exc)
        return jsonify({"success": True, **payload})

    @app.route("/api/plugins/<plugin_id>", methods=["GET"])
    def get_plugin(plugin_id: str):
        try:
            detail = runtime.get_plugin(plugin_id)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/install", methods=["POST"])
    def install_plugin():
        upload = request.files.get("file") if request.files else None
        if upload is None:
            return _error("未选择插件包（multipart 字段 file）")

        overwrite_raw = request.form.get("overwrite", request.args.get("overwrite", ""))
        overwrite = str(overwrite_raw).strip().lower() in ("1", "true", "yes", "on")

        blob = upload.read(safety.MAX_PLUGIN_ZIP_BYTES + 1)
        if len(blob) > safety.MAX_PLUGIN_ZIP_BYTES:
            limit_mb = safety.MAX_PLUGIN_ZIP_BYTES // (1024 * 1024)
            return _error(f"插件包超过 {limit_mb}MB 上限")

        try:
            detail = runtime.install_from_zip(blob, overwrite=overwrite)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/<plugin_id>/enable", methods=["POST"])
    def enable_plugin(plugin_id: str):
        try:
            detail = runtime.enable(plugin_id)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/<plugin_id>/disable", methods=["POST"])
    def disable_plugin(plugin_id: str):
        try:
            detail = runtime.disable(plugin_id)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/<plugin_id>/reload", methods=["POST"])
    def reload_plugin(plugin_id: str):
        try:
            detail = runtime.reload(plugin_id)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/<plugin_id>", methods=["DELETE"])
    def uninstall_plugin(plugin_id: str):
        def _flag(name: str) -> bool:
            raw = request.args.get(name, "")
            return str(raw).strip().lower() in ("1", "true", "yes", "on")

        keep_state = _flag("keep_state")
        delete_data = _flag("delete_data")
        try:
            result = runtime.uninstall(
                plugin_id, keep_state=keep_state, delete_data=delete_data
            )
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, **result})

    @app.route("/api/plugins/<plugin_id>/settings", methods=["PUT"])
    def update_plugin_settings(plugin_id: str):
        data = request.get_json(silent=True) or {}
        values: Optional[Dict[str, Any]] = data.get("settings")
        if values is None:
            values = data
        if not isinstance(values, dict):
            return _error("settings 必须是对象")
        try:
            detail = runtime.update_settings(plugin_id, values)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

    @app.route("/api/plugins/<plugin_id>/permissions", methods=["PUT"])
    def update_plugin_permissions(plugin_id: str):
        data = request.get_json(silent=True) or {}
        if "permissions" not in data:
            return _error("缺少 permissions 字段（传 null 表示恢复全量声明）")
        permissions = data.get("permissions")
        if permissions is not None and not isinstance(permissions, list):
            return _error("permissions 必须是字符串数组或 null")
        try:
            detail = runtime.update_permissions(plugin_id, permissions)
        except Exception as exc:  # noqa: BLE001
            return _failure(exc)
        return jsonify({"success": True, "plugin": detail})

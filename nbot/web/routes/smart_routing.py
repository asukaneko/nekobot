"""
智能模型路由 Web API

路由：
    GET    /api/smart-routing/settings      — 读取路由设置
    POST   /api/smart-routing/settings      — 保存路由设置
    GET    /api/smart-routing/budget        — 预算使用情况
    GET    /api/smart-routing/analytics     — 路由分析（按模型聚合）
    GET    /api/smart-routing/logs          — 决策日志
    POST   /api/smart-routing/logs/clear    — 清空决策日志
"""

from __future__ import annotations

import logging

from flask import jsonify, request

_log = logging.getLogger(__name__)


def _data_dir(server) -> str:
    return getattr(server, "data_dir", "data/web") or "data/web"


def register_smart_routing_routes(app, server):

    @app.route("/api/smart-routing/settings", methods=["GET"])
    def get_routing_settings():
        from nbot.core.smart_routing import get_smart_router
        router = get_smart_router(_data_dir(server))
        router.load_settings()
        return jsonify({"settings": router.settings_dict()})

    @app.route("/api/smart-routing/settings", methods=["POST"])
    def save_routing_settings():
        from nbot.core.smart_routing import get_smart_router
        data = request.json or {}
        router = get_smart_router(_data_dir(server))
        updated = router.update_settings(data)
        # 持久化到 settings.json 的 smart_routing 键
        try:
            if not isinstance(server.settings, dict):
                server.settings = {}
            server.settings["smart_routing"] = updated
            server._save_data("settings")
        except Exception as exc:
            _log.error("[SmartRouting] save settings failed: %s", exc)
        return jsonify({"ok": True, "settings": updated})

    @app.route("/api/smart-routing/budget")
    def get_routing_budget():
        from nbot.core.smart_routing import get_smart_router
        router = get_smart_router(_data_dir(server))
        router.load_settings()
        return jsonify(router.budget_status())

    @app.route("/api/smart-routing/analytics")
    def get_routing_analytics():
        from nbot.core.smart_routing import get_smart_router
        router = get_smart_router(_data_dir(server))
        return jsonify(router.analytics())

    @app.route("/api/smart-routing/logs")
    def get_routing_logs():
        from nbot.core.smart_routing import get_smart_router
        limit = int(request.args.get("limit", 200))
        router = get_smart_router(_data_dir(server))
        logs = router.decision_logs(limit=limit)
        return jsonify({"logs": logs, "total": len(logs)})

    @app.route("/api/smart-routing/logs/clear", methods=["POST"])
    def clear_routing_logs():
        from nbot.core.smart_routing import get_smart_router
        router = get_smart_router(_data_dir(server))
        router.clear_logs()
        return jsonify({"ok": True})

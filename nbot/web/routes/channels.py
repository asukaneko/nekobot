import re
import uuid
from datetime import datetime

from flask import jsonify, request

from nbot.channels.registry import channel_registry
from nbot.services.telegram_service import (
    answer_telegram_update,
    resolve_config_secret,
    set_telegram_webhook,
)

CHANNEL_ID_RE = re.compile(r"^[a-zA-Z0-9_-]{2,64}$")

TELEGRAM_PRESET = {
    "id": "telegram",
    "name": "Telegram",
    "type": "telegram",
    "transport": "webhook",
    "description": "Telegram Bot Webhook 频道",
    "enabled": True,
    "config": {
        "bot_token_env": "TELEGRAM_BOT_TOKEN",
        "secret_token_env": "TELEGRAM_WEBHOOK_SECRET",
        "webhook_url": "",
    },
    "capabilities": {
        "supports_stream": False,
        "supports_progress_updates": False,
        "supports_file_send": False,
        "supports_stop": False,
    },
}


def _now():
    return datetime.now().isoformat()


def _normalize_channel(data, *, existing=None, builtin=False):
    existing = existing or {}
    channel_id = str(data.get("id") or existing.get("id") or uuid.uuid4()).strip()
    name = str(data.get("name") or existing.get("name") or channel_id).strip()
    channel_type = str(data.get("type") or existing.get("type") or "custom").strip().lower()
    transport = str(data.get("transport") or existing.get("transport") or "").strip().lower()
    config = data.get("config", existing.get("config", {}))
    capabilities = data.get("capabilities", existing.get("capabilities", {}))

    if not CHANNEL_ID_RE.match(channel_id):
        raise ValueError("频道 ID 必须为 2-64 个字符，只允许字母、数字、下划线和中划线")
    if not name:
        raise ValueError("频道名称不能为空")
    if config is None:
        config = {}
    if capabilities is None:
        capabilities = {}
    if not isinstance(config, dict):
        raise ValueError("频道配置必须是 JSON 对象")
    if not isinstance(capabilities, dict):
        raise ValueError("频道能力必须是 JSON 对象")

    created_at = existing.get("created_at") or data.get("created_at") or _now()
    return {
        "id": channel_id,
        "name": name,
        "type": channel_type,
        "transport": transport,
        "description": str(data.get("description", existing.get("description", ""))),
        "enabled": bool(data.get("enabled", existing.get("enabled", True))),
        "builtin": bool(existing.get("builtin", builtin)),
        "config": config,
        "capabilities": capabilities,
        "created_at": created_at,
        "updated_at": _now(),
    }


def _find_channel(server, channel_id):
    for channel in server.channels_config:
        if channel.get("id") == channel_id:
            return channel
    return None


def _save_channels(server):
    server._save_data("channels")


def _telegram_webhook_path(channel_id):
    return f"/api/channels/telegram/{channel_id}/webhook"


def _public_preset(preset):
    item = dict(preset)
    item["config"] = dict(preset.get("config") or {})
    return item


def register_channel_routes(app, server):
    @app.route("/api/channels/presets")
    def get_channel_presets():
        return jsonify({"presets": [_public_preset(TELEGRAM_PRESET)]})

    @app.route("/api/channels/presets/<preset_id>", methods=["POST"])
    def create_channel_from_preset(preset_id):
        if preset_id != "telegram":
            return jsonify({"error": "频道预设不存在"}), 404

        data = dict(TELEGRAM_PRESET)
        data.update(request.json or {})
        data.setdefault("id", TELEGRAM_PRESET["id"])
        data.setdefault("config", dict(TELEGRAM_PRESET["config"]))
        data.setdefault("capabilities", dict(TELEGRAM_PRESET["capabilities"]))
        try:
            channel = _normalize_channel(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if _find_channel(server, channel["id"]):
            return jsonify({"error": "频道 ID 已存在"}), 409

        server.channels_config.append(channel)
        _save_channels(server)
        return jsonify({"success": True, "channel": channel})

    @app.route("/api/channels")
    def get_channels():
        registered = set(channel_registry.list_adapters())
        channels = []
        for channel in server.channels_config:
            item = dict(channel)
            item["registered"] = item.get("id") in registered
            if item.get("type") == "telegram":
                item["webhook_path"] = _telegram_webhook_path(item.get("id"))
            channels.append(item)
        return jsonify(
            {
                "channels": channels,
                "registered_adapters": sorted(registered),
                "registered_handlers": channel_registry.list_handlers(),
            }
        )

    @app.route("/api/channels", methods=["POST"])
    def create_channel():
        data = request.json or {}
        try:
            channel = _normalize_channel(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if _find_channel(server, channel["id"]):
            return jsonify({"error": "频道 ID 已存在"}), 409

        server.channels_config.append(channel)
        _save_channels(server)
        return jsonify({"success": True, "channel": channel})

    @app.route("/api/channels/<channel_id>", methods=["PUT"])
    def update_channel(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel:
            return jsonify({"error": "频道不存在"}), 404

        data = request.json or {}
        data["id"] = channel_id
        try:
            updated = _normalize_channel(data, existing=channel)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        channel.clear()
        channel.update(updated)
        _save_channels(server)
        return jsonify({"success": True, "channel": channel})

    @app.route("/api/channels/<channel_id>", methods=["DELETE"])
    def delete_channel(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel:
            return jsonify({"error": "频道不存在"}), 404
        if channel.get("builtin"):
            return jsonify({"error": "内置频道不能删除"}), 400

        server.channels_config = [
            item for item in server.channels_config if item.get("id") != channel_id
        ]
        _save_channels(server)
        return jsonify({"success": True})

    @app.route("/api/channels/<channel_id>/toggle", methods=["POST"])
    def toggle_channel(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel:
            return jsonify({"error": "频道不存在"}), 404
        channel["enabled"] = not bool(channel.get("enabled", True))
        channel["updated_at"] = _now()
        _save_channels(server)
        return jsonify({"success": True, "enabled": channel["enabled"]})

    @app.route("/api/channels/telegram/<channel_id>/webhook", methods=["POST"])
    def telegram_webhook(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "telegram":
            return jsonify({"error": "Telegram 频道不存在"}), 404
        if channel.get("enabled") is False:
            return jsonify({"error": "Telegram 频道未启用"}), 403

        config = channel.get("config") or {}
        expected_secret = resolve_config_secret(
            config, "secret_token", "secret_token_env"
        )
        if (config.get("secret_token") or config.get("secret_token_env")) and not expected_secret:
            return jsonify({"error": "Telegram webhook secret 未配置或环境变量为空"}), 500
        if expected_secret:
            request_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token", "")
            if request_secret != expected_secret:
                return jsonify({"error": "Telegram webhook secret 不匹配"}), 403

        update = request.get_json(silent=True) or {}
        server.socketio.start_background_task(answer_telegram_update, server, channel, update)
        return jsonify({"ok": True})

    @app.route("/api/channels/telegram/<channel_id>/set-webhook", methods=["POST"])
    def telegram_set_webhook(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "telegram":
            return jsonify({"error": "Telegram 频道不存在"}), 404

        payload = request.json or {}
        config = dict(channel.get("config") or {})
        webhook_url = str(payload.get("webhook_url") or config.get("webhook_url") or "").strip()
        if not webhook_url:
            return jsonify({"error": "请先填写 webhook_url"}), 400

        try:
            result = set_telegram_webhook(config, webhook_url)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400
        except Exception as exc:
            return jsonify({"error": f"设置 Telegram Webhook 失败：{exc}"}), 502
        return jsonify({"success": True, "result": result})

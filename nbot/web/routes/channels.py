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
from nbot.services.feishu_service import (
    answer_feishu_event,
    handle_feishu_challenge,
    verify_feishu_request,
)
from nbot.services.feishu_ws_service import (
    feishu_ws_service,
    handle_feishu_message,
    resolve_feishu_ws_credentials,
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

FEISHU_PRESET = {
    "id": "feishu",
    "name": "飞书",
    "type": "feishu",
    "transport": "webhook",
    "description": "飞书自建应用 Webhook 频道，支持接收和发送消息。直接填写 app_id 和 app_secret，或使用环境变量",
    "enabled": True,
    "config": {
        "app_id": "",
        "app_secret": "",
        "encrypt_key": "",
        "verification_token": "",
        "webhook_url": "",
    },
    "capabilities": {
        "supports_stream": False,
        "supports_progress_updates": False,
        "supports_file_send": False,
        "supports_stop": False,
    },
}

FEISHU_WS_PRESET = {
    "id": "feishu_ws",
    "name": "飞书 (长连接)",
    "type": "feishu_ws",
    "transport": "websocket",
    "description": "飞书自建应用 WebSocket 长连接频道，无需公网地址即可接收消息。直接填写 app_id 和 app_secret，或使用环境变量",
    "enabled": True,
    "config": {
        "app_id": "",
        "app_secret": "",
        "encrypt_key": "",
        "verification_token": "",
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


def _feishu_webhook_path(channel_id):
    return f"/api/channels/feishu/{channel_id}/webhook"


def _start_feishu_ws_client(server, channel):
    """启动飞书 WebSocket 客户端"""
    try:
        channel_id = channel.get("id")
        config = channel.get("config") or {}
        credentials = resolve_feishu_ws_credentials(config)

        if not credentials["app_id"] or not credentials["app_secret"]:
            print(f"[FeishuWS] 频道 {channel_id} 未配置 App ID 或 App Secret，跳过启动")
            return False

        # 检查是否已经在运行
        if feishu_ws_service.is_running(channel_id):
            print(f"[FeishuWS] 频道 {channel_id} WebSocket 客户端已在运行")
            return True

        # 设置服务器实例（如果未设置）
        if not feishu_ws_service._server:
            feishu_ws_service.set_server(server)

        # 启动客户端
        success = feishu_ws_service.start_client(
            channel_id=channel_id,
            app_id=credentials["app_id"],
            app_secret=credentials["app_secret"],
            encrypt_key=credentials["encrypt_key"] or None,
            verification_token=credentials["verification_token"] or None,
        )

        if success:
            print(f"[FeishuWS] 频道 {channel_id} WebSocket 客户端启动成功")
        else:
            print(f"[FeishuWS] 频道 {channel_id} WebSocket 客户端启动失败")

        return success

    except Exception as e:
        print(f"[FeishuWS] 启动客户端失败: {e}")
        import traceback
        traceback.print_exc()
        return False


def _stop_feishu_ws_client(channel_id):
    """停止飞书 WebSocket 客户端"""
    try:
        feishu_ws_service.stop_client(channel_id)
        print(f"[FeishuWS] 频道 {channel_id} WebSocket 客户端已停止")
        return True
    except Exception as e:
        print(f"[FeishuWS] 停止客户端失败: {e}")
        return False


def auto_start_feishu_ws_clients(server):
    """自动启动所有已启用的飞书长连接频道

    在服务器启动时调用，自动连接所有配置好的飞书长连接频道
    """
    started_count = 0

    for channel in server.channels_config:
        if channel.get("type") == "feishu_ws" and channel.get("enabled"):
            channel_id = channel.get("id")
            config = channel.get("config") or {}
            credentials = resolve_feishu_ws_credentials(config)

            if not credentials["app_id"] or not credentials["app_secret"]:
                continue

            # 检查是否已经在运行
            if feishu_ws_service.is_running(channel_id):
                continue

            # 启动客户端
            success = feishu_ws_service.start_client(
                channel_id=channel_id,
                app_id=credentials["app_id"],
                app_secret=credentials["app_secret"],
                encrypt_key=credentials["encrypt_key"] or None,
                verification_token=credentials["verification_token"] or None,
            )

            if success:
                started_count += 1


def _public_preset(preset):
    item = dict(preset)
    item["config"] = dict(preset.get("config") or {})
    return item


def register_channel_routes(app, server):
    @app.route("/api/channels/presets")
    def get_channel_presets():
        return jsonify({"presets": [
            _public_preset(TELEGRAM_PRESET),
            _public_preset(FEISHU_PRESET),
            _public_preset(FEISHU_WS_PRESET),
        ]})

    @app.route("/api/channels/presets/<preset_id>", methods=["POST"])
    def create_channel_from_preset(preset_id):
        if preset_id == "telegram":
            preset = TELEGRAM_PRESET
        elif preset_id == "feishu":
            preset = FEISHU_PRESET
        elif preset_id == "feishu_ws":
            preset = FEISHU_WS_PRESET
        else:
            return jsonify({"error": "频道预设不存在"}), 404

        data = dict(preset)
        data.update(request.json or {})
        data.setdefault("id", preset["id"])
        data.setdefault("config", dict(preset["config"]))
        data.setdefault("capabilities", dict(preset["capabilities"]))
        try:
            channel = _normalize_channel(data)
        except ValueError as exc:
            return jsonify({"error": str(exc)}), 400

        if _find_channel(server, channel["id"]):
            return jsonify({"error": "频道 ID 已存在"}), 409

        server.channels_config.append(channel)
        _save_channels(server)

        # 如果是飞书长连接预设，自动启动 WebSocket 客户端
        if preset_id == "feishu_ws" and channel.get("enabled"):
            _start_feishu_ws_client(server, channel)

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
            elif item.get("type") == "feishu":
                item["webhook_path"] = _feishu_webhook_path(item.get("id"))
            elif item.get("type") == "feishu_ws":
                # 飞书长连接模式显示连接状态
                item["ws_connected"] = feishu_ws_service.is_running(item.get("id"))
            channels.append(item)
        return jsonify(
            {
                "channels": channels,
                "registered_adapters": sorted(registered),
                "registered_handlers": channel_registry.list_handlers(),
                "feishu_ws_running": feishu_ws_service.list_running_clients(),
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

        # 如果是飞书长连接频道且已启用，自动启动 WebSocket 客户端
        if channel.get("type") == "feishu_ws" and channel.get("enabled"):
            _start_feishu_ws_client(server, channel)

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

        # 如果是飞书长连接频道，先停止现有的 WebSocket 客户端
        if channel.get("type") == "feishu_ws" and feishu_ws_service.is_running(channel_id):
            _stop_feishu_ws_client(channel_id)

        channel.clear()
        channel.update(updated)
        _save_channels(server)

        # 如果是飞书长连接频道且已启用，重新启动 WebSocket 客户端
        if channel.get("type") == "feishu_ws" and channel.get("enabled"):
            _start_feishu_ws_client(server, channel)

        return jsonify({"success": True, "channel": channel})

    @app.route("/api/channels/<channel_id>", methods=["DELETE"])
    def delete_channel(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel:
            return jsonify({"error": "频道不存在"}), 404
        if channel.get("builtin"):
            return jsonify({"error": "内置频道不能删除"}), 400

        # 如果是飞书长连接频道，先停止 WebSocket 客户端
        if channel.get("type") == "feishu_ws":
            _stop_feishu_ws_client(channel_id)

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

        # 处理飞书长连接频道的启动/停止
        if channel.get("type") == "feishu_ws":
            if channel["enabled"]:
                _start_feishu_ws_client(server, channel)
            else:
                _stop_feishu_ws_client(channel_id)

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

    @app.route("/api/channels/feishu-ws/<channel_id>/start", methods=["POST"])
    def feishu_ws_start(channel_id):
        """手动启动飞书 WebSocket 客户端"""
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "feishu_ws":
            return jsonify({"error": "飞书长连接频道不存在"}), 404
        if channel.get("enabled") is False:
            return jsonify({"error": "飞书长连接频道未启用"}), 403

        success = _start_feishu_ws_client(server, channel)
        if success:
            return jsonify({"success": True, "message": "WebSocket 客户端已启动"})
        else:
            return jsonify({"error": "启动 WebSocket 客户端失败"}), 500

    @app.route("/api/channels/feishu-ws/<channel_id>/stop", methods=["POST"])
    def feishu_ws_stop(channel_id):
        """手动停止飞书 WebSocket 客户端"""
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "feishu_ws":
            return jsonify({"error": "飞书长连接频道不存在"}), 404

        success = _stop_feishu_ws_client(channel_id)
        if success:
            return jsonify({"success": True, "message": "WebSocket 客户端已停止"})
        else:
            return jsonify({"error": "停止 WebSocket 客户端失败"}), 500

    @app.route("/api/channels/feishu-ws/<channel_id>/status", methods=["GET"])
    def feishu_ws_status(channel_id):
        """获取飞书 WebSocket 连接状态"""
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "feishu_ws":
            return jsonify({"error": "飞书长连接频道不存在"}), 404

        is_running = feishu_ws_service.is_running(channel_id)
        return jsonify({
            "success": True,
            "channel_id": channel_id,
            "connected": is_running,
            "enabled": channel.get("enabled", True),
        })

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

    @app.route("/api/channels/feishu/<channel_id>/webhook", methods=["POST"])
    def feishu_webhook(channel_id):
        channel = _find_channel(server, channel_id)
        if not channel or channel.get("type") != "feishu":
            return jsonify({"error": "飞书频道不存在"}), 404
        if channel.get("enabled") is False:
            return jsonify({"error": "飞书频道未启用"}), 403

        config = channel.get("config") or {}

        # 获取请求头
        signature = request.headers.get("X-Lark-Signature", "")
        timestamp = request.headers.get("X-Lark-Request-Timestamp", "")
        nonce = request.headers.get("X-Lark-Request-Nonce", "")

        # 获取原始请求体
        body = request.get_data(as_text=True)

        # 验证请求签名（如果配置了加密密钥）
        if not verify_feishu_request(config, signature, timestamp, nonce, body):
            return jsonify({"error": "飞书请求签名验证失败"}), 403

        # 解析请求数据
        data = request.get_json(silent=True) or {}

        # 处理URL验证请求（challenge）
        if "challenge" in data:
            try:
                result = handle_feishu_challenge(config, data)
                return jsonify(result)
            except ValueError as exc:
                return jsonify({"error": str(exc)}), 403

        # 处理事件回调
        event = data.get("event")
        if event:
            server.socketio.start_background_task(answer_feishu_event, server, channel, data)

        return jsonify({"ok": True})

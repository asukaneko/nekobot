import json
import logging
import os
import time
import sys
import importlib
from collections import defaultdict
from datetime import datetime, timedelta

from flask import jsonify, request


_log = logging.getLogger(__name__)


def register_admin_misc_routes(app, server):
    @app.route("/api/commands")
    def get_commands_catalog():
        try:
            from nbot.commands import command_handlers

            category_names = {
                "1": "漫画相关",
                "2": "聊天设置",
                "3": "娱乐功能",
                "4": "系统处理",
                "5": "群聊管理",
                "6": "轻小说",
                "7": "定时任务",
                "8": "帮助",
            }

            commands = []
            seen = set()
            for aliases, handler in command_handlers.items():
                alias_list = [alias for alias in aliases if isinstance(alias, str) and alias.startswith("/")]
                if not alias_list:
                    continue

                primary = alias_list[0]
                if primary in seen:
                    continue
                seen.add(primary)

                help_text = getattr(handler, "help_text", "") or primary
                commands.append(
                    {
                        "name": primary,
                        "aliases": alias_list[1:],
                        "help_text": help_text,
                        "category": getattr(handler, "category", "0"),
                        "category_name": category_names.get(getattr(handler, "category", "0"), "其他"),
                        "admin_only": bool(getattr(handler, "admin_show", False)),
                    }
                )

            commands.sort(key=lambda item: (item["category"], item["name"]))
            return jsonify({"commands": commands})
        except Exception as e:
            _log.error(f"Failed to load commands catalog: {e}", exc_info=True)
            return jsonify({"commands": [], "error": str(e)}), 500

    @app.route("/api/startup-status")
    def get_startup_status():
        return jsonify(
            {
                "ready": bool(server.startup_ready),
                "error": server.startup_error,
                "bot_connected": bool(getattr(server, "bot", None)),
            }
        )

    @app.route("/api/tokens")
    def get_token_stats():
        date_range = request.args.get("dateRange", "today")

        token_stats_file = os.path.join(server.data_dir, "token_stats.json")
        real_stats = {}
        if os.path.exists(token_stats_file):
            try:
                with open(token_stats_file, "r", encoding="utf-8") as f:
                    real_stats = json.load(f)
            except Exception:
                real_stats = {}

        stats_data = {**server.token_stats, **real_stats}
        history = stats_data.get("history", [])
        today_str = datetime.now().strftime("%Y-%m-%d")

        if date_range == "today":
            history = [h for h in history if h.get("date") == today_str]
        elif date_range == "7d":
            cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
            history = [h for h in history if h.get("date", "") >= cutoff]
        elif date_range == "30d":
            cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
            history = [h for h in history if h.get("date", "") >= cutoff]

        today_input = 0
        today_output = 0
        for entry in history:
            today_input += entry.get("input", 0)
            today_output += entry.get("output", 0)

        return jsonify(
            {
                "today": stats_data.get("today", 0),
                "month": stats_data.get("month", 0),
                "total": stats_data.get("total", 0),
                "today_input": today_input,
                "today_output": today_output,
                "history": history,
            }
        )

    @app.route("/api/tokens/record", methods=["POST"])
    def record_token_usage():
        data = request.json or {}
        tokens = data.get("tokens", 0)
        server.token_stats["today"] += tokens
        server.token_stats["month"] += tokens
        return jsonify({"success": True})

    @app.route("/api/logs")
    def get_logs():
        level = request.args.get("level", "all")
        limit = request.args.get("limit", 100, type=int)
        logs = server.system_logs
        if level != "all":
            logs = [l for l in logs if l["level"] == level]
        return jsonify(logs[-limit:])

    @app.route("/api/logs", methods=["POST"])
    def add_log():
        data = request.json or {}
        log = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": data.get("level", "info"),
            "message": data.get("message", ""),
        }
        server.system_logs.append(log)
        if len(server.system_logs) > 1000:
            server.system_logs = server.system_logs[-1000:]
        server._save_data("logs")
        return jsonify({"success": True})

    @app.route("/api/logs", methods=["DELETE"])
    def clear_logs():
        server.system_logs = []
        server._save_data("logs")
        return jsonify({"success": True})

    @app.route("/api/settings")
    def get_settings():
        return jsonify(server.settings)

    @app.route("/api/settings", methods=["PUT"])
    def update_settings():
        data = request.json or {}
        server.settings.update(data)
        server._save_data("settings")
        return jsonify({"success": True, "settings": server.settings})

    @app.route("/api/stats")
    def get_stats():
        current_time = time.time()
        if (
            hasattr(server, "_stats_cache")
            and hasattr(server, "_stats_cache_time")
            and (current_time - server._stats_cache_time) < server._stats_cache_ttl
        ):
            return jsonify(server._stats_cache)

        today = datetime.now().strftime("%Y-%m-%d")
        today_active_users = set()
        ai_calls = 0
        file_transfers = 0
        
        # 计算今日消息数和总消息数
        today_messages = 0
        total_messages = 0

        try:
            qq_data_dir = os.path.join(server.base_dir, "data", "qq")
            for qq_type in ["private", "group"]:
                qq_dir = os.path.join(qq_data_dir, qq_type)
                if not os.path.exists(qq_dir):
                    continue
                for filename in os.listdir(qq_dir):
                    if not filename.endswith(".json"):
                        continue
                    file_path = os.path.join(qq_dir, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            messages = json.load(f)
                        for msg in messages:
                            if msg.get("role") != "system":
                                total_messages += 1
                                today_active_users.add(f"{qq_type}:{filename[:-5]}")
                                # 检查是否是今日消息
                                msg_time = msg.get("time", 0)
                                if msg_time:
                                    from datetime import datetime as dt
                                    try:
                                        msg_date = dt.fromtimestamp(msg_time).strftime("%Y-%m-%d")
                                        if msg_date == today:
                                            today_messages += 1
                                    except:
                                        pass
                    except Exception:
                        pass
        except Exception:
            pass

        for session in server.sessions.values():
            if session.get("type", "web") == "web":
                for msg in session.get("messages", []):
                    if msg.get("role") != "system":
                        total_messages += 1
                        today_active_users.add(f"web:{session.get('id', 'unknown')}")
                        # 检查是否是今日消息
                        try:
                            msg_time = msg.get("timestamp", "")
                            if msg_time:
                                msg_date = msg_time[:10]  # ISO format date part
                                if msg_date == today:
                                    today_messages += 1
                        except:
                            pass

        uptime = int(current_time - server.start_time)

        stats = {
            "today_messages": today_messages,
            "total_messages": total_messages,
            "active_sessions": len(server.sessions),
            "token_usage": server.token_stats.get("today", 0),
            "qq_connected": bool(getattr(server, "qq_bot", None)),
            "ai_service_status": "normal" if server.ai_client else "not_configured",
            "platform_count": 1,
            "uptime": uptime,
            "today_active_users": len(today_active_users),
            "ai_calls": ai_calls,
            "file_transfers": file_transfers,
            "avg_response_time": "1.2",
        }

        server._stats_cache = stats
        server._stats_cache_time = current_time
        return jsonify(stats)

    @app.route("/api/stats/messages")
    def get_message_stats():
        period = request.args.get("period", "day")
        now = datetime.now()
        stats = defaultdict(int)

        if period == "day":
            for i in range(24):
                hour_time = now - timedelta(hours=i)
                stats[hour_time.strftime("%H:00")] = 0
            for session in server.sessions.values():
                for msg in session.get("messages", []):
                    try:
                        msg_time = datetime.fromisoformat(msg.get("timestamp", "").replace("Z", "+00:00"))
                        if now - msg_time <= timedelta(hours=24):
                            stats[msg_time.strftime("%H:00")] += 1
                    except Exception:
                        pass
            sorted_stats = sorted(stats.items(), key=lambda x: x[0])
            return jsonify({"labels": [item[0] for item in sorted_stats], "values": [item[1] for item in sorted_stats]})

        if period == "week":
            for i in range(7):
                day_time = now - timedelta(days=i)
                stats[day_time.strftime("%m/%d")] = 0
            for session in server.sessions.values():
                for msg in session.get("messages", []):
                    try:
                        msg_time = datetime.fromisoformat(msg.get("timestamp", "").replace("Z", "+00:00"))
                        if now - msg_time <= timedelta(days=7):
                            stats[msg_time.strftime("%m/%d")] += 1
                    except Exception:
                        pass
            sorted_stats = sorted(stats.items(), key=lambda x: datetime.strptime(x[0], "%m/%d"))
            return jsonify({"labels": [item[0] for item in sorted_stats], "values": [item[1] for item in sorted_stats]})

        if period == "month":
            for i in range(30):
                day_time = now - timedelta(days=i)
                stats[day_time.strftime("%m/%d")] = 0
            for session in server.sessions.values():
                for msg in session.get("messages", []):
                    try:
                        msg_time = datetime.fromisoformat(msg.get("timestamp", "").replace("Z", "+00:00"))
                        if now - msg_time <= timedelta(days=30):
                            stats[msg_time.strftime("%m/%d")] += 1
                    except Exception:
                        pass
            sorted_stats = sorted(stats.items(), key=lambda x: datetime.strptime(x[0], "%m/%d"))
            return jsonify({"labels": [item[0] for item in sorted_stats], "values": [item[1] for item in sorted_stats]})

        return jsonify({"labels": [], "values": []})

    @app.route("/api/stats/platforms")
    def get_platform_stats():
        platform_stats = defaultdict(int)
        platform_stats["QQ 私聊"] = 0
        platform_stats["QQ 群聊"] = 0
        platform_stats["Web 会话"] = 0

        try:
            qq_data_dir = os.path.join(server.base_dir, "data", "qq")
            for qq_type, label in [("private", "QQ 私聊"), ("group", "QQ 群聊")]:
                qq_dir = os.path.join(qq_data_dir, qq_type)
                if not os.path.exists(qq_dir):
                    continue
                for filename in os.listdir(qq_dir):
                    if not filename.endswith('.json'):
                        continue
                    file_path = os.path.join(qq_dir, filename)
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            messages = json.load(f)
                        non_system_msgs = [m for m in messages if m.get('role') != 'system']
                        platform_stats[label] += len(non_system_msgs)
                    except Exception as e:
                        _log.warning(f"读取 QQ 消息文件失败 {filename}: {e}")
        except Exception as e:
            _log.error(f"统计 QQ 平台消息失败: {e}")

        for session in server.sessions.values():
            if session.get("type", "web") == "web":
                platform_stats["Web 会话"] += len(session.get("messages", []))

        colors = {"QQ 私聊": "#667eea", "QQ 群聊": "#4facfe", "Web 会话": "#43e97b"}
        result = [
            {"name": name, "value": value, "itemStyle": {"color": colors.get(name, "#999")}}
            for name, value in platform_stats.items()
            if value > 0
        ]
        if not result:
            result = [
                {"name": "QQ 私聊", "value": 0, "itemStyle": {"color": "#667eea"}},
                {"name": "QQ 群聊", "value": 0, "itemStyle": {"color": "#4facfe"}},
                {"name": "Web 会话", "value": 0, "itemStyle": {"color": "#43e97b"}},
            ]
        return jsonify(result)

    @app.route("/api/system/reload-core", methods=["POST"])
    def reload_core_modules():
        """重载所有核心代码模块"""
        try:
            reloaded_modules = []
            failed_modules = []
            
            # 定义要重载的核心模块
            core_modules = [
                # 核心服务
                "nbot.core.session_store",
                "nbot.core.workflow",
                "nbot.core.workspace",
                "nbot.core.progress_card",
                "nbot.core.agent_service",
                "nbot.core.message",
                "nbot.core.model_adapter",
                "nbot.core.prompt_format",
                "nbot.core.prompt",
                "nbot.core.file_parser",
                # 服务层
                "nbot.services.chat_service",
                "nbot.services.ai",
                "nbot.services.tools",
                # Web 层 - 路由
                "nbot.web.routes.sessions",
                "nbot.web.routes.files",
                "nbot.web.routes.workflows",
                "nbot.web.routes.admin_misc",
                "nbot.web.routes.auth",
                "nbot.web.routes.ai_config",
                "nbot.web.routes.ai_models",
                "nbot.web.routes.tools",
                "nbot.web.routes.skills",
                "nbot.web.routes.memory",
                "nbot.web.routes.personality",
                "nbot.web.routes.knowledge",
                "nbot.web.routes.voice",
                "nbot.web.routes.live2d",
                "nbot.web.routes.web_agent",
                "nbot.web.routes.task_center",
                "nbot.web.routes.channels",
                "nbot.web.routes.heartbeat",
                "nbot.web.routes.qq_overview",
                "nbot.web.routes.api_keys",
                "nbot.web.routes.config_legacy",
                "nbot.web.routes.skills_storage",
                "nbot.web.routes.workspace_misc",
                "nbot.web.routes.workspace_private",
                "nbot.web.routes.workspace_shared",
                # Web 层 - 其他
                "nbot.web.ai_service",
                "nbot.web.agent_tools",
                "nbot.web.socket_events",
                "nbot.web.persistence",
                "nbot.web.sessions_db",
                "nbot.web.message_adapter",
                # 工具函数
                "nbot.web.utils.config_loader",
                # 插件
                "nbot.plugins.dispatcher",
                "nbot.plugins.manager",
                "nbot.plugins.skills.loader",
                "nbot.plugins.skills.dynamic_skill",
                # 频道
                "nbot.channels.base",
                "nbot.channels.qq",
                "nbot.channels.web",
                "nbot.channels.telegram",
                "nbot.channels.registry",
                "nbot.channels.configured",
            ]
            
            for module_name in core_modules:
                try:
                    if module_name in sys.modules:
                        importlib.reload(sys.modules[module_name])
                        reloaded_modules.append(module_name)
                        _log.info(f"已重载模块: {module_name}")
                    else:
                        # 模块尚未加载，尝试导入
                        importlib.import_module(module_name)
                        reloaded_modules.append(f"{module_name} (新导入)")
                        _log.info(f"已导入模块: {module_name}")
                except Exception as e:
                    failed_modules.append({"module": module_name, "error": str(e)})
                    _log.error(f"重载模块失败 {module_name}: {e}")
            
            # 注意：Flask 应用一旦开始处理请求，就不能再动态添加路由
            # 路由配置的修改需要重启服务才能生效
            # 这里我们只重载模块代码，让业务逻辑层的修改生效
            
            # 记录系统日志
            server.log_message(
                "info", 
                f"核心代码重载完成: 成功 {len(reloaded_modules)} 个, 失败 {len(failed_modules)} 个",
                important=True
            )
            
            return jsonify({
                "success": True,
                "message": "核心代码重载完成",
                "reloaded": reloaded_modules,
                "failed": failed_modules,
                "reloaded_count": len(reloaded_modules),
                "failed_count": len(failed_modules)
            })
        except Exception as e:
            _log.error(f"重载核心代码时发生错误: {e}", exc_info=True)
            return jsonify({
                "success": False,
                "error": str(e),
                "message": "重载核心代码失败"
            }), 500

    @app.route("/api/system/reload-config", methods=["POST"])
    def reload_config():
        """重载系统配置"""
        try:
            # 重新加载配置
            server._load_config()
            server._load_skills()
            server._load_tools()
            server._load_personality()
            server._load_heartbeat()
            
            server.log_message("info", "系统配置已重载", important=True)
            return jsonify({"success": True, "message": "配置已重载"})
        except Exception as e:
            _log.error(f"重载配置失败: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

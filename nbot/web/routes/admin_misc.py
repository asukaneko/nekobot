import json
import logging
import os
import time
import sys
import importlib
import csv
import io
from collections import defaultdict
from datetime import datetime, timedelta

from flask import Response, jsonify, request

from nbot.web.log_cleanup import cleanup_logs_dir, normalize_log_cleanup_settings


_log = logging.getLogger(__name__)


def _get_token_today(server) -> int:
    """安全读取今日 token 用量。"""
    try:
        from nbot.core.token_stats import get_token_stats_manager
        return get_token_stats_manager().data.get("today", 0)
    except RuntimeError:
        return 0


def register_admin_misc_routes(app, server):
    def _normalize_settings():
        if not isinstance(server.settings, dict):
            server.settings = {}
        default_features = {
            "skills_prompt_injection": False,
            "live2d": True,
        }
        features = dict(default_features)
        features.update(server.settings.get("features") or {})
        server.settings["features"] = features
        normalize_log_cleanup_settings(server.settings)
        return server.settings

    def _sync_bot_config_to_ini(data: dict):
        """将 web 界面修改的 BotConfig 写回 config.ini，重启后生效。"""
        config_map = {
            "bot_id": "bot_uin",
            "master_id": "root",
            "ws_uri": "ws_uri",
        }
        changed = {}
        for settings_key, ini_key in config_map.items():
            if settings_key in data:
                changed[ini_key] = str(data[settings_key] or "")

        if not changed:
            return

        try:
            import configparser
            config = configparser.ConfigParser()
            config.read("config.ini", encoding="utf-8")
            if not config.has_section("BotConfig"):
                config.add_section("BotConfig")
            for ini_key, value in changed.items():
                config.set("BotConfig", ini_key, value)
            with open("config.ini", "w", encoding="utf-8") as f:
                config.write(f)
            _log.info(f"已同步设置到 config.ini: {list(changed.keys())}")
        except Exception as e:
            _log.error(f"同步设置到 config.ini 失败: {e}")

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
        start_date = request.args.get("startDate", "").strip()
        end_date = request.args.get("endDate", "").strip()

        def _valid_date(value: str) -> bool:
            if not value:
                return True
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return True
            except ValueError:
                return False

        if not _valid_date(start_date) or not _valid_date(end_date):
            return jsonify({"success": False, "error": "日期格式必须为 YYYY-MM-DD"}), 400
        if start_date and end_date and start_date > end_date:
            return jsonify({"success": False, "error": "开始日期不能晚于结束日期"}), 400

        try:
            from nbot.core.token_stats import get_token_stats_manager

            manager = get_token_stats_manager()
            return jsonify(manager.get_stats(
                date_range,
                start_date=start_date,
                end_date=end_date,
            ))
        except RuntimeError:
            # TokenStatsManager 未初始化，返回空数据
            return jsonify({
                "today": 0, "month": 0, "total": 0,
                "total_tokens": 0, "today_input": 0, "today_output": 0,
                "message_count": 0, "avg_tokens_per_msg": 0,
                "estimated_cost": "0.00", "active_sessions": 0,
                "avg_response_time": "0", "history": [],
                "recent_records": [], "records": [],
                "sessions": {}, "models": {}, "users": {},
            })

    @app.route("/api/tokens/rankings")
    def get_token_rankings():
        try:
            from nbot.core.token_stats import get_token_stats_manager
            from nbot.web.sessions_db import load_sessions as load_sessions_from_db

            manager = get_token_stats_manager()
            # 构造会话存在性判断：已删除的会话不纳入排行榜
            sessions_meta = manager.data.get("sessions", {}) or {}
            # 合并内存与 DB 中的所有 web 可见会话 ID
            known_session_ids = set(server.sessions.keys())
            try:
                db_sessions = load_sessions_from_db(server.data_dir) or {}
                known_session_ids.update(db_sessions.keys())
            except Exception:
                pass

            def _is_session_active(session_id: str) -> bool:
                # 会话在内存或 DB 中存在：保留
                if session_id in known_session_ids:
                    return True
                # 不在内存/DB 中，但属于非 web 渠道（QQ/CLI/Feishu 等），无显式删除逻辑，保留
                sdata = sessions_meta.get(session_id) or {}
                if isinstance(sdata, dict):
                    ctype = (sdata.get("type") or "").strip().lower()
                    if ctype and ctype not in ("web", "api"):
                        return True
                # web 渠道但已不在内存/DB 中：视为已删除
                return False

            return jsonify(manager.get_rankings(is_session_active=_is_session_active))
        except RuntimeError:
            return jsonify({"sessions": [], "models": [], "users": []})

    @app.route("/api/tokens/export")
    def export_token_records():
        date_range = request.args.get("dateRange", "today")
        start_date = request.args.get("startDate", "").strip()
        end_date = request.args.get("endDate", "").strip()

        def _valid_date(value: str) -> bool:
            if not value:
                return True
            try:
                datetime.strptime(value, "%Y-%m-%d")
                return True
            except ValueError:
                return False

        if not _valid_date(start_date) or not _valid_date(end_date):
            return jsonify({"success": False, "error": "日期格式必须为 YYYY-MM-DD"}), 400
        if start_date and end_date and start_date > end_date:
            return jsonify({"success": False, "error": "开始日期不能晚于结束日期"}), 400

        try:
            from nbot.core.token_stats import get_token_stats_manager

            records = get_token_stats_manager().get_records(
                date_range,
                start_date=start_date,
                end_date=end_date,
            )
        except RuntimeError:
            records = []

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow([
            "时间",
            "日期",
            "来源",
            "用途",
            "模型",
            "会话",
            "频道",
            "用户",
            "输入Token",
            "缓存命中Token",
            "输出Token",
            "总计",
            "费用",
            "耗时ms",
            "记录ID",
        ])
        try:
            from nbot.core.token_stats import PURPOSE_LABELS

            def _purpose_label(value):
                return PURPOSE_LABELS.get(str(value or ""), str(value or ""))
        except Exception:
            def _purpose_label(value):
                return str(value or "")
        for item in records:
            writer.writerow([
                item.get("timestamp", ""),
                item.get("date", ""),
                item.get("source") or item.get("channel_type") or "api",
                _purpose_label(item.get("purpose", "")),
                item.get("model") or "unknown",
                item.get("session_id", ""),
                item.get("channel_type", ""),
                item.get("user_id", ""),
                item.get("input", 0),
                item.get("cached_tokens", 0),
                item.get("output", 0),
                item.get("total", (item.get("input", 0) or 0) + (item.get("output", 0) or 0)),
                item.get("cost", 0),
                item.get("duration_ms", ""),
                item.get("id", ""),
            ])

        period = (
            f"{start_date or 'start'}_{end_date or 'end'}"
            if start_date or end_date
            else date_range
        )
        filename = f"token_usage_records_{period}_{datetime.now().strftime('%Y%m%d%H%M%S')}.csv"
        csv_body = "\ufeff" + output.getvalue()
        return Response(
            csv_body,
            mimetype="text/csv; charset=utf-8",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/api/tokens/record", methods=["POST"])
    def record_token_usage():
        """已废弃：token 用量由各频道的回调统一管理。"""
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

    @app.route("/api/logs/cleanup", methods=["POST"])
    def cleanup_log_files():
        _normalize_settings()
        result = cleanup_logs_dir(server.base_dir, server.settings.get("log_cleanup") or {})
        cleanup = server.settings.get("log_cleanup") or {}
        cleanup.update(
            {
                "last_run": datetime.now().isoformat(),
                "last_deleted_count": result.get("deleted_count", 0),
                "last_deleted_entries": result.get("deleted_entries", 0),
                "last_freed_bytes": result.get("freed_bytes", 0),
                "last_error": result.get("error", ""),
            }
        )
        server.settings["log_cleanup"] = cleanup
        system_logs_result = result.get("system_logs") or {}
        token_stats_result = result.get("token_stats") or {}
        if system_logs_result.get("data") is not None:
            server.system_logs = system_logs_result["data"]
        if token_stats_result.get("data") is not None:
            try:
                from nbot.core.token_stats import get_token_stats_manager

                manager = get_token_stats_manager()
                with manager._lock:
                    manager._stats = token_stats_result["data"]
                server.token_stats = manager.data
            except Exception:
                pass
        server._save_data("settings")
        if result.get("deleted_count", 0):
            server.log_message(
                "info",
                f"Log cleanup deleted {result['deleted_count']} files, freed {result['freed_bytes']} bytes",
            )
        elif result.get("deleted_entries", 0):
            server.log_message(
                "info",
                f"Log cleanup deleted {result['deleted_entries']} JSON entries, freed {result['freed_bytes']} bytes",
            )
        return jsonify(result)

    @app.route("/api/settings")
    def get_settings():
        return jsonify(_normalize_settings())

    @app.route("/api/settings", methods=["PUT"])
    def update_settings():
        _normalize_settings()
        data = request.json or {}
        skip_log_cleanup = bool(data.pop("_skip_log_cleanup", False))
        if "features" in data and isinstance(data.get("features"), dict):
            merged_features = dict(server.settings.get("features") or {})
            merged_features.update(data["features"])
            data = dict(data)
            data["features"] = merged_features
        server.settings.update(data)
        _normalize_settings()
        server._save_data("settings")
        if not skip_log_cleanup and server.settings.get("log_cleanup", {}).get("enabled"):
            cleanup_log_files()

        # 将 BotConfig 相关设置写回 config.ini
        _sync_bot_config_to_ini(data)

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
            "token_usage": _get_token_today(server),
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
            # 重新加载所有持久化数据（settings、skills、tools、personality 等）
            server._load_all_data()
            server._load_personality()
            server._load_ai_config()
            server._load_ai_models()

            server.log_message("info", "系统配置已重载", important=True)
            return jsonify({"success": True, "message": "配置已重载"})
        except Exception as e:
            _log.error(f"重载配置失败: {e}")
            return jsonify({"success": False, "error": str(e)}), 500

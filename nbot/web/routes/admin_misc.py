import json
import logging
import os
import time
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
            if entry.get("date") == today_str:
                today_input = entry.get("input", 0)
                today_output = entry.get("output", 0)
                break

        if date_range == "today":
            period_total = today_input + today_output
            period_input = today_input
            period_output = today_output
        else:
            period_total = sum((h.get("input", 0) + h.get("output", 0)) for h in history)
            period_input = sum(h.get("input", 0) for h in history)
            period_output = sum(h.get("output", 0) for h in history)

        if today_input == 0 and today_output == 0 and stats_data.get("today", 0) > 0:
            today_input = stats_data.get("today", 0) // 2
            today_output = stats_data.get("today", 0) // 2

        estimated_cost = round(period_input * 0.000001 + period_output * 0.000008, 4)
        message_count = sum(len(s.get("messages", [])) for s in server.sessions.values())
        active_sessions = len([s for s in server.sessions.values() if len(s.get("messages", [])) > 0])
        avg_tokens_per_msg = round(period_total / max(message_count, 1), 2)

        if date_range != "all":
            history = sorted(history, key=lambda x: x.get("date", ""))[-30:] if date_range != "today" else history

        if len(history) >= 2:
            prev_entry = history[-2] if date_range == "today" else history[0] if len(history) == 1 else history[-2]
            prev_total = (prev_entry.get("input", 0) + prev_entry.get("output", 0)) if prev_entry else 0
            token_change_val = (
                (today_input + today_output) - prev_total
                if date_range == "today"
                else period_total - prev_total * (len(history) - 1 if len(history) > 1 else 1)
            )
            token_change = f"+{token_change_val}" if token_change_val >= 0 else f"{token_change_val}"
        else:
            token_change = "+0"

        stats = {
            "today": period_total,
            "today_input": period_input,
            "today_output": period_output,
            "month": stats_data.get("month", 0),
            "avg_per_chat": stats_data.get("avg_per_chat", 0),
            "estimated_cost": f"{estimated_cost:.4f}",
            "history": history,
            "message_count": message_count,
            "total_tokens": period_total,
            "avg_tokens_per_msg": avg_tokens_per_msg,
            "avg_response_time": 1.5,
            "active_sessions": active_sessions,
            "message_change": f"+{message_count // 7 if message_count > 0 else 0}",
            "token_change": token_change,
            "cost_change": "+0%",
            "avg_change": "+0%",
            "response_change": "-5%",
            "session_change": f"+{active_sessions}",
        }
        return jsonify(stats)

    @app.route("/api/tokens/history")
    def get_token_history():
        return jsonify(server.token_stats.get("history", []))

    @app.route("/api/tokens/rankings")
    def get_token_rankings():
        token_stats_file = os.path.join(server.data_dir, "token_stats.json")
        real_stats = {}
        if os.path.exists(token_stats_file):
            try:
                with open(token_stats_file, "r", encoding="utf-8") as f:
                    real_stats = json.load(f)
            except Exception:
                real_stats = {}

        sessions_file = os.path.join(server.data_dir, "sessions.json")
        session_names = {}
        if os.path.exists(sessions_file):
            try:
                with open(sessions_file, "r", encoding="utf-8") as f:
                    sessions_data = json.load(f)
                for sid, session in sessions_data.items():
                    session_names[sid] = session.get("name", f"?? {sid[:8]}")
            except Exception:
                pass

        for sid, session in server.sessions.items():
            if sid not in session_names:
                session_names[sid] = session.get("name", f"?? {sid[:8]}")

        sessions_data = real_stats.get("sessions", {})
        session_rankings = []
        total_tokens = 0
        total_messages = 0
        for session_id, data in sessions_data.items():
            total = data.get("total", 0)
            total_tokens += total
            message_count = data.get("message_count", 0)
            total_messages += message_count
            session_rankings.append(
                {
                    "id": session_id,
                    "name": session_names.get(session_id, f"?? {session_id[:8]}"),
                    "value": total,
                    "input": data.get("input", 0),
                    "output": data.get("output", 0),
                    "message_count": message_count,
                }
            )
        session_rankings.sort(key=lambda x: x["value"], reverse=True)

        avg_tokens_per_message = round(total_tokens / total_messages, 2) if total_messages > 0 else 0
        model_rankings = [{"name": server.ai_model or "MiniMax-Text-01", "value": real_stats.get("today", 0)}]
        user_rankings = []
        for session_id, data in sessions_data.items():
            session_type = data.get("type", "web")
            if session_type in ["private", "group"]:
                user_rankings.append(
                    {"name": session_names.get(session_id, session_id), "value": data.get("total", 0)}
                )
        user_rankings.sort(key=lambda x: x["value"], reverse=True)

        return jsonify(
            {
                "sessions": session_rankings[:10],
                "models": model_rankings,
                "users": user_rankings[:10],
                "total_tokens": total_tokens,
                "total_messages": total_messages,
                "avg_tokens_per_message": avg_tokens_per_message,
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
        if (current_time - server._stats_cache_time) < server._stats_cache_ttl and server._stats_cache:
            return jsonify(server._stats_cache)

        today_messages = 0
        total_messages = 0
        today_str = datetime.now().strftime("%Y-%m-%d")
        today_active_users = set()

        for session in server.sessions.values():
            messages = session.get("messages", [])
            total_messages += len(messages)
            session_type = session.get("type", "web")
            session_id = session.get("id", "")
            for msg in messages[-100:]:
                timestamp = msg.get("timestamp")
                if isinstance(timestamp, (int, float)):
                    msg_date = datetime.fromtimestamp(timestamp).strftime("%Y-%m-%d")
                    if msg_date == today_str:
                        today_messages += 1
                        if session_type == "web":
                            today_active_users.add(f"web:{session_id}")
                elif isinstance(timestamp, str) and today_str in timestamp:
                    today_messages += 1
                    if session_type == "web":
                        today_active_users.add(f"web:{session_id}")

        try:
            qq_data_dir = os.path.join(server.base_dir, "data", "qq")
            for qq_type in ["private", "group"]:
                qq_dir = os.path.join(qq_data_dir, qq_type)
                if not os.path.exists(qq_dir):
                    continue
                for filename in os.listdir(qq_dir):
                    if not filename.endswith('.json'):
                        continue
                    file_path = os.path.join(qq_dir, filename)
                    target_id = filename[:-5]
                    try:
                        with open(file_path, 'r', encoding='utf-8') as f:
                            messages = json.load(f)
                        for msg in messages:
                            timestamp = msg.get('timestamp', '')
                            if today_str in str(timestamp):
                                today_messages += 1
                                today_active_users.add(f"qq_{qq_type}:{target_id}")
                    except Exception as e:
                        _log.warning(f"?? QQ {qq_type} ???? {filename}: {e}")
        except Exception as e:
            _log.error(f"?? QQ ????: {e}")

        uptime_seconds = int(time.time() - server.start_time)
        uptime = server._format_uptime(uptime_seconds)

        try:
            import psutil
            process = psutil.Process()
            memory_usage = round(process.memory_info().rss / 1024 / 1024, 1)
        except Exception:
            memory_usage = 42

        ai_calls = 0
        try:
            for entry in server.token_stats.get("history", []):
                if entry.get("date") == today_str:
                    ai_calls = entry.get("message_count", 0)
                    break
        except Exception:
            ai_calls = today_messages

        file_transfers = 0
        try:
            workspaces_dir = os.path.join(server.base_dir, "data", "workspaces")
            if os.path.exists(workspaces_dir):
                for session_folder in os.listdir(workspaces_dir):
                    session_workspace = os.path.join(workspaces_dir, session_folder)
                    if not os.path.isdir(session_workspace) or session_folder.startswith('_'):
                        continue
                    for filename in os.listdir(session_workspace):
                        if filename.startswith('_'):
                            continue
                        file_path = os.path.join(session_workspace, filename)
                        try:
                            mtime = os.path.getmtime(file_path)
                            if datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") == today_str:
                                file_transfers += 1
                        except Exception:
                            pass

            static_files_dir = os.path.join(server.static_folder, "files")
            if os.path.exists(static_files_dir):
                for filename in os.listdir(static_files_dir):
                    file_path = os.path.join(static_files_dir, filename)
                    try:
                        mtime = os.path.getmtime(file_path)
                        if datetime.fromtimestamp(mtime).strftime("%Y-%m-%d") == today_str:
                            file_transfers += 1
                    except Exception:
                        pass
        except Exception as e:
            _log.warning(f"????????: {e}")

        kb_docs_count = 0
        if server.KNOWLEDGE_MANAGER_AVAILABLE:
            try:
                km = server.get_knowledge_manager()
                for kb in km.list_knowledge_bases():
                    kb_docs_count += len(kb.documents)
            except Exception:
                pass

        stats = {
            "today_messages": today_messages,
            "total_messages": total_messages,
            "active_sessions": len(server.sessions),
            "token_usage": server.token_stats.get("today", 0),
            "kb_docs": kb_docs_count,
            "memory_usage": memory_usage,
            "qq_connected": True,
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
        platform_stats["QQ ??"] = 0
        platform_stats["QQ ??"] = 0
        platform_stats["Web ??"] = 0

        try:
            qq_data_dir = os.path.join(server.base_dir, "data", "qq")
            for qq_type, label in [("private", "QQ ??"), ("group", "QQ ??")]:
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
                        _log.warning(f"?? QQ ?????? {filename}: {e}")
        except Exception as e:
            _log.error(f"?? QQ ??????: {e}")

        for session in server.sessions.values():
            if session.get("type", "web") == "web":
                platform_stats["Web ??"] += len(session.get("messages", []))

        colors = {"QQ ??": "#667eea", "QQ ??": "#4facfe", "Web ??": "#43e97b"}
        result = [
            {"name": name, "value": value, "itemStyle": {"color": colors.get(name, "#999")}}
            for name, value in platform_stats.items()
            if value > 0
        ]
        if not result:
            result = [
                {"name": "QQ ??", "value": 0, "itemStyle": {"color": "#667eea"}},
                {"name": "QQ ??", "value": 0, "itemStyle": {"color": "#4facfe"}},
                {"name": "Web ??", "value": 0, "itemStyle": {"color": "#43e97b"}},
            ]
        return jsonify(result)

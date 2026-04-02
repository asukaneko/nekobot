import json
import logging
import os
import threading
import uuid
from datetime import datetime

from flask import jsonify, request

from nbot.core import WebSessionStore

_log = logging.getLogger(__name__)


def register_qq_overview_routes(app, server):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    @app.route("/api/qq/users")
    def get_qq_users():
        try:
            users = []
            qq_private_dir = os.path.join(server.base_dir, "data", "qq", "private")
            _log.info(f"QQ private dir: {qq_private_dir}")
            _log.info(f"Dir exists: {os.path.exists(qq_private_dir)}")
            if os.path.exists(qq_private_dir):
                for filename in os.listdir(qq_private_dir):
                    if not filename.endswith(".json"):
                        continue
                    user_id = filename.replace(".json", "")
                    file_path = os.path.join(qq_private_dir, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            messages = json.load(f)
                        last_msg = messages[-1] if messages else None
                        users.append(
                            {
                                "user_id": user_id,
                                "last_message": last_msg.get("content", "")[:50]
                                if last_msg
                                else "",
                                "last_time": last_msg.get("timestamp", "")
                                if last_msg
                                else "",
                                "message_count": len(messages),
                            }
                        )
                    except Exception as e:
                        _log.error(f"Error reading {file_path}: {e}")
            return jsonify(
                {"users": sorted(users, key=lambda x: x["last_time"], reverse=True)}
            )
        except Exception as e:
            _log.error(f"Error in get_qq_users: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/groups")
    def get_qq_groups():
        try:
            groups = []
            qq_group_dir = os.path.join(server.base_dir, "data", "qq", "group")
            _log.info(f"QQ group dir: {qq_group_dir}")
            if os.path.exists(qq_group_dir):
                for filename in os.listdir(qq_group_dir):
                    if not filename.endswith(".json"):
                        continue
                    group_id = filename.replace(".json", "")
                    file_path = os.path.join(qq_group_dir, filename)
                    try:
                        with open(file_path, "r", encoding="utf-8") as f:
                            messages = json.load(f)
                        last_msg = messages[-1] if messages else None
                        groups.append(
                            {
                                "group_id": group_id,
                                "last_message": last_msg.get("content", "")[:50]
                                if last_msg
                                else "",
                                "last_time": last_msg.get("timestamp", "")
                                if last_msg
                                else "",
                                "message_count": len(messages),
                            }
                        )
                    except Exception as e:
                        _log.error(f"Error reading {file_path}: {e}")
            return jsonify(
                {"groups": sorted(groups, key=lambda x: x["last_time"], reverse=True)}
            )
        except Exception as e:
            _log.error(f"Error in get_qq_groups: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/messages/<qq_type>/<qq_id>")
    def get_qq_messages(qq_type, qq_id):
        try:
            if qq_type == "private":
                file_path = os.path.join(
                    server.base_dir, "data", "qq", "private", f"{qq_id}.json"
                )
            elif qq_type == "group":
                file_path = os.path.join(
                    server.base_dir, "data", "qq", "group", f"{qq_id}.json"
                )
            else:
                return jsonify({"error": "Invalid type"}), 400

            if not os.path.exists(file_path):
                return jsonify({"messages": []})

            with open(file_path, "r", encoding="utf-8") as f:
                messages = json.load(f)
            for msg in messages:
                msg["source_type"] = "qq"
                msg["qq_type"] = qq_type
                msg["qq_id"] = qq_id
            return jsonify({"messages": messages})
        except Exception as e:
            _log.error(f"获取QQ消息失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/messages/<qq_type>/<qq_id>", methods=["DELETE"])
    def delete_qq_messages(qq_type, qq_id):
        try:
            if qq_type == "private":
                file_path = os.path.join(
                    server.base_dir, "data", "qq", "private", f"{qq_id}.json"
                )
            elif qq_type == "group":
                file_path = os.path.join(
                    server.base_dir, "data", "qq", "group", f"{qq_id}.json"
                )
            else:
                return jsonify({"error": "Invalid type"}), 400

            if os.path.exists(file_path):
                os.remove(file_path)
                _log.info(f"Deleted QQ {qq_type} messages for {qq_id}")

            session_id_to_delete = None
            for sid, session in server.sessions.items():
                if session.get("qq_id") == qq_id and session.get("type") == f"qq_{qq_type}":
                    session_id_to_delete = sid
                    break

            if session_id_to_delete:
                session_store.delete_session(session_id_to_delete)
                if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                    server.workspace_manager.delete_workspace(session_id_to_delete)
                _log.info(f"Deleted associated web session {session_id_to_delete}")

            return jsonify({"success": True})
        except Exception as e:
            _log.error(f"删除QQ消息失败: {e}")
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/sessions")
    def get_qq_sessions():
        qq_sessions = []
        for session_id, session in server.sessions.items():
            if session.get("type") in ["qq_group", "qq_private"]:
                qq_sessions.append(
                    {
                        "id": session_id,
                        "name": session.get("name", "QQ ??"),
                        "type": session.get("type"),
                        "qq_id": session.get("qq_id"),
                        "message_count": len(session.get("messages", [])),
                        "last_message": session.get("last_message"),
                        "created_at": session.get("created_at"),
                    }
                )
        return jsonify(qq_sessions)

    @app.route("/api/qq/sessions/<session_id>/sync", methods=["POST"])
    def sync_qq_session(session_id):
        session = session_store.get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        if session.get("type") not in ["qq_group", "qq_private"]:
            return jsonify({"error": "Not a QQ session"}), 400

        try:
            from nbot.chat import group_messages, user_messages

            qq_id = session.get("qq_id")

            if session["type"] == "qq_private" and qq_id in user_messages:
                messages = user_messages[qq_id]
            elif session["type"] == "qq_group" and qq_id in group_messages:
                messages = group_messages[qq_id]
            else:
                messages = []

            for msg in messages:
                session_store.append_message(
                    session_id,
                    {
                        "id": str(uuid.uuid4()),
                        "role": "user" if msg.get("role") == "user" else "assistant",
                        "content": msg.get("content", ""),
                        "timestamp": msg.get("time", datetime.now().isoformat()),
                        "sender": msg.get("sender", "QQ User"),
                    },
                )

            updated_session = session_store.get_session(session_id) or session
            return jsonify(
                {"success": True, "message_count": len(updated_session.get("messages", []))}
            )
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/sessions/<session_id>/send", methods=["POST"])
    def send_qq_message(session_id):
        session = session_store.get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        if session.get("type") not in ["qq_group", "qq_private"]:
            return jsonify({"error": "Not a QQ session"}), 400

        if not server.qq_bot:
            return jsonify({"error": "QQ Bot not available"}), 503

        data = request.json or {}
        content = data.get("content", "").strip()
        if not content:
            return jsonify({"error": "Content is required"}), 400

        qq_id = session.get("qq_id")

        async def send_qq_msg():
            try:
                if session["type"] == "qq_group":
                    await server.qq_bot.api.post_group_msg(group_id=qq_id, text=content)
                else:
                    await server.qq_bot.api.post_private_msg(user_id=qq_id, text=content)
            except Exception as e:
                _log.error(f"Failed to send QQ message: {e}")

        def run_async_task():
            try:
                import asyncio

                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop.run_until_complete(send_qq_msg())
                loop.close()
            except Exception as e:
                _log.error(f"Failed to run async task: {e}")

        try:
            threading.Thread(target=run_async_task, daemon=True).start()

            message = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "sender": "Bot",
            }
            session_store.append_message(session_id, message)
            session["last_message"] = content
            session_store.set_session(session_id, session)

            return jsonify({"success": True, "message": message})
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    @app.route("/api/qq/link-session", methods=["POST"])
    def link_qq_session():
        data = request.json or {}
        qq_id = data.get("qq_id")
        session_type = data.get("type")
        name = data.get("name", f"QQ {qq_id}")

        if not qq_id or not session_type:
            return jsonify({"error": "qq_id and type are required"}), 400

        for session_id, session in server.sessions.items():
            if session.get("qq_id") == qq_id and session.get("type") == session_type:
                return jsonify({"success": True, "session_id": session_id, "exists": True})

        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "name": name,
            "type": session_type,
            "qq_id": qq_id,
            "created_at": datetime.now().isoformat(),
            "messages": [],
            "system_prompt": "",
        }
        session_store.set_session(session_id, session)

        return jsonify({"success": True, "session_id": session_id, "exists": False})


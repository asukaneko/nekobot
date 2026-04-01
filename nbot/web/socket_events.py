import asyncio
import hashlib
import json
import logging
import os
import uuid
from datetime import datetime

from flask import request
from flask_socketio import emit, join_room, leave_room

from nbot.web.message_adapter import WebMessageAdapter

_log = logging.getLogger(__name__)


def register_socket_events(server):
    @server.socketio.on("connect")
    def handle_connect():
        user_id = request.args.get("user_id", "web_user")
        server.web_users[request.sid] = user_id
        _log.info(f"Web client connected: {user_id}")

    @server.socketio.on("disconnect")
    def handle_disconnect():
        user_id = server.web_users.pop(request.sid, "unknown")
        session_id = server.active_connections.pop(request.sid, None)
        if session_id:
            leave_room(session_id)
        _log.info(f"Web client disconnected: {user_id}")

    @server.socketio.on("join_session")
    def handle_join_session(data):
        session_id = data.get("session_id")
        if session_id in server.sessions:
            join_room(session_id)
            server.active_connections[request.sid] = session_id
            server.socketio.emit(
                "joined_session", {"session_id": session_id}, room=request.sid
            )
        else:
            _log.warning(f"Client tried to join non-existent session: {session_id}")

    @server.socketio.on("leave_session")
    def handle_leave_session():
        session_id = server.active_connections.pop(request.sid, None)
        if session_id:
            leave_room(session_id)

    @server.socketio.on("send_message")
    def handle_send_message(data):
        try:
            session_id = data.get("session_id")
            content = data.get("content", "")
            sender = data.get("sender", "web_user")
            attachments = data.get("attachments", [])

            attachment_info = ""
            if attachments and isinstance(attachments, list):
                for att in attachments:
                    if isinstance(att, dict):
                        att_name = att.get("name", "unknown")
                        att_type = att.get("type", "")
                        attachment_info += f"\n[Attachment: {att_name}, type: {att_type}]"

            preview = content[:50] if content else ""
            server.log_message(
                "info",
                f"Received Web message from {sender}: {preview}... {len(attachments)} attachments",
            )
            _log.info(
                f"Received Web message: session={session_id}, sender={sender}, attachments={len(attachments)}"
            )

            sessions_file = os.path.join(server.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, "r", encoding="utf-8") as f:
                        file_sessions = json.load(f)
                    for sid, sess in file_sessions.items():
                        if sid not in server.sessions:
                            server.sessions[sid] = sess
                except Exception:
                    pass

            if session_id not in server.sessions:
                server.socketio.emit(
                    "error", {"message": "Session not found"}, room=request.sid
                )
                return

            is_command = False
            matched_handler = None
            if content and content.startswith("/"):
                try:
                    import nbot.commands
                    from nbot.commands import command_handlers

                    _log.info(
                        f"Checking command: {content}, registered count: {len(command_handlers)}"
                    )

                    for commands, handler in command_handlers.items():
                        for cmd in commands:
                            if content.startswith(cmd):
                                _log.info(f"Matched command: {cmd}")
                                is_command = True
                                matched_handler = handler
                                break
                        if is_command:
                            break

                    if not is_command:
                        _log.warning(f"Unknown command: {content}")
                except ImportError as e:
                    _log.warning(f"Failed to import command handlers: {e}")
                except Exception as e:
                    _log.error(f"Command matching failed: {e}", exc_info=True)

            temp_id = data.get("tempId")

            processed_attachments = []
            if attachments and isinstance(attachments, list):
                for att in attachments:
                    if isinstance(att, dict):
                        processed_att = {
                            "name": att.get("name", "unknown"),
                            "type": att.get("type", ""),
                            "size": att.get("size", 0),
                            "url": att.get("url", att.get("path", "")),
                            "content": att.get("content"),
                            "preview": att.get("preview")
                            if att.get("type", "").startswith("image/")
                            else att.get("url", att.get("path", "")),
                        }
                        processed_attachments.append(processed_att)

            message = {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": content,
                "timestamp": datetime.now().isoformat(),
                "sender": sender,
                "source": "web",
                "attachments": processed_attachments,
                "tempId": temp_id,
                "session_id": session_id,
            }

            server.sessions[session_id]["messages"].append(message)

            if getattr(server, "MESSAGE_MODULE_AVAILABLE", False) and getattr(
                server, "message_manager", None
            ):
                server.message_manager.add_web_message(
                    session_id,
                    server.create_message(
                        "user",
                        content,
                        sender=sender,
                        source="web",
                        session_id=session_id,
                        attachments=processed_attachments,
                        metadata={"tempId": temp_id},
                    ),
                )

            server.socketio.emit("new_message", message, room=session_id)
            server._save_data("sessions")

            if is_command and matched_handler:
                web_user_id = str(int(hashlib.md5(session_id.encode()).hexdigest(), 16))[
                    :10
                ]
                msg_adapter = WebMessageAdapter(content, web_user_id, session_id, server)

                def run_command():
                    original_bot = None
                    try:
                        import nbot.commands as cmd_module

                        original_bot = getattr(cmd_module, "bot", None)
                        cmd_module.bot = msg_adapter.bot
                        _log.info("Patched command bot for Web mock adapter")
                        asyncio.run(matched_handler(msg_adapter, is_group=True))
                    except Exception as e:
                        _log.error(f"Command execution failed: {e}", exc_info=True)
                        try:
                            asyncio.run(msg_adapter.reply(text=f"Command error: {e}"))
                        except Exception as reply_error:
                            _log.error(f"Failed to send command error reply: {reply_error}")
                    finally:
                        if original_bot:
                            cmd_module.bot = original_bot
                            _log.info("Restored command bot")

                server.socketio.start_background_task(run_command)
            else:
                parent_msg_id = temp_id if temp_id else message["id"]
                server._trigger_ai_response(
                    session_id, content, sender, attachments, parent_msg_id
                )

        except Exception as e:
            _log.error(f"Failed to handle Web message: {e}", exc_info=True)
            server.socketio.emit(
                "error", {"message": f"Message handling failed: {str(e)}"}, room=request.sid
            )

    @server.socketio.on("typing")
    def handle_typing(data):
        session_id = data.get("session_id")
        emit(
            "user_typing",
            {"sender": server.web_users.get(request.sid)},
            room=session_id,
        )

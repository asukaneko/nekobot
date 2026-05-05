import logging
import uuid
from copy import deepcopy
from datetime import datetime

from flask import jsonify, request

from nbot.core import WebSessionStore
from nbot.core.prompt_format import format_memory_items, format_skills_prompt
from nbot.web.persistence import is_web_visible_session
from nbot.web.sessions_db import get_session as get_session_from_db

_log = logging.getLogger(__name__)


def register_session_routes(app, server):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    def _get_web_session(session_id):
        session = session_store.get_session(session_id)
        if not session:
            session = get_session_from_db(server.data_dir, session_id)
        if not session or not is_web_visible_session(session_id, session):
            return None
        return session

    def _find_message_index(messages, message_id):
        if not message_id:
            return -1
        return next(
            (idx for idx, msg in enumerate(messages) if str(msg.get("id")) == str(message_id)),
            -1,
        )

    def _ensure_mutable_session(session_id, session):
        if not session_store.get_session(session_id):
            session_store.set_session(session_id, session)
        return session

    @app.route("/api/sessions")
    def get_sessions():
        current_time = server.time.time() if hasattr(server, "time") else None
        if current_time is None:
            import time

            current_time = time.time()
        if not hasattr(server, "_sessions_cache"):
            server._sessions_cache = []
            server._sessions_cache_time = 0

        sessions_data = dict(server.sessions)

        sessions = []
        for sid, session in sessions_data.items():
            if not is_web_visible_session(sid, session):
                continue
            archived = bool(session.get("archived"))
            sessions.append(
                {
                    "id": sid,
                    "name": session.get("name", f"会话 {sid[:8]}"),
                    "type": session.get("type", "web"),
                    "user_id": session.get("user_id"),
                    "qq_id": session.get("qq_id"),
                    "channel_id": session.get("channel_id"),
                    "created_at": session.get("created_at"),
                    "archived": archived,
                    "archived_at": session.get("archived_at") if archived else None,
                    "message_count": len(session.get("messages", [])),
                    "system_prompt": session.get("system_prompt", ""),
                    "sender_name": session.get("sender_name", ""),
                    "sender_avatar": session.get("sender_avatar", ""),
                    "sender_portrait": session.get("sender_portrait", ""),
                }
            )

        server._sessions_cache = sessions
        server._sessions_cache_time = current_time
        return jsonify(sessions)


    @app.route("/api/sessions", methods=["POST"])
    def create_session():
        data = request.json
        session_id = str(uuid.uuid4())
    
        # 使用人格提示词作为默认系统提示词
        system_prompt = data.get(
            "system_prompt", server.personality.get("systemPrompt", "")
        )

        # 替换模板变量 {{user}} -> 当前用户名
        user_id = data.get("user_id", "")
        if user_id:
            system_prompt = system_prompt.replace('{{user}}', user_id)
    
        # 获取所有记忆（标题+摘要）并加入系统提示词
        memory_items = []
        try:
            if server.PROMPT_MANAGER_AVAILABLE and server.prompt_manager:
                # 从 server.prompt_manager 获取所有记忆
                all_memories = server.prompt_manager.get_memories()
                for mem in all_memories:
                    # 兼容新旧格式：获取标题和摘要
                    title = mem.get("title", mem.get("key", ""))
                    summary = mem.get("summary", "")
                    content = mem.get("content", mem.get("value", ""))
                    if title:
                        # 优先使用摘要，否则使用内容前100字
                        display = (
                            summary
                            if summary
                            else (
                                content[:100] + "..."
                                if len(content) > 100
                                else content
                            )
                        )
                        memory_items.append({"title": title, "summary": display})
            elif server.memories:
                # 从 server.memories 获取
                for mem in server.memories:
                    title = mem.get("title", mem.get("key", ""))
                    summary = mem.get("summary", "")
                    content = mem.get("content", mem.get("value", ""))
                    if title:
                        display = (
                            summary
                            if summary
                            else (
                                content[:100] + "..."
                                if len(content) > 100
                                else content
                            )
                        )
                        memory_items.append({"title": title, "summary": display})
        except Exception as e:
            _log.warning(f"获取记忆失败: {e}")
    
        # 如果有记忆，添加到系统提示词
        if memory_items:
            system_prompt += format_memory_items(memory_items)
            _log.info(f"已添加 {len(memory_items)} 个记忆到会话 {session_id[:8]}")
    
        # 添加 Skills 到系统提示词
        enabled_skills = [s for s in server.skills_config if s.get("enabled", True)]
        system_prompt += format_skills_prompt(server.skills_config)
        _log.info(f"已添加 {len(enabled_skills)} 个技能到会话 {session_id[:8]}")
    
        # 获取角色信息
        sender_name = data.get("sender_name") or server.personality.get("name", "AI")
        sender_avatar = data.get("sender_avatar") or server.personality.get("avatar", "")
        sender_portrait = data.get("sender_portrait") or server.personality.get("portrait", "")

        session = {
            "id": session_id,
            "name": data.get("name", f"新会话 {session_id[:8]}"),
            "type": data.get("type", "web"),
            "user_id": data.get("user_id"),
            "created_at": datetime.now().isoformat(),
            "archived": False,
            "archived_at": None,
            "messages": [{"role": "system", "content": system_prompt}],
            "system_prompt": system_prompt,
            "sender_name": sender_name,
            "sender_avatar": sender_avatar,
            "sender_portrait": sender_portrait,
        }

        # 如果有开场白，添加为第一条 assistant 消息
        # 优先使用请求中指定的 first_message，否则使用当前角色的开场白
        first_message = data.get("first_message") or server.personality.get("firstMessage", "")
        if first_message:
            if user_id:
                first_message = first_message.replace('{{user}}', user_id)
            session["messages"].append({
                "role": "assistant",
                "content": first_message,
                "sender": sender_name
            })
        if not is_web_visible_session(session_id, session):
            return jsonify({"error": "Invalid session type"}), 400
    
        session_store.set_session(session_id, session)
    
        # 创建对应的工作区
        if server.WORKSPACE_AVAILABLE:
            server.workspace_manager.get_or_create(
                session_id, session.get("type", "web"), session.get("name", "")
            )
    
        return jsonify({"id": session_id, "session": session})
    @app.route("/api/sessions/<session_id>")
    def get_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        session["message_count"] = len(session.get("messages", []))
        return jsonify(session)

    @app.route("/api/sessions/<session_id>/debug")
    def debug_session(session_id):
        """调试 API：查看会话详细信息"""
        raw_session = session_store.get_session(session_id)
        db_session = get_session_from_db(server.data_dir, session_id)
        
        result = {
            "session_id": session_id,
            "in_memory": raw_session is not None,
            "in_db": db_session is not None,
            "is_visible": False,
        }
        
        if raw_session:
            result["memory_session"] = {
                "type": raw_session.get("type"),
                "channel_id": raw_session.get("channel_id"),
                "message_count": len(raw_session.get("messages", [])),
                "first_message_source": raw_session.get("messages", [{}])[0].get("source") if raw_session.get("messages") else None,
            }
            result["is_visible"] = is_web_visible_session(session_id, raw_session)
        
        if db_session:
            result["db_session"] = {
                "type": db_session.get("type"),
                "channel_id": db_session.get("channel_id"),
            }
        
        return jsonify(result)

    @app.route("/api/sessions/<session_id>", methods=["PUT"])
    def update_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json
        session["name"] = data.get("name", session["name"])

        new_prompt = data.get("system_prompt", session.get("system_prompt", ""))
        if new_prompt != session.get("system_prompt", ""):
            session["system_prompt"] = new_prompt
            if session["messages"] and session["messages"][0].get("role") == "system":
                session["messages"][0]["content"] = new_prompt
            else:
                session["messages"].insert(0, {"role": "system", "content": new_prompt})

        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    @app.route("/api/sessions/<session_id>/archive", methods=["POST"])
    def archive_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        session["archived"] = True
        session["archived_at"] = datetime.now().isoformat()
        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    @app.route("/api/sessions/<session_id>/restore", methods=["POST"])
    def restore_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        session["archived"] = False
        session["archived_at"] = None
        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        _log.info(f"[DeleteSession] 尝试删除会话: {session_id}")
        _log.info(f"[DeleteSession] 删除前会话列表: {list(server.sessions.keys())}")
        if not _get_web_session(session_id):
            _log.warning(f"[DeleteSession] 会话不存在或不可见: {session_id}")
            return jsonify({"error": "Session not found"}), 404
        if session_store.delete_session(session_id):
            _log.info(f"[DeleteSession] 会话已删除: {session_id}")
            _log.info(f"[DeleteSession] 删除后会话列表: {list(server.sessions.keys())}")
            if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                server.workspace_manager.delete_workspace(session_id)
            server.log_message("info", f"删除了会话 {session_id[:8]}...", important=True)
            return jsonify({"success": True})
        _log.warning(f"[DeleteSession] 删除会话失败: {session_id}")
        return jsonify({"error": "Session not found"}), 404

    def _export_session_payload(sessions):
        return {
            "version": 1,
            "type": "nbot_session_export",
            "exported_at": datetime.now().isoformat(),
            "total": len(sessions),
            "sessions": sessions,
        }

    def _normalize_imported_session(raw_session):
        if not isinstance(raw_session, dict):
            raise ValueError("session must be an object")
        old_id = str(raw_session.get("id") or "").strip()
        new_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        session = dict(raw_session)
        session["id"] = new_id
        session["name"] = session.get("name") or f"Imported {new_id[:8]}"
        if old_id:
            session["imported_from"] = old_id
        session["imported_at"] = now
        session["type"] = session.get("type") or "web"
        if session["type"] not in {"web", "cli"}:
            session["type"] = "web"
        messages = session.get("messages")
        if not isinstance(messages, list):
            messages = []
        session["messages"] = [m for m in messages if isinstance(m, dict)]
        system_prompt = session.get("system_prompt")
        if not system_prompt:
            system_msg = next((m for m in session["messages"] if m.get("role") == "system"), None)
            system_prompt = (system_msg or {}).get("content", "")
        session["system_prompt"] = system_prompt or ""
        if session["system_prompt"] and not any(m.get("role") == "system" for m in session["messages"]):
            session["messages"].insert(0, {"role": "system", "content": session["system_prompt"]})
        session["created_at"] = session.get("created_at") or now
        session["archived"] = bool(session.get("archived"))
        session["archived_at"] = session.get("archived_at") if session["archived"] else None
        if not is_web_visible_session(new_id, session):
            raise ValueError("invalid session type")
        return new_id, session

    @app.route("/api/sessions/<session_id>/export")
    def export_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        return jsonify(_export_session_payload([session]))

    @app.route("/api/sessions/export")
    def export_sessions():
        ids = [sid.strip() for sid in request.args.get("ids", "").split(",") if sid.strip()]
        sessions = []
        if ids:
            for sid in ids:
                session = _get_web_session(sid)
                if session:
                    sessions.append(session)
        else:
            for sid, session in (server.sessions or {}).items():
                if is_web_visible_session(sid, session):
                    sessions.append(session)
        return jsonify(_export_session_payload(sessions))

    @app.route("/api/sessions/import", methods=["POST"])
    def import_sessions():
        data = request.get_json(silent=True) or {}
        sessions_payload = data.get("sessions")
        if sessions_payload is None and isinstance(data.get("session"), dict):
            sessions_payload = [data["session"]]
        if sessions_payload is None and isinstance(data, dict) and data.get("type") != "nbot_session_export":
            sessions_payload = [data]
        if not isinstance(sessions_payload, list):
            return jsonify({"success": False, "error": "sessions must be a list"}), 400

        imported = []
        errors = []
        for idx, raw_session in enumerate(sessions_payload):
            try:
                session_id, session = _normalize_imported_session(raw_session)
                session_store.set_session(session_id, session)
                if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                    server.workspace_manager.get_or_create(
                        session_id, session.get("type", "web"), session.get("name", "")
                    )
                imported.append({"id": session_id, "name": session.get("name")})
            except Exception as exc:
                errors.append({"index": idx, "error": str(exc)})

        return jsonify({
            "success": True,
            "imported": len(imported),
            "failed": len(errors),
            "sessions": imported,
            "errors": errors,
        })

    @app.route("/api/sessions/<session_id>/fork", methods=["POST"])
    def fork_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        message_id = data.get("message_id")
        messages = session.get("messages", [])
        message_index = _find_message_index(messages, message_id)
        if message_index < 0:
            return jsonify({"success": False, "error": "Message not found"}), 404

        now = datetime.now().isoformat()
        new_id = str(uuid.uuid4())
        base_name = session.get("name") or f"Session {session_id[:8]}"
        forked_messages = deepcopy(messages[: message_index + 1])
        system_prompt = session.get("system_prompt", "")
        if not system_prompt:
            system_msg = next((m for m in forked_messages if m.get("role") == "system"), None)
            system_prompt = (system_msg or {}).get("content", "")
        if system_prompt and not any(m.get("role") == "system" for m in forked_messages):
            forked_messages.insert(0, {"role": "system", "content": system_prompt})

        new_session = {
            "id": new_id,
            "name": f"{base_name} - Fork",
            "type": session.get("type") if session.get("type") in {"web", "cli"} else "web",
            "user_id": session.get("user_id"),
            "qq_id": session.get("qq_id"),
            "created_at": now,
            "archived": False,
            "archived_at": None,
            "messages": forked_messages,
            "system_prompt": system_prompt,
            "forked_from": {
                "session_id": session_id,
                "message_id": message_id,
                "message_index": message_index,
                "created_at": now,
            },
        }
        session_store.set_session(new_id, new_session)
        if server.WORKSPACE_AVAILABLE and server.workspace_manager:
            server.workspace_manager.get_or_create(
                new_id, new_session.get("type", "web"), new_session.get("name", "")
            )
        return jsonify({"success": True, "id": new_id, "session": new_session})

    @app.route("/api/sessions/<session_id>/regenerate", methods=["POST"])
    def regenerate_message(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        message_id = data.get("message_id")
        messages = session.get("messages", [])
        message_index = _find_message_index(messages, message_id)
        if message_index < 0:
            return jsonify({"success": False, "error": "Message not found"}), 404

        target = messages[message_index]
        if target.get("role") != "assistant":
            return jsonify({"success": False, "error": "Only assistant messages can be regenerated"}), 400

        previous_user = next(
            (msg for msg in reversed(messages[:message_index]) if msg.get("role") == "user"),
            None,
        )
        if not previous_user or not previous_user.get("content"):
            return jsonify({"success": False, "error": "Previous user message not found"}), 400

        _ensure_mutable_session(session_id, session)
        original_messages = deepcopy(messages)
        trimmed_messages = deepcopy(messages[:message_index])
        previous_user_id = previous_user.get("id")
        for retained_msg in reversed(trimmed_messages):
            if str(retained_msg.get("id")) == str(previous_user_id):
                retained_msg.pop("thinking_cards", None)
                retained_msg.pop("todo_cards", None)
                retained_msg.pop("change_cards", None)
                break
        session_store.replace_messages(session_id, trimmed_messages)
        trigger = getattr(server, "_trigger_ai_response", None)
        if not trigger:
            session_store.replace_messages(session_id, original_messages)
            return jsonify({"success": False, "error": "AI trigger is unavailable"}), 500

        try:
            trigger(
                session_id,
                previous_user.get("content", ""),
                previous_user.get("sender", "web_user"),
                previous_user.get("attachments") or [],
                previous_user.get("id"),
            )
        except Exception as exc:
            session_store.replace_messages(session_id, original_messages)
            _log.error("Failed to trigger regenerated response: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": "Failed to trigger AI response"}), 500
        return jsonify({
            "success": True,
            "session_id": session_id,
            "removed_count": len(messages) - message_index,
            "prompt_message_id": previous_user.get("id"),
        })

    @app.route("/api/stop", methods=["POST"])
    def stop_generation():
        data = request.json or {}
        session_id = data.get("session_id")
        adapter = getattr(server, "web_channel_adapter", None)
        capabilities = adapter.get_capabilities() if adapter else None

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        if capabilities is not None and not capabilities.supports_stop:
            return jsonify({"success": False, "error": "Stop is not supported for this channel"}), 400

        if session_id in server.stop_events:
            server.stop_events[session_id].set()
            _log.info(f"[Stop] stop requested for session: {session_id}")
            return jsonify({"success": True, "message": "已请求停止生成"})

        return jsonify(
            {"success": False, "error": "No active generation for this session"}
        ), 404

    @app.route("/api/sessions/<session_id>/messages", methods=["GET"])
    def get_messages(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = session.get("messages", [])
        display_messages = [m for m in messages if m.get("role") != "system"]
        return jsonify(display_messages)

    @app.route("/api/sessions/<session_id>/messages", methods=["POST"])
    def add_message(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json
        message = {
            "id": str(uuid.uuid4()),
            "role": data.get("role", "user"),
            "content": data.get("content", ""),
            "timestamp": datetime.now().isoformat(),
            "sender": data.get("sender", "web_user"),
        }

        session_store.append_message(session_id, message)
        return jsonify(message)

    @app.route("/api/sessions/<session_id>/messages", methods=["DELETE"])
    def clear_messages(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        system_msg = None
        if session["messages"] and session["messages"][0].get("role") == "system":
            system_msg = session["messages"][0]

        session_store.replace_messages(session_id, [system_msg] if system_msg else [])
        server.log_message("info", f"清空了会话 {session_id[:8]} 的消息", important=True)
        return jsonify({"success": True})

    @app.route("/api/sessions/<session_id>/compress", methods=["POST"])
    def compress_context(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = session.get("messages", [])
        if len(messages) < 10:
            return jsonify({"success": False, "error": "消息数量不足，无需压缩"}), 400

        system_msg = None
        if messages and messages[0].get("role") == "system":
            system_msg = messages[0]

        keep_count = min(5, len(messages) - 2)
        recent_messages = messages[-keep_count:] if messages else []

        compress_start = 1 if system_msg else 0
        compress_end = len(messages) - keep_count

        if compress_end <= compress_start:
            return jsonify(
                {"success": False, "error": "没有足够的早期消息需要压缩"}
            ), 400

        messages_to_compress = messages[compress_start:compress_end]
        if not messages_to_compress:
            return jsonify({"success": False, "error": "没有消息需要压缩"}), 400

        conversation_text = "\n".join(
            [
                f"[{msg.get('role', 'user')}]: {msg.get('content', '')[:500]}"
                for msg in messages_to_compress
                if msg.get("content")
            ]
        )

        summary_prompt = f"""请简洁地总结以下对话的主要内容，保留关键信息和结论：

{conversation_text}

请用50-100字总结："""

        try:
            if not server.ai_client:
                return jsonify(
                    {"success": False, "error": "AI服务不可用，请先配置AI"}
                ), 503

            _log.info(f"[Compress] 开始压缩会话 {session_id[:8]}... 的上下文")

            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[{"role": "user", "content": summary_prompt}],
                stream=False,
            )

            summary = response.choices[0].message.content.strip()

            new_messages = [system_msg] if system_msg else []
            summary_msg = {
                "id": f"summary_{int(__import__('time').time())}",
                "role": "system",
                "content": f"【对话总结】{summary}",
                "timestamp": __import__('time').time(),
            }
            new_messages.append(summary_msg)
            new_messages.extend(recent_messages)

            session_store.replace_messages(session_id, new_messages)

            _log.info(
                f"[Compress] 上下文压缩完成: {session_id[:8]}... ({len(messages_to_compress)} 条消息被压缩)"
            )

            return jsonify(
                {
                    "success": True,
                    "compressed_count": len(messages_to_compress),
                    "summary": summary[:200],
                }
            )
        except Exception as e:
            _log.error(f"[Compress] 压缩上下文失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"压缩失败: {str(e)}"}), 500

    @app.route("/api/sessions/<session_id>/chat", methods=["POST"])
    def chat_with_ai(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json or {}
        user_content = (data.get("content") or "").strip()
        if not user_content:
            return jsonify({"error": "Content is required"}), 400

        user_message = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": user_content,
            "timestamp": datetime.now().isoformat(),
            "sender": data.get("sender", "web_user"),
            "source": "web",
            "session_id": session_id,
        }

        session_store.append_message(session_id, user_message)

        server._trigger_ai_response(
            session_id,
            user_content,
            user_message["sender"],
            data.get("attachments", []),
            user_message["id"],
        )

        return jsonify({"success": True, "user_message": user_message})


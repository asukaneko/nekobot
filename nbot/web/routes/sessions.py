import json
import logging
import os
import uuid
from datetime import datetime

from flask import jsonify, request

from nbot.core import WebSessionStore

_log = logging.getLogger(__name__)


def register_session_routes(app, server):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    @app.route("/api/sessions")
    def get_sessions():
        current_time = server.time.time() if hasattr(server, "time") else None
        if current_time is None:
            import time

            current_time = time.time()
        if not hasattr(server, "_sessions_cache"):
            server._sessions_cache = []
            server._sessions_cache_time = 0

        if (current_time - server._sessions_cache_time) < 5.0 and server._sessions_cache:
            return jsonify(server._sessions_cache)

        sessions_data = dict(server.sessions)
        if not sessions_data:
            sessions_file = os.path.join(server.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, "r", encoding="utf-8") as f:
                        sessions_data = json.load(f)
                except Exception:
                    sessions_data = {}

        sessions = []
        for sid, session in sessions_data.items():
            sessions.append(
                {
                    "id": sid,
                    "name": session.get("name", f"会话 {sid[:8]}"),
                    "type": session.get("type", "web"),
                    "user_id": session.get("user_id"),
                    "qq_id": session.get("qq_id"),
                    "created_at": session.get("created_at"),
                    "message_count": len(session.get("messages", [])),
                    "system_prompt": session.get("system_prompt", ""),
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
            "system_prompt", server.personality.get("prompt", "")
        )
    
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
                        memory_items.append(f"【{title}】{display}")
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
                        memory_items.append(f"【{title}】{display}")
        except Exception as e:
            _log.warning(f"获取记忆失败: {e}")
    
        # 如果有记忆，添加到系统提示词
        if memory_items:
            memory_context = "\n\n【可用记忆主题】\n" + "\n".join(
                [f"- {item}" for item in memory_items]
            )
            system_prompt += memory_context
            _log.info(f"已添加 {len(memory_items)} 个记忆到会话 {session_id[:8]}")
    
        # 添加 Skills 到系统提示词
        skills = server.skills_config
        enabled_skills = [s for s in skills if s.get("enabled", True)]
    
        skills_desc = "\n\n## 可用技能 (Skills)\n"
        skills_desc += "你可以使用以下技能来帮助用户：\n\n"
    
        if enabled_skills:
            for skill in enabled_skills:
                skills_desc += f"### {skill['name']}\n"
                skills_desc += f"- 描述: {skill['description']}\n"
                if skill.get("aliases"):
                    skills_desc += f"- 别名: {', '.join(skill['aliases'])}\n"
                skills_desc += "\n"
        else:
            skills_desc += "（暂无可用技能）\n\n"
    
        skills_desc += """
    **使用规则：**
    1. 当用户需要使用或了解某个技能时，使用 `skill_get_info` 工具获取该技能的详细信息
    2. 使用 `skill_list` 工具可以列出所有可用的 Skills
    """
        system_prompt += skills_desc
        _log.info(f"已添加 {len(enabled_skills)} 个技能到会话 {session_id[:8]}")
    
        session = {
            "id": session_id,
            "name": data.get("name", f"新会话 {session_id[:8]}"),
            "type": data.get("type", "web"),
            "user_id": data.get("user_id"),
            "created_at": datetime.now().isoformat(),
            "messages": [{"role": "system", "content": system_prompt}],
            "system_prompt": system_prompt,
        }
    
        session_store.set_session(session_id, session)
    
        # 创建对应的工作区
        if server.WORKSPACE_AVAILABLE:
            server.workspace_manager.get_or_create(
                session_id, session.get("type", "web"), session.get("name", "")
            )
    
        return jsonify({"id": session_id, "session": session})
    @app.route("/api/sessions/<session_id>")
    def get_session(session_id):
        session = session_store.get_session(session_id)
        if not session:
            sessions_file = os.path.join(server.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, "r", encoding="utf-8") as f:
                        sessions_data = json.load(f)
                    session = sessions_data.get(session_id)
                except Exception:
                    session = None
        if not session:
            return jsonify({"error": "Session not found"}), 404

        session["message_count"] = len(session.get("messages", []))
        return jsonify(session)

    @app.route("/api/sessions/<session_id>", methods=["PUT"])
    def update_session(session_id):
        session = session_store.get_session(session_id)
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

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        if session_store.delete_session(session_id):
            if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                server.workspace_manager.delete_workspace(session_id)
            return jsonify({"success": True})
        return jsonify({"error": "Session not found"}), 404

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
        session = session_store.get_session(session_id)

        if not session:
            sessions_file = os.path.join(server.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, "r", encoding="utf-8") as f:
                        sessions_data = json.load(f)
                    session = sessions_data.get(session_id)
                except Exception:
                    session = None

        if not session:
            return jsonify({"error": "Session not found"}), 404

        limit = request.args.get("limit", 50, type=int)
        messages = session.get("messages", [])[-limit:]
        display_messages = [m for m in messages if m.get("role") != "system"]
        return jsonify(display_messages)

    @app.route("/api/sessions/<session_id>/messages", methods=["POST"])
    def add_message(session_id):
        session = session_store.get_session(session_id)
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
        session = session_store.get_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        system_msg = None
        if session["messages"] and session["messages"][0].get("role") == "system":
            system_msg = session["messages"][0]

        session_store.replace_messages(session_id, [system_msg] if system_msg else [])
        return jsonify({"success": True})

    @app.route("/api/sessions/<session_id>/compress", methods=["POST"])
    def compress_context(session_id):
        session = session_store.get_session(session_id)
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
        session = session_store.get_session(session_id)
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


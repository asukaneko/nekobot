import json
import logging
from datetime import datetime

from flask import jsonify, request

from nbot.web.agent_tools import execute_web_agent_tool, get_web_agent_tools, run_web_agent_turn

_log = logging.getLogger(__name__)


def _safe_count(value):
    try:
        return len(value or [])
    except TypeError:
        return 0


def _enabled_count(items):
    if not isinstance(items, list):
        return 0
    return sum(1 for item in items if not isinstance(item, dict) or item.get("enabled", True))


def _names(items, limit=8):
    if not isinstance(items, list):
        return []
    names = []
    for item in items[:limit]:
        if isinstance(item, dict):
            names.append(item.get("name") or item.get("title") or item.get("id") or "unnamed")
        else:
            names.append(str(item))
    return names


def _session_summary(server, frontend):
    sessions = getattr(server, "sessions", {}) or {}
    active_count = 0
    total_messages = 0
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        messages = session.get("messages") or []
        message_count = len(messages)
        total_messages += message_count
        if message_count > 1:
            active_count += 1

    current = frontend.get("currentSession") if isinstance(frontend, dict) else {}
    return {
        "total": len(sessions),
        "active": active_count,
        "total_messages": total_messages,
        "current": current or None,
    }


def _build_status(server, payload):
    frontend = payload.get("frontend") if isinstance(payload.get("frontend"), dict) else {}
    live2d = payload.get("live2d") if isinstance(payload.get("live2d"), dict) else {}
    tools = getattr(server, "tools_config", []) or []
    skills = getattr(server, "skills_config", []) or []
    workflows = getattr(server, "workflows", []) or []
    scheduled_tasks = getattr(server, "scheduled_tasks", []) or []
    channels = getattr(server, "channels_config", []) or []
    token_stats = getattr(server, "token_stats", {}) or {}
    memories = getattr(server, "memories", []) or []
    stop_events = getattr(server, "stop_events", {}) or {}
    ai_client = getattr(server, "ai_client", None)

    return {
        "time": datetime.now().isoformat(timespec="seconds"),
        "startup": {
            "ready": bool(getattr(server, "startup_ready", False)),
            "error": getattr(server, "startup_error", None),
        },
        "connection": {
            "qq_bot": bool(getattr(server, "qq_bot", None)),
            "web_socket": bool(frontend.get("socketConnected")),
            "page": frontend.get("currentPage"),
            "chat_tab": frontend.get("chatTab"),
            "is_loading": bool(frontend.get("isLoading")),
            "active_generations": _safe_count(stop_events),
        },
        "ai": {
            "model": getattr(server, "ai_model", None),
            "configured": bool(ai_client),
            "provider_type": getattr(ai_client, "provider_type", None) if ai_client else None,
            "supports_stream": bool(getattr(ai_client, "supports_stream", False)) if ai_client else False,
            "personality": (getattr(server, "personality", {}) or {}).get("name"),
        },
        "sessions": _session_summary(server, frontend),
        "features": {
            "tools": {"total": _safe_count(tools), "enabled": _enabled_count(tools), "examples": _names(tools)},
            "skills": {"total": _safe_count(skills), "enabled": _enabled_count(skills), "examples": _names(skills)},
            "workflows": {
                "total": _safe_count(workflows),
                "enabled": _enabled_count(workflows),
                "examples": _names(workflows),
            },
            "scheduled_tasks": {
                "total": _safe_count(scheduled_tasks),
                "enabled": _enabled_count(scheduled_tasks),
                "examples": _names(scheduled_tasks),
            },
            "channels": {"total": _safe_count(channels), "enabled": _enabled_count(channels), "examples": _names(channels)},
        },
        "tokens": token_stats if isinstance(token_stats, dict) else {},
        "memory": {
            "items": _safe_count(memories),
            "prompt_manager": bool(getattr(server, "prompt_manager", None)),
        },
        "workspace": {
            "available": bool(getattr(server, "WORKSPACE_AVAILABLE", False)),
        },
        "web_agent": {
            "tools": [
                {
                    "name": tool["name"],
                    "permission": tool["permission"],
                    "description": tool["description"],
                }
                for tool in get_web_agent_tools()
            ],
        },
        "live2d": live2d,
    }


def _response_text(response):
    if isinstance(response, str):
        return response.strip()
    try:
        return response.choices[0].message.content.strip()
    except Exception:
        return ""


def _clean_line(text):
    text = (text or "").strip()
    for prefix in ("```text", "```markdown", "```"):
        if text.startswith(prefix):
            text = text[len(prefix):].strip()
    if text.endswith("```"):
        text = text[:-3].strip()
    text = text.replace("\r", " ").replace("\n", " ").strip(" \t\"'")
    if len(text) > 120:
        text = text[:117].rstrip(" ,，.。") + "..."
    return text


def _parse_json_array(raw):
    """Try to extract a JSON array from an AI response string."""
    raw = (raw or "").strip()
    # Try direct parse first
    try:
        result = json.loads(raw)
        if isinstance(result, list):
            return result
    except (json.JSONDecodeError, ValueError):
        pass
    # Try to find array in markdown code blocks
    import re
    m = re.search(r'\[[\s\S]*?\]', raw)
    if m:
        try:
            result = json.loads(m.group(0))
            if isinstance(result, list):
                return result
        except (json.JSONDecodeError, ValueError):
            pass
    return None


def _build_system_prompt(server, extra_instruction):
    personality = getattr(server, "personality", {}) or {}
    base_prompt = str(personality.get("prompt") or "").strip()
    return (
        f"{base_prompt}\n\n"
        f"{extra_instruction}\n"
        "必须延续上面的人格设定和说话风格。"
        "结合当前系统状态和对话历史自然回应，但不要逐项复述状态。"
        "只输出一句中文短句，30字以内，不要 Markdown，不要解释。"
    )


def register_live2d_routes(app, server):
    # Initialize invisible Live2D conversation history
    if not hasattr(server, "live2d_messages"):
        server.live2d_messages = []
    if not hasattr(server, "live2d_pending_tool"):
        server.live2d_pending_tool = None

    @app.route("/api/live2d/random-talk", methods=["POST"])
    def random_talk():
        if not getattr(server, "ai_client", None):
            return jsonify({"success": False, "error": "AI is not configured"}), 503

        payload = request.get_json(silent=True) or {}
        topic = (payload.get("topic") or "").strip()
        status = _build_status(server, payload)

        if topic and topic.lower() in {"确认", "确认执行", "执行", "同意", "yes", "ok"}:
            pending_tool = getattr(server, "live2d_pending_tool", None)
            if pending_tool:
                result = execute_web_agent_tool(
                    server,
                    pending_tool.get("tool", ""),
                    pending_tool.get("arguments") or {},
                    confirm=True,
                )
                server.live2d_pending_tool = None
                message = _clean_line(result.get("message") or result.get("error") or "")
                if not message:
                    message = "已经执行完成。"
                server.live2d_messages.append({"role": "user", "content": topic})
                server.live2d_messages.append({"role": "assistant", "content": message})
                if len(server.live2d_messages) > 60:
                    server.live2d_messages = server.live2d_messages[-40:]
                return jsonify({
                    "success": bool(result.get("success")),
                    "message": message,
                    "agent": {"used_tool": True, "tool": pending_tool.get("tool"), "tool_result": result},
                    "status": status,
                    "history_length": len(server.live2d_messages),
                })

        if topic:
            agent_result = run_web_agent_turn(server, topic, allow_write=False)
            if agent_result.get("used_tool") or agent_result.get("requires_confirmation"):
                message = _clean_line(agent_result.get("message") or "")
                if not message:
                    message = "这个操作需要确认后再执行。"
                if agent_result.get("requires_confirmation"):
                    server.live2d_pending_tool = {
                        "tool": agent_result.get("tool"),
                        "arguments": agent_result.get("arguments") or {},
                    }
                server.live2d_messages.append({"role": "user", "content": topic})
                server.live2d_messages.append({"role": "assistant", "content": message})
                if len(server.live2d_messages) > 60:
                    server.live2d_messages = server.live2d_messages[-40:]
                return jsonify({
                    "success": True,
                    "message": message,
                    "agent": agent_result,
                    "status": status,
                    "history_length": len(server.live2d_messages),
                })

        system_prompt = _build_system_prompt(
            server,
            "你正在作为网页右下角的 Live2D 看板娘与用户互动。"
            "根据对话历史和当前上下文自然地回应。"
            "如果用户提出了具体话题就回应那个话题，否则请说一句自然的随机闲聊。",
        )

        # Build messages: system + history + current user message
        messages = [{"role": "system", "content": system_prompt}]

        # Include recent conversation history (last 20 exchanges = 40 messages)
        history = list(getattr(server, "live2d_messages", [])[-40:])
        messages.extend(history)

        if topic:
            user_content = topic
        else:
            status_json = json.dumps(status, ensure_ascii=False, indent=2)
            user_content = f"当前系统状态 JSON：\n{status_json}\n请说一句自然的随机闲聊。"

        messages.append({"role": "user", "content": user_content})

        try:
            response = server.ai_client.chat_completion(
                model=getattr(server, "ai_model", None),
                messages=messages,
                stream=False,
            )
            message = _clean_line(_response_text(response))
            if not message:
                message = "我看了一下状态，现在一切都在运行。"

            # Persist to invisible conversation history
            server.live2d_messages.append({"role": "user", "content": user_content})
            server.live2d_messages.append({"role": "assistant", "content": message})
            # Keep memory bounded
            if len(server.live2d_messages) > 60:
                server.live2d_messages = server.live2d_messages[-40:]

            return jsonify({
                "success": True,
                "message": message,
                "status": status,
                "history_length": len(server.live2d_messages),
            })
        except Exception as exc:
            _log.warning("[Live2D] random talk failed: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500

    @app.route("/api/live2d/menu-options", methods=["POST"])
    def menu_options():
        if not getattr(server, "ai_client", None):
            return jsonify({
                "success": True,
                "greeting": None,
                "options": ["随机说话", "查看状态"],
            })

        payload = request.get_json(silent=True) or {}
        status = _build_status(server, payload)
        status_json = json.dumps(status, ensure_ascii=False, indent=2)

        system_prompt = _build_system_prompt(
            server,
            "用户右键点击了你，你需要：\n"
            "1. 先说一句简短的开场白（不含「点击」或「菜单」字样）\n"
            "2. 然后生成 2-3 个互动选项\n"
            "返回 JSON 格式：{\"greeting\": \"开场白\", \"options\": [\"选项1\", \"选项2\"]}\n"
            "选项以动词开头（如「讲个笑话」「看看状态」「聊聊今天」），8 个字以内。\n"
            "只输出 JSON，不要 Markdown 代码块，不要解释。",
        ).replace(
            "只输出一句中文短句，30字以内，不要 Markdown，不要解释。",
            "只输出 JSON 对象，不要 Markdown，不要解释。",
        )

        # Include recent history for context-aware suggestions
        messages = [{"role": "system", "content": system_prompt}]
        history = list(getattr(server, "live2d_messages", [])[-10:])
        messages.extend(history)
        messages.append({
            "role": "user",
            "content": f"当前系统状态 JSON：\n{status_json}\n请生成互动选项 JSON。",
        })

        try:
            response = server.ai_client.chat_completion(
                model=getattr(server, "ai_model", None),
                messages=messages,
                stream=False,
            )
            raw = _response_text(response)
            parsed = _parse_json_array(raw)

            greeting = None
            options = None

            if isinstance(parsed, dict):
                greeting = _clean_line(parsed.get("greeting"))
                options = parsed.get("options")
            elif isinstance(parsed, list):
                options = parsed

            if not isinstance(options, list) or len(options) == 0:
                # Fallback: use the raw text as a single option
                cleaned = _clean_line(raw)
                if cleaned and len(cleaned) <= 20:
                    options = [cleaned]
                else:
                    options = ["随便聊聊", "看看状态"]

            # Clean and limit options
            options = [_clean_line(o) for o in options[:4]]
            options = [o for o in options if o and len(o) <= 20]

            return jsonify({
                "success": True,
                "greeting": greeting,
                "options": options,
            })
        except Exception as exc:
            _log.warning("[Live2D] menu options failed: %s", exc)
            return jsonify({
                "success": False,
                "greeting": None,
                "options": ["随机说话", "查看状态"],
            })

    @app.route("/api/live2d/reset", methods=["POST"])
    def reset_live2d():
        server.live2d_messages = []
        return jsonify({"success": True, "message": "会话历史已清除"})

    @app.route("/api/live2d/history")
    def live2d_history():
        try:
            limit = int(request.args.get("limit", 20))
        except (TypeError, ValueError):
            limit = 20
        limit = max(1, min(limit, 60))
        all_messages = list(getattr(server, "live2d_messages", []) or [])
        return jsonify({
            "success": True,
            "messages": all_messages[-limit:],
            "total": len(all_messages),
        })

    @app.route("/api/live2d/comment", methods=["POST"])
    def live2d_comment():
        if not getattr(server, "ai_client", None):
            return jsonify({"success": False, "error": "AI is not configured"}), 503

        payload = request.get_json(silent=True) or {}
        recent_messages = payload.get("recent_messages") or []
        status = _build_status(server, payload)

        if not isinstance(recent_messages, list) or len(recent_messages) == 0:
            return jsonify({"success": True, "message": None})

        # Build conversation log from recent messages (up to 5 pairs)
        log_parts = []
        for m in recent_messages[-10:]:  # max 5 user+assistant pairs = 10 messages
            if not isinstance(m, dict):
                continue
            role = m.get("role", "")
            content = (m.get("content") or "").strip()
            if not content:
                continue
            if len(content) > 300:
                content = content[:297] + "..."
            label = "用户" if role == "user" else ("AI" if role == "assistant" else role)
            log_parts.append(f"{label}：{content}")

        if not log_parts:
            return jsonify({"success": True, "message": None})

        conversation_log = "\n".join(log_parts)

        system_prompt = _build_system_prompt(
            server,
            "你正在作为网页右下角的 Live2D 看板娘。"
            "以下是用户和 AI 最近的对话记录，你需要对 AI 的回复发表一句简短的评论、吐槽或补充。"
            "可以赞同、调侃、表示好奇或给出简短的提醒，但不要逐字复述内容。"
            "自然融入你的人格设定。",
        )

        messages = [{"role": "system", "content": system_prompt}]

        # Include recent Live2D conversation history
        history = list(getattr(server, "live2d_messages", [])[-10:])
        messages.extend(history)

        context = (
            f"最近对话记录：\n{conversation_log}\n\n"
            "请对 AI 最后的回复发表一句简短评论（不是回答问题本身，而是评论 AI 的表现）。"
        )
        messages.append({"role": "user", "content": context})

        try:
            response = server.ai_client.chat_completion(
                model=getattr(server, "ai_model", None),
                messages=messages,
                stream=False,
            )
            message = _clean_line(_response_text(response))
            if not message:
                message = "这个回复看起来不错。"
        except Exception as exc:
            _log.warning("[Live2D] comment failed, using fallback: %s", exc)
            message = None

        # Only append to history if we got a real response
        if message:
            server.live2d_messages.append({"role": "user", "content": context})
            server.live2d_messages.append({"role": "assistant", "content": message})
            if len(server.live2d_messages) > 60:
                server.live2d_messages = server.live2d_messages[-40:]

        return jsonify({
            "success": True,
            "message": message,
            "history_length": len(server.live2d_messages),
        })

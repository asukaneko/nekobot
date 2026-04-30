import json
import uuid
from datetime import datetime


def _ok(data=None, message="ok"):
    return {"success": True, "message": message, "data": data or {}}


def _err(message, status=400):
    return {"success": False, "error": message, "status": status}


def _require_confirmation(tool, arguments, message):
    return {
        "success": False,
        "requires_confirmation": True,
        "tool": tool,
        "arguments": arguments,
        "message": message,
    }


def _safe_limit(value, default=20, maximum=100):
    try:
        return max(1, min(int(value), maximum))
    except (TypeError, ValueError):
        return default


def get_web_agent_tools():
    return [
        {
            "name": "system.status",
            "description": "Read current web server, AI, feature, session, and Live2D status.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "tokens.summary",
            "description": "Read token usage summary and recent history.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "logs.recent",
            "description": "Read recent system logs. Optional level: info, warning, error.",
            "permission": "read",
            "parameters": {
                "type": "object",
                "properties": {"level": {"type": "string"}, "limit": {"type": "integer"}},
            },
        },
        {
            "name": "workflows.list",
            "description": "List configured workflows.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "workflows.create",
            "description": "Create a workflow draft.",
            "permission": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "trigger": {"type": "string"},
                    "enabled": {"type": "boolean"},
                    "config": {"type": "object"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "memory.list",
            "description": "List memories.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "memory.create",
            "description": "Create long-term or short-term memory.",
            "permission": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "content": {"type": "string"},
                    "summary": {"type": "string"},
                    "type": {"type": "string"},
                    "target_id": {"type": "string"},
                    "expire_days": {"type": "integer"},
                },
                "required": ["title", "content"],
            },
        },
        {
            "name": "knowledge.list",
            "description": "List knowledge documents.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {"limit": {"type": "integer"}}},
        },
        {
            "name": "knowledge.create",
            "description": "Create a knowledge document in the default knowledge base.",
            "permission": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "content": {"type": "string"},
                    "source": {"type": "string"},
                    "tags": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["name", "content"],
            },
        },
        {
            "name": "skills.list",
            "description": "List skills.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "skills.create",
            "description": "Create a skill config and optional SKILL.md content.",
            "permission": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "description": {"type": "string"},
                    "aliases": {"type": "array", "items": {"type": "string"}},
                    "enabled": {"type": "boolean"},
                    "parameters": {"type": "object"},
                    "skill_md": {"type": "string"},
                },
                "required": ["name"],
            },
        },
        {
            "name": "ai.config.summary",
            "description": "Read AI config without secrets.",
            "permission": "read",
            "parameters": {"type": "object", "properties": {}},
        },
        {
            "name": "ai.config.update",
            "description": "Update AI config fields such as provider_type, base_url, model, temperature, token limits, or feature flags.",
            "permission": "write",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string"},
                    "provider_type": {"type": "string"},
                    "base_url": {"type": "string"},
                    "model": {"type": "string"},
                    "temperature": {"type": "number"},
                    "max_tokens": {"type": "integer"},
                    "top_p": {"type": "number"},
                    "frequency_penalty": {"type": "number"},
                    "presence_penalty": {"type": "number"},
                    "system_prompt": {"type": "string"},
                    "timeout": {"type": "integer"},
                    "retry_count": {"type": "integer"},
                    "stream": {"type": "boolean"},
                    "enable_memory": {"type": "boolean"},
                    "image_model": {"type": "string"},
                    "embedding_model": {"type": "string"},
                    "max_context_length": {"type": "integer"},
                    "supports_tools": {"type": "boolean"},
                    "supports_reasoning": {"type": "boolean"},
                    "supports_stream": {"type": "boolean"},
                },
            },
        },
        {
            "name": "theme.update",
            "description": "Update web theme settings such as theme, background image, or overlay.",
            "permission": "write",
            "parameters": {"type": "object", "properties": {"settings": {"type": "object"}}},
        },
    ]


def get_tool_descriptor(name):
    return next((tool for tool in get_web_agent_tools() if tool["name"] == name), None)


def execute_web_agent_tool(server, name, arguments=None, confirm=False):
    arguments = arguments or {}
    descriptor = get_tool_descriptor(name)
    if not descriptor:
        return _err(f"Unknown tool: {name}", 404)

    if descriptor["permission"] == "write" and not confirm:
        return _require_confirmation(name, arguments, f"工具 {name} 会修改配置，需要确认后执行。")

    if name == "system.status":
        return _ok(
            {
                "startup_ready": bool(getattr(server, "startup_ready", False)),
                "startup_error": getattr(server, "startup_error", None),
                "ai_model": getattr(server, "ai_model", None),
                "ai_configured": bool(getattr(server, "ai_client", None)),
                "sessions": len(getattr(server, "sessions", {}) or {}),
                "workflows": len(getattr(server, "workflows", []) or []),
                "skills": len(getattr(server, "skills_config", []) or []),
                "tools": len(getattr(server, "tools_config", []) or []),
                "knowledge_available": bool(getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False)),
                "settings": getattr(server, "settings", {}) or {},
            },
            "已读取系统状态。",
        )

    if name == "tokens.summary":
        limit = _safe_limit(arguments.get("limit"), default=7, maximum=60)
        stats = getattr(server, "token_stats", {}) or {}
        history = list(stats.get("history", []))[-limit:]
        return _ok({**stats, "history": history}, "已读取 Token 用量。")

    if name == "logs.recent":
        level = arguments.get("level", "all")
        limit = _safe_limit(arguments.get("limit"), default=20, maximum=100)
        logs = list(getattr(server, "system_logs", []) or [])
        if level != "all":
            logs = [log for log in logs if log.get("level") == level]
        return _ok({"logs": logs[-limit:]}, "已读取系统日志。")

    if name == "workflows.list":
        return _ok({"workflows": getattr(server, "workflows", []) or []}, "已读取工作流。")

    if name == "workflows.create":
        workflow = {
            "id": str(uuid.uuid4()),
            "name": arguments.get("name") or "未命名工作流",
            "description": arguments.get("description", ""),
            "enabled": bool(arguments.get("enabled", True)),
            "trigger": arguments.get("trigger", "manual"),
            "config": arguments.get("config", {}),
        }
        server.workflows.append(workflow)
        server._save_data("workflows")
        if workflow.get("enabled") and workflow.get("trigger") == "cron":
            server._schedule_workflow(workflow)
        return _ok({"workflow": workflow}, "已创建工作流。")

    if name == "memory.list":
        limit = _safe_limit(arguments.get("limit"), default=20, maximum=100)
        return _ok({"memories": list(getattr(server, "memories", []) or [])[-limit:]}, "已读取记忆。")

    if name == "memory.create":
        title = arguments.get("title", "").strip()
        content = arguments.get("content", "").strip()
        if not title or not content:
            return _err("title and content are required")
        prompt_manager = getattr(server, "prompt_manager", None)
        if not prompt_manager:
            return _err("Prompt manager not available", 503)
        success = prompt_manager.add_memory(
            title,
            content,
            arguments.get("target_id", ""),
            arguments.get("summary"),
            arguments.get("type", "long"),
            arguments.get("expire_days", 7),
        )
        if not success:
            return _err("Failed to add memory", 500)
        server.memories = prompt_manager.get_memories()
        server._save_data("memories")
        server.log_message("info", f"创建了记忆: {title}", important=True)
        return _ok({"title": title}, "已创建记忆。")

    if name == "knowledge.list":
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return _ok({"documents": []}, "知识库未启用。")
        limit = _safe_limit(arguments.get("limit"), default=20, maximum=100)
        km = server.get_knowledge_manager()
        docs = []
        for base in km.list_knowledge_bases():
            for doc_id in base.documents:
                doc = km.store.load_document(doc_id)
                if doc:
                    docs.append({"id": doc.id, "title": doc.title, "size": len(doc.content), "tags": doc.tags})
        return _ok({"documents": docs[-limit:]}, "已读取知识库。")

    if name == "knowledge.create":
        if not getattr(server, "KNOWLEDGE_MANAGER_AVAILABLE", False):
            return _err("Knowledge manager not available", 503)
        title = arguments.get("name", "").strip()
        content = arguments.get("content", "").strip()
        if not title or not content:
            return _err("name and content are required")
        km = server.get_knowledge_manager()
        if not km.store.load_base("default"):
            km.create_knowledge_base("默认知识库", "默认知识库")
        doc = km.add_document("default", title, content, arguments.get("source", ""), arguments.get("tags", []))
        server.log_message("info", f"创建了知识库文档: {title}", important=True)
        return _ok({"document": {"id": doc.id, "title": doc.title, "size": len(doc.content)}}, "已创建知识库文档。")

    if name == "skills.list":
        return _ok({"skills": getattr(server, "skills_config", []) or []}, "已读取 Skills。")

    if name == "skills.create":
        skill_name = arguments.get("name", "").strip()
        if not skill_name:
            return _err("name is required")
        skill = {
            "id": str(uuid.uuid4()),
            "name": skill_name,
            "description": arguments.get("description", ""),
            "aliases": arguments.get("aliases", []),
            "enabled": bool(arguments.get("enabled", True)),
            "parameters": arguments.get("parameters", {}),
        }
        server.skills_config.append(skill)
        server._save_data("skills")
        server.log_message("info", f"创建了 Skill: {skill_name}", important=True)
        return _ok({"skill": skill}, "已创建 Skill 配置。")

    if name == "ai.config.summary":
        config = dict(getattr(server, "ai_config", {}) or {})
        for key in ("api_key", "search_api_key", "silicon_api_key"):
            if config.get(key):
                config[key] = "********"
        config["model"] = getattr(server, "ai_model", None) or config.get("model")
        config["base_url"] = getattr(server, "ai_base_url", None) or config.get("base_url")
        return _ok({"ai_config": config}, "已读取 AI 配置摘要。")

    if name == "ai.config.update":
        allowed = {
            "provider",
            "provider_type",
            "base_url",
            "model",
            "temperature",
            "max_tokens",
            "top_p",
            "frequency_penalty",
            "presence_penalty",
            "system_prompt",
            "timeout",
            "retry_count",
            "stream",
            "enable_memory",
            "image_model",
            "embedding_model",
            "max_context_length",
            "supports_tools",
            "supports_reasoning",
            "supports_stream",
        }
        updates = {key: value for key, value in arguments.items() if key in allowed and value is not None}
        if not updates:
            return _err("No supported AI config fields provided")
        # max_context_length 最低 100k
        if "max_context_length" in updates:
            updates["max_context_length"] = max(100000, int(updates["max_context_length"]))
        server.ai_config.update(updates)
        if "base_url" in updates:
            server.ai_base_url = updates["base_url"]
        if "model" in updates:
            server.ai_model = updates["model"]
        reinitialized = None
        if getattr(server, "ai_api_key", None) and getattr(server, "ai_base_url", None):
            reinitialized = bool(server._initialize_ai_client())
        server._save_data("ai_config")
        server.log_message("info", f"修改了 AI 配置: {', '.join(sorted(updates.keys()))}", important=True)
        return _ok(
            {"updated": sorted(updates.keys()), "reinitialized": reinitialized},
            "已更新 AI 配置。",
        )

    if name == "theme.update":
        settings = arguments.get("settings")
        if not isinstance(settings, dict):
            return _err("settings must be an object")
        server.settings.update(settings)
        server._save_data("settings")
        server.log_message("info", "修改了界面主题/设置", important=True)
        return _ok({"settings": server.settings}, "已更新 Web 界面设置。")

    return _err(f"Tool not implemented: {name}", 501)


def _extract_json_object(text):
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").strip()
        if text.startswith("json"):
            text = text[4:].strip()
    try:
        value = json.loads(text)
        return value if isinstance(value, dict) else None
    except (TypeError, ValueError):
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start >= 0 and end > start:
        try:
            value = json.loads(text[start : end + 1])
            return value if isinstance(value, dict) else None
        except ValueError:
            return None
    return None


def run_web_agent_turn(server, instruction, allow_write=False):
    if not instruction or not getattr(server, "ai_client", None):
        return {"success": False, "used_tool": False, "message": ""}

    tools = get_web_agent_tools()
    prompt = (
        "你是 NekoBot Web Agent，可以选择一个后台工具帮助用户。\n"
        "如果不需要工具，返回 {\"tool\": null, \"arguments\": {}, \"reply\": \"一句自然回复\"}。\n"
        "如果需要工具，返回 {\"tool\": \"工具名\", \"arguments\": {...}, \"reply\": \"简短说明\"}。\n"
        "只返回 JSON，不要 Markdown。\n"
        "写入类工具会修改配置；除非用户明确要求创建/修改/更新，否则不要选择写入工具。\n\n"
        f"可用工具：{json.dumps(tools, ensure_ascii=False)}"
    )
    response = server.ai_client.chat_completion(
        model=getattr(server, "ai_model", None),
        messages=[{"role": "system", "content": prompt}, {"role": "user", "content": instruction}],
        stream=False,
    )
    try:
        raw = response.choices[0].message.content
    except Exception:
        raw = str(response)
    decision = _extract_json_object(raw) or {}
    tool = decision.get("tool")
    if not tool:
        return {"success": True, "used_tool": False, "message": decision.get("reply") or raw.strip()}

    result = execute_web_agent_tool(server, tool, decision.get("arguments") or {}, confirm=allow_write)
    if result.get("requires_confirmation"):
        return {
            "success": True,
            "used_tool": True,
            "requires_confirmation": True,
            "tool": tool,
            "arguments": decision.get("arguments") or {},
            "message": result.get("message", "这个操作需要确认后执行。"),
        }

    return {
        "success": bool(result.get("success")),
        "used_tool": True,
        "tool": tool,
        "tool_result": result,
        "message": _summarize_tool_result(tool, result),
    }


def _summarize_tool_result(tool, result):
    if not result.get("success"):
        return result.get("error") or "工具执行失败。"
    data = result.get("data") or {}
    if tool == "tokens.summary":
        return f"今天 Token 约 {data.get('today', 0)}，本月约 {data.get('month', 0)}。"
    if tool == "logs.recent":
        logs = data.get("logs", [])
        return f"最近有 {len(logs)} 条日志，最新一条是：{logs[-1].get('message', '无') if logs else '无'}"
    if tool == "system.status":
        return f"系统已启动：{data.get('startup_ready')}，当前模型：{data.get('ai_model') or '未配置'}。"
    return result.get("message") or "工具执行完成。"

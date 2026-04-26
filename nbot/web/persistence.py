import json
import logging
import os
from datetime import datetime
from nbot.web.sessions_db import load_sessions as load_sessions_from_db
from nbot.web.sessions_db import save_sessions as save_sessions_to_db

_log = logging.getLogger(__name__)


def _derive_session_name(session_id, session):
    existing_name = (session or {}).get("name")
    if existing_name:
        return existing_name

    messages = (session or {}).get("messages", [])
    for message in messages:
        if message.get("role") == "user":
            content = (message.get("content") or "").strip()
            if content:
                return content[:24] + ("..." if len(content) > 24 else "")

    return f"会话 {session_id[:8]}"


def _normalize_session_record(session_id, session):
    session = dict(session or {})
    messages = session.get("messages")
    if not isinstance(messages, list):
        messages = []

    system_prompt = session.get("system_prompt")
    if not system_prompt:
        for message in messages:
            if message.get("role") == "system":
                system_prompt = message.get("content", "")
                break

    created_at = session.get("created_at")
    if not created_at:
        for message in messages:
            if message.get("timestamp"):
                created_at = message.get("timestamp")
                break
    if not created_at:
        created_at = datetime.now().isoformat()

    normalized = {
        **session,
        "id": session.get("id") or session_id,
        "name": _derive_session_name(session_id, session),
        "type": session.get("type") or "web",
        "created_at": created_at,
        "messages": messages,
        "system_prompt": system_prompt or "",
    }
    return normalized


def init_default_data(server):
    """初始化默认数据"""
    # 默认工作流
    server.workflows = [
        {
            "id": "1",
            "name": "早安问候",
            "description": "每天早上向群组发送问候消息",
            "enabled": True,
            "trigger": "cron",
            "config": {"time": "08:00"},
        },
        {
            "id": "2",
            "name": "天气提醒",
            "description": "定时获取并发送天气信息",
            "enabled": True,
            "trigger": "cron",
            "config": {"time": "07:00"},
        },
        {
            "id": "3",
            "name": "新闻推送",
            "description": "每日新闻摘要推送",
            "enabled": False,
            "trigger": "cron",
            "config": {"time": "09:00"},
        },
        {
            "id": "4",
            "name": "自动回复",
            "description": "基于关键词的自动回复",
            "enabled": True,
            "trigger": "message",
            "config": {"keywords": ["帮助", "help"]},
        },
    ]

    # 加载人格提示词
    server._load_personality()

    # 默认 AI 配置
    server.ai_config = {
        "provider": "openai",
        "provider_type": "openai_compatible",
        "api_key": "",
        "base_url": "",
        "model": server.ai_model or "gpt-4",
        "temperature": 0.7,
        "max_tokens": 2000,
        "top_p": 0.9,
        "supports_tools": True,
        "supports_reasoning": True,
        "supports_stream": True,
    }
    server.scheduled_tasks = []

    # 默认 Token 统计
    server.token_stats = {
        "today": 0,
        "month": 0,
        "avg_per_chat": 0,
        "estimated_cost": "0.00",
        "history": [],
        "sessions": {},
    }

    # 默认系统日志
    server.system_logs = [
        {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": "info",
            "message": "Web server started",
        }
    ]

    # 默认设置
    server.settings = {
        "web_port": 5000,
        "ws_uri": "ws://127.0.0.1:3001",
        "master_id": "",
        "bot_id": "",
        "max_context_length": 50,
        "features": {
            "ai": True,
            "memory": True,
            "knowledge": True,
            "tts": False,
            "workflow": True,
            "web": True,
        },
    }


def init_default_skills(server):
    """初始化默认 Skills 配置（支持HTTP请求模板）"""
    server.skills_config = [
        {
            "id": "search",
            "name": "search",
            "description": "搜索互联网获取最新信息，适用于询问天气、新闻、实时数据等需要最新信息的问题",
            "aliases": ["搜索", "查找", "联网搜索"],
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"}
                },
                "required": ["query"],
            },
            "implementation": {
                "type": "http",
                "method": "POST",
                "url": "{{search_api_url}}",
                "headers": {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer {{search_api_key}}",
                },
                "body": {"query": "{{query}}", "query_rewrite": True, "top_k": 6},
                "response_path": "result.search_result",
                "error_message": "搜索服务未配置",
            },
        },
        {
            "id": "image_search",
            "name": "image_search",
            "description": "搜索相关图片，适用于需要展示图片的场景",
            "aliases": ["搜图", "找图"],
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "keyword": {"type": "string", "description": "图片关键词"}
                },
                "required": ["keyword"],
            },
            "implementation": {
                "type": "static",
                "response": "[图片搜索] 关键词: {{keyword}}",
            },
        },
    ]
    server._save_data("skills")


def init_default_tools(server):
    """初始化默认 Tools 配置（支持HTTP请求模板）"""
    server.tools_config = [
        {
            "id": "search_news",
            "name": "search_news",
            "description": "搜索最新新闻。当用户需要获取新闻资讯时使用此工具。",
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "搜索关键词，如'科技'、'体育'、'财经'等，默认为'热点新闻'",
                    },
                    "count": {
                        "type": "integer",
                        "description": "返回的新闻数量，默认5条",
                        "default": 5,
                    },
                },
            },
            "implementation": {
                "type": "minimax_web_search",
            },
        },
        {
            "id": "get_weather",
            "name": "get_weather",
            "description": "查询指定城市的天气信息。当用户询问天气时使用此工具。",
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，如'北京'、'上海'、'广州'等，默认'北京'",
                    }
                },
            },
            "implementation": {
                "type": "http",
                "method": "GET",
                "url": "https://wttr.in/{{city}}?format=j1",
                "headers": {"User-Agent": "Mozilla/5.0"},
                "response_path": "current_condition.0",
                "transform": {
                    "city": "{{city}}",
                    "temperature": "{{temp_C}}",
                    "description": "{{lang_zh.0.value}}",
                },
            },
        },
        {
            "id": "search_web",
            "name": "search_web",
            "description": "搜索网页内容。当需要查询网络信息时使用此工具。",
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词"},
                    "num_results": {
                        "type": "integer",
                        "description": "返回结果数量，默认3条",
                        "default": 3,
                    },
                },
                "required": ["query"],
            },
            "implementation": {
                "type": "minimax_web_search",
            },
        },
        {
            "id": "get_date_time",
            "name": "get_date_time",
            "description": "获取当前日期和时间信息。",
            "enabled": True,
            "parameters": {"type": "object", "properties": {}},
            "implementation": {
                "type": "python",
                "code": "import datetime; return datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')",
            },
        },
        {
            "id": "http_get",
            "name": "http_get",
            "description": "发送 HTTP GET 请求获取网页内容。",
            "enabled": True,
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "要访问的 URL 地址"}
                },
                "required": ["url"],
            },
            "implementation": {
                "type": "http",
                "method": "GET",
                "url": "{{url}}",
                "headers": {"User-Agent": "Mozilla/5.0"},
                "max_length": 2000,
            },
        },
        {
            "id": "exec_command",
            "name": "exec_command",
            "description": "执行命令行命令。默认禁用；必须显式设置 NBOT_ENABLE_EXEC_COMMAND=1 才允许使用。",
            "enabled": False,
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "要执行的命令行命令，如'ls -la'、'cat file.txt'、'python script.py'等",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "命令超时时间（秒），默认30秒",
                        "default": 30,
                    },
                    "confirmed": {
                        "type": "boolean",
                        "description": "是否已经用户确认。首次调用时设为false，如果返回需要确认，则用户确认后再次调用设为true",
                        "default": False,
                    },
                },
                "required": ["command"],
            },
            "implementation": {"type": "builtin", "handler": "exec_command"},
        },
    ]
    server._save_data("tools")


def load_all_data(server):
    """加载所有持久化数据"""
    try:
        # 加载会话
        loaded_sessions = load_sessions_from_db(server.data_dir)
        if not loaded_sessions:
            sessions_file = os.path.join(server.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                with open(sessions_file, "r", encoding="utf-8") as f:
                    loaded_sessions = json.load(f)
        if loaded_sessions:
            normalized_sessions = {
                session_id: _normalize_session_record(session_id, session)
                for session_id, session in loaded_sessions.items()
            }
            server.sessions.clear()
            server.sessions.update(normalized_sessions)
            save_sessions_to_db(server.data_dir, normalized_sessions)
            # 重新设置 sessions 到 ProgressCardManager
            if server.PROGRESS_CARD_AVAILABLE and server.progress_card_manager:
                server.progress_card_manager.set_sessions(server.sessions)
                _log.info("[ProgressCard] 重新设置 sessions 到 ProgressCardManager")

            # 重新设置 sessions 到 TodoCardManager
            if server.TODO_CARD_AVAILABLE and server.todo_card_manager:
                server.todo_card_manager.set_sessions(server.sessions)
                _log.info("[TodoCard] 重新设置 sessions 到 TodoCardManager")

        # 加载工作流
        workflows_file = os.path.join(server.data_dir, "workflows.json")
        if os.path.exists(workflows_file):
            with open(workflows_file, "r", encoding="utf-8") as f:
                server.workflows = json.load(f)

        # 加载记忆
        memories_file = os.path.join(server.data_dir, "memories.json")
        if os.path.exists(memories_file):
            with open(memories_file, "r", encoding="utf-8") as f:
                server.memories = json.load(f)

        # 如果 server.prompt_manager 可用，同步记忆数据
        if server.PROMPT_MANAGER_AVAILABLE and server.prompt_manager:
            try:
                # server.prompt_manager 会自动从自己的文件加载，这里确保 server.memories 也同步
                prompt_memories = server.prompt_manager.get_memories()
                if prompt_memories and not server.memories:
                    # 如果 server.prompt_manager 有数据但 server.memories 为空，使用 server.prompt_manager 的数据
                    server.memories = prompt_memories
                elif server.memories and not prompt_memories:
                    # 如果 server.memories 有数据但 server.prompt_manager 为空，同步到 server.prompt_manager
                    for mem in server.memories:
                        # 兼容旧格式数据
                        title = mem.get("title", mem.get("key", ""))
                        content = mem.get("content", mem.get("value", ""))
                        summary = mem.get("summary", None)
                        server.prompt_manager.add_memory(
                            title,
                            content,
                            mem.get("target_id", ""),
                            summary,
                            mem.get("type", "long"),
                            mem.get("expire_days", 7),
                        )
            except Exception as e:
                _log.error(f"Failed to sync memories with server.prompt_manager: {e}")

        # 知识库现在由 knowledge_manager 管理，不再从旧文件加载
        # if os.path.exists(knowledge_file):
        #     with open(knowledge_file, 'r', encoding='utf-8') as f:
        #         server.knowledge_docs = json.load(f)

        # 加载 AI 配置
        ai_config_file = os.path.join(server.data_dir, "ai_config.json")
        if os.path.exists(ai_config_file):
            with open(ai_config_file, "r", encoding="utf-8") as f:
                saved_config = json.load(f)
                server.ai_config.update(saved_config)

        # 加载 Token 统计
        token_stats_file = os.path.join(server.data_dir, "token_stats.json")
        if os.path.exists(token_stats_file):
            with open(token_stats_file, "r", encoding="utf-8") as f:
                saved_stats = json.load(f)
                # 检查是否是今天的数据
                today_str = datetime.now().strftime("%Y-%m-%d")
                history = saved_stats.get("history", [])
                if history:
                    last_date = history[-1].get("date", "")
                    if last_date != today_str:
                        # 新的一天，将昨天的数据保存到历史，重置 today
                        yesterday_total = saved_stats.get("today", 0)
                        if yesterday_total > 0 and last_date:
                            saved_stats["history"].append(
                                {
                                    "date": last_date,
                                    "input": yesterday_total // 2,
                                    "output": yesterday_total // 2,
                                    "total": yesterday_total,
                                    "cost": 0.0,
                                    "message_count": 0,
                                }
                            )
                        saved_stats["today"] = 0
                server.token_stats = saved_stats

        # 加载设置
        settings_file = os.path.join(server.data_dir, "settings.json")
        if os.path.exists(settings_file):
            with open(settings_file, "r", encoding="utf-8") as f:
                saved_settings = json.load(f)
                server.settings.update(saved_settings)

        # 加载登录 Token（持久化存储，重启后仍然有效）
        login_tokens_file = os.path.join(server.data_dir, "login_tokens.json")
        if os.path.exists(login_tokens_file):
            try:
                with open(login_tokens_file, "r", encoding="utf-8") as f:
                    server.login_tokens = json.load(f)
                # 清理已过期的 token
                server._cleanup_expired_tokens()
                _log.info(f"[Auth] 已加载 {len(server.login_tokens)} 个登录 Token")
            except Exception as e:
                _log.warning(f"[Auth] 加载登录 Token 失败: {e}")
                server.login_tokens = {}

        # 加载 Heartbeat 配置
        heartbeat_file = os.path.join(server.data_dir, "heartbeat.json")
        if os.path.exists(heartbeat_file):
            with open(heartbeat_file, "r", encoding="utf-8") as f:
                saved_heartbeat = json.load(f)
                server.heartbeat_config.update(saved_heartbeat)

        tasks_file = os.path.join(server.data_dir, "scheduled_tasks.json")
        if os.path.exists(tasks_file):
            with open(tasks_file, "r", encoding="utf-8") as f:
                server.scheduled_tasks = json.load(f)

        # 加载多模型配置
        server._load_ai_models()

        # 加载 Skills 配置
        skills_file = os.path.join(server.data_dir, "skills.json")
        if os.path.exists(skills_file):
            with open(skills_file, "r", encoding="utf-8") as f:
                server.skills_config = json.load(f)
        else:
            # 初始化默认 skills 配置
            server._init_default_skills()

        # 加载 Tools 配置
        tools_file = os.path.join(server.data_dir, "tools.json")
        if os.path.exists(tools_file):
            with open(tools_file, "r", encoding="utf-8") as f:
                server.tools_config = json.load(f)
        else:
            # 初始化默认 tools 配置
            server._init_default_tools()

        # 加载自定义人格预设
        custom_presets_file = os.path.join(
            server.data_dir, "custom_personality_presets.json"
        )
        if os.path.exists(custom_presets_file):
            with open(custom_presets_file, "r", encoding="utf-8") as f:
                server.custom_personality_presets = json.load(f)

        # 加载系统日志
        logs_file = os.path.join(server.data_dir, "system_logs.json")
        if os.path.exists(logs_file):
            with open(logs_file, "r", encoding="utf-8") as f:
                server.system_logs = json.load(f)
        else:
            # 初始化默认日志
            server.system_logs = [
                {
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "level": "info",
                    "message": "Web server started",
                }
            ]
            server._save_data("logs")

        # 启动 Heartbeat 调度器
        server._init_heartbeat_scheduler()

    except Exception as e:
        _log.error(f"Failed to load data: {e}")


def save_data(server, data_type: str):
    """保存指定类型的数据到磁盘"""
    try:
        if data_type == "sessions":
            save_sessions_to_db(server.data_dir, server.sessions)
            server._invalidate_sessions_cache()
        elif data_type == "workflows":
            with open(
                os.path.join(server.data_dir, "workflows.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.workflows, f, ensure_ascii=False, indent=2)
        elif data_type == "memories":
            with open(
                os.path.join(server.data_dir, "memories.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.memories, f, ensure_ascii=False, indent=2)
        elif data_type == "knowledge":
            pass  # 知识库由 knowledge_manager 管理，无需保存
        elif data_type == "ai_config":
            with open(
                os.path.join(server.data_dir, "ai_config.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.ai_config, f, ensure_ascii=False, indent=2)
        elif data_type == "token_stats":
            with open(
                os.path.join(server.data_dir, "token_stats.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(server.token_stats, f, ensure_ascii=False, indent=2)
        elif data_type == "settings":
            with open(
                os.path.join(server.data_dir, "settings.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.settings, f, ensure_ascii=False, indent=2)
        elif data_type == "heartbeat":
            with open(
                os.path.join(server.data_dir, "heartbeat.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.heartbeat_config, f, ensure_ascii=False, indent=2)
        elif data_type == "scheduled_tasks":
            with open(
                os.path.join(server.data_dir, "scheduled_tasks.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(server.scheduled_tasks, f, ensure_ascii=False, indent=2)
        elif data_type == "ai_models":
            with open(
                os.path.join(server.data_dir, "ai_models.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(
                    {
                        "models": server.ai_models,
                        "active_model_id": server.active_model_id,
                        "active_models_by_purpose": getattr(server, "active_models_by_purpose", {}),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        elif data_type == "skills":
            with open(
                os.path.join(server.data_dir, "skills.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.skills_config, f, ensure_ascii=False, indent=2)
        elif data_type == "tools":
            with open(
                os.path.join(server.data_dir, "tools.json"), "w", encoding="utf-8"
            ) as f:
                json.dump(server.tools_config, f, ensure_ascii=False, indent=2)
        elif data_type == "logs":
            with open(
                os.path.join(server.data_dir, "system_logs.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(server.system_logs, f, ensure_ascii=False, indent=2)
        elif data_type == "custom_personality_presets":
            with open(
                os.path.join(server.data_dir, "custom_personality_presets.json"),
                "w",
                encoding="utf-8",
            ) as f:
                json.dump(
                    server.custom_personality_presets, f, ensure_ascii=False, indent=2
                )
    except Exception as e:
        _log.error(f"Failed to save {data_type}: {e}")

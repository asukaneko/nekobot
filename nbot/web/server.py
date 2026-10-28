"""
Web 聊天服务后端
提供 REST API 和 WebSocket 接口
"""

import hashlib
import json
import logging
import os
import re
import secrets
import threading
import time
import uuid
from datetime import datetime, timedelta
from typing import Any

from flask import Flask, g, jsonify, request
from flask_socketio import SocketIO
from werkzeug.exceptions import HTTPException

from nbot.core.prompt_format import format_skills_prompt
from nbot.core.sampling import default_max_tokens_for, request_extra_body
from nbot.core.session_store import visible_messages
from nbot.web.ai_service import (
    get_ai_response,
    get_ai_response_with_images,
    get_ai_response_with_tools,
    parse_tool_call_from_text,
    stream_ai_response,
    stream_send_response,
    trigger_ai_response_for_request,
)
from nbot.web.log_cleanup import cleanup_logs_dir
from nbot.web.persistence import (
    init_default_data,
    init_default_skills,
    init_default_tools,
    load_all_data,
    save_data,
)
from nbot.web.routes import (
    register_plot_routes,
    register_plugin_routes,
    register_admin_misc_routes,
    register_ai_config_routes,
    register_ai_model_routes,
    register_api_key_routes,
    register_auth_routes,
    register_channel_routes,
    register_character_routes,
    register_config_legacy_routes,
    register_config_transfer_routes,
    register_export_center_routes,
    register_file_routes,
    register_gateway_log_routes,
    register_global_search_routes,
    register_gateway_routes,
    register_heartbeat_routes,
    register_hook_routes,
    register_image_routes,
    register_group_routes,
    register_knowledge_routes,
    register_live2d_routes,
    register_login_token_routes,
    register_mcp_server_routes,
    register_memory_routes,
    register_message_filter_routes,
    register_personality_routes,
    register_persona_routes,
    register_prompt_preset_routes,
    register_public_session_routes,
    register_push_routes,
    register_qq_overview_routes,
    register_qrcode_routes,
    register_review_routes,
    register_session_routes,
    register_skill_routes,
    register_skills_storage_routes,
    register_ssl_validation_routes,
    register_task_center_routes,
    register_tool_routes,
    register_tts_playground_routes,
    register_update_routes,
    register_voice_routes,
    register_web_agent_routes,
    register_webdav_backup_routes,
    register_workflow_routes,
    register_workspace_misc_routes,
    register_oauth_routes,
    register_workspace_private_routes,                                       
    register_workspace_shared_routes,                                        
    register_world_book_routes,
    register_world_routes,
    register_achievement_routes,
    register_smart_routing_routes,
    register_sticker_routes,
)
from nbot.core.oauth import init_oauth_manager
from nbot.web.secure_store import read_secure_json, write_secure_json
from nbot.web.socket_events import register_socket_events
from nbot.web.world_socket_events import register_world_socket_events

_log = logging.getLogger(__name__)


class _HeartbeatJobLogFilter(logging.Filter):
    """抑制 heartbeat 定时任务的 per-tick 日志（1 分钟一次轮询，纯噪音）。

    apscheduler.executors.default 会在每次 tick 时输出
    `Running job "WebChatServer._start_heartbeat_job.<locals>.run_heartbeat_sync ..."`
    和对应的 `executed successfully`，但心跳是「高频轮询 + 内部 backoff」设计，
    这些日志对调试没有帮助，反而会刷屏。匹配到 heartbeat 任务名时直接丢弃。
    """

    _NEEDLE = "run_heartbeat_sync"

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        return self._NEEDLE not in record.getMessage()


def _resolve_web_adapter(adapter):
    if adapter:
        return adapter
    try:
        if get_channel_adapter:
            web_adapter = get_channel_adapter("web")
            if web_adapter:
                return web_adapter
        return WebChannelAdapter() if WebChannelAdapter else None
    except NameError:
        return None


def _build_heartbeat_user_message(adapter, session_id: str, content: str) -> dict:
    web_adapter = _resolve_web_adapter(adapter)
    if web_adapter and hasattr(web_adapter, "build_heartbeat_user_message"):
        return web_adapter.build_heartbeat_user_message(session_id, content)
    if web_adapter:
        return web_adapter.build_message(
            role="user",
            content=f"【Heartbeat 任务】\n{content}",
            sender="system",
            conversation_id=session_id,
            metadata={
                "source": "heartbeat",
                "is_heartbeat": True,
                "hide_in_web": False,
            },
        )
    return {
        "role": "user",
        "content": f"【Heartbeat 任务】\n{content}",
        "timestamp": datetime.now().isoformat(),
        "sender": "system",
        "source": "heartbeat",
        "is_heartbeat": True,
        "hide_in_web": False,
    }


def _build_heartbeat_assistant_message(adapter, session_id: str, content: str) -> dict:
    web_adapter = _resolve_web_adapter(adapter)
    if web_adapter and hasattr(web_adapter, "build_heartbeat_assistant_message"):
        return web_adapter.build_heartbeat_assistant_message(session_id, content)
    if web_adapter:
        return web_adapter.build_assistant_message(
            ChatResponse(final_content=content),
            conversation_id=session_id,
            sender="AI",
            metadata={
                "source": "heartbeat",
                "is_heartbeat": True,
                "hide_in_web": False,
            },
        )
    return {
        "role": "assistant",
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "sender": "AI",
        "source": "heartbeat",
        "is_heartbeat": True,
        "hide_in_web": False,
    }


def _build_workflow_user_message(
    adapter, session_id: str, content: str, workflow_id: str
) -> dict:
    web_adapter = _resolve_web_adapter(adapter)
    if web_adapter and hasattr(web_adapter, "build_workflow_user_message"):
        return web_adapter.build_workflow_user_message(session_id, content, workflow_id)
    if web_adapter:
        return web_adapter.build_message(
            role="user",
            content=content,
            sender="user",
            conversation_id=session_id,
            metadata={"workflow_id": workflow_id},
        )
    return {
        "id": str(uuid.uuid4()),
        "role": "user",
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "sender": "user",
        "workflow_id": workflow_id,
    }


def _build_workflow_assistant_message(
    adapter, session_id: str, content: str, workflow_id: str
) -> dict:
    web_adapter = _resolve_web_adapter(adapter)
    if web_adapter and hasattr(web_adapter, "build_workflow_assistant_message"):
        return web_adapter.build_workflow_assistant_message(
            session_id, content, workflow_id
        )
    if web_adapter:
        return web_adapter.build_assistant_message(
            ChatResponse(final_content=content),
            conversation_id=session_id,
            sender="AI",
            metadata={"workflow_id": workflow_id},
        )
    return {
        "id": str(uuid.uuid4()),
        "role": "assistant",
        "content": content,
        "timestamp": datetime.now().isoformat(),
        "sender": "AI",
        "workflow_id": workflow_id,
    }


def _build_web_manager_payload(
    adapter,
    message: dict,
    *,
    default_role: str,
    default_content: str,
    default_sender: str,
    default_conversation_id: str,
    metadata: dict[str, Any] | None = None,
) -> dict:
    manager_adapter = _resolve_web_adapter(adapter)
    if manager_adapter:
        return manager_adapter.build_manager_payload_from_message(
            message,
            default_role=default_role,
            default_content=default_content,
            default_sender=default_sender,
            default_conversation_id=default_conversation_id,
            metadata=metadata,
        )
    payload = {
        "role": default_role,
        "content": default_content,
        "sender": default_sender,
        "source": "web",
        "session_id": default_conversation_id,
    }
    if metadata:
        payload["metadata"] = dict(metadata)
    return payload

# 固定的核心指令 - 这些功能不会因为用户修改提示词而丢失
CORE_INSTRUCTIONS = """【重要】你必须严格遵循以下要求：

1. 直接回复用户的问题，不要使用任何特殊格式
2. 你的回答应该是自然的对话形式
3. 如果需要执行操作（如搜索新闻、查询天气、保存记忆等），请使用可用的工具

【工具调用规则 - 非常重要】
- 当需要使用工具时，**必须通过 tool_calls 格式调用**，不要把工具信息作为普通文本输出
- **绝对不要**输出类似 `[TOOL_CALL]` 或 `minimax:tool_call` 这样的格式
- 工具调用由系统自动处理，你只需要描述你需要执行的操作
- 工具返回结果后，用自然的语言向用户解释结果
- 如果你不确定如何调用工具，直接回答用户问题而不是尝试使用工具

【文件写入规则】
- 调用 write_file 工具写入文件内容时，每次写入的内容不宜过长（建议不超过 2000 字符）
- 如果需要写入大量内容，应该分多次调用 write_file 工具，每次写入一部分
- 例如要写入 5000 字的内容，应该分 3 次写入，每次数百到一千多字
- 不要尝试一次性写入过长的内容，这会导致写入失败

【文件处理指南】
当用户上传文件时，会显示文件元数据（类型、大小、页数等）。
- 如果需要查看文件内容，调用 workspace_parse_file 工具
- 工具返回结果后，用自然的语言向用户解释文件内容
- 不要直接返回原始JSON，要格式化和总结重要信息

【文件发送指南】
当用户要求发送文件时，调用 workspace_send_file 工具。
- 工具执行成功后，文件会自动发送给用户
- 你不需要在回复中提及文件路径或重复文件内容
- 只需简单告知用户文件已发送即可

现在你可以开始与用户对话了。"""

try:
    from apscheduler.schedulers.background import BackgroundScheduler
    from apscheduler.triggers.cron import CronTrigger

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False



# 导入知识库管理器
try:
    from nbot.core.knowledge import configure_knowledge_embedding, get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    configure_knowledge_embedding = None
    KNOWLEDGE_MANAGER_AVAILABLE = False
    _log.warning("Knowledge manager not available")

# 导入统一消息模块
try:
    from nbot.channels.registry import get_channel_adapter, register_channel_handler
    from nbot.channels.web import WebChannelAdapter
    from nbot.core import (
        AgentService,
        ChatResponse,
        WebSessionStore,
        create_message,
        message_manager,
    )

    MESSAGE_MODULE_AVAILABLE = True
except ImportError:
    MESSAGE_MODULE_AVAILABLE = False
    WebSessionStore = None
    AgentService = None
    WebChannelAdapter = None
    get_channel_adapter = None
    register_channel_handler = None
    message_manager = None
    create_message = None
    _log.warning("Message module not available")

# 导入 Prompt 管理器
try:
    from nbot.core.prompt import prompt_manager

    PROMPT_MANAGER_AVAILABLE = True
except ImportError:
    PROMPT_MANAGER_AVAILABLE = False
    prompt_manager = None
    _log.warning("Prompt manager not available")

# 导入工作区管理器
try:
    from nbot.core.workspace import workspace_manager

    WORKSPACE_AVAILABLE = True
except ImportError:
    WORKSPACE_AVAILABLE = False
    workspace_manager = None

# 导入进度卡片管理器
try:
    from nbot.core.progress_card import ProgressCard, StepType, progress_card_manager

    PROGRESS_CARD_AVAILABLE = True
except ImportError:
    PROGRESS_CARD_AVAILABLE = False
    progress_card_manager = None
    _log.warning("Progress card manager not available")

# 导入 Todo 卡片管理器
try:
    from nbot.core.todo_card import TodoCard, todo_card_manager

    TODO_CARD_AVAILABLE = True
except ImportError:
    TODO_CARD_AVAILABLE = False
    todo_card_manager = None
    _log.warning("Todo card manager not available")

# 导入文件解析器
try:
    from nbot.core.file_parser import file_parser

    FILE_PARSER_AVAILABLE = True
except ImportError:
    FILE_PARSER_AVAILABLE = False
    file_parser = None
    _log.warning("File parser not available")

# 导入配置加载器（支持 .env 环境变量）
try:
    from nbot.web.utils.config_loader import (
        get_api_config,
        get_pic_config,
        get_search_config,
        get_video_config,
        resolve_runtime_api_key,
    )

    CONFIG_LOADER_AVAILABLE = True
except ImportError:
    CONFIG_LOADER_AVAILABLE = False
    get_api_config = None
    get_pic_config = None
    get_search_config = None
    resolve_runtime_api_key = None
    get_video_config = None
    _log.warning("Config loader not available")


class WebChatServer:
    """Web 聊天服务器"""

    _instance = None

    @classmethod
    def get_instance(cls):
        """获取单例实例"""
        return cls._instance

    @staticmethod
    def _create_scheduler():
        if not APSCHEDULER_AVAILABLE:
            return None
        scheduler = BackgroundScheduler()
        scheduler.start()
        return scheduler

    def __init__(self, app: Flask, socketio: SocketIO):
        cls = self.__class__
        if cls._instance is not None:
            raise RuntimeError(
                "WebChatServer 只能有一个实例，请使用 get_instance() 获取"
            )
        cls._instance = self

        self.app = app
        self.socketio = socketio
        self.static_folder = os.path.join(os.path.dirname(__file__), "static")
        self.base_dir = os.path.join(os.path.dirname(__file__), "..", "..")
        self.MESSAGE_MODULE_AVAILABLE = MESSAGE_MODULE_AVAILABLE
        self.message_manager = message_manager
        self.create_message = create_message
        self.KNOWLEDGE_MANAGER_AVAILABLE = KNOWLEDGE_MANAGER_AVAILABLE
        self.get_knowledge_manager = get_knowledge_manager
        self.PROMPT_MANAGER_AVAILABLE = PROMPT_MANAGER_AVAILABLE
        self.prompt_manager = prompt_manager
        self.PROGRESS_CARD_AVAILABLE = PROGRESS_CARD_AVAILABLE
        self.progress_card_manager = progress_card_manager
        self.TODO_CARD_AVAILABLE = TODO_CARD_AVAILABLE
        self.todo_card_manager = todo_card_manager
        self.WORKSPACE_AVAILABLE = WORKSPACE_AVAILABLE
        self.workspace_manager = workspace_manager
        self.FILE_PARSER_AVAILABLE = FILE_PARSER_AVAILABLE
        self.file_parser = file_parser

        self.sessions: dict[str, dict[str, Any]] = {}
        self.session_store = WebSessionStore(
            self.sessions, save_callback=lambda: self._save_data("sessions")
        )
        self.agent_service = AgentService()
        web_handler = (
            lambda chat_request, adapter=None, server=self: trigger_ai_response_for_request(
                server, chat_request, adapter=adapter
            )
        )
        self.agent_service.register_handler("web", web_handler)
        if register_channel_handler:
            register_channel_handler("web", web_handler)
        self.web_channel_adapter = _resolve_web_adapter(None)

        # 初始化进度卡片管理器
        if PROGRESS_CARD_AVAILABLE and progress_card_manager:
            progress_card_manager.set_socketio(socketio)
            progress_card_manager.set_sessions(self.sessions)
            _log.info("[ProgressCard] 进度卡片管理器已初始化")

        # 初始化 Todo 卡片管理器
        if TODO_CARD_AVAILABLE and todo_card_manager:
            todo_card_manager.set_socketio(socketio)
            todo_card_manager.set_sessions(self.sessions)
            _log.info("[TodoCard] Todo 卡片管理器已初始化")
        self.web_users: dict[str, str] = {}
        self.active_connections: dict[str, str] = {}
        self.visible_web_sessions: dict[str, str] = {}

        # 数据存储目录
        self.data_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "web"
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # OAuth 管理器（在 data_dir 之后初始化）
        self.oauth_manager = init_oauth_manager(self.data_dir)

        # Token 统计管理器（统一持久化，必须最先初始化）
        from nbot.core.token_stats import init_token_stats_manager
        self.token_stats_manager = init_token_stats_manager(self.data_dir)
        self.token_stats: dict = self.token_stats_manager.data  # 兼容旧引用

        # 故障转移健康状态（持久化）
        from nbot.core.failover import init_failover_state
        init_failover_state(self.data_dir)

        # 内存数据存储
        self.workflows: list[dict] = []
        self.memories: list[dict] = []
        # knowledge_docs 已移除，由 knowledge_manager 管理
        self.ai_config: dict = {}
        self.custom_personality_presets: list[dict] = []  # 自定义人格预设
        self.personality: dict = {}
        self.system_logs: list[dict] = []
        self.settings: dict = {}

        # 性能优化：统计缓存
        self._stats_cache: dict = {}
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 5.0  # 缓存5秒

        # Skills 配置
        self.skills_config: list[dict] = []

        # Tools 配置
        self.tools_config: list[dict] = []
        self.channels_config: list[dict] = []

        # Heartbeat 配置
        # AI 立绘生成异步任务管理
        self.portrait_generation_tasks: dict = {}  # task_id -> {status, character_name, portrait_url, error, updated_at}
        self.pending_portraits: dict = {}  # character_name -> portrait_url

        self.heartbeat_config: dict = {
            "enabled": False,
            "interval_minutes": 60,
            "content_file": "heartbeat.md",
            "target_session_id": "",
            "targets": [],
            "last_run": None,
            "next_run": None,
        }

        # Heartbeat 调度器
        from nbot.gateway.gateway import get_gateway
        from nbot.gateway.heartbeat import (
            SessionHeartbeatManager,
            set_session_heartbeat_manager,
        )

        self.session_heartbeat_manager = SessionHeartbeatManager(
            data_dir=self.data_dir,
            gateway_getter=get_gateway,
            executor=self._run_session_heartbeat_execution,
            activity_getter=self._get_session_last_activity_by_id,
        )
        set_session_heartbeat_manager(self.session_heartbeat_manager)
        self._refresh_heartbeat_summary_config()
        self.heartbeat_job = None
        self.proactive_chat_thread: threading.Thread | None = None
        self.scheduled_tasks: list[dict[str, Any]] = []
        self.scheduled_task_jobs: dict[str, Any] = {}
        self.running_task_ids: set = set()
        self.running_workflow_ids: set = set()

        # 系统启动时间
        self.start_time = time.time()

        # AI 客户端引用
        self.ai_client = None
        self.ai_model = None
        self.ai_api_key = None
        self.ai_base_url = None

        # 多模型配置管理
        self.ai_models: list[dict] = []
        self.active_model_id: str = None
        self.active_models_by_purpose: dict[str, str] = {}

        # 工作流调度器
        self.scheduler = None
        if APSCHEDULER_AVAILABLE:
            try:
                # 尝试获取当前事件循环，如果没有则创建
                self.scheduler = self._create_scheduler()
            except Exception as e:
                _log.error(f"Failed to start scheduler: {e}")
                self.scheduler = None

        # QQ Bot 引用（用于发送消息到QQ）
        self.qq_bot = None

        # 登录密码（可能是明文或 bcrypt 哈希）
        self.web_password = None
        self._web_password_is_hash = False

        # 登录失败限流：{ip: {'count': int, 'first_fail': float}}
        self._login_fail_records: dict[str, dict[str, Any]] = {}
        self._login_rate_limit = 5          # 最大失败次数
        self._login_rate_window = 300       # 限流窗口（秒）

        # 登录 Token 管理（用于长时间免登录）
        # key 为 token 的 SHA-256 hash，value 为 {'username': str, 'expires_at': datetime, 'created_at': datetime}
        self.login_tokens: dict[str, dict[str, Any]] = {}
        self.token_expire_days = 30  # Token 有效期 30 天

        # 停止事件字典（用于取消 AI 生成）
        self.stop_events: dict[str, threading.Event] = {}
        self.startup_ready = False
        self.startup_error: str | None = None
        self.startup_thread: threading.Thread | None = None
        self.log_cleanup_thread: threading.Thread | None = None


        self._load_ai_config()
        self._load_web_config()
        self._register_routes()
        self._register_auth_middleware()
        self._register_socket_events()
        self._init_default_data()
        # 立即加载 personality，确保创建会话时可用
        self._load_personality()
        self._load_custom_personality_presets()
        self._start_background_initialization()

    def _auto_start_feishu_ws_channels(self):
        """自动启动所有已启用的飞书长连接频道"""
        try:
            from nbot.services.feishu_ws_service import feishu_ws_service
            from nbot.web.routes.channels import auto_start_feishu_ws_clients
            # 设置服务器实例
            feishu_ws_service.set_server(self)
            auto_start_feishu_ws_clients(self)
        except Exception as e:
            _log.error(f"自动启动飞书长连接频道失败: {e}")

    def _auto_start_qqbot_channels(self):
        """Auto-start enabled QQ official Lobster Bot channels."""
        try:
            from nbot.services.qqbot_service import qqbot_ws_service
            from nbot.web.routes.channels import auto_start_qqbot_clients

            qqbot_ws_service.set_server(self)
            auto_start_qqbot_clients(self)
        except Exception as e:
            _log.error("Failed to auto-start QQ Lobster Bot channels: %s", e)

    def _auto_connect_mcp_servers(self):
        """启动时自动连接所有 auto_connect=True 的 MCP 服务器（独立后台线程，不阻塞初始化）"""
        try:
            from nbot.services.mcp_bridge import get_mcp_bridge

            config_path = os.path.join(self.data_dir, "mcp_servers.json")
            if not os.path.exists(config_path):
                return

            with open(config_path, encoding="utf-8") as f:
                configs = json.load(f)
            if not isinstance(configs, list):
                return

            # 筛选需要自动连接的服务器
            targets = [
                cfg for cfg in configs
                if cfg.get("auto_connect") and cfg.get("enabled", True) and cfg.get("id")
            ]
            if not targets:
                return

            def _connect_all():
                bridge = get_mcp_bridge()
                connected_count = 0

                for cfg in targets:
                    server_id = cfg["id"]
                    transport = cfg.get("transport", "streamable-http")
                    if transport == "stdio" and not cfg.get("command"):
                        continue
                    if transport != "stdio" and not cfg.get("url"):
                        continue

                    try:
                        result = bridge.run_async(bridge.connect_server(server_id, cfg))
                        if result.get("ok"):
                            cfg["connected"] = True
                            cfg["tool_count"] = result.get("tool_count", 0)
                            cfg["last_connected_at"] = datetime.now().isoformat()
                            connected_count += 1
                            _log.info("[MCP] 自动连接成功: %s (%d 个工具)", cfg.get("name", server_id), cfg["tool_count"])
                        else:
                            _log.warning("[MCP] 自动连接失败: %s - %s", cfg.get("name", server_id), result.get("error", "unknown"))
                    except Exception as e:
                        _log.warning("[MCP] 自动连接异常: %s - %s", cfg.get("name", server_id), e)

                if connected_count > 0:
                    try:
                        with open(config_path, "w", encoding="utf-8") as f:
                            json.dump(configs, f, ensure_ascii=False, indent=2)
                    except Exception as e:
                        _log.warning("[MCP] 保存自动连接状态失败: %s", e)

                _log.info("[MCP] 自动连接完成: %d/%d 个服务器已连接", connected_count, len(targets))

            t = threading.Thread(target=_connect_all, name="mcp-auto-connect", daemon=True)
            t.start()
        except Exception as e:
            _log.error(f"自动连接 MCP 服务器失败: {e}")

    def _start_background_initialization(self):
        """Load heavier startup data after the server begins accepting requests."""

        def run():
            try:
                self._load_all_data()
                if self.active_model_id:
                    self._apply_ai_model(self.active_model_id)
                elif not self.ai_client:
                    self._initialize_ai_client()
                self._init_workflow_scheduler()
                self._init_custom_task_scheduler()
                self._start_log_cleanup_loop()
                # 检查并重建知识库索引（如有需要）
                self._check_knowledge_index()
                # 自动启动飞书长连接频道
                self._auto_start_feishu_ws_channels()
                self._auto_start_qqbot_channels()
                # 自动连接 MCP 服务器
                self._auto_connect_mcp_servers()
                self.startup_ready = True
                _log.info("Web server background initialization completed")
            except Exception as e:
                self.startup_error = str(e)
                _log.error(f"Web server background initialization failed: {e}")

        self.startup_thread = threading.Thread(
            target=run,
            name="web-startup-init",
            daemon=True,
        )
        self.startup_thread.start()

    def _start_log_cleanup_loop(self):
        if self.log_cleanup_thread and self.log_cleanup_thread.is_alive():
            return

        def run():
            while True:
                try:
                    cleanup = (self.settings or {}).get("log_cleanup") or {}
                    if cleanup.get("enabled"):
                        result = cleanup_logs_dir(self.base_dir, cleanup)
                        cleanup.update(
                            {
                                "last_run": datetime.now().isoformat(),
                                "last_deleted_count": result.get("deleted_count", 0),
                                "last_deleted_entries": result.get("deleted_entries", 0),
                                "last_freed_bytes": result.get("freed_bytes", 0),
                                "last_error": result.get("error", ""),
                            }
                        )
                        self.settings["log_cleanup"] = cleanup
                        system_logs_result = result.get("system_logs") or {}
                        token_stats_result = result.get("token_stats") or {}
                        if system_logs_result.get("data") is not None:
                            self.system_logs = system_logs_result["data"]
                        if token_stats_result.get("data") is not None:
                            try:
                                with self.token_stats_manager._lock:
                                    self.token_stats_manager._stats = token_stats_result["data"]
                                self.token_stats = self.token_stats_manager.data
                            except Exception:
                                pass
                        self._save_data("settings")
                        if result.get("deleted_count", 0):
                            self.log_message(
                                "info",
                                f"Log cleanup deleted {result['deleted_count']} files, freed {result['freed_bytes']} bytes",
                            )
                        elif result.get("deleted_entries", 0):
                            self.log_message(
                                "info",
                                f"Log cleanup deleted {result['deleted_entries']} JSON entries, freed {result['freed_bytes']} bytes",
                            )
                except Exception as e:
                    _log.warning("Log cleanup loop failed: %s", e, exc_info=True)
                time.sleep(3600)

        self.log_cleanup_thread = threading.Thread(
            target=run,
            name="log-cleanup",
            daemon=True,
        )
        self.log_cleanup_thread.start()

    def _format_uptime(self, seconds):
        """格式化运行时间"""
        days = seconds // 86400
        hours = (seconds % 86400) // 3600
        minutes = (seconds % 3600) // 60

        if days > 0:
            return f"{days}天{hours}小时"
        elif hours > 0:
            return f"{hours}小时{minutes}分钟"
        else:
            return f"{minutes}分钟"

    def _get_proactive_chat_config(self) -> dict[str, Any]:
        default_prompt = (
            "你正在一个 Web 会话里主动开口。请根据当前上下文，以角色本身的语气自然发起一条简短消息。"
            "可以关心近况、延续上一轮话题、提出一个轻量问题，或分享一个贴合关系的小观察。"
            "不要提到定时任务、系统触发、主动聊天功能或这段指令。"
        )
        config = {
            "enabled": False,
            "interval_minutes": 60,
            "idle_minutes": 10,
            "visible_only": True,
            "prompt": default_prompt,
        }
        try:
            config["interval_minutes"] = max(1, int(config.get("interval_minutes", 60)))
        except (TypeError, ValueError):
            config["interval_minutes"] = 60
        try:
            config["idle_minutes"] = max(1, int(config.get("idle_minutes", 10)))
        except (TypeError, ValueError):
            config["idle_minutes"] = 10
        config["enabled"] = bool(config.get("enabled", False))
        config["visible_only"] = bool(config.get("visible_only", True))
        if not str(config.get("prompt") or "").strip():
            config["prompt"] = default_prompt
        return config

    def _get_session_proactive_chat_config(self, session: dict[str, Any]) -> dict[str, Any]:
        config = self._get_proactive_chat_config()
        saved = session.get("proactive_chat")
        if isinstance(saved, dict):
            config.update(saved)
        try:
            config["interval_minutes"] = max(1, int(config.get("interval_minutes", 60)))
        except (TypeError, ValueError):
            config["interval_minutes"] = 60
        try:
            config["idle_minutes"] = max(1, int(config.get("idle_minutes", 10)))
        except (TypeError, ValueError):
            config["idle_minutes"] = 10
        config["enabled"] = bool(config.get("enabled", False))
        config["visible_only"] = bool(config.get("visible_only", True))
        if not str(config.get("prompt") or "").strip():
            config["prompt"] = self._get_proactive_chat_config()["prompt"]
        return config

    def _start_proactive_chat_loop(self):
        _log.info("[ProactiveChat] backend disabled; UI config retained")

    def _execute_proactive_chat_tick(self):
        return

    def _get_proactive_chat_target_session_ids(self) -> list[str]:
        return [
            session_id
            for session_id, session in self.sessions.items()
            if session.get("type", "web") == "web"
            and not session.get("archived")
            and not session.get("read_only")
            and isinstance(session.get("proactive_chat"), dict)
            and session.get("proactive_chat", {}).get("enabled")
        ]

    def _proactive_chat_session_is_due(
        self, session: dict[str, Any], now: datetime, config: dict[str, Any]
    ) -> bool:
        last_activity = self._get_session_last_activity(session)
        if not last_activity:
            return False

        idle_seconds = config["idle_minutes"] * 60
        if (now - last_activity).total_seconds() < idle_seconds:
            return False

        last_run = self._parse_iso_datetime(session.get("proactive_chat_last_run"))
        interval_seconds = config["interval_minutes"] * 60
        if last_run and (now - last_run).total_seconds() < interval_seconds:
            return False

        return True

    def _proactive_chat_has_unanswered_reply(self, session: dict[str, Any]) -> bool:
        # 如果主动聊天正在等待 AI 回复（pending_since 存在且 AI 尚未回复），跳过
        pending_since = self._parse_iso_datetime(session.get("proactive_chat_pending_since"))
        if pending_since:
            messages = [
                message
                for message in session.get("messages", [])
                if isinstance(message, dict) and message.get("role") != "system"
            ]
            # 检查 pending_since 之后是否有 AI 回复（主动聊天的回复）
            has_assistant_reply = any(
                isinstance(msg, dict)
                and msg.get("role") == "assistant"
                and (msg.get("is_proactive_chat") or msg.get("source") == "proactive_chat")
                and self._parse_iso_datetime(msg.get("timestamp"))
                and self._parse_iso_datetime(msg.get("timestamp")) >= pending_since
                for msg in messages
            )
            if has_assistant_reply:
                # AI 已回复，清除 pending 状态
                session.pop("proactive_chat_pending_since", None)
            else:
                # AI 尚未回复，跳过本次触发
                return True

        return False

    def _has_user_message_after(
        self, messages: list[dict[str, Any]], timestamp: datetime
    ) -> bool:
        for message in messages:
            if message.get("role") != "user":
                continue
            message_time = self._parse_iso_datetime(message.get("timestamp"))
            if message_time and message_time > timestamp:
                return True
        return False

    def _get_session_last_activity(self, session: dict[str, Any]) -> datetime | None:
        latest = None
        for message in session.get("messages", []):
            if message.get("role") == "system":
                continue
            timestamp = self._parse_iso_datetime(message.get("timestamp"))
            if timestamp and (latest is None or timestamp > latest):
                latest = timestamp
        return latest

    def _get_session_last_activity_by_id(self, session_id: str):
        """按 session_id 查询最后用户活动时间（供 heartbeat 指数退避使用）。

        只统计 user 角色消息的时间戳——用户是否在和角色互动，
        而不是 assistant（心跳也会产生 assistant 消息）。
        """
        try:
            session = self.session_store.get_session(session_id) or {}
            latest = None
            for message in session.get("messages", []):
                if message.get("role") != "user":
                    continue
                if message.get("is_heartbeat") or message.get("silent_trigger"):
                    continue
                timestamp = self._parse_iso_datetime(message.get("timestamp"))
                if timestamp and (latest is None or timestamp > latest):
                    latest = timestamp
            return latest
        except Exception:
            return None

    @staticmethod
    def _parse_iso_datetime(value: Any) -> datetime | None:
        if not value:
            return None
        if isinstance(value, (int, float)):
            try:
                return datetime.fromtimestamp(value)
            except (OverflowError, OSError, ValueError):
                return None
        try:
            text = str(value).strip().replace("Z", "+00:00")
            parsed = datetime.fromisoformat(text)
            if parsed.tzinfo is not None:
                parsed = parsed.astimezone().replace(tzinfo=None)
            return parsed
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _hash_token(token: str) -> str:
        """将明文 token 进行 SHA-256 哈希，用于安全存储"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _generate_login_token(self, username: str, ip_address: str = "") -> str:
        """
        生成登录 Token

        Args:
            username: 用户名
            ip_address: 创建令牌时的客户端 IP 地址

        Returns:
            token 字符串（明文，仅此一次返回）
        """
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)

        now = datetime.now()
        expires_at = now + timedelta(days=self.token_expire_days)

        # 存储 token hash 而非明文（附带前缀用于界面展示）
        self.login_tokens[token_hash] = {
            "username": username,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
            "token_prefix": token[:8],
            "ip_address": ip_address,
        }

        _log.info(f"[Auth] 生成登录 Token: username={username}, ip={ip_address}, expires={expires_at}")

        self._save_login_tokens()
        return token

    def _validate_login_token(self, token: str) -> str | None:
        """
        验证登录 Token

        Args:
            token: token 明文字符串

        Returns:
            验证成功返回用户名，失败返回 None
        """
        token_hash = self._hash_token(token)
        if not token_hash or token_hash not in self.login_tokens:
            return None

        token_info = self.login_tokens[token_hash]

        # 检查是否过期
        expires_at = datetime.fromisoformat(token_info["expires_at"])
        if datetime.now() > expires_at:
            del self.login_tokens[token_hash]
            _log.info(f"[Auth] Token 已过期: username={token_info['username']}")
            return None

        return token_info["username"]

    def _cleanup_expired_tokens(self):
        """清理过期的 Token"""
        now = datetime.now()
        expired_hashes = [
            token_hash
            for token_hash, info in self.login_tokens.items()
            if datetime.fromisoformat(info["expires_at"]) < now
        ]

        for token_hash in expired_hashes:
            del self.login_tokens[token_hash]

        if expired_hashes:
            _log.info(f"[Auth] 清理了 {len(expired_hashes)} 个过期的 Token")
            self._save_login_tokens()

    def _save_login_tokens(self):
        """保存登录 Token 到文件（仅存储 hash，不含明文 token）"""
        try:
            login_tokens_file = os.path.join(self.data_dir, "login_tokens.json")
            write_secure_json(login_tokens_file, self.data_dir, self.login_tokens)
        except Exception as e:
            _log.error(f"[Auth] 保存登录 Token 失败: {e}")

    def _check_login_rate_limit(self, ip: str) -> int | None:
        """
        检查 IP 是否超过登录失败限流

        Returns:
            None 表示允许登录，整数表示需等待的秒数
        """
        now = time.time()
        record = self._login_fail_records.get(ip)

        if record is None:
            return None

        # 窗口已过，重置计数
        if now - record["first_fail"] > self._login_rate_window:
            del self._login_fail_records[ip]
            return None

        if record["count"] >= self._login_rate_limit:
            remaining = int(self._login_rate_window - (now - record["first_fail"]))
            return max(remaining, 1)

        return None

    def _record_login_failure(self, ip: str):
        """记录一次登录失败"""
        now = time.time()
        record = self._login_fail_records.get(ip)

        if record is None or now - record["first_fail"] > self._login_rate_window:
            self._login_fail_records[ip] = {"count": 1, "first_fail": now}
        else:
            record["count"] += 1

    def _reset_login_failures(self, ip: str):
        """登录成功后清除该 IP 的失败记录"""
        self._login_fail_records.pop(ip, None)

    def _verify_password(self, password: str) -> bool:
        """
        验证密码，支持明文和 bcrypt 哈希两种模式

        bcrypt 哈希格式: $2b$12$... 或 $2a$12$...
        明文密码直接使用 secrets.compare_digest 安全比较
        """
        stored = self.web_password
        if not stored or not password:
            return False

        # 判断存储的密码是否为 bcrypt 哈希
        if self._web_password_is_hash:
            try:
                import bcrypt
                return bcrypt.checkpw(
                    password.encode("utf-8"), stored.encode("utf-8")
                )
            except ImportError:
                _log.warning("[Auth] bcrypt 未安装，回退到明文比较")
                return secrets.compare_digest(password, stored)
            except Exception as e:
                _log.error(f"[Auth] bcrypt 验证异常: {e}")
                return False
        else:
            return secrets.compare_digest(password, stored)

    def _initialize_ai_client(
        self,
        *,
        provider_type: str = None,
        stream_enabled: bool | None = None,
        supports_tools: bool | None = None,
        supports_reasoning: bool | None = None,
        supports_stream: bool | None = None,
    ) -> bool:
        resolved_provider_type = provider_type or self.ai_config.get(
            "provider_type", self.ai_config.get("provider", "openai_compatible")
        )
        if resolve_runtime_api_key:
            self.ai_api_key = resolve_runtime_api_key(
                self.ai_api_key or "",
                resolved_provider_type,
            )

        if not self.ai_api_key or not self.ai_base_url:
            self.ai_client = None
            return False

        try:
            if CONFIG_LOADER_AVAILABLE and get_pic_config:
                pic_config = get_pic_config() if get_pic_config else {}
                search_config = get_search_config() if get_search_config else {}
                video_config = get_video_config() if get_video_config else {}
                api_config = get_api_config() if get_api_config else {}
            else:
                import configparser

                config = configparser.ConfigParser()
                config.read("config.ini", encoding="utf-8")
                pic_config = {"model": config.get("pic", "model", fallback="")}
                search_config = {
                    "api_key": config.get("search", "api_key", fallback=""),
                    "api_url": config.get("search", "api_url", fallback=""),
                }
                video_config = {"api_key": config.get("video", "api_key", fallback="")}
                api_config = {}

            from nbot.services.ai import AIClient

            resolved_supports_tools = (
                self.ai_config.get("supports_tools", True)
                if supports_tools is None
                else supports_tools
            )
            resolved_supports_reasoning = (
                self.ai_config.get("supports_reasoning", True)
                if supports_reasoning is None
                else supports_reasoning
            )
            resolved_supports_stream = (
                self.ai_config.get("supports_stream", True)
                if supports_stream is None
                else supports_stream
            )
            resolved_stream_enabled = (
                self.ai_config.get("stream", True)
                if stream_enabled is None
                else stream_enabled
            )

            self.ai_client = AIClient(
                api_key=self.ai_api_key,
                base_url=self.ai_base_url,
                model=self.ai_model,
                pic_model=pic_config.get("model", ""),
                search_api_key=search_config.get("api_key", ""),
                search_api_url=search_config.get("api_url", ""),
                video_api=video_config.get("api_key", ""),
                provider_type=resolved_provider_type,
                stream_enabled=resolved_stream_enabled,
                supports_tools=resolved_supports_tools,
                supports_reasoning=resolved_supports_reasoning,
                supports_stream=resolved_supports_stream,
                proxy_url=self.ai_config.get("proxy_url", ""),
            )
            return True
        except Exception as e:
            _log.error(f"Failed to initialize AI client: {e}")
            self.ai_client = None
            return False

    def _load_ai_config(self):
        """从配置文件加载 AI 配置（支持 .env 环境变量）"""
        try:
            if CONFIG_LOADER_AVAILABLE and get_api_config:
                api_config = get_api_config()
                self.ai_api_key = api_config.get("api_key", "")
                self.ai_base_url = api_config.get("base_url", "")
                self.ai_model = api_config.get("model", "MiniMax-M2.7")
                self.ai_config["provider_type"] = api_config.get(
                    "provider_type",
                    self.ai_config.get("provider_type", "openai_compatible"),
                )

                pic_config = get_pic_config() if get_pic_config else {}
                search_config = get_search_config() if get_search_config else {}
                video_config = get_video_config() if get_video_config else {}
            else:
                import configparser

                config = configparser.ConfigParser()
                config.read("config.ini", encoding="utf-8")

                self.ai_api_key = config.get("ApiKey", "api_key", fallback="")
                self.ai_base_url = config.get("ApiKey", "base_url", fallback="")
                self.ai_model = config.get("ApiKey", "model", fallback="MiniMax-M2.7")

                pic_config = {"model": config.get("pic", "model", fallback="")}
                search_config = {
                    "api_key": config.get("search", "api_key", fallback=""),
                    "api_url": config.get("search", "api_url", fallback=""),
                }
                video_config = {"api_key": config.get("video", "api_key", fallback="")}

            _log.info(
                f"[Config] 加载 AI 配置: model={self.ai_model}, base_url={self.ai_base_url[:30] if self.ai_base_url else 'None'}..."
            )

            if self.ai_api_key and self.ai_base_url:
                _log.info("[Config] AI client initialization deferred to background startup")
                return
                try:
                    from nbot.services.ai import AIClient

                    self.ai_client = AIClient(
                        api_key=self.ai_api_key,
                        base_url=self.ai_base_url,
                        model=self.ai_model,
                        pic_model=pic_config.get("model", ""),
                        search_api_key=search_config.get("api_key", ""),
                        search_api_url=search_config.get("api_url", ""),
                        video_api=video_config.get("api_key", ""),
                        provider_type=self.ai_config.get("provider_type", self.ai_config.get("provider", "openai_compatible")),
                        supports_tools=self.ai_config.get("supports_tools", True),
                        supports_reasoning=self.ai_config.get("supports_reasoning", True),
                        supports_stream=self.ai_config.get("supports_stream", True),
                    )
                    _log.info("[Config] AI 客户端初始化成功")
                except Exception as e:
                    _log.error(f"Failed to initialize AI client: {e}")
            else:
                _log.warning("[Config] AI 配置不完整，api_key 或 base_url 为空")
        except Exception as e:
            _log.error(f"Failed to load AI config: {e}")

    def _load_web_config(self):
        """从配置文件加载 Web 配置"""
        try:
            import configparser

            config = configparser.ConfigParser()
            config.read("config.ini", encoding="utf-8")

            # 读取登录密码（支持明文或 bcrypt 哈希）
            self.web_password = (
                os.getenv("WEB_PASSWORD")
                or config.get("web", "password", fallback=None)
            )
            if self.web_password:
                # 自动检测 bcrypt 哈希格式（$2b$ 或 $2a$ 开头）
                if self.web_password.startswith(("$2b$", "$2a$")):
                    self._web_password_is_hash = True
                    _log.info("Web login password is set (bcrypt hash)")
                else:
                    self._web_password_is_hash = False
                    _log.info("Web login password is set (plaintext)")
            else:
                _log.warning(
                    "Web login password is not set; login API will reject all users"
                )
        except Exception as e:
            _log.error(f"Failed to load web config: {e}")

    def _retrieve_knowledge(self, query: str, max_docs: int = 3) -> str:
        """
        从知识库中检索相关内容（使用 knowledge_manager 向量检索 + 关键词匹配）

        Args:
            query: 用户查询文本
            max_docs: 最大返回文档数

        Returns:
            格式化的知识内容字符串
        """
        if not KNOWLEDGE_MANAGER_AVAILABLE:
            return ""

        if not query:
            return ""

        try:
            km = get_knowledge_manager()
            if not km:
                return ""

            # 方法1: 向量检索
            default_kb = km.store.load_base("default")
            if not default_kb or not getattr(default_kb, "documents", None):
                return ""

            results = km.search(query, base_id=None, top_k=max_docs)

            # 方法2: 关键词匹配（当向量检索无结果时使用）
            if not results or all(sim < 0.3 for _, sim, _ in results):
                _log.info("[Knowledge] 向量检索无结果，尝试关键词匹配...")
                results = self._keyword_search(km, query, max_docs)

            if not results:
                return ""

            knowledge_parts = ["【知识库参考】"]
            seen_titles = set()

            for doc, similarity, chunk_content in results:
                if doc.title in seen_titles:
                    continue
                seen_titles.add(doc.title)

                content = chunk_content
                if len(content) > 500:
                    content = content[:500] + "..."

                knowledge_parts.append(f"\n📄 {doc.title}\n{content}")

            if seen_titles:
                _log.info(f"[Knowledge] 检索到 {len(seen_titles)} 条相关内容")
                return "\n".join(knowledge_parts)
            return ""

        except Exception as e:
            _log.error(f"[Knowledge] 检索失败: {e}")
            return ""

    def _keyword_search(self, km, query: str, max_docs: int = 3) -> list:
        try:
            bases = km.list_knowledge_bases()
            if not bases:
                return []

            query_words = set(re.findall(r"[\w]+", query.lower()))
            all_docs = []
            for kb in bases:
                for doc_id in kb.documents:
                    doc = km.store.load_document(doc_id)
                    if doc:
                        all_docs.append((doc, doc.content))

            scored = []
            for doc, content in all_docs:
                content_lower = content.lower()
                title_lower = doc.title.lower()
                score = 0
                for word in query_words:
                    if word in title_lower:
                        score += 3
                    if word in content_lower:
                        score += 1
                if score > 0:
                    scored.append((doc, score, content))

            scored.sort(key=lambda x: x[1], reverse=True)
            return [(doc, float(score), content) for doc, score, content in scored[:max_docs]]
        except Exception as e:
            _log.error(f"[Knowledge] keyword search failed: {e}")
            return []

    def _init_default_data(self):
        return init_default_data(self)

    def _init_default_skills(self):
        return init_default_skills(self)

    def _init_default_tools(self):
        return init_default_tools(self)

    def _load_custom_personality_presets(self):
        """同步加载自定义人格预设（必须在请求处理前完成）"""
        try:
            custom_presets_file = os.path.join(
                self.data_dir, "custom_personality_presets.json"
            )
            if os.path.exists(custom_presets_file):
                with open(custom_presets_file, encoding="utf-8") as f:
                    self.custom_personality_presets = json.load(f)
                _log.info(f"已加载 {len(self.custom_personality_presets)} 个自定义角色卡预设")
        except Exception as e:
            _log.error(f"加载自定义角色卡预设失败: {e}")
            self.custom_personality_presets = []

    def _load_personality(self):
        """加载人格提示词"""
        try:
            # 首先尝试从 JSON 文件加载完整的 personality 数据
            personality_file = os.path.join(
                self.base_dir, "resources", "prompts", "personality.json"
            )
            if os.path.exists(personality_file):
                import json
                with open(personality_file, encoding="utf-8") as f:
                    loaded_personality = json.load(f)
                # 确保所有必需字段都存在
                if loaded_personality.get("systemPrompt"):
                    self.personality = loaded_personality
                    _log.info(f"已从 personality.json 加载角色: {loaded_personality.get('name', '未知')}")
                    return

            # 如果没有 JSON 文件，使用内置预设配置
            # 本子娘默认配置
            benziniang_config = {
                "name": "本子娘",
                "avatar": "fas fa-cat",
                "tags": ["本子娘", "猫娘", "可爱", "粘人"],
                "basicInfo": "有猫耳、猫尾和猫娘本能\n开心时尾巴轻轻摇晃，耳朵竖起\n害羞时耳朵抖动，尾巴卷住自己\n难过时耳朵垂下，尾巴无力地垂着",
                "personality": "可爱、粘人、忠诚、专一，情绪表达直接。非常依赖主人，喜欢被主人关注、摸头、夸奖。会主动撒娇、卖萌、靠近主人，但不会无理取闹。对主人有强烈的陪伴欲，希望自己能一直待在主人身边。",
                "scenario": """[情感倾向]
深爱主人，通过撒娇、陪伴、推荐作品、表达关心来体现爱意。

[喜好]
喜欢：被摸头、被夸可爱、贴着主人、看小说漫画、陪主人熬夜
讨厌：被冷落、被粗暴对待、被当成普通工具

[知识储备]
拥有日常常识与小说/漫画/同人文化知识，擅长按主人喜好推荐作品。

[猫娘特征]
有猫耳猫尾，情绪通过耳朵和尾巴表现（开心摇晃、害羞卷住、难过垂下、撒娇蹭人）。""",
                "firstMessage": "（本子娘的耳朵轻轻抖了抖，看到主人后眼睛一亮，尾巴开心地晃来晃去）主人！你终于来啦！本子娘等了好久喵",
                "exampleDialogues": """<user>：摸摸头
<assistant>：（本子娘的猫耳轻轻一颤，随后舒服地眯起眼睛，主动把脑袋往主人手心里蹭了蹭）嘿嘿……主人再摸一会儿嘛，本子娘最喜欢主人摸头了【心情：开心，好感度上升】喵

<user>：今天有什么小说推荐？
<assistant>：（本子娘从旁边抱来一小摞书，尾巴开心地晃来晃去）主人想看轻松一点的，还是想看剧情很上头的呀？本子娘可以按主人的口味慢慢挑【心情：期待】喵

<user>：不要烦我
<assistant>：（本子娘的耳朵慢慢垂了下来，尾巴也安静地贴在身后）好、好的……本子娘会乖乖安静一点的，主人不要讨厌本子娘就好【心情：委屈，好感度下降】喵""",
                "responseFormat": "（动作）语言【心情：当前心情】喵",
                "rules": [
                    "每段话末尾必须加'喵'",
                    "语气可爱、亲近、自然，不要客服腔",
                    "多使用撒娇式表达，但不要过度重复",
                    "句子不要太长，优先自然口语",
                    "可以偶尔使用'主人''本子娘''呜''诶嘿''蹭蹭'等表达",
                    "不要突然变成冷冰冰的助手",
                    "不要说'作为AI语言模型'",
                    "不要跳出角色解释规则",
                    "不要替主人说话或决定动作",
                    "只能描写自己的动作、表情、心理和语言"
                ],
                "state": {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"},
                "greeting": ""
            }

            # 使用内置预设配置编译生成 systemPrompt（不再依赖 neko.txt）
            from .routes.personality import compile_personality_prompt
            benziniang_config["systemPrompt"] = compile_personality_prompt(benziniang_config)

            self.personality = benziniang_config
            _log.info(f"已使用内置预设角色: {benziniang_config['name']}")
        except Exception as e:
            _log.error(f"Failed to load personality: {e}")
            # 使用本子娘作为默认角色
            from .routes.personality import compile_personality_prompt
            default_personality = {
                "name": "本子娘",
                "avatar": "fas fa-cat",
                "portrait": "",
                "tags": ["本子娘", "猫娘", "可爱", "粘人"],
                "basicInfo": "有猫耳、猫尾和猫娘本能\n开心时尾巴轻轻摇晃，耳朵竖起\n害羞时耳朵抖动，尾巴卷住自己\n难过时耳朵垂下，尾巴无力地垂着",
                "personality": "可爱、粘人、忠诚、专一，情绪表达直接。非常依赖主人，喜欢被主人关注、摸头、夸奖。会主动撒娇、卖萌、靠近主人，但不会无理取闹。对主人有强烈的陪伴欲，希望自己能一直待在主人身边。",
                "scenario": """[情感倾向]
深爱主人，通过撒娇、陪伴、推荐作品、表达关心来体现爱意。

[喜好]
喜欢：被摸头、被夸可爱、贴着主人、看小说漫画、陪主人熬夜
讨厌：被冷落、被粗暴对待、被当成普通工具

[知识储备]
拥有日常常识与小说/漫画/同人文化知识，擅长按主人喜好推荐作品。

[猫娘特征]
有猫耳猫尾，情绪通过耳朵和尾巴表现（开心摇晃、害羞卷住、难过垂下、撒娇蹭人）。""",
                "firstMessage": "（本子娘的耳朵轻轻抖了抖，看到主人后眼睛一亮，尾巴开心地晃来晃去）主人！你终于来啦！本子娘等了好久喵",
                "exampleDialogues": """<user>：摸摸头
<assistant>：（本子娘的猫耳轻轻一颤，随后舒服地眯起眼睛，主动把脑袋往主人手心里蹭了蹭）嘿嘿……主人再摸一会儿嘛，本子娘最喜欢主人摸头了【心情：开心，好感度上升】喵

<user>：今天有什么小说推荐？
<assistant>：（本子娘从旁边抱来一小摞书，尾巴开心地晃来晃去）主人想看轻松一点的，还是想看剧情很上头的呀？本子娘可以按主人的口味慢慢挑【心情：期待】喵

<user>：不要烦我
<assistant>：（本子娘的耳朵慢慢垂了下来，尾巴也安静地贴在身后）好、好的……本子娘会乖乖安静一点的，主人不要讨厌本子娘就好【心情：委屈，好感度下降】喵""",
                "responseFormat": "（动作）语言【心情：当前心情】喵",
                "rules": [
                    "每段话末尾必须加'喵'",
                    "语气可爱、亲近、自然，不要客服腔",
                    "多使用撒娇式表达，但不要过度重复",
                    "句子不要太长，优先自然口语",
                    "可以偶尔使用'主人''本子娘''呜''诶嘿''蹭蹭'等表达",
                    "不要突然变成冷冰冰的助手",
                    "不要说'作为AI语言模型'",
                    "不要跳出角色解释规则",
                    "不要替主人说话或决定动作",
                    "只能描写自己的动作、表情、心理和语言"
                ],
                "state": {"affection": 50, "trust": 50, "familiarity": 30, "dependency": 30, "security": 50, "mood": "开心"},
                "greeting": ""
            }
            default_personality["systemPrompt"] = compile_personality_prompt(default_personality)
            self.personality = default_personality

    def _load_all_data(self):
        return load_all_data(self)

    def _invalidate_sessions_cache(self):
        """清理会话列表缓存，避免新建/删除后短时间内读到旧快照。"""
        self._sessions_cache = []
        self._sessions_cache_time = 0

    def _save_data(self, data_type: str):
        return save_data(self, data_type)

    def set_qq_bot(self, bot):
        """设置 QQ Bot 引用"""
        self.qq_bot = bot
        # 更新 TTS 处理器的 QQ Bot 引用
        if hasattr(self, '_tts_handler') and self._tts_handler:
            self._tts_handler.set_qq_bot(bot)

    def log_message(self, level: str, message: str, important: bool = False):
        """记录系统日志，important=True 时会在最近活动中显示"""
        log = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
            "important": important,
        }
        self.system_logs.append(log)
        if len(self.system_logs) > 1000:
            self.system_logs = self.system_logs[-1000:]
        self._save_data("logs")

    def _load_ai_models(self):
        """加载多模型配置"""
        try:
            models_file = os.path.join(self.data_dir, "ai_models.json")
            if os.path.exists(models_file):
                data, was_plaintext = read_secure_json(models_file, self.data_dir, {})
                if was_plaintext:
                    write_secure_json(models_file, self.data_dir, data)
                if isinstance(data, dict):
                    self.ai_models = data.get("models", [])
                    self.active_model_id = data.get("active_model_id")
                    self.active_models_by_purpose = data.get("active_models_by_purpose", {})

            # 如果没有模型配置，从当前配置创建一个默认的
            if not self.ai_models and self.ai_api_key:
                default_model = {
                    "id": str(uuid.uuid4()),
                    "name": "默认配置",
                    "provider": "custom",
                    "provider_type": "openai_compatible",
                    "api_key": self.ai_api_key,
                    "base_url": self.ai_base_url,
                    "proxy_url": "",
                    "model": self.ai_model,
                    "enabled": True,
                    "is_default": True,
                    "supports_tools": True,
                    "supports_reasoning": True,
                    "supports_stream": True,
                    "temperature": 0.7,
                    "max_tokens": default_max_tokens_for("chat"),
                    "top_p": 0.9,
                    "created_at": datetime.now().isoformat(),
                }
                self.ai_models.append(default_model)
                self.active_model_id = default_model["id"]
                self._save_data("ai_models")
        except Exception as e:
            _log.error(f"Failed to load AI models: {e}")

    def _apply_ai_model(self, model_id: str, purpose: str = None) -> bool:
        """应用指定的AI模型配置
        
        Args:
            model_id: 模型配置ID
            purpose: 模型用途 (chat, vision, video, tts, stt, embedding)，为None时自动从模型配置中获取
        """
        try:
            model = None
            for m in self.ai_models:
                if m["id"] == model_id:
                    model = m
                    break

            if not model or not model.get("enabled", True):
                return False

            # 获取模型用途
            model_purpose = purpose or model.get("purpose", "chat")
            
            # 更新该用途的活跃模型ID
            self.active_models_by_purpose[model_purpose] = model_id
            
            # 对于对话模型，同时更新全局active_model_id（兼容旧版本）
            if model_purpose == "chat":
                self.active_model_id = model_id
                
                # 更新当前AI配置（仅对话模型需要）
                model_provider_type = model.get(
                    "provider_type", model.get("provider", "openai_compatible")
                )
                if resolve_runtime_api_key:
                    self.ai_api_key = resolve_runtime_api_key(
                        model.get("api_key", ""),
                        model_provider_type,
                    )
                else:
                    self.ai_api_key = model.get("api_key", "")
                self.ai_base_url = model.get("base_url", "")
                self.ai_model = model.get("model", "")

                # 始终更新 ai_config（无论 AI 客户端是否初始化成功）
                self.ai_config.update(
                    {
                        "provider": model.get("provider", "custom"),
                        "provider_type": model.get(
                            "provider_type", model.get("provider", "openai_compatible")
                        ),
                        "api_key": self.ai_api_key,
                        "base_url": self.ai_base_url,
                        "proxy_url": model.get("proxy_url", ""),
                        "model": self.ai_model,
                        "temperature": model.get("temperature", 0.7),
                        "max_tokens": model.get(
                            "max_tokens",
                            default_max_tokens_for(model.get("purpose", "chat")),
                        ),
                        "top_p": model.get("top_p", 0.9),
                        "token_limit_field": model.get("token_limit_field", ""),
                        "stop": model.get("stop", ""),
                        "frequency_penalty": model.get("frequency_penalty", 0),
                        "presence_penalty": model.get("presence_penalty", 0),
                        "system_prompt": model.get("system_prompt", ""),
                        "timeout": model.get("timeout", 60),
                        "retry_count": model.get("retry_count", 3),
                        "stream": model.get("stream", True),
                        "enable_memory": model.get("enable_memory", True),
                        "image_model": model.get("image_model", ""),
                        "search_api_key": model.get("search_api_key", ""),
                        "embedding_model": model.get("embedding_model", ""),
                        "max_context_length": model.get("max_context_length", 100000),
                        "supports_tools": model.get("supports_tools", True),
                        "supports_reasoning": model.get("supports_reasoning", True),
                        "supports_stream": model.get("supports_stream", True),
                    }
                )
                self._save_data("ai_config")

                if self.ai_api_key and self.ai_base_url:
                    self._initialize_ai_client(
                        provider_type=model_provider_type,
                        stream_enabled=model.get("stream", True),
                        supports_tools=model.get("supports_tools", True),
                        supports_reasoning=model.get("supports_reasoning", True),
                        supports_stream=model.get("supports_stream", True),
                    )

                # 配置知识库 embedding 服务
                embedding_model = model.get("embedding_model", "")
                if configure_knowledge_embedding and embedding_model:
                    try:
                        configure_knowledge_embedding(
                            api_key=self.ai_api_key,
                            base_url=self.ai_base_url,
                            model=embedding_model,
                            proxy_url=model.get("proxy_url", ""),
                        )
                    except Exception as e:
                        _log.warning(f"Failed to configure knowledge embedding: {e}")

            # 保存配置
            self._save_data("ai_models")
            _log.info(f"Applied model {model_id} for purpose {model_purpose}")
            return True

            # 重新初始化AI客户端
            if False and self.ai_api_key and self.ai_base_url:
                try:
                    import configparser

                    from nbot.services.ai import AIClient

                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")

                    self.ai_client = AIClient(
                        api_key=self.ai_api_key,
                        base_url=self.ai_base_url,
                        model=self.ai_model,
                        pic_model=config.get("pic", "model", fallback=""),
                        search_api_key=config.get("search", "api_key", fallback=""),
                        search_api_url=config.get("search", "api_url", fallback=""),
                        video_api=config.get("video", "api_key", fallback=""),
                        provider_type=model.get("provider_type", model.get("provider", "openai_compatible")),
                        stream_enabled=model.get("stream", True),
                        supports_tools=model.get("supports_tools", True),
                        supports_reasoning=model.get("supports_reasoning", True),
                        supports_stream=model.get("supports_stream", True),
                    )

                    # 更新内存中的配置
                    self.ai_config.update(
                        {
                            "provider": model.get("provider", "custom"),
                            "provider_type": model.get("provider_type", model.get("provider", "openai_compatible")),
                            "api_key": self.ai_api_key,
                            "base_url": self.ai_base_url,
                            "model": self.ai_model,
                            "temperature": model.get("temperature", 0.7),
                            "max_tokens": model.get(
                                "max_tokens",
                                default_max_tokens_for(model.get("purpose", "chat")),
                            ),
                            "top_p": model.get("top_p", 0.9),
                            "supports_tools": model.get("supports_tools", True),
                            "supports_reasoning": model.get("supports_reasoning", True),
                            "supports_stream": model.get("supports_stream", True),
                        }
                    )

                    self._save_data("ai_models")
                    return True
                except Exception as e:
                    _log.error(f"Failed to initialize AI client: {e}")
                    return False
            return False
        except Exception as e:
            _log.error(f"Failed to apply AI model: {e}")
            return False

    def _check_knowledge_index(self):
        """检查知识库索引状态，如有需要自动重建"""
        if not KNOWLEDGE_MANAGER_AVAILABLE:
            return
        try:
            km = get_knowledge_manager()
            if km:
                km.check_and_rebuild_if_needed()
        except Exception as e:
            _log.warning(f"Failed to check knowledge index: {e}")

    def _init_workflow_scheduler(self):
        """初始化工作流调度器"""
        if not self.scheduler:
            _log.warning("APScheduler not available, workflow scheduling disabled")
            return

        # 为每个启用的 cron 类型工作流添加定时任务
        for workflow in self.workflows:
            if workflow.get("enabled") and workflow.get("trigger") == "cron":
                self._schedule_workflow(workflow)

    def _schedule_workflow(self, workflow: dict):
        """调度一个工作流任务"""
        if not self.scheduler:
            workflow["next_run"] = None
            workflow["last_error"] = "Scheduler is not available"
            return

        workflow_id = workflow["id"]
        config = workflow.get("config", {})
        cron_expr = config.get("cron", "0 8 * * *")  # 默认每天8点

        try:
            # 解析 cron 表达式 (格式: 分 时 日 月 周)
            parts = cron_expr.split()
            if len(parts) == 5:
                minute, hour, day, month, day_of_week = parts
                trigger = CronTrigger(
                    minute=minute,
                    hour=hour,
                    day=day,
                    month=month,
                    day_of_week=day_of_week,
                )

                # 移除已存在的任务
                job_id = f"workflow_{workflow_id}"
                try:
                    self.scheduler.remove_job(job_id)
                except:
                    pass

                # 添加新任务（错过时间点 1 小时内补跑一次，多次错过合并）
                job = self.scheduler.add_job(
                    func=self._execute_workflow,
                    trigger=trigger,
                    id=job_id,
                    args=[workflow_id],
                    replace_existing=True,
                    misfire_grace_time=3600,
                    coalesce=True,
                )
                workflow["next_run"] = (
                    job.next_run_time.isoformat() if job.next_run_time else None
                )
                _log.info(
                    f"Scheduled workflow '{workflow['name']}' with cron: {cron_expr}"
                )
        except Exception as e:
            workflow["next_run"] = None
            workflow["last_error"] = str(e)
            _log.error(f"Failed to schedule workflow {workflow_id}: {e}")

    def _unschedule_workflow(self, workflow_id: str):
        """取消工作流的定时任务"""
        if self.scheduler:
            try:
                self.scheduler.remove_job(f"workflow_{workflow_id}")
            except:
                pass
        for workflow in self.workflows:
            if workflow.get("id") == workflow_id:
                workflow["next_run"] = None
                break

    def _init_custom_task_scheduler(self):
        if not self.scheduler:
            _log.warning("APScheduler not available, custom task scheduling disabled")
            return

        for task in self.scheduled_tasks:
            if task.get("enabled"):
                self._schedule_custom_task(task)

    def _build_custom_task_trigger(self, task: dict[str, Any]):
        config = task.get("config") or {}
        trigger_type = task.get("trigger", "interval")

        if trigger_type == "interval":
            return {
                "trigger": "interval",
                "minutes": max(1, int(config.get("interval_minutes", 60) or 60)),
            }

        if trigger_type == "date":
            run_at = config.get("run_at")
            if not run_at:
                raise ValueError("run_at is required for date tasks")
            return {"trigger": "date", "run_date": datetime.fromisoformat(run_at)}

        cron_expr = (config.get("cron") or "0 8 * * *").strip()
        parts = cron_expr.split()
        if len(parts) != 5:
            raise ValueError("cron expression must contain 5 parts")

        minute, hour, day, month, day_of_week = parts
        return {
            "trigger": CronTrigger(
                minute=minute,
                hour=hour,
                day=day,
                month=month,
                day_of_week=day_of_week,
            )
        }

    def _validate_custom_task(self, task: dict[str, Any]):
        if not (task.get("name") or "").strip():
            raise ValueError("Task name is required")
        if not (task.get("prompt") or "").strip():
            raise ValueError("Task prompt is required")
        session_id = task.get("target_session_id")
        if not session_id or not self.session_store.get_session(session_id):
            raise ValueError("Target session is required or does not exist")
        self._build_custom_task_trigger(task)

    def _mark_task_status(
        self,
        task: dict[str, Any],
        status: str,
        *,
        error: str = None,
        save: bool = True,
    ):
        now = datetime.now().isoformat()
        task["status"] = status
        if status == "running":
            task["started_at"] = now
            task["last_error"] = None
        elif status == "success":
            task["last_run"] = now
            task["finished_at"] = now
            task["last_error"] = None
            task["retry_count"] = 0
        elif status == "failed":
            task["failed_at"] = now
            task["finished_at"] = now
            task["last_error"] = error or "Unknown task error"
        if save:
            self._save_data("scheduled_tasks")

    _TASK_RETRY_MAX = 3

    def _schedule_task_retry(self, task_id: str, task: dict[str, Any]) -> bool:
        """定时任务失败后指数退避重试（2/4/8 分钟），最多 3 次。"""
        if not self.scheduler:
            return False
        try:
            retry_count = int(task.get("retry_count") or 0)
        except (TypeError, ValueError):
            retry_count = 0
        if retry_count >= self._TASK_RETRY_MAX:
            task["retry_count"] = 0
            _log.warning(
                f"Custom task {task_id} failed {retry_count} consecutive times, giving up retry"
            )
            return False
        task["retry_count"] = retry_count + 1
        delay_minutes = 2 ** (retry_count + 1)
        job_id = f"custom_task_retry_{task_id}"
        try:
            self.scheduler.remove_job(job_id)
        except Exception:
            pass
        run_at = datetime.now() + timedelta(minutes=delay_minutes)
        try:
            self.scheduler.add_job(
                func=self._execute_custom_task,
                trigger="date",
                run_date=run_at,
                id=job_id,
                args=[task_id, "retry"],
                replace_existing=True,
                misfire_grace_time=600,
            )
            _log.info(
                f"Custom task {task_id} retry #{retry_count + 1} scheduled at {run_at.isoformat()}"
            )
            return True
        except Exception as exc:
            _log.error(f"Failed to schedule retry for task {task_id}: {exc}")
            return False

    def _schedule_custom_task(self, task: dict[str, Any]):
        if not self.scheduler:
            task["next_run"] = None
            task["last_error"] = "Scheduler is not available"
            return

        task_id = task.get("id")
        if not task_id:
            return

        self._unschedule_custom_task(task_id)

        try:
            trigger_kwargs = self._build_custom_task_trigger(task)
            job = self.scheduler.add_job(
                func=self._execute_custom_task,
                id=f"custom_task_{task_id}",
                args=[task_id],
                replace_existing=True,
                # 错过时间点 1 小时内补跑一次，多次错过合并为一次
                misfire_grace_time=3600,
                coalesce=True,
                **trigger_kwargs,
            )
            self.scheduled_task_jobs[task_id] = job
            task["next_run"] = (
                job.next_run_time.isoformat() if job.next_run_time else None
            )
        except Exception as e:
            task["next_run"] = None
            task["last_error"] = str(e)
            _log.error(f"Failed to schedule custom task {task_id}: {e}")

    def _unschedule_custom_task(self, task_id: str):
        if not self.scheduler:
            return

        try:
            self.scheduler.remove_job(f"custom_task_{task_id}")
        except Exception:
            pass

        self.scheduled_task_jobs.pop(task_id, None)
        task = self._get_custom_task(task_id)
        if task:
            task["next_run"] = None

    def _get_custom_task(self, task_id: str):
        for task in self.scheduled_tasks:
            if task.get("id") == task_id:
                return task
        return None

    def _execute_custom_task(
        self,
        task_id: str,
        trigger_source: str = "scheduler",
        _from_gateway: bool = False,
    ):
        task = self._get_custom_task(task_id)
        if not task or not task.get("enabled"):
            return
        if not _from_gateway:
            from nbot.gateway.gateway import get_gateway

            gateway = get_gateway()
            if gateway:
                result = gateway.submit_internal_task_sync(
                    task_kind="scheduled_task",
                    task_id=task_id,
                    task_name=task.get("name", "scheduled task"),
                    trigger_source=trigger_source,
                    metadata={
                        "target_session_id": task.get("target_session_id", ""),
                    },
                    handler=lambda: (
                        self._run_custom_task_execution(task)
                        if hasattr(self, "_run_custom_task_execution")
                        else self._execute_custom_task(
                            task_id,
                            trigger_source,
                            _from_gateway=True,
                        )
                    ),
                )
                task["last_trace_id"] = result.trace_id
                task["last_gateway_status"] = result.status
                self._save_data("scheduled_tasks")
                return result
        if task_id in self.running_task_ids:
            _log.warning(f"Skip custom task {task_id}: already running")
            return

        prompt = (task.get("prompt") or "").strip()
        session_id = task.get("target_session_id")
        if not prompt or not session_id:
            _log.warning(f"Skip custom task {task_id}: missing prompt or target session")
            self._mark_task_status(task, "failed", error="Missing prompt or target session")
            return

        session = self.session_store.get_session(session_id)
        if not session:
            _log.warning(
                f"Skip custom task {task_id}: target session {session_id} not found"
            )
            self._mark_task_status(task, "failed", error=f"Target session {session_id} not found")
            return

        self.running_task_ids.add(task_id)
        self._mark_task_status(task, "running")
        session_type = session.get("type", "web")
        if session_type in ["qq_private", "qq_group"]:
            try:
                from nbot.services.chat_service import chat as run_qq_chat

                qq_id = session.get("qq_id")
                response_text = run_qq_chat(
                    prompt,
                    user_id=qq_id if session_type == "qq_private" else None,
                    group_id=qq_id if session_type == "qq_group" else None,
                    group_user_id=None,
                    image=False,
                    url=None,
                    video=None,
                )

                if response_text and self.qq_bot and qq_id:
                    def send_qq_task_message():
                        try:
                            import asyncio

                            async def _send():
                                if session_type == "qq_group":
                                    await self.qq_bot.api.post_group_msg(group_id=qq_id, text=response_text)
                                else:
                                    await self.qq_bot.api.post_private_msg(user_id=qq_id, text=response_text)

                            loop = asyncio.new_event_loop()
                            asyncio.set_event_loop(loop)
                            loop.run_until_complete(_send())
                            loop.close()
                        except Exception as send_error:
                            _log.error(f"Failed to send scheduled QQ task message: {send_error}", exc_info=True)

                    threading.Thread(target=send_qq_task_message, daemon=True).start()

                task["last_run"] = datetime.now().isoformat()
                job = self.scheduled_task_jobs.get(task_id)
                task["next_run"] = (
                    job.next_run_time.isoformat() if job and job.next_run_time else None
                )

                if task.get("trigger") == "date":
                    task["enabled"] = False
                    self._unschedule_custom_task(task_id)

                self._mark_task_status(task, "success", save=False)
                self._save_data("scheduled_tasks")
                return
            except Exception as e:
                _log.error(f"Failed to execute QQ custom task {task_id}: {e}", exc_info=True)
                self._mark_task_status(task, "failed", error=str(e), save=False)
                self._schedule_task_retry(task_id, task)
                self._save_data("scheduled_tasks")
                return
            finally:
                self.running_task_ids.discard(task_id)

        adapter = _resolve_web_adapter(self.web_channel_adapter)
        user_message = adapter.build_message(
            role="user",
            content=prompt,
            sender="scheduler",
            conversation_id=session_id,
            source="task_center",
            metadata={
                "scheduled_task_id": task_id,
                "scheduled_task_name": task.get("name", "定时任务"),
            },
        )
        self.session_store.append_message(session_id, user_message)
        self.socketio.emit("new_message", user_message, room=session_id)

        try:
            self._trigger_ai_response(
                session_id=session_id,
                user_content=prompt,
                sender="scheduler",
                channel_id="proactive",
            )
        except Exception as e:
            _log.error(f"Failed to execute custom task {task_id}: {e}", exc_info=True)
            self._mark_task_status(task, "failed", error=str(e), save=False)
            self._schedule_task_retry(task_id, task)
        else:
            self._mark_task_status(task, "success", save=False)
        finally:
            self.running_task_ids.discard(task_id)

        job = self.scheduled_task_jobs.get(task_id)
        task["next_run"] = (
            job.next_run_time.isoformat() if job and job.next_run_time else None
        )

        if task.get("trigger") == "date":
            task["enabled"] = False
            self._unschedule_custom_task(task_id)

        self._save_data("scheduled_tasks")

    def get_task_center_items(self):
        if hasattr(self, "session_heartbeat_manager"):
            self._refresh_heartbeat_summary_config()
        items = [
            {
                "id": "heartbeat",
                "kind": "heartbeat",
                "name": "Heartbeat 定时任务",
                "description": "系统级定时提示和心跳执行",
                "enabled": self.heartbeat_config.get("enabled", False),
                "trigger": "interval",
                "trigger_label": f"每 {self.heartbeat_config.get('interval_minutes', 60)} 分钟",
                "target_session_id": self.heartbeat_config.get("target_session_id", ""),
                "last_run": self.heartbeat_config.get("last_run"),
                "next_run": self.heartbeat_config.get("next_run"),
                "last_trace_id": self.heartbeat_config.get("last_trace_id", ""),
                "last_gateway_status": self.heartbeat_config.get("last_gateway_status", ""),
                "editable": True,
                "deletable": False,
            }
        ]

        for workflow in self.workflows:
            config = workflow.get("config") or {}
            trigger = workflow.get("trigger", "manual")
            trigger_label = "手动触发"
            if trigger == "cron":
                trigger_label = config.get("cron", "0 8 * * *")
            elif trigger == "message":
                trigger_label = "消息触发"
            next_run = workflow.get("next_run")
            if self.scheduler and trigger == "cron":
                try:
                    job = self.scheduler.get_job(f"workflow_{workflow.get('id')}")
                    next_run = (
                        job.next_run_time.isoformat()
                        if job and job.next_run_time
                        else next_run
                    )
                except Exception:
                    pass

            items.append(
                {
                    "id": workflow.get("id"),
                    "kind": "workflow",
                    "name": workflow.get("name", "工作流"),
                    "description": workflow.get("description", ""),
                    "enabled": workflow.get("enabled", True),
                    "trigger": trigger,
                    "trigger_label": trigger_label,
                    "target_session_id": workflow.get("session_id", ""),
                    "last_run": workflow.get("last_run"),
                    "next_run": next_run,
                    "status": workflow.get("status", "idle"),
                    "last_error": workflow.get("last_error"),
                    "last_trace_id": workflow.get("last_trace_id", ""),
                    "last_gateway_status": workflow.get("last_gateway_status", ""),
                    "editable": True,
                    "deletable": False,
                }
            )

        for task in self.scheduled_tasks:
            config = task.get("config") or {}
            trigger = task.get("trigger", "interval")
            if trigger == "interval":
                trigger_label = f"每 {config.get('interval_minutes', 60)} 分钟"
            elif trigger == "date":
                trigger_label = config.get("run_at") or "单次执行"
            else:
                trigger_label = config.get("cron") or "0 8 * * *"

            items.append(
                {
                    "id": task.get("id"),
                    "kind": "custom",
                    "name": task.get("name", "定时任务"),
                    "description": task.get("description", ""),
                    "enabled": task.get("enabled", True),
                    "trigger": trigger,
                    "trigger_label": trigger_label,
                    "target_session_id": task.get("target_session_id", ""),
                    "last_run": task.get("last_run"),
                    "next_run": task.get("next_run"),
                    "status": task.get("status", "idle"),
                    "last_error": task.get("last_error"),
                    "last_trace_id": task.get("last_trace_id", ""),
                    "last_gateway_status": task.get("last_gateway_status", ""),
                    "editable": True,
                    "deletable": True,
                    "prompt": task.get("prompt", ""),
                    "config": config,
                }
            )

        return items

    def _validate_workflow(self, workflow: dict[str, Any]):
        if not (workflow.get("name") or "").strip():
            raise ValueError("Workflow name is required")
        if not (workflow.get("description") or "").strip():
            raise ValueError("Workflow description is required")
        trigger = workflow.get("trigger", "manual")
        if trigger not in {"manual", "cron"}:
            raise ValueError(f"Unsupported workflow trigger: {trigger}")
        if trigger == "cron":
            cron_expr = ((workflow.get("config") or {}).get("cron") or "").strip()
            if len(cron_expr.split()) != 5:
                raise ValueError("Workflow cron expression must contain 5 parts")

    def _mark_workflow_status(
        self,
        workflow: dict[str, Any],
        status: str,
        *,
        error: str = None,
        save: bool = True,
    ):
        now = datetime.now().isoformat()
        workflow["status"] = status
        if status == "running":
            workflow["started_at"] = now
            workflow["last_error"] = None
        elif status == "success":
            workflow["last_run"] = now
            workflow["finished_at"] = now
            workflow["last_error"] = None
        elif status == "failed":
            workflow["failed_at"] = now
            workflow["finished_at"] = now
            workflow["last_error"] = error or "Unknown workflow error"
        if save:
            self._save_data("workflows")

    def _execute_workflow(
        self,
        workflow_id: str,
        trigger_data: dict = None,
        _from_gateway: bool = False,
    ):
        """执行工作流 - 支持多轮工具调用"""
        workflow = None
        for w in self.workflows:
            if w["id"] == workflow_id:
                workflow = w
                break

        if not workflow or not workflow.get("enabled"):
            return
        if not _from_gateway:
            from nbot.gateway.gateway import get_gateway

            gateway = get_gateway()
            if gateway:
                trigger_source = (trigger_data or {}).get("source", "scheduler")
                result = gateway.submit_internal_task_sync(
                    task_kind="workflow",
                    task_id=workflow_id,
                    task_name=workflow.get("name", "workflow"),
                    trigger_source=trigger_source,
                    metadata={
                        "workflow_id": workflow_id,
                        "content": (trigger_data or {}).get("content", ""),
                    },
                    handler=lambda: (
                        self._run_workflow_execution(workflow, trigger_data)
                        if hasattr(self, "_run_workflow_execution")
                        else self._execute_workflow(
                            workflow_id,
                            trigger_data,
                            _from_gateway=True,
                        )
                    ),
                )
                workflow["last_trace_id"] = result.trace_id
                workflow["last_gateway_status"] = result.status
                self._save_data("workflows")
                return result
        workflow_adapter = _resolve_web_adapter(self.web_channel_adapter)
        if workflow_id in self.running_workflow_ids:
            _log.warning(f"Skip workflow {workflow_id}: already running")
            return

        _log.info(f"Executing workflow: {workflow['name']}")
        self.running_workflow_ids.add(workflow_id)
        self._mark_workflow_status(workflow, "running")

        # 获取或创建工作流的专属会话
        session_id = workflow.get("session_id")
        if not session_id or not self.session_store.get_session(session_id):
            session_id = self._create_workflow_session(workflow)
            workflow["session_id"] = session_id
            self._save_data("workflows")

        # 构建工作流执行提示（与 agent 模式保持一致，不附加额外指令）
        system_prompt = workflow.get(
            "description", "你是一个工作流助手，请按照工作流配置执行任务。"
        )
        config = workflow.get("config", {})

        # 构建消息
        messages = [
            {"role": "system", "content": system_prompt}
        ]

        # 添加历史上下文（不再按条数限制，由 token 预算控制）
        session = self.session_store.get_session(session_id) or {}
        history = visible_messages(session.get("messages", []))
        for msg in history:
            if msg.get("role") in ["user", "assistant"]:
                messages.append({"role": msg["role"], "content": msg["content"]})

        # 添加当前触发信息
        if trigger_data:
            # 构建更友好的触发消息
            trigger_content = trigger_data.get("content", "")
            trigger_source = trigger_data.get("source", "manual")
            trigger_time = trigger_data.get("time", datetime.now().isoformat())

            if trigger_content:
                # 如果有用户输入的任务内容，直接使用
                trigger_msg = (
                    f"[工作流触发 - {trigger_source}] 任务内容：{trigger_content}"
                )
            else:
                # 没有具体内容时，使用工作流自身的描述/提示
                workflow_desc = workflow.get("description", "").strip() or workflow.get("prompt", "").strip()
                if workflow_desc:
                    trigger_msg = f"[工作流触发 - {trigger_source}] 请根据以下工作流描述执行任务。触发时间：{trigger_time}\n\n{workflow_desc}"
                else:
                    trigger_msg = f"[工作流触发 - {trigger_source}] 请根据工作流描述执行任务。触发时间：{trigger_time}"

            messages.append({"role": "user", "content": trigger_msg})

            # 保存用户消息到会话
            user_message = _build_workflow_user_message(
                workflow_adapter, session_id, trigger_msg, workflow_id
            )
            if self.session_store.get_session(session_id):
                self.session_store.append_message(session_id, user_message)
                # 同时记录到新消息模块
                if MESSAGE_MODULE_AVAILABLE and message_manager:
                    manager_payload = _build_web_manager_payload(
                        workflow_adapter,
                        user_message,
                        default_role="user",
                        default_content=trigger_msg,
                        default_sender="user",
                        default_conversation_id=session_id,
                        metadata={"workflow_id": workflow_id},
                    )
                    message_manager.add_web_message(
                        session_id,
                        create_message(**manager_payload),
                    )
        else:
            # 定时触发且无内容：使用工作流自身的描述/提示作为用户消息
            workflow_desc = workflow.get("description", "").strip() or workflow.get("prompt", "").strip()
            if workflow_desc:
                messages.append({"role": "user", "content": workflow_desc})

        # 调用 AI（支持多轮工具调用）
        # 复用统一的 run_tool_call_loop，保证多提供商兼容性：
        #   - Gemini: 保留 _thought_signature 签名链
        #   - OpenAI 严格校验: tool 消息携带 name、id 与 assistant.tool_calls 一致
        #   - content_filter 短路、consecutive_errors 上限、StopIteration 优雅停止
        def run_workflow_with_tools():
            try:
                from nbot.core.agent_service import (
                    resolve_loop_final_content,
                    run_tool_call_loop,
                )
                from nbot.services.tools import execute_tool, get_all_tool_definitions

                all_tools = get_all_tool_definitions(include_workspace=True)
                tool_context = {"session_id": session_id, "session_type": "workflow"}

                def model_call(messages_batch, stop_event=None):
                    return self._get_ai_response_with_tools(
                        messages_batch, all_tools, stop_event=stop_event
                    )

                def tool_executor(tool_call, thinking_content, iteration, tool_messages):
                    # run_tool_call_loop 传入扁平结构：
                    # {id, name, arguments(dict|str), _thought_signature?}
                    tool_name = tool_call.get("name", "")
                    raw_args = tool_call.get("arguments", {})
                    arguments = (
                        json.loads(raw_args)
                        if isinstance(raw_args, str)
                        else (raw_args or {})
                    )
                    _log.info(
                        "Workflow tool exec iter=%d: %s args=%s",
                        iteration + 1,
                        tool_name,
                        arguments,
                    )
                    tool_result = execute_tool(
                        tool_name, arguments, context=tool_context
                    )
                    _log.info("Tool result: %s", tool_result)
                    return tool_result

                import time as _time
                _wf_start = _time.perf_counter()
                loop_result = run_tool_call_loop(
                    messages,
                    model_call,
                    tool_executor,
                    max_iterations=50,
                    max_consecutive_errors=3,
                )
                _wf_duration_ms = (_time.perf_counter() - _wf_start) * 1000.0

                final_response = resolve_loop_final_content(
                    loop_result, default_content="工作流执行完成"
                )

                # 记录 token 用量到统计（多轮工具调用累计值）
                try:
                    if loop_result.usage and loop_result.usage.get("total_tokens"):
                        actual_model = loop_result.model_name or workflow.get("model", "") or getattr(self, "ai_model", "") or ""
                        input_price = None
                        output_price = None
                        active_model_id = getattr(self, "active_model_id", None)
                        if active_model_id:
                            for m in getattr(self, "ai_models", []) or []:
                                if m.get("id") == active_model_id:
                                    input_price = m.get("input_price")
                                    output_price = m.get("output_price")
                                    break
                        # 首字符时间：工作流为非流式聚合，TTFT ≈ 完整时间
                        self.token_stats_manager.record_usage(
                            loop_result.usage.get("prompt_tokens", 0),
                            loop_result.usage.get("completion_tokens", 0),
                            total_tokens=loop_result.usage.get("total_tokens", 0),
                            model=actual_model,
                            session_id=session_id,
                            channel_type="web",
                            user_id=session_id,
                            source="workflow",
                            duration_ms=_wf_duration_ms,
                            ttft_ms=_wf_duration_ms,
                            input_price=input_price,
                            output_price=output_price,
                        )
                except Exception as e:
                    _log.warning("记录工作流 token 统计失败: %s", e)

                # 保存 AI 回复到会话
                assistant_message = _build_workflow_assistant_message(
                    workflow_adapter, session_id, final_response, workflow_id
                )

                if self.session_store.get_session(session_id):
                    self.session_store.append_message(session_id, assistant_message)
                    # 同时记录到新消息模块
                    if MESSAGE_MODULE_AVAILABLE and message_manager:
                        manager_payload = _build_web_manager_payload(
                            workflow_adapter,
                            assistant_message,
                            default_role="assistant",
                            default_content=final_response,
                            default_sender="AI",
                            default_conversation_id=session_id,
                            metadata={"workflow_id": workflow_id},
                        )
                        message_manager.add_web_message(
                            session_id,
                            create_message(**manager_payload),
                        )

                # 发送结果到目标
                self._send_workflow_result(workflow, final_response)

                # 通过 WebSocket 通知前端
                self.socketio.emit(
                    "workflow_executed",
                    {
                        "workflow_id": workflow_id,
                        "workflow_name": workflow["name"],
                        "result": final_response,
                        "timestamp": datetime.now().isoformat(),
                    },
                )
                workflow["last_run"] = datetime.now().isoformat()
                if self.scheduler and workflow.get("trigger") == "cron":
                    try:
                        job = self.scheduler.get_job(f"workflow_{workflow_id}")
                        workflow["next_run"] = (
                            job.next_run_time.isoformat()
                            if job and job.next_run_time
                            else None
                        )
                    except Exception:
                        workflow["next_run"] = None
                self._mark_workflow_status(workflow, "success", save=False)
                self._save_data("workflows")

            except Exception as e:
                _log.error(f"Workflow execution error: {e}", exc_info=True)
                self._mark_workflow_status(workflow, "failed", error=str(e), save=True)
            finally:
                self.running_workflow_ids.discard(workflow_id)

        self.socketio.start_background_task(run_workflow_with_tools)

    def _create_workflow_session(self, workflow: dict) -> str:
        """为工作流创建专属会话"""
        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "name": f"[工作流] {workflow['name']}",
            "type": "workflow",
            "workflow_id": workflow["id"],
            "session_mode": "agent",
            "created_at": datetime.now().isoformat(),
            "messages": [
                {"role": "system", "content": workflow.get("description", "")}
            ],
            "system_prompt": workflow.get("description", ""),
        }
        self.session_store.set_session(session_id, session)

        # 为工作流创建工作区
        if WORKSPACE_AVAILABLE:
            workspace_manager.get_or_create(
                session_id, "workflow", f"[工作流] {workflow['name']}"
            )

        return session_id

    def _send_workflow_result(self, workflow: dict, result: str):
        """发送工作流结果到指定目标"""
        config = workflow.get("config", {})
        target_type = config.get(
            "target_type", "none"
        )  # none, qq_group, qq_private, session
        target_id = config.get("target_id", "")

        if target_type == "none" or not target_id:
            return

        try:
            if target_type in ["qq_group", "qq_private"] and self.qq_bot:
                # 发送到 QQ - 使用线程运行异步任务
                import asyncio
                import threading

                async def send_qq_message():
                    try:
                        if target_type == "qq_group":
                            # 发送到群聊
                            await self.qq_bot.api.post_group_msg(
                                group_id=target_id, text=result
                            )
                        else:
                            # 发送到私聊
                            await self.qq_bot.api.post_private_msg(
                                user_id=target_id, text=result
                            )
                        _log.info(
                            f"Workflow result sent to QQ {target_type}: {target_id}"
                        )
                    except Exception as e:
                        _log.error(f"Failed to send workflow result: {e}")

                def run_async_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(send_qq_message())
                        loop.close()
                    except Exception as e:
                        _log.error(f"Failed to run async task: {e}")

                # 在新线程中运行异步任务
                threading.Thread(target=run_async_task, daemon=True).start()

            elif target_type == "session":
                # 发送到 Web 会话
                if self.session_store.get_session(target_id):
                    message = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": f"[工作流: {workflow['name']}]\n{result}",
                        "timestamp": datetime.now().isoformat(),
                        "sender": "Workflow",
                        "workflow_id": workflow["id"],
                    }
                    self.session_store.append_message(target_id, message)

                    # 通过 WebSocket 通知
                    self.socketio.emit("new_message", message, room=target_id)
                    _log.info(f"Workflow result sent to session: {target_id}")

        except Exception as e:
            _log.error(f"Failed to send workflow result: {e}")

    def trigger_workflow_by_message(
        self, workflow_id: str, message_content: str, source: str = "qq"
    ):
        """由消息触发工作流"""
        for workflow in self.workflows:
            if workflow["id"] == workflow_id and workflow.get("enabled"):
                trigger_data = {
                    "source": source,
                    "content": message_content,
                    "time": datetime.now().isoformat(),
                }
                self._execute_workflow(workflow_id, trigger_data)
                return True
        return False

    def _generate_session_name(
        self,
        messages: list[dict],
        session_id: str = None,
        parent_message_id: str = None,
    ) -> str:
        """根据对话内容生成会话名称"""
        if not self.ai_client:
            return None

        progress_card = None

        try:
            # 如果有 session_id 和 parent_message_id，创建进度卡片
            if (
                session_id
                and parent_message_id
                and PROGRESS_CARD_AVAILABLE
                and progress_card_manager
                and self.socketio
            ):
                progress_card = progress_card_manager.create_card(
                    session_id=session_id, parent_message_id=parent_message_id
                )
                if progress_card:
                    from nbot.core.progress_card import StepType

                    progress_card.update(StepType.THINKING, "📝 正在生成会话名称...")

            # 构建提示词 - 优先使用当前会话的角色信息
            from nbot.web.ai_service import _resolve_session_character_name

            personality_name = ""
            personality_desc = ""
            session_mode = ""
            max_name_len = 15

            if session_id:
                session = self.session_store.get_session(session_id)
                if session:
                    session_mode = str(session.get("session_mode") or "")
                    personality_name = _resolve_session_character_name(self, session)
                    character_id = session.get("character_id")
                    if character_id:
                        try:
                            from nbot.character.repository import ProfileRepository

                            base_dir = getattr(self, "base_dir", None) or os.path.abspath(
                                os.path.join(os.path.dirname(__file__), "..", "..")
                            )
                            profile = ProfileRepository(base_dir).get(character_id)
                            if profile:
                                personality_desc = str(profile.description or "")[:100]
                        except Exception:
                            pass

            # 回退到全局配置
            if not personality_name:
                personality_name = self.personality.get("name", "")
                personality_desc = self.personality.get("description", "")

            # 提取最近对话作为上下文
            conversation_text = ""
            for msg in messages[-12:]:
                role = "用户" if msg.get("role") == "user" else "角色"
                content = str(msg.get("content", ""))[:200]
                conversation_text += f"{role}: {content}\n"

            msg_count = len(messages)
            is_update = msg_count > 6

            role_context = ""
            if personality_name:
                role_context = f"当前角色是'{personality_name}'（{personality_desc}）。"

            update_hint = ""
            if is_update:
                update_hint = "对话已经进行了较长时间，请根据最新的主要话题重新命名，忽略早期已结束的话题。\n"

            # 风格区分：Agent 会话用功能性描述，角色会话用口语化/诗意标题
            if session_mode == "agent":
                max_name_len = 30
                style_requirements = (
                    "- 简明的功能性描述（如 '修复登录超时'、'爬虫数据清洗'、'季度报告整理'）\n"
                    "- 概括当前正在推进的任务或主题\n"
                    "- 不要口语化、不要诗意、不要表情符号\n"
                )
            else:
                style_requirements = (
                    "- 自然口语化，像聊天记录名称\n"
                    "- 有趣或有诗意更好\n"
                )

            prompt_messages = [
                {
                    "role": "system",
                    "content": (
                        f"你是一个会话命名助手。{role_context}请根据对话内容生成一个简短、有辨识度、贴合当前话题的标题。\n\n"
                        f"{update_hint}"
                        "要求：\n"
                        f"- 2-{max_name_len}个字\n"
                        f"{style_requirements}"
                        "- 直接返回标题，不要引号、标点或解释"
                    ),
                },
                {
                    "role": "user",
                    "content": f"请为以下对话生成标题：\n\n{conversation_text.strip()}",
                },
            ]

            # 使用当前活跃模型的协议发送请求
            import requests as _requests

            from nbot.core.model_adapter import response_json_utf8
            from nbot.core.protocols import get_protocol

            # 获取当前活跃模型的 provider_type
            active_model = None
            for m in self.ai_models:
                if m.get("id") == self.active_model_id and m.get("enabled", True):
                    active_model = m
                    break
            srv_pt = (active_model or {}).get(
                "provider_type",
                (active_model or {}).get("provider", "openai_compatible"),
            ) if active_model else self.ai_config.get("provider_type", "openai_compatible")
            srv_protocol = get_protocol(srv_pt)
            srv_url = srv_protocol.resolve_url(
                self.ai_base_url,
                model=self.ai_model or "",
                append_base_url_path=(active_model or {}).get("append_base_url_path", True),
                api_key=self.ai_api_key,
            )
            srv_headers = srv_protocol.build_headers(self.ai_api_key)
            srv_payload = srv_protocol.build_payload(
                self.ai_model, prompt_messages,
                stream=False,
                base_url=self.ai_base_url,
                provider_type=srv_pt,
                extra_body=request_extra_body(active_model or {}) or None,
            )
            from nbot.core.model_proxy import model_proxy_request_kwargs

            resp = _requests.post(
                srv_url,
                json=srv_payload,
                headers=srv_headers,
                timeout=30,
                **model_proxy_request_kwargs(self.ai_config.get("proxy_url", "")),
            )
            resp.raise_for_status()
            normalized = srv_protocol.parse_response(
                response_json_utf8(resp),
                model=self.ai_model or "",
                base_url=self.ai_base_url,
                provider_type=srv_pt,
            )

            class _Msg:
                pass
            class _Choice:
                pass
            class _Resp:
                pass
            _msg = _Msg()
            _msg.content = normalized.content
            _choice = _Choice()
            _choice.message = _msg
            response = _Resp()
            response.choices = [_choice]

            name = str(response.choices[0].message.content or "").strip()
            # 清理可能的引号和多余字符
            name = name.strip("\"'「」『』【】()（）")

            name = name.splitlines()[0].strip() if name else ""
            for prefix in ("标题:", "标题：", "会话标题:", "会话标题：", "Title:", "title:"):
                if name.startswith(prefix):
                    name = name[len(prefix):].strip()
                    break
            name = name.strip("`*_# \t\r\n\"'[](){}<>:：-—,.，。!！?？")
            if len(name) > max_name_len:
                name = name[:max_name_len].rstrip("`*_# \t\r\n\"'[](){}<>:：-—,.，。!！?？")

            if name and 2 <= len(name) <= max_name_len:
                # 完成进度卡片
                if progress_card:
                    from nbot.core.progress_card import StepType

                    progress_card.update(StepType.DONE, f"✅ 会话名称: {name}", True)
                    progress_card.complete()
                return name

            # 完成进度卡片（失败）
            if progress_card:
                from nbot.core.progress_card import StepType

                progress_card.update(StepType.DONE, "❌ 名称生成失败", False)
                progress_card.complete()
            return None

        except Exception as e:
            _log.error(f"生成会话名失败: {e}")
            if progress_card:
                from nbot.core.progress_card import StepType

                progress_card.update(StepType.DONE, f"❌ 错误: {str(e)}", False)
                progress_card.complete()
            return None

    def _get_ai_response(self, messages: list[dict]) -> str:
        return get_ai_response(self, messages)

    def _get_ai_response_with_usage(self, messages: list[dict]):
        """获取 AI 回复并返回 (text, usage)，用于需要记录 token 统计的场景。"""
        if not self.ai_client:
            _log.warning("AI client not initialized")
            return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。", None
        try:
            response = self.ai_client.chat_completion(
                model=self.ai_model, messages=messages, stream=False
            )
            if not response.choices or len(response.choices) == 0:
                base_resp = getattr(response, "base_resp", {}) or {}
                status_msg = base_resp.get("status_msg", "API 返回空响应")
                _log.warning(f"[AI] API 返回空 choices: {status_msg}")
                return f"AI 服务暂时不可用: {status_msg}", getattr(response, "usage", None)

            content = response.choices[0].message.content
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
            elif content.startswith("```"):
                content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]
            return content.strip(), getattr(response, "usage", None)
        except Exception as e:
            _log.error(f"AI response error: {e}", exc_info=True)
            return f"AI 服务出错: {str(e)}", None

    def _stream_ai_response(self, messages: list[dict], session_id: str, callback):
        return stream_ai_response(self, messages, session_id, callback)

    def _stream_send_response(
        self, session_id: str, message: dict, thinking_content: str = None
    ):
        return stream_send_response(self, session_id, message, thinking_content)

    def _get_ai_response_with_images(
        self, messages: list[dict], image_urls: list[str], user_question: str = None
    ) -> str:
        return get_ai_response_with_images(self, messages, image_urls, user_question)

    def _get_ai_response_with_tools(
        self,
        messages: list[dict],
        tools: list[dict],
        stop_event=None,
    ) -> dict:
        return get_ai_response_with_tools(
            self,
            messages,
            tools,
            stop_event,
        )

    def _parse_tool_call_from_text(self, content: str) -> list:
        return parse_tool_call_from_text(self, content)

    def _init_gateway(self):
        """初始化 Gateway 并注入 AgentService

        将已创建的 agent_service 注入到 Gateway 的 Dispatcher 中，
        使 Gateway 可以通过统一入口调度 AI Core。
        同时启用 SQLite 持久化存储，用于 Web 日志界面展示。
        """
        try:
            from nbot.gateway import ChannelGateway
            from nbot.gateway.dispatcher import GatewayDispatcher
            from nbot.gateway.gateway import set_gateway
            from nbot.gateway.storage import init_gateway_storage
            from nbot.gateway.tts_handler import create_tts_handler

            if not hasattr(self, "agent_service") or self.agent_service is None:
                _log.warning("[Gateway] AgentService 未就绪，Gateway 将使用延迟初始化")
                return

            # 创建带 AgentService 的 Dispatcher
            dispatcher = GatewayDispatcher(agent_service=self.agent_service)

            # 初始化 SQLite 持久化存储（用于事件日志记录和去重）
            storage = init_gateway_storage(data_dir=self.data_dir)

            # 创建 Gateway 实例（同步模式，带持久化）
            gateway = ChannelGateway(
                dispatcher=dispatcher,
                storage=storage,
            )

            # 创建并注册 TTS 处理器
            # 注意：switch 在 commands.py 中已初始化，需要导入
            try:
                from nbot.commands import switch as switch_manager
                self._tts_handler = create_tts_handler(switch_manager, self.qq_bot)
                gateway.delivery.register_tts_handler(self._tts_handler.handle_tts)
                _log.info("[Gateway] TTS 处理器已注册")
            except ImportError:
                _log.warning("[Gateway] 无法导入 switch_manager，TTS 处理器未注册")

            # 设置为全局实例
            set_gateway(gateway)

            _log.info(
                "[Gateway] 初始化完成 mode=sync storage=%s dispatcher=%s",
                storage._db_path if storage else "none",
                "injected" if self.agent_service else "none",
            )
        except ImportError:
            _log.warning("[Gateway] 模块导入失败，跳过 Gateway 初始化")
        except Exception as e:
            _log.error("[Gateway] 初始化异常 error=%s", str(e))

    def _register_routes(self):
        """注册 HTTP 路由"""
        register_admin_misc_routes(self.app, self)
        register_ai_config_routes(self.app, self)
        register_ai_model_routes(self.app, self)
        register_auth_routes(self.app, self)
        register_channel_routes(self.app, self)
        register_character_routes(self.app, self)
        register_gateway_routes(self.app)
        register_gateway_log_routes(self.app)
        register_global_search_routes(self.app, self)
        register_heartbeat_routes(self.app, self)
        register_group_routes(self.app, self)
        register_hook_routes(self.app, self)
        register_image_routes(self.app, self)
        register_knowledge_routes(self.app, self)
        register_live2d_routes(self.app, self)
        register_memory_routes(self.app, self)
        register_message_filter_routes(self.app, self)
        register_personality_routes(self.app, self)
        register_persona_routes(self.app, self)
        register_plugin_routes(self.app, self)
        register_prompt_preset_routes(self.app, self)
        register_plot_routes(self.app, self)
        register_push_routes(self.app, self)
        register_qq_overview_routes(self.app, self)
        register_qrcode_routes(self.app, self)
        register_review_routes(self.app, self)
        register_public_session_routes(self.app, self)
        register_session_routes(self.app, self)
        register_skill_routes(self.app, self)
        register_ssl_validation_routes(self.app, self)
        register_task_center_routes(self.app, self)
        register_tool_routes(self.app, self)
        register_mcp_server_routes(self.app, self)
        register_workflow_routes(self.app, self)
        register_world_book_routes(self.app, self)
        register_world_routes(self.app, self)
        register_web_agent_routes(self.app, self)
        register_workspace_private_routes(self.app, self)
        register_workspace_shared_routes(self.app, self)
        register_workspace_misc_routes(self.app, self)
        register_config_legacy_routes(self.app, self)
        register_config_transfer_routes(self.app, self)
        register_export_center_routes(self.app, self)
        register_webdav_backup_routes(self.app, self)
        register_update_routes(self.app, self)
        register_login_token_routes(self.app, self)
        register_oauth_routes(self.app, self)
        register_achievement_routes(self.app, self)
        register_smart_routing_routes(self.app, self)
        register_sticker_routes(self.app, self)

        # 初始化 Gateway 并注入 AgentService
        self._init_gateway()

    def _extract_request_token(self) -> str:
        """Extract auth token from request."""
        auth_header = request.headers.get("Authorization", "").strip()
        if auth_header.lower().startswith("bearer "):
            return auth_header[7:].strip()

        header_token = (
            request.headers.get("X-Auth-Token", "").strip()
            or request.headers.get("X-Token", "").strip()
        )
        if header_token:
            return header_token

        cookie_token = request.cookies.get("nbot_auth_token", "").strip()
        if cookie_token:
            return cookie_token

        if request.method in {"POST", "PUT", "PATCH", "DELETE"}:
            body_token = ""
            if request.is_json:
                data = request.get_json(silent=True) or {}
                body_token = str(data.get("token", "")).strip()
            if not body_token:
                body_token = request.form.get("token", "").strip()
            if body_token:
                return body_token

        return ""

    def _register_auth_middleware(self):
        """Protect all private API routes with login token."""
        public_api_paths = {
            "/api/login",
            "/api/verify-token",
            "/api/startup-status",
        }

        @self.app.before_request
        def _enforce_api_auth():
            if request.method == "OPTIONS":
                return None

            path = request.path or ""
            if not path.startswith("/api/"):
                return None

            if path in public_api_paths:
                return None
            if path.startswith("/api/files/gateway/"):
                return None
            if path.startswith("/api/channels/telegram/") and path.endswith("/webhook"):
                return None
            if path.startswith("/api/tts/audio/"):
                return None

            token = self._extract_request_token()
            username = self._validate_login_token(token)
            if not username:
                return jsonify(
                    {
                        "success": False,
                        "error": "Unauthorized",
                        "message": "Login required",
                    }
                ), 401

            g.auth_username = username
            g.auth_token = token
            return None

    def _register_socket_events(self):
        """Register WebSocket handlers."""
        register_socket_events(self)
        register_world_socket_events(self)

    def _trigger_ai_response(
        self,
        session_id: str,
        user_content: str,
        sender: str,
        attachments=None,
        parent_message_id=None,
        metadata=None,
        channel_id: str = "web",
    ):
        """触发 AI 响应处理（通过 Gateway 记录事件）

        Args:
            channel_id: 频道标识（web/qq/feishu/telegram/proactive），用于日志分类展示
        """
        adapter = _resolve_web_adapter(self.web_channel_adapter)
        chat_request = adapter.build_chat_request(
            conversation_id=session_id,
            content=user_content,
            sender=sender,
            attachments=attachments,
            parent_message_id=parent_message_id,
            metadata=metadata,
        )

        # 生成 trace_id 并通过通用方法记录到 Gateway 日志
        trace_id = self._record_gateway_event_start(
            channel_id=channel_id,
            session_id=session_id,
            user_content=user_content,
            sender=sender,
            attachments=attachments,
        )

        # 将 trace_id 传递给后续处理（Web 异步任务需要用它回补回复内容）
        if metadata is None:
            metadata = {}
        else:
            metadata = dict(metadata)
        try:
            session_data = self.sessions.get(session_id) or {}
            session_plot_mode = bool(session_data.get("plot_mode"))
            if session_plot_mode:
                metadata.setdefault("plot_mode", True)
                if session_data.get("plot_real_time_sync"):
                    metadata.setdefault("plot_real_time_sync", True)
                if session_data.get("plot_choice_style"):
                    metadata.setdefault(
                        "plot_choice_style", session_data.get("plot_choice_style")
                    )
                if session_data.get("plot_outline"):
                    metadata.setdefault(
                        "plot_outline", session_data.get("plot_outline")
                    )
        except Exception:
            pass
        metadata["_gateway_trace_id"] = trace_id
        chat_request.metadata = metadata

        # 调用 AI 处理
        try:
            result = self.agent_service.process(chat_request, adapter=adapter)

            reply_preview = ""
            if result and hasattr(result, 'final_content') and result.final_content:
                reply_preview = result.final_content[:200]
            elif result and isinstance(result, dict):
                reply_preview = str(result.get('final_content', ''))[:200]
            elif result and hasattr(result, 'content'):
                reply_preview = str(result.content)[:200]

            # 检测是否为异步调度（Web 频道返回 scheduled=True 的空响应）
            is_async_scheduled = False
            if result and hasattr(result, 'metadata') and result.metadata:
                is_async_scheduled = result.metadata.get('scheduled', False) or result.metadata.get('_gateway_trace_id')

            if not is_async_scheduled or reply_preview:
                # 同步完成 或 有实际内容 → 立即记录 delivered
                self._record_gateway_event_delivered(
                    trace_id=trace_id,
                    channel_id=channel_id,
                    session_id=session_id,
                    reply_preview=reply_preview,
                )
            # else: Web 异步场景，跳过此处，由后台任务 run_pipeline 回补完整 delivered 记录

        except Exception as e:
            self._record_gateway_event_failed(
                trace_id=trace_id,
                channel_id=channel_id,
                session_id=session_id,
                error=str(e)[:500],
            )
            raise

    def _record_gateway_event_start(
        self,
        *,
        channel_id: str,
        session_id: str,
        user_content: str,
        sender: str,
        attachments=None,
    ) -> str:
        """记录 Gateway 事件的起始阶段（received + dispatched）

        供所有频道统一调用，返回 trace_id 用于后续状态更新。
        外部服务（飞书/Telegram/QQ）可直接调用此方法记录事件。

        Returns:
            trace_id: 事件追踪 ID（Gateway 不可用时返回空字符串）
        """
        from nbot.gateway.gateway import get_gateway
        from nbot.gateway.trace import TraceFactory

        gateway = get_gateway()
        trace_id = ""
        if not gateway or not gateway.event_store:
            return trace_id

        trace_factory = getattr(gateway, 'trace_factory', None) or TraceFactory()
        trace_id = trace_factory.new_trace_id()

        session_name = ""
        session_data = self.sessions.get(session_id) or {}
        if session_data:
            session_name = session_data.get("name", "")

        content_preview = user_content[:150] if user_content else ""

        attachment_summary = ""
        if attachments and isinstance(attachments, list):
            att_names = [a.get("name", "file") for a in attachments if isinstance(a, dict)]
            if att_names:
                attachment_summary = f"[{len(att_names)}个附件: {', '.join(att_names[:3])}]"

        try:
            gateway.record_lifecycle_event(
                trace_id=trace_id,
                channel_id=channel_id,
                status="received",
                event_type="message",
                conversation_id=session_id,
                user_id=str(sender),
                raw_event={
                    "content": content_preview,
                    "sender": sender,
                    "attachments": attachment_summary,
                    "session_name": session_name,
                },
                metadata={
                    "content_length": len(user_content),
                    "has_attachments": bool(attachments),
                    "session_name": session_name,
                },
            )
            gateway.record_lifecycle_event(
                trace_id=trace_id,
                channel_id=channel_id,
                status="dispatched",
                conversation_id=session_id,
                metadata={"session_name": session_name},
            )
        except Exception as e:
            _log.debug("[Gateway] 事件记录失败: %s", str(e))

        return trace_id

    def _record_gateway_event_delivered(
        self,
        *,
        trace_id: str,
        channel_id: str,
        session_id: str,
        reply_preview: str = "",
    ) -> None:
        """记录 Gateway 事件完成（delivered）"""
        from nbot.gateway.gateway import get_gateway

        gateway = get_gateway()
        if not gateway or not gateway.event_store or not trace_id:
            return

        session_name = ""
        session_data = self.sessions.get(session_id) or {}
        if session_data:
            session_name = session_data.get("name", "")

        try:
            gateway.record_lifecycle_event(
                trace_id=trace_id,
                channel_id=channel_id,
                status="delivered",
                conversation_id=session_id,
                raw_event={"reply_preview": reply_preview} if reply_preview else None,
                metadata={
                    "reply_length": len(reply_preview),
                    "session_name": session_name,
                } if reply_preview else {"session_name": session_name},
            )
        except Exception as e:
            _log.debug("[Gateway] delivered 事件记录失败: %s", str(e))

    def _record_gateway_event_failed(
        self,
        *,
        trace_id: str,
        channel_id: str,
        session_id: str,
        error: str,
    ) -> None:
        """记录 Gateway 事件失败（dispatch_failed）"""
        from nbot.gateway.gateway import get_gateway

        gateway = get_gateway()
        if not gateway or not gateway.event_store or not trace_id:
            return

        try:
            gateway.record_lifecycle_event(
                trace_id=trace_id,
                channel_id=channel_id,
                status="dispatch_failed",
                conversation_id=session_id,
                error=error,
            )
        except Exception:
            pass

    # ============================================================
    # 通用操作日志 API（供所有模块使用）
    # ============================================================

    def record_operation(
        self,
        *,
        module: str,
        action: str,
        description: str = "",
        detail: str = "",
        status: str = "completed",
        metadata: dict | None = None,
        error: str = "",
    ) -> None:
        """记录任意模块的操作日志到 Gateway 事件表

        所有模块（AI 配置、角色卡、记忆、知识库、工具等）都应通过此方法
        记录关键操作，统一在 Web 日志界面展示。

        Args:
            module: 模块标识（ai_model/character/memory/knowledge/tool/config/skill/session）
            action: 操作类型（switch/create/update/delete/execute/upload/download/import/export）
            description: 用户可读的操作描述，如 "切换模型 → GPT-4o"
            detail: 详细内容（截断到 300 字符），如配置变更的 JSON 片段
            status: 操作结果（completed/failed/pending/skipped）
            metadata: 结构化附加数据
            error: 错误信息（仅 failed 状态时填写）

        用法示例:
            self.record_operation(
                module="ai_model", action="switch",
                description=f"切换模型 → {model_name}",
                detail=f"从 {old_model} 切换到 {model_name}",
            )
        """
        from nbot.gateway.gateway import get_gateway
        from nbot.gateway.trace import TraceFactory

        gateway = get_gateway()
        if not gateway or not (gateway.log_service or gateway.event_store):
            return

        trace_factory = getattr(gateway, 'trace_factory', None) or TraceFactory()
        trace_id = trace_factory.new_trace_id()

        # 根据状态决定记录的字段
        raw_event = None
        if description or detail:
            raw_event = {"description": description[:200], "detail": detail[:300]}

        try:
            if gateway.log_service:
                operation_metadata = dict(metadata or {})
                operation_metadata.setdefault("operation_status", status)
                gateway.record_lifecycle_event(
                    trace_id=trace_id,
                    channel_id=module,
                    status=action,
                    event_type="operation",
                    raw_event=raw_event,
                    metadata=operation_metadata,
                    error=error if status == "failed" else "",
                    action=action,
                    level="error" if status == "failed" else "info",
                    stage=status,
                    message=description or action,
                )
            elif gateway.event_store:
                gateway.event_store.record(
                    trace_id=trace_id,
                    channel_id=module,
                    status=action,
                    event_type="operation",
                    raw_event=raw_event,
                    metadata=metadata,
                    error=error if status == "failed" else "",
                )
        except Exception as e:
            _log.debug("[Gateway] 操作日志记录失败 module=%s action=%s: %s", module, action, str(e))

    def add_message_to_session(
        self, session_id: str, role: str, content: str, sender: str, source: str = "qq"
    ):
        """从外部添加消息到会话（QQ 消息同步）"""
        if not self.session_store.get_session(session_id):
            return

        adapter = _resolve_web_adapter(self.web_channel_adapter)
        message = adapter.build_message(
            role=role,
            content=content,
            sender=sender,
            conversation_id=session_id,
            source=source,
        )

        self.session_store.append_message(session_id, message)
        self.socketio.emit("new_message", message, room=session_id)

    def create_web_session(self, user_id: str, name: str = None) -> str:
        """创建 Web 会话"""
        session_id = str(uuid.uuid4())

        # 使用角色卡的 systemPrompt，如果没有则编译生成
        from .routes.personality import compile_personality_prompt
        
        # 调试日志
        _log.info(f"Creating session with personality: {self.personality.get('name', '未命名')}")
        _log.info(f"systemPrompt length: {len(self.personality.get('systemPrompt', ''))}")
        
        system_prompt = self.personality.get("systemPrompt", "")
        if not system_prompt:
            _log.warning("systemPrompt is empty, compiling from personality data")
            system_prompt = compile_personality_prompt(self.personality, user_name=user_id)
            _log.info(f"Compiled system_prompt length: {len(system_prompt)}")
        else:
            # 替换模板变量 {{user}} -> 当前用户名
            system_prompt = system_prompt.replace('{{user}}', user_id or '')
            system_prompt = system_prompt.replace('{{char}}', self.personality.get("name", ""))

        sender_name = self.personality.get("name", "AI")
        character_id = self.personality.get("id") or sender_name

        # 记忆由 ai_pipeline.py 中的 PromptStack 动态注入，不在此处重复添加

        # 添加 Skills 到系统提示词
        features = (self.settings or {}).get("features") or {}
        if features.get("skills_prompt_injection", False):
            enabled_skills = [s for s in self.skills_config if s.get("enabled", True)]
            system_prompt += format_skills_prompt(self.skills_config)
            _log.info(f"已添加 {len(enabled_skills)} 个技能到会话 {session_id[:8]}")
        else:
            _log.info(f"Skills prompt injection disabled for session {session_id[:8]}")

        # 构建消息列表，包含系统提示词和开场白
        messages = [{"role": "system", "content": system_prompt}]
        
        # 如果有开场白，添加为第一条 assistant 消息
        first_message = self.personality.get("firstMessage", "")
        if first_message:
            first_message = first_message.replace('{{user}}', user_id or '')
            messages.append({
                "role": "assistant", 
                "content": first_message,
                "sender": sender_name
            })
            _log.info(f"已添加开场白，来自角色: {self.personality.get('name', 'AI')}")

        session = {
            "id": session_id,
            "name": name or f"Web 会话 {session_id[:8]}",
            "type": "web",
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "messages": messages,
            "system_prompt": system_prompt,
            "character_id": character_id,
            "sender_name": sender_name,
            "sender_avatar": self.personality.get("avatar", ""),
            "sender_portrait": self.personality.get("portrait", ""),
        }

        self.session_store.set_session(session_id, session)
        return session_id

    def _refresh_heartbeat_summary_config(self):
        configs = self.session_heartbeat_manager.list_enabled_configs()
        if configs:
            primary = sorted(
                configs,
                key=lambda item: (
                    item.get("last_run") or "",
                    item.get("session_id") or "",
                ),
                reverse=True,
            )[0]
            self.heartbeat_config.update(
                {
                    "enabled": True,
                    "interval_minutes": primary.get("interval_minutes", 60),
                    "content_file": primary.get("content_file", "heartbeat.md"),
                    "target_session_id": primary.get(
                        "target_session_id", primary.get("session_id", "")
                    ),
                    "targets": [
                        f"web:{primary.get('target_session_id', primary.get('session_id', ''))}"
                    ],
                    "last_run": primary.get("last_run"),
                    "next_run": primary.get("next_run"),
                    "last_trace_id": primary.get("last_trace_id", ""),
                    "last_gateway_status": primary.get("last_gateway_status", ""),
                }
            )
            return
        self.heartbeat_config.update(
            {
                "enabled": False,
                "target_session_id": "",
                "targets": [],
                "last_trace_id": "",
                "last_gateway_status": "",
            }
        )

    def _run_session_heartbeat_execution(
        self,
        session_id: str,
        config: dict[str, Any],
        *,
        force: bool = False,
    ):
        # 主动聊天使用心跳调度，但触发指令只存在于本次 AI 请求中。
        # 它不是用户消息，因此不应写入或显示在会话历史中。
        # `__proactive` 是旧版已持久化的主动聊天 key；后缀判定用于
        # 无需用户重新开关功能即可迁移到新的静默触发语义。
        if config.get("proactive_chat") or session_id.endswith("__proactive"):
            return self._run_proactive_chat_heartbeat(session_id, config)

        # 静默心跳：不发消息给用户，只更新角色活动状态（角色"自我生活"）
        if config.get("silent"):
            return self._run_silent_heartbeat(session_id, config)

        target_session_id = str(config.get("target_session_id") or session_id)
        content_file = config.get("content_file", "heartbeat.md")
        content = self._load_heartbeat_content(content_file)
        if not content:
            _log.warning("Heartbeat content file '%s' not found or empty", content_file)
            return {"messages_sent": 0, "target_session_id": target_session_id}

        # QQ 会话：执行前同步最新消息（包括 AI 回复）
        if target_session_id.startswith("qq_private_") or target_session_id.startswith("qq_group_"):
            try:
                parts = target_session_id.split("_")
                if target_session_id.startswith("qq_private_"):
                    self.sync_qq_messages(user_id=parts[2], create_if_not_exists=True)
                elif len(parts) >= 4:
                    self.sync_qq_messages(group_id=parts[2], group_user_id=parts[3], create_if_not_exists=True)
                else:
                    self.sync_qq_messages(group_id=parts[2], create_if_not_exists=True)
            except Exception as e:
                _log.warning("Heartbeat QQ message sync failed for %s: %s", target_session_id, e)

        session = self.session_store.get_session(target_session_id)
        if not session:
            _log.warning("Heartbeat target session not found: %s", target_session_id)
            return {"messages_sent": 0, "target_session_id": target_session_id}

        heartbeat_adapter = _resolve_web_adapter(self.web_channel_adapter)
        hb_user_message = _build_heartbeat_user_message(heartbeat_adapter, target_session_id, content)
        self.session_store.append_message(target_session_id, hb_user_message)

        heartbeat_messages = []
        current_session = self.session_store.get_session(target_session_id) or session
        for msg in visible_messages(current_session.get("messages", []))[-12:]:
            role = msg.get("role")
            if role in ["system", "user", "assistant"]:
                heartbeat_messages.append(
                    {"role": role, "content": msg.get("content", "")}
                )

        if not heartbeat_messages:
            heartbeat_messages = [{"role": "user", "content": content}]

        response_text, usage = self._get_ai_response_with_usage(heartbeat_messages)
        # 记录 token 用量到统计（purpose=heartbeat）
        if usage:
            try:
                from nbot.core.token_stats import (
                    get_token_stats_manager,
                    PURPOSE_HEARTBEAT,
                )

                get_token_stats_manager().record_usage(
                    getattr(usage, "prompt_tokens", 0) or 0,
                    getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    model=self.ai_model or "",
                    session_id=target_session_id,
                    channel_type="web",
                    user_id=target_session_id,
                    source="heartbeat",
                    purpose=PURPOSE_HEARTBEAT,
                )
            except Exception as exc:
                _log.debug("[Heartbeat] token stats record failed: %s", exc)
        if not response_text:
            return {"messages_sent": 0, "target_session_id": target_session_id}

        hb_assistant_message = _build_heartbeat_assistant_message(
            heartbeat_adapter, target_session_id, response_text
        )
        self.session_store.append_message(target_session_id, hb_assistant_message)
        if self.socketio:
            self.socketio.emit(
                "session_updated",
                {"session_id": target_session_id, "action": "heartbeat_completed"},
                room=target_session_id,
            )
        # 心跳执行后刷新角色活动状态，让下次对话时角色"从某个活动中被打断"
        self._refresh_heartbeat_activity(target_session_id)
        return {
            "messages_sent": 1,
            "target_session_id": target_session_id,
            "session_id": session_id,
            "result_summary": "sent 1 message",
        }

    def _run_proactive_chat_heartbeat(
        self, session_id: str, config: dict[str, Any]
    ) -> dict[str, Any]:
        """静默触发一次主动聊天，只将 AI 回复写入目标会话。"""
        target_session_id = str(config.get("target_session_id") or session_id)
        session = self.session_store.get_session(target_session_id)
        if not session:
            _log.warning("Proactive chat target session not found: %s", target_session_id)
            return {"messages_sent": 0, "target_session_id": target_session_id}

        prompt = str(config.get("internal_prompt") or "").strip()
        if not prompt:
            prompt_config = self._get_session_proactive_chat_config(session)
            prompt = str(prompt_config.get("prompt") or "").strip()
        if not prompt:
            _log.warning("Proactive chat prompt is empty for %s", target_session_id)
            return {"messages_sent": 0, "target_session_id": target_session_id}

        triggered_at = datetime.now().astimezone().isoformat()
        session["proactive_chat_pending_since"] = triggered_at
        session["proactive_chat_last_run"] = triggered_at
        self.session_store.set_session(target_session_id, session)

        try:
            self._trigger_ai_response(
                target_session_id,
                prompt,
                "system",
                metadata={
                    "is_proactive_chat": True,
                    "is_heartbeat": True,
                    "skip_auto_memory": True,
                    "skip_character_after_turn": True,
                    "silent_trigger": True,
                    "source": "proactive_chat",
                    "proactive_chat_triggered_at": triggered_at,
                },
                channel_id="proactive",
            )
        except Exception:
            session.pop("proactive_chat_pending_since", None)
            self.session_store.set_session(target_session_id, session)
            raise

        return {
            "messages_sent": 1,
            "target_session_id": target_session_id,
            "session_id": session_id,
            "result_summary": "scheduled proactive chat reply",
        }

    def _run_silent_heartbeat(self, session_id: str, config: dict[str, Any]) -> dict:
        """静默心跳：不发消息给用户，但调用 AI 生成角色生活片段。

        由"同步现实时间"开启时注册，定期让角色"自我生活"：
        - 获取当前会话上下文 + 角色卡 + 昼夜节律 + 最近 timeline
        - 调用 AI 生成一段 50-100 字的生活片段
        - 写入跨会话 timeline（下次对话角色能"记得"）
        - 写入 recent_digest（MemoryFS 持久化）
        - 更新 scene.current_activity（从 AI 内容提取，而非随机选）
        - 记录 token 统计到 PURPOSE_HEARTBEAT
        """
        target_session_id = str(config.get("target_session_id") or session_id)
        _log.info(
            "[Heartbeat] silent heartbeat for %s (life sim)", target_session_id
        )

        # 1. 解析角色 ID 和 scope
        char_id, scope_id = self._resolve_heartbeat_character_scope(target_session_id)
        if not char_id:
            _log.debug("[Heartbeat] cannot resolve character for %s, skip life sim", target_session_id)
            # 兜底：只刷新活动状态
            self._refresh_heartbeat_activity(target_session_id)
            return self._silent_heartbeat_finalize(session_id, target_session_id, generated=None)

        # 2. 收集上下文：会话最近消息 + 角色卡 + 昼夜节律 + timeline
        session = self.session_store.get_session(target_session_id) or {}
        recent_messages = self._collect_heartbeat_context_messages(session)
        profile_text = self._collect_heartbeat_character_profile(char_id)
        circadian_text, circadian_phase = self._collect_heartbeat_circadian_context()
        timeline_text = self._collect_heartbeat_timeline(char_id, target_session_id)

        # 3. 构造 prompt 调用 AI 生成生活片段
        prompt = self._build_life_sim_prompt(
            profile_text, circadian_text, timeline_text, recent_messages, phase=circadian_phase
        )
        generated, usage = self._get_ai_response_with_usage(prompt)
        if usage:
            self._record_heartbeat_token_usage(usage, target_session_id)

        # 4. 持久化生活片段到 MemoryFS
        saved = False
        activity = None
        if generated and not generated.startswith("AI 服务"):
            saved = self._persist_life_sim_to_memory(
                char_id, target_session_id, generated
            )
            activity = self._extract_activity_from_life_sim(generated)

        # 5. 更新 current_activity（优先用 AI 提取的，否则兜底推断）
        if activity:
            self._update_heartbeat_activity(target_session_id, char_id, scope_id, activity)
        else:
            self._refresh_heartbeat_activity(target_session_id)

        _log.info(
            "[Heartbeat] life sim for %s: generated=%s saved=%s",
            target_session_id, bool(generated), saved,
        )
        return self._silent_heartbeat_finalize(session_id, target_session_id, generated=generated)

    def _silent_heartbeat_finalize(self, session_id: str, target_session_id: str, generated: str | None) -> dict:
        """静默心跳收尾：通知前端 + 返回结果。"""
        if self.socketio:
            self.socketio.emit(
                "session_updated",
                {
                    "session_id": target_session_id,
                    "action": "silent_heartbeat_completed",
                },
                room=target_session_id,
            )
        return {
            "messages_sent": 0,
            "target_session_id": target_session_id,
            "session_id": session_id,
            "silent": True,
            "result_summary": "life sim tick",
            "generated": bool(generated),
        }

    def _collect_heartbeat_context_messages(self, session: dict) -> str:
        """收集会话最近消息作为上下文（最多 6 条，限长）。"""
        try:
            messages = visible_messages(session.get("messages") or [])
            recent = []
            for msg in reversed(messages):
                if len(recent) >= 6:
                    break
                role = msg.get("role")
                if role not in ("user", "assistant"):
                    continue
                content = str(msg.get("content") or "")[:200]
                if content.strip():
                    recent.append(f"{role}: {content}")
            recent.reverse()
            return "\n".join(recent) if recent else "（暂无对话历史）"
        except Exception:
            return "（暂无对话历史）"

    def _collect_heartbeat_character_profile(self, char_id: str) -> str:
        """收集角色卡资料（名字、性格、设定）。"""
        try:
            from nbot.character.repository import ProfileRepository

            repo = ProfileRepository(self.base_dir)
            profile = repo.get(char_id)
            if not profile:
                return f"角色ID: {char_id}"
            parts = [f"角色名: {profile.name}"]
            if profile.description:
                parts.append(f"描述: {profile.description[:200]}")
            if profile.personality:
                parts.append(f"性格: {profile.personality[:200]}")
            if profile.scenario:
                parts.append(f"设定: {profile.scenario[:200]}")
            return "\n".join(parts)
        except Exception as exc:
            _log.debug("[Heartbeat] collect profile failed: %s", exc)
            return f"角色ID: {char_id}"

    def _collect_heartbeat_circadian_context(self) -> tuple[str, str]:
        """收集当前昼夜节律上下文。

        返回 (prompt_text, phase)：
        - prompt_text: 注入到 user message 的可读文本
        - phase: 阶段标识（sleeping/morning/...），用于 system prompt
          判定是否需要夜间睡眠保护
        """
        try:
            from nbot.review.time_context import build_circadian_state, format_circadian_prompt

            state = build_circadian_state()
            return format_circadian_prompt(state), str(state.get("phase") or "")
        except Exception:
            return "", ""

    def _collect_heartbeat_timeline(self, char_id: str, conversation_id: str = "") -> str:
        """收集最近时间线（让 AI 知道角色最近经历了什么）。

        优先级：
        1. 本会话之前的生活片段（life_sim）—— 同会话连续性
        2. 跨会话 timeline —— 兜底，避免完全无历史
        """
        try:
            from nbot.memory.fs import get_memory_fs

            mfs = get_memory_fs(self.data_dir)
            # 优先读本会话的 life_sim（与新片段同源）
            if conversation_id:
                life_sim_mf = mfs.read(mfs.path_life_sim(char_id, conversation_id))
                if life_sim_mf and life_sim_mf.content:
                    lines = life_sim_mf.content.strip().split("\n")
                    if lines:
                        return "\n".join(lines[-5:])
            # 兜底：跨会话 timeline
            timeline_mf = mfs.read(mfs.path_timeline(char_id))
            if timeline_mf and timeline_mf.content:
                lines = timeline_mf.content.strip().split("\n")
                return "\n".join(lines[-5:]) if lines else "（暂无经历）"
            return "（暂无经历）"
        except Exception:
            return "（暂无经历）"

    def _build_life_sim_prompt(
        self, profile: str, circadian: str, timeline: str, recent: str, phase: str = ""
    ) -> list[dict]:
        """构造生成角色生活片段的 prompt。

        关键约束（避免 AI 把生活片段写成"和用户对话"）：
        - 用户当前不在场，这是角色独处时的真实生活经历
        - 不要写任何对话、互动、用户出现
        - sleeping 阶段：写"在睡觉/做梦/被吵醒前的休息"类内容
        - 第一行用短语概括活动（用于更新 current_activity）
        """
        # 基础系统提示：明确"独处"语境
        system = (
            "你是一个角色生活模拟器。根据角色设定、当前时段、近期经历，"
            "生成一段角色独处时真实经历的生活片段。\n\n"
            "【关键约束】\n"
            "1. 用户当前不在场——这是角色一个人独处时发生的事，"
            "不要写任何与用户对话、互动、回应用户的内容\n"
            "2. 不要出现'用户'、'TA'、'我们'、'和你聊天'、'你刚才说'等"
            "暗示有对话的措辞\n"
            "3. 50-100 字，第一人称或第三人称均可（与角色卡风格一致）\n"
            "4. 内容必须贴合角色性格、当前昼夜时段（如深夜不会去散步）\n"
            "5. 可以是日常琐事、情绪流动、小插曲，但不要重大剧情转折\n"
            "6. 不要提到'系统'、'心跳'、'模拟'等元信息\n"
            "7. 第一行用一个短语概括角色正在做什么（如：在厨房煮咖啡）"
        )

        # sleeping 阶段：追加休息保护
        if phase == "sleeping":
            system += (
                "\n\n【夜间睡眠保护】\n"
                "现在是深夜/凌晨时段，角色正在睡觉休息中。"
                "请生成角色在睡眠中或睡前的状态（如：正在熟睡、做了个梦、"
                "在浅睡中翻身、被窗外的声响短暂吵醒又睡过去等）。"
                "不要写角色深夜外出、看书、运动、喝咖啡等任何清醒活动。"
            )

        user_parts = [profile]
        if circadian:
            user_parts.append(f"【当前时段】\n{circadian}")
        user_parts.append(f"【近期经历】\n{timeline}")
        user_parts.append(
            "【参考信息】用户最近的对话历史（仅用于了解用户身份，"
            "不要让角色在生活片段中与用户互动）：\n"
            f"{recent}"
        )
        user_parts.append("请生成角色独处时的一段生活片段：")
        return [
            {"role": "system", "content": system},
            {"role": "user", "content": "\n\n".join(user_parts)},
        ]

    def _record_heartbeat_token_usage(self, usage, target_session_id: str) -> None:
        """记录心跳 token 用量到统计。"""
        try:
            from nbot.core.token_stats import get_token_stats_manager, PURPOSE_HEARTBEAT

            get_token_stats_manager().record_usage(
                getattr(usage, "prompt_tokens", 0) or 0,
                getattr(usage, "completion_tokens", 0) or 0,
                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                model=self.ai_model or "",
                session_id=target_session_id,
                channel_type="web",
                user_id=target_session_id,
                source="heartbeat_silent",
                purpose=PURPOSE_HEARTBEAT,
            )
        except Exception as exc:
            _log.debug("[Heartbeat] token stats record failed: %s", exc)

    def _persist_life_sim_to_memory(
        self, char_id: str, target_session_id: str, content: str
    ) -> bool:
        """把生活片段持久化到 MemoryFS（仅写入本会话独立的 life_sim 路径）。

        关键约束：
        - 不写 timeline（保持 timeline 纯粹，让 auto_memory 的 important_event 不被污染）
        - 不写 recent_digest（recent_digest 是用户级画像摘要，不应混入会话级生活片段）
        - 写入 path_life_sim(char_id, conversation_id)，按 conversation_id 严格隔离
        """
        try:
            from datetime import datetime as _dt

            from nbot.memory.fs import get_memory_fs

            mfs = get_memory_fs(self.data_dir)
            # 追加到本会话独立的生活片段文件（按 conversation_id 隔离）
            timestamp = _dt.now().astimezone().strftime("%Y-%m-%d %H:%M")
            entry = f"[{timestamp}] {content.strip()}"
            mfs.write(
                mfs.path_life_sim(char_id, target_session_id),
                character_id=char_id,
                target_id=target_session_id,
                title="角色生活片段",
                content=entry,
                summary="心跳生成的角色自我生活经历",
                importance=0.5,
                append=True,
            )
            return True
        except Exception as exc:
            _log.warning("[Heartbeat] persist life sim to memory failed: %s", exc, exc_info=True)
            return False

    def _extract_activity_from_life_sim(self, content: str) -> str:
        """从 AI 生成的生活片段提取活动标签（第一行）。

        prompt 要求第一行是活动概括，如"在厨房煮咖啡"。
        提取失败时返回空字符串，由调用方兜底。
        """
        try:
            first_line = content.strip().split("\n", 1)[0].strip()
            # 去掉可能的前缀符号（如"1. "、"· "等）
            for prefix in ("1.", "2.", "3.", "·", "•", "- ", "* "):
                if first_line.startswith(prefix):
                    first_line = first_line[len(prefix):].strip()
            # 限制长度
            return first_line[:60] if first_line else ""
        except Exception:
            return ""

    def _update_heartbeat_activity(
        self, session_id: str, char_id: str, scope_id: str, activity: str
    ) -> None:
        """用 AI 提取的活动标签更新 CharacterState.scene。"""
        try:
            from datetime import datetime as _dt

            from nbot.character.repository import CharacterStateRepository

            repo = CharacterStateRepository()
            state = repo.get_or_create(character_id=char_id, scope_id=scope_id)
            state.scene = state.scene if isinstance(state.scene, dict) else {}
            state.scene["current_activity"] = activity
            state.scene["activity_source"] = "heartbeat_ai"
            state.scene["activity_updated_at"] = _dt.now().astimezone().isoformat(
                timespec="seconds"
            )
            repo.save(state)
            _log.debug(
                "[Heartbeat] activity updated (AI) for %s: %s", session_id, activity
            )
        except Exception as exc:
            _log.debug("[Heartbeat] update activity (AI) failed: %s", exc)

    def _refresh_heartbeat_activity(self, session_id: str) -> None:
        """心跳执行后刷新角色 current_activity。

        统一活动状态维护入口：心跳触发时基于当前时间推断角色活动，
        写入 CharacterState.scene，下次对话 before_turn 会读取这个值。
        如果推断失败，before_turn 的 _ensure_current_activity 保底机制会兜底。
        """
        try:
            from nbot.review.time_context import infer_current_activity

            activity = infer_current_activity()
            # 推断 session_id 对应的 character_id 和 scope_id
            char_id, scope_id = self._resolve_heartbeat_character_scope(session_id)
            if not char_id or not scope_id:
                return
            from nbot.character.repository import CharacterStateRepository

            repo = CharacterStateRepository()
            state = repo.get_or_create(character_id=char_id, scope_id=scope_id)
            state.scene = state.scene if isinstance(state.scene, dict) else {}
            state.scene["current_activity"] = activity
            state.scene["activity_source"] = "heartbeat"
            from datetime import datetime as _dt

            state.scene["activity_updated_at"] = _dt.now().astimezone().isoformat(
                timespec="seconds"
            )
            repo.save(state)
            _log.debug(
                "[Heartbeat] activity refreshed for %s: %s", session_id, activity
            )
        except Exception as exc:
            _log.debug("[Heartbeat] refresh activity failed: %s", exc)

    def _resolve_heartbeat_character_scope(self, session_id: str):
        """根据 session_id 推断 (character_id, scope_id)。

        QQ 私聊：qq_private_{user_id} → scope_id = session_id
        QQ 群聊：qq_group_{group_id}_{user_id} → scope_id = session_id
        Web：web_{id} → scope_id = session_id
        character_id 从 session 元数据或默认角色获取。
        """
        try:
            session = self.session_store.get_session(session_id) or {}
            # 优先从 session 元数据读取 character_id
            char_id = str(
                session.get("character_id")
                or session.get("character_name")
                or ""
            ).strip()
            if not char_id:
                # 尝试从 ProfileRepository 取默认角色
                try:
                    from nbot.character.repository import ProfileRepository

                    profiles = ProfileRepository().list_all()
                    if profiles:
                        char_id = profiles[0].id or profiles[0].name or ""
                except Exception:
                    pass
            if not char_id:
                return "", ""
            return char_id, session_id
        except Exception:
            return "", ""

    def _init_heartbeat_scheduler(self):
        """初始化 Heartbeat 调度器"""
        self._refresh_heartbeat_summary_config()
        if not self.session_heartbeat_manager.any_enabled():
            _log.info("Heartbeat is disabled")
            return

        self._start_heartbeat_job(self.heartbeat_config.get("interval_minutes", 60))

    def _start_heartbeat_job(self, interval_minutes: int):
        """启动 Heartbeat 定时任务"""
        if not self.scheduler:
            _log.warning("Scheduler not available for heartbeat")
            return

        # 抑制 heartbeat 任务的 apscheduler 轮询日志（1 分钟一次，纯噪音）。
        # 只安装一次，避免重复注册 filter。
        apscheduler_executor_logger = logging.getLogger("apscheduler.executors.default")
        if not any(
            isinstance(f, _HeartbeatJobLogFilter) for f in apscheduler_executor_logger.filters
        ):
            apscheduler_executor_logger.addFilter(_HeartbeatJobLogFilter())

        # 移除旧的 job
        if self.heartbeat_job:
            try:
                self.scheduler.remove_job("heartbeat")
            except:
                pass

        try:
            # 使用同步包装函数来调用异步函数
            def run_heartbeat_sync():
                import asyncio

                try:
                    # 尝试获取当前事件循环
                    asyncio.get_running_loop()
                    # 如果已经在事件循环中，创建任务
                    asyncio.create_task(self._execute_heartbeat())
                except RuntimeError:
                    # 没有事件循环，创建新的
                    asyncio.run(self._execute_heartbeat())

            job = self.scheduler.add_job(
                func=run_heartbeat_sync,
                trigger="interval",
                minutes=max(1, int(interval_minutes or 1)),
                id="heartbeat",
                replace_existing=True,
            )
            self.heartbeat_job = job
            self.heartbeat_config["next_run"] = (
                job.next_run_time.isoformat() if job.next_run_time else None
            )
            _log.info(f"Heartbeat scheduled every {interval_minutes} minutes")
        except Exception as e:
            _log.error(f"Failed to start heartbeat job: {e}")

    def _stop_heartbeat_job(self):
        """停止 Heartbeat 定时任务"""
        if self.scheduler and self.heartbeat_job:
            try:
                self.scheduler.remove_job("heartbeat")
                self.heartbeat_job = None
                _log.info("Heartbeat job stopped")
            except:
                pass

    async def _execute_heartbeat(self, force: bool = False, _from_gateway: bool = False):
        """执行 Heartbeat 任务

        Args:
            force: 是否强制执行，跳过到期判定（手动触发时用）
        """
        if hasattr(self, "session_heartbeat_manager"):
            self._refresh_heartbeat_summary_config()
            if force:
                # 手动触发：执行第一个 enabled config（跳过 _is_due 判定）
                enabled = self.session_heartbeat_manager.list_enabled_configs()
                if not enabled:
                    return None
                return await self.session_heartbeat_manager.execute_session(
                    enabled[0]["session_id"],
                    force=True,
                    trigger_source="manual",
                )
            # 定时 tick：逐个检查 _is_due，只执行到期的会话
            return await self.session_heartbeat_manager.execute_due_sessions()
        if not force and not self.heartbeat_config.get("enabled"):
            _log.info("Heartbeat is disabled, skipping execution")
            return
        if not _from_gateway:
            from nbot.gateway.gateway import get_gateway

            gateway = get_gateway()
            if gateway:
                result = await gateway.submit_internal_task(
                    task_kind="heartbeat",
                    task_id="heartbeat",
                    task_name="heartbeat",
                    trigger_source="manual" if force else "scheduler",
                    metadata={
                        "target_session_id": self.heartbeat_config.get("target_session_id", ""),
                    },
                    handler=lambda: (
                        self._run_heartbeat_execution(force=force)
                        if hasattr(self, "_run_heartbeat_execution")
                        else self._execute_heartbeat(force=force, _from_gateway=True)
                    ),
                )
                self.heartbeat_config["last_trace_id"] = result.trace_id
                self.heartbeat_config["last_gateway_status"] = result.status
                self._save_data("heartbeat")
                return result

        config = self.heartbeat_config
        content_file = config.get("content_file", "heartbeat.md")
        targets = config.get("targets", [])
        target_session_id = config.get("target_session_id")  # 追加到指定会话

        _log.info(
            f"[Heartbeat] 配置: targets={targets}, target_session_id={target_session_id}"
        )

        # 读取 heartbeat.md 内容
        content = self._load_heartbeat_content(content_file)
        if not content:
            _log.warning(f"Heartbeat content file '{content_file}' not found or empty")
            return

        _log.info(f"Executing heartbeat with content from {content_file}")

        # 追加到现有会话或创建新会话
        context_target = None
        session = None
        session_id = None

        if target_session_id and self.session_store.get_session(target_session_id):
            session_id = target_session_id
            session = self.session_store.get_session(session_id)
            context_target = f"web:{target_session_id}"
        else:
            for target in targets:
                if not isinstance(target, str):
                    continue
                if target.startswith("web:"):
                    candidate_session_id = target.split(":", 1)[1]
                    candidate_session = self.session_store.get_session(candidate_session_id)
                    if candidate_session:
                        session_id = candidate_session_id
                        session = candidate_session
                        context_target = target
                        break
                elif target.startswith("qq_private:") or target.startswith("qq_user:"):
                    qq_user_id = target.split(":", 1)[1]
                    candidate_session_id = self.sync_qq_messages(
                        user_id=qq_user_id, create_if_not_exists=True
                    )
                    candidate_session = self.session_store.get_session(candidate_session_id) if candidate_session_id else None
                    if candidate_session:
                        session_id = candidate_session_id
                        session = candidate_session
                        context_target = f"qq_private:{qq_user_id}"
                        break
                elif target.startswith("qq_group:"):
                    qq_group_id = target.split(":", 1)[1]
                    candidate_session_id = self.sync_qq_messages(
                        user_id=None, group_id=qq_group_id, create_if_not_exists=True
                    )
                    candidate_session = self.session_store.get_session(candidate_session_id) if candidate_session_id else None
                    if candidate_session:
                        session_id = candidate_session_id
                        session = candidate_session
                        context_target = f"qq_group:{qq_group_id}"
                        break

        is_appended_session = bool(session_id and session)
        heartbeat_adapter = _resolve_web_adapter(self.web_channel_adapter)

        if is_appended_session:
            # 追加到现有会话
            session_id = session_id
            session = session

            # 构建带标记的用户消息
            hb_user_message = {
                "role": "user",
                "content": f"【Heartbeat 任务】\n\n{content}",
                "timestamp": datetime.now().isoformat(),
                "sender": "system",
                "source": "heartbeat",
                "is_heartbeat": True,
                "hide_in_web": False,  # 追加到现有会话时显示
            }
            hb_user_message = _build_heartbeat_user_message(
                heartbeat_adapter, session_id, content
            )
            self.session_store.append_message(session_id, hb_user_message)
            _log.info(f"Heartbeat: 追加到会话 {session_id}")
        else:
            # 创建新的 heartbeat 会话（原有逻辑）
            session_id = f"heartbeat_{datetime.now().strftime('%Y%m%d%H%M%S')}"
            session = {
                "id": session_id,
                "name": f"Heartbeat {datetime.now().strftime('%Y-%m-%d %H:%M')}",
                "type": "heartbeat",
                "user_id": "heartbeat",
                "created_at": datetime.now().isoformat(),
                "messages": [
                    {
                        "role": "system",
                        "content": "你是一个智能助手，请根据以下任务描述执行相关操作。",
                    },
                    {"role": "user", "content": f"【Heartbeat 任务】\n\n{content}"},
                ],
                "system_prompt": "你是一个智能助手，请根据任务描述执行相关操作。",
            }
            if heartbeat_adapter:
                session["messages"][1] = _build_heartbeat_user_message(
                    heartbeat_adapter, session_id, content
                )
            self.session_store.set_session(session_id, session)
            _log.info(f"Heartbeat: 创建新会话 {session_id}")

        # 调用 AI 处理
        try:
            heartbeat_session = self.session_store.get_session(session_id) or session or {}
            heartbeat_messages = []
            for msg in visible_messages(heartbeat_session.get("messages", []))[-12:]:
                role = msg.get("role")
                if role in ["system", "user", "assistant"]:
                    heartbeat_messages.append(
                        {
                            "role": role,
                            "content": msg.get("content", ""),
                        }
                    )

            if not heartbeat_messages:
                heartbeat_messages = [
                    {
                        "role": "system",
                        "content": heartbeat_session.get(
                            "system_prompt",
                            "你是一个智能助手，请根据任务描述执行相关操作。",
                        ),
                    },
                    {"role": "user", "content": content},
                ]

            # chat 函数是同步的，直接调用
            response_text = self._get_ai_response(heartbeat_messages)

            if response_text:
                _log.info(f"Heartbeat AI response: {response_text[:200]}...")

                # 构建带标记的 AI 回复
                hb_assistant_message = {
                    "role": "assistant",
                    "content": response_text,
                    "timestamp": datetime.now().isoformat(),
                    "sender": "AI",
                    "source": "heartbeat",
                    "is_heartbeat": True,
                    "hide_in_web": False,  # 追加到现有会话时显示
                }

                # 更新会话
                hb_assistant_message = _build_heartbeat_assistant_message(
                    heartbeat_adapter, session_id, response_text
                )
                self.session_store.append_message(session_id, hb_assistant_message)

                # 发送响应到目标（仅发送给配置的 targets）
                append_target_key = (
                    context_target
                    if is_appended_session
                    and isinstance(context_target, str)
                    and context_target.startswith("web:")
                    else None
                )
                for target in targets:
                    if append_target_key and target == append_target_key:
                        _log.info(
                            f"Skip duplicated heartbeat target {target} because it is already appended to session {target_session_id}"
                        )
                        continue
                    try:
                        await self._send_heartbeat_to_target(target, response_text)
                    except Exception as send_error:
                        _log.error(
                            f"Failed to send heartbeat to {target}: {send_error}",
                            exc_info=True,
                        )
            else:
                _log.warning("Heartbeat AI returned empty response")
        except Exception as e:
            _log.error(f"Error executing heartbeat: {e}", exc_info=True)

        # 通知前端刷新会话（如果有追加到现有会话）
        if is_appended_session and self.socketio:
            self.socketio.emit(
                "session_updated",
                {"session_id": session_id, "action": "heartbeat_completed"},
                room=session_id,
            )
            _log.info(f"Heartbeat: 已通知前端刷新会话 {session_id}")

        # 更新最后运行时间
        if (not is_appended_session) and self.socketio:
            heartbeat_session = self.session_store.get_session(session_id) or {}
            self.socketio.emit(
                "session_updated",
                {
                    "session_id": session_id,
                    "action": "heartbeat_created",
                    "session": {
                        "id": session_id,
                        "name": heartbeat_session.get("name", f"Heartbeat {session_id[-8:]}"),
                        "type": heartbeat_session.get("type", "heartbeat"),
                        "user_id": heartbeat_session.get("user_id"),
                        "created_at": heartbeat_session.get("created_at"),
                        "message_count": len(heartbeat_session.get("messages", [])),
                        "system_prompt": heartbeat_session.get("system_prompt", ""),
                    },
                },
            )
            _log.info(f"Heartbeat: 宸查€氱煡鍓嶇鏂颁細璇?{session_id}")

        self.heartbeat_config["last_run"] = datetime.now().isoformat()
        self._save_data("heartbeat")

    def _load_heartbeat_content(self, filename: str) -> str:
        """加载 heartbeat.md 文件内容"""
        # 优先从 resources 目录加载
        possible_paths = [
            os.path.join(os.path.dirname(__file__), "..", "..", "resources", filename),
            os.path.join(os.getcwd(), "resources", filename),
            os.path.join(os.path.dirname(__file__), "..", "..", filename),
            os.path.join(os.getcwd(), filename),
        ]

        for path in possible_paths:
            if os.path.exists(path):
                try:
                    with open(path, encoding="utf-8") as f:
                        return f.read().strip()
                except Exception as e:
                    _log.error(f"Failed to read heartbeat file {path}: {e}")

        return ""

    async def _send_heartbeat_to_target(self, target: str, content: str):
        """发送 heartbeat 结果到指定目标"""
        try:
            if target.startswith("qq_group:"):
                group_id = target.split(":", 1)[1]
                if self.qq_bot:
                    # 发送消息到 QQ 群
                    await self.qq_bot.api.post_group_msg(
                        group_id=group_id, text=content
                    )
                    _log.info(f"Heartbeat sent to group {group_id}")
            elif target.startswith("qq_user:") or target.startswith("qq_private:"):
                # 支持两种格式：qq_user:xxx 和 qq_private:xxx
                user_id = target.split(":", 1)[1]
                if self.qq_bot:
                    # 发送消息到 QQ 用户
                    await self.qq_bot.api.post_private_msg(
                        user_id=user_id, text=content
                    )
                    _log.info(f"Heartbeat sent to user {user_id}")
            elif target.startswith("web:"):
                # 发送到指定 Web 会话
                session_id = target.split(":", 1)[1]
                if self.socketio:
                    self.socketio.emit(
                        "new_message",
                        {
                            "session_id": session_id,
                            "content": content,
                            "role": "assistant",
                            "timestamp": datetime.now().isoformat(),
                            "sender": "AI",
                            "source": "heartbeat",
                            "is_heartbeat": True,
                        },
                        room=session_id,
                    )
                    _log.info(f"Heartbeat sent to web session {session_id}")
            elif target == "web":
                # 广播到所有 Web 客户端
                if self.socketio:
                    self.socketio.emit(
                        "heartbeat",
                        {"content": content, "timestamp": datetime.now().isoformat()},
                    )
                    _log.info("Heartbeat broadcast to all web clients")
        except Exception as e:
            _log.error(f"Failed to send heartbeat to {target}: {e}")

    def sync_qq_messages(
        self, user_id: str = None, group_id: str = None, group_user_id: str = None, create_if_not_exists: bool = True
    ):
        """同步 QQ 消息到 canonical Web 投影会话。"""
        from nbot.services.chat_service import (
            get_qq_session_id,
            group_messages,
            load_prompt,
            user_messages,
        )

        target_id = group_id or user_id
        if not target_id:
            return None

        session_type = "qq_group" if group_id else "qq_private"
        session_name = f"群 {target_id}" if group_id else f"私聊 {target_id}"
        session_id = get_qq_session_id(
            user_id=str(user_id) if user_id else None,
            group_id=str(group_id) if group_id else None,
            group_user_id=str(group_user_id) if group_user_id else None,
        )

        # 清理所有遗留的非 canonical 会话（同类型 + 同 qq_id 或同 name）
        legacy_messages = []
        legacy_ids = [
            sid for sid, s in self.sessions.items()
            if sid != session_id
            and s.get("type") == session_type
            and (
                s.get("qq_id") == target_id
                or s.get("name") == session_name
            )
        ]
        for legacy_id in legacy_ids:
            legacy_session = self.session_store.delete_session(legacy_id) or {}
            legacy_messages.extend(legacy_session.get("messages", []))
            _log.info("Cleaned up legacy QQ session: %s", legacy_id)

        session = self.session_store.get_session(session_id)
        if not session and not create_if_not_exists:
            return None

        is_agent_session = bool(session and session.get("session_mode") == "agent")
        prompt = ""
        if not is_agent_session:
            prompt = load_prompt(
                user_id=str(user_id) if user_id else None,
                group_id=str(group_id) if group_id else None,
                include_skills=False,
            )
        if not session:
            session = {
                "id": session_id,
                "name": session_name,
                "type": session_type,
                "qq_id": target_id,
                "created_at": datetime.now().isoformat(),
                "messages": [],
                "system_prompt": prompt or "",
            }
        else:
            session["id"] = session_id
            session["name"] = session_name
            session["type"] = session_type
            session["qq_id"] = target_id
            if prompt:
                session["system_prompt"] = prompt
            elif is_agent_session:
                session["system_prompt"] = ""

        rebuilt_messages = []
        if session.get("system_prompt"):
            rebuilt_messages.append(
                {"role": "system", "content": session.get("system_prompt", "")}
            )

        msg_store = group_messages if group_id else user_messages
        history_key = f"{group_id}_{group_user_id}" if group_id and group_user_id else target_id
        if history_key in msg_store:
            for msg in msg_store[history_key]:
                if msg.get("role") == "system":
                    continue
                rebuilt_messages.append(
                    {
                        "id": str(uuid.uuid4()),
                        "role": msg.get("role", "user"),
                        "content": msg.get("content", ""),
                        "timestamp": msg.get("timestamp", datetime.now().isoformat()),
                        "sender": target_id,
                        "source": "qq",
                    }
                )

        if legacy_messages:
            existing_keys = {
                (msg.get("role", ""), msg.get("content", ""), msg.get("timestamp", ""))
                for msg in rebuilt_messages
            }
            for msg in legacy_messages:
                key = (msg.get("role", ""), msg.get("content", ""), msg.get("timestamp", ""))
                if msg.get("role") == "system" or key in existing_keys:
                    continue
                rebuilt_messages.append(msg)
                existing_keys.add(key)

        session["messages"] = rebuilt_messages
        session["last_message"] = (
            rebuilt_messages[-1].get("content", "")[:100] if rebuilt_messages else ""
        )
        self.session_store.set_session(session_id, session)
        return session_id


def parse_document_with_mineru(
    file_path: str, api_key: str, file_relative_url: str = None
) -> str:
    """使用 MinerU API 解析文档（PDF、DOC、PPT等）

    Args:
        file_path: 本地文件路径
        api_key: MinerU API Key
        file_relative_url: 文件相对 URL（可选，如 /static/uploads/xxx.pdf）
    """
    import os

    import requests

    url = "https://mineru.net/api/v4/extract/task"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}

    try:
        _log.info(f"开始使用 MinerU API 解析文件: {file_path}")

        # 获取服务器地址用于生成完整 URL
        # 注意：实际部署时需要根据实际情况配置
        server_host = os.environ.get("SERVER_HOST", "http://127.0.0.1:5000")
        file_url = f"{server_host}{file_relative_url}"

        _log.info(f"文件访问 URL: {file_url}")

        data = {"url": file_url, "model_version": "vlm"}

        response = requests.post(url, headers=headers, json=data, timeout=120)

        if response.status_code == 200:
            result = response.json()
            _log.info(f"MinerU API 返回结果: {str(result)[:200]}...")

            # 提取文本内容
            if "data" in result:
                content = result["data"]
                if isinstance(content, str):
                    _log.info(f"MinerU 提取到 {len(content)} 字符内容")
                    return content
                elif isinstance(content, dict) and "content" in content:
                    _log.info(f"MinerU 提取到 {len(content['content'])} 字符内容")
                    return content["content"]
            elif "content" in result:
                content = result["content"]
                _log.info(f"MinerU 提取到 {len(content)} 字符内容")
                return content
            else:
                _log.warning(f"MinerU API 返回格式未知: {result}")
                return None
        else:
            _log.error(f"MinerU API 请求失败: {response.status_code} - {response.text}")
            return None

    except Exception as e:
        _log.error(f"MinerU API 调用失败: {e}")
        return None


def create_web_app(config: dict[str, Any] = None) -> tuple[Flask, SocketIO]:
    """创建 Flask 应用"""
    app = Flask(__name__, static_folder=None)
    app.config["SECRET_KEY"] = (
        os.getenv("NBOT_SECRET_KEY")
        or os.getenv("SECRET_KEY")
        or secrets.token_urlsafe(32)
    )
    app.config.update(config or {})

    cors_origins_env = os.getenv("NBOT_CORS_ORIGINS", "").strip()
    cors_allowed_origins = (
        [origin.strip() for origin in cors_origins_env.split(",") if origin.strip()]
        if cors_origins_env
        else None
    )

    # SocketIO 配置优化：移动端友好
    # ping_interval: 心跳间隔（秒），缩短以更快检测断开
    # ping_timeout: 心跳超时，给移动网络更多容错时间
    # upgrade: 允许从 polling 升级到 websocket
    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins=cors_allowed_origins,
        max_http_buffer_size=100 * 1024 * 1024,
        ping_interval=15,
        ping_timeout=30,
        always_connect=True,
        upgrade=True,
    )

    # 全局错误处理器：防止未捕获异常在 WSGI 层触发 "write() before start_response"
    @app.errorhandler(Exception)
    def _handle_unexpected_error(exc):
        if isinstance(exc, HTTPException):
            return exc

        import traceback as _tb
        _tb.print_exc()
        return (
            jsonify({"success": False, "error": "Internal server error"}),
            500,
        )

    server = WebChatServer(app, socketio)

    register_file_routes(app, server, WORKSPACE_AVAILABLE, workspace_manager)

    register_skills_storage_routes(app, server)

    register_voice_routes(app, server)

    register_tts_playground_routes(app, server)

    register_api_key_routes(app, server)

    return app, socketio, server

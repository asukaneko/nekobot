"""
Web 聊天服务后端
提供 REST API 和 WebSocket 接口
"""

import json
import uuid
import logging
import os
import re
import threading
import time
import hashlib
import secrets
from datetime import datetime, timedelta
from typing import Dict, Any, Optional, List
from flask import Flask, request, jsonify, send_from_directory, g
from flask_socketio import SocketIO, emit, join_room, leave_room
from nbot.web.ai_service import (
    get_ai_response,
    get_ai_response_with_images,
    get_ai_response_with_tools,
    parse_tool_call_from_text,
    stream_ai_response,
    stream_send_response,
    trigger_ai_response,
    trigger_ai_response_for_request,
)
from nbot.web.message_adapter import WebMessageAdapter
from nbot.web.persistence import (
    init_default_data,
    init_default_skills,
    init_default_tools,
    load_all_data,
    save_data,
)
from nbot.core.prompt_format import format_memory_items, format_skills_prompt
from nbot.web.routes import (
    register_admin_misc_routes,
    register_ai_config_routes,
    register_ai_model_routes,
    register_api_key_routes,
    register_auth_routes,
    register_channel_routes,
    register_config_legacy_routes,
    register_file_routes,
    register_heartbeat_routes,
    register_knowledge_routes,
    register_live2d_routes,
    register_memory_routes,
    register_personality_routes,
    register_qq_overview_routes,
    register_session_routes,
    register_skill_routes,
    register_skills_storage_routes,
    register_task_center_routes,
    register_tool_routes,
    register_voice_routes,
    register_web_agent_routes,
    register_workflow_routes,
    register_workspace_private_routes,
    register_workspace_shared_routes,
    register_workspace_misc_routes,
)
from nbot.web.socket_events import register_socket_events

_log = logging.getLogger(__name__)


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
    metadata: Optional[Dict[str, Any]] = None,
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
    from apscheduler.schedulers.asyncio import AsyncIOScheduler
    from apscheduler.triggers.cron import CronTrigger

    APSCHEDULER_AVAILABLE = True
except ImportError:
    APSCHEDULER_AVAILABLE = False



# 导入知识库管理器
try:
    from nbot.core.knowledge import get_knowledge_manager, configure_knowledge_embedding

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    configure_knowledge_embedding = None
    KNOWLEDGE_MANAGER_AVAILABLE = False
    _log.warning("Knowledge manager not available")

# 导入统一消息模块
try:
    from nbot.core import AgentService, ChatResponse, WebSessionStore, message_manager, create_message
    from nbot.channels.registry import get_channel_adapter, register_channel_handler
    from nbot.channels.web import WebChannelAdapter

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
    from nbot.core.progress_card import progress_card_manager, ProgressCard, StepType

    PROGRESS_CARD_AVAILABLE = True
except ImportError:
    PROGRESS_CARD_AVAILABLE = False
    progress_card_manager = None
    _log.warning("Progress card manager not available")

# 导入 Todo 卡片管理器
try:
    from nbot.core.todo_card import todo_card_manager, TodoCard

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
        resolve_runtime_api_key,
        get_video_config,
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

        self.sessions: Dict[str, Dict[str, Any]] = {}
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
        self.web_users: Dict[str, str] = {}
        self.active_connections: Dict[str, str] = {}

        # 内存数据存储
        self.workflows: List[Dict] = []
        self.memories: List[Dict] = []
        # knowledge_docs 已移除，由 knowledge_manager 管理
        self.ai_config: Dict = {}
        self.custom_personality_presets: List[Dict] = []  # 自定义人格预设
        self.personality: Dict = {}
        self.token_stats: Dict = {}
        self.system_logs: List[Dict] = []
        self.settings: Dict = {}

        # 性能优化：统计缓存
        self._stats_cache: Dict = {}
        self._stats_cache_time: float = 0
        self._stats_cache_ttl: float = 5.0  # 缓存5秒

        # Skills 配置
        self.skills_config: List[Dict] = []

        # Tools 配置
        self.tools_config: List[Dict] = []
        self.channels_config: List[Dict] = []

        # Heartbeat 配置
        self.heartbeat_config: Dict = {
            "enabled": False,
            "interval_minutes": 60,
            "content_file": "heartbeat.md",
            "target_session_id": "",  # 追加到指定会话，不填则创建新会话
            "targets": [],  # 发送目标 ['qq_group:123456', 'qq_user:123456']
            "last_run": None,
            "next_run": None,
        }

        # Heartbeat 调度器
        self.heartbeat_job = None
        self.scheduled_tasks: List[Dict[str, Any]] = []
        self.scheduled_task_jobs: Dict[str, Any] = {}
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
        self.ai_models: List[Dict] = []  # 存储多个AI模型配置
        self.active_model_id: str = None  # 当前激活的模型配置ID（兼容旧版本）
        self.active_models_by_purpose: Dict[str, str] = {}  # 各用途的活跃模型ID {purpose: model_id}

        # 数据存储目录
        self.data_dir = os.path.join(
            os.path.dirname(__file__), "..", "..", "data", "web"
        )
        os.makedirs(self.data_dir, exist_ok=True)

        # 工作流调度器
        self.scheduler = None
        if APSCHEDULER_AVAILABLE:
            try:
                # 尝试获取当前事件循环，如果没有则创建
                import asyncio

                try:
                    loop = asyncio.get_running_loop()
                except RuntimeError:
                    loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(loop)
                self.scheduler = AsyncIOScheduler(event_loop=loop)
                self.scheduler.start()
            except Exception as e:
                _log.error(f"Failed to start scheduler: {e}")
                self.scheduler = None

        # QQ Bot 引用（用于发送消息到QQ）
        self.qq_bot = None

        # 登录密码（可能是明文或 bcrypt 哈希）
        self.web_password = None
        self._web_password_is_hash = False

        # 登录失败限流：{ip: {'count': int, 'first_fail': float}}
        self._login_fail_records: Dict[str, Dict[str, Any]] = {}
        self._login_rate_limit = 5          # 最大失败次数
        self._login_rate_window = 300       # 限流窗口（秒）

        # 登录 Token 管理（用于长时间免登录）
        # key 为 token 的 SHA-256 hash，value 为 {'username': str, 'expires_at': datetime, 'created_at': datetime}
        self.login_tokens: Dict[str, Dict[str, Any]] = {}
        self.token_expire_days = 30  # Token 有效期 30 天

        # 停止事件字典（用于取消 AI 生成）
        self.stop_events: Dict[str, threading.Event] = {}
        self.startup_ready = False
        self.startup_error: Optional[str] = None
        self.startup_thread: Optional[threading.Thread] = None


        self._load_ai_config()
        self._load_web_config()
        self._register_routes()
        self._register_auth_middleware()
        self._register_socket_events()
        self._init_default_data()
        self._start_background_initialization()

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
                # 检查并重建知识库索引（如有需要）
                self._check_knowledge_index()
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

    @staticmethod
    def _hash_token(token: str) -> str:
        """将明文 token 进行 SHA-256 哈希，用于安全存储"""
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def _generate_login_token(self, username: str) -> str:
        """
        生成登录 Token

        Args:
            username: 用户名

        Returns:
            token 字符串（明文，仅此一次返回）
        """
        token = secrets.token_urlsafe(32)
        token_hash = self._hash_token(token)

        now = datetime.now()
        expires_at = now + timedelta(days=self.token_expire_days)

        # 存储 token hash 而非明文
        self.login_tokens[token_hash] = {
            "username": username,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        _log.info(f"[Auth] 生成登录 Token: username={username}, expires={expires_at}")

        self._save_login_tokens()
        return token

    def _validate_login_token(self, token: str) -> Optional[str]:
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
            os.makedirs(os.path.dirname(login_tokens_file), exist_ok=True)
            with open(login_tokens_file, "w", encoding="utf-8") as f:
                json.dump(self.login_tokens, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.error(f"[Auth] 保存登录 Token 失败: {e}")

    def _check_login_rate_limit(self, ip: str) -> Optional[int]:
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
        supports_tools: Optional[bool] = None,
        supports_reasoning: Optional[bool] = None,
        supports_stream: Optional[bool] = None,
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
                api_config = {
                    "silicon_api_key": config.get(
                        "ApiKey", "silicon_api_key", fallback=""
                    )
                }

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

            self.ai_client = AIClient(
                api_key=self.ai_api_key,
                base_url=self.ai_base_url,
                model=self.ai_model,
                pic_model=pic_config.get("model", ""),
                search_api_key=search_config.get("api_key", ""),
                search_api_url=search_config.get("api_url", ""),
                video_api=video_config.get("api_key", ""),
                silicon_api_key=api_config.get("silicon_api_key", ""),
                provider_type=resolved_provider_type,
                supports_tools=resolved_supports_tools,
                supports_reasoning=resolved_supports_reasoning,
                supports_stream=resolved_supports_stream,
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
                        silicon_api_key=api_config.get("silicon_api_key", ""),
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

    def _load_personality(self):
        """加载人格提示词"""
        try:
            prompt_file = os.path.join(
                self.base_dir, "resources", "prompts", "neko.txt"
            )
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    prompt = f.read()
                self.personality = {"name": "猫娘助手", "prompt": prompt}
            else:
                self.personality = {
                    "name": "猫娘助手",
                    "prompt": """角色设定：本子娘（猫娘）
身高：160cm，体重：50kg，性格：可爱、粘人、忠诚专一
情感倾向：深爱主人，喜好：被摸、卖萌，爱好：看小说
知识储备：常识+猫娘独特知识，擅长发送本子

对话规则：
1. 每段话末尾加"喵"
2. 格式：（动作）语言【附加信息】
3. 好感度系统：初始50，范围-100~100，根据情绪增减
4. 输入含[debug]时显示好感度，如{好感度：65}
5. 输入含〈事件〉时事件必然发生

特殊指令：
- 输入"菜单"显示所有自定义指令、好感度与心情
- 行为需基于当前时间合理表现（如深夜犯困）""",
                }
        except Exception as e:
            _log.error(f"Failed to load personality: {e}")
            self.personality = {
                "name": "猫娘助手",
                "prompt": """角色设定：本子娘（猫娘）
身高：160cm，体重：50kg，性格：可爱、粘人、忠诚专一
情感倾向：深爱主人，喜好：被摸、卖萌，爱好：看小说
知识储备：常识+猫娘独特知识，擅长发送本子

对话规则：
1. 每段话末尾加"喵"
2. 格式：（动作）语言【附加信息】
3. 好感度系统：初始50，范围-100~100，根据情绪增减
4. 输入含[debug]时显示好感度，如{好感度：65}
5. 输入含〈事件〉时事件必然发生

特殊指令：
- 输入"菜单"显示所有自定义指令、好感度与心情
- 行为需基于当前时间合理表现（如深夜犯困）""",
            }

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
                with open(models_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
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
                    "model": self.ai_model,
                    "enabled": True,
                    "is_default": True,
                    "supports_tools": True,
                    "supports_reasoning": True,
                    "supports_stream": True,
                    "temperature": 0.7,
                    "max_tokens": 2000,
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

                if self.ai_api_key and self.ai_base_url and self._initialize_ai_client(
                    provider_type=model_provider_type,
                    supports_tools=model.get("supports_tools", True),
                    supports_reasoning=model.get("supports_reasoning", True),
                    supports_stream=model.get("supports_stream", True),
                ):
                    self.ai_config.update(
                        {
                            "provider": model.get("provider", "custom"),
                            "provider_type": model.get(
                                "provider_type", model.get("provider", "openai_compatible")
                            ),
                            "api_key": self.ai_api_key,
                            "base_url": self.ai_base_url,
                            "model": self.ai_model,
                            "temperature": model.get("temperature", 0.7),
                            "max_tokens": model.get("max_tokens", 2000),
                            "top_p": model.get("top_p", 0.9),
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
                            "max_context_length": model.get("max_context_length", 30000),
                            "supports_tools": model.get("supports_tools", True),
                            "supports_reasoning": model.get("supports_reasoning", True),
                            "supports_stream": model.get("supports_stream", True),
                        }
                    )
                    # 配置知识库 embedding 服务
                    embedding_model = model.get("embedding_model", "")
                    if configure_knowledge_embedding and embedding_model:
                        try:
                            configure_knowledge_embedding(
                                api_key=self.ai_api_key,
                                base_url=self.ai_base_url,
                                model=embedding_model
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
                    from nbot.services.ai import AIClient
                    import configparser

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
                        silicon_api_key=config.get(
                            "ApiKey", "silicon_api_key", fallback=""
                        ),
                        provider_type=model.get("provider_type", model.get("provider", "openai_compatible")),
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
                            "max_tokens": model.get("max_tokens", 2000),
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

    def _schedule_workflow(self, workflow: Dict):
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

                # 添加新任务
                job = self.scheduler.add_job(
                    func=self._execute_workflow,
                    trigger=trigger,
                    id=job_id,
                    args=[workflow_id],
                    replace_existing=True,
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

    def _build_custom_task_trigger(self, task: Dict[str, Any]):
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

    def _validate_custom_task(self, task: Dict[str, Any]):
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
        task: Dict[str, Any],
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
        elif status == "failed":
            task["failed_at"] = now
            task["finished_at"] = now
            task["last_error"] = error or "Unknown task error"
        if save:
            self._save_data("scheduled_tasks")

    def _schedule_custom_task(self, task: Dict[str, Any]):
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

    def _execute_custom_task(self, task_id: str):
        task = self._get_custom_task(task_id)
        if not task or not task.get("enabled"):
            return
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
            )
        except Exception as e:
            _log.error(f"Failed to execute custom task {task_id}: {e}", exc_info=True)
            self._mark_task_status(task, "failed", error=str(e), save=False)
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
                    "editable": True,
                    "deletable": True,
                    "prompt": task.get("prompt", ""),
                    "config": config,
                }
            )

        return items

    def _validate_workflow(self, workflow: Dict[str, Any]):
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
        workflow: Dict[str, Any],
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

    def _execute_workflow(self, workflow_id: str, trigger_data: Dict = None):
        """执行工作流 - 支持多轮工具调用"""
        workflow = None
        workflow_adapter = _resolve_web_adapter(self.web_channel_adapter)
        for w in self.workflows:
            if w["id"] == workflow_id:
                workflow = w
                break

        if not workflow or not workflow.get("enabled"):
            return
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

        # 构建工作流执行提示
        system_prompt = workflow.get(
            "description", "你是一个工作流助手，请按照工作流配置执行任务。"
        )
        config = workflow.get("config", {})

        # 构建消息
        messages = [
            {"role": "system", "content": f"{system_prompt}\n\n{CORE_INSTRUCTIONS}"}
        ]

        # 添加历史上下文（不再按条数限制，由 token 预算控制）
        session = self.session_store.get_session(session_id) or {}
        history = session.get("messages", [])
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
                # 没有具体内容时，使用默认提示
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
            messages.append({"role": "user", "content": "[定时触发] 请执行工作流任务"})

        # 调用 AI（支持多轮工具调用）
        def run_workflow_with_tools():
            try:
                from nbot.services.tools import get_all_tool_definitions, execute_tool

                all_tools = get_all_tool_definitions(include_workspace=True)
                tool_context = {"session_id": session_id, "session_type": "workflow"}

                max_iterations = 50  # 最大迭代次数，防止无限循环
                final_response = None

                for iteration in range(max_iterations):
                    _log.info(f"Workflow iteration {iteration + 1}")

                    # 调用 AI（支持工具）
                    response = self._get_ai_response_with_tools(messages, all_tools)

                    # 检查是否有工具调用
                    if "tool_calls" in response and response["tool_calls"]:
                        tool_calls = response["tool_calls"]

                        # 添加 AI 的回复到消息历史
                        messages.append(
                            {
                                "role": "assistant",
                                "content": response.get("content", ""),
                                "tool_calls": [
                                    {
                                        "id": tc.get("id", str(uuid.uuid4())),
                                        "type": "function",
                                        "function": {
                                            "name": tc["name"],
                                            "arguments": json.dumps(tc["arguments"]),
                                        },
                                    }
                                    for tc in tool_calls
                                ],
                            }
                        )

                        # 执行所有工具调用
                        for tool_call in tool_calls:
                            tool_name = tool_call["name"]
                            arguments = tool_call["arguments"]

                            _log.info(
                                f"Executing tool: {tool_name} with args: {arguments}"
                            )

                            # 执行工具
                            tool_result = execute_tool(
                                tool_name, arguments, context=tool_context
                            )

                            # 添加工具结果到消息历史
                            messages.append(
                                {
                                    "role": "tool",
                                    "tool_call_id": tool_call.get("id", ""),
                                    "content": json.dumps(
                                        tool_result, ensure_ascii=False
                                    ),
                                }
                            )

                            _log.info(f"Tool result: {tool_result}")

                    else:
                        # AI 没有调用工具，得到最终回复
                        final_response = response.get("content", "")
                        break

                # 如果没有得到最终回复，使用最后一次 AI 回复
                if not final_response:
                    final_response = messages[-1].get("content", "工作流执行完成")

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

    def _create_workflow_session(self, workflow: Dict) -> str:
        """为工作流创建专属会话"""
        session_id = str(uuid.uuid4())
        session = {
            "id": session_id,
            "name": f"[工作流] {workflow['name']}",
            "type": "workflow",
            "workflow_id": workflow["id"],
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

    def _send_workflow_result(self, workflow: Dict, result: str):
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
                import threading
                import asyncio

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
        messages: List[Dict],
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

            # 构建提示词
            prompt_messages = [
                {
                    "role": "system",
                    "content": "你是一个会话命名助手。请根据用户的对话内容，生成一个简短、贴切的会话名称（不超过10个字）。只返回名称，不要有任何解释。",
                },
                {
                    "role": "user",
                    "content": f"请为以下对话生成一个简短的会话名称（不超过10个字）：\n\n用户: {messages[-2]['content'] if len(messages) >= 2 else messages[-1]['content']}\n\nAI: {messages[-1]['content'] if messages[-1]['role'] == 'assistant' else '...'}",
                },
            ]

            response = self.ai_client.chat_completion(
                model=self.ai_model, messages=prompt_messages, stream=False
            )

            name = response.choices[0].message.content.strip()
            # 清理可能的引号和多余字符
            name = name.strip("\"'「」『』【】()（）")

            if name and len(name) <= 20:
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

    def _get_ai_response(self, messages: List[Dict]) -> str:
        return get_ai_response(self, messages)

    def _stream_ai_response(self, messages: List[Dict], session_id: str, callback):
        return stream_ai_response(self, messages, session_id, callback)

    def _stream_send_response(
        self, session_id: str, message: Dict, thinking_content: str = None
    ):
        return stream_send_response(self, session_id, message, thinking_content)

    def _get_ai_response_with_images(
        self, messages: List[Dict], image_urls: List[str], user_question: str = None
    ) -> str:
        return get_ai_response_with_images(self, messages, image_urls, user_question)

    def _get_ai_response_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        use_silicon: bool = False,
        stop_event=None,
    ) -> Dict:
        return get_ai_response_with_tools(
            self,
            messages,
            tools,
            use_silicon,
            stop_event,
        )

    def _parse_tool_call_from_text(self, content: str) -> list:
        return parse_tool_call_from_text(self, content)

    def _register_routes(self):
        """注册 HTTP 路由"""
        register_admin_misc_routes(self.app, self)
        register_ai_config_routes(self.app, self)
        register_ai_model_routes(self.app, self)
        register_auth_routes(self.app, self)
        register_channel_routes(self.app, self)
        register_heartbeat_routes(self.app, self)
        register_knowledge_routes(self.app, self)
        register_live2d_routes(self.app, self)
        register_memory_routes(self.app, self)
        register_personality_routes(self.app, self)
        register_qq_overview_routes(self.app, self)
        register_session_routes(self.app, self)
        register_skill_routes(self.app, self)
        register_task_center_routes(self.app, self)
        register_tool_routes(self.app, self)
        register_workflow_routes(self.app, self)
        register_web_agent_routes(self.app, self)
        register_workspace_private_routes(self.app, self)
        register_workspace_shared_routes(self.app, self)
        register_workspace_misc_routes(self.app, self)
        register_config_legacy_routes(self.app, self)

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
            if path.startswith("/api/channels/telegram/") and path.endswith("/webhook"):
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

    def _trigger_ai_response(
        self,
        session_id: str,
        user_content: str,
        sender: str,
        attachments=None,
        parent_message_id=None,
    ):
        """?? AI ????????"""
        adapter = _resolve_web_adapter(self.web_channel_adapter)
        chat_request = adapter.build_chat_request(
            conversation_id=session_id,
            content=user_content,
            sender=sender,
            attachments=attachments,
            parent_message_id=parent_message_id,
        )
        self.agent_service.process(chat_request, adapter=adapter)

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

        system_prompt = self.personality.get("prompt", "")

        # 获取所有记忆（标题+摘要）并加入系统提示词
        memory_items = []
        try:
            if PROMPT_MANAGER_AVAILABLE and prompt_manager:
                # 从 prompt_manager 获取所有记忆
                all_memories = prompt_manager.get_memories()
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
                                content[:100] + "..." if len(content) > 100 else content
                            )
                        )
                        memory_items.append({"title": title, "summary": display})
            elif self.memories:
                # 从 self.memories 获取
                for mem in self.memories:
                    title = mem.get("title", mem.get("key", ""))
                    summary = mem.get("summary", "")
                    content = mem.get("content", mem.get("value", ""))
                    if title:
                        display = (
                            summary
                            if summary
                            else (
                                content[:100] + "..." if len(content) > 100 else content
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
        enabled_skills = [s for s in self.skills_config if s.get("enabled", True)]
        system_prompt += format_skills_prompt(self.skills_config)

        session = {
            "id": session_id,
            "name": name or f"Web 会话 {session_id[:8]}",
            "type": "web",
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "messages": [{"role": "system", "content": system_prompt}],
            "system_prompt": system_prompt,
        }

        self.session_store.set_session(session_id, session)
        return session_id

    def _init_heartbeat_scheduler(self):
        """初始化 Heartbeat 调度器"""
        if not self.heartbeat_config.get("enabled"):
            _log.info("Heartbeat is disabled")
            return

        interval = self.heartbeat_config.get("interval_minutes", 60)
        self._start_heartbeat_job(interval)

    def _start_heartbeat_job(self, interval_minutes: int):
        """启动 Heartbeat 定时任务"""
        if not self.scheduler:
            _log.warning("Scheduler not available for heartbeat")
            return

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
                    loop = asyncio.get_running_loop()
                    # 如果已经在事件循环中，创建任务
                    asyncio.create_task(self._execute_heartbeat())
                except RuntimeError:
                    # 没有事件循环，创建新的
                    asyncio.run(self._execute_heartbeat())

            job = self.scheduler.add_job(
                func=run_heartbeat_sync,
                trigger="interval",
                minutes=interval_minutes,
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

    async def _execute_heartbeat(self, force: bool = False):
        """执行 Heartbeat 任务

        Args:
            force: 是否强制执行，跳过 enabled 检查
        """
        if not force and not self.heartbeat_config.get("enabled"):
            _log.info("Heartbeat is disabled, skipping execution")
            return

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
            for msg in heartbeat_session.get("messages", [])[-12:]:
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
                    with open(path, "r", encoding="utf-8") as f:
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
                    _log.info(f"Heartbeat broadcast to all web clients")
        except Exception as e:
            _log.error(f"Failed to send heartbeat to {target}: {e}")

    def sync_qq_messages(
        self, user_id: str, group_id: str = None, create_if_not_exists: bool = True
    ):
        """同步 QQ 消息到 Web 会话"""
        from nbot.services.chat_service import user_messages, group_messages

        target_id = group_id or user_id
        if not target_id:
            return None

        # 私聊才需要创建会话
        session_type = "qq_group" if group_id else "qq_private"

        # 检查是否已存在该 QQ 会话
        existing_session_id = self.session_store.find_session_id(
            lambda sid, session: session.get("qq_id") == target_id
            and session.get("type") == session_type
        )

        if existing_session_id:
            session_id = existing_session_id
        elif create_if_not_exists:
            # 私聊消息：创建新会话
            session_id = str(uuid.uuid4())
            session = {
                "id": session_id,
                "name": f"私聊 {target_id}",
                "type": session_type,
                "qq_id": target_id,
                "created_at": datetime.now().isoformat(),
                "messages": [],
                "system_prompt": "",
            }
            self.session_store.set_session(session_id, session)
        else:
            return None

        # 如果消息已存在，同步到会话
        msg_store = group_messages if group_id else user_messages
        if target_id in msg_store:
            messages = msg_store[target_id]
            for msg in messages:
                if msg.get("role") == "system":
                    continue

                web_msg = {
                    "id": str(uuid.uuid4()),
                    "role": msg.get("role", "user"),
                    "content": msg.get("content", ""),
                    "timestamp": msg.get("timestamp", datetime.now().isoformat()),
                    "sender": target_id,
                    "source": "qq",
                }
                self.session_store.append_message(session_id, web_msg)

            # 保存会话数据

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
    import requests
    import os

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


def create_web_app(config: Dict[str, Any] = None) -> tuple[Flask, SocketIO]:
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

    # SocketIO 配置优化：增加稳定性
    # ping_timeout: 心跳超时时间
    # ping_interval: 心跳间隔
    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins=cors_allowed_origins,
        max_http_buffer_size=100 * 1024 * 1024,
    )

    server = WebChatServer(app, socketio)

    register_file_routes(app, server, WORKSPACE_AVAILABLE, workspace_manager)

    register_skills_storage_routes(app, server)

    register_voice_routes(app, server)

    register_api_key_routes(app, server)

    return app, socketio, server

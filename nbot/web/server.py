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
from flask import Flask, request, jsonify, send_from_directory
from flask_socketio import SocketIO, emit, join_room, leave_room

_log = logging.getLogger(__name__)

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

# 导入 Memory 系统（已废弃，使用 prompt_manager 的 memory）
try:
    from nbot.core.memory import MemoryStore, MemoryType

    MEMORY_AVAILABLE = True
except ImportError:
    MEMORY_AVAILABLE = False
    _log.warning("Memory system not available")

# 导入知识库管理器
try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False
    _log.warning("Knowledge manager not available")

# 导入统一消息模块
try:
    from nbot.core import message_manager, create_message

    MESSAGE_MODULE_AVAILABLE = True
except ImportError:
    MESSAGE_MODULE_AVAILABLE = False
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
        get_video_config,
    )

    CONFIG_LOADER_AVAILABLE = True
except ImportError:
    CONFIG_LOADER_AVAILABLE = False
    get_api_config = None
    get_pic_config = None
    get_search_config = None
    get_video_config = None
    _log.warning("Config loader not available")


class WebMessageAdapter:
    """Web 消息适配器，用于兼容 QQ 命令处理"""

    def __init__(
        self, content: str, user_id: str, session_id: str, server: "WebChatServer"
    ):
        self.raw_message = content
        self.user_id = user_id
        self.group_id = None  # Web 端没有群聊概念
        self.session_id = session_id
        self.server = server
        self._reply_text = None
        self._reply_image = None
        self._thinking_card_id = None  # 进度卡片ID

        # Web 端用户默认拥有 admin 权限
        # 将 Web 端用户添加到 admin 列表
        try:
            from nbot.commands import admin

            if str(user_id) not in admin:
                admin.append(str(user_id))
        except ImportError:
            pass

        # 创建模拟的 QQ API 对象
        self.api = self._create_mock_api()

        # 创建模拟的 bot 对象，用于兼容直接使用 bot.api 的命令
        from types import SimpleNamespace

        self.bot = SimpleNamespace(api=self.api)

    def _load_memories_for_context(self, user_id: str) -> str:
        """加载记忆用于 AI 上下文（与 QQ 端一致）"""
        if not user_id:
            return ""

        # 优先使用 prompt_manager（如果可用）
        if PROMPT_MANAGER_AVAILABLE and prompt_manager:
            try:
                # 直接使用 prompt_manager 的 load_memories 方法（已包含标题和摘要）
                memory_text = prompt_manager.load_memories(user_id)
                if memory_text:
                    return memory_text
            except Exception as e:
                _log.error(f"Failed to load memories from prompt_manager: {e}")

        # 回退到使用 self.memories
        memories = []
        now = datetime.now()

        for mem in self.memories:
            # 检查记忆是否关联到当前用户
            mem_target = mem.get("target_id", "")
            if mem_target and mem_target != user_id:
                continue

            mem_type = mem.get("type", "long")

            # 兼容新旧格式
            title = mem.get("title", mem.get("key", ""))
            content = mem.get("content", mem.get("value", ""))
            summary = mem.get("summary", "")

            # 检查是否过期
            if mem_type == "short":
                created_at = mem.get("created_at", "")
                expire_days = mem.get("expire_days", 7)

                if created_at:
                    try:
                        created = datetime.fromisoformat(created_at)
                        diff_days = (now - created).days
                        if diff_days > expire_days:
                            continue
                    except:
                        pass

            # 新格式：包含标题、摘要、内容
            if title or content:
                display_text = (
                    summary
                    if summary
                    else (content[:100] + "..." if len(content) > 100 else content)
                )
                memories.append(f"【{title}】{display_text}")

        if memories:
            return "\n".join(["【重要记忆】"] + memories)
        return ""

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
            if not results or all(sim < 0.1 for _, sim, _ in results):
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
        """关键词匹配搜索"""
        try:
            bases = km.list_knowledge_bases()
            if not bases:
                return []

            query_lower = query.lower()
            query_words = set(re.findall(r"[\w]+", query_lower))

            all_docs = []
            for kb in bases:
                for doc_id in kb.documents:
                    doc = km.store.load_document(doc_id)
                    if doc:
                        all_docs.append((doc, doc.content))

            if not all_docs:
                return []

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

            return [
                (doc, float(score), content)
                for doc, score, content in scored[:max_docs]
            ]

        except Exception as e:
            _log.error(f"[Knowledge] 关键词搜索失败: {e}")
            return []

    def _create_mock_api(self):
        """创建模拟的 QQ API 对象，用于兼容 QQ 命令"""
        adapter = self

        class MockAPI:
            async def post_group_file(self, group_id, file=None, **kwargs):
                """模拟发送群文件"""
                if file:
                    return await adapter.send_file(file)
                return True

            async def upload_private_file(
                self, user_id, file=None, name=None, **kwargs
            ):
                """模拟发送私聊文件"""
                if file:
                    return await adapter.send_file(file, name)
                return True

            async def post_private_file(self, user_id, file=None, name=None, **kwargs):
                """模拟发送私聊文件（另一种方法名）"""
                if file:
                    file_name = name if name else os.path.basename(file)
                    return await adapter.send_file(file, file_name)
                return True

            async def post_private_msg(self, user_id, text=None, rtf=None, **kwargs):
                """模拟发送私聊消息"""
                # 处理 dice 和 rps 参数
                if kwargs.get("dice"):
                    import random

                    text = f"🎲 掷出了 {random.randint(1, 6)} 点"
                elif kwargs.get("rps"):
                    import random

                    choices = ["✊ 石头", "✌️ 剪刀", "🖐️ 布"]
                    text = f"猜拳结果: {random.choice(choices)}"
                elif rtf:
                    # 处理 MessageChain
                    content = str(rtf) if hasattr(rtf, "__str__") else str(rtf)
                    text = content
                elif not text:
                    text = ""
                return await adapter.reply(text=text)

            async def post_group_msg(self, group_id, text=None, rtf=None, **kwargs):
                """模拟发送群消息"""
                return await self.post_private_msg(None, text=text, rtf=rtf, **kwargs)

            async def set_friend_add_request(
                self, flag, approve=True, remark=None, **kwargs
            ):
                """模拟处理好友添加请求"""
                _log.info(
                    f"模拟处理好友请求: flag={flag}, approve={approve}, remark={remark}"
                )
                return True

        return MockAPI()

    async def reply(self, text: str = None, image: str = None, rtf=None):
        """模拟 QQ 消息的 reply 方法

        Args:
            text: 纯文本消息
            image: 图片 URL 或 base64
            rtf: MessageChain 对象（包含文本和图片）
        """
        # 处理 rtf (MessageChain)
        if rtf is not None:
            # 尝试从 MessageChain 提取文本和图片
            content_text = ""
            has_image = False

            # MessageChain 可能是列表或对象
            if isinstance(rtf, list):
                for item in rtf:
                    if isinstance(item, str):
                        content_text += item
                    elif hasattr(item, "text"):
                        content_text += item.text
                    elif hasattr(item, "url") or hasattr(item, "data"):
                        has_image = True
            elif hasattr(rtf, "text"):
                content_text = rtf.text
            elif hasattr(rtf, "__str__"):
                content_text = str(rtf)

            # 使用提取的文本
            if content_text:
                text = content_text

        if text:
            self._reply_text = text
            # 构建助手消息
            message = {
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": text,
                "timestamp": datetime.now().isoformat(),
                "sender": "AI",
                "source": "web",
                "session_id": self.session_id,  # 添加会话ID以便前端识别
            }
            # 保存到 session
            if self.session_id in self.server.sessions:
                self.server.sessions[self.session_id]["messages"].append(message)
                self.server._save_data("sessions")
            # 通过 WebSocket 发送回复
            self.server.socketio.emit("new_message", message, room=self.session_id)
        if image:
            self._reply_image = image

    async def send_file(self, file_path: str, file_name: str = None):
        """发送文件到 Web 端，支持下载

        支持类型：
        - 图片：png, jpg, jpeg, webp, ico, gif
        - 文本：txt, json, yaml, xml, csv
        - 文档：pdf, doc, docx, ppt, pptx
        - 媒体：mp4, mp3, wav, ogg
        """
        import base64
        import mimetypes
        import os
        import shutil

        if not os.path.exists(file_path):
            _log.error(f"文件不存在: {file_path}")
            return False

        if not file_name:
            file_name = os.path.basename(file_path)

        # 获取文件类型
        mime_type, _ = mimetypes.guess_type(file_path)
        if not mime_type:
            mime_type = "application/octet-stream"

        # 获取文件扩展名
        ext = os.path.splitext(file_path)[1].lower()

        # 读取文件内容
        try:
            file_size = os.path.getsize(file_path)
        except Exception as e:
            _log.error(f"获取文件大小失败: {file_path}, 错误: {e}")
            return False

        # 判断文件类型
        is_image = mime_type and mime_type.startswith("image/")
        is_text = mime_type and (
            mime_type.startswith("text/")
            or mime_type in ["application/json", "application/xml", "application/yaml"]
        )
        is_video = mime_type and mime_type.startswith("video/")
        is_audio = mime_type and mime_type.startswith("audio/")

        # 创建文件存储目录
        files_dir = os.path.join(self.server.static_folder, "files")
        os.makedirs(files_dir, exist_ok=True)

        # 生成唯一文件名
        import hashlib
        import time

        file_hash = hashlib.md5(f"{file_path}{time.time()}".encode()).hexdigest()[:8]
        safe_name = f"{file_hash}_{file_name}"
        dest_path = os.path.join(files_dir, safe_name)

        # 确保目标目录存在（处理子目录情况）
        os.makedirs(os.path.dirname(dest_path), exist_ok=True)

        # 复制文件到静态目录
        try:
            shutil.copy2(file_path, dest_path)
            _log.info(f"文件已复制到: {dest_path}")

            # 自动删除源文件（只删除 cache 目录下的文件，避免误删）
            original_file_path = file_path
            cache_dirs = [
                os.path.join(self.server.base_dir, "data", "cache"),
                os.path.join(self.server.base_dir, "data", "workspace"),
                os.path.join(self.server.base_dir, "rank"),
                os.path.join(self.server.base_dir, "search"),
            ]
            is_cache_file = any(
                original_file_path.startswith(cache_dir) for cache_dir in cache_dirs
            )
            if is_cache_file and os.path.exists(original_file_path):
                try:
                    os.remove(original_file_path)
                    _log.info(f"已删除源文件: {original_file_path}")
                except Exception as del_err:
                    _log.warning(f"删除源文件失败: {del_err}")
        except Exception as e:
            _log.error(f"复制文件失败: {e}")
            return False

        # 生成下载 URL
        download_url = f"/static/files/{safe_name}"

        # 构建文件消息
        file_info = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": f"[文件: {file_name}]",
            "timestamp": datetime.now().isoformat(),
            "sender": "AI",
            "source": "web",
            "session_id": self.session_id,  # 添加会话ID以便前端识别
            "file": {
                "name": file_name,
                "type": mime_type,
                "size": file_size,
                "is_image": is_image,
                "is_text": is_text,
                "is_video": is_video,
                "is_audio": is_audio,
                "extension": ext,
                "download_url": download_url,  # 下载链接
                "url": download_url,  # 供前端使用
                "safe_name": safe_name,  # 静态文件目录中的文件名
            },
        }

        # 对于图片，同时嵌入 base64 数据用于预览
        if is_image and file_size < 5 * 1024 * 1024:  # 小于 5MB 的图片嵌入 base64
            try:
                with open(file_path, "rb") as f:
                    file_data = f.read()
                b64_data = base64.b64encode(file_data).decode("utf-8")
                file_info["file"]["data"] = f"data:{mime_type};base64,{b64_data}"
                file_info["file"]["preview_url"] = file_info["file"]["data"]
            except Exception as e:
                _log.error(f"图片转base64失败: {e}")

        # 对于文本文件，读取内容用于预览
        elif is_text and file_size < 102400:  # 限制 100KB
            try:
                with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                    text_content = f.read()
                file_info["file"]["content"] = text_content[:5000]  # 限制预览长度
            except Exception as e:
                _log.warning(f"读取文本内容失败: {e}")

        # 保存到 session
        if self.session_id in self.server.sessions:
            self.server.sessions[self.session_id]["messages"].append(file_info)
            self.server._save_data("sessions")

        # 发送文件消息
        self.server.socketio.emit("new_message", file_info, room=self.session_id)
        _log.info(
            f"发送文件: {file_name} ({mime_type}, {file_size} bytes), 下载链接: {download_url}"
        )
        return True


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

        self.sessions: Dict[str, Dict[str, Any]] = {}

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

        # 系统启动时间
        self.start_time = time.time()

        # AI 客户端引用
        self.ai_client = None
        self.ai_model = None
        self.ai_api_key = None
        self.ai_base_url = None

        # 多模型配置管理
        self.ai_models: List[Dict] = []  # 存储多个AI模型配置
        self.active_model_id: str = None  # 当前激活的模型配置ID

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

        # 登录密码
        self.web_password = None

        # 登录 Token 管理（用于长时间免登录）
        # token_store: {token: {'username': str, 'expires_at': datetime, 'created_at': datetime}}
        self.login_tokens: Dict[str, Dict[str, Any]] = {}
        self.token_expire_days = 30  # Token 有效期 30 天

        # 停止事件字典（用于取消 AI 生成）
        self.stop_events: Dict[str, threading.Event] = {}

        # 初始化 Memory 存储
        self.memory_store = None
        if MEMORY_AVAILABLE:
            try:
                self.memory_store = MemoryStore()
                _log.info("Memory store initialized")
            except Exception as e:
                _log.error(f"Failed to initialize memory store: {e}")

        self._load_ai_config()
        self._load_web_config()
        self._register_routes()
        self._register_socket_events()
        self._init_default_data()
        self._load_all_data()
        self._init_workflow_scheduler()

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

    def _generate_login_token(self, username: str) -> str:
        """
        生成登录 Token

        Args:
            username: 用户名

        Returns:
            token 字符串
        """
        # 生成随机 token
        token = secrets.token_urlsafe(32)

        # 存储 token 信息
        now = datetime.now()
        expires_at = now + timedelta(days=self.token_expire_days)

        self.login_tokens[token] = {
            "username": username,
            "created_at": now.isoformat(),
            "expires_at": expires_at.isoformat(),
        }

        _log.info(f"[Auth] 生成登录 Token: username={username}, expires={expires_at}")

        # 持久化保存 Token
        self._save_login_tokens()

        return token

    def _validate_login_token(self, token: str) -> Optional[str]:
        """
        验证登录 Token

        Args:
            token: token 字符串

        Returns:
            验证成功返回用户名，失败返回 None
        """
        if not token or token not in self.login_tokens:
            return None

        token_info = self.login_tokens[token]

        # 检查是否过期
        expires_at = datetime.fromisoformat(token_info["expires_at"])
        if datetime.now() > expires_at:
            # Token 已过期，删除
            del self.login_tokens[token]
            _log.info(f"[Auth] Token 已过期: username={token_info['username']}")
            return None

        return token_info["username"]

    def _cleanup_expired_tokens(self):
        """清理过期的 Token"""
        now = datetime.now()
        expired_tokens = [
            token
            for token, info in self.login_tokens.items()
            if datetime.fromisoformat(info["expires_at"]) < now
        ]

        for token in expired_tokens:
            del self.login_tokens[token]

        if expired_tokens:
            _log.info(f"[Auth] 清理了 {len(expired_tokens)} 个过期的 Token")
            self._save_login_tokens()

    def _save_login_tokens(self):
        """保存登录 Token 到文件"""
        try:
            login_tokens_file = os.path.join(self.data_dir, "login_tokens.json")
            os.makedirs(os.path.dirname(login_tokens_file), exist_ok=True)
            with open(login_tokens_file, "w", encoding="utf-8") as f:
                json.dump(self.login_tokens, f, ensure_ascii=False, indent=2)
        except Exception as e:
            _log.error(f"[Auth] 保存登录 Token 失败: {e}")

    def _load_ai_config(self):
        """从配置文件加载 AI 配置（支持 .env 环境变量）"""
        try:
            if CONFIG_LOADER_AVAILABLE and get_api_config:
                api_config = get_api_config()
                self.ai_api_key = api_config.get("api_key", "")
                self.ai_base_url = api_config.get("base_url", "")
                self.ai_model = api_config.get("model", "MiniMax-M2.7")

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

            # 读取登录密码
            self.web_password = config.get("web", "password", fallback=None)
            if self.web_password:
                _log.info("Web login password is set")
        except Exception as e:
            _log.error(f"Failed to load web config: {e}")

    def _retrieve_knowledge(self, query: str, max_docs: int = 3) -> str:
        """
        从知识库中检索相关内容（使用 knowledge_manager 向量检索）

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

            results = km.search(query, base_id=None, top_k=max_docs)

            if not results:
                return ""

            knowledge_parts = ["【知识库参考】"]
            seen_titles = set()

            for doc, similarity, chunk_content in results:
                if similarity < 0.1:
                    continue
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

    def _init_default_data(self):
        """初始化默认数据"""
        # 默认工作流
        self.workflows = [
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
        self._load_personality()

        # 默认 AI 配置
        self.ai_config = {
            "provider": "openai",
            "api_key": "",
            "base_url": "",
            "model": self.ai_model or "gpt-4",
            "temperature": 0.7,
            "max_tokens": 2000,
            "top_p": 0.9,
        }

        # 默认 Token 统计
        self.token_stats = {
            "today": 0,
            "month": 0,
            "avg_per_chat": 0,
            "estimated_cost": "0.00",
            "history": [],
            "sessions": {},
        }

        # 默认系统日志
        self.system_logs = [
            {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": "info",
                "message": "Web server started",
            }
        ]

        # 默认设置
        self.settings = {
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

    def _init_default_skills(self):
        """初始化默认 Skills 配置（支持HTTP请求模板）"""
        self.skills_config = [
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
        self._save_data("skills")

    def _init_default_tools(self):
        """初始化默认 Tools 配置（支持HTTP请求模板）"""
        self.tools_config = [
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
                    "api_key": "{{minimax_api_key}}",
                    "model": "{{minimax_model}}",
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
                    "api_key": "{{minimax_api_key}}",
                    "model": "{{minimax_model}}",
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
                "description": "执行命令行命令。白名单内的命令（如ls, cat, echo, git, python等）会直接执行，不在白名单中的命令需要用户确认。危险命令会被自动阻止。",
                "enabled": True,
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
        self._save_data("tools")

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
        """加载所有持久化数据"""
        try:
            # 加载会话
            sessions_file = os.path.join(self.data_dir, "sessions.json")
            if os.path.exists(sessions_file):
                with open(sessions_file, "r", encoding="utf-8") as f:
                    self.sessions = json.load(f)
                # 重新设置 sessions 到 ProgressCardManager
                if PROGRESS_CARD_AVAILABLE and progress_card_manager:
                    progress_card_manager.set_sessions(self.sessions)
                    _log.info("[ProgressCard] 重新设置 sessions 到 ProgressCardManager")

                # 重新设置 sessions 到 TodoCardManager
                if TODO_CARD_AVAILABLE and todo_card_manager:
                    todo_card_manager.set_sessions(self.sessions)
                    _log.info("[TodoCard] 重新设置 sessions 到 TodoCardManager")

            # 加载工作流
            workflows_file = os.path.join(self.data_dir, "workflows.json")
            if os.path.exists(workflows_file):
                with open(workflows_file, "r", encoding="utf-8") as f:
                    self.workflows = json.load(f)

            # 加载记忆
            memories_file = os.path.join(self.data_dir, "memories.json")
            if os.path.exists(memories_file):
                with open(memories_file, "r", encoding="utf-8") as f:
                    self.memories = json.load(f)

            # 如果 prompt_manager 可用，同步记忆数据
            if PROMPT_MANAGER_AVAILABLE and prompt_manager:
                try:
                    # prompt_manager 会自动从自己的文件加载，这里确保 self.memories 也同步
                    prompt_memories = prompt_manager.get_memories()
                    if prompt_memories and not self.memories:
                        # 如果 prompt_manager 有数据但 self.memories 为空，使用 prompt_manager 的数据
                        self.memories = prompt_memories
                    elif self.memories and not prompt_memories:
                        # 如果 self.memories 有数据但 prompt_manager 为空，同步到 prompt_manager
                        for mem in self.memories:
                            # 兼容旧格式数据
                            title = mem.get("title", mem.get("key", ""))
                            content = mem.get("content", mem.get("value", ""))
                            summary = mem.get("summary", None)
                            prompt_manager.add_memory(
                                title,
                                content,
                                mem.get("target_id", ""),
                                summary,
                                mem.get("type", "long"),
                                mem.get("expire_days", 7),
                            )
                except Exception as e:
                    _log.error(f"Failed to sync memories with prompt_manager: {e}")

            # 知识库现在由 knowledge_manager 管理，不再从旧文件加载
            # if os.path.exists(knowledge_file):
            #     with open(knowledge_file, 'r', encoding='utf-8') as f:
            #         self.knowledge_docs = json.load(f)

            # 加载 AI 配置
            ai_config_file = os.path.join(self.data_dir, "ai_config.json")
            if os.path.exists(ai_config_file):
                with open(ai_config_file, "r", encoding="utf-8") as f:
                    saved_config = json.load(f)
                    self.ai_config.update(saved_config)

            # 加载 Token 统计
            token_stats_file = os.path.join(self.data_dir, "token_stats.json")
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
                    self.token_stats = saved_stats

            # 加载设置
            settings_file = os.path.join(self.data_dir, "settings.json")
            if os.path.exists(settings_file):
                with open(settings_file, "r", encoding="utf-8") as f:
                    saved_settings = json.load(f)
                    self.settings.update(saved_settings)

            # 加载登录 Token（持久化存储，重启后仍然有效）
            login_tokens_file = os.path.join(self.data_dir, "login_tokens.json")
            if os.path.exists(login_tokens_file):
                try:
                    with open(login_tokens_file, "r", encoding="utf-8") as f:
                        self.login_tokens = json.load(f)
                    # 清理已过期的 token
                    self._cleanup_expired_tokens()
                    _log.info(f"[Auth] 已加载 {len(self.login_tokens)} 个登录 Token")
                except Exception as e:
                    _log.warning(f"[Auth] 加载登录 Token 失败: {e}")
                    self.login_tokens = {}

            # 加载 Heartbeat 配置
            heartbeat_file = os.path.join(self.data_dir, "heartbeat.json")
            if os.path.exists(heartbeat_file):
                with open(heartbeat_file, "r", encoding="utf-8") as f:
                    saved_heartbeat = json.load(f)
                    self.heartbeat_config.update(saved_heartbeat)

            # 加载多模型配置
            self._load_ai_models()

            # 加载 Skills 配置
            skills_file = os.path.join(self.data_dir, "skills.json")
            if os.path.exists(skills_file):
                with open(skills_file, "r", encoding="utf-8") as f:
                    self.skills_config = json.load(f)
            else:
                # 初始化默认 skills 配置
                self._init_default_skills()

            # 加载 Tools 配置
            tools_file = os.path.join(self.data_dir, "tools.json")
            if os.path.exists(tools_file):
                with open(tools_file, "r", encoding="utf-8") as f:
                    self.tools_config = json.load(f)
            else:
                # 初始化默认 tools 配置
                self._init_default_tools()

            # 加载自定义人格预设
            custom_presets_file = os.path.join(
                self.data_dir, "custom_personality_presets.json"
            )
            if os.path.exists(custom_presets_file):
                with open(custom_presets_file, "r", encoding="utf-8") as f:
                    self.custom_personality_presets = json.load(f)

            # 加载系统日志
            logs_file = os.path.join(self.data_dir, "system_logs.json")
            if os.path.exists(logs_file):
                with open(logs_file, "r", encoding="utf-8") as f:
                    self.system_logs = json.load(f)
            else:
                # 初始化默认日志
                self.system_logs = [
                    {
                        "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                        "level": "info",
                        "message": "Web server started",
                    }
                ]
                self._save_data("logs")

            # 启动 Heartbeat 调度器
            self._init_heartbeat_scheduler()

        except Exception as e:
            _log.error(f"Failed to load data: {e}")

    def _invalidate_sessions_cache(self):
        """清理会话列表缓存，避免新建/删除后短时间内读到旧快照。"""
        self._sessions_cache = []
        self._sessions_cache_time = 0

    def _save_data(self, data_type: str):
        """保存指定类型的数据到磁盘"""
        try:
            if data_type == "sessions":
                with open(
                    os.path.join(self.data_dir, "sessions.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.sessions, f, ensure_ascii=False, indent=2)
                self._invalidate_sessions_cache()
            elif data_type == "workflows":
                with open(
                    os.path.join(self.data_dir, "workflows.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.workflows, f, ensure_ascii=False, indent=2)
            elif data_type == "memories":
                with open(
                    os.path.join(self.data_dir, "memories.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.memories, f, ensure_ascii=False, indent=2)
            elif data_type == "knowledge":
                pass  # 知识库由 knowledge_manager 管理，无需保存
            elif data_type == "ai_config":
                with open(
                    os.path.join(self.data_dir, "ai_config.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.ai_config, f, ensure_ascii=False, indent=2)
            elif data_type == "token_stats":
                with open(
                    os.path.join(self.data_dir, "token_stats.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(self.token_stats, f, ensure_ascii=False, indent=2)
            elif data_type == "settings":
                with open(
                    os.path.join(self.data_dir, "settings.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.settings, f, ensure_ascii=False, indent=2)
            elif data_type == "heartbeat":
                with open(
                    os.path.join(self.data_dir, "heartbeat.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.heartbeat_config, f, ensure_ascii=False, indent=2)
            elif data_type == "ai_models":
                with open(
                    os.path.join(self.data_dir, "ai_models.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(
                        {
                            "models": self.ai_models,
                            "active_model_id": self.active_model_id,
                        },
                        f,
                        ensure_ascii=False,
                        indent=2,
                    )
            elif data_type == "skills":
                with open(
                    os.path.join(self.data_dir, "skills.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.skills_config, f, ensure_ascii=False, indent=2)
            elif data_type == "tools":
                with open(
                    os.path.join(self.data_dir, "tools.json"), "w", encoding="utf-8"
                ) as f:
                    json.dump(self.tools_config, f, ensure_ascii=False, indent=2)
            elif data_type == "logs":
                with open(
                    os.path.join(self.data_dir, "system_logs.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(self.system_logs, f, ensure_ascii=False, indent=2)
            elif data_type == "custom_personality_presets":
                with open(
                    os.path.join(self.data_dir, "custom_personality_presets.json"),
                    "w",
                    encoding="utf-8",
                ) as f:
                    json.dump(
                        self.custom_personality_presets, f, ensure_ascii=False, indent=2
                    )
        except Exception as e:
            _log.error(f"Failed to save {data_type}: {e}")

    def set_qq_bot(self, bot):
        """设置 QQ Bot 引用"""
        self.qq_bot = bot

    def log_message(self, level: str, message: str):
        """记录系统日志"""
        log = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "level": level,
            "message": message,
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

            # 如果没有模型配置，从当前配置创建一个默认的
            if not self.ai_models and self.ai_api_key:
                default_model = {
                    "id": str(uuid.uuid4()),
                    "name": "默认配置",
                    "provider": "custom",
                    "api_key": self.ai_api_key,
                    "base_url": self.ai_base_url,
                    "model": self.ai_model,
                    "enabled": True,
                    "is_default": True,
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

    def _apply_ai_model(self, model_id: str) -> bool:
        """应用指定的AI模型配置"""
        try:
            model = None
            for m in self.ai_models:
                if m["id"] == model_id:
                    model = m
                    break

            if not model or not model.get("enabled", True):
                return False

            # 更新当前AI配置
            self.ai_api_key = model.get("api_key", "")
            self.ai_base_url = model.get("base_url", "")
            self.ai_model = model.get("model", "")
            self.active_model_id = model_id

            # 重新初始化AI客户端
            if self.ai_api_key and self.ai_base_url:
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
                    )

                    # 更新内存中的配置
                    self.ai_config.update(
                        {
                            "provider": model.get("provider", "custom"),
                            "api_key": self.ai_api_key,
                            "base_url": self.ai_base_url,
                            "model": self.ai_model,
                            "temperature": model.get("temperature", 0.7),
                            "max_tokens": model.get("max_tokens", 2000),
                            "top_p": model.get("top_p", 0.9),
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
                self.scheduler.add_job(
                    func=self._execute_workflow,
                    trigger=trigger,
                    id=job_id,
                    args=[workflow_id],
                    replace_existing=True,
                )
                _log.info(
                    f"Scheduled workflow '{workflow['name']}' with cron: {cron_expr}"
                )
        except Exception as e:
            _log.error(f"Failed to schedule workflow {workflow_id}: {e}")

    def _unschedule_workflow(self, workflow_id: str):
        """取消工作流的定时任务"""
        if self.scheduler:
            try:
                self.scheduler.remove_job(f"workflow_{workflow_id}")
            except:
                pass

    def _execute_workflow(self, workflow_id: str, trigger_data: Dict = None):
        """执行工作流 - 支持多轮工具调用"""
        workflow = None
        for w in self.workflows:
            if w["id"] == workflow_id:
                workflow = w
                break

        if not workflow or not workflow.get("enabled"):
            return

        _log.info(f"Executing workflow: {workflow['name']}")

        # 获取或创建工作流的专属会话
        session_id = workflow.get("session_id")
        if not session_id or session_id not in self.sessions:
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

        # 添加历史上下文（最近10条）
        session = self.sessions.get(session_id, {})
        history = session.get("messages", [])[-10:]
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
            user_message = {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": trigger_msg,
                "timestamp": datetime.now().isoformat(),
                "sender": "user",
                "workflow_id": workflow_id,
            }
            if session_id in self.sessions:
                self.sessions[session_id]["messages"].append(user_message)
                # 同时记录到新消息模块
                if MESSAGE_MODULE_AVAILABLE and message_manager:
                    message_manager.add_web_message(
                        session_id,
                        create_message(
                            "user",
                            trigger_msg,
                            sender="user",
                            source="web",
                            session_id=session_id,
                            metadata={"workflow_id": workflow_id},
                        ),
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
                assistant_message = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": final_response,
                    "timestamp": datetime.now().isoformat(),
                    "sender": "AI",
                    "workflow_id": workflow_id,
                }

                if session_id in self.sessions:
                    self.sessions[session_id]["messages"].append(assistant_message)
                    self._save_data("sessions")
                    # 同时记录到新消息模块
                    if MESSAGE_MODULE_AVAILABLE and message_manager:
                        message_manager.add_web_message(
                            session_id,
                            create_message(
                                "assistant",
                                final_response,
                                sender="AI",
                                source="web",
                                session_id=session_id,
                                metadata={"workflow_id": workflow_id},
                            ),
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

            except Exception as e:
                _log.error(f"Workflow execution error: {e}", exc_info=True)

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
        self.sessions[session_id] = session
        self._save_data("sessions")

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
                if target_id in self.sessions:
                    message = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": f"[工作流: {workflow['name']}]\n{result}",
                        "timestamp": datetime.now().isoformat(),
                        "sender": "Workflow",
                        "workflow_id": workflow["id"],
                    }
                    self.sessions[target_id]["messages"].append(message)
                    self._save_data("sessions")

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
        """获取 AI 回复"""
        if not self.ai_client:
            _log.warning("AI client not initialized")
            return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

        try:
            response = self.ai_client.chat_completion(
                model=self.ai_model, messages=messages, stream=False
            )

            # 检查 choices 是否有效
            if not response.choices or len(response.choices) == 0:
                base_resp = getattr(response, "base_resp", {}) or {}
                status_msg = base_resp.get("status_msg", "API 返回空响应")
                _log.warning(f"[AI] API 返回空 choices: {status_msg}")
                return f"AI 服务暂时不可用: {status_msg}"

            content = response.choices[0].message.content

            # 清理响应内容
            content = content.strip()
            if content.startswith("```json"):
                content = content[7:]
                if content.endswith("```"):
                    content = content[:-3]
            elif content.startswith("```"):
                content = content[3:]
                if content.endswith("```"):
                    content = content[:-3]

            return content.strip()
        except Exception as e:
            _log.error(f"AI response error: {e}", exc_info=True)
            return f"AI 服务出错: {str(e)}"

    def _stream_ai_response(self, messages: List[Dict], session_id: str, callback):
        """流式获取 AI 回复，通过回调逐段发送内容

        Args:
            messages: 消息列表
            session_id: 会话 ID
            callback: 回调函数，接收 (chunk: str) 参数
        """
        if not self.ai_client:
            _log.warning("AI client not initialized")
            callback("AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。")
            return

        try:
            # 获取流式响应
            for chunk in self.ai_client.chat_completion(
                model=self.ai_model, messages=messages, stream=True
            ):
                # 清理内容
                chunk = chunk.strip()
                if chunk:
                    callback(chunk)

        except Exception as e:
            _log.error(f"AI stream error: {e}", exc_info=True)
            callback(f"\n\nAI 服务出错: {str(e)}")

    def _stream_send_response(
        self, session_id: str, message: Dict, thinking_content: str = None
    ):
        """通过 WebSocket 发送流式响应

        Args:
            session_id: 会话 ID
            message: 消息对象（包含完整的 content）
            thinking_content: AI 思考内容
        """
        try:
            content = message.get("content", "")
            _log.info(
                f"[Stream] 开始流式发送, session={session_id[:8]}, content长度={len(content)}"
            )

            # 发送开始事件
            self.socketio.emit(
                "ai_stream_start",
                {
                    "session_id": session_id,
                    "message": message,
                    "thinking_content": thinking_content,
                },
                room=session_id,
            )
            _log.info(f"[Stream] 已发送 ai_stream_start")

            # 清理并分割内容
            content = content.strip()

            # 发送内容片段（每10个字符一段）
            chunk_size = 10
            chunk_count = 0
            for i in range(0, len(content), chunk_size):
                chunk = content[i : i + chunk_size]
                self.socketio.emit(
                    "ai_stream_chunk",
                    {
                        "session_id": session_id,
                        "message_id": message["id"],
                        "chunk": chunk,
                        "is_end": False,
                    },
                    room=session_id,
                )
                chunk_count += 1

            _log.info(f"[Stream] 已发送 {chunk_count} 个 chunk")

            # 发送结束事件
            self.socketio.emit(
                "ai_stream_end",
                {"session_id": session_id, "message_id": message["id"], "is_end": True},
                room=session_id,
            )
            _log.info(f"[Stream] 流式发送完成")

        except Exception as e:
            _log.error(f"Stream send error: {e}", exc_info=True)
            # 降级为普通发送
            self.socketio.emit(
                "ai_response",
                {"session_id": session_id, "message": message},
                room=session_id,
            )

    def _get_ai_response_with_images(
        self, messages: List[Dict], image_urls: List[str], user_question: str = None
    ) -> str:
        """获取带图片的 AI 回复（多模态）"""
        if not self.ai_client:
            _log.warning("AI client not initialized")
            return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

        try:
            # 获取图片模型
            pic_model = getattr(self.ai_client, "pic_model", None)
            if not pic_model:
                try:
                    import configparser

                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    pic_model = config.get("pic", "model", fallback="glm-4v-flash")
                except:
                    pic_model = "glm-4v-flash"

            # 获取 silicon API key
            silicon_api_key = getattr(self.ai_client, "silicon_api_key", None)
            if not silicon_api_key:
                try:
                    import configparser

                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    silicon_api_key = config.get(
                        "ApiKey", "silicon_api_key", fallback=""
                    )
                except:
                    silicon_api_key = ""

            if not silicon_api_key:
                _log.warning("Silicon API key not configured")
                return "图片处理服务未配置 Silicon API Key。"

            # 构建多模态消息
            multimodal_messages = []

            # 添加系统提示
            if messages and messages[0].get("role") == "system":
                multimodal_messages.append(messages[0])

            # 处理历史消息，只保留文本内容
            for msg in messages[1:]:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                multimodal_messages.append({"role": role, "content": content})

            # 构建用户内容（图片 + 文本）
            user_content = []
            for img_url in image_urls:
                user_content.append(
                    {"type": "image_url", "image_url": {"url": img_url}}
                )

            # 添加用户的原始问题
            if user_question:
                user_text = user_question
            else:
                user_text = "请描述这些图片的内容并回答我的问题。"
            user_content.append({"type": "text", "text": user_text})

            multimodal_messages.append({"role": "user", "content": user_content})

            # 使用 Silicon API 调用多模态模型
            import requests

            url = "https://api.siliconflow.cn/v1/chat/completions"
            headers = {
                "Authorization": f"Bearer {silicon_api_key}",
                "Content-Type": "application/json",
            }
            payload = {
                "model": pic_model,
                "messages": multimodal_messages,
                "stream": False,
            }

            response = requests.post(url, json=payload, headers=headers, timeout=120)
            response.raise_for_status()
            data = response.json()

            if not data.get("choices"):
                return "图片处理返回结果为空。"

            content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
            return content.strip() if content else "图片处理完成，但未返回内容。"

        except ImportError:
            _log.error("requests library not available")
            return "图片处理失败：缺少 requests 库。"
        except Exception as e:
            _log.error(f"AI multimodal response error: {e}", exc_info=True)
            # 回退到普通响应
            if user_question:
                temp_messages = messages.copy()
                temp_messages.append({"role": "user", "content": user_question})
                return self._get_ai_response(temp_messages)
            return f"处理图片时出错: {str(e)}"

    def _get_ai_response_with_tools(
        self,
        messages: List[Dict],
        tools: List[Dict],
        use_silicon: bool = False,
        stop_event=None,
    ) -> Dict:
        """调用 AI 并支持工具

        Args:
            messages: 消息列表
            tools: 工具定义列表
            use_silicon: 是否使用 Silicon API（默认 False，使用主 API）
            stop_event: 可选的停止事件，用于立即停止
        """
        # 如果传入了 stop_event 且已设置，立即返回
        if stop_event and stop_event.is_set():
            raise StopIteration("用户停止生成")

        def check_stop():
            if stop_event and stop_event.is_set():
                raise StopIteration("用户停止生成")

        try:
            if not self.ai_client:
                return {"content": "AI 服务未配置"}

            import requests

            # 从设置中获取超时时间，默认 120 秒
            timeout = self.settings.get("api_timeout", 120)
            max_retries = self.settings.get("api_retry_count", 3)

            # 使用适当的超时：有工具时至少 60 秒，无工具时至少 30 秒
            # 这样可以更频繁地检查停止事件，同时不会因为太短而频繁超时
            if tools:
                api_timeout = max(timeout, 60)  # 有工具调用时至少 60 秒
            else:
                api_timeout = max(timeout, 30)  # 无工具调用时至少 30 秒

            # 检查是否应该使用 Silicon API
            # 只有在明确指定 use_silicon=True 且有 Silicon API key 时才使用
            if use_silicon:
                silicon_api_key = getattr(self.ai_client, "silicon_api_key", None)
                if not silicon_api_key:
                    try:
                        import configparser

                        config = configparser.ConfigParser()
                        config.read("config.ini", encoding="utf-8")
                        silicon_api_key = config.get(
                            "ApiKey", "silicon_api_key", fallback=""
                        )
                    except:
                        silicon_api_key = ""

                if not silicon_api_key:
                    _log.info("[AI] Silicon API key 未配置，使用主 API")
                    use_silicon = False

            if use_silicon:
                # Silicon API 调用
                url = "https://api.siliconflow.cn/v1/chat/completions"
                headers = {
                    "Authorization": f"Bearer {silicon_api_key}",
                    "Content-Type": "application/json",
                }
                # Silicon 支持的工具调用模型
                model = "Qwen/Qwen2.5-72B-Instruct"

                # 检查消息总长度，必要时截断工具结果
                MAX_CONTENT_LENGTH = 12000  # 每个消息内容的最大长度
                processed_messages = []
                for msg in messages:
                    msg_copy = msg.copy()
                    if "content" in msg_copy and isinstance(msg_copy["content"], str):
                        content_len = len(msg_copy["content"])
                        if content_len > MAX_CONTENT_LENGTH:
                            # 尝试保留 JSON 的完整性
                            truncated = msg_copy["content"][:MAX_CONTENT_LENGTH]
                            # 如果看起来像 JSON，尝试找到最后一个完整的括号
                            if truncated.strip().startswith(
                                "{"
                            ) or truncated.strip().startswith("["):
                                # 找到最后一个完整的 JSON 对象/数组
                                last_brace = max(
                                    truncated.rfind("}"), truncated.rfind("]")
                                )
                                if (
                                    last_brace > MAX_CONTENT_LENGTH - 500
                                ):  # 如果最后一个括号位置还合理
                                    truncated = truncated[: last_brace + 1]
                            msg_copy["content"] = (
                                truncated
                                + f"\n... [内容过长，已截断，原始长度: {content_len} 字符]"
                            )
                    processed_messages.append(msg_copy)

                payload = {
                    "model": model,
                    "messages": processed_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }

                # 重试机制
                last_error = None
                for attempt in range(max_retries):
                    check_stop()  # 检查是否停止
                    try:
                        _log.info(
                            f"[AI] Silicon API 调用 (尝试 {attempt + 1}/{max_retries})"
                        )

                        # 使用线程来执行请求，以便能够响应停止事件
                        import threading

                        result_container = {"data": None, "error": None}

                        def make_request():
                            try:
                                resp = requests.post(
                                    url,
                                    json=payload,
                                    headers=headers,
                                    timeout=api_timeout,
                                )
                                resp.raise_for_status()
                                result_container["data"] = resp.json()
                            except Exception as e:
                                result_container["error"] = e

                        request_thread = threading.Thread(target=make_request)
                        request_thread.daemon = True
                        request_thread.start()

                        # 等待请求完成，同时检查停止事件（每0.5秒检查一次）
                        while request_thread.is_alive():
                            check_stop()  # 如果停止事件被设置，这里会抛出 StopIteration
                            request_thread.join(timeout=0.5)

                        # 检查请求结果
                        if result_container["error"]:
                            raise result_container["error"]

                        data = result_container["data"]
                        if data is None:
                            raise Exception("请求未返回数据")

                        break
                    except StopIteration:
                        # 用户停止生成，立即抛出
                        _log.info(f"[AI] 检测到停止信号，中断 Silicon API 请求")
                        raise
                    except requests.exceptions.Timeout as e:
                        last_error = e
                        _log.warning(
                            f"[AI] Silicon API 超时 (尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        if attempt < max_retries - 1:
                            time.sleep(min(2**attempt, 2))  # 限制最大等待2秒
                        continue
                    except requests.exceptions.RequestException as e:
                        last_error = e
                        _log.error(
                            f"[AI] Silicon API 错误 (尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        if attempt < max_retries - 1:
                            time.sleep(min(2**attempt, 2))
                        continue
                else:
                    # 所有重试都失败
                    raise last_error or Exception("API 调用失败")
            else:
                # 使用主 API
                url_base = (self.ai_base_url or "").rstrip("/")

                # 检查 base_url 是否已经是完整的 API 端点
                # MiniMax 的 base_url 通常已经包含完整的路径（如 /v1/text/chatcompletion_v2）
                if "completion" in url_base.lower() or url_base.endswith("/v1"):
                    # 已经是完整端点，直接使用
                    url = url_base
                else:
                    # 需要添加 /chat/completions
                    url = f"{url_base}/chat/completions"

                headers = {
                    "Authorization": f"Bearer {self.ai_api_key}",
                    "Content-Type": "application/json",
                }

                # 检查消息总长度，必要时截断工具结果
                MAX_CONTENT_LENGTH = 12000  # 每个消息内容的最大长度
                MAX_ARGUMENTS_LENGTH = 50000  # tool_calls arguments 的最大长度
                processed_messages = []
                for msg in messages:
                    msg_copy = msg.copy()
                    if "content" in msg_copy and isinstance(msg_copy["content"], str):
                        content_len = len(msg_copy["content"])
                        if content_len > MAX_CONTENT_LENGTH:
                            # 尝试保留 JSON 的完整性
                            truncated = msg_copy["content"][:MAX_CONTENT_LENGTH]
                            # 如果看起来像 JSON，尝试找到最后一个完整的括号
                            if truncated.strip().startswith(
                                "{"
                            ) or truncated.strip().startswith("["):
                                # 找到最后一个完整的 JSON 对象/数组
                                last_brace = max(
                                    truncated.rfind("}"), truncated.rfind("]")
                                )
                                if (
                                    last_brace > MAX_CONTENT_LENGTH - 500
                                ):  # 如果最后一个括号位置还合理
                                    truncated = truncated[: last_brace + 1]
                            msg_copy["content"] = (
                                truncated
                                + f"\n... [内容过长，已截断，原始长度: {content_len} 字符]"
                            )

                    # 检查 tool_calls arguments 长度
                    if "tool_calls" in msg_copy and msg_copy["tool_calls"]:
                        for tc in msg_copy["tool_calls"]:
                            if "function" in tc and "arguments" in tc["function"]:
                                args_str = tc["function"]["arguments"]
                                if (
                                    isinstance(args_str, str)
                                    and len(args_str) > MAX_ARGUMENTS_LENGTH
                                ):
                                    tc["function"]["arguments"] = (
                                        args_str[:MAX_ARGUMENTS_LENGTH]
                                        + f"\n... [参数过长已截断，原始长度: {len(args_str)}]"
                                    )
                                    _log.warning(
                                        f"[AI] 工具 {tc.get('function', {}).get('name')} 的 arguments 过长，已截断"
                                    )

                    processed_messages.append(msg_copy)

                payload = {
                    "model": self.ai_model,
                    "messages": processed_messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }

                # 记录 payload 大小
                import json

                payload_size = len(json.dumps(payload, ensure_ascii=False))
                _log.info(
                    f"[AI] 发送请求到 {url}, 模型={self.ai_model}, 工具数={len(tools) if tools else 0}, payload大小={payload_size} bytes"
                )

                if payload_size > 500000:  # 超过 500KB
                    _log.warning(
                        f"[AI] Payload 过大 ({payload_size} bytes)，可能导致 API 拒绝"
                    )

                # 重试机制
                last_error = None
                for attempt in range(max_retries):
                    check_stop()  # 检查是否停止
                    try:
                        _log.info(
                            f"[AI] 主 API 调用 (尝试 {attempt + 1}/{max_retries})"
                        )

                        # 使用线程来执行请求，以便能够响应停止事件
                        import threading

                        result_container = {"data": None, "error": None}

                        def make_request():
                            try:
                                resp = requests.post(
                                    url,
                                    json=payload,
                                    headers=headers,
                                    timeout=api_timeout,
                                )
                                resp.raise_for_status()
                                result_container["data"] = resp.json()
                            except Exception as e:
                                result_container["error"] = e

                        request_thread = threading.Thread(target=make_request)
                        request_thread.daemon = True
                        request_thread.start()

                        # 等待请求完成，同时检查停止事件（每0.5秒检查一次）
                        while request_thread.is_alive():
                            check_stop()  # 如果停止事件被设置，这里会抛出 StopIteration
                            request_thread.join(timeout=0.5)

                        # 检查请求结果
                        if result_container["error"]:
                            raise result_container["error"]

                        data = result_container["data"]
                        if data is None:
                            raise Exception("请求未返回数据")

                        break
                    except StopIteration:
                        # 用户停止生成，立即抛出
                        _log.info(f"[AI] 检测到停止信号，中断 API 请求")
                        raise
                    except requests.exceptions.Timeout as e:
                        last_error = e
                        _log.warning(
                            f"[AI] 主 API 超时 (尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        if attempt < max_retries - 1:
                            time.sleep(min(2**attempt, 2))  # 限制最大等待2秒
                        continue
                    except requests.exceptions.RequestException as e:
                        last_error = e
                        _log.error(
                            f"[AI] 主 API 错误 (尝试 {attempt + 1}/{max_retries}): {e}"
                        )
                        if attempt < max_retries - 1:
                            time.sleep(min(2**attempt, 2))
                        continue
                else:
                    raise last_error or Exception("API 调用失败")

            # 检查 choices 是否有效
            choices = data.get("choices")
            if not choices or len(choices) == 0:
                base_resp = data.get("base_resp", {})
                status_msg = base_resp.get("status_msg", "API 返回空响应")
                _log.warning(f"[AI] API 返回空 choices: {status_msg}")
                raise Exception(f"API 错误: {status_msg}")

            choice = choices[0]
            message = choice.get("message", {})
            finish_reason = choice.get("finish_reason", "")

            _log.info(
                f"[AI] API 响应: finish_reason={finish_reason}, has_tool_calls={'tool_calls' in message}"
            )
            _log.debug(f"[AI] 工具数量: {len(tools) if tools else 0}")

            # 记录完整响应用于调试
            if "tool_calls" not in message and message.get("content", ""):
                _log.warning(
                    f"[AI] 工具未生效，content 前100字符: {message.get('content', '')[:100]}"
                )

            result = {
                "content": message.get("content", ""),
                "finish_reason": finish_reason,
            }

            # 获取AI思考内容（如果API返回了的话）
            thinking_content = message.get("thinking_content", "")
            if thinking_content:
                result["thinking_content"] = thinking_content
                _log.debug(f"[AI] 收到思考内容: {len(thinking_content)} 字符")

            # 处理工具调用
            if "tool_calls" in message or finish_reason == "tool_calls":
                tool_calls = message["tool_calls"]
                result["tool_calls"] = []

                for tool_call in tool_calls:
                    function_name = tool_call.get("function", {}).get("name")
                    arguments = json.loads(
                        tool_call.get("function", {}).get("arguments", "{}")
                    )

                    result["tool_calls"].append(
                        {
                            "id": tool_call.get("id", ""),
                            "name": function_name,
                            "arguments": arguments,
                        }
                    )
            else:
                # 检查 content 中是否包含 [TOOL_CALL] 格式（备用工具调用格式）
                content = result.get("content", "")
                if "[TOOL_CALL]" in content and "[/TOOL_CALL]" in content:
                    _log.info(
                        f"[AI] 检测到 [TOOL_CALL] 格式，content 前200字符: {content[:200]}..."
                    )
                    parsed_calls = self._parse_tool_call_from_text(content)
                    _log.info(
                        f"[AI] 解析结果: {len(parsed_calls)} 个工具调用, 内容: {parsed_calls}"
                    )
                    if parsed_calls:
                        result["tool_calls"] = parsed_calls
                        # 移除原始的 [TOOL_CALL] 内容
                        import re

                        cleaned = re.sub(
                            r"\[TOOL_CALL\]\s*.*?\s*\[/TOOL_CALL\]\s*",
                            "",
                            content,
                            flags=re.DOTALL,
                        )
                        cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
                        result["content"] = cleaned.strip()
                        _log.info(f"[AI] 成功解析 {len(parsed_calls)} 个工具调用")

            return result

        except StopIteration as e:
            # 用户停止生成
            _log.info(f"[AI] 停止生成: {e}")
            raise  # 重新抛出，让调用者处理

        except Exception as e:
            _log.error(f"AI with tools error: {e}")
            # 回退到普通 AI 调用
            content = self._get_ai_response(messages)
            return {"content": content}

    def _parse_tool_call_from_text(self, content: str) -> list:
        """解析 [TOOL_CALL] 格式的工具调用

        支持的格式：
        [TOOL_CALL]
        {tool => "exec_command", args => {
        --command ls -la
        --timeout 15
        }}
        [/TOOL_CALL]
        """
        import re

        tool_calls = []

        # 首先查找所有 [TOOL_CALL]...[/TOOL_CALL] 块
        # 使用简单的标记来分割
        pattern = r"\[TOOL_CALL\](.*?)\[/TOOL_CALL\]"
        matches = re.finditer(pattern, content, re.DOTALL)

        for match in matches:
            block = match.group(1).strip()

            # 提取工具名称
            name_match = re.search(r'tool\s*=>\s*["\']([^"\']+)["\']', block)
            if not name_match:
                # 尝试另一种格式: tool_name => "xxx" 或 "tool": "xxx"
                name_match = re.search(r'tool_name\s*=>\s*["\']([^"\']+)["\']', block)
                if not name_match:
                    name_match = re.search(r'"tool":\s*"([^"]+)"', block)

            if not name_match:
                continue
            tool_name = name_match.group(1)

            # 提取参数块 { ... }
            # 找到 args => { 开始的位置
            args_start = block.find("args")
            if args_start == -1:
                continue

            # 找到第一个 {
            brace_start = block.find("{", args_start)
            if brace_start == -1:
                continue

            # 计算嵌套的括号，找到匹配的 }
            depth = 0
            args_end = brace_start
            for i in range(brace_start, len(block)):
                if block[i] == "{":
                    depth += 1
                elif block[i] == "}":
                    depth -= 1
                    if depth == 0:
                        args_end = i
                        break

            args_block = block[brace_start + 1 : args_end]

            # 解析参数（--key value 格式）
            arguments = {}
            # 将参数按行分割
            lines = args_block.strip().split("\n")
            current_key = None
            current_value_lines = []

            for line in lines:
                line = line.strip()
                if not line:
                    continue

                # 检查是否是 --key 开头
                if line.startswith("--"):
                    # 保存上一个参数
                    if current_key is not None:
                        arguments[current_key] = "\n".join(current_value_lines).strip()

                    # 解析新的 key
                    parts = line[2:].split(
                        None, 1
                    )  # 分割一次，空格前是key，后面是value
                    current_key = parts[0] if parts else None
                    current_value_lines = [parts[1]] if len(parts) > 1 else []
                elif current_key:
                    # continuation of previous value
                    current_value_lines.append(line)

            # 保存最后一个参数
            if current_key is not None:
                arguments[current_key] = "\n".join(current_value_lines).strip()

            tool_calls.append(
                {
                    "id": f"tool_call_{len(tool_calls) + 1}",
                    "name": tool_name,
                    "arguments": arguments,
                }
            )

        return tool_calls

    def _register_routes(self):
        """注册 HTTP 路由"""

        @self.app.route("/")
        def index():
            template_path = os.path.join(
                os.path.dirname(__file__), "templates", "index.html"
            )
            with open(template_path, "r", encoding="utf-8") as f:
                return f.read(), 200, {"Content-Type": "text/html"}

        # ==================== 登录 API ====================
        @self.app.route("/api/login", methods=["POST"])
        def login():
            """用户登录验证"""
            data = request.json
            username = data.get("username", "").strip()
            password = data.get("password", "").strip()

            if not username:
                return jsonify({"success": False, "message": "用户名不能为空"}), 400

            # 如果设置了密码，需要验证
            if self.web_password:
                if not password:
                    return jsonify({"success": False, "message": "请输入密码"}), 401
                if password != self.web_password:
                    return jsonify({"success": False, "message": "密码错误"}), 401

            # 生成登录 Token（用于长时间免登录）
            token = self._generate_login_token(username)

            # 登录成功
            return jsonify(
                {
                    "success": True,
                    "message": "登录成功",
                    "token": token,
                    "expires_days": self.token_expire_days,
                }
            )

        @self.app.route("/api/verify-token", methods=["POST"])
        def verify_token():
            """验证登录 Token"""
            data = request.json
            token = data.get("token", "").strip()

            if not token:
                return jsonify({"success": False, "message": "Token 不能为空"}), 400

            # 验证 Token
            username = self._validate_login_token(token)

            if username:
                # Token 有效，返回用户名
                return jsonify(
                    {"success": True, "username": username, "message": "Token 验证成功"}
                )
            else:
                return jsonify({"success": False, "message": "Token 无效或已过期"}), 401

        @self.app.route("/api/logout", methods=["POST"])
        def logout():
            """登出（删除 Token）"""
            data = request.json
            token = data.get("token", "").strip()

            # 删除 Token
            if token and token in self.login_tokens:
                del self.login_tokens[token]
                _log.info(f"[Auth] Token 已删除: {token[:8]}...")
                # 持久化保存
                self._save_login_tokens()

            return jsonify({"success": True, "message": "已登出"})

        # ==================== 会话管理 API ====================
        @self.app.route("/api/sessions")
        def get_sessions():
            # 使用缓存避免频繁组装列表
            current_time = time.time()
            if not hasattr(self, "_sessions_cache"):
                self._sessions_cache = []
                self._sessions_cache_time = 0

            # 缓存5秒
            if (
                current_time - self._sessions_cache_time
            ) < 5.0 and self._sessions_cache:
                return jsonify(self._sessions_cache)

            # 以内存中的会话为准，避免磁盘旧快照把刚创建/删除的会话覆盖回列表。
            sessions_data = dict(self.sessions)
            if not sessions_data:
                sessions_file = os.path.join(self.data_dir, "sessions.json")
                if os.path.exists(sessions_file):
                    try:
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions_data = json.load(f)
                    except:
                        sessions_data = {}

            sessions = []
            for sid, session in sessions_data.items():
                # 只返回基本信息，不返回消息内容
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

            # 更新缓存
            self._sessions_cache = sessions
            self._sessions_cache_time = current_time

            return jsonify(sessions)

        @self.app.route("/api/sessions", methods=["POST"])
        def create_session():
            data = request.json
            session_id = str(uuid.uuid4())

            # 使用人格提示词作为默认系统提示词
            system_prompt = data.get(
                "system_prompt", self.personality.get("prompt", "")
            )

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
                                    content[:100] + "..."
                                    if len(content) > 100
                                    else content
                                )
                            )
                            memory_items.append(f"【{title}】{display}")
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
            skills = self.skills_config
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

            self.sessions[session_id] = session
            self._save_data("sessions")

            # 创建对应的工作区
            if WORKSPACE_AVAILABLE:
                workspace_manager.get_or_create(
                    session_id, session.get("type", "web"), session.get("name", "")
                )

            return jsonify({"id": session_id, "session": session})

        @self.app.route("/api/sessions/<session_id>")
        def get_session(session_id):
            session = self.sessions.get(session_id)
            if not session:
                # 尝试从文件读取
                sessions_file = os.path.join(self.data_dir, "sessions.json")
                if os.path.exists(sessions_file):
                    try:
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions_data = json.load(f)
                            session = sessions_data.get(session_id)
                            if session:
                                # 检查并更新 system_prompt 中的 skills 部分
                                session = self._update_session_skills(session)
                                session["message_count"] = len(
                                    session.get("messages", [])
                                )
                                return jsonify(session)
                    except:
                        pass
                return jsonify({"error": "Session not found"}), 404

            # 检查并更新 system_prompt 中的 skills 部分
            if "## 可用技能" not in session.get("system_prompt", ""):
                skills = self.skills_config
                enabled_skills = [s for s in skills if s.get("enabled", True)]

                skills_desc = (
                    "\n\n## 可用技能 (Skills)\n你可以使用以下技能来帮助用户：\n\n"
                )
                if enabled_skills:
                    for skill in enabled_skills:
                        skills_desc += (
                            f"### {skill['name']}\n- 描述: {skill['description']}\n\n"
                        )
                else:
                    skills_desc += "（暂无可用技能）\n\n"
                skills_desc += """**使用规则：**
1. 当用户需要使用或了解某个技能时，使用 `skill_get_info` 工具获取该技能的详细信息
2. 使用 `skill_list` 工具可以列出所有可用的 Skills
"""
                session["system_prompt"] += skills_desc
                if (
                    session.get("messages")
                    and session["messages"][0].get("role") == "system"
                ):
                    session["messages"][0]["content"] = session["system_prompt"]
                self.sessions[session["id"]] = session
                self._save_data("sessions")
                _log.info(f"已为会话 {session_id[:8]} 更新技能列表")

            session["message_count"] = len(session.get("messages", []))
            return jsonify(session)

        # QQ 消息相关 API
        @self.app.route("/api/qq/users")
        def get_qq_users():
            """获取所有 QQ 私聊用户列表"""
            try:
                users = []
                qq_private_dir = os.path.join(self.base_dir, "data", "qq", "private")
                _log.info(f"QQ private dir: {qq_private_dir}")
                _log.info(f"Dir exists: {os.path.exists(qq_private_dir)}")
                if os.path.exists(qq_private_dir):
                    for filename in os.listdir(qq_private_dir):
                        if filename.endswith(".json"):
                            user_id = filename.replace(".json", "")
                            file_path = os.path.join(qq_private_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    last_msg = messages[-1] if messages else None
                                    users.append(
                                        {
                                            "user_id": user_id,
                                            "last_message": last_msg.get("content", "")[
                                                :50
                                            ]
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

        @self.app.route("/api/qq/groups")
        def get_qq_groups():
            """获取所有 QQ 群聊列表"""
            try:
                groups = []
                qq_group_dir = os.path.join(self.base_dir, "data", "qq", "group")
                _log.info(f"QQ group dir: {qq_group_dir}")
                if os.path.exists(qq_group_dir):
                    for filename in os.listdir(qq_group_dir):
                        if filename.endswith(".json"):
                            group_id = filename.replace(".json", "")
                            file_path = os.path.join(qq_group_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    last_msg = messages[-1] if messages else None
                                    groups.append(
                                        {
                                            "group_id": group_id,
                                            "last_message": last_msg.get("content", "")[
                                                :50
                                            ]
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
                    {
                        "groups": sorted(
                            groups, key=lambda x: x["last_time"], reverse=True
                        )
                    }
                )
            except Exception as e:
                _log.error(f"Error in get_qq_groups: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/workspace/download")
        def workspace_download():
            """通过路径下载工作区文件"""
            import os
            from flask import send_file

            file_path = request.args.get("path", "")
            if not file_path:
                return jsonify({"error": "缺少 path 参数"}), 400

            # 安全检查：只允许访问 workspace 目录和 static/files 目录
            file_path = os.path.abspath(file_path)
            base_dir = os.path.abspath(self.base_dir)
            allowed_dirs = [
                os.path.join(base_dir, "data", "workspace"),
                os.path.join(base_dir, "data", "web"),
                os.path.join(base_dir, "static", "files"),
            ]

            is_allowed = False
            for allowed_dir in allowed_dirs:
                if file_path.startswith(os.path.abspath(allowed_dir)):
                    is_allowed = True
                    break

            if not is_allowed:
                _log.warning(f"Workspace download blocked: {file_path} 不在允许目录内")
                return jsonify({"error": "路径不在允许范围内"}), 403

            if not os.path.exists(file_path) or not os.path.isfile(file_path):
                return jsonify({"error": "文件不存在"}), 404

            filename = os.path.basename(file_path)
            return send_file(file_path, as_attachment=True, download_name=filename)

        @self.app.route("/api/workspace/convert/pptx-to-pdf", methods=["POST"])
        def convert_pptx_to_pdf():
            """将 PPTX 文件转换为 PDF（支持缓存）

            上传 PPTX 文件，计算文件 hash 作为缓存键
            如果缓存存在直接返回，否则转换后缓存结果
            需要系统安装 LibreOffice
            """
            import subprocess
            import tempfile
            import shutil
            import sys
            import hashlib
            from flask import send_file

            # 缓存目录
            cache_dir = os.path.join(self.base_dir, "data", "workspace", "pptx_cache")
            os.makedirs(cache_dir, exist_ok=True)

            def get_file_hash(file_path):
                """计算文件 MD5 hash"""
                hash_md5 = hashlib.md5()
                with open(file_path, "rb") as f:
                    for chunk in iter(lambda: f.read(4096), b""):
                        hash_md5.update(chunk)
                return hash_md5.hexdigest()

            def get_soffice_path():
                """获取 soffice 可执行文件路径"""
                import glob

                # Windows 常见安装路径
                windows_paths = [
                    r"C:\Program Files\LibreOffice\program\soffice.exe",
                    r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
                    os.path.expanduser(
                        r"~\AppData\Local\Programs\LibreOffice\program\soffice.exe"
                    ),
                ]

                # 搜索 Program Files 下的 LibreOffice
                program_files = os.environ.get("ProgramFiles", r"C:\Program Files")
                program_files_x86 = os.environ.get(
                    "ProgramFiles(x86)", r"C:\Program Files (x86)"
                )

                for pf in [program_files, program_files_x86]:
                    pattern = os.path.join(pf, "LibreOffice*", "program", "soffice.exe")
                    matches = glob.glob(pattern)
                    if matches:
                        windows_paths.insert(0, matches[0])

                # 检查环境变量 PATH 中的 soffice
                import shutil

                for name in ["soffice", "soffice.exe"]:
                    soffice_in_path = shutil.which(name)
                    if soffice_in_path:
                        return soffice_in_path

                # 检查 Windows 常见安装路径
                if sys.platform == "win32":
                    for path in windows_paths:
                        if os.path.exists(path):
                            return path

                return "soffice"  # 默认尝试 PATH 中的 soffice

            if "file" not in request.files:
                return jsonify({"error": "没有上传文件"}), 400

            file = request.files["file"]
            if not file.filename or not file.filename.lower().endswith(".pptx"):
                return jsonify({"error": "只支持 .pptx 文件"}), 400

            try:
                # 创建临时目录
                with tempfile.TemporaryDirectory() as tmpdir:
                    input_path = os.path.join(tmpdir, "input.pptx")

                    # 保存上传的文件
                    file.save(input_path)

                    # 计算文件 hash 作为缓存键
                    file_hash = get_file_hash(input_path)
                    cached_pdf_path = os.path.join(cache_dir, f"{file_hash}.pdf")

                    # 如果缓存存在，直接返回
                    if os.path.exists(cached_pdf_path):
                        _log.info(f"[PPTX->PDF] 使用缓存: {cached_pdf_path}")
                        return send_file(cached_pdf_path, mimetype="application/pdf")

                    # 获取 soffice 路径
                    soffice_cmd = get_soffice_path()
                    _log.info(f"[PPTX->PDF] 使用 soffice: {soffice_cmd}")

                    # 尝试使用 LibreOffice 转换
                    try:
                        cmd_args = [
                            soffice_cmd,
                            "--headless",
                            "--norestore",
                            "--nofirststartwizard",
                            "--convert-to",
                            "pdf",
                            "--outdir",
                            tmpdir,
                            input_path,
                        ]
                        _log.info(f"[PPTX->PDF] 执行命令: {' '.join(cmd_args)}")

                        result = subprocess.run(
                            cmd_args, capture_output=True, text=True, timeout=120
                        )

                        _log.info(f"[PPTX->PDF] 结果: returncode={result.returncode}")

                        # LibreOffice 输出文件与输入文件同名，只是扩展名变为 .pdf
                        pdf_path = os.path.join(tmpdir, "input.pdf")

                        if result.returncode == 0 and os.path.exists(pdf_path):
                            # 保存到缓存
                            shutil.copy2(pdf_path, cached_pdf_path)
                            _log.info(f"[PPTX->PDF] 已缓存: {cached_pdf_path}")
                            return send_file(pdf_path, mimetype="application/pdf")
                        else:
                            return jsonify(
                                {
                                    "error": "PDF 转换失败",
                                    "detail": result.stderr or "LibreOffice 转换失败",
                                }
                            ), 500
                    except subprocess.TimeoutExpired:
                        _log.error("[PPTX->PDF] 转换超时 (120秒)")
                        return jsonify({"error": "转换超时 (120秒)"}), 500
                    except FileNotFoundError:
                        _log.error(f"[PPTX->PDF] 文件未找到: {soffice_cmd}")
                        return jsonify(
                            {
                                "error": "LibreOffice 未找到",
                                "detail": f"请安装 LibreOffice，当前尝试: {soffice_cmd}",
                            }
                        ), 500

            except Exception as e:
                _log.error(f"PPTX 转 PDF 失败: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/qq/messages/<qq_type>/<qq_id>")
        def get_qq_messages(qq_type, qq_id):
            """获取 QQ 消息

            Args:
                qq_type: private 或 group
                qq_id: 用户 ID 或群 ID
            """
            try:
                import os
                import json

                if qq_type == "private":
                    file_path = os.path.join(
                        self.base_dir, "data", "qq", "private", f"{qq_id}.json"
                    )
                elif qq_type == "group":
                    file_path = os.path.join(
                        self.base_dir, "data", "qq", "group", f"{qq_id}.json"
                    )
                else:
                    return jsonify({"error": "Invalid type"}), 400

                if not os.path.exists(file_path):
                    return jsonify({"messages": []})

                with open(file_path, "r", encoding="utf-8") as f:
                    messages = json.load(f)
                    # 添加 source 标记用于前端区分显示
                    for msg in messages:
                        msg["source_type"] = "qq"
                        msg["qq_type"] = qq_type
                        msg["qq_id"] = qq_id
                    return jsonify({"messages": messages})
            except Exception as e:
                _log.error(f"获取QQ消息失败: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/qq/messages/<qq_type>/<qq_id>", methods=["DELETE"])
        def delete_qq_messages(qq_type, qq_id):
            """删除 QQ 消息记录

            Args:
                qq_type: private 或 group
                qq_id: 用户 ID 或群 ID
            """
            try:
                import os

                if qq_type == "private":
                    file_path = os.path.join(
                        self.base_dir, "data", "qq", "private", f"{qq_id}.json"
                    )
                elif qq_type == "group":
                    file_path = os.path.join(
                        self.base_dir, "data", "qq", "group", f"{qq_id}.json"
                    )
                else:
                    return jsonify({"error": "Invalid type"}), 400

                if os.path.exists(file_path):
                    os.remove(file_path)
                    _log.info(f"Deleted QQ {qq_type} messages for {qq_id}")

                # 同时删除对应的 Web 会话（如果存在）
                session_id_to_delete = None
                for sid, session in self.sessions.items():
                    if (
                        session.get("qq_id") == qq_id
                        and session.get("type") == f"qq_{qq_type}"
                    ):
                        session_id_to_delete = sid
                        break

                if session_id_to_delete:
                    del self.sessions[session_id_to_delete]
                    self._save_data("sessions")
                    if WORKSPACE_AVAILABLE:
                        workspace_manager.delete_workspace(session_id_to_delete)
                    _log.info(f"Deleted associated web session {session_id_to_delete}")

                return jsonify({"success": True})
            except Exception as e:
                _log.error(f"删除QQ消息失败: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/sessions/<session_id>", methods=["PUT"])
        def update_session(session_id):
            if session_id not in self.sessions:
                return jsonify({"error": "Session not found"}), 404

            data = request.json
            session = self.sessions[session_id]
            session["name"] = data.get("name", session["name"])

            # 更新系统提示词
            new_prompt = data.get("system_prompt", session.get("system_prompt", ""))
            if new_prompt != session.get("system_prompt", ""):
                session["system_prompt"] = new_prompt
                # 更新消息列表中的系统消息
                if (
                    session["messages"]
                    and session["messages"][0].get("role") == "system"
                ):
                    session["messages"][0]["content"] = new_prompt
                else:
                    session["messages"].insert(
                        0, {"role": "system", "content": new_prompt}
                    )

            self._save_data("sessions")
            return jsonify({"success": True, "session": session})

        @self.app.route("/api/sessions/<session_id>", methods=["DELETE"])
        def delete_session(session_id):
            if session_id in self.sessions:
                del self.sessions[session_id]
                self._save_data("sessions")
                # 删除对应的工作区
                if WORKSPACE_AVAILABLE:
                    workspace_manager.delete_workspace(session_id)
                return jsonify({"success": True})
            return jsonify({"error": "Session not found"}), 404

        @self.app.route("/api/stop", methods=["POST"])
        def stop_generation():
            """停止指定会话的 AI 生成"""
            data = request.json or {}
            session_id = data.get("session_id")

            if not session_id:
                return jsonify({"error": "session_id is required"}), 400

            # 设置停止事件
            if session_id in self.stop_events:
                self.stop_events[session_id].set()
                _log.info(f"[Stop] 停止信号已发送: {session_id}")
                return jsonify({"success": True, "message": "停止信号已发送"})

            # 没有正在运行的生成
            return jsonify(
                {"success": False, "error": "No active generation for this session"}
            ), 404

        @self.app.route("/api/sessions/<session_id>/messages", methods=["GET"])
        def get_messages(session_id):
            # 优先使用内存中的会话数据（包含 thinking_cards 等实时数据）
            session = self.sessions.get(session_id)

            # 如果内存中没有，从文件读取
            if not session:
                sessions_file = os.path.join(self.data_dir, "sessions.json")
                if os.path.exists(sessions_file):
                    try:
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            sessions_data = json.load(f)
                            session = sessions_data.get(session_id)
                    except:
                        session = None

            if not session:
                return jsonify({"error": "Session not found"}), 404

            limit = request.args.get("limit", 50, type=int)
            messages = session.get("messages", [])[-limit:]
            # 不返回系统消息给前端显示
            display_messages = [m for m in messages if m.get("role") != "system"]
            return jsonify(display_messages)

        @self.app.route("/api/sessions/<session_id>/messages", methods=["POST"])
        def add_message(session_id):
            session = self.sessions.get(session_id)
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

            session["messages"].append(message)
            return jsonify(message)

        @self.app.route("/api/sessions/<session_id>/messages", methods=["DELETE"])
        def clear_messages(session_id):
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({"error": "Session not found"}), 404

            # 保留系统消息
            system_msg = None
            if session["messages"] and session["messages"][0].get("role") == "system":
                system_msg = session["messages"][0]

            session["messages"] = [system_msg] if system_msg else []

            # 保存到文件
            self._save_data("sessions")

            return jsonify({"success": True})

        @self.app.route("/api/sessions/<session_id>/compress", methods=["POST"])
        def compress_context(session_id):
            """压缩会话上下文 - 使用AI总结早期对话"""
            session = self.sessions.get(session_id)
            if not session:
                return jsonify({"error": "Session not found"}), 404

            messages = session.get("messages", [])
            if len(messages) < 10:
                return jsonify(
                    {"success": False, "error": "消息数量不足，无需压缩"}
                ), 400

            # 获取系统提示
            system_msg = None
            if messages and messages[0].get("role") == "system":
                system_msg = messages[0]

            # 保留系统消息和最近的对话
            keep_count = min(5, len(messages) - 2)  # 至少保留2条
            recent_messages = (
                messages[-keep_count:] if not system_msg else messages[-keep_count:]
            )

            # 计算需要压缩的消息
            compress_start = 1 if system_msg else 0  # 跳过系统消息
            compress_end = len(messages) - keep_count

            if compress_end <= compress_start:
                return jsonify(
                    {"success": False, "error": "没有足够的早期消息需要压缩"}
                ), 400

            messages_to_compress = messages[compress_start:compress_end]

            if not messages_to_compress:
                return jsonify({"success": False, "error": "没有消息需要压缩"}), 400

            # 构建总结提示
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
                # 检查AI客户端
                if not self.ai_client:
                    return jsonify(
                        {"success": False, "error": "AI服务不可用，请先配置AI"}
                    ), 503

                _log.info(f"[Compress] 开始压缩会话 {session_id[:8]}... 的上下文")

                # 调用AI进行总结
                response = self.ai_client.chat_completion(
                    model=self.ai_model,
                    messages=[{"role": "user", "content": summary_prompt}],
                    stream=False,
                )

                summary = response.choices[0].message.content.strip()

                # 构建新的消息列表
                if system_msg:
                    new_messages = [system_msg]
                else:
                    new_messages = []

                # 添加总结消息
                summary_msg = {
                    "id": f"summary_{int(time.time())}",
                    "role": "system",
                    "content": f"【对话总结】{summary}",
                    "timestamp": time.time(),
                }
                new_messages.append(summary_msg)

                # 添加最近的对话
                new_messages.extend(recent_messages)

                # 更新会话
                session["messages"] = new_messages

                # 保存到文件
                self._save_data("sessions")

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

        # ==================== 工作区 API ====================
        @self.app.route("/api/sessions/<session_id>/workspace/files", methods=["GET"])
        def get_workspace_files(session_id):
            """获取会话工作区的文件列表"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            # 支持 path 参数来列出子目录
            path = request.args.get("path", "")
            result = workspace_manager.list_files(session_id, path)
            return jsonify(result)

        @self.app.route("/api/sessions/<session_id>/workspace/upload", methods=["POST"])
        def upload_workspace_file(session_id):
            """上传文件到会话工作区"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            if session_id not in self.sessions:
                return jsonify({"error": "Session not found"}), 404

            if "file" not in request.files:
                return jsonify({"error": "No file provided"}), 400

            file = request.files["file"]
            if not file.filename:
                return jsonify({"error": "Empty filename"}), 400

            file_data = file.read()
            session_type = self.sessions[session_id].get("type", "web")
            result = workspace_manager.save_uploaded_file(
                session_id, file_data, file.filename, session_type
            )

            if result.get("success"):
                # 通知前端文件已上传
                self.socketio.emit(
                    "workspace_file_uploaded",
                    {
                        "session_id": session_id,
                        "filename": result["filename"],
                        "size": result["size"],
                    },
                    room=session_id,
                )

            return jsonify(result)

        @self.app.route(
            "/api/sessions/<session_id>/workspace/files/<path:filename>",
            methods=["GET"],
        )
        def download_workspace_file(session_id, filename):
            """下载工作区中的文件"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            file_path = workspace_manager.get_file_path(session_id, filename)
            if not file_path:
                return jsonify({"error": "File not found"}), 404

            directory = os.path.dirname(file_path)
            fname = os.path.basename(file_path)
            response = send_from_directory(directory, fname, as_attachment=True)
            response.headers["Access-Control-Allow-Origin"] = "*"
            response.headers["Access-Control-Expose-Headers"] = "Content-Disposition"
            return response

        @self.app.route(
            "/api/sessions/<session_id>/workspace/files/<path:filename>",
            methods=["DELETE"],
        )
        def delete_workspace_file(session_id, filename):
            """删除工作区中的文件"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503
            result = workspace_manager.delete_file(session_id, filename)
            return jsonify(result)

        @self.app.route(
            "/api/sessions/<session_id>/workspace/folders", methods=["POST"]
        )
        def create_workspace_folder(session_id):
            """在工作区创建文件夹"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            data = request.json or {}
            folder_name = data.get("name", "").strip()
            path = data.get("path", "")  # 可选，上级路径

            if not folder_name:
                return jsonify({"success": False, "error": "文件夹名称不能为空"}), 400

            # 安全检查：禁止特殊字符
            import re

            if not re.match(r"^[\w\-\. ]+$", folder_name):
                return jsonify(
                    {"success": False, "error": "文件夹名称包含非法字符"}
                ), 400

            ws_path = workspace_manager.get_workspace(session_id)
            if not ws_path:
                return jsonify({"success": False, "error": "工作区不存在"}), 404

            # 构建完整路径
            if path:
                folder_path = os.path.join(ws_path, path, folder_name)
            else:
                folder_path = os.path.join(ws_path, folder_name)

            try:
                if os.path.exists(folder_path):
                    return jsonify({"success": False, "error": "文件夹已存在"}), 400

                os.makedirs(folder_path, exist_ok=True)
                return jsonify(
                    {
                        "success": True,
                        "name": folder_name,
                        "path": path + "/" + folder_name if path else folder_name,
                    }
                )
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route(
            "/api/sessions/<session_id>/workspace/files/<path:filename>/move",
            methods=["POST"],
        )
        def move_workspace_file(session_id, filename):
            """移动工作区中的文件或文件夹到指定目录"""
            if not WORKSPACE_AVAILABLE:
                return jsonify(
                    {"success": False, "error": "Workspace not available"}
                ), 503

            data = request.json or {}
            target_path = data.get("target", "")  # 目标目录路径

            result = workspace_manager.move_file(session_id, filename, target_path)
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        # ==================== 共享工作区 API ====================
        @self.app.route("/api/workspace/shared/files", methods=["GET"])
        def get_shared_workspace_files():
            """获取共享工作区的文件列表"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            path = request.args.get("path", "")
            result = workspace_manager.list_shared_files(path)
            return jsonify(result)

        @self.app.route("/api/workspace/shared/files/<path:filename>", methods=["GET"])
        def get_shared_workspace_file(filename):
            """获取共享工作区中的文件内容"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            import os
            from urllib.parse import unquote

            filename = unquote(filename)
            file_path = os.path.join(workspace_manager.get_shared_workspace(), filename)
            if not os.path.exists(file_path):
                return jsonify({"error": "File not found"}), 404

            ext = os.path.splitext(filename.lower())[1]

            # 图片文件直接返回 URL
            image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
            if ext in image_exts:
                return jsonify(
                    {
                        "success": True,
                        "type": "image",
                        "filename": filename,
                        "url": f"/api/workspace/shared/files/{filename}",
                    }
                )

            # 其他文件使用 file_parser 解析
            from nbot.core.file_parser import FileParser

            parse_result = FileParser.parse_file(file_path, filename, max_chars=50000)

            if parse_result and parse_result.get("success"):
                return jsonify(
                    {
                        "success": True,
                        "type": parse_result.get("type", "text"),
                        "content": parse_result.get("content", ""),
                        "filename": filename,
                        "extracted_length": parse_result.get("extracted_length", 0),
                        "original_length": parse_result.get("original_length", 0),
                        "truncated": parse_result.get("truncated", False),
                    }
                )
            else:
                error_msg = (
                    parse_result.get("error", "Failed to parse file")
                    if parse_result
                    else "File not found"
                )
                return jsonify({"success": False, "error": error_msg}), 400

        @self.app.route(
            "/api/workspace/shared/files/<path:filename>", methods=["DELETE"]
        )
        def delete_shared_workspace_file(filename):
            """删除共享工作区中的文件"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            result = workspace_manager.delete_shared_file(filename)
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        @self.app.route("/api/workspace/shared/folders", methods=["POST"])
        def create_shared_workspace_folder():
            """在共享工作区创建文件夹"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            data = request.json or {}
            folder_name = data.get("name", "").strip()
            path = data.get("path", "")

            result = workspace_manager.create_shared_folder(folder_name, path)
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        @self.app.route(
            "/api/workspace/shared/files/<path:filename>/move", methods=["POST"]
        )
        def move_shared_workspace_file(filename):
            """移动共享工作区中的文件"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            data = request.json or {}
            target_path = data.get("target", "")

            result = workspace_manager.move_shared_file(filename, target_path)
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        @self.app.route(
            "/api/sessions/<session_id>/workspace/files/<path:filename>/move-to-shared",
            methods=["POST"],
        )
        def move_file_to_shared(session_id, filename):
            """将私有工作区的文件移动到共享工作区"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            data = request.json or {}
            target_path = data.get("target", "")

            result = workspace_manager.move_to_shared(session_id, filename, target_path)
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        @self.app.route(
            "/api/workspace/shared/files/<path:filename>/move-to-private",
            methods=["POST"],
        )
        def move_shared_file_to_private(filename):
            """将共享工作区的文件移动到私有工作区"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            data = request.json or {}
            session_id = data.get("session_id")
            target_path = data.get("target", "")

            if not session_id:
                return jsonify({"error": "缺少 session_id"}), 400

            result = workspace_manager.move_from_shared(
                session_id, filename, target_path
            )
            if result.get("success"):
                return jsonify(result)
            else:
                return jsonify(result), 400

        @self.app.route(
            "/api/sessions/<session_id>/workspace/files/<path:filename>/preview",
            methods=["GET"],
        )
        def preview_workspace_file(session_id, filename):
            """预览工作区中的文件内容（文本、图片等）"""
            if not WORKSPACE_AVAILABLE:
                return jsonify({"error": "Workspace not available"}), 503

            file_path = workspace_manager.get_file_path(session_id, filename)
            if not file_path:
                return jsonify({"error": "File not found"}), 404

            import os

            ext = os.path.splitext(filename.lower())[1]

            # 图片文件直接返回 URL
            image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
            if ext in image_exts:
                return jsonify(
                    {
                        "success": True,
                        "type": "image",
                        "url": f"/api/sessions/{session_id}/workspace/files/{filename}",
                    }
                )

            # 文本文件、代码文件、文档等使用 file_parser 解析
            from nbot.core.file_parser import FileParser

            parse_result = FileParser.parse_file(file_path, filename, max_chars=50000)

            if not parse_result or not parse_result.get("success"):
                return jsonify(
                    {
                        "success": False,
                        "error": parse_result.get("error", "Failed to parse file")
                        if parse_result
                        else "File not found",
                    }
                ), 400

            return jsonify(
                {
                    "success": True,
                    "type": parse_result.get("type", "text"),
                    "content": parse_result.get("content", ""),
                    "filename": filename,
                    "extracted_length": parse_result.get("extracted_length", 0),
                    "original_length": parse_result.get("original_length", 0),
                    "truncated": parse_result.get("truncated", False),
                }
            )

        @self.app.route("/api/sessions/<session_id>/chat", methods=["POST"])
        def chat_with_ai(session_id):
            """与 AI 对话"""
            _log.info(
                f"[SessionRename] chat_with_ai called for session {session_id[:8]}..."
            )
            session = self.sessions.get(session_id)
            if not session:
                _log.warning(f"[SessionRename] Session not found: {session_id}")
                return jsonify({"error": "Session not found"}), 404

            data = request.json
            user_content = data.get("content", "").strip()

            if not user_content:
                return jsonify({"error": "Content is required"}), 400

            # 添加用户消息
            user_message = {
                "id": str(uuid.uuid4()),
                "role": "user",
                "content": user_content,
                "timestamp": datetime.now().isoformat(),
                "sender": data.get("sender", "web_user"),
            }
            session["messages"].append(user_message)

            # 获取 AI 回复
            messages_for_ai = session["messages"].copy()

            # 限制消息总字符数
            MAX_TOTAL_CHARS = 30000
            MAX_MESSAGE_COUNT = 50  # 最多保留的消息数

            total_chars = sum(len(str(m.get("content", ""))) for m in messages_for_ai)
            if (
                total_chars > MAX_TOTAL_CHARS
                or len(messages_for_ai) > MAX_MESSAGE_COUNT
            ):
                # 保留系统消息，只保留最近的 MAX_MESSAGE_COUNT 条
                system_msg = (
                    messages_for_ai[0]
                    if messages_for_ai and messages_for_ai[0].get("role") == "system"
                    else None
                )
                recent_msgs = messages_for_ai[-MAX_MESSAGE_COUNT:]
                if system_msg:
                    messages_for_ai = [system_msg] + recent_msgs
                else:
                    messages_for_ai = recent_msgs
                _log.info(
                    f"[Context] 消息已压缩: {total_chars} 字符, {len(messages_for_ai)} 条消息"
                )

            # 在系统提示词后添加核心指令（确保JSON输出功能不丢失）
            if messages_for_ai and messages_for_ai[0].get("role") == "system":
                system_prompt = messages_for_ai[0].get("content", "")
                messages_for_ai[0]["content"] = (
                    f"{system_prompt}\n\n{CORE_INSTRUCTIONS}"
                )
                _log.info(
                    f"[CoreInstructions] 已添加到现有系统提示词后，长度: {len(messages_for_ai[0]['content'])}"
                )
            elif messages_for_ai and messages_for_ai[0].get("role") != "system":
                # 如果第一条不是系统消息，插入核心指令
                messages_for_ai.insert(
                    0, {"role": "system", "content": CORE_INSTRUCTIONS}
                )
                _log.info(
                    f"[CoreInstructions] 已插入到消息开头（原第一条不是系统消息）"
                )
            else:
                # 没有任何消息，添加系统消息和核心指令
                messages_for_ai.insert(
                    0, {"role": "system", "content": CORE_INSTRUCTIONS}
                )
                _log.info(f"[CoreInstructions] 已添加到空消息列表")

            # 收集需要添加到系统提示词的内容
            system_additions = []
            knowledge_retrieved = False  # 记录知识库是否检索成功

            # 检索相关记忆并添加到上下文（使用旧的记忆系统）
            if session.get("type") == "web":
                try:
                    user_id = session.get("user_id", session.get("id", ""))
                    _log.info(f"Retrieving memories for web user: {user_id}")

                    # 从旧的记忆系统加载（与QQ端一致）
                    memories_text = self._load_memories_for_context(user_id)
                    if memories_text:
                        system_additions.append(memories_text)
                        _log.info(f"Added memories to context for user {user_id}")
                except Exception as e:
                    _log.error(f"Failed to retrieve memories: {e}", exc_info=True)

            # 检索知识库相关内容并添加到上下文
            try:
                if self.settings.get("knowledge", True):  # 检查知识库开关
                    knowledge_text = self._retrieve_knowledge(user_content)
                    if knowledge_text:
                        system_additions.append(knowledge_text)
                        knowledge_retrieved = True  # 标记知识库检索成功
                        _log.info(f"Added knowledge to context")
            except Exception as e:
                _log.error(f"Failed to retrieve knowledge: {e}", exc_info=True)

            # 添加工作区文件信息到上下文
            if WORKSPACE_AVAILABLE:
                try:
                    ws_files = workspace_manager.list_files(session_id)
                    files = ws_files.get("files", [])
                    if files:
                        file_list = ", ".join([f["name"] for f in files])
                        system_additions.append(
                            f"【工作区】当前会话工作区中有以下文件: {file_list}\n"
                            "你可以使用 workspace_read_file、workspace_edit_file、workspace_create_file、"
                            "workspace_delete_file、workspace_list_files、workspace_send_file 等工具操作这些文件。"
                        )
                except Exception as e:
                    _log.error(f"Failed to get workspace info: {e}")

            # 将所有系统内容合并到第一条system消息中（确保AI能正确处理）
            if (
                system_additions
                and messages_for_ai
                and messages_for_ai[0].get("role") == "system"
            ):
                combined_content = messages_for_ai[0]["content"]
                for addition in system_additions:
                    combined_content += f"\n\n{addition}"
                messages_for_ai[0]["content"] = combined_content

            # 异步获取 AI 回复
            def get_response():
                _log.info(
                    f"[SessionRename] get_response started for session {session_id[:8]}..."
                )
                try:
                    # 导入工具定义（包括工作区工具）
                    try:
                        from nbot.services.tools import (
                            get_all_tool_definitions,
                            execute_tool,
                        )

                        use_tools = True
                        all_tools = get_all_tool_definitions(include_workspace=True)
                        available_tools = [
                            tool.get("function", {}).get("name", "unknown")
                            for tool in all_tools
                        ]
                        _log.info(
                            f"[Tools] 加载了 {len(all_tools)} 个工具: {available_tools}"
                        )
                    except ImportError as e:
                        _log.warning(f"[Tools] 工具模块不可用: {e}")
                        use_tools = False
                        all_tools = []
                        available_tools = []

                    # 构建工作区上下文
                    tool_context = {
                        "session_id": session_id,
                        "session_type": session.get("type", "web"),
                    }

                    # 调用 AI（支持工具调用）
                    if use_tools:
                        _log.info(
                            f"[Tools] 发送请求到 AI，消息数量: {len(messages_for_ai)}"
                        )
                        response = self._get_ai_response_with_tools(
                            messages_for_ai, all_tools
                        )

                        # 处理AI思考内容（如果API返回了的话）
                        if (
                            "thinking_content" in response
                            and response["thinking_content"]
                        ):
                            thinking_content = response["thinking_content"]
                            _log.debug(
                                f"[ThinkingCard] 收到AI思考内容: {len(thinking_content)} 字符"
                            )
                            # 更新思考内容到进度卡片
                            try:
                                update_thinking_stream(thinking_content)
                            except Exception as e:
                                _log.warning(f"[ThinkingCard] 更新思考内容失败: {e}")

                        # 检查是否有工具调用
                        if "tool_calls" in response and response["tool_calls"]:
                            tool_calls = response["tool_calls"]
                            _log.info(
                                f"[Tools] 🤖 AI 请求调用 {len(tool_calls)} 个工具:"
                            )
                            for i, tc in enumerate(tool_calls, 1):
                                _log.info(
                                    f"[Tools]   [{i}] 工具名: {tc.get('name', 'unknown')}"
                                )
                                _log.info(
                                    f"[Tools]   [{i}] 参数: {tc.get('arguments', {})}"
                                )

                            # 添加 AI 的初始回复
                            initial_content = response.get("content", "")
                            if initial_content:
                                _log.info(
                                    f"[Tools] 💬 AI 初始回复: {initial_content[:200]}..."
                                )

                            messages_for_ai.append(
                                {
                                    "role": "assistant",
                                    "content": initial_content,
                                    "tool_calls": [
                                        {
                                            "id": tc["id"],
                                            "type": "function",
                                            "function": {
                                                "name": tc["name"],
                                                "arguments": json.dumps(
                                                    tc["arguments"]
                                                ),
                                            },
                                        }
                                        for tc in tool_calls
                                    ],
                                }
                            )

                            # 执行工具调用
                            _log.info(
                                f"[Tools] 开始执行 {len(tool_calls)} 个工具调用..."
                            )
                            for tool_call in tool_calls:
                                tool_name = tool_call["name"]
                                tool_args = tool_call["arguments"]
                                _log.info(f"[Tools] ➜ 执行工具: {tool_name}")
                                args_str = json.dumps(tool_args, ensure_ascii=False)
                                if len(args_str) > 200:
                                    _log.info(
                                        f"[Tools] ➜ 参数详情: {args_str[:200]}... (共 {len(args_str)} 字符)"
                                    )
                                else:
                                    _log.info(f"[Tools] ➜ 参数详情: {args_str}")

                                start_time = time.time()
                                try:
                                    tool_result = execute_tool(
                                        tool_name, tool_args, context=tool_context
                                    )
                                    tool_result_str = json.dumps(
                                        tool_result, ensure_ascii=False
                                    )
                                    elapsed = (time.time() - start_time) * 1000
                                    _log.info(
                                        f"[Tools] ✓ 工具执行成功 ({elapsed:.0f}ms): {tool_name}"
                                    )
                                    _log.info(
                                        f"[Tools] ✓ 结果预览: {tool_result_str[:500]}{'...' if len(tool_result_str) > 500 else ''}"
                                    )

                                    # 更新 Todo 卡片
                                    if (
                                        tool_name.startswith("todo_")
                                        and todo_card
                                        and TODO_CARD_AVAILABLE
                                    ):
                                        try:
                                            if (
                                                tool_name == "todo_add"
                                                and tool_result.get("success")
                                            ):
                                                todo_info = tool_result.get("todo", {})
                                                todo_card.add_todo(
                                                    todo_id=todo_info.get("id"),
                                                    content=todo_info.get(
                                                        "content", ""
                                                    ),
                                                    priority=todo_info.get(
                                                        "priority", "medium"
                                                    ),
                                                )
                                            elif (
                                                tool_name == "todo_complete"
                                                and tool_result.get("success")
                                            ):
                                                todo_info = tool_result.get("todo", {})
                                                todo_card.complete_todo(
                                                    todo_info.get("id")
                                                )
                                            elif (
                                                tool_name == "todo_delete"
                                                and tool_result.get("success")
                                            ):
                                                deleted_todo = tool_result.get(
                                                    "deleted_todo", {}
                                                )
                                                todo_card.delete_todo(
                                                    deleted_todo.get("id")
                                                )
                                            elif (
                                                tool_name == "todo_list"
                                                and tool_result.get("success")
                                            ):
                                                todos = tool_result.get("todos", [])
                                                todo_card.update_todos(todos)
                                            elif (
                                                tool_name == "todo_clear"
                                                and tool_result.get("success")
                                            ):
                                                todo_card.todos = []
                                                todo_card._emit_update()
                                        except Exception as e:
                                            _log.warning(
                                                f"[TodoCard] 更新 Todo 卡片失败: {e}"
                                            )

                                    # 更新进度卡片 - 标记工具调用完成
                                    if progress_card:
                                        try:
                                            from nbot.core.progress_card import StepType

                                            # 获取工具显示名称
                                            tool_names = {
                                                "workspace_read_file": "读取文件",
                                                "workspace_create_file": "创建文件",
                                                "workspace_edit_file": "编辑文件",
                                                "workspace_delete_file": "删除文件",
                                                "workspace_list_files": "列出文件",
                                                "workspace_send_file": "发送文件",
                                                "workspace_parse_file": "解析文件",
                                                "workspace_file_info": "文件信息",
                                                "web_search": "网页搜索",
                                                "generate_image": "生成图片",
                                                "save_to_memory": "保存记忆",
                                                "todo_add": "添加待办",
                                                "todo_list": "列出待办",
                                                "todo_complete": "完成待办",
                                                "todo_delete": "删除待办",
                                                "todo_clear": "清空待办",
                                            }
                                            tool_display_name = tool_names.get(
                                                tool_name, tool_name
                                            )

                                            # 检查工具执行结果
                                            if tool_result.get("success"):
                                                progress_card.update(
                                                    StepType.TOOL_DONE,
                                                    f"{tool_display_name}完成",
                                                    True,
                                                )
                                            else:
                                                progress_card.update(
                                                    StepType.TOOL_DONE,
                                                    f"{tool_display_name}失败",
                                                    False,
                                                )
                                        except Exception as e:
                                            _log.warning(
                                                f"[ProgressCard] 更新工具完成状态失败: {e}"
                                            )

                                    # 如果是 workspace_send_file，自动发送文件到 Web 端
                                    if (
                                        tool_name == "workspace_send_file"
                                        and tool_result.get("action") == "send_file"
                                    ):
                                        file_path = tool_result.get("path", "")
                                        filename = tool_result.get("filename", "")
                                        _log.info(
                                            f"[SendFile] 准备发送文件: {filename}, path: {file_path}, session_id: {session_id}"
                                        )

                                        if (
                                            file_path
                                            and filename
                                            and session_id
                                            and session
                                        ):
                                            try:
                                                # 直接构建文件消息并发送
                                                import mimetypes
                                                import shutil

                                                # 获取文件信息
                                                if not os.path.exists(file_path):
                                                    _log.error(
                                                        f"[SendFile] 文件不存在: {file_path}"
                                                    )
                                                else:
                                                    file_size = os.path.getsize(
                                                        file_path
                                                    )
                                                    mime_type, _ = mimetypes.guess_type(
                                                        file_path
                                                    )
                                                    if not mime_type:
                                                        mime_type = (
                                                            "application/octet-stream"
                                                        )
                                                    ext = os.path.splitext(file_path)[
                                                        1
                                                    ].lower()
                                                    is_image = (
                                                        mime_type
                                                        and mime_type.startswith(
                                                            "image/"
                                                        )
                                                    )

                                                    # 复制文件到静态目录
                                                    files_dir = os.path.join(
                                                        self.static_folder, "files"
                                                    )
                                                    os.makedirs(
                                                        files_dir, exist_ok=True
                                                    )

                                                    import hashlib
                                                    import time

                                                    file_hash = hashlib.md5(
                                                        f"{file_path}{time.time()}".encode()
                                                    ).hexdigest()[:8]
                                                    safe_name = (
                                                        f"{file_hash}_{filename}"
                                                    )
                                                    dest_path = os.path.join(
                                                        files_dir, safe_name
                                                    )

                                                    # 确保目标目录存在（处理子目录情况）
                                                    os.makedirs(
                                                        os.path.dirname(dest_path),
                                                        exist_ok=True,
                                                    )

                                                    shutil.copy2(file_path, dest_path)
                                                    download_url = (
                                                        f"/static/files/{safe_name}"
                                                    )

                                                    # 构建文件消息
                                                    file_info = {
                                                        "id": str(uuid.uuid4()),
                                                        "role": "assistant",
                                                        "content": f"[文件: {filename}]",
                                                        "timestamp": datetime.now().isoformat(),
                                                        "sender": "AI",
                                                        "source": "web",
                                                        "session_id": session_id,
                                                        "file": {
                                                            "name": filename,
                                                            "type": mime_type,
                                                            "size": file_size,
                                                            "is_image": is_image,
                                                            "extension": ext,
                                                            "download_url": download_url,
                                                            "url": download_url,
                                                            "safe_name": safe_name,
                                                        },
                                                    }

                                                    # 对于图片，嵌入 base64 数据
                                                    if (
                                                        is_image
                                                        and file_size < 5 * 1024 * 1024
                                                    ):
                                                        try:
                                                            import base64

                                                            with open(
                                                                file_path, "rb"
                                                            ) as f:
                                                                file_data = f.read()
                                                            b64_data = base64.b64encode(
                                                                file_data
                                                            ).decode("utf-8")
                                                            file_info["file"][
                                                                "data"
                                                            ] = f"data:{mime_type};base64,{b64_data}"
                                                            file_info["file"][
                                                                "preview_url"
                                                            ] = file_info["file"][
                                                                "data"
                                                            ]
                                                        except Exception as img_err:
                                                            _log.warning(
                                                                f"[SendFile] 图片转base64失败: {img_err}"
                                                            )

                                                    # 保存到 session
                                                    session["messages"].append(
                                                        file_info
                                                    )
                                                    self._save_data("sessions")

                                                    # 发送文件消息到前端
                                                    self.socketio.emit(
                                                        "new_message",
                                                        file_info,
                                                        room=session_id,
                                                    )
                                                    _log.info(
                                                        f"[SendFile] 文件已发送: {filename} ({mime_type}, {file_size} bytes)"
                                                    )

                                            except Exception as send_err:
                                                _log.error(
                                                    f"[SendFile] 发送文件时出错: {send_err}",
                                                    exc_info=True,
                                                )
                                        else:
                                            _log.warning(
                                                f"[SendFile] 无法发送文件: file_path={file_path}, filename={filename}, session_id={session_id}, session_exists={session is not None}"
                                            )

                                except Exception as te:
                                    tool_result_str = f"工具执行出错: {str(te)}"
                                    _log.error(
                                        f"[Tools] ✗ 工具执行失败: {tool_name} - {str(te)}"
                                    )

                                    # 更新进度卡片 - 标记工具调用失败
                                    if progress_card:
                                        try:
                                            from nbot.core.progress_card import StepType

                                            tool_names = {
                                                "workspace_read_file": "读取文件",
                                                "workspace_create_file": "创建文件",
                                                "workspace_edit_file": "编辑文件",
                                                "workspace_delete_file": "删除文件",
                                                "workspace_list_files": "列出文件",
                                                "workspace_send_file": "发送文件",
                                                "workspace_parse_file": "解析文件",
                                                "workspace_file_info": "文件信息",
                                                "web_search": "网页搜索",
                                                "generate_image": "生成图片",
                                                "save_to_memory": "保存记忆",
                                                "todo_add": "添加待办",
                                                "todo_list": "列出待办",
                                                "todo_complete": "完成待办",
                                                "todo_delete": "删除待办",
                                                "todo_clear": "清空待办",
                                            }
                                            tool_display_name = tool_names.get(
                                                tool_name, tool_name
                                            )
                                            progress_card.update(
                                                StepType.TOOL_DONE,
                                                f"{tool_display_name}失败",
                                                False,
                                            )
                                        except Exception as e:
                                            _log.warning(
                                                f"[ProgressCard] 更新工具失败状态失败: {e}"
                                            )

                                # 添加工具结果到消息
                                messages_for_ai.append(
                                    {
                                        "role": "tool",
                                        "tool_call_id": tool_call["id"],
                                        "content": tool_result_str,
                                    }
                                )

                            _log.info(
                                f"[Tools] 所有工具执行完成，开始第二次 AI 调用获取最终回复..."
                            )
                            # 再次调用 AI，获取最终回复（不传递 tools，避免无限循环）
                            response = self._get_ai_response_with_tools(
                                messages_for_ai, []
                            )

                            # 处理AI思考内容（如果API返回了的话）
                            if (
                                "thinking_content" in response
                                and response["thinking_content"]
                            ):
                                thinking_content = response["thinking_content"]
                                _log.debug(
                                    f"[ThinkingCard] 第二次调用收到AI思考内容: {len(thinking_content)} 字符"
                                )
                                try:
                                    update_thinking_stream(thinking_content)
                                except Exception as e:
                                    _log.warning(
                                        f"[ThinkingCard] 更新思考内容失败: {e}"
                                    )
                        else:
                            # 没有工具调用，直接使用响应内容
                            pass

                        assistant_content = response.get(
                            "content", "抱歉，暂时无法处理您的请求。"
                        )
                        _log.info(
                            f"[Tools] ✓ AI 最终回复: {assistant_content[:300]}{'...' if len(assistant_content) > 300 else ''}"
                        )
                    else:
                        # 如果工具不可用，回退到普通 AI 调用
                        _log.info("[Tools] 工具不可用，使用普通 AI 调用")
                        assistant_content = self._get_ai_response(messages_for_ai)
                        thinking_content = None

                    # 创建 AI 消息对象
                    assistant_message = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": assistant_content,
                        "timestamp": datetime.now().isoformat(),
                        "sender": "AI",
                    }

                    _log.info(
                        f"[Stream] 准备发送流式响应, message_id={assistant_message['id']}"
                    )

                    # 使用流式发送（前端可以实时看到内容逐字出现）
                    self._stream_send_response(
                        session_id, assistant_message, thinking_content
                    )

                    # 保存消息到会话
                    session["messages"].append(assistant_message)

                    # 更新 Token 统计（估算）
                    estimated_tokens = len(user_content) + len(assistant_content)
                    input_tokens = len(user_content)
                    output_tokens = len(assistant_content)
                    self.token_stats["today"] = (
                        self.token_stats.get("today", 0) + estimated_tokens
                    )
                    self.token_stats["month"] = (
                        self.token_stats.get("month", 0) + estimated_tokens
                    )

                    # 更新历史记录
                    today_str = datetime.now().strftime("%Y-%m-%d")
                    history = self.token_stats.get("history", [])
                    if not history or history[-1].get("date") != today_str:
                        history.append(
                            {
                                "date": today_str,
                                "input": input_tokens,
                                "output": output_tokens,
                                "total": estimated_tokens,
                                "cost": 0.0,
                                "message_count": 1,
                            }
                        )
                    else:
                        history[-1]["input"] = (
                            history[-1].get("input", 0) + input_tokens
                        )
                        history[-1]["output"] = (
                            history[-1].get("output", 0) + output_tokens
                        )
                        history[-1]["total"] = (
                            history[-1].get("total", 0) + estimated_tokens
                        )
                        history[-1]["message_count"] = (
                            history[-1].get("message_count", 0) + 1
                        )
                    self.token_stats["history"] = history[-30:]  # 只保留最近30天

                    # 更新会话统计
                    if session_id:
                        sessions_stats = self.token_stats.get("sessions", {})
                        if session_id not in sessions_stats:
                            sessions_stats[session_id] = {
                                "input": 0,
                                "output": 0,
                                "total": 0,
                                "message_count": 0,
                            }
                        sessions_stats[session_id]["input"] = (
                            sessions_stats[session_id].get("input", 0) + input_tokens
                        )
                        sessions_stats[session_id]["output"] = (
                            sessions_stats[session_id].get("output", 0) + output_tokens
                        )
                        sessions_stats[session_id]["total"] = (
                            sessions_stats[session_id].get("total", 0)
                            + estimated_tokens
                        )
                        sessions_stats[session_id]["message_count"] = (
                            sessions_stats[session_id].get("message_count", 0) + 2
                        )  # 用户消息 + AI回复
                        self.token_stats["sessions"] = sessions_stats

                    # 注意：ai_response 现在通过 _stream_send_response 在第 3373 行流式发送了，不再单独发送

                    # 自动重命名会话（如果是默认名称且对话轮数达到一定数量）
                    try:
                        current_name = session.get("name", "")
                        message_count = len(session.get("messages", []))
                        _log.info(
                            f"[SessionRename] 检查重命名条件: name='{current_name}', count={message_count}"
                        )
                        # 如果是默认名称（以"会话"开头或是空名称），且已有至少4条消息（2轮对话）
                        if (
                            current_name.startswith("会话") or not current_name
                        ) and message_count >= 4:
                            _log.info(f"[SessionRename] 条件满足，开始生成新名称...")
                            new_name = self._generate_session_name(
                                session["messages"],
                                session_id=session_id,
                                parent_message_id=assistant_message["id"],
                            )
                            if new_name:
                                session["name"] = new_name
                                self._save_data("sessions")
                                # 通知前端会话名称已更新
                                self.socketio.emit(
                                    "session_renamed",
                                    {"session_id": session_id, "name": new_name},
                                    room=session_id,
                                )
                                _log.info(
                                    f"[SessionRename] 会话 {session_id[:8]}... 已重命名为: {new_name}"
                                )
                            else:
                                _log.warning(f"[SessionRename] 生成名称失败，返回 None")
                        else:
                            _log.info(
                                f"[SessionRename] 条件不满足: startswith={current_name.startswith('会话')}, empty={not current_name}, count={message_count}"
                            )
                    except Exception as e:
                        _log.error(
                            f"[SessionRename] 自动重命名失败: {e}", exc_info=True
                        )

                except Exception as e:
                    _log.error(f"Error in AI chat: {e}")
                    error_message = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": f"抱歉，处理消息时出错: {str(e)}",
                        "timestamp": datetime.now().isoformat(),
                        "sender": "AI",
                        "error": True,
                    }
                    session["messages"].append(error_message)
                    self.socketio.emit(
                        "ai_response",
                        {"session_id": session_id, "message": error_message},
                        room=session_id,
                    )

            # 启动后台线程获取 AI 回复
            threading.Thread(target=get_response, daemon=True).start()

            return jsonify({"success": True, "user_message": user_message})

        # ==================== Exec 命令确认 API ====================
        @self.app.route("/api/exec/confirm", methods=["POST"])
        def exec_confirm():
            """确认执行命令（用于非白名单命令）"""
            data = request.json
            command = data.get("command")
            timeout = data.get("timeout", 30)

            if not command:
                return jsonify({"error": "Command is required"}), 400

            try:
                from nbot.services.tools import ToolExecutor

                # 执行命令，confirmed=True 表示用户已确认
                result = ToolExecutor.exec_command(
                    command, timeout=timeout, confirmed=True
                )
                return jsonify(result)
            except Exception as e:
                _log.error(f"Exec confirm error: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        # ==================== AI 工具调用 API ====================
        @self.app.route("/api/ai/tools", methods=["POST"])
        def ai_tools_handler():
            """AI 工具调用接口 - 让 AI 可以直接操作工作流等功能"""
            data = request.json
            messages = data.get("messages", [])

            if not messages:
                return jsonify({"error": "Messages are required"}), 400

            # 定义可用工具
            tools = [
                {
                    "type": "function",
                    "function": {
                        "name": "create_workflow",
                        "description": "创建一个新的工作流。当用户想要创建定时任务、自动回复等功能时使用。",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "name": {
                                    "type": "string",
                                    "description": "工作流名称，简短明了",
                                },
                                "description": {
                                    "type": "string",
                                    "description": "工作流描述，作为系统提示词给AI，详细说明工作流的功能和执行逻辑",
                                },
                                "trigger": {
                                    "type": "string",
                                    "enum": ["manual", "cron", "message"],
                                    "description": "触发方式：manual手动、cron定时、message消息触发",
                                },
                                "cron": {
                                    "type": "string",
                                    "description": "定时触发时的cron表达式，如'0 8 * * *'表示每天8点",
                                },
                                "keywords": {
                                    "type": "string",
                                    "description": "消息触发时的关键词，多个用逗号分隔",
                                },
                                "target_type": {
                                    "type": "string",
                                    "enum": [
                                        "none",
                                        "qq_group",
                                        "qq_private",
                                        "session",
                                    ],
                                    "description": "结果发送目标",
                                },
                                "target_id": {
                                    "type": "string",
                                    "description": "目标ID，如QQ群号、QQ号或会话ID",
                                },
                            },
                            "required": ["name", "description", "trigger"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "list_workflows",
                        "description": "列出所有工作流，用于查看当前有哪些工作流",
                        "parameters": {"type": "object", "properties": {}},
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "toggle_workflow",
                        "description": "启用或禁用工作流",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "workflow_id": {
                                    "type": "string",
                                    "description": "工作流ID",
                                }
                            },
                            "required": ["workflow_id"],
                        },
                    },
                },
                {
                    "type": "function",
                    "function": {
                        "name": "execute_workflow",
                        "description": "立即执行一个工作流",
                        "parameters": {
                            "type": "object",
                            "properties": {
                                "workflow_id": {
                                    "type": "string",
                                    "description": "工作流ID",
                                }
                            },
                            "required": ["workflow_id"],
                        },
                    },
                },
            ]

            try:
                # 调用 AI 并启用工具
                if not self.ai_client:
                    return jsonify({"error": "AI client not initialized"}), 503

                # 添加系统提示词
                system_prompt = """你是一个智能助手，可以帮助用户管理工作流。你可以使用工具来创建、查看、启用/禁用和执行工作流。

工作流是一种自动化任务，可以：
1. 定时执行（如每天早上8点发送问候）
2. 消息触发（如收到关键词时自动回复）
3. 手动执行

当用户想要创建工作流时，请：
1. 理解用户的需求
2. 使用 create_workflow 工具创建工作流
3. 向用户确认创建结果

当用户询问有哪些工作流时，使用 list_workflows 工具。
当用户想要启用/禁用工作流时，先列出工作流让用户选择，然后使用 toggle_workflow 工具。
当用户想要执行工作流时，使用 execute_workflow 工具。"""

                full_messages = [
                    {
                        "role": "system",
                        "content": f"{system_prompt}\n\n{CORE_INSTRUCTIONS}",
                    }
                ] + messages

                # 调用 AI（支持工具的版本）
                response = self._get_ai_response_with_tools(full_messages, tools)

                return jsonify(
                    {
                        "content": response.get("content", ""),
                        "tool_calls": response.get("tool_calls", []),
                    }
                )

            except Exception as e:
                _log.error(f"AI tools error: {e}")
                return jsonify({"error": str(e)}), 500

        def _get_ai_response_with_tools(
            self, messages: List[Dict], tools: List[Dict]
        ) -> Dict:
            """调用 AI 并支持工具"""
            try:
                # 检查 AI 客户端是否支持工具调用
                url_base = (self.ai_base_url or "").rstrip("/")
                url = f"{url_base}/chat/completions"

                headers = {
                    "Authorization": f"Bearer {self.ai_api_key}",
                    "Content-Type": "application/json",
                }

                payload = {
                    "model": self.ai_model,
                    "messages": messages,
                    "tools": tools,
                    "tool_choice": "auto",
                }

                import requests

                resp = requests.post(url, json=payload, headers=headers)
                resp.raise_for_status()
                data = resp.json()

                choice = data.get("choices", [{}])[0]
                message = choice.get("message", {})

                result = {"content": message.get("content", "")}

                # 处理工具调用
                if "tool_calls" in message:
                    tool_calls = message["tool_calls"]
                    result["tool_calls"] = []

                    for tool_call in tool_calls:
                        function_name = tool_call.get("function", {}).get("name")
                        arguments = json.loads(
                            tool_call.get("function", {}).get("arguments", "{}")
                        )

                        # 执行工具
                        tool_result = self._execute_tool(function_name, arguments)
                        result["tool_calls"].append(
                            {
                                "name": function_name,
                                "arguments": arguments,
                                "result": tool_result,
                            }
                        )

                return result

            except Exception as e:
                _log.error(f"AI with tools error: {e}")
                # 回退到普通 AI 调用
                content = self._get_ai_response(messages)
                return {"content": content}

        def _execute_tool(self, function_name: str, arguments: Dict) -> Dict:
            """执行工具函数"""
            try:
                if function_name == "create_workflow":
                    workflow = {
                        "id": str(uuid.uuid4()),
                        "name": arguments["name"],
                        "description": arguments["description"],
                        "enabled": True,
                        "trigger": arguments["trigger"],
                        "config": {
                            "cron": arguments.get("cron", "0 8 * * *"),
                            "keywords": arguments.get("keywords", ""),
                            "target_type": arguments.get("target_type", "none"),
                            "target_id": arguments.get("target_id", ""),
                            "max_history": 10,
                        },
                    }
                    self.workflows.append(workflow)
                    self._save_data("workflows")

                    # 如果是定时触发，添加到调度器
                    if workflow["trigger"] == "cron":
                        self._schedule_workflow(workflow)

                    return {"success": True, "workflow": workflow}

                elif function_name == "list_workflows":
                    return {"success": True, "workflows": self.workflows}

                elif function_name == "toggle_workflow":
                    workflow_id = arguments["workflow_id"]
                    for w in self.workflows:
                        if w["id"] == workflow_id:
                            w["enabled"] = not w.get("enabled", True)
                            self._save_data("workflows")

                            if w["trigger"] == "cron":
                                if w["enabled"]:
                                    self._schedule_workflow(w)
                                else:
                                    self._unschedule_workflow(workflow_id)

                            return {"success": True, "enabled": w["enabled"]}
                    return {"success": False, "error": "Workflow not found"}

                elif function_name == "execute_workflow":
                    workflow_id = arguments["workflow_id"]
                    self._execute_workflow(workflow_id, {"source": "manual"})
                    return {"success": True}

                else:
                    return {
                        "success": False,
                        "error": f"Unknown function: {function_name}",
                    }

            except Exception as e:
                return {"success": False, "error": str(e)}

        # ==================== QQ 会话 API ====================
        @self.app.route("/api/qq/sessions")
        def get_qq_sessions():
            """获取所有 QQ 会话（群聊和私聊）"""
            qq_sessions = []
            for session_id, session in self.sessions.items():
                if session.get("type") in ["qq_group", "qq_private"]:
                    qq_sessions.append(
                        {
                            "id": session_id,
                            "name": session.get("name", "QQ 会话"),
                            "type": session.get("type"),
                            "qq_id": session.get("qq_id"),
                            "message_count": len(session.get("messages", [])),
                            "last_message": session.get("last_message"),
                            "created_at": session.get("created_at"),
                        }
                    )
            return jsonify(qq_sessions)

        @self.app.route("/api/qq/sessions/<session_id>/sync", methods=["POST"])
        def sync_qq_session(session_id):
            """同步 QQ 会话消息"""
            if session_id not in self.sessions:
                return jsonify({"error": "Session not found"}), 404

            session = self.sessions[session_id]
            if session.get("type") not in ["qq_group", "qq_private"]:
                return jsonify({"error": "Not a QQ session"}), 400

            # 从保存的 QQ 消息记录中加载
            try:
                from nbot.chat import user_messages, group_messages

                qq_id = session.get("qq_id")

                if session["type"] == "qq_private" and qq_id in user_messages:
                    messages = user_messages[qq_id]
                    # 转换格式
                    for msg in messages:
                        session["messages"].append(
                            {
                                "id": str(uuid.uuid4()),
                                "role": "user"
                                if msg.get("role") == "user"
                                else "assistant",
                                "content": msg.get("content", ""),
                                "timestamp": msg.get(
                                    "time", datetime.now().isoformat()
                                ),
                                "sender": msg.get("sender", "QQ User"),
                            }
                        )
                elif session["type"] == "qq_group" and qq_id in group_messages:
                    messages = group_messages[qq_id]
                    for msg in messages:
                        session["messages"].append(
                            {
                                "id": str(uuid.uuid4()),
                                "role": "user"
                                if msg.get("role") == "user"
                                else "assistant",
                                "content": msg.get("content", ""),
                                "timestamp": msg.get(
                                    "time", datetime.now().isoformat()
                                ),
                                "sender": msg.get("sender", "QQ User"),
                            }
                        )

                self._save_data("sessions")
                return jsonify(
                    {"success": True, "message_count": len(session["messages"])}
                )
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/qq/sessions/<session_id>/send", methods=["POST"])
        def send_qq_message(session_id):
            """从 Web 发送消息到 QQ"""
            if session_id not in self.sessions:
                return jsonify({"error": "Session not found"}), 404

            session = self.sessions[session_id]
            if session.get("type") not in ["qq_group", "qq_private"]:
                return jsonify({"error": "Not a QQ session"}), 400

            if not self.qq_bot:
                return jsonify({"error": "QQ Bot not available"}), 503

            data = request.json
            content = data.get("content", "").strip()

            if not content:
                return jsonify({"error": "Content is required"}), 400

            try:
                qq_id = session.get("qq_id")

                # 使用线程运行异步任务
                import threading
                import asyncio

                async def send_qq_msg():
                    try:
                        if session["type"] == "qq_group":
                            # 发送到群聊
                            await self.qq_bot.api.post_group_msg(
                                group_id=qq_id, text=content
                            )
                        else:
                            # 发送到私聊
                            await self.qq_bot.api.post_private_msg(
                                user_id=qq_id, text=content
                            )
                    except Exception as e:
                        _log.error(f"Failed to send QQ message: {e}")

                def run_async_task():
                    try:
                        loop = asyncio.new_event_loop()
                        asyncio.set_event_loop(loop)
                        loop.run_until_complete(send_qq_msg())
                        loop.close()
                    except Exception as e:
                        _log.error(f"Failed to run async task: {e}")

                threading.Thread(target=run_async_task, daemon=True).start()

                # 记录到会话
                message = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": content,
                    "timestamp": datetime.now().isoformat(),
                    "sender": "Bot",
                }
                session["messages"].append(message)
                session["last_message"] = content
                self._save_data("sessions")

                return jsonify({"success": True, "message": message})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/qq/link-session", methods=["POST"])
        def link_qq_session():
            """将 QQ 聊天关联为 Web 会话"""
            data = request.json
            qq_id = data.get("qq_id")
            session_type = data.get("type")  # 'qq_group' 或 'qq_private'
            name = data.get("name", f"QQ {qq_id}")

            if not qq_id or not session_type:
                return jsonify({"error": "qq_id and type are required"}), 400

            # 查找是否已存在
            for session_id, session in self.sessions.items():
                if (
                    session.get("qq_id") == qq_id
                    and session.get("type") == session_type
                ):
                    return jsonify(
                        {"success": True, "session_id": session_id, "exists": True}
                    )

            # 创建新会话
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
            self.sessions[session_id] = session
            self._save_data("sessions")

            return jsonify({"success": True, "session_id": session_id, "exists": False})

        # ==================== Heartbeat API ====================
        @self.app.route("/api/heartbeat")
        def get_heartbeat():
            """获取 Heartbeat 配置"""
            return jsonify(self.heartbeat_config)

        @self.app.route("/api/heartbeat", methods=["PUT"])
        def update_heartbeat():
            """更新 Heartbeat 配置"""
            data = request.json

            enabled = data.get("enabled", self.heartbeat_config.get("enabled", False))
            interval_minutes = data.get(
                "interval_minutes", self.heartbeat_config.get("interval_minutes", 60)
            )
            content_file = data.get(
                "content_file",
                self.heartbeat_config.get("content_file", "heartbeat.md"),
            )
            target_session_id = data.get(
                "target_session_id", self.heartbeat_config.get("target_session_id", "")
            )
            targets = data.get("targets", self.heartbeat_config.get("targets", []))

            self.heartbeat_config = {
                "enabled": enabled,
                "interval_minutes": interval_minutes,
                "content_file": content_file,
                "target_session_id": target_session_id,
                "targets": targets,
                "last_run": self.heartbeat_config.get("last_run"),
                "next_run": self.heartbeat_config.get("next_run"),
            }

            # 更新调度器
            if enabled:
                self._start_heartbeat_job(interval_minutes)
            else:
                self._stop_heartbeat_job()

            self._save_data("heartbeat")
            return jsonify({"success": True, "config": self.heartbeat_config})

        @self.app.route("/api/heartbeat/run", methods=["POST"])
        def run_heartbeat():
            """手动触发 Heartbeat"""
            try:
                import asyncio

                # 使用 asyncio.run 运行异步函数，force=True 跳过 enabled 检查
                asyncio.run(self._execute_heartbeat(force=True))
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/api/heartbeat/content", methods=["GET"])
        def get_heartbeat_content():
            """获取 heartbeat.md 文件内容"""
            content_file = request.args.get("file", "heartbeat.md")
            content = self._load_heartbeat_content(content_file)
            return jsonify({"content": content, "file": content_file})

        @self.app.route("/api/heartbeat/content", methods=["PUT"])
        def save_heartbeat_content():
            """保存 heartbeat.md 文件内容"""
            data = request.json
            content = data.get("content", "")
            content_file = data.get("file", "heartbeat.md")

            # 优先保存到 resources 目录
            save_path = os.path.join(
                os.path.dirname(__file__), "..", "..", "resources", content_file
            )

            try:
                os.makedirs(os.path.dirname(save_path), exist_ok=True)
                with open(save_path, "w", encoding="utf-8") as f:
                    f.write(content)
                return jsonify({"success": True, "path": save_path})
            except Exception as e:
                # 尝试备用路径
                try:
                    save_path = os.path.join(os.getcwd(), "resources", content_file)
                    os.makedirs(os.path.dirname(save_path), exist_ok=True)
                    with open(save_path, "w", encoding="utf-8") as f:
                        f.write(content)
                    return jsonify({"success": True, "path": save_path})
                except Exception as e2:
                    return jsonify(
                        {"success": False, "error": f"无法保存文件: {e}"}
                    ), 500

        # ==================== 工作流 API ====================
        @self.app.route("/api/workflows")
        def get_workflows():
            return jsonify(self.workflows)

        @self.app.route("/api/workflows", methods=["POST"])
        def create_workflow():
            data = request.json
            workflow = {
                "id": str(uuid.uuid4()),
                "name": data.get("name", "新工作流"),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "trigger": data.get("trigger", "manual"),
                "config": data.get("config", {}),
            }
            self.workflows.append(workflow)
            self._save_data("workflows")

            # 如果启用了 cron 触发，添加到调度器
            if workflow.get("enabled") and workflow.get("trigger") == "cron":
                self._schedule_workflow(workflow)

            return jsonify({"success": True, "workflow": workflow})

        @self.app.route("/api/workflows/<workflow_id>", methods=["PUT"])
        def update_workflow(workflow_id):
            for workflow in self.workflows:
                if workflow["id"] == workflow_id:
                    data = request.json
                    old_trigger = workflow.get("trigger")
                    old_enabled = workflow.get("enabled")

                    workflow.update(data)
                    self._save_data("workflows")

                    # 更新调度器
                    if workflow.get("trigger") == "cron":
                        if workflow.get("enabled"):
                            self._schedule_workflow(workflow)
                        else:
                            self._unschedule_workflow(workflow_id)
                    elif old_trigger == "cron":
                        self._unschedule_workflow(workflow_id)

                    return jsonify({"success": True, "workflow": workflow})
            return jsonify({"error": "Workflow not found"}), 404

        @self.app.route("/api/workflows/<workflow_id>", methods=["DELETE"])
        def delete_workflow(workflow_id):
            # 取消调度
            self._unschedule_workflow(workflow_id)

            self.workflows = [w for w in self.workflows if w["id"] != workflow_id]
            self._save_data("workflows")
            return jsonify({"success": True})

        @self.app.route("/api/workflows/<workflow_id>/toggle", methods=["POST"])
        def toggle_workflow(workflow_id):
            for workflow in self.workflows:
                if workflow["id"] == workflow_id:
                    workflow["enabled"] = not workflow.get("enabled", True)
                    self._save_data("workflows")

                    # 更新调度器
                    if workflow.get("trigger") == "cron":
                        if workflow["enabled"]:
                            self._schedule_workflow(workflow)
                        else:
                            self._unschedule_workflow(workflow_id)

                    return jsonify({"success": True, "enabled": workflow["enabled"]})
            return jsonify({"error": "Workflow not found"}), 404

        @self.app.route("/api/workflows/<workflow_id>/execute", methods=["POST"])
        def execute_workflow(workflow_id):
            """手动执行工作流"""
            for workflow in self.workflows:
                if workflow["id"] == workflow_id:
                    data = request.json or {}
                    # 提取用户输入的内容
                    user_content = data.get("content", "")
                    trigger_data = {
                        "source": "manual",
                        "content": user_content,
                        "time": datetime.now().isoformat(),
                    }
                    self._execute_workflow(workflow_id, trigger_data)
                    return jsonify(
                        {"success": True, "message": "Workflow execution started"}
                    )
            return jsonify({"error": "Workflow not found"}), 404

        # ==================== 人格设置 API ====================
        @self.app.route("/api/personality")
        def get_personality():
            return jsonify(self.personality)

        @self.app.route("/api/personality", methods=["PUT"])
        def update_personality():
            data = request.json
            self.personality["name"] = data.get(
                "name", self.personality.get("name", "")
            )
            self.personality["prompt"] = data.get(
                "prompt", self.personality.get("prompt", "")
            )

            # 保存到文件
            try:
                prompt_file = os.path.join(
                    self.base_dir, "resources", "prompts", "neko.txt"
                )
                os.makedirs(os.path.dirname(prompt_file), exist_ok=True)
                with open(prompt_file, "w", encoding="utf-8") as f:
                    f.write(self.personality["prompt"])
            except Exception as e:
                _log.error(f"Failed to save personality: {e}")

            return jsonify({"success": True, "personality": self.personality})

        @self.app.route("/api/personality/presets")
        def get_personality_presets():
            # 读取 neko.txt 作为猫娘助手的预设
            neko_prompt = self.personality.get("prompt", "")
            presets = [
                {
                    "id": "1",
                    "name": "猫娘助手",
                    "icon": "🐱",
                    "description": "可爱温柔的猫娘，说话带喵尾音",
                    "prompt": neko_prompt,
                },
                {
                    "id": "2",
                    "name": "专业助手",
                    "icon": "👔",
                    "description": "专业、高效、简洁的助手",
                    "prompt": "你是一个专业的 AI 助手，回答简洁明了，注重效率。",
                },
                {
                    "id": "3",
                    "name": "创意作家",
                    "icon": "✍️",
                    "description": "富有创造力的写作助手",
                    "prompt": "你是一个富有创造力的作家，擅长各种文体，能够帮助用户创作精彩内容。",
                },
                {
                    "id": "4",
                    "name": "代码专家",
                    "icon": "💻",
                    "description": "精通各种编程语言",
                    "prompt": "你是一个编程专家，精通多种编程语言，能够提供高质量的代码和编程建议。",
                },
            ]
            return jsonify(presets)

        # ==================== 自定义人格预设 API ====================
        @self.app.route("/api/personality/custom-presets", methods=["GET"])
        def get_custom_personality_presets():
            """获取自定义人格预设列表"""
            return jsonify(self.custom_personality_presets)

        @self.app.route("/api/personality/custom-presets", methods=["POST"])
        def add_custom_personality_preset():
            """添加自定义人格预设"""
            data = request.json
            preset = {
                "id": str(uuid.uuid4()),
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "icon": data.get("icon", "🎭"),
                "prompt": data.get("prompt", ""),
                "created_at": datetime.now().isoformat(),
            }
            self.custom_personality_presets.append(preset)
            self._save_data("custom_personality_presets")
            return jsonify(preset)

        @self.app.route(
            "/api/personality/custom-presets/<preset_id>", methods=["DELETE"]
        )
        def delete_custom_personality_preset(preset_id):
            """删除自定义人格预设"""
            self.custom_personality_presets = [
                p for p in self.custom_personality_presets if p["id"] != preset_id
            ]
            self._save_data("custom_personality_presets")
            return jsonify({"success": True})

        # ==================== 记忆管理 API ====================
        @self.app.route("/api/memory")
        def get_memory():
            """获取记忆列表，支持按类型筛选"""
            mem_type = request.args.get("type", "all")
            target_id = request.args.get("target_id", "")

            # 优先使用 prompt_manager（确保获取最新数据）
            if PROMPT_MANAGER_AVAILABLE and prompt_manager:
                try:
                    memories = prompt_manager.get_memories(
                        target_id, mem_type if mem_type != "all" else None
                    )
                except Exception as e:
                    _log.warning(f"从 prompt_manager 获取记忆失败: {e}")
                    memories = self.memories
            else:
                memories = self.memories

            if mem_type != "all":
                memories = [m for m in memories if m.get("type", "long") == mem_type]

            if target_id:
                memories = [m for m in memories if m.get("target_id", "") == target_id]

            # 分离长期和短期记忆
            long_term = [m for m in memories if m.get("type", "long") == "long"]
            short_term = [m for m in memories if m.get("type", "long") == "short"]

            return jsonify(
                {"memories": memories, "long_term": long_term, "short_term": short_term}
            )

        @self.app.route("/api/memory", methods=["POST"])
        def add_memory():
            data = request.json
            target_id = data.get("target_id", "")
            title = data.get("title", "")
            content = data.get("content", "")
            summary = data.get("summary", None)
            mem_type = data.get("type", "long")
            expire_days = data.get("expire_days", 7)

            if not title or not content:
                return jsonify({"success": False, "error": "标题和内容不能为空"}), 400

            success = prompt_manager.add_memory(
                title, content, target_id, summary, mem_type, expire_days
            )
            if success:
                # 重新加载 prompt_manager 的记忆并同步到 self.memories
                self.memories = prompt_manager.get_memories()
                self._save_data("memories")
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "Failed to add memory"}), 500

        @self.app.route("/api/memory/<memory_id>", methods=["PUT"])
        def update_memory(memory_id):
            data = request.json

            # 查找记忆
            for mem in self.memories:
                if mem.get("id") == memory_id:
                    # 支持新格式
                    if "title" in data:
                        mem["title"] = data.get("title", mem.get("title", ""))
                    elif "key" in data:
                        # 兼容旧格式
                        mem["title"] = data.get("key", mem.get("title", ""))

                    if "content" in data:
                        mem["content"] = data.get("content", mem.get("content", ""))
                    elif "value" in data:
                        # 兼容旧格式
                        mem["content"] = data.get("value", mem.get("content", ""))

                    mem["summary"] = data.get("summary", mem.get("summary", ""))
                    mem["type"] = data.get("type", mem.get("type", "long"))
                    mem["priority"] = data.get(
                        "priority", mem.get("priority", "normal")
                    )
                    mem["expire_days"] = data.get(
                        "expire_days", mem.get("expire_days", 7)
                    )
                    mem["target_id"] = data.get("target_id", mem.get("target_id", ""))
                    mem["updated_at"] = datetime.now().isoformat()
                    self._save_data("memories")

                    # 同时更新 prompt_manager 中的记忆
                    try:
                        pm_memories = prompt_manager.get_memories()
                        for pm_mem in pm_memories:
                            if pm_mem.get("id") == memory_id:
                                pm_mem.update(mem)
                                break
                    except:
                        pass

                    return jsonify({"success": True, "memory": mem})
            return jsonify({"error": "Memory not found"}), 404

        @self.app.route("/api/memory/<memory_id>", methods=["DELETE"])
        def delete_memory(memory_id):
            success = prompt_manager.delete_memory(memory_id)
            if success:
                self.memories = [m for m in self.memories if m.get("id") != memory_id]
                self._save_data("memories")
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "Failed to delete memory"}), 500

        @self.app.route("/api/memory", methods=["DELETE"])
        def clear_all_memory():
            target_id = request.args.get("target_id")
            success = prompt_manager.clear_memories(target_id)
            if success:
                if target_id:
                    self.memories = [
                        m for m in self.memories if m.get("target_id") != target_id
                    ]
                else:
                    self.memories = []
                self._save_data("memories")
                return jsonify({"success": True})
            return jsonify({"success": False, "error": "Failed to clear memories"}), 500

        @self.app.route("/api/memory/export")
        def export_memory():
            return jsonify({"memories": self.memories})

        # ==================== 知识库 API ====================
        @self.app.route("/api/knowledge")
        def get_knowledge():
            if not KNOWLEDGE_MANAGER_AVAILABLE:
                return jsonify([])
            try:
                km = get_knowledge_manager()
                bases = km.list_knowledge_bases()
                docs = []
                for kb in bases:
                    for doc_id in kb.documents:
                        doc = km.store.load_document(doc_id)
                        if doc:
                            docs.append(
                                {
                                    "id": doc.id,
                                    "name": doc.title,
                                    "title": doc.title,
                                    "type": doc.source or "txt",
                                    "source": doc.source,
                                    "size": len(doc.content),
                                    "content": doc.content[:200] + "..."
                                    if len(doc.content) > 200
                                    else doc.content,
                                    "full_content": doc.content,
                                    "description": "",
                                    "indexed": True,
                                    "tags": doc.tags,
                                    "created_at": doc.created_at,
                                }
                            )
                return jsonify(docs)
            except Exception as e:
                _log.error(f"Failed to get knowledge: {e}")
                return jsonify([])

        @self.app.route("/api/knowledge", methods=["POST"])
        def add_knowledge():
            if not KNOWLEDGE_MANAGER_AVAILABLE:
                return jsonify(
                    {"success": False, "error": "Knowledge manager not available"}
                ), 503
            try:
                data = request.json
                km = get_knowledge_manager()
                title = data.get("name", "未命名")
                content = data.get("content", "")
                source = data.get("source", "")
                tags = data.get("tags", [])

                if not content:
                    return jsonify(
                        {"success": False, "error": "Content is required"}
                    ), 400

                default_kb = km.store.load_base("default")
                if not default_kb:
                    default_kb = km.create_knowledge_base(
                        "默认知识库", "系统默认知识库"
                    )

                doc = km.add_document("default", title, content, source, tags)

                return jsonify(
                    {
                        "success": True,
                        "document": {
                            "id": doc.id,
                            "name": doc.title,
                            "title": doc.title,
                            "type": doc.source or "txt",
                            "source": doc.source,
                            "size": len(doc.content),
                            "content": doc.content,
                            "description": "",
                            "indexed": True,
                            "tags": doc.tags,
                            "created_at": doc.created_at,
                        },
                    }
                )
            except Exception as e:
                _log.error(f"Failed to add knowledge: {e}")
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/api/knowledge/<doc_id>")
        def get_knowledge_doc(doc_id):
            if not KNOWLEDGE_MANAGER_AVAILABLE:
                return jsonify({"error": "Knowledge manager not available"}), 503
            try:
                km = get_knowledge_manager()
                doc = km.store.load_document(doc_id)
                if doc:
                    return jsonify(
                        {
                            "id": doc.id,
                            "name": doc.title,
                            "title": doc.title,
                            "type": doc.source or "txt",
                            "source": doc.source,
                            "size": len(doc.content),
                            "content": doc.content,
                            "description": "",
                            "indexed": True,
                            "tags": doc.tags,
                            "created_at": doc.created_at,
                        }
                    )
                return jsonify({"error": "Document not found"}), 404
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/knowledge/<doc_id>", methods=["DELETE"])
        def delete_knowledge(doc_id):
            if not KNOWLEDGE_MANAGER_AVAILABLE:
                return jsonify(
                    {"success": False, "error": "Knowledge manager not available"}
                ), 503
            try:
                km = get_knowledge_manager()
                doc = km.store.load_document(doc_id)
                if not doc:
                    return jsonify(
                        {"success": False, "error": "Document not found"}
                    ), 404

                chunk_file = km.store.chunks_dir / f"{doc_id}_0.json"
                if chunk_file.exists():
                    chunk_file.unlink()

                doc_file = km.store.documents_dir / f"{doc_id}.json"
                if doc_file.exists():
                    doc_file.unlink()

                for kb_file in km.store.bases_dir.glob("*.json"):
                    with open(kb_file, "r", encoding="utf-8") as f:
                        kb_data = json.load(f)
                    if doc_id in kb_data.get("documents", []):
                        kb_data["documents"].remove(doc_id)
                        with open(kb_file, "w", encoding="utf-8") as f:
                            json.dump(kb_data, f, ensure_ascii=False, indent=2)

                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"success": False, "error": str(e)}), 500

        @self.app.route("/api/knowledge/<doc_id>/index", methods=["POST"])
        def index_knowledge(doc_id):
            if not KNOWLEDGE_MANAGER_AVAILABLE:
                return jsonify(
                    {"success": False, "error": "Knowledge manager not available"}
                ), 503
            return jsonify({"success": True, "message": "知识库自动索引，无需手动触发"})

        # ==================== AI 配置 API ====================
        @self.app.route("/api/ai-config")
        def get_ai_config():
            # 返回配置时不包含 API Key
            config = self.ai_config.copy()
            config["api_key"] = "********" if config.get("api_key") else ""
            config["model"] = self.ai_model or config.get("model", "gpt-4")
            config["base_url"] = self.ai_base_url or config.get("base_url", "")
            return jsonify(config)

        @self.app.route("/api/ai-config", methods=["PUT"])
        def update_ai_config():
            data = request.json

            # 更新内存配置
            if data.get("provider"):
                self.ai_config["provider"] = data["provider"]
            if data.get("api_key") and data["api_key"] != "********":
                self.ai_config["api_key"] = data["api_key"]
                self.ai_api_key = data["api_key"]
            if data.get("base_url") is not None:
                self.ai_config["base_url"] = data["base_url"]
                self.ai_base_url = data["base_url"]
            if data.get("model"):
                self.ai_config["model"] = data["model"]
                self.ai_model = data["model"]
            if data.get("temperature") is not None:
                self.ai_config["temperature"] = data["temperature"]
            if data.get("max_tokens") is not None:
                self.ai_config["max_tokens"] = data["max_tokens"]
            if data.get("top_p") is not None:
                self.ai_config["top_p"] = data["top_p"]

            # 重新初始化 AI 客户端
            if self.ai_api_key and self.ai_base_url:
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
                    )
                except Exception as e:
                    _log.error(f"Failed to reinitialize AI client: {e}")

            # 保存 AI 配置到磁盘
            self._save_data("ai_config")

            return jsonify({"success": True})

        @self.app.route("/api/ai-config/test", methods=["POST"])
        def test_ai_config():
            """测试 AI 配置的连接"""
            data = request.json

            provider = data.get("provider", "custom")
            api_key = data.get("api_key", "")
            base_url = data.get("base_url", "")
            model = data.get("model", "")

            # 验证必要参数
            if not api_key:
                return jsonify({"success": False, "message": "API Key 不能为空"})

            if not base_url:
                return jsonify({"success": False, "message": "Base URL 不能为空"})

            if not model:
                return jsonify({"success": False, "message": "模型名称不能为空"})

            try:
                import requests

                # 清理 base_url
                url_base = base_url.rstrip("/")

                # 构建 API URL
                if "/chat/completions" in url_base or "/chatcompletion" in url_base:
                    url = url_base
                else:
                    url = f"{url_base}/chat/completions"

                # 构建请求头
                headers = {
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                }

                # 构建测试请求
                payload = {
                    "model": model,
                    "messages": [{"role": "user", "content": "Hello"}],
                    "max_tokens": 10,
                }

                # 发送测试请求
                resp = requests.post(url, json=payload, headers=headers, timeout=30)
                resp.raise_for_status()

                return jsonify({"success": True, "message": "连接测试成功"})

            except requests.exceptions.Timeout:
                return jsonify(
                    {"success": False, "message": "连接超时，请检查 Base URL 是否正确"}
                )
            except requests.exceptions.ConnectionError:
                return jsonify(
                    {
                        "success": False,
                        "message": "无法连接到服务器，请检查 Base URL 是否正确",
                    }
                )
            except requests.exceptions.HTTPError as e:
                error_msg = f"HTTP错误: {e.response.status_code}"
                try:
                    error_data = e.response.json()
                    if "error" in error_data:
                        error_msg += (
                            f" - {error_data['error'].get('message', '未知错误')}"
                        )
                except:
                    pass
                return jsonify({"success": False, "message": error_msg})
            except Exception as e:
                return jsonify({"success": False, "message": f"测试失败: {str(e)}"})

        # ==================== 多模型配置管理 API ====================
        @self.app.route("/api/ai-models")
        def get_ai_models():
            """获取所有AI模型配置列表"""
            # 返回时隐藏API Key
            models = []
            for m in self.ai_models:
                model_copy = m.copy()
                if "api_key" in model_copy:
                    model_copy["api_key"] = "********" if model_copy["api_key"] else ""
                models.append(model_copy)
            return jsonify({"models": models, "active_model_id": self.active_model_id})

        @self.app.route("/api/ai-models", methods=["POST"])
        def create_ai_model():
            """创建新的AI模型配置"""
            data = request.json
            model = {
                "id": str(uuid.uuid4()),
                "name": data.get("name", "新配置"),
                "provider": data.get("provider", "custom"),
                "api_key": data.get("api_key", ""),
                "base_url": data.get("base_url", ""),
                "model": data.get("model", ""),
                "enabled": data.get("enabled", True),
                "temperature": data.get("temperature", 0.7),
                "max_tokens": data.get("max_tokens", 2000),
                "top_p": data.get("top_p", 0.9),
                "frequency_penalty": data.get("frequency_penalty", 0),
                "presence_penalty": data.get("presence_penalty", 0),
                "system_prompt": data.get("system_prompt", ""),
                "timeout": data.get("timeout", 60),
                "retry_count": data.get("retry_count", 3),
                "stream": data.get("stream", True),
                "enable_memory": data.get("enable_memory", True),
                "image_model": data.get("image_model", ""),
                "search_api_key": data.get("search_api_key", ""),
                "embedding_model": data.get("embedding_model", ""),
                "max_context_length": data.get("max_context_length", 8000),
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
            }
            self.ai_models.append(model)
            self._save_data("ai_models")
            return jsonify({"success": True, "model": model})

        @self.app.route("/api/ai-models/<model_id>", methods=["PUT"])
        def update_ai_model(model_id):
            """更新AI模型配置"""
            for model in self.ai_models:
                if model["id"] == model_id:
                    data = request.json
                    model["name"] = data.get("name", model["name"])
                    model["provider"] = data.get("provider", model["provider"])
                    if data.get("api_key") and data["api_key"] != "********":
                        model["api_key"] = data["api_key"]
                    model["base_url"] = data.get("base_url", model["base_url"])
                    model["model"] = data.get("model", model["model"])
                    model["enabled"] = data.get("enabled", model.get("enabled", True))
                    model["temperature"] = data.get(
                        "temperature", model.get("temperature", 0.7)
                    )
                    model["max_tokens"] = data.get(
                        "max_tokens", model.get("max_tokens", 2000)
                    )
                    model["top_p"] = data.get("top_p", model.get("top_p", 0.9))
                    model["frequency_penalty"] = data.get(
                        "frequency_penalty", model.get("frequency_penalty", 0)
                    )
                    model["presence_penalty"] = data.get(
                        "presence_penalty", model.get("presence_penalty", 0)
                    )
                    model["system_prompt"] = data.get(
                        "system_prompt", model.get("system_prompt", "")
                    )
                    model["timeout"] = data.get("timeout", model.get("timeout", 60))
                    model["retry_count"] = data.get(
                        "retry_count", model.get("retry_count", 3)
                    )
                    model["stream"] = data.get("stream", model.get("stream", True))
                    model["enable_memory"] = data.get(
                        "enable_memory", model.get("enable_memory", True)
                    )
                    model["image_model"] = data.get(
                        "image_model", model.get("image_model", "")
                    )
                    model["search_api_key"] = data.get(
                        "search_api_key", model.get("search_api_key", "")
                    )
                    model["embedding_model"] = data.get(
                        "embedding_model", model.get("embedding_model", "")
                    )
                    model["max_context_length"] = data.get(
                        "max_context_length", model.get("max_context_length", 8000)
                    )
                    model["updated_at"] = datetime.now().isoformat()
                    self._save_data("ai_models")
                    return jsonify({"success": True, "model": model})
            return jsonify({"error": "Model not found"}), 404

        @self.app.route("/api/ai-models/<model_id>", methods=["DELETE"])
        def delete_ai_model(model_id):
            """删除AI模型配置"""
            # 不能删除当前激活的配置
            if self.active_model_id == model_id:
                return jsonify({"error": "Cannot delete active model"}), 400
            self.ai_models = [m for m in self.ai_models if m["id"] != model_id]
            self._save_data("ai_models")
            return jsonify({"success": True})

        @self.app.route("/api/ai-models/<model_id>/apply", methods=["POST"])
        def apply_ai_model(model_id):
            """应用指定的AI模型配置"""
            if self._apply_ai_model(model_id):
                return jsonify(
                    {"success": True, "message": "Model applied successfully"}
                )
            return jsonify({"error": "Failed to apply model"}), 400

        @self.app.route("/api/ai-models/<model_id>/toggle", methods=["POST"])
        def toggle_ai_model(model_id):
            """启用/禁用AI模型配置"""
            for model in self.ai_models:
                if model["id"] == model_id:
                    model["enabled"] = not model.get("enabled", True)
                    self._save_data("ai_models")
                    return jsonify({"success": True, "enabled": model["enabled"]})
            return jsonify({"error": "Model not found"}), 404

        @self.app.route("/api/ai-models/<model_id>/clone", methods=["POST"])
        def clone_ai_model(model_id):
            """复制AI模型配置"""
            for model in self.ai_models:
                if model["id"] == model_id:
                    cloned = model.copy()
                    cloned["id"] = str(uuid.uuid4())
                    cloned["name"] = f"{model['name']} (复制)"
                    cloned["is_default"] = False
                    cloned["created_at"] = datetime.now().isoformat()
                    cloned["updated_at"] = datetime.now().isoformat()
                    self.ai_models.append(cloned)
                    self._save_data("ai_models")
                    return jsonify({"success": True, "model": cloned})
            return jsonify({"error": "Model not found"}), 404

        @self.app.route("/api/ai-models/<model_id>/test", methods=["POST"])
        def test_ai_model(model_id):
            """测试AI模型配置"""
            for model in self.ai_models:
                if model["id"] == model_id:
                    api_key = model.get("api_key", "")
                    base_url = model.get("base_url", "")
                    model_name = model.get("model", "")

                    # 验证必要参数
                    if not api_key:
                        return jsonify(
                            {"success": False, "message": "API Key 不能为空"}
                        )

                    if not base_url:
                        return jsonify(
                            {"success": False, "message": "Base URL 不能为空"}
                        )

                    if not model_name:
                        return jsonify(
                            {"success": False, "message": "模型名称不能为空"}
                        )

                    try:
                        import requests

                        url_base = base_url.rstrip("/")
                        if (
                            "/chat/completions" in url_base
                            or "/chatcompletion" in url_base
                        ):
                            url = url_base
                        else:
                            url = f"{url_base}/chat/completions"

                        headers = {
                            "Authorization": f"Bearer {api_key}",
                            "Content-Type": "application/json",
                        }
                        payload = {
                            "model": model_name,
                            "messages": [{"role": "user", "content": "Hello"}],
                            "max_tokens": 10,
                        }
                        resp = requests.post(
                            url, json=payload, headers=headers, timeout=30
                        )
                        resp.raise_for_status()
                        return jsonify({"success": True, "message": "连接测试成功"})
                    except requests.exceptions.Timeout:
                        return jsonify(
                            {
                                "success": False,
                                "message": "连接超时，请检查 Base URL 是否正确",
                            }
                        )
                    except requests.exceptions.ConnectionError:
                        return jsonify(
                            {
                                "success": False,
                                "message": "无法连接到服务器，请检查 Base URL 是否正确",
                            }
                        )
                    except requests.exceptions.HTTPError as e:
                        error_msg = f"HTTP错误: {e.response.status_code}"
                        try:
                            error_data = e.response.json()
                            if "error" in error_data:
                                error_msg += f" - {error_data['error'].get('message', '未知错误')}"
                        except:
                            pass
                        return jsonify({"success": False, "message": error_msg})
                    except Exception as e:
                        return jsonify(
                            {"success": False, "message": f"测试失败: {str(e)}"}
                        )
            return jsonify({"error": "Model not found"}), 404

        # ==================== Token 统计 API ====================
        @self.app.route("/api/tokens")
        def get_token_stats():
            date_range = request.args.get("dateRange", "today")

            # 从文件加载真实的 token 统计
            token_stats_file = os.path.join(self.data_dir, "token_stats.json")
            real_stats = {}
            if os.path.exists(token_stats_file):
                try:
                    with open(token_stats_file, "r", encoding="utf-8") as f:
                        real_stats = json.load(f)
                except:
                    real_stats = {}

            stats_data = {**self.token_stats, **real_stats}

            history = stats_data.get("history", [])
            today_str = datetime.now().strftime("%Y-%m-%d")

            # 根据 dateRange 筛选历史数据
            if date_range == "today":
                history = [h for h in history if h.get("date") == today_str]
            elif date_range == "7d":
                # 最近7天
                cutoff = (datetime.now() - timedelta(days=7)).strftime("%Y-%m-%d")
                history = [h for h in history if h.get("date", "") >= cutoff]
            elif date_range == "30d":
                # 最近30天
                cutoff = (datetime.now() - timedelta(days=30)).strftime("%Y-%m-%d")
                history = [h for h in history if h.get("date", "") >= cutoff]
            # 'all' 不做筛选，使用全部数据

            # 计算筛选后的统计
            today_input = 0
            today_output = 0
            for entry in history:
                if entry.get("date") == today_str:
                    today_input = entry.get("input", 0)
                    today_output = entry.get("output", 0)
                    break

            # 计算所选时间范围的总计
            if date_range == "today":
                period_total = today_input + today_output
                period_input = today_input
                period_output = today_output
            else:
                period_total = sum(
                    (h.get("input", 0) + h.get("output", 0)) for h in history
                )
                period_input = sum(h.get("input", 0) for h in history)
                period_output = sum(h.get("output", 0) for h in history)

            if (
                today_input == 0
                and today_output == 0
                and stats_data.get("today", 0) > 0
            ):
                today_input = stats_data.get("today", 0) // 2
                today_output = stats_data.get("today", 0) // 2

            estimated_cost = round(
                period_input * 0.000001 + period_output * 0.000008, 4
            )

            message_count = sum(
                len(s.get("messages", [])) for s in self.sessions.values()
            )
            active_sessions = len(
                [s for s in self.sessions.values() if len(s.get("messages", [])) > 0]
            )
            avg_tokens_per_msg = round(period_total / max(message_count, 1), 2)

            # 只返回所选时间范围的历史数据
            if date_range != "all":
                history = (
                    sorted(history, key=lambda x: x.get("date", ""))[-30:]
                    if date_range != "today"
                    else history
                )

            if len(history) >= 2:
                prev_entry = (
                    history[-2]
                    if date_range == "today"
                    else history[0]
                    if len(history) == 1
                    else history[-2]
                )
                prev_total = (
                    prev_entry.get("input", 0) + prev_entry.get("output", 0)
                    if prev_entry
                    else 0
                )
                token_change_val = (
                    (today_input + today_output) - prev_total
                    if date_range == "today"
                    else period_total
                    - prev_total * (len(history) - 1 if len(history) > 1 else 1)
                )
                token_change = (
                    f"+{token_change_val}"
                    if token_change_val >= 0
                    else f"{token_change_val}"
                )
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

        @self.app.route("/api/tokens/history")
        def get_token_history():
            return jsonify(self.token_stats.get("history", []))

        @self.app.route("/api/tokens/rankings")
        def get_token_rankings():
            """获取 Token 用量排行榜（使用真实数据）"""
            # 从文件加载真实统计数据
            token_stats_file = os.path.join(self.data_dir, "token_stats.json")
            real_stats = {}
            if os.path.exists(token_stats_file):
                try:
                    with open(token_stats_file, "r", encoding="utf-8") as f:
                        real_stats = json.load(f)
                except:
                    real_stats = {}

            # 从会话文件获取会话名称映射
            sessions_file = os.path.join(self.data_dir, "sessions.json")
            session_names = {}
            if os.path.exists(sessions_file):
                try:
                    with open(sessions_file, "r", encoding="utf-8") as f:
                        sessions_data = json.load(f)
                        for sid, session in sessions_data.items():
                            name = session.get("name", f"会话 {sid[:8]}")
                            session_names[sid] = name
                except:
                    pass

            # 合并内存中的会话名称
            for sid, session in self.sessions.items():
                if sid not in session_names:
                    name = session.get("name", f"会话 {sid[:8]}")
                    session_names[sid] = name

            # 会话排行（使用真实的token统计和会话名称）
            sessions_data = real_stats.get("sessions", {})
            session_rankings = []
            total_tokens = 0
            total_messages = 0

            for session_id, data in sessions_data.items():
                # 获取会话名称
                session_name = session_names.get(session_id, f"会话 {session_id[:8]}")
                total = data.get("total", 0)
                total_tokens += total

                # 计算消息数（如果有记录）
                message_count = data.get("message_count", 0)
                total_messages += message_count

                session_rankings.append(
                    {
                        "id": session_id,
                        "name": session_name,
                        "value": total,
                        "input": data.get("input", 0),
                        "output": data.get("output", 0),
                        "message_count": message_count,
                    }
                )
            session_rankings.sort(key=lambda x: x["value"], reverse=True)

            # 计算平均 Token/消息
            avg_tokens_per_message = 0
            if total_messages > 0:
                avg_tokens_per_message = round(total_tokens / total_messages, 2)

            # 模型排行
            model_rankings = [
                {
                    "name": self.ai_model or "MiniMax-Text-01",
                    "value": real_stats.get("today", 0),
                }
            ]

            # 用户排行（从会话统计中获取）
            user_rankings = []
            for session_id, data in sessions_data.items():
                session_type = data.get("type", "web")
                if session_type in ["private", "group"]:
                    user_rankings.append(
                        {
                            "name": session_names.get(session_id, session_id),
                            "value": data.get("total", 0),
                        }
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

        @self.app.route("/api/tokens/record", methods=["POST"])
        def record_token_usage():
            data = request.json
            tokens = data.get("tokens", 0)
            self.token_stats["today"] += tokens
            self.token_stats["month"] += tokens
            return jsonify({"success": True})

        # ==================== 日志 API ====================
        @self.app.route("/api/logs")
        def get_logs():
            level = request.args.get("level", "all")
            limit = request.args.get("limit", 100, type=int)

            logs = self.system_logs
            if level != "all":
                logs = [l for l in logs if l["level"] == level]

            return jsonify(logs[-limit:])

        @self.app.route("/api/logs", methods=["POST"])
        def add_log():
            data = request.json
            log = {
                "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                "level": data.get("level", "info"),
                "message": data.get("message", ""),
            }
            self.system_logs.append(log)
            if len(self.system_logs) > 1000:
                self.system_logs = self.system_logs[-1000:]
            # 持久化保存日志
            self._save_data("logs")
            return jsonify({"success": True})

        @self.app.route("/api/logs", methods=["DELETE"])
        def clear_logs():
            self.system_logs = []
            self._save_data("logs")
            return jsonify({"success": True})

        # ==================== 系统设置 API ====================
        @self.app.route("/api/settings")
        def get_settings():
            return jsonify(self.settings)

        @self.app.route("/api/settings", methods=["PUT"])
        def update_settings():
            data = request.json
            self.settings.update(data)
            self._save_data("settings")
            return jsonify({"success": True, "settings": self.settings})

        # ==================== 统计数据 API ====================
        @self.app.route("/api/stats")
        def get_stats():
            # 使用缓存避免频繁计算
            current_time = time.time()
            if (
                current_time - self._stats_cache_time
            ) < self._stats_cache_ttl and self._stats_cache:
                return jsonify(self._stats_cache)

            # 计算今日消息数（使用缓存后只需每5秒重新计算一次）
            today_messages = 0
            total_messages = 0
            today_str = datetime.now().strftime("%Y-%m-%d")
            today_active_users = set()  # 今日活跃用户（去重）

            # 统计Web会话消息
            for session in self.sessions.values():
                messages = session.get("messages", [])
                msg_count = len(messages)
                total_messages += msg_count
                session_type = session.get("type", "web")

                # 只检查最近的消息是否在今天
                for msg in messages[-100:]:  # 只检查最近100条
                    timestamp = msg.get("timestamp")
                    if timestamp:
                        # timestamp 可能是浮点数(时间戳)或字符串
                        if isinstance(timestamp, (int, float)):
                            msg_date = datetime.fromtimestamp(timestamp).strftime(
                                "%Y-%m-%d"
                            )
                            if msg_date == today_str:
                                today_messages += 1
                                # 记录活跃用户
                                if session_type == "web":
                                    today_active_users.add(
                                        f"web:{session.get('id', '')}"
                                    )
                        elif isinstance(timestamp, str) and today_str in timestamp:
                            today_messages += 1
                            if session_type == "web":
                                today_active_users.add(f"web:{session.get('id', '')}")

            # 从QQ消息文件统计今日活跃用户和消息
            try:
                import os
                import json

                qq_data_dir = os.path.join(self.base_dir, "data", "qq")

                # 统计QQ私聊今日消息和活跃用户
                private_dir = os.path.join(qq_data_dir, "private")
                if os.path.exists(private_dir):
                    for filename in os.listdir(private_dir):
                        if filename.endswith(".json"):
                            user_id = filename.replace(".json", "")
                            file_path = os.path.join(private_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    for msg in messages:
                                        timestamp = msg.get("timestamp", "")
                                        if today_str in str(timestamp):
                                            today_messages += 1
                                            today_active_users.add(
                                                f"qq_private:{user_id}"
                                            )
                            except Exception as e:
                                _log.warning(f"读取QQ私聊文件失败 {filename}: {e}")

                # 统计QQ群今日消息和活跃用户
                group_dir = os.path.join(qq_data_dir, "group")
                if os.path.exists(group_dir):
                    for filename in os.listdir(group_dir):
                        if filename.endswith(".json"):
                            group_id = filename.replace(".json", "")
                            file_path = os.path.join(group_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    for msg in messages:
                                        timestamp = msg.get("timestamp", "")
                                        if today_str in str(timestamp):
                                            today_messages += 1
                                            today_active_users.add(
                                                f"qq_group:{group_id}"
                                            )
                            except Exception as e:
                                _log.warning(f"读取QQ群文件失败 {filename}: {e}")
            except Exception as e:
                _log.error(f"统计QQ消息失败: {e}")

            # 计算运行时间
            uptime_seconds = int(time.time() - self.start_time)
            uptime = self._format_uptime(uptime_seconds)

            # 获取内存使用（如果可能）
            try:
                import psutil

                process = psutil.Process()
                memory_usage = round(process.memory_info().rss / 1024 / 1024, 1)
            except:
                memory_usage = 42  # 默认值

            # 计算AI调用次数（从token_stats中统计今日消息数作为AI调用次数）
            ai_calls = 0
            try:
                history = self.token_stats.get("history", [])
                for entry in history:
                    if entry.get("date") == today_str:
                        ai_calls = entry.get("message_count", 0)
                        break
            except:
                ai_calls = today_messages  # 如果没有详细记录，使用今日消息数

            # 统计文件传输次数（从工作区文件统计）
            file_transfers = 0
            try:
                # 从 data/workspaces 目录统计（工作区文件）
                workspaces_dir = os.path.join(self.base_dir, "data", "workspaces")
                if os.path.exists(workspaces_dir):
                    for session_folder in os.listdir(workspaces_dir):
                        session_workspace = os.path.join(workspaces_dir, session_folder)
                        if os.path.isdir(
                            session_workspace
                        ) and not session_folder.startswith("_"):
                            # 统计今日上传的文件
                            for filename in os.listdir(session_workspace):
                                if filename.startswith("_"):  # 跳过元数据文件
                                    continue
                                file_path = os.path.join(session_workspace, filename)
                                try:
                                    mtime = os.path.getmtime(file_path)
                                    file_date = datetime.fromtimestamp(mtime).strftime(
                                        "%Y-%m-%d"
                                    )
                                    if file_date == today_str:
                                        file_transfers += 1
                                except:
                                    pass

                # 从 static/files 目录统计（通过 send_file 发送的文件）
                static_files_dir = os.path.join(self.static_folder, "files")
                if os.path.exists(static_files_dir):
                    for filename in os.listdir(static_files_dir):
                        file_path = os.path.join(static_files_dir, filename)
                        try:
                            mtime = os.path.getmtime(file_path)
                            file_date = datetime.fromtimestamp(mtime).strftime(
                                "%Y-%m-%d"
                            )
                            if file_date == today_str:
                                file_transfers += 1
                        except:
                            pass
            except Exception as e:
                _log.warning(f"统计文件传输失败: {e}")

            kb_docs_count = 0
            if KNOWLEDGE_MANAGER_AVAILABLE:
                try:
                    km = get_knowledge_manager()
                    bases = km.list_knowledge_bases()
                    for kb in bases:
                        kb_docs_count += len(kb.documents)
                except:
                    pass

            stats = {
                "today_messages": today_messages,
                "total_messages": total_messages,
                "active_sessions": len(self.sessions),
                "token_usage": self.token_stats.get("today", 0),
                "kb_docs": kb_docs_count,
                "memory_usage": memory_usage,
                "qq_connected": True,
                "ai_service_status": "normal" if self.ai_client else "not_configured",
                "platform_count": 1,  # QQ平台
                "uptime": uptime,
                "today_active_users": len(today_active_users),
                "ai_calls": ai_calls,
                "file_transfers": file_transfers,
                "avg_response_time": "1.2",
            }

            # 更新缓存
            self._stats_cache = stats
            self._stats_cache_time = current_time

            return jsonify(stats)

        @self.app.route("/api/stats/messages")
        def get_message_stats():
            """获取消息历史统计数据"""
            period = request.args.get("period", "day")  # day, week, month

            from collections import defaultdict
            from datetime import datetime, timedelta

            now = datetime.now()
            stats = defaultdict(int)

            if period == "day":
                # 过去24小时，按小时统计
                for i in range(24):
                    hour_time = now - timedelta(hours=i)
                    hour_key = hour_time.strftime("%H:00")
                    stats[hour_key] = 0

                for session in self.sessions.values():
                    for msg in session.get("messages", []):
                        try:
                            msg_time = datetime.fromisoformat(
                                msg.get("timestamp", "").replace("Z", "+00:00")
                            )
                            if now - msg_time <= timedelta(hours=24):
                                hour_key = msg_time.strftime("%H:00")
                                stats[hour_key] += 1
                        except:
                            pass

                # 按时间顺序排列
                sorted_stats = sorted(stats.items(), key=lambda x: x[0])
                return jsonify(
                    {
                        "labels": [item[0] for item in sorted_stats],
                        "values": [item[1] for item in sorted_stats],
                    }
                )

            elif period == "week":
                # 过去7天，按天统计
                for i in range(7):
                    day_time = now - timedelta(days=i)
                    day_key = day_time.strftime("%m/%d")
                    stats[day_key] = 0

                for session in self.sessions.values():
                    for msg in session.get("messages", []):
                        try:
                            msg_time = datetime.fromisoformat(
                                msg.get("timestamp", "").replace("Z", "+00:00")
                            )
                            if now - msg_time <= timedelta(days=7):
                                day_key = msg_time.strftime("%m/%d")
                                stats[day_key] += 1
                        except:
                            pass

                sorted_stats = sorted(
                    stats.items(), key=lambda x: datetime.strptime(x[0], "%m/%d")
                )
                return jsonify(
                    {
                        "labels": [item[0] for item in sorted_stats],
                        "values": [item[1] for item in sorted_stats],
                    }
                )

            elif period == "month":
                # 过去30天，按天统计
                for i in range(30):
                    day_time = now - timedelta(days=i)
                    day_key = day_time.strftime("%m/%d")
                    stats[day_key] = 0

                for session in self.sessions.values():
                    for msg in session.get("messages", []):
                        try:
                            msg_time = datetime.fromisoformat(
                                msg.get("timestamp", "").replace("Z", "+00:00")
                            )
                            if now - msg_time <= timedelta(days=30):
                                day_key = msg_time.strftime("%m/%d")
                                stats[day_key] += 1
                        except:
                            pass

                sorted_stats = sorted(
                    stats.items(), key=lambda x: datetime.strptime(x[0], "%m/%d")
                )
                return jsonify(
                    {
                        "labels": [item[0] for item in sorted_stats],
                        "values": [item[1] for item in sorted_stats],
                    }
                )

            return jsonify({"labels": [], "values": []})

        @self.app.route("/api/stats/platforms")
        def get_platform_stats():
            """获取各平台消息统计数据"""
            from collections import defaultdict

            platform_stats = defaultdict(int)
            platform_stats["QQ群消息"] = 0
            platform_stats["QQ私聊"] = 0
            platform_stats["Web会话"] = 0

            # 从QQ消息文件统计（QQ消息保存在 data/qq 目录）
            try:
                import os
                import json

                qq_data_dir = os.path.join(self.base_dir, "data", "qq")

                # 统计QQ私聊消息
                private_dir = os.path.join(qq_data_dir, "private")
                if os.path.exists(private_dir):
                    for filename in os.listdir(private_dir):
                        if filename.endswith(".json"):
                            file_path = os.path.join(private_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    # 排除system消息
                                    non_system_msgs = [
                                        m for m in messages if m.get("role") != "system"
                                    ]
                                    platform_stats["QQ私聊"] += len(non_system_msgs)
                            except Exception as e:
                                _log.warning(f"读取QQ私聊文件失败 {filename}: {e}")

                # 统计QQ群消息
                group_dir = os.path.join(qq_data_dir, "group")
                if os.path.exists(group_dir):
                    for filename in os.listdir(group_dir):
                        if filename.endswith(".json"):
                            file_path = os.path.join(group_dir, filename)
                            try:
                                with open(file_path, "r", encoding="utf-8") as f:
                                    messages = json.load(f)
                                    # 排除system消息
                                    non_system_msgs = [
                                        m for m in messages if m.get("role") != "system"
                                    ]
                                    platform_stats["QQ群消息"] += len(non_system_msgs)
                            except Exception as e:
                                _log.warning(f"读取QQ群文件失败 {filename}: {e}")

            except Exception as e:
                _log.error(f"统计QQ消息失败: {e}")

            # 从Web会话统计（只统计web类型，避免重复统计QQ）
            for session in self.sessions.values():
                session_type = session.get("type", "web")
                msg_count = len(session.get("messages", []))

                if session_type == "web":
                    platform_stats["Web会话"] += msg_count

            # 转换为列表格式
            result = []
            colors = {"QQ群消息": "#667eea", "QQ私聊": "#4facfe", "Web会话": "#43e97b"}

            for name, value in platform_stats.items():
                if value > 0:  # 只返回有数据的平台
                    result.append(
                        {
                            "name": name,
                            "value": value,
                            "itemStyle": {"color": colors.get(name, "#999")},
                        }
                    )

            # 如果没有数据，返回默认值
            if not result:
                result = [
                    {"name": "QQ群消息", "value": 0, "itemStyle": {"color": "#667eea"}},
                    {"name": "QQ私聊", "value": 0, "itemStyle": {"color": "#4facfe"}},
                    {"name": "Web会话", "value": 0, "itemStyle": {"color": "#43e97b"}},
                ]

            return jsonify(result)

        # ==================== 配置文件 API (兼容旧版) ====================
        @self.app.route("/api/config")
        def get_config():
            try:
                with open("config.ini", "r", encoding="utf-8") as f:
                    content = f.read()
                return jsonify({"content": content})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        @self.app.route("/api/config", methods=["POST"])
        def save_config():
            data = request.json
            try:
                with open("config.ini", "w", encoding="utf-8") as f:
                    f.write(data.get("content", ""))
                return jsonify({"success": True})
            except Exception as e:
                return jsonify({"error": str(e)}), 500

        # ==================== Skills API ====================
        @self.app.route("/api/skills")
        def get_skills():
            """获取所有 Skills 配置"""
            return jsonify(self.skills_config)

        @self.app.route("/api/skills", methods=["POST"])
        def create_skill():
            """创建新 Skill"""
            data = request.json
            skill_name = data.get("name", "")
            skill = {
                "id": str(uuid.uuid4()),
                "name": skill_name,
                "description": data.get("description", ""),
                "aliases": data.get("aliases", []),
                "enabled": data.get("enabled", True),
                "parameters": data.get("parameters", {}),
            }
            self.skills_config.append(skill)
            self._save_data("skills")

            # 自动创建 Skill 存储空间
            storage_created = False
            try:
                from nbot.core.skills_manager import get_skills_storage_manager

                manager = get_skills_storage_manager()
                if not manager.skill_exists(skill_name):
                    storage = manager.create_skill(
                        skill_name,
                        {
                            "name": skill_name,
                            "description": data.get("description", ""),
                            "config": skill,
                        },
                    )

                    # 保存用户提供的 SKILL.md 内容（可选）
                    skill_md_content = data.get("skill_md", "")
                    if skill_md_content:
                        storage.save_skill_md(skill_md_content)
                        _log.info(f"已保存用户提供的 SKILL.md 内容: {skill_name}")

                    _log.info(f"已创建 Skill 存储空间: {skill_name}")
                    storage_created = True
            except Exception as e:
                _log.error(f"创建 Skill 存储空间失败: {e}")

            return jsonify(
                {"success": True, "skill": skill, "storage_created": storage_created}
            )

        @self.app.route("/api/skills/<skill_id>", methods=["PUT"])
        def update_skill(skill_id):
            """更新 Skill"""
            data = request.json
            for skill in self.skills_config:
                if skill["id"] == skill_id:
                    old_name = skill["name"]
                    new_name = data.get("name", skill["name"])

                    skill["name"] = new_name
                    skill["description"] = data.get("description", skill["description"])
                    skill["aliases"] = data.get("aliases", skill["aliases"])
                    skill["enabled"] = data.get("enabled", skill["enabled"])
                    skill["parameters"] = data.get("parameters", skill["parameters"])
                    self._save_data("skills")

                    # 更新 Skill 存储空间
                    try:
                        from nbot.core.skills_manager import get_skills_storage_manager

                        manager = get_skills_storage_manager()

                        # 如果名称改变了，重命名存储空间
                        if old_name != new_name:
                            if manager.skill_exists(old_name):
                                # 创建新的存储空间并复制内容
                                new_storage = manager.create_skill(
                                    new_name,
                                    {
                                        "name": new_name,
                                        "description": data.get("description", ""),
                                        "config": skill,
                                    },
                                )
                                old_storage = manager.get_skill_storage(old_name)
                                # 复制脚本
                                for script in old_storage.list_scripts():
                                    content = old_storage.load_script(script)
                                    if content:
                                        new_storage.save_script(script, content)
                                # 复制资源
                                for resource in old_storage.list_resources():
                                    content = old_storage.load_resource(resource)
                                    if content:
                                        new_storage.save_resource(resource, content)
                                # 删除旧的存储空间
                                manager.delete_skill(old_name)
                                _log.info(
                                    f"Skill 存储空间已重命名: {old_name} -> {new_name}"
                                )
                        else:
                            # 只是更新配置
                            storage = manager.get_skill_storage(new_name)
                            storage.save_config(
                                {
                                    "name": new_name,
                                    "description": data.get("description", ""),
                                    "config": skill,
                                }
                            )
                    except Exception as e:
                        _log.error(f"更新 Skill 存储空间失败: {e}")

                    return jsonify({"success": True, "skill": skill})
            return jsonify({"error": "Skill not found"}), 404

        @self.app.route("/api/skills/<skill_id>", methods=["DELETE"])
        def delete_skill(skill_id):
            """删除 Skill"""
            # 获取要删除的 Skill 名称
            skill_to_delete = None
            for skill in self.skills_config:
                if skill["id"] == skill_id:
                    skill_to_delete = skill
                    break

            skill_name = skill_to_delete.get("name") if skill_to_delete else None

            # 删除配置
            self.skills_config = [s for s in self.skills_config if s["id"] != skill_id]
            self._save_data("skills")

            # 同时删除存储空间
            if skill_name:
                try:
                    from nbot.core.skills_manager import get_skills_storage_manager

                    manager = get_skills_storage_manager()
                    if manager.skill_exists(skill_name):
                        manager.delete_skill(skill_name)
                        _log.info(f"已删除 Skill 存储空间: {skill_name}")
                except Exception as e:
                    _log.error(f"删除 Skill 存储空间失败: {e}")

            return jsonify({"success": True})

        @self.app.route("/api/skills/<skill_id>/toggle", methods=["POST"])
        def toggle_skill(skill_id):
            """切换 Skill 启用状态"""
            for skill in self.skills_config:
                if skill["id"] == skill_id:
                    skill["enabled"] = not skill.get("enabled", True)
                    self._save_data("skills")
                    return jsonify({"success": True, "enabled": skill["enabled"]})
            return jsonify({"error": "Skill not found"}), 404

        # ==================== Tools API ====================
        @self.app.route("/api/tools")
        def get_tools():
            """获取所有 Tools 配置（包括内置的工具和注册的工具）"""
            # 基于 name 去重，最终只保留每个 name 的一个条目
            seen_names: set = set()
            unique_tools: List[Dict] = []

            # 先加入 self.tools_config
            for t in self.tools_config:
                name = t.get("name", "")
                if name not in seen_names:
                    seen_names.add(name)
                    unique_tools.append(t)

            # 补充内置工具（只补充 self.tools_config 中没有的）
            try:
                from nbot.services.tools import (
                    TOOL_DEFINITIONS,
                    WORKSPACE_TOOL_DEFINITIONS,
                )

                for tool_def in TOOL_DEFINITIONS + WORKSPACE_TOOL_DEFINITIONS:
                    func = tool_def.get("function", {})
                    name = func.get("name", "")
                    if name and name not in seen_names:
                        unique_tools.append(
                            {
                                "id": f"_builtin_{name}",
                                "name": name,
                                "description": func.get("description", ""),
                                "enabled": True,
                                "parameters": func.get("parameters", {}),
                                "_builtin": True,
                            }
                        )
                        seen_names.add(name)
            except Exception as e:
                _log.error(f"Failed to load built-in tools: {e}")

            # 补充注册的工具（通过 @register_tool 装饰器注册的工具，如 skill_list, skill_view, skill_read, skill_get_info）
            try:
                from nbot.services.tool_registry import (
                    get_all_tool_definitions as get_registered_tools,
                )

                registered_tools = get_registered_tools()
                for tool_def in registered_tools:
                    func = tool_def.get("function", {})
                    name = func.get("name", "")
                    if name and name not in seen_names:
                        unique_tools.append(
                            {
                                "id": f"_builtin_{name}",
                                "name": name,
                                "description": func.get("description", ""),
                                "enabled": True,
                                "parameters": func.get("parameters", {}),
                                "_builtin": True,
                            }
                        )
                        seen_names.add(name)
                _log.info(
                    f"[Tools] 已加载 {len(registered_tools)} 个注册的工具: {[t.get('function', {}).get('name') for t in registered_tools]}"
                )
            except Exception as e:
                _log.error(f"Failed to load registered tools: {e}")

            return jsonify(unique_tools)

        @self.app.route("/api/tools", methods=["POST"])
        def create_tool():
            """创建新 Tool"""
            data = request.json
            tool = {
                "id": str(uuid.uuid4()),
                "name": data.get("name", ""),
                "description": data.get("description", ""),
                "enabled": data.get("enabled", True),
                "parameters": data.get("parameters", {}),
            }
            self.tools_config.append(tool)
            self._save_data("tools")
            return jsonify({"success": True, "tool": tool})

        @self.app.route("/api/tools/<tool_id>", methods=["PUT"])
        def update_tool(tool_id):
            """更新 Tool"""
            data = request.json
            for tool in self.tools_config:
                if tool["id"] == tool_id:
                    tool["name"] = data.get("name", tool["name"])
                    tool["description"] = data.get("description", tool["description"])
                    tool["enabled"] = data.get("enabled", tool["enabled"])
                    tool["parameters"] = data.get("parameters", tool["parameters"])
                    self._save_data("tools")
                    return jsonify({"success": True, "tool": tool})
            return jsonify({"error": "Tool not found"}), 404

        @self.app.route("/api/tools/<tool_id>", methods=["DELETE"])
        def delete_tool(tool_id):
            """删除 Tool"""
            if tool_id.startswith("_builtin_"):
                return jsonify({"error": "Cannot delete built-in tool"}), 400
            self.tools_config = [t for t in self.tools_config if t["id"] != tool_id]
            self._save_data("tools")
            return jsonify({"success": True})

        @self.app.route("/api/tools/<tool_id>/toggle", methods=["POST"])
        def toggle_tool(tool_id):
            """切换 Tool 启用状态"""
            if tool_id.startswith("_builtin_"):
                return jsonify({"error": "Cannot toggle built-in tool"}), 400
            for tool in self.tools_config:
                if tool["id"] == tool_id:
                    tool["enabled"] = not tool.get("enabled", True)
                    self._save_data("tools")
                    return jsonify({"success": True, "enabled": tool["enabled"]})
            return jsonify({"error": "Tool not found"}), 404

    def _register_socket_events(self):
        """注册 WebSocket 事件"""

        @self.socketio.on("connect")
        def handle_connect():
            user_id = request.args.get("user_id", "anonymous")
            self.web_users[request.sid] = user_id
            _log.info(f"Web client connected: {user_id}")

        @self.socketio.on("disconnect")
        def handle_disconnect():
            user_id = self.web_users.pop(request.sid, None)
            session_id = self.active_connections.pop(request.sid, None)
            if session_id:
                leave_room(session_id)
            _log.info(f"Web client disconnected: {user_id}")

        @self.socketio.on("join_session")
        def handle_join_session(data):
            session_id = data.get("session_id")
            if session_id in self.sessions:
                join_room(session_id)
                self.active_connections[request.sid] = session_id
                self.socketio.emit(
                    "joined_session", {"session_id": session_id}, room=request.sid
                )
            else:
                _log.warning(f"Client tried to join non-existent session: {session_id}")

        @self.socketio.on("leave_session")
        def handle_leave_session():
            session_id = self.active_connections.pop(request.sid, None)
            if session_id:
                leave_room(session_id)

        @self.socketio.on("send_message")
        def handle_send_message(data):
            try:
                session_id = data.get("session_id")
                content = data.get("content", "")
                sender = data.get("sender", "web_user")
                attachments = data.get("attachments", [])

                # 处理附件信息
                attachment_info = ""
                if attachments and isinstance(attachments, list):
                    for att in attachments:
                        if isinstance(att, dict):
                            att_name = att.get("name", "unknown")
                            att_type = att.get("type", "")
                            attachment_info += f"\n[附件: {att_name}, 类型: {att_type}]"

                # 记录日志
                preview = content[:50] if content else ""
                self.log_message(
                    "info",
                    f"收到Web消息 from {sender}: {preview}... {len(attachments)}个附件",
                )
                _log.info(
                    f"收到Web消息: session={session_id}, sender={sender}, attachments={len(attachments)}"
                )

                # 先从文件加载会话
                sessions_file = os.path.join(self.data_dir, "sessions.json")
                if os.path.exists(sessions_file):
                    try:
                        with open(sessions_file, "r", encoding="utf-8") as f:
                            file_sessions = json.load(f)
                        # 合并到内存
                        for sid, sess in file_sessions.items():
                            if sid not in self.sessions:
                                self.sessions[sid] = sess
                    except:
                        pass

                if session_id not in self.sessions:
                    self.socketio.emit(
                        "error", {"message": "Session not found"}, room=request.sid
                    )
                    return

                # 检查是否是命令（以 / 开头）
                is_command = False
                matched_handler = None
                if content and content.startswith("/"):
                    try:
                        # 导入命令处理模块（确保所有命令已注册）
                        import nbot.commands
                        from nbot.commands import command_handlers
                        import asyncio

                        _log.info(
                            f"检查命令: {content}, 可用命令数: {len(command_handlers)}, 命令列表: {list(command_handlers.keys())[:10]}..."
                        )

                        # 检查是否匹配任何命令
                        for commands, handler in command_handlers.items():
                            for cmd in commands:
                                if content.startswith(cmd):
                                    _log.info(f"匹配到命令: {cmd}")
                                    is_command = True
                                    matched_handler = handler
                                    break
                            if is_command:
                                break

                        if not is_command:
                            _log.warning(f"未匹配到任何命令: {content}")

                    except ImportError as e:
                        _log.warning(f"无法导入命令处理模块: {e}")
                    except Exception as e:
                        _log.error(f"命令处理错误: {e}", exc_info=True)

                # 构建消息（保留原始内容和附件元数据，不保存文件内容）
                temp_id = data.get("tempId")  # 获取前端发送的临时ID

                # 处理附件：只保留元数据，不保存文件内容（data字段）
                processed_attachments = []
                if attachments and isinstance(attachments, list):
                    for att in attachments:
                        if isinstance(att, dict):
                            # 保留元数据和URL，用于预览
                            processed_att = {
                                "name": att.get("name", "unknown"),
                                "type": att.get("type", ""),
                                "size": att.get("size", 0),
                                "url": att.get("url", att.get("path", "")),
                                "content": att.get("content"),  # 文本内容
                                "preview": att.get("preview")
                                if att.get("type", "").startswith("image/")
                                else att.get("url", att.get("path", "")),
                            }
                            processed_attachments.append(processed_att)

                message = {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": content,  # 只保留原始文本内容
                    "timestamp": datetime.now().isoformat(),
                    "sender": sender,
                    "source": "web",
                    "attachments": processed_attachments,
                    "tempId": temp_id,  # 保留临时ID以便前端替换
                    "session_id": session_id,  # 添加会话ID以便前端识别
                }

                self.sessions[session_id]["messages"].append(message)
                # 同时记录到新消息模块
                if MESSAGE_MODULE_AVAILABLE and message_manager:
                    message_manager.add_web_message(
                        session_id,
                        create_message(
                            "user",
                            content,
                            sender=sender,
                            source="web",
                            session_id=session_id,
                            attachments=processed_attachments,
                            metadata={"tempId": temp_id},
                        ),
                    )

                # 使用 socketio.emit 替代 emit，确保广播到 room
                self.socketio.emit("new_message", message, room=session_id)

                # 保存会话到磁盘
                self._save_data("sessions")

                # 如果是命令，执行命令处理
                if is_command and matched_handler:
                    # 创建 Web 消息适配器，使用纯数字 user_id（确保唯一性）
                    import hashlib

                    web_user_id = str(
                        int(hashlib.md5(session_id.encode()).hexdigest(), 16)
                    )[:10]
                    msg_adapter = WebMessageAdapter(
                        content, web_user_id, session_id, self
                    )

                    # 使用 socketio 的 background task 执行命令
                    # 注意：Web 端始终使用 is_group=True，这样命令会使用 msg.reply() 而不是 bot.api.post_private_msg
                    def run_command():
                        import asyncio

                        # 临时替换全局的 bot 变量，让命令中的 bot.api 调用生效
                        original_bot = None
                        try:
                            import nbot.commands as cmd_module

                            original_bot = getattr(cmd_module, "bot", None)
                            # 使用 msg_adapter 的模拟 bot
                            cmd_module.bot = msg_adapter.bot
                            _log.info(f"临时替换 bot 变量为 Web 模拟对象")

                            asyncio.run(matched_handler(msg_adapter, is_group=True))
                        except Exception as e:
                            _log.error(f"命令执行错误: {e}", exc_info=True)
                            # 发送错误消息给用户
                            error_msg = f"❌ 命令执行出错: {str(e)}"
                            try:
                                asyncio.run(msg_adapter.reply(text=error_msg))
                            except Exception as reply_error:
                                _log.error(f"发送错误消息失败: {reply_error}")
                        finally:
                            # 恢复原始的 bot 变量
                            if original_bot:
                                cmd_module.bot = original_bot
                                _log.info(f"恢复原始 bot 变量")

                    self.socketio.start_background_task(run_command)
                else:
                    # 触发 AI 回复（传递附件信息和用户消息ID）
                    # 只使用原始内容，附件信息通过卡片显示
                    ai_content = content
                    # 使用 temp_id 作为 parent_message_id，这样前端可以正确关联进度卡片
                    # 因为前端先用 temp_id 创建了本地消息
                    parent_msg_id = temp_id if temp_id else message["id"]
                    self._trigger_ai_response(
                        session_id, ai_content, sender, attachments, parent_msg_id
                    )

            except Exception as e:
                _log.error(f"处理消息时出错: {e}", exc_info=True)
                # 发送错误消息给客户端
                self.socketio.emit(
                    "error", {"message": f"消息处理失败: {str(e)}"}, room=request.sid
                )

        @self.socketio.on("typing")
        def handle_typing(data):
            session_id = data.get("session_id")
            emit(
                "user_typing",
                {"sender": self.web_users.get(request.sid)},
                room=session_id,
            )

    def _trigger_ai_response(
        self,
        session_id: str,
        user_content: str,
        sender: str,
        attachments=None,
        parent_message_id=None,
    ):
        """触发 AI 回复（支持附件）"""
        # 强制转换为列表
        if not attachments or not isinstance(attachments, list):
            attachments = []

        session = self.sessions.get(session_id)
        if not session:
            _log.warning(f"Session not found: {session_id}")
            self.log_message("warning", f"Session not found: {session_id}")
            return

        # 检查是否有图片附件
        has_image = False
        try:
            for att in attachments:
                if isinstance(att, dict):
                    att_type = att.get("type", "")
                    if (
                        att_type
                        and hasattr(att_type, "startswith")
                        and att_type.startswith("image/")
                    ):
                        has_image = True
                        break
        except:
            attachments = []

        self.log_message(
            "info",
            f"开始生成AI回复 for session {session_id[:8]}... (附件: {len(attachments)}, 图片: {has_image})",
        )

        # 知识库检索
        knowledge_retrieved = False
        knowledge_text = ""
        _log.info(f"[Knowledge] 知识库开关状态: {self.settings.get('knowledge', True)}")
        if self.settings.get("knowledge", True):
            try:
                _log.info(f"[Knowledge] 开始检索，查询: {user_content[:50]}...")
                knowledge_text = self._retrieve_knowledge(user_content)
                _log.info(f"[Knowledge] 检索结果长度: {len(knowledge_text)} 字符")
                if knowledge_text:
                    knowledge_retrieved = True
                    _log.info(f"[Knowledge] 知识库检索成功")
            except Exception as e:
                _log.error(f"[Knowledge] 知识库检索失败: {e}")

        def get_response():
            try:
                # 使用深拷贝避免修改原始消息
                import copy

                messages_for_ai = copy.deepcopy(session["messages"])

                # 检测"继续"意图
                tool_call_history = None
                if messages_for_ai and user_content.strip() in [
                    "继续",
                    "继续执行",
                    "continue",
                ]:
                    last_msg = messages_for_ai[-1]
                    if last_msg.get("can_continue") and last_msg.get(
                        "tool_call_history"
                    ):
                        tool_call_history = last_msg["tool_call_history"]
                        messages_for_ai.pop()  # 移除继续消息
                        messages_for_ai.pop()  # 移除不完整生成标记
                        _log.info(
                            f"[Continue] 检测到继续请求，恢复 {len(tool_call_history)} 条工具调用记录"
                        )

                # 限制历史长度
                MAX_HISTORY = 20
                MAX_TOTAL_CHARS = 30000  # 限制消息总字符数（大约）

                # 如果消息太多，只保留最近的
                if len(messages_for_ai) > MAX_HISTORY:
                    messages_for_ai = [messages_for_ai[0]] + messages_for_ai[
                        -MAX_HISTORY:
                    ]

                # 计算消息总字符数
                total_chars = sum(
                    len(str(m.get("content", ""))) for m in messages_for_ai
                )
                if total_chars > MAX_TOTAL_CHARS:
                    # 保留系统消息和最近的消息
                    system_msg = (
                        messages_for_ai[0]
                        if messages_for_ai
                        and messages_for_ai[0].get("role") == "system"
                        else None
                    )
                    recent_msgs = (
                        messages_for_ai[-MAX_HISTORY:] if messages_for_ai else []
                    )
                    if system_msg:
                        messages_for_ai = [system_msg] + recent_msgs
                    else:
                        messages_for_ai = recent_msgs
                    _log.warning(
                        f"[Context] 消息过长，已压缩: 总字符数 {total_chars} -> {MAX_TOTAL_CHARS}"
                    )

                # 添加知识库内容到系统消息
                if knowledge_text and messages_for_ai:
                    if messages_for_ai[0].get("role") == "system":
                        messages_for_ai[0]["content"] += f"\n\n{knowledge_text}"
                    else:
                        messages_for_ai.insert(
                            0, {"role": "system", "content": knowledge_text}
                        )

                # 检查附件并处理
                image_urls = []
                file_contents = []

                # 支持的文本文件MIME类型
                TEXT_MIME_TYPES = [
                    "text/plain",
                    "application/json",
                    "application/xml",
                    "text/csv",
                    "text/yaml",
                    "application/x-yaml",
                    "application/yaml",
                    "text/x-python",
                    "text/x-java",
                    "text/x-c",
                    "text/x-c++",
                    "text/html",
                    "text/css",
                    "text/javascript",
                    "application/javascript",
                    "text/markdown",
                    "text/x-markdown",
                    "application/x-httpd-php",
                    "text/x-php",
                ]

                # 根据扩展名判断是否为文本文件
                TEXT_EXTENSIONS = [
                    ".txt",
                    ".json",
                    ".xml",
                    ".csv",
                    ".yaml",
                    ".yml",
                    ".py",
                    ".java",
                    ".c",
                    ".cpp",
                    ".h",
                    ".hpp",
                    ".html",
                    ".css",
                    ".js",
                    ".ts",
                    ".jsx",
                    ".tsx",
                    ".md",
                    ".markdown",
                    ".php",
                    ".rb",
                    ".go",
                    ".rs",
                    ".sh",
                    ".bash",
                    ".sql",
                    ".ini",
                    ".cfg",
                    ".conf",
                    ".log",
                    ".env",
                    ".properties",
                    ".toml",
                ]

                # 创建进度卡片（所有消息都创建，立即显示"AI 正在思考..."）
                progress_card = None
                todo_card = None
                has_attachments = (
                    attachments
                    and isinstance(attachments, list)
                    and len(attachments) > 0
                )

                if PROGRESS_CARD_AVAILABLE and progress_card_manager and self.socketio:
                    progress_card = progress_card_manager.create_card(
                        session_id=session_id,
                        parent_message_id=parent_message_id,
                        max_iterations=50,
                    )
                    _log.info(f"[ProgressCard] 创建进度卡片: {progress_card.card_id}")
                    # 立即显示"AI 正在思考..."
                    from nbot.core.progress_card import StepType

                    progress_card.update(StepType.THINKING, "AI 正在思考...")

                # 创建 Todo 卡片（用于显示待办事项）
                if TODO_CARD_AVAILABLE and todo_card_manager and self.socketio:
                    todo_card = todo_card_manager.create_card(
                        session_id=session_id, parent_message_id=parent_message_id
                    )
                    _log.info(f"[TodoCard] 创建 Todo 卡片: {todo_card.card_id}")

                # 创建停止事件
                stop_event = threading.Event()
                self.stop_events[session_id] = stop_event

                if has_attachments:
                    # 更新进度：开始处理附件
                    if progress_card:
                        progress_card.update(
                            StepType.UPLOAD, f"正在处理 {len(attachments)} 个附件..."
                        )

                    for att in attachments:
                        if isinstance(att, dict):
                            att_type = att.get("type", "")
                            att_data = att.get("data", "")
                            att_path = att.get("path", "")
                            att_name = att.get("name", "unknown")

                            if isinstance(att_type, str):
                                # 图片附件 - 优先使用 data URL，其次使用文件路径
                                if att_type.startswith("image/"):
                                    # 更新进度：正在识别图片
                                    if progress_card:
                                        progress_card.update(
                                            StepType.IMAGE, f"正在识别图片: {att_name}"
                                        )

                                    image_loaded = False
                                    if att_data:
                                        image_urls.append(att_data)
                                        image_loaded = True
                                    elif att_path:
                                        # 尝试读取服务器上的图片文件
                                        try:
                                            import os

                                            file_path = os.path.join(
                                                self.static_folder,
                                                att_path.replace("/static/", ""),
                                            )
                                            if os.path.exists(file_path):
                                                with open(file_path, "rb") as f:
                                                    import base64

                                                    b64_data = base64.b64encode(
                                                        f.read()
                                                    ).decode("utf-8")
                                                    image_urls.append(
                                                        f"data:{att_type};base64,{b64_data}"
                                                    )
                                                    image_loaded = True
                                            else:
                                                _log.warning(
                                                    f"图片文件不存在: {file_path}"
                                                )
                                        except Exception as e:
                                            _log.warning(
                                                f"读取图片文件失败: {att_name}, {e}"
                                            )

                                    # 无论成功还是失败，都更新完成状态
                                    if progress_card:
                                        _log.info(
                                            f"[ProgressCard] 准备更新 IMAGE_DONE，图片: {att_name}, 成功: {image_loaded}"
                                        )
                                        if image_loaded:
                                            progress_card.update(
                                                StepType.IMAGE_DONE,
                                                f"图片已加载: {att_name}",
                                                True,
                                            )
                                        else:
                                            progress_card.update(
                                                StepType.IMAGE_DONE,
                                                f"图片加载失败: {att_name}",
                                                False,
                                            )
                                        _log.info(f"[ProgressCard] IMAGE_DONE 更新完成")

                                # 文本文件 - 优先使用 data URL，其次使用文件路径
                                elif att_type in TEXT_MIME_TYPES:
                                    # 更新进度：正在读取文件
                                    if progress_card:
                                        progress_card.update(
                                            StepType.FILE, f"正在读取文件: {att_name}"
                                        )

                                    text_content = None
                                    # 从 data URL 提取内容
                                    if att_data and att_data.startswith("data:"):
                                        try:
                                            import base64

                                            b64_data = (
                                                att_data.split(",")[1]
                                                if "," in att_data
                                                else att_data
                                            )
                                            text_content = base64.b64decode(
                                                b64_data
                                            ).decode("utf-8", errors="ignore")
                                        except Exception as e:
                                            _log.warning(
                                                f"提取文本文件失败: {att_name}, {e}"
                                            )
                                    # 从文件路径读取内容
                                    elif att_path:
                                        try:
                                            import os

                                            file_path = os.path.join(
                                                self.static_folder,
                                                att_path.replace("/static/", ""),
                                            )
                                            if os.path.exists(file_path):
                                                with open(
                                                    file_path,
                                                    "r",
                                                    encoding="utf-8",
                                                    errors="ignore",
                                                ) as f:
                                                    text_content = f.read()
                                        except Exception as e:
                                            _log.warning(
                                                f"读取文件失败: {att_name}, {e}"
                                            )

                                    if text_content:
                                        file_contents.append(
                                            f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                        )
                                        # 文件处理完成
                                        if progress_card:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"文件已读取: {att_name}",
                                                True,
                                            )
                                    else:
                                        # 文件处理失败
                                        if progress_card:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"文件读取失败: {att_name}",
                                                False,
                                            )

                                # 根据扩展名判断是否为文本文件
                                elif any(
                                    att_name.lower().endswith(ext)
                                    for ext in TEXT_EXTENSIONS
                                ):
                                    # 更新进度：正在读取文件
                                    if progress_card:
                                        progress_card.update(
                                            StepType.FILE, f"正在读取文件: {att_name}"
                                        )

                                    text_content = None
                                    file_read_success = False

                                    # 从 data URL 提取内容
                                    if att_data and att_data.startswith("data:"):
                                        try:
                                            import base64

                                            b64_data = (
                                                att_data.split(",")[1]
                                                if "," in att_data
                                                else att_data
                                            )
                                            text_content = base64.b64decode(
                                                b64_data
                                            ).decode("utf-8", errors="ignore")
                                            file_read_success = True
                                        except Exception as e:
                                            _log.warning(
                                                f"提取文本文件失败: {att_name}, {e}"
                                            )
                                    # 从文件路径读取内容
                                    elif att_path:
                                        try:
                                            import os

                                            file_path = os.path.join(
                                                self.static_folder,
                                                att_path.replace("/static/", ""),
                                            )
                                            if os.path.exists(file_path):
                                                with open(
                                                    file_path,
                                                    "r",
                                                    encoding="utf-8",
                                                    errors="ignore",
                                                ) as f:
                                                    text_content = f.read()
                                                    file_read_success = True
                                        except Exception as e:
                                            _log.warning(
                                                f"读取文件失败: {att_name}, {e}"
                                            )

                                    if text_content:
                                        file_contents.append(
                                            f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                        )

                                    # 更新完成状态
                                    if progress_card:
                                        if file_read_success:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"文件已读取: {att_name}",
                                                True,
                                            )
                                        else:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"文件读取失败: {att_name}",
                                                False,
                                            )

                                # 工作区文件 - 从 URL 中提取路径并读取内容
                                elif att_type == "workspace/file" or (
                                    att_path and "/workspace/files/" in str(att_path)
                                ):
                                    # 更新进度：正在读取工作区文件
                                    if progress_card:
                                        progress_card.update(
                                            StepType.FILE,
                                            f"正在读取工作区文件: {att_name}",
                                        )

                                    text_content = None
                                    file_read_success = False

                                    # 尝试从 URL 中提取工作区文件路径
                                    try:
                                        import re

                                        url = att.get("url", "")
                                        # 匹配 /api/sessions/{session_id}/workspace/files/{path}
                                        match = re.search(
                                            r"/workspace/files/([^?]+)", url
                                        )
                                        if match:
                                            ws_file_path = match.group(1)
                                            if (
                                                WORKSPACE_AVAILABLE
                                                and workspace_manager
                                            ):
                                                # 使用 workspace_manager 读取文件
                                                ws_path = (
                                                    workspace_manager.get_workspace(
                                                        session_id
                                                    )
                                                )
                                                if ws_path:
                                                    full_path = os.path.join(
                                                        ws_path, ws_file_path
                                                    )
                                                    if os.path.exists(
                                                        full_path
                                                    ) and os.path.isfile(full_path):
                                                        with open(
                                                            full_path,
                                                            "r",
                                                            encoding="utf-8",
                                                            errors="ignore",
                                                        ) as f:
                                                            text_content = f.read()
                                                            file_read_success = True
                                                            _log.info(
                                                                f"工作区文件已读取: {ws_file_path}"
                                                            )
                                    except Exception as e:
                                        _log.warning(
                                            f"读取工作区文件失败: {att_name}, {e}"
                                        )

                                    if text_content:
                                        file_contents.append(
                                            f"【文件 {att_name} 内容】:\n{text_content[:10000]}"
                                        )

                                    # 更新完成状态
                                    if progress_card:
                                        if file_read_success:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"工作区文件已读取: {att_name}",
                                                True,
                                            )
                                        else:
                                            progress_card.update(
                                                StepType.FILE_DONE,
                                                f"工作区文件读取失败: {att_name}",
                                                False,
                                            )

                                # 使用本地解析器解析的文件类型
                                elif (
                                    any(
                                        att_name.lower().endswith(ext)
                                        for ext in [
                                            ".pdf",
                                            ".doc",
                                            ".docx",
                                            ".ppt",
                                            ".pptx",
                                            ".xls",
                                            ".xlsx",
                                        ]
                                    )
                                    and att_path
                                ):
                                    try:
                                        import os

                                        file_abs_path = os.path.join(
                                            self.static_folder,
                                            att_path.replace("/static/", ""),
                                        )
                                        if os.path.exists(file_abs_path):
                                            # 使用文件解析器获取元数据
                                            if FILE_PARSER_AVAILABLE and file_parser:
                                                metadata = (
                                                    file_parser.get_file_metadata(
                                                        file_abs_path, att_name
                                                    )
                                                )
                                                if metadata.get("success"):
                                                    # 构建元数据信息
                                                    meta_parts = [f"文件: {att_name}"]
                                                    meta_parts.append(
                                                        f"类型: {metadata.get('type', 'unknown')}"
                                                    )
                                                    meta_parts.append(
                                                        f"大小: {metadata.get('size_str', 'unknown')}"
                                                    )

                                                    # 添加额外信息（页数、工作表数等）
                                                    if "pages" in metadata:
                                                        meta_parts.append(
                                                            f"页数: {metadata['pages']}"
                                                        )
                                                    if "slides" in metadata:
                                                        meta_parts.append(
                                                            f"幻灯片数: {metadata['slides']}"
                                                        )
                                                    if "sheets" in metadata:
                                                        meta_parts.append(
                                                            f"工作表数: {metadata['sheets']}"
                                                        )
                                                        if "sheet_names" in metadata:
                                                            sheet_names = metadata[
                                                                "sheet_names"
                                                            ]
                                                            if isinstance(
                                                                sheet_names, list
                                                            ):
                                                                meta_parts.append(
                                                                    f"工作表: {', '.join(str(s) for s in sheet_names)}"
                                                                )
                                                    if "paragraphs" in metadata:
                                                        meta_parts.append(
                                                            f"段落数: {metadata['paragraphs']}"
                                                        )
                                                    if "tables" in metadata:
                                                        meta_parts.append(
                                                            f"表格数: {metadata['tables']}"
                                                        )

                                                    meta_parts.append(
                                                        "\n如需查看文件内容，请调用 workspace_parse_file 工具解析此文件。"
                                                    )

                                                    file_contents.append(
                                                        "【文件元数据】\n"
                                                        + "\n".join(meta_parts)
                                                    )
                                                    _log.info(
                                                        f"文件元数据已提取: {att_name}"
                                                    )
                                                else:
                                                    file_contents.append(
                                                        f"【文件 {att_name}】类型: {att_type} (无法获取元数据)"
                                                    )
                                            else:
                                                file_contents.append(
                                                    f"【文件 {att_name}】类型: {att_type} (文件解析器不可用)"
                                                )
                                        else:
                                            file_contents.append(
                                                f"【文件 {att_name}】类型: {att_type} (文件不存在)"
                                            )
                                    except Exception as e:
                                        _log.warning(
                                            f"获取文件元数据失败: {att_name}, {e}"
                                        )
                                        file_contents.append(
                                            f"【文件 {att_name}】类型: {att_type} (获取元数据失败)"
                                        )
                                # 其他文件 - 告知AI文件类型
                                elif att_type:
                                    file_contents.append(
                                        f"【文件 {att_name}】类型: {att_type} (暂不支持解析)"
                                    )

                # 附件处理完成，更新进度
                if progress_card:
                    _log.info(
                        f"[ProgressCard] 准备更新 UPLOAD_DONE，当前步骤数: {len(progress_card.steps)}"
                    )
                    progress_card.update(
                        StepType.UPLOAD_DONE,
                        f"附件处理完成 ({len(attachments)} 个文件)",
                        True,
                    )
                    _log.info(
                        f"[ProgressCard] UPLOAD_DONE 更新完成，步骤数: {len(progress_card.steps)}"
                    )

                # 知识库检索成功，更新进度
                if progress_card and knowledge_retrieved:
                    _log.info(f"[ProgressCard] 准备更新 KNOWLEDGE 步骤")
                    progress_card.update(StepType.KNOWLEDGE, "📚 知识库检索...")
                    progress_card.update(
                        StepType.KNOWLEDGE_DONE, "📚 知识库已加载", True
                    )
                    _log.info(f"[ProgressCard] KNOWLEDGE 步骤更新完成")

                # 合并文件内容到用户消息
                enhanced_content = user_content
                if file_contents:
                    enhanced_content = (
                        user_content + "\n\n" + "\n\n".join(file_contents)
                    )

                # 定义默认的完成函数（用于多模态AI分支）
                def complete_thinking_card():
                    """将进度卡片标记为完成状态"""
                    if progress_card:
                        try:
                            from nbot.core.progress_card import StepType

                            progress_card.update(StepType.DONE, "✅ 处理完成", True)
                            progress_card.complete()
                        except Exception as e:
                            _log.warning(f"[ThinkingCard] 标记完成失败: {e}")

                # 检查是否有图片附件，如果有则使用多模态AI
                if image_urls:
                    # 使用多模态AI处理图片，传递用户的原始问题
                    assistant_content = self._get_ai_response_with_images(
                        messages_for_ai, image_urls, enhanced_content
                    )
                    final_content = assistant_content
                    # 完成进度卡片
                    complete_thinking_card()
                else:
                    # 尝试使用工具调用（多轮）
                    try:
                        from nbot.services.tools import get_enabled_tools, execute_tool

                        enabled_tools = get_enabled_tools()
                        # 记录加载的工具列表
                        tool_names = [
                            t.get("function", {}).get("name", "unknown")
                            for t in enabled_tools
                        ]
                        _log.info(
                            f"[Tools] 已加载 {len(enabled_tools)} 个工具: {tool_names}"
                        )
                        if enabled_tools:
                            # 构建工具上下文（包含 session_id）
                            tool_context = {
                                "session_id": session_id,
                                "session_type": session.get("type", "unknown"),
                                "user_id": session.get("user_id", session_id),
                            }

                            # 构建多轮消息（使用深拷贝）
                            tool_messages = copy.deepcopy(messages_for_ai)
                            if (
                                file_contents
                                and tool_messages
                                and tool_messages[-1].get("role") == "user"
                            ):
                                tool_messages[-1]["content"] = enhanced_content

                            # 如果是继续请求，恢复工具调用历史
                            if tool_call_history:
                                tool_messages.extend(tool_call_history)
                                _log.info(
                                    f"[Continue] 已恢复工具调用历史，当前消息数: {len(tool_messages)}"
                                )

                            max_iterations = 50
                            final_content = None

                            _log.info(
                                f"[ThinkingCard] 使用 ProgressCard 系统, session_id={session_id}, card_id={progress_card.card_id if progress_card else 'None'}"
                            )

                            def update_thinking_card(
                                step_type,
                                step_name,
                                step_detail=None,
                                step_result=None,
                                step_arguments=None,
                                step_full_result=None,
                                thinking_content=None,
                            ):
                                """更新进度卡片 - 使用新的 ProgressCard 系统"""
                                if not progress_card:
                                    return

                                try:
                                    from nbot.core.progress_card import StepType

                                    step_type_map = {
                                        "start": StepType.START,
                                        "thinking": StepType.THINKING,
                                        "ai_thinking": StepType.AI_THINKING,
                                        "tool": StepType.TOOL,
                                        "tool_done": StepType.TOOL_DONE,
                                        "image": StepType.IMAGE,
                                        "image_done": StepType.IMAGE_DONE,
                                        "file": StepType.FILE,
                                        "file_done": StepType.FILE_DONE,
                                        "upload": StepType.UPLOAD,
                                        "upload_done": StepType.UPLOAD_DONE,
                                        "knowledge": StepType.KNOWLEDGE,
                                        "knowledge_done": StepType.KNOWLEDGE_DONE,
                                    }
                                    step_type_enum = step_type_map.get(step_type)
                                    if step_type_enum:
                                        progress_card.update(
                                            step_type_enum,
                                            step_name,
                                            step_detail,
                                            step_result,
                                            step_arguments,
                                            step_full_result,
                                            thinking_content,
                                        )
                                except Exception as e:
                                    _log.warning(f"[ThinkingCard] 更新进度失败: {e}")

                            def update_thinking_stream(thinking_content):
                                """流式更新AI思考内容"""
                                if not progress_card:
                                    return
                                try:
                                    progress_card.append_thinking_content(
                                        thinking_content
                                    )
                                except Exception as e:
                                    _log.warning(
                                        f"[ThinkingCard] 流式更新思考内容失败: {e}"
                                    )

                            # 注意：complete_thinking_card 函数已在前面定义

                            stopped_prematurely = False
                            for iteration in range(max_iterations):
                                # 检查停止事件
                                if stop_event.is_set():
                                    _log.info(
                                        f"[Stop] 检测到停止信号，终止生成: session_id={session_id}"
                                    )
                                    stopped_prematurely = True
                                    break

                                # 更新进度卡片迭代计数
                                if progress_card:
                                    progress_card.increment_iteration()

                                try:
                                    response = self._get_ai_response_with_tools(
                                        tool_messages,
                                        enabled_tools,
                                        stop_event=stop_event,
                                    )
                                except StopIteration:
                                    # 用户停止生成
                                    _log.info(f"[Stop] 检测到停止信号，终止生成")
                                    stopped_prematurely = True
                                    break

                                if "tool_calls" in response and response["tool_calls"]:
                                    tool_calls = response["tool_calls"]
                                    _log.info(
                                        f"[Tools] AI 调用工具: {[tc.get('name') for tc in tool_calls]}"
                                    )

                                    # 获取 AI 的思考内容（content），后续会关联到每个工具步骤
                                    ai_thinking_content = response.get("content", "")

                                    # 添加 AI 回复到消息历史
                                    tool_messages.append(
                                        {
                                            "role": "assistant",
                                            "content": response.get("content", ""),
                                            "tool_calls": [
                                                {
                                                    "id": tc.get(
                                                        "id", str(uuid.uuid4())
                                                    ),
                                                    "type": "function",
                                                    "function": {
                                                        "name": tc["name"],
                                                        "arguments": json.dumps(
                                                            tc["arguments"]
                                                        ),
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

                                        # 更新进度卡片 - 工具开始
                                        tool_display_name = {
                                            "search_news": "🔍 搜索新闻",
                                            "get_weather": "🌤️ 查询天气",
                                            "search_web": "🌐 网页搜索",
                                            "get_date_time": "🕐 获取时间",
                                            "http_get": "📡 获取网页",
                                            "understand_image": "🖼️ 理解图片",
                                            "workspace_create_file": "📝 创建文件",
                                            "workspace_read_file": "📖 读取文件",
                                            "workspace_edit_file": "✏️ 编辑文件",
                                            "workspace_delete_file": "🗑️ 删除文件",
                                            "workspace_list_files": "📁 列出文件",
                                            "workspace_tree": "🌳 显示目录树",
                                            "workspace_send_file": "📤 发送文件",
                                            "todo_add": "✅ 添加待办",
                                            "todo_list": "📋 列出待办",
                                            "todo_complete": "✓ 完成待办",
                                            "todo_delete": "🗑️ 删除待办",
                                            "todo_clear": "🧹 清空待办",
                                        }.get(tool_name, f"⚙️ {tool_name}")

                                        update_thinking_card(
                                            "tool",
                                            tool_display_name,
                                            json.dumps(arguments, ensure_ascii=False)[
                                                :100
                                            ],
                                            None,
                                            None,
                                            None,
                                            ai_thinking_content
                                            if ai_thinking_content
                                            else None,
                                        )

                                        # 工作区工具日志
                                        if tool_name.startswith("workspace_"):
                                            _log.info(
                                                f"[Workspace] 工具调用: {tool_name}"
                                            )
                                            args_str = json.dumps(
                                                arguments, ensure_ascii=False
                                            )
                                            if len(args_str) > 200:
                                                _log.info(
                                                    f"[Workspace] 参数: {args_str[:200]}... (共 {len(args_str)} 字符)"
                                                )
                                            else:
                                                _log.info(
                                                    f"[Workspace] 参数: {args_str}"
                                                )

                                        # 执行工具，传递 context
                                        tool_result = execute_tool(
                                            tool_name, arguments, context=tool_context
                                        )

                                        # 更新进度卡片 - 工具完成（保存完整参数、返回值和思考内容）
                                        if tool_result.get("success"):
                                            result_preview = str(
                                                tool_result.get(
                                                    "content",
                                                    tool_result.get(
                                                        "files", tool_result
                                                    ),
                                                )
                                            )[:100]
                                            update_thinking_card(
                                                "tool_done",
                                                tool_display_name,
                                                result_preview,
                                                None,
                                                step_arguments=arguments,  # 保存完整参数
                                                step_full_result=tool_result,  # 保存完整返回值
                                                thinking_content=ai_thinking_content
                                                if ai_thinking_content
                                                else None,  # 保存AI思考内容
                                            )
                                        else:
                                            update_thinking_card(
                                                "tool_done",
                                                tool_display_name,
                                                None,
                                                None,
                                                step_arguments=arguments,
                                                step_full_result=tool_result,
                                                thinking_content=ai_thinking_content
                                                if ai_thinking_content
                                                else None,  # 保存AI思考内容
                                            )

                                        # 工作区工具日志
                                        if tool_name.startswith("workspace_"):
                                            if tool_result.get("success"):
                                                _log.info(
                                                    f"[Workspace] ✓ 执行成功: {tool_name}"
                                                )
                                                _log.info(
                                                    f"[Workspace] 结果预览: {str(tool_result)[:300]}"
                                                )
                                            else:
                                                _log.error(
                                                    f"[Workspace] ✗ 执行失败: {tool_name} - {tool_result.get('error')}"
                                                )

                                        # Todo 工具日志
                                        if tool_name.startswith("todo_"):
                                            if tool_result.get("success"):
                                                _log.info(
                                                    f"[Todo] ✓ 执行成功: {tool_name} - {tool_result.get('message', '')}"
                                                )
                                                # 更新 Todo 卡片
                                                if todo_card and TODO_CARD_AVAILABLE:
                                                    try:
                                                        if tool_name == "todo_add":
                                                            todo_info = tool_result.get(
                                                                "todo", {}
                                                            )
                                                            todo_card.add_todo(
                                                                todo_id=todo_info.get(
                                                                    "id"
                                                                ),
                                                                content=todo_info.get(
                                                                    "content", ""
                                                                ),
                                                                priority=todo_info.get(
                                                                    "priority", "medium"
                                                                ),
                                                            )
                                                        elif (
                                                            tool_name == "todo_complete"
                                                        ):
                                                            todo_info = tool_result.get(
                                                                "todo", {}
                                                            )
                                                            todo_card.complete_todo(
                                                                todo_info.get("id")
                                                            )
                                                        elif tool_name == "todo_delete":
                                                            deleted_todo = (
                                                                tool_result.get(
                                                                    "deleted_todo", {}
                                                                )
                                                            )
                                                            todo_card.delete_todo(
                                                                deleted_todo.get("id")
                                                            )
                                                        elif tool_name == "todo_list":
                                                            todos = tool_result.get(
                                                                "todos", []
                                                            )
                                                            todo_card.update_todos(
                                                                todos
                                                            )
                                                        elif tool_name == "todo_clear":
                                                            todo_card.todos = []
                                                            todo_card._emit_update()
                                                    except Exception as e:
                                                        _log.warning(
                                                            f"[TodoCard] 更新 Todo 卡片失败: {e}"
                                                        )
                                            else:
                                                _log.error(
                                                    f"[Todo] ✗ 执行失败: {tool_name} - {tool_result.get('error', '')}"
                                                )

                                        # 处理 send_message 工具（不中断思考流程）
                                        if (
                                            tool_name == "send_message"
                                            and tool_result.get("action")
                                            == "send_message"
                                        ):
                                            # 立即发送消息给用户，不添加到 tool_messages
                                            progress_msg = {
                                                "id": str(uuid.uuid4()),
                                                "role": "assistant",
                                                "content": tool_result.get(
                                                    "content", ""
                                                ),
                                                "timestamp": datetime.now().isoformat(),
                                                "sender": "AI",
                                                "message_type": tool_result.get(
                                                    "message_type", "progress"
                                                ),
                                                "is_progress_message": True,  # 标记为进度消息
                                            }
                                            # 发送消息但不保存到 session（避免污染对话历史）
                                            self.socketio.emit(
                                                "progress_message",
                                                {
                                                    "session_id": session_id,
                                                    "message": progress_msg,
                                                },
                                                room=session_id,
                                            )
                                            _log.info(
                                                f"[SendMessage] 已发送进度消息: {tool_result.get('content', '')[:50]}..."
                                            )

                                            # 添加工具结果到消息历史（让 AI 知道消息已发送）
                                            tool_messages.append(
                                                {
                                                    "role": "tool",
                                                    "tool_call_id": tool_call.get(
                                                        "id", ""
                                                    ),
                                                    "content": json.dumps(
                                                        {
                                                            "success": True,
                                                            "message": "消息已发送",
                                                        },
                                                        ensure_ascii=False,
                                                    ),
                                                }
                                            )
                                        else:
                                            # 普通工具，正常处理
                                            tool_messages.append(
                                                {
                                                    "role": "tool",
                                                    "tool_call_id": tool_call.get(
                                                        "id", ""
                                                    ),
                                                    "content": json.dumps(
                                                        tool_result, ensure_ascii=False
                                                    ),
                                                }
                                            )

                                        # 如果是 workspace_send_file ，自动发送文件到 Web 端
                                        if (
                                            tool_name == "workspace_send_file"
                                            and tool_result.get("action") == "send_file"
                                        ):
                                            file_path = tool_result.get("path", "")
                                            filename = tool_result.get("filename", "")
                                            _log.info(
                                                f"[SendFile] 准备发送文件: {filename}, path: {file_path}, session_id: {session_id}"
                                            )

                                            if file_path and filename and session_id:
                                                try:
                                                    # 直接构建文件消息并发送
                                                    import os
                                                    import mimetypes
                                                    import shutil

                                                    # 获取文件信息
                                                    if not os.path.exists(file_path):
                                                        _log.error(
                                                            f"[SendFile] 文件不存在: {file_path}"
                                                        )
                                                    else:
                                                        file_size = os.path.getsize(
                                                            file_path
                                                        )
                                                        mime_type, _ = (
                                                            mimetypes.guess_type(
                                                                file_path
                                                            )
                                                        )
                                                        if not mime_type:
                                                            mime_type = "application/octet-stream"
                                                        ext = os.path.splitext(
                                                            file_path
                                                        )[1].lower()
                                                        is_image = (
                                                            mime_type
                                                            and mime_type.startswith(
                                                                "image/"
                                                            )
                                                        )

                                                        # 复制文件到静态目录
                                                        files_dir = os.path.join(
                                                            self.static_folder, "files"
                                                        )
                                                        os.makedirs(
                                                            files_dir, exist_ok=True
                                                        )

                                                        import hashlib
                                                        import time

                                                        file_hash = hashlib.md5(
                                                            f"{file_path}{time.time()}".encode()
                                                        ).hexdigest()[:8]
                                                        safe_name = (
                                                            f"{file_hash}_{filename}"
                                                        )
                                                        dest_path = os.path.join(
                                                            files_dir, safe_name
                                                        )

                                                        # 确保目标目录存在（处理子目录情况）
                                                        os.makedirs(
                                                            os.path.dirname(dest_path),
                                                            exist_ok=True,
                                                        )

                                                        shutil.copy2(
                                                            file_path, dest_path
                                                        )
                                                        download_url = (
                                                            f"/static/files/{safe_name}"
                                                        )

                                                        # 构建文件消息
                                                        file_info = {
                                                            "id": str(uuid.uuid4()),
                                                            "role": "assistant",
                                                            "content": f"[文件: {filename}]",
                                                            "timestamp": datetime.now().isoformat(),
                                                            "sender": "AI",
                                                            "source": "web",
                                                            "session_id": session_id,
                                                            "file": {
                                                                "name": filename,
                                                                "type": mime_type,
                                                                "size": file_size,
                                                                "is_image": is_image,
                                                                "extension": ext,
                                                                "download_url": download_url,
                                                                "url": download_url,
                                                            },
                                                        }

                                                        # 对于图片，嵌入 base64 数据
                                                        if (
                                                            is_image
                                                            and file_size
                                                            < 5 * 1024 * 1024
                                                        ):
                                                            try:
                                                                import base64

                                                                with open(
                                                                    file_path, "rb"
                                                                ) as f:
                                                                    file_data = f.read()
                                                                b64_data = (
                                                                    base64.b64encode(
                                                                        file_data
                                                                    ).decode("utf-8")
                                                                )
                                                                file_info["file"][
                                                                    "data"
                                                                ] = f"data:{mime_type};base64,{b64_data}"
                                                                file_info["file"][
                                                                    "preview_url"
                                                                ] = file_info["file"][
                                                                    "data"
                                                                ]
                                                            except Exception as img_err:
                                                                _log.warning(
                                                                    f"[SendFile] 图片转base64失败: {img_err}"
                                                                )

                                                        # 保存到 session
                                                        if session_id in self.sessions:
                                                            self.sessions[session_id][
                                                                "messages"
                                                            ].append(file_info)
                                                            self._save_data("sessions")

                                                        # 发送文件消息到前端
                                                        self.socketio.emit(
                                                            "new_message",
                                                            file_info,
                                                            room=session_id,
                                                        )
                                                        _log.info(
                                                            f"[SendFile] 文件已发送: {filename} ({mime_type}, {file_size} bytes)"
                                                        )

                                                except Exception as send_err:
                                                    _log.error(
                                                        f"[SendFile] 发送文件时出错: {send_err}",
                                                        exc_info=True,
                                                    )
                                            else:
                                                _log.warning(
                                                    f"[SendFile] 无法发送文件: file_path={file_path}, filename={filename}, session_id={session_id}"
                                                )
                                else:
                                    # AI 没有调用工具，得到回复内容
                                    final_content = response.get("content", "")
                                    finish_reason = response.get("finish_reason", "")

                                    # 判断是否应该终止思考
                                    # 1. finish_reason == 'stop' 表示模型正常完成（OpenAI标准）
                                    # 2. finish_reason 为空但 AI 返回了内容（某些国内API不返回finish_reason）
                                    # 3. 回复以 'break' 结尾表示AI主动要求终止
                                    # 4. 达到最大迭代次数
                                    should_stop = (
                                        finish_reason == "stop"
                                        or (
                                            not finish_reason and final_content
                                        )  # 兼容不返回finish_reason的API
                                        or final_content.rstrip().endswith("break")
                                        or iteration >= max_iterations - 1
                                    )

                                    if should_stop:
                                        # 移除末尾的break标记（如果有）
                                        if final_content.rstrip().endswith("break"):
                                            final_content = final_content.rstrip()[
                                                :-5
                                            ].rstrip()
                                        _log.info(
                                            f"[AgentLoop] 终止思考: finish_reason={finish_reason}, iteration={iteration}"
                                        )
                                        break
                                    else:
                                        # AI 没有要求终止，继续下一轮思考
                                        # 将AI的回复添加到消息历史
                                        tool_messages.append(
                                            {
                                                "role": "assistant",
                                                "content": final_content,
                                            }
                                        )
                                        _log.info(
                                            f"[AgentLoop] 继续思考: finish_reason={finish_reason}, iteration={iteration}"
                                        )
                                        # 继续下一轮迭代，给AI机会调用工具
                                        continue

                            if not final_content:
                                _log.warning(f"[Tools] AI 未生成最终回复，使用默认提示")
                                final_content = (
                                    "抱歉，处理过程中出现了问题，请稍后再试~"
                                )

                            _log.info(f"[Tools] 最终回复长度: {len(final_content)}")

                            # 将进度卡片标记为完成（不再删除）
                            complete_thinking_card()
                        else:
                            # 无可用工具，使用普通 AI 调用
                            if (
                                file_contents
                                and messages_for_ai
                                and messages_for_ai[-1].get("role") == "user"
                            ):
                                messages_for_ai[-1]["content"] = enhanced_content
                            final_content = self._get_ai_response(messages_for_ai)
                            # 完成进度卡片
                            complete_thinking_card()
                    except ImportError:
                        # 工具模块不可用，使用普通 AI 调用
                        if (
                            file_contents
                            and messages_for_ai
                            and messages_for_ai[-1].get("role") == "user"
                        ):
                            messages_for_ai[-1]["content"] = enhanced_content
                        final_content = self._get_ai_response(messages_for_ai)
                        # 完成进度卡片
                        complete_thinking_card()
                    except Exception as e:
                        _log.warning(
                            f"Tool calling error: {e}, falling back to normal AI"
                        )
                        if (
                            file_contents
                            and messages_for_ai
                            and messages_for_ai[-1].get("role") == "user"
                        ):
                            messages_for_ai[-1]["content"] = enhanced_content
                        final_content = self._get_ai_response(messages_for_ai)
                        # 完成进度卡片
                        complete_thinking_card()

                # 如果是提前停止，保存工具调用历史并添加继续按钮
                if stopped_prematurely:
                    tool_call_history = [
                        m
                        for m in tool_messages
                        if m.get("role") in ("assistant", "tool")
                    ]
                    _log.info(
                        f"[Stop] 保存工具调用历史，共 {len(tool_call_history)} 条记录"
                    )

                    assistant_message = {
                        "id": str(uuid.uuid4()),
                        "role": "assistant",
                        "content": "【生成已停止 - 工具调用记录已保存，回复「继续」可继续执行】",
                        "timestamp": datetime.now().isoformat(),
                        "sender": "AI",
                        "can_continue": True,
                        "tool_call_history": tool_call_history,
                    }
                    session["messages"].append(assistant_message)
                    self._save_data("sessions")
                    complete_thinking_card()
                    self._stream_send_response(session_id, assistant_message)
                    return

                assistant_content = final_content

                assistant_message = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": assistant_content,
                    "timestamp": datetime.now().isoformat(),
                    "sender": "AI",
                }

                # 使用流式发送
                _log.info(f"[Stream] _trigger_ai_response 准备发送流式响应")
                self._stream_send_response(session_id, assistant_message)

                session["messages"].append(assistant_message)

                # 更新 Token 统计
                estimated_tokens = len(user_content) + len(assistant_content)
                input_tokens = len(user_content)
                output_tokens = len(assistant_content)
                self.token_stats["today"] = (
                    self.token_stats.get("today", 0) + estimated_tokens
                )
                self.token_stats["month"] = (
                    self.token_stats.get("month", 0) + estimated_tokens
                )

                # 更新历史记录
                today_str = datetime.now().strftime("%Y-%m-%d")
                history = self.token_stats.get("history", [])
                if not history or history[-1].get("date") != today_str:
                    history.append(
                        {
                            "date": today_str,
                            "input": input_tokens,
                            "output": output_tokens,
                            "total": estimated_tokens,
                            "cost": 0.0,
                            "message_count": 1,
                        }
                    )
                else:
                    history[-1]["input"] = history[-1].get("input", 0) + input_tokens
                    history[-1]["output"] = history[-1].get("output", 0) + output_tokens
                    history[-1]["total"] = (
                        history[-1].get("total", 0) + estimated_tokens
                    )
                    history[-1]["message_count"] = (
                        history[-1].get("message_count", 0) + 1
                    )
                self.token_stats["history"] = history[-30:]

                # 更新会话统计
                if session_id:
                    sessions_stats = self.token_stats.get("sessions", {})
                    if session_id not in sessions_stats:
                        sessions_stats[session_id] = {
                            "input": 0,
                            "output": 0,
                            "total": 0,
                            "message_count": 0,
                        }
                    sessions_stats[session_id]["input"] = (
                        sessions_stats[session_id].get("input", 0) + input_tokens
                    )
                    sessions_stats[session_id]["output"] = (
                        sessions_stats[session_id].get("output", 0) + output_tokens
                    )
                    sessions_stats[session_id]["total"] = (
                        sessions_stats[session_id].get("total", 0) + estimated_tokens
                    )
                    sessions_stats[session_id]["message_count"] = (
                        sessions_stats[session_id].get("message_count", 0) + 2
                    )  # 用户消息 + AI回复
                    self.token_stats["sessions"] = sessions_stats

                # 注意：ai_response 现在通过 _stream_send_response 流式发送了，不再单独发送

                # 记录日志
                self.log_message(
                    "info",
                    f"AI回复完成 for session {session_id[:8]}, tokens: {estimated_tokens}",
                )

                # 保存会话和 Token 统计到磁盘
                self._save_data("sessions")
                self._save_data("token_stats")

                # 自动重命名会话（如果是默认名称且对话轮数达到一定数量）
                try:
                    current_name = session.get("name", "")
                    message_count = len(session.get("messages", []))
                    _log.info(
                        f"[SessionRename] 检查重命名条件: name='{current_name}', count={message_count}"
                    )
                    # 如果是默认名称（以"会话"开头或是空名称），且已有至少4条消息（2轮对话）
                    if (
                        current_name.startswith("会话")
                        or not current_name
                        or current_name.startswith("Web 会话")
                    ) and message_count >= 4:
                        _log.info(f"[SessionRename] 条件满足，开始生成新名称...")
                        new_name = self._generate_session_name(
                            session["messages"],
                            session_id=session_id,
                            parent_message_id=assistant_message["id"],
                        )
                        if new_name:
                            session["name"] = new_name
                            self._save_data("sessions")
                            # 通知前端会话名称已更新
                            self.socketio.emit(
                                "session_renamed",
                                {"session_id": session_id, "name": new_name},
                                room=session_id,
                            )
                            _log.info(
                                f"[SessionRename] 会话 {session_id[:8]}... 已重命名为: {new_name}"
                            )
                        else:
                            _log.warning(f"[SessionRename] 生成名称失败，返回 None")
                    else:
                        _log.info(
                            f"[SessionRename] 条件不满足: startswith={current_name.startswith('会话')}, empty={not current_name}, count={message_count}"
                        )
                except Exception as e:
                    _log.error(f"[SessionRename] 自动重命名失败: {e}", exc_info=True)

            except Exception as e:
                _log.error(f"Error in AI response: {e}")
                error_message = {
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": f"抱歉，处理消息时出错: {str(e)}",
                    "timestamp": datetime.now().isoformat(),
                    "sender": "AI",
                    "error": True,
                }
                session["messages"].append(error_message)
                self.socketio.emit(
                    "ai_response",
                    {"session_id": session_id, "message": error_message},
                    room=session_id,
                )
                # 即使出错也保存会话
                self._save_data("sessions")
            finally:
                # 清理停止事件
                if session_id in self.stop_events:
                    del self.stop_events[session_id]
                    _log.info(f"[Stop] 清理停止事件: {session_id}")

        # 使用 Flask-SocketIO 的后台任务机制
        self.socketio.start_background_task(get_response)

    def add_message_to_session(
        self, session_id: str, role: str, content: str, sender: str, source: str = "qq"
    ):
        """从外部添加消息到会话（QQ 消息同步）"""
        if session_id not in self.sessions:
            return

        message = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": content,
            "timestamp": datetime.now().isoformat(),
            "sender": sender,
            "source": source,
        }

        self.sessions[session_id]["messages"].append(message)
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
                        memory_items.append(f"【{title}】{display}")
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
        skills = self.skills_config
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

        session = {
            "id": session_id,
            "name": name or f"Web 会话 {session_id[:8]}",
            "type": "web",
            "user_id": user_id,
            "created_at": datetime.now().isoformat(),
            "messages": [{"role": "system", "content": system_prompt}],
            "system_prompt": system_prompt,
        }

        self.sessions[session_id] = session
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
        is_appended_session = target_session_id and target_session_id in self.sessions

        if is_appended_session:
            # 追加到现有会话
            session_id = target_session_id
            session = self.sessions[session_id]

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
            session["messages"].append(hb_user_message)
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
            self.sessions[session_id] = session
            _log.info(f"Heartbeat: 创建新会话 {session_id}")

        # 调用 AI 处理
        try:
            from nbot.services.chat_service import chat as do_chat

            # chat 函数是同步的，直接调用
            response_text = do_chat(
                content,
                user_id="heartbeat",
                group_id=None,
                group_user_id=None,
                image=False,
                url=None,
                video=None,
            )

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
                self.sessions[session_id]["messages"].append(hb_assistant_message)
                self._save_data("sessions")

                # 发送响应到目标（仅发送给配置的 targets）
                for target in targets:
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
        existing_session_id = None
        for sid, session in self.sessions.items():
            if (
                session.get("qq_id") == target_id
                and session.get("type") == session_type
            ):
                existing_session_id = sid
                break

        if existing_session_id:
            session_id = existing_session_id
        elif create_if_not_exists and not group_id:
            # 私聊消息：创建新会话
            session_id = str(uuid.uuid4())
            session = {
                "id": session_id,
                "name": f"私聊 {target_id}",
                "type": "qq_private",
                "qq_id": target_id,
                "created_at": datetime.now().isoformat(),
                "messages": [],
                "system_prompt": "",
            }
            self.sessions[session_id] = session
            self._save_data("sessions")
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
                self.sessions[session_id]["messages"].append(web_msg)

            # 保存会话数据
            self._save_data("sessions")

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
    app.config["SECRET_KEY"] = "nbot-secret-key"
    app.config.update(config or {})

    # SocketIO 配置优化：增加稳定性
    # ping_timeout: 心跳超时时间
    # ping_interval: 心跳间隔
    socketio = SocketIO(
        app,
        async_mode="threading",
        cors_allowed_origins="*",
        max_http_buffer_size=100 * 1024 * 1024,
    )

    server = WebChatServer(app, socketio)

    # 添加静态文件服务路由
    @app.route("/static/files/<path:filename>")
    def serve_file(filename):
        """提供文件下载服务"""
        files_dir = os.path.join(server.static_folder, "files")
        return send_from_directory(files_dir, filename, as_attachment=True)

    # 添加静态文件预览 API
    @app.route("/api/files/<path:safe_name>/preview", methods=["GET"])
    def preview_static_file(safe_name):
        """预览 static/files 目录中的文件"""
        import mimetypes

        files_dir = os.path.join(server.static_folder, "files")
        file_path = os.path.join(files_dir, safe_name)

        if not os.path.exists(file_path):
            return jsonify({"success": False, "error": "File not found"}), 404

        ext = os.path.splitext(safe_name.lower())[1]
        mime_type, _ = mimetypes.guess_type(file_path)

        # 图片文件直接返回 URL
        image_exts = [".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"]
        if ext in image_exts:
            return jsonify(
                {"success": True, "type": "image", "url": f"/static/files/{safe_name}"}
            )

        # PDF、PPTX、DOCX 等需要前端渲染的文件，返回 blob 标识
        frontend_render_exts = [
            ".pdf",
            ".pptx",
            ".ppt",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
        ]
        if ext in frontend_render_exts:
            return jsonify(
                {
                    "success": True,
                    "type": ext[1:],  # 返回 'pdf', 'pptx' 等
                    "is_blob": True,
                    "url": f"/static/files/{safe_name}",
                }
            )

        # 使用 file_parser 解析其他文件
        from nbot.core.file_parser import FileParser

        parse_result = FileParser.parse_file(file_path, safe_name, max_chars=50000)

        if not parse_result or not parse_result.get("success"):
            return jsonify(
                {
                    "success": False,
                    "error": parse_result.get("error", "Failed to parse file")
                    if parse_result
                    else "File not found",
                }
            ), 400

        return jsonify(
            {
                "success": True,
                "type": parse_result.get("type", "text"),
                "content": parse_result.get("content", ""),
                "filename": safe_name,
                "extracted_length": parse_result.get("extracted_length", 0),
                "original_length": parse_result.get("original_length", 0),
                "truncated": parse_result.get("truncated", False),
            }
        )

    # 添加通用静态文件服务
    @app.route("/static/<path:filename>")
    def serve_static(filename):
        """提供静态文件服务"""
        return send_from_directory(server.static_folder, filename)

    # 添加文件上传 API
    # 文件大小限制：50MB
    MAX_FILE_SIZE = 50 * 1024 * 1024
    # 文本内容限制：100KB（足够大多数文本文件）
    MAX_TEXT_CONTENT_SIZE = 100 * 1024

    @app.route("/api/upload", methods=["POST"])
    def upload_file():
        """上传文件并返回文件信息"""
        try:
            if "file" not in request.files:
                return jsonify({"error": "No file provided"}), 400

            file = request.files["file"]
            if file.filename == "":
                return jsonify({"error": "No file selected"}), 400

            # 检查文件大小
            file.seek(0, 2)  # 跳到文件末尾
            file_size = file.tell()
            file.seek(0)  # 重置文件指针

            if file_size > MAX_FILE_SIZE:
                return jsonify(
                    {"error": f"文件过大，最大支持 {MAX_FILE_SIZE // (1024 * 1024)}MB"}
                ), 400

            session_id = request.form.get("session_id", "")
            save_to_workspace = False

            # 先读取文件内容（统一读取一次）
            file_data = file.read()

            # 如果提供了 session_id 且工作区可用，保存到工作区
            if session_id and WORKSPACE_AVAILABLE:
                session_type = "web"
                # 尝试从 server 实例获取会话类型
                if session_id in server.sessions:
                    session_type = server.sessions[session_id].get("type", "web")
                else:
                    # 从磁盘加载会话
                    try:
                        sessions_file = os.path.join(server.data_dir, "sessions.json")
                        if os.path.exists(sessions_file):
                            with open(sessions_file, "r", encoding="utf-8") as f:
                                disk_sessions = json.load(f)
                            if session_id in disk_sessions:
                                session_type = disk_sessions[session_id].get(
                                    "type", "web"
                                )
                    except Exception as e:
                        _log.warning(f"加载会话信息失败: {e}")

                try:
                    ws_result = workspace_manager.save_uploaded_file(
                        session_id, file_data, file.filename, session_type
                    )
                    if ws_result.get("success"):
                        save_to_workspace = True
                        _log.info(f"文件已直接保存到工作区: {file.filename}")
                except Exception as e:
                    _log.error(f"保存到工作区异常，回退到普通上传: {e}", exc_info=True)

            if save_to_workspace:
                # 读取文本内容（如果需要）
                content = None
                if ws_result.get("mime_type", "").startswith("text/") or any(
                    file.filename.lower().endswith(ext)
                    for ext in [".txt", ".md", ".json", ".xml", ".csv"]
                ):
                    try:
                        ws_file_path = ws_result.get("path", "")
                        if ws_file_path and os.path.exists(ws_file_path):
                            with open(
                                ws_file_path, "r", encoding="utf-8", errors="ignore"
                            ) as f:
                                content = f.read()
                                if len(content.encode("utf-8")) > MAX_TEXT_CONTENT_SIZE:
                                    content = content[:MAX_TEXT_CONTENT_SIZE]
                    except Exception as e:
                        _log.warning(f"读取工作区文件内容失败: {e}")

                return jsonify(
                    {
                        "success": True,
                        "filename": ws_result.get("filename", file.filename),
                        "path": ws_result.get("path", ""),
                        "url": f"/api/sessions/{session_id}/workspace/files/{ws_result.get('filename', file.filename)}",
                        "size": ws_result.get("size", file_size),
                        "content": content,
                        "in_workspace": True,
                    }
                )

            # 回退：保存到 static/uploads（需要重置文件指针）
            import hashlib

            file.seek(0)  # 重置指针，因为前面可能已经读过
            file_ext = os.path.splitext(file.filename)[1]
            unique_name = (
                hashlib.md5(f"{file.filename}{time.time()}".encode()).hexdigest()[:16]
                + file_ext
            )

            upload_dir = os.path.join(server.static_folder, "uploads")
            os.makedirs(upload_dir, exist_ok=True)
            file_path = os.path.join(upload_dir, unique_name)
            file.save(file_path)

            # 读取文件内容（文本文件）
            content = None
            try:
                if file_ext.lower() in [".txt", ".md", ".json", ".xml", ".csv"]:
                    with open(file_path, "r", encoding="utf-8") as f:
                        content = f.read()
                        if len(content.encode("utf-8")) > MAX_TEXT_CONTENT_SIZE:
                            content = content[:MAX_TEXT_CONTENT_SIZE]
                elif file_ext.lower() in [".docx"]:
                    try:
                        import docx

                        doc = docx.Document(file_path)
                        content = "\n".join([para.text for para in doc.paragraphs])
                        if len(content.encode("utf-8")) > MAX_TEXT_CONTENT_SIZE:
                            content = content[:MAX_TEXT_CONTENT_SIZE]
                    except ImportError:
                        content = None
            except Exception as e:
                _log.warning(f"无法读取文件内容: {e}")

            return jsonify(
                {
                    "success": True,
                    "filename": file.filename,
                    "unique_name": unique_name,
                    "path": f"/static/uploads/{unique_name}",
                    "url": f"/static/uploads/{unique_name}",
                    "size": os.path.getsize(file_path),
                    "content": content,
                    "in_workspace": False,
                }
            )

        except Exception as e:
            _log.error(f"文件上传失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    # Skills 存储 API
    @app.route("/api/skills/storage", methods=["GET"])
    def get_skills_storage():
        """获取所有 Skills 的存储列表"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            skills = manager.list_skills()
            return jsonify(skills)
        except Exception as e:
            _log.error(f"获取 Skills 存储失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>", methods=["GET"])
    def get_skill_storage(skill_name):
        """获取指定 Skill 的存储详情"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            info = manager.get_skill_info(skill_name)
            if not info:
                return jsonify({"error": "Skill 不存在"}), 404
            return jsonify(
                {
                    "name": skill_name,
                    "skill_md": info.get("skill_md"),
                    "reference_md": info.get("reference_md"),
                    "license": info.get("license"),
                    "files": info.get("files", []),
                    "scripts": info.get("scripts", []),
                    "resources": info.get("resources", []),
                }
            )
        except Exception as e:
            _log.error(f"获取 Skill 存储详情失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/skill-md", methods=["GET", "POST"])
    def skill_md_file(skill_name):
        """获取或保存 SKILL.md"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            storage = manager.get_skill_storage(skill_name)

            if request.method == "GET":
                content = storage.load_skill_md()
                return jsonify({"content": content or ""})
            else:
                content = request.json.get("content", "")
                storage.save_skill_md(content)
                return jsonify({"success": True, "message": "SKILL.md 已保存"})
        except Exception as e:
            _log.error(f"SKILL.md 操作失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/reference-md", methods=["GET", "POST"])
    def reference_md_file(skill_name):
        """获取或保存 reference.md"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            storage = manager.get_skill_storage(skill_name)

            if request.method == "GET":
                content = storage.load_reference_md()
                return jsonify({"content": content or ""})
            else:
                content = request.json.get("content", "")
                storage.save_reference_md(content)
                return jsonify({"success": True, "message": "reference.md 已保存"})
        except Exception as e:
            _log.error(f"reference.md 操作失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/file/<path:file_name>", methods=["GET"]
    )
    def get_skill_file(skill_name, file_name):
        """读取 Skill 任意文件内容"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            storage = manager.get_skill_storage(skill_name)

            # 构建文件路径
            file_path = os.path.join(storage.skill_dir, file_name)

            if not os.path.exists(file_path):
                return jsonify({"error": f"文件不存在: {file_name}"}), 404

            try:
                with open(file_path, "r", encoding="utf-8") as f:
                    content = f.read()
                return jsonify({"content": content, "file_name": file_name})
            except UnicodeDecodeError:
                return jsonify({"error": "不支持读取二进制文件"}), 400
        except Exception as e:
            _log.error(f"读取 Skill 文件失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/file/<path:file_name>", methods=["POST"]
    )
    def save_skill_file(skill_name, file_name):
        """保存 Skill 任意文件内容"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()

            if not manager.skill_exists(skill_name):
                return jsonify({"error": "Skill 不存在"}), 404

            storage = manager.get_skill_storage(skill_name)

            # 构建文件路径
            file_path = os.path.join(storage.skill_dir, file_name)
            os.makedirs(os.path.dirname(file_path), exist_ok=True)

            # 获取请求内容
            data = request.json or {}
            content = data.get("content", "")

            # 保存文件
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return jsonify({"success": True, "message": "文件已保存"})
        except Exception as e:
            _log.error(f"保存 Skill 文件失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>/script/<script_name>", methods=["GET"])
    def get_skill_script(skill_name, script_name):
        """读取 Skill 脚本内容"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()
            storage = manager.get_skill_storage(skill_name)
            content = storage.load_script(script_name)
            if content is None:
                return jsonify({"error": "脚本不存在"}), 404
            return jsonify({"content": content})
        except Exception as e:
            _log.error(f"读取 Skill 脚本失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route(
        "/api/skills/storage/<skill_name>/script/<script_name>", methods=["POST"]
    )
    def save_skill_script(skill_name, script_name):
        """保存 Skill 脚本"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()

            # 确保存储目录存在
            if not manager.skill_exists(skill_name):
                manager.create_skill(skill_name, {"name": skill_name})

            storage = manager.get_skill_storage(skill_name)

            # 获取请求内容
            data = request.json or {}
            content = data.get("content", "")

            # 保存脚本
            storage.save_script(script_name, content)

            return jsonify({"success": True, "message": "脚本已保存"})
        except Exception as e:
            _log.error(f"保存 Skill 脚本失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/storage/<skill_name>", methods=["DELETE"])
    def delete_skill_storage(skill_name):
        """删除 Skill 存储"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager

            manager = get_skills_storage_manager()

            if not manager.skill_exists(skill_name):
                return jsonify({"error": "Skill 存储不存在"}), 404

            success = manager.delete_skill(skill_name)
            if success:
                _log.info(f"已删除 Skill 存储: {skill_name}")
                return jsonify({"success": True, "message": "Skill 存储已删除"})
            else:
                return jsonify({"error": "删除失败"}), 500
        except Exception as e:
            _log.error(f"删除 Skill 存储失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    @app.route("/api/skills/upload-folder", methods=["POST"])
    def upload_skill_folder():
        """上传 Skill 文件夹并自动创建 Skill 配置"""
        try:
            from nbot.core.skills_manager import get_skills_storage_manager, SKILLS_ROOT
            import uuid

            # 获取上传的数据
            folder_name = request.form.get("folder_name", "")
            skill_md = request.form.get("skill_md", "")
            skill_config_str = request.form.get("skill_config", "{}")

            if not folder_name:
                return jsonify({"error": "文件夹名称不能为空"}), 400

            try:
                skill_config = json.loads(skill_config_str)
            except json.JSONDecodeError:
                skill_config = {}

            # 获取存储管理器
            manager = get_skills_storage_manager()

            # 检查是否已存在同名 Skill
            if manager.skill_exists(folder_name):
                return jsonify(
                    {"error": f'Skill "{folder_name}" 已存在，请先删除或重命名'}
                ), 409

            # 创建 Skill 存储空间
            success = manager.create_skill(folder_name)
            if not success:
                return jsonify({"error": "创建 Skill 存储空间失败"}), 500

            # 保存所有上传的文件
            files = request.files.getlist("files")
            saved_files = []

            for file in files:
                if file.filename:
                    # 确保文件路径安全
                    file_path = file.filename.replace("..", "").lstrip("/\\")
                    full_path = os.path.join(SKILLS_ROOT, folder_name, file_path)

                    # 创建目录
                    os.makedirs(os.path.dirname(full_path), exist_ok=True)

                    # 保存文件
                    file.save(full_path)
                    saved_files.append(file_path)

            _log.info(
                f"已上传 Skill 文件夹: {folder_name}, 包含 {len(saved_files)} 个文件"
            )

            # 使用文件夹名称作为 skill 名称（确保存储空间和配置名称一致）
            skill_name = folder_name

            # 创建 Skill 配置
            skill_id = str(uuid.uuid4())
            skill = {
                "id": skill_id,
                "name": skill_name,
                "description": skill_config.get(
                    "description", f"从文件夹 {folder_name} 导入的 Skill"
                ),
                "aliases": skill_config.get("aliases", []),
                "enabled": True,
                "parameters": skill_config.get("parameters", {}),
                "has_storage": True,
                "scripts": [f for f in saved_files if f.endswith(".py")],
                "skill_md": skill_md,
            }

            # 添加到配置
            server.skills_config.append(skill)
            server._save_data("skills")

            return jsonify(
                {
                    "success": True,
                    "skill": skill,
                    "message": f'Skill "{skill["name"]}" 上传成功',
                    "files_count": len(saved_files),
                }
            )

        except Exception as e:
            _log.error(f"上传 Skill 文件夹失败: {e}", exc_info=True)
            return jsonify({"error": str(e)}), 500

    return app, socketio, server

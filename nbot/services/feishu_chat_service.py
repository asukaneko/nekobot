"""飞书聊天服务 - 支持完整 Web 功能

为飞书频道提供与 Web 相同的完整功能支持：
- 会话管理
- React 流式响应
- 知识库
- Skills
- 工具调用
"""

import asyncio
import hashlib
import threading
import time
import uuid
from typing import Any, Dict, List, Optional

from nbot.channels.registry import get_channel_adapter
from nbot.channels.web import WebChannelAdapter
from nbot.core import WebSessionStore
from nbot.core.ai_pipeline import (
    AIPipeline,
    PipelineContext,
    PipelineCallbacks,
    PipelineResult,
)
from nbot.web.message_adapter import WebMessageAdapter


class FeishuChatCallbacks(PipelineCallbacks):
    """飞书 WS 频道的管道回调实现。"""

    def __init__(
        self,
        server: Any,
        session_id: str,
        credentials: Dict[str, str],
        chat_id: str,
    ):
        self.server = server
        self.session_id = session_id
        self.credentials = credentials
        self.chat_id = chat_id

    def _get_session_store(self) -> WebSessionStore:
        return WebSessionStore(
            self.server.sessions,
            save_callback=lambda: self.server._save_data("sessions"),
        )

    def load_messages(self, ctx: PipelineContext) -> List[Dict[str, Any]]:
        """加载会话消息历史。"""
        messages = []

        # 系统提示
        system = self.get_system_prompt(ctx)
        if system:
            messages.append({"role": "system", "content": system})

        # 会话历史（最近 20 条）
        session_store = self._get_session_store()
        session = session_store.get_session(self.session_id)

        if session and "messages" in session:
            recent = session["messages"][-20:]
            for msg in recent:
                role = msg.get("role", "user")
                content = msg.get("content", "")
                if role in ("user", "assistant") and content:
                    messages.append({"role": role, "content": content})

        return messages

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        return str(
            getattr(self.server, "personality", {}).get("systemPrompt") or ""
        ).strip()

    def save_assistant_message(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """保存 AI 消息到会话。"""
        session_store = self._get_session_store()
        session_store.append_message(self.session_id, message)

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        """提供工作区上下文给工具调用。"""
        return {"session_id": self.session_id, "session_type": "feishu"}

    def search_knowledge(self, ctx: PipelineContext, query: str) -> str:
        """搜索知识库。"""
        try:
            from nbot.core.knowledge import get_knowledge_manager
            km = get_knowledge_manager()
            if not km:
                return ""
            results = km.search(query, top_k=3)
            if not results:
                return ""
            parts = ["【知识库检索结果】"]
            for doc, similarity, chunk in results:
                if similarity < 0.1:
                    continue
                parts.append(f"\n📄 {doc.title}\n{chunk[:300]}")
            return "\n".join(parts) if len(parts) > 1 else ""
        except Exception:
            return ""

    def send_response(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """发送回复到飞书。"""
        from nbot.services.feishu_ws_service import send_feishu_reply

        send_feishu_reply(
            self.credentials["app_id"],
            self.credentials["app_secret"],
            self.chat_id,
            message.get("content", ""),
        )


class FeishuChatService:
    """飞书聊天服务 - 提供 Web 级别的功能支持"""

    def __init__(self, server):
        self.server = server
        self.channel_adapter = get_channel_adapter("web") or WebChannelAdapter()
        self._session_locks: Dict[str, threading.Lock] = {}
        self._lock = threading.Lock()

    def _get_session_lock(self, session_id: str) -> threading.Lock:
        """获取会话锁"""
        with self._lock:
            if session_id not in self._session_locks:
                self._session_locks[session_id] = threading.Lock()
            return self._session_locks[session_id]

    def _get_or_create_session(
        self,
        chat_id: str,
        user_id: str,
        channel_id: str,
        channel_name: str = "飞书"
    ) -> tuple[str, Dict[str, Any]]:
        """获取或创建会话

        Returns:
            (session_id, session_data)
        """
        # 使用 chat_id 和 channel_id 生成稳定的会话 ID
        session_key = f"feishu_{channel_id}_{chat_id}"
        session_id = hashlib.md5(session_key.encode()).hexdigest()[:16]

        session_store = WebSessionStore(
            self.server.sessions,
            save_callback=lambda: self.server._save_data("sessions")
        )

        session = session_store.get_session(session_id)
        if not session:
            # 创建新会话
            from datetime import datetime

            session = {
                "id": session_id,
                "name": f"{channel_name} - {chat_id[:8]}",
                "type": "feishu",  # 设置类型为 feishu，避免被归类到 web
                "channel": "feishu",
                "channel_id": channel_id,
                "feishu_chat_id": chat_id,
                "feishu_user_id": user_id,
                "created_at": datetime.now().isoformat(),
                "updated_at": datetime.now().isoformat(),
                "messages": [],
                "metadata": {
                    "source": "feishu",
                    "chat_id": chat_id,
                    "user_id": user_id,
                    "channel_id": channel_id,
                }
            }
            session_store.set_session(session_id, session)
            print(f"[FeishuChat] 创建新会话: {session_id}")
        else:
            print(f"[FeishuChat] 使用现有会话: {session_id}")

        return session_id, session

    def handle_message(
        self,
        channel_id: str,
        channel_config: Dict[str, Any],
        message_data: Dict[str, Any],
        credentials: Dict[str, str]
    ):
        """处理飞书消息 - 完整 Web 功能支持"""
        chat_id = message_data["chat_id"]
        user_id = message_data["user_id"]
        content = message_data["content"]
        message_id = message_data["message_id"]
        attachments = message_data.get("attachments", [])

        # 注入飞书凭证到附件，供中间件下载使用
        for att in attachments:
            att.setdefault("app_id", credentials.get("app_id"))
            att.setdefault("app_secret", credentials.get("app_secret"))

        print(f"[FeishuChat] 处理消息: {content[:50]}..., attachments={len(attachments)}")

        # 获取或创建会话
        session_id, session = self._get_or_create_session(
            chat_id, user_id, channel_id, channel_config.get("name", "飞书")
        )

        # 获取会话锁
        session_lock = self._get_session_lock(session_id)

        with session_lock:
            try:
                # 构建用户消息
                temp_id = f"feishu_{message_id}"
                user_message = self.channel_adapter.build_message(
                    role="user",
                    content=content,
                    sender=user_id,
                    conversation_id=session_id,
                    source="feishu",  # 正确设置 source 参数
                    metadata={
                        "tempId": temp_id,
                        "chat_id": chat_id,
                        "message_id": message_id,
                    }
                )

                # 保存用户消息到会话
                session_store = WebSessionStore(
                    self.server.sessions,
                    save_callback=lambda: self.server._save_data("sessions")
                )
                session_store.append_message(session_id, user_message)

                print(f"[FeishuChat] 用户消息已保存到会话 {session_id}")

                # 检查是否是命令
                is_command = False
                matched_handler = None
                if content and content.startswith("/"):
                    try:
                        import nbot.commands
                        from nbot.commands import command_handlers

                        for commands, handler in command_handlers.items():
                            for cmd in commands:
                                if content.startswith(cmd):
                                    is_command = True
                                    matched_handler = handler
                                    break
                            if is_command:
                                break

                        if not is_command:
                            print(f"[FeishuChat] 未知命令: {content}")
                    except Exception as e:
                        print(f"[FeishuChat] 命令匹配失败: {e}")

                # 处理命令或触发 AI 响应
                if is_command and matched_handler:
                    self._handle_command(
                        session_id, content, user_id, chat_id,
                        credentials, matched_handler
                    )
                else:
                    self._trigger_ai_response(
                        session_id, content, user_id, chat_id,
                        credentials, temp_id, attachments
                    )

            except Exception as e:
                print(f"[FeishuChat] 处理消息失败: {e}")
                import traceback
                traceback.print_exc()
                # 发送错误回复
                self._send_feishu_reply(
                    credentials, chat_id,
                    f"处理消息失败: {e}",
                    reply_message_id=message_id
                )

    def _handle_command(
        self,
        session_id: str,
        content: str,
        user_id: str,
        chat_id: str,
        credentials: Dict[str, str],
        handler: Any
    ):
        """处理命令"""
        print(f"[FeishuChat] 执行命令: {content}")

        # 创建消息适配器
        msg_adapter = FeishuMessageAdapter(
            content, user_id, chat_id, credentials, self.server, session_id
        )

        def run_command():
            original_bot = None
            try:
                import nbot.commands as cmd_module

                original_bot = getattr(cmd_module, "bot", None)
                cmd_module.bot = msg_adapter.bot

                # 执行命令
                asyncio.run(handler(msg_adapter, is_group=True))

            except Exception as e:
                print(f"[FeishuChat] 命令执行失败: {e}")
                error_msg = f"命令执行失败: {e}"
                self._send_feishu_reply(credentials, chat_id, error_msg)
            finally:
                if original_bot:
                    cmd_module.bot = original_bot

        # 在后台线程执行命令
        threading.Thread(target=run_command, daemon=True).start()

    def _trigger_ai_response(
        self,
        session_id: str,
        content: str,
        user_id: str,
        chat_id: str,
        credentials: Dict[str, str],
        parent_message_id: str,
        attachments: list = None
    ):
        """触发 AI 响应"""
        print(f"[FeishuChat] 触发 AI 响应")

        def run_ai():
            try:
                self._process_ai_response(
                    session_id, content, user_id, chat_id,
                    credentials, parent_message_id, attachments or []
                )
            except Exception as e:
                print(f"[FeishuChat] AI 响应失败: {e}")
                import traceback
                traceback.print_exc()
                error_msg = f"AI 响应失败: {e}"
                self._send_feishu_reply(credentials, chat_id, error_msg)

        # 在后台线程运行 AI
        threading.Thread(target=run_ai, daemon=True).start()

    def _process_ai_response(
        self,
        session_id: str,
        content: str,
        user_id: str,
        chat_id: str,
        credentials: Dict[str, str],
        parent_message_id: str,
        attachments: list = None
    ):
        """通过统一管道处理 AI 响应。"""
        from nbot.channels.feishu import FeishuChannelAdapter
        from nbot.core.ai_pipeline import handle_tool_confirmation

        # === 确认/拒绝待执行命令检测 ===
        content = handle_tool_confirmation(
            content, session_id, log_prefix="FeishuChat"
        )

        # 使用飞书适配器构建 ChatRequest
        adapter = FeishuChannelAdapter()
        chat_request = adapter.build_chat_request(
            conversation_id=session_id,
            user_id=user_id,
            content=content,
            sender=user_id,
            attachments=attachments or [],
            metadata={
                "chat_id": chat_id,
                "parent_message_id": parent_message_id,
                "source": "feishu_ws",
            },
        )

        ctx = PipelineContext(chat_request=chat_request, adapter=adapter)
        callbacks = FeishuChatCallbacks(server=self.server, session_id=session_id,
                                         credentials=credentials, chat_id=chat_id)

        # 获取启用的工具列表
        tools = None
        try:
            from nbot.services.tools import get_enabled_tools
            enabled = get_enabled_tools()
            if enabled:
                tools = enabled
        except Exception as e:
            print(f"[FeishuChat] 加载工具失败: {e}")

        pipeline = AIPipeline()
        result = pipeline.process(ctx, callbacks, tools=tools)

        if result.error:
            self._send_feishu_reply(
                credentials, chat_id,
                f"AI 处理失败: {result.error}"
            )
        else:
            print(f"[FeishuChat] AI 回复: {result.final_content[:100]}...")

    def _send_feishu_reply(
        self,
        credentials: Dict[str, str],
        chat_id: str,
        text: str,
        reply_message_id: Optional[str] = None
    ) -> Dict[str, Any]:
        """发送飞书回复"""
        from nbot.services.feishu_ws_service import send_feishu_reply

        return send_feishu_reply(
            credentials["app_id"],
            credentials["app_secret"],
            chat_id,
            text,
            reply_message_id=reply_message_id
        )


class FeishuMessageAdapter:
    """飞书消息适配器 - 模拟 Bot 接口供命令使用"""

    def __init__(
        self,
        content: str,
        user_id: str,
        chat_id: str,
        credentials: Dict[str, str],
        server: Any,
        session_id: str = None
    ):
        self.content = content
        self.raw_message = content  # 兼容命令系统
        self.user_id = user_id
        self.chat_id = chat_id
        self.credentials = credentials
        self.server = server
        self.session_id = session_id
        self.bot = FeishuBotMock(self)

    async def reply(self, text: str, **kwargs):
        """回复消息 - 兼容命令系统"""
        return await self.bot.send(text, **kwargs)


class FeishuBotMock:
    """飞书 Bot 模拟器 - 提供与 QQ Bot 兼容的接口"""

    def __init__(self, adapter: FeishuMessageAdapter):
        self.adapter = adapter

    async def send(self, message: str, **kwargs):
        """发送消息"""
        from nbot.services.feishu_ws_service import send_feishu_reply

        result = send_feishu_reply(
            self.adapter.credentials["app_id"],
            self.adapter.credentials["app_secret"],
            self.adapter.chat_id,
            message
        )
        print(f"[FeishuBotMock] 发送结果: {result}")
        return result

    async def reply(self, message: str, **kwargs):
        """回复消息"""
        return await self.send(message, **kwargs)

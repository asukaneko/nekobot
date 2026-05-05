import hashlib
import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Dict, List, Optional
from nbot.channels.registry import get_channel_adapter
from nbot.channels.web import WebChannelAdapter
from nbot.core import (
    build_continue_chat_response,
    build_chat_completion_payload,
    ChatRequest,
    ChatResponse,
    extract_tool_call_history,
    normalize_chat_completion_data,
    prepare_chat_context,
    resolve_chat_completion_url,
    ToolLoopExit,
    ToolLoopHooks,
    ToolLoopSession,
    WebSessionStore,
    run_tool_loop_session,
)
from nbot.core.ai_pipeline import (
    AIPipeline,
    PipelineContext,
    PipelineCallbacks,
    PipelineResult,
)
from nbot.web.utils.config_loader import get_vision_model_config

try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False

# WORKSPACE_AVAILABLE 会在函数中从 server 对象获取

_log = logging.getLogger(__name__)


def _feature_enabled(server, name: str, default: bool = True) -> bool:
    settings = getattr(server, "settings", {}) or {}
    features = settings.get("features")
    if isinstance(features, dict) and name in features:
        return bool(features.get(name))
    return bool(settings.get(name, default))


def _looks_like_tool_request(content: str) -> bool:
    text = (content or "").lower()
    if not text:
        return False
    keywords = (
        "搜索",
        "查询",
        "天气",
        "新闻",
        "待办",
        "文件",
        "工作区",
        "创建",
        "编辑",
        "删除",
        "读取",
        "列出",
        "运行",
        "执行",
        "命令",
        "todo",
        "search",
        "weather",
        "file",
        "workspace",
        "command",
    )
    return any(keyword in text for keyword in keywords)


def _build_channel_assistant_message(
    chat_response: ChatResponse,
    *,
    session_id: str,
    adapter=None,
    sender: str = "AI",
):
    channel_adapter = adapter or get_channel_adapter("web") or WebChannelAdapter()
    return channel_adapter.build_assistant_message(
        chat_response,
        conversation_id=session_id,
        sender=sender,
    )


def _build_change_card(
    *,
    session_id: str,
    parent_message_id: str,
    file_changes,
):
    normalized_changes = []
    summary = {"total": 0, "created": 0, "modified": 0, "deleted": 0}
    change_by_path = {}

    for change in file_changes or []:
        if not isinstance(change, dict):
            continue
        action = str(change.get("action") or "modified")
        if action not in summary:
            action = "modified"
        normalized_change = {
            "action": action,
            "path": change.get("path") or change.get("filename") or "",
            "scope": change.get("scope") or "private",
            "before_preview": change.get("before_preview"),
            "after_preview": change.get("after_preview"),
            "diff_preview": change.get("diff_preview"),
        }
        change_key = normalized_change["path"] or f"unnamed_{len(change_by_path)}"
        change_by_path[change_key] = normalized_change

    normalized_changes = list(change_by_path.values())
    for change in normalized_changes:
        action = change["action"]
        summary[action] += 1
        summary["total"] += 1

    if not normalized_changes:
        return None

    return {
        "id": str(uuid.uuid4()),
        "session_id": session_id,
        "parent_message_id": parent_message_id,
        "role": "system",
        "type": "change_card",
        "content": "本轮文件变更",
        "summary": summary,
        "file_changes": normalized_changes,
        "timestamp": datetime.now().isoformat(),
        "is_complete": True,
    }


def _emit_change_card(
    server,
    session_store,
    *,
    session_id: str,
    parent_message_id: str,
    file_changes,
):
    if not parent_message_id:
        return None

    change_card = _build_change_card(
        session_id=session_id,
        parent_message_id=parent_message_id,
        file_changes=file_changes,
    )
    if not change_card:
        return None

    session = session_store.get_session(session_id)
    if not session:
        return None

    for msg in session.get("messages", []):
        if (
            msg.get("id") == parent_message_id
            or msg.get("tempId") == parent_message_id
            or msg.get("originalTempId") == parent_message_id
        ):
            if "change_cards" not in msg:
                msg["change_cards"] = []
            msg["change_cards"] = [
                card
                for card in msg["change_cards"]
                if card.get("type") != "change_card"
            ]
            msg["change_cards"].append(change_card)
            break

    if session_store.save_callback:
        session_store.save_callback()
    server.socketio.emit("new_message", change_card, room=session_id)
    return change_card


# ============================================================================
# Web 频道管道回调
# ============================================================================


class WebProgressReporter:
    """Web 频道的进度报告实现，封装 ProgressCard 和 TodoCard。"""

    def __init__(self, server, session_id: str, parent_message_id: str, session_store):
        self.server = server
        self.session_id = session_id
        self.parent_message_id = parent_message_id
        self.session_store = session_store
        self.progress_card = None
        self.todo_card = None
        self._init_cards()

    def _init_cards(self):
        if (
            self.server.PROGRESS_CARD_AVAILABLE
            and self.server.progress_card_manager
            and self.server.socketio
        ):
            self.progress_card = self.server.progress_card_manager.create_card(
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
                max_iterations=50,
            )

        if (
            self.server.TODO_CARD_AVAILABLE
            and self.server.todo_card_manager
            and self.server.socketio
        ):
            self.todo_card = self.server.todo_card_manager.create_card(
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
            )

    def on_thinking_start(self, ctx) -> None:
        if self.progress_card:
            from nbot.core.progress_card import StepType
            self.progress_card.update(StepType.THINKING, "AI 正在思考...")

    def on_knowledge_start(self, ctx) -> None:
        pass

    def on_knowledge_done(self, ctx, retrieved: bool) -> None:
        if self.progress_card and retrieved:
            from nbot.core.progress_card import StepType
            self.progress_card.update(StepType.KNOWLEDGE_DONE, "知识库检索完成")

    def on_tool_start(self, ctx, tool_name: str, arguments: dict, thinking: str) -> None:
        if self.progress_card:
            from nbot.core.progress_card import StepType
            display_name = _get_tool_display_name(tool_name)
            self.progress_card.update(
                StepType.TOOL,
                display_name,
                json.dumps(arguments, ensure_ascii=False)[:100],
                step_arguments=arguments,
                thinking_content=thinking,
            )

    def on_tool_done(self, ctx, tool_name: str, result: dict, thinking: str) -> None:
        if self.progress_card:
            from nbot.core.progress_card import StepType
            result_preview = json.dumps(result, ensure_ascii=False)[:200]
            self.progress_card.update(
                StepType.TOOL_DONE,
                _get_tool_display_name(tool_name),
                result_preview,
                thinking_content=thinking,
            )
        if self.todo_card:
            try:
                # 更新 Todo 卡片
                self.server.todo_card_manager.update_from_tool_result(
                    self.todo_card, tool_name, result
                )
            except Exception:
                pass

    def on_tool_iteration(self, ctx, iteration: int) -> None:
        pass

    def on_attachment_start(self, ctx, count: int) -> None:
        if self.progress_card:
            from nbot.core.progress_card import StepType
            self.progress_card.update(StepType.UPLOAD, f"正在处理 {count} 个附件...")

    def on_attachment_item(self, ctx, name: str, item_type: str) -> None:
        pass

    def on_attachment_item_done(self, ctx, name: str, success: bool, result_preview: str = "") -> None:
        pass

    def on_attachments_done(self, ctx) -> None:
        pass

    def on_done(self, ctx) -> None:
        if self.progress_card:
            from nbot.core.progress_card import StepType
            self.progress_card.complete("✅ 处理完成")

    def on_waiting_confirmation(self, ctx, command: str, request_id: str) -> None:
        pass

    def dispose(self):
        """清理卡片资源。"""
        self.progress_card = None
        self.todo_card = None


class WebCallbacks(PipelineCallbacks):
    """Web 频道的管道回调实现。"""

    def __init__(
        self,
        server,
        session_store,
        session_id: str,
        adapter,
        parent_message_id: str = None,
        progress_reporter: WebProgressReporter = None,
    ):
        self.server = server
        self.session_store = session_store
        self.session_id = session_id
        self.adapter = adapter
        self.parent_message_id = parent_message_id
        self._progress = progress_reporter

    # ---- 会话 / 消息 I/O ----

    def load_messages(self, ctx: PipelineContext) -> List[Dict]:
        import copy
        session = self.session_store.get_session(self.session_id)
        if session:
            return copy.deepcopy(session.get("messages", []))
        return []

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        return str(
            getattr(self.server, "personality", {}).get("systemPrompt") or ""
        ).strip()

    def save_assistant_message(self, ctx: PipelineContext, message: Dict) -> None:
        self.session_store.append_message(self.session_id, message)
        self._try_auto_name_session()

    # ---- AI 模型交互 ----

    def build_model_call(self, ctx, tools):
        """Web 频道使用服务器的 AI 方法。"""
        server = self.server
        return lambda messages, stop_event=None: _call_web_ai(
            server, messages, tools, stop_event
        )

    def build_model_call_streaming(self, ctx, tools):
        """返回 provider 级流式迭代器。"""
        server = self.server
        session_id = self.session_id

        def streamer(messages, stop_event=None):
            return _stream_to_web(server, messages, tools, session_id, stop_event)

        return streamer

    # ---- 输出 / 回复 ----

    def send_response(self, ctx: PipelineContext, message: Dict) -> None:
        if ctx.metadata.get("streamed"):
            # 流式已发送，无需再次发送
            return
        self.server.socketio.emit(
            "ai_response",
            {"session_id": self.session_id, "message": message},
            room=self.session_id,
        )

    def on_stream_start(self, ctx: PipelineContext, message: Dict) -> None:
        self.server.socketio.emit(
            "ai_stream_start",
            {"session_id": self.session_id, "message": message},
            room=self.session_id,
        )

    def on_stream_chunk(self, ctx: PipelineContext, chunk: str, message_id: str) -> None:
        self.server.socketio.emit(
            "ai_stream_chunk",
            {
                "session_id": self.session_id,
                "message_id": message_id,
                "chunk": chunk,
                "is_end": False,
            },
            room=self.session_id,
        )

    def on_stream_end(self, ctx: PipelineContext, message_id: str) -> None:
        self.server.socketio.emit(
            "ai_stream_end",
            {
                "session_id": self.session_id,
                "message_id": message_id,
                "is_end": True,
            },
            room=self.session_id,
        )

    # ---- 进度 ----

    def get_progress_reporter(self, ctx: PipelineContext):
        if self._progress:
            return self._progress
        from nbot.core.ai_pipeline import NoOpProgressReporter
        return NoOpProgressReporter()

    # ---- 工具确认 ----

    def on_confirmation_required(self, ctx: PipelineContext, request_id: str, command: str) -> None:
        self.server.socketio.emit(
            "exec_confirm_request",
            {
                "request_id": request_id,
                "command": command,
                "message": f"命令 `{command}` 需要您的确认",
                "session_id": self.session_id,
            },
            room=self.session_id,
        )

    # ---- 知识库 ----

    def search_knowledge(self, ctx: PipelineContext, query: str) -> str:
        if not _feature_enabled(self.server, "knowledge", True):
            return ""
        try:
            return self.server._retrieve_knowledge(query)
        except Exception:
            return ""

    # ---- 工作区 ----

    def ensure_workspace(self, ctx: PipelineContext) -> str:
        if getattr(self.server, "WORKSPACE_AVAILABLE", False) and self.server.workspace_manager:
            return self.server.workspace_manager.get_or_create(
                self.session_id, "web"
            )
        return ""

    def get_workspace_context(self, ctx: PipelineContext) -> Dict:
        return {"session_id": self.session_id, "session_type": "web"}

    # ---- 响应完成 ----

    def on_response_complete(self, ctx: PipelineContext, result) -> None:
        """AI 响应完成后的回调，兜底触发自动命名"""
        self._try_auto_name_session()

    def _try_auto_name_session(self):
        """会话名称自动更新：首次命名 + 每隔一段对话更新"""
        try:
            session = self.session_store.get_session(self.session_id)
            if not session:
                return

            messages = session.get("messages", [])
            user_assistant_msgs = [m for m in messages if m.get("role") in ("user", "assistant")]
            total_count = len(user_assistant_msgs)
            if total_count < 2:
                return

            name = session.get("name", "")
            is_default_name = (
                not name
                or name.startswith("Web 会话")
                or name.startswith("新会话")
                or name.startswith("新对话")
            )

            # 首次命名：默认名称 + 至少一轮对话
            # 后续更新：每 10 条消息（约 5 轮对话）更新一次
            last_rename_count = session.get("_last_rename_count", 0)
            should_rename = is_default_name or (total_count - last_rename_count >= 10)

            if not should_rename:
                return

            # 防止并发重复生成
            if getattr(self, "_naming_in_progress", False):
                return
            self._naming_in_progress = True

            _log.info(f"开始为会话 {self.session_id[:8]} 自动生成名称 (当前: {name}, 消息数: {total_count})")

            import copy
            # 取最近对话作为上下文
            recent_msgs = copy.deepcopy(user_assistant_msgs[-10:])

            def generate_and_update():
                try:
                    new_name = self.server._generate_session_name(recent_msgs)
                    if new_name:
                        session["name"] = new_name
                        session["_last_rename_count"] = total_count
                        self.session_store.set_session(self.session_id, session)
                        self.server.socketio.emit(
                            "session_renamed",
                            {"session_id": self.session_id, "name": new_name},
                            room=self.session_id,
                        )
                        _log.info(f"会话自动命名成功: {self.session_id[:8]} -> {new_name}")
                except Exception as e:
                    _log.error(f"自动命名失败: {e}")
                finally:
                    self._naming_in_progress = False

            threading.Thread(target=generate_and_update, daemon=True).start()
        except Exception as e:
            _log.error(f"自动命名检查失败: {e}")

    # ---- 附件解析 ----

    def resolve_attachment_data(self, ctx: PipelineContext, attachment: Dict) -> Optional[Dict]:
        """Web 频道附件解析：静态文件 / 工作区文件 / 数据 URL。"""
        att_path = attachment.get("path", "")
        att_url = attachment.get("url", "")
        att_data = attachment.get("data", "")
        att_name = attachment.get("name", "unknown")

        result = {"type": attachment.get("type", ""), "name": att_name}

        # 数据 URL 直接返回
        if att_data:
            result["data"] = att_data
            return result

        path_to_use = att_path or att_url
        if not path_to_use:
            return None

        try:
            file_path = None
            if path_to_use.startswith("/static/"):
                file_path = os.path.join(
                    self.server.static_folder,
                    path_to_use.replace("/static/", ""),
                )
            elif "/workspace/files/" in path_to_use:
                filename = path_to_use.split("/workspace/files/")[-1]
                if self.server.workspace_manager:
                    file_path = self.server.workspace_manager.get_file_path(
                        self.session_id, filename
                    )
                    if not file_path:
                        ws_path = self.server.workspace_manager.get_workspace(
                            self.session_id
                        )
                        if ws_path:
                            file_path = os.path.join(ws_path, filename)

            if file_path and os.path.isfile(file_path):
                result["path"] = file_path
                # 读取文本文件内容
                att_type = attachment.get("type", "")
                ext = os.path.splitext(att_name)[1].lower()
                from nbot.core.ai_pipeline import AIPipeline
                if att_type in AIPipeline.TEXT_MIME_TYPES or ext in AIPipeline.TEXT_EXTENSIONS:
                    with open(file_path, "r", encoding="utf-8", errors="replace") as f:
                        result["text_content"] = f.read()
                return result
        except Exception:
            pass

        return None

    # ---- 后处理 ----

    def on_response_complete(self, ctx: PipelineContext, result: PipelineResult) -> None:
        # 自动重命名会话
        self._auto_rename_session(ctx)
        # 文件变更卡片
        if ctx.round_file_changes:
            _emit_change_card(
                self.server,
                self.session_store,
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
                file_changes=ctx.round_file_changes,
            )

    def _auto_rename_session(self, ctx: PipelineContext) -> None:
        """自动重命名会话（基于对话内容）。"""
        try:
            session = self.session_store.get_session(self.session_id)
            if not session:
                return
            name = session.get("name", "")
            if name and name != "新对话":
                return
            messages = session.get("messages", [])
            user_count = sum(1 for m in messages if m.get("role") == "user")
            if user_count < 2:
                return
            # 简化：取第一条用户消息的前30字作为会话名
            first_user_msg = ""
            for m in messages:
                if m.get("role") == "user":
                    content = m.get("content", "").strip()
                    if content and len(content) > 3:
                        first_user_msg = content
                        break
            if first_user_msg:
                new_name = first_user_msg[:30] + ("..." if len(first_user_msg) > 30 else "")
                session["name"] = new_name
                self.session_store.set_session(self.session_id, session)
        except Exception:
            pass


# ---- Web 频道的 model_call 辅助函数 ----

def _call_web_ai(server, messages: List[Dict], tools: list, stop_event=None) -> Dict:
    """Web 频道的 model_call 实现。"""
    import requests
    from nbot.services.ai import refresh_runtime_ai_config

    if stop_event and stop_event.is_set():
        raise StopIteration("User stopped")

    runtime_ai = refresh_runtime_ai_config()
    base_url = runtime_ai.get("base_url") or ""
    model = runtime_ai.get("model") or ""
    provider_type = runtime_ai.get("provider_type") or "openai_compatible"
    api_key = runtime_ai.get("api_key") or ""

    url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = build_chat_completion_payload(
        model, messages,
        base_url=base_url, provider_type=provider_type,
        tools=tools if tools else None,
        tool_choice="auto" if tools else None,
        stream=False,
    )
    resp = requests.post(url, json=payload, headers=headers, timeout=120)
    resp.raise_for_status()
    normalized = normalize_chat_completion_data(
        resp.json(),
        base_url=base_url, model=model, provider_type=provider_type,
    )
    return normalized.to_dict()


def _stream_to_web(server, messages: List[Dict], tools: list, session_id: str, stop_event=None):
    """Web 频道的 provider 级流式实现，含 chunk 去重。"""
    import requests
    from nbot.services.ai import refresh_runtime_ai_config

    runtime_ai = refresh_runtime_ai_config()
    base_url = runtime_ai.get("base_url") or ""
    model = runtime_ai.get("model") or ""
    provider_type = runtime_ai.get("provider_type") or "openai_compatible"
    api_key = runtime_ai.get("api_key") or ""

    url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = build_chat_completion_payload(
        model, messages,
        base_url=base_url, provider_type=provider_type,
        tools=tools if tools else None,
        tool_choice="auto" if tools else None,
        stream=True,
    )
    resp = requests.post(url, json=payload, headers=headers, stream=True, timeout=120)
    resp.raise_for_status()

    # chunk 去重：部分提供商返回累积文本而非增量
    content_parts: List[str] = []

    def normalize_chunk(raw: str) -> str:
        """从累积文本中提取新增部分。"""
        if not raw:
            return ""
        existing = "".join(content_parts)
        if not existing:
            return raw
        if raw.startswith(existing):
            return raw[len(existing):]
        if existing.endswith(raw):
            return ""
        max_overlap = min(len(existing), len(raw), 32)
        for overlap in range(max_overlap, 2, -1):
            if existing.endswith(raw[:overlap]):
                return raw[overlap:]
        return raw

    for line in resp.iter_lines(decode_unicode=True):
        if stop_event and stop_event.is_set():
            break
        if line and line.startswith("data: "):
            data_str = line[6:]
            if data_str.strip() == "[DONE]":
                break
            try:
                data = json.loads(data_str)
                choices = data.get("choices", [{}])
                delta = choices[0].get("delta", {})
                raw = delta.get("content", "")
                if raw:
                    chunk = normalize_chunk(raw)
                    if chunk:
                        content_parts.append(chunk)
                        yield {"content": chunk}
            except json.JSONDecodeError:
                continue


def _get_tool_display_name(tool_name: str) -> str:
    """获取工具显示名称。"""
    names = {
        "search_web": "网页搜索",
        "search_news": "新闻搜索",
        "get_weather": "天气查询",
        "get_date_time": "日期时间",
        "http_get": "HTTP 请求",
        "understand_image": "图片理解",
        "exec_command": "命令执行",
        "download_file": "文件下载",
        "save_to_memory": "保存记忆",
        "read_memory": "读取记忆",
        "workspace_create_file": "创建工作区文件",
        "workspace_read_file": "读取工作区文件",
        "workspace_edit_file": "编辑工作区文件",
        "workspace_delete_file": "删除工作区文件",
        "workspace_list_files": "列出工作区文件",
        "workspace_send_file": "发送工作区文件",
        "workspace_parse_file": "解析文件",
        "todo_add": "添加待办",
        "todo_list": "列出待办",
        "todo_complete": "完成待办",
        "todo_delete": "删除待办",
    }
    return names.get(tool_name, tool_name)


def trigger_ai_response(
    server,
    session_id: str,
    user_content: str,
    sender: str,
    attachments=None,
    parent_message_id=None,
):
    """Trigger an AI response for a web session."""
    adapter = getattr(server, "web_channel_adapter", None) or get_channel_adapter("web") or WebChannelAdapter()
    chat_request = adapter.build_chat_request(
        conversation_id=session_id,
        content=user_content,
        sender=sender,
        attachments=attachments,
        parent_message_id=parent_message_id,
    )
    return trigger_ai_response_for_request(server, chat_request, adapter=adapter)


def trigger_ai_response_for_request(server, chat_request: ChatRequest, adapter=None):
    """通过统一管道处理 Web 频道的 AI 请求。"""
    adapter = adapter or getattr(server, "web_channel_adapter", None) or get_channel_adapter("web") or WebChannelAdapter()
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )
    session_id = chat_request.conversation_id
    user_content = chat_request.content
    attachments = list(chat_request.attachments or [])
    parent_message_id = chat_request.parent_message_id
    channel_capabilities = adapter.get_capabilities()

    if not isinstance(attachments, list):
        attachments = []

    session = session_store.get_session(session_id)
    if not session:
        _log.warning(f"Session not found: {session_id}")
        return ChatResponse(error=f"Session not found: {session_id}")

    has_image = False
    try:
        for att in attachments:
            if isinstance(att, dict) and str(att.get("type", "")).startswith("image/"):
                has_image = True
                break
    except Exception:
        attachments = []

    server.log_message(
        "info",
        f"开始生成AI回复 for session {session_id[:8]}... (附件: {len(attachments)}, 图片: {has_image})",
    )

    # 创建停止事件
    stop_event = threading.Event()
    server.stop_events[session_id] = stop_event

    # 进度/待办卡片
    progress_reporter = None
    if (
        channel_capabilities.supports_progress_updates
        and server.PROGRESS_CARD_AVAILABLE
        and server.progress_card_manager
    ):
        progress_reporter = WebProgressReporter(
            server, session_id, parent_message_id, session_store
        )

    # 确定是否启用工具
    tools = None
    has_tools = (
        _looks_like_tool_request(user_content)
        and getattr(server, "ai_config", {}).get("supports_tools", True)
    )
    if has_tools:
        try:
            from nbot.services.tools import get_enabled_tools
            tools = get_enabled_tools()
        except Exception:
            tools = None

    # 构建管道上下文和回调
    ctx = PipelineContext(
        chat_request=chat_request,
        adapter=adapter,
        stop_event=stop_event,
    )
    callbacks = WebCallbacks(
        server=server,
        session_store=session_store,
        session_id=session_id,
        adapter=adapter,
        parent_message_id=parent_message_id,
        progress_reporter=progress_reporter,
    )

    # 上下文字符预算
    try:
        context_char_budget = int(
            getattr(server, "ai_config", {}).get("max_context_length", 100000)
        )
    except (TypeError, ValueError):
        context_char_budget = 100000
    context_char_budget = max(100000, context_char_budget)

    def run_pipeline():
        try:
            pipeline = AIPipeline()
            result = pipeline.process(
                ctx, callbacks,
                tools=tools,
                max_context_chars=context_char_budget,
            )

            # 更新 token 统计
            _update_web_token_stats(server, result.usage, session_id)

            return result
        finally:
            server.stop_events.pop(session_id, None)
            if progress_reporter:
                progress_reporter.dispose()

    # 使用后台任务执行管道
    server.socketio.start_background_task(run_pipeline)
    return ChatResponse(metadata={"scheduled": True, "session_id": session_id})


def _update_web_token_stats(server, usage: dict, session_id: str):
    """更新 Web 频道的 token 统计（统一持久化到磁盘）。"""
    try:
        if not usage:
            return
        total = usage.get("total_tokens", 0)
        if not total:
            return

        from nbot.core.token_stats import get_token_stats_manager

        get_token_stats_manager().record_usage(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            model=getattr(server, "ai_model", "") or "",
            session_id=session_id,
            channel_type="web",
            user_id=session_id,
        )
    except Exception:
        pass


def get_ai_response(self, messages: List[Dict]) -> str:
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


def stream_ai_response(self, messages: List[Dict], session_id: str, callback):
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


def stream_provider_response_to_web(
    server,
    messages: List[Dict],
    session_id: str,
    message: Dict,
    thinking_content: str = None,
) -> str:
    """Stream provider chunks directly to the web chat bubble."""
    if not server.ai_client:
        raise RuntimeError("AI client not initialized")

    stream_iter = server.ai_client.chat_completion(
        model=server.ai_model,
        messages=messages,
        stream=True,
    )

    message["content"] = ""
    server.socketio.emit(
        "ai_stream_start",
        {
            "session_id": session_id,
            "message": message,
            "thinking_content": thinking_content,
        },
        room=session_id,
    )

    content_parts = []
    pending_parts = []
    last_emit_at = time.monotonic()

    def normalize_provider_chunk(raw_chunk: str) -> str:
        chunk_text = str(raw_chunk or "")
        if not chunk_text:
            return ""
        existing = "".join(content_parts)
        if not existing:
            return chunk_text
        if chunk_text.startswith(existing):
            return chunk_text[len(existing):]
        if existing.endswith(chunk_text):
            return ""

        max_overlap = min(len(existing), len(chunk_text), 32)
        for overlap in range(max_overlap, 2, -1):
            if existing.endswith(chunk_text[:overlap]):
                return chunk_text[overlap:]
        return chunk_text

    def emit_pending(force: bool = False):
        nonlocal last_emit_at
        if not pending_parts:
            return
        pending_text = "".join(pending_parts)
        if not force and len(pending_text) < 4 and time.monotonic() - last_emit_at < 0.02:
            return
        pending_parts.clear()
        server.socketio.emit(
            "ai_stream_chunk",
            {
                "session_id": session_id,
                "message_id": message["id"],
                "chunk": pending_text,
                "is_end": False,
            },
            room=session_id,
        )
        last_emit_at = time.monotonic()
        server.socketio.sleep(0)

    try:
        for chunk in stream_iter:
            if chunk is None:
                continue
            chunk = normalize_provider_chunk(chunk)
            if not chunk:
                continue
            content_parts.append(chunk)
            pending_parts.append(chunk)
            emit_pending()
    except Exception as e:
        _log.error(f"Provider stream error: {e}", exc_info=True)
        if not content_parts:
            raise
        error_chunk = f"\n\n[stream interrupted: {e}]"
        content_parts.append(error_chunk)
        pending_parts.append(error_chunk)
        emit_pending(force=True)
    else:
        emit_pending(force=True)
    finally:
        server.socketio.emit(
            "ai_stream_end",
            {"session_id": session_id, "message_id": message["id"], "is_end": True},
            room=session_id,
        )

    final_content = "".join(content_parts)
    message["content"] = final_content
    return final_content


def stream_send_response(
    server, session_id: str, message: Dict, thinking_content: str = None
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
        server.socketio.emit(
            "ai_stream_start",
            {
                "session_id": session_id,
                "message": message,
                "thinking_content": thinking_content,
            },
            room=session_id,
        )
        _log.info("[Stream] 已发送 ai_stream_start")

        # 清理并分割内容
        content = content.strip()

        # 发送内容片段（每10个字符一段）
        chunk_size = 10
        chunk_count = 0
        for i in range(0, len(content), chunk_size):
            chunk = content[i : i + chunk_size]
            server.socketio.emit(
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
        server.socketio.emit(
            "ai_stream_end",
            {"session_id": session_id, "message_id": message["id"], "is_end": True},
            room=session_id,
        )
        _log.info("[Stream] 流式发送完成")

    except Exception as e:
        _log.error(f"Stream send error: {e}", exc_info=True)
        # 降级为普通发送
        server.socketio.emit(
            "ai_response",
            {"session_id": session_id, "message": message},
            room=session_id,
        )


def get_ai_response_with_images(
    server, messages: List[Dict], image_urls: List[str], user_question: str = None
) -> str:
    """获取带图片的 AI 回复（多模态）"""
    try:
        # 获取图片理解模型配置（新架构）
        vision_config = get_vision_model_config()
        api_key = None
        base_url = ""
        model = "zai-org/GLM-4.6V"
        provider_type = "openai_compatible"
        system_prompt = "请详细描述这张图片的内容。"

        if vision_config and vision_config.get("api_key"):
            # 使用新架构的配置
            api_key = vision_config.get("api_key")
            base_url = vision_config.get("base_url", "")
            model = vision_config.get("model", "zai-org/GLM-4.6V")
            provider_type = vision_config.get("provider_type", "openai_compatible")
            system_prompt = vision_config.get("system_prompt", "请详细描述这张图片的内容。")
        else:
            # 回退到旧的配置方式
            if not server.ai_client:
                return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

            api_key = getattr(server.ai_client, "api_key", None)
            base_url = getattr(server.ai_client, "base_url", None)
            model = getattr(server.ai_client, "pic_model", None) or "zai-org/GLM-4.6V"
            provider_type = getattr(server.ai_client, "provider_type", "openai_compatible")
            system_prompt = "请详细描述这张图片的内容。"

            # 尝试从config.ini获取silicon_api_key
            if not api_key:
                try:
                    import configparser
                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    api_key = config.get("ApiKey", "silicon_api_key", fallback="") or config.get("ApiKey", "api_key", fallback="")
                    base_url = "https://api.siliconflow.cn/v1"
                    model = config.get("pic", "model", fallback="zai-org/GLM-4.6V")
                except Exception:
                    pass

        if not api_key:
            _log.warning("API key not configured for image processing")
            return "图片处理服务未配置 API Key，请在 AI 配置中心配置图片理解模型。"

        # 构建多模态消息 - 简化版本，只包含当前图片，不包含历史记录
        multimodal_messages = []

        # 添加系统提示
        multimodal_messages.append({
            "role": "system",
            "content": "你是一个专业的图片分析助手。请详细描述图片中的内容，包括场景、人物、物体、颜色、氛围等细节。如果用户有具体问题，请结合图片内容回答。"
        })

        # 构建用户内容（图片 + 文本）
        user_content = []
        for img_url in image_urls:
            user_content.append(
                {"type": "image_url", "image_url": {"url": img_url}}
            )

        # 添加用户的原始问题或默认提示
        if user_question:
            user_text = user_question
        else:
            user_text = system_prompt
        user_content.append({"type": "text", "text": user_text})

        multimodal_messages.append({"role": "user", "content": user_content})

        # 调用多模态模型
        import requests

        # 构建请求URL
        if provider_type == "siliconflow" or "siliconflow" in base_url:
            url = "https://api.siliconflow.cn/v1/chat/completions"
        else:
            url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)

        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
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
        return "图片处理失败：缺少 requests 库。"
    except Exception as e:
        # 回退到普通响应
        if user_question:
            temp_messages = messages.copy()
            temp_messages.append({"role": "user", "content": user_question})
            return server._get_ai_response(temp_messages)
        return f"处理图片时出错: {str(e)}"


def get_ai_response_with_tools(
server,
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
        if not server.ai_client:
            return {"content": "AI 服务未配置"}

        import requests

        # 从设置中获取超时时间，默认 120 秒
        timeout = server.settings.get("api_timeout", 120)
        max_retries = server.settings.get("api_retry_count", 3)

        # 使用适当的超时：有工具时至少 60 秒，无工具时至少 30 秒
        # 这样可以更频繁地检查停止事件，同时不会因为太短而频繁超时
        if tools:
            api_timeout = max(timeout, 60)  # 有工具调用时至少 60 秒
        else:
            api_timeout = max(timeout, 30)  # 无工具调用时至少 30 秒

        # 检查是否应该使用 Silicon API
        # 只有在明确指定 use_silicon=True 且有 Silicon API key 时才使用
        if use_silicon:
            silicon_api_key = getattr(server.ai_client, "silicon_api_key", None)
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
                    _log.info("[AI] 检测到停止信号，中断 Silicon API 请求")
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
            url = resolve_chat_completion_url(
                server.ai_base_url,
                model=server.ai_model or "",
                provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
            )

            headers = {
                "Authorization": f"Bearer {server.ai_api_key}",
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

            payload = build_chat_completion_payload(
                server.ai_model,
                processed_messages,
                base_url=server.ai_base_url,
                provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
                tools=tools,
                tool_choice="auto",
            )

            # 记录 payload 大小
            import json

            payload_size = len(json.dumps(payload, ensure_ascii=False))
            _log.info(
                f"[AI] 发送请求到 {url}, 模型={server.ai_model}, 工具数={len(tools) if tools else 0}, payload大小={payload_size} bytes"
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
                    _log.info("[AI] 检测到停止信号，中断 API 请求")
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

        normalized = normalize_chat_completion_data(
            data,
            base_url=server.ai_base_url or "",
            model=server.ai_model or "",
            provider_type=server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
            fallback_tool_parser=server._parse_tool_call_from_text,
        )
        message = normalized.raw_message
        finish_reason = normalized.finish_reason

        _log.info(
            f"[AI] API 响应: finish_reason={finish_reason}, has_tool_calls={'tool_calls' in message}"
        )
        _log.debug(f"[AI] 工具数量: {len(tools) if tools else 0}")

        # 记录完整响应用于调试
        if "tool_calls" not in message and message.get("content", ""):
            _log.warning(
                f"[AI] 工具未生效，content 前100字符: {message.get('content', '')[:100]}"
            )

        result = normalized.to_dict()

        # 获取AI思考内容（如果API返回了的话）
        supports_reasoning = server.ai_config.get("supports_reasoning", True)
        thinking_content = normalized.thinking_content if supports_reasoning else ""
        if not supports_reasoning and "thinking_content" in result:
            result.pop("thinking_content", None)
        if thinking_content:
            _log.debug(f"[AI] 收到思考内容: {len(thinking_content)} 字符")
        elif normalized.thinking_content and not supports_reasoning:
            _log.info("[AI] 当前模型配置声明不展示 reasoning 字段，已忽略思考内容")
        if result.get("tool_calls") and "[TOOL_CALL]" in result.get("content", ""):
            cleaned = re.sub(
                r"\[TOOL_CALL\]\s*.*?\s*\[/TOOL_CALL\]\s*",
                "",
                result["content"],
                flags=re.DOTALL,
            )
            cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
            result["content"] = cleaned.strip()
            _log.info(f"[AI] 成功解析 {len(result['tool_calls'])} 个工具调用")

        return result

    except StopIteration as e:
        # 用户停止生成
        _log.info(f"[AI] 停止生成: {e}")
        raise  # 重新抛出，让调用者处理

    except Exception as e:
        _log.error(f"AI with tools error: {e}")
        # 回退到普通 AI 调用
        content = server._get_ai_response(messages)
        return {"content": content}


def parse_tool_call_from_text(self, content: str) -> list:
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

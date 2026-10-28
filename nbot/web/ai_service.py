import json
import logging
import os
import re
import threading
import time
import uuid
from datetime import datetime
from typing import Any

from nbot.channels.registry import get_channel_adapter
from nbot.channels.web import WebChannelAdapter
from nbot.core import (
    ChatRequest,
    ChatResponse,
    WebSessionStore,
    repair_mojibake_text,
    response_json_utf8,
)
from nbot.core.ai_pipeline import (
    AIPipeline,
    ModePolicy,
    PipelineCallbacks,
    PipelineContext,
    PipelineResult,
)
from nbot.core.background_tasks import submit_background_task
from nbot.core.model_proxy import model_proxy_request_kwargs
from nbot.core.sampling import request_extra_body
from nbot.core.session_store import merge_message_variant
from nbot.web.utils.config_loader import get_vision_model_config

try:
    from nbot.core.knowledge import get_knowledge_manager

    KNOWLEDGE_MANAGER_AVAILABLE = True
except ImportError:
    get_knowledge_manager = None
    KNOWLEDGE_MANAGER_AVAILABLE = False

# WORKSPACE_AVAILABLE 会在函数中从 server 对象获取

_log = logging.getLogger(__name__)


def _runtime_timeline_state_signature(entry: dict) -> tuple:
    ignore_keys = {"timestamp", "message_id", "message_index"}
    return tuple(
        (key, entry.get(key))
        for key in sorted(entry.keys())
        if key not in ignore_keys
    )


def _resolve_session_character_name(server, session: dict) -> str:
    if not isinstance(session, dict):
        session = {}

    sender_name = str(session.get("sender_name") or "").strip()
    if sender_name:
        return sender_name

    character_id = str(session.get("character_id") or "").strip()
    if character_id:
        try:
            from nbot.character.repository import ProfileRepository

            base_dir = getattr(server, "base_dir", None) or os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..")
            )
            profile = ProfileRepository(base_dir).get(character_id)
            if profile and profile.name:
                return str(profile.name).strip()
        except Exception:
            pass

    personality = getattr(server, "personality", {}) or {}
    if isinstance(personality, dict):
        return str(personality.get("name") or "").strip()
    return ""


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
    metadata: dict | None = None,
):
    channel_adapter = adapter or get_channel_adapter("web") or WebChannelAdapter()
    return channel_adapter.build_assistant_message(
        chat_response,
        conversation_id=session_id,
        sender=sender,
        metadata=metadata,
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
        "timestamp": datetime.now().astimezone().isoformat(),
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

    def __init__(
        self,
        server,
        session_id: str,
        parent_message_id: str,
        session_store,
        is_agent: bool = False,
        progress_card=None,
        todo_card=None,
    ):
        self.server = server
        self.session_id = session_id
        self.parent_message_id = parent_message_id
        self.session_store = session_store
        self.progress_card = progress_card
        self.todo_card = todo_card
        self.is_agent = is_agent
        self._init_cards()

    def _init_cards(self):
        # Agent 模式强制创建进度/待办卡片;否则仍受 PROGRESS_CARD_AVAILABLE 开关控制
        if (
            self.progress_card is None
            and self.server.PROGRESS_CARD_AVAILABLE
            and self.server.progress_card_manager
            and self.server.socketio
        ):
            self.progress_card = self.server.progress_card_manager.create_card(
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
                max_iterations=50,
                is_agent=self.is_agent,
            )

        if (
            self.todo_card is None
            and self.server.TODO_CARD_AVAILABLE
            and self.server.todo_card_manager
            and self.server.socketio
        ):
            self.todo_card = self.server.todo_card_manager.create_card(
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
                is_agent=self.is_agent,
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
            duration_ms = None
            if isinstance(result, dict):
                duration_ms = result.pop("_duration_ms", None)
            result_preview = json.dumps(result, ensure_ascii=False)[:200]
            if duration_ms is not None:
                result_preview += f" · {duration_ms}ms"
            self.progress_card.update(
                StepType.TOOL_DONE,
                _get_tool_display_name(tool_name),
                result_preview,
                step_full_result=result,
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
            self.progress_card.complete("✅ 处理完成")

    def on_waiting_confirmation(self, ctx, command: str, request_id: str) -> None:
        """Agent 向用户提问时推送 Web 弹窗事件（ask_user_question）。"""
        try:
            self.server.socketio.emit(
                "ask_user_question",
                {
                    "session_id": self.session_id,
                    "request_id": request_id,
                    "question": command,
                },
                room=self.session_id,
            )
        except Exception as exc:
            _log.debug("[AskUser] 弹窗事件发送失败: %s", exc)

    def on_send_file(self, ctx, file_path: str, filename: str) -> None:
        if not file_path or not os.path.isfile(file_path):
            return
        try:
            from nbot.web.file_gateway import build_file_metadata

            file_name = filename or os.path.basename(file_path)
            file_meta = build_file_metadata(self.server, file_path, filename=file_name)
            message = _build_channel_assistant_message(
                ChatResponse(final_content=f"[File: {file_name}]"),
                session_id=self.session_id,
                sender="AI",
                metadata={"file": file_meta},
            )
            session = self.session_store.get_session(self.session_id)
            if session:
                self.session_store.append_message(self.session_id, message)
            self.server.socketio.emit("new_message", message, room=self.session_id)
        except Exception as exc:
            _log.warning("[FileGateway] failed to emit file card: %s", exc)

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

    def load_messages(self, ctx: PipelineContext) -> list[dict]:
        import copy

        from nbot.core.session_store import visible_messages

        session = self.session_store.get_session(self.session_id)
        if not session:
            return []
        messages = copy.deepcopy(visible_messages(session.get("messages", [])))
        # swipes：重新生成时上下文截断到目标消息之前（目标消息本身不进上下文）
        swipe_target_id = str((ctx.metadata or {}).get("swipe_target_id") or "")
        if swipe_target_id:
            target_index = next(
                (
                    i
                    for i, msg in enumerate(messages)
                    if str(msg.get("id") or "") == swipe_target_id
                ),
                -1,
            )
            if target_index >= 0:
                messages = messages[:target_index]
        return messages

    def auto_compress_context(
        self, ctx: PipelineContext, messages: list, threshold_chars: int
    ) -> list:
        """上下文达到 80% 阈值时自动摘要压缩（失败时按未压缩继续）。"""
        from nbot.core.auto_compression import auto_compress
        from nbot.core.token_stats import PURPOSE_COMPRESSION, get_token_stats_manager

        # swipes 模式下上下文已按目标截断，压缩会破坏目标之后的消息，跳过
        if ctx.metadata.get("swipe_target_id"):
            return messages
        session = self.session_store.get_session(self.session_id)
        if not session:
            return messages

        ts_map = getattr(self.server, "_auto_compress_ts", None)
        if ts_map is None:
            ts_map = {}
            self.server._auto_compress_ts = ts_map
        last_ts = float(ts_map.get(self.session_id, 0.0))

        def _chat_complete(complete_messages):
            if not self.server.ai_client:
                raise RuntimeError("AI 服务不可用")
            return self.server.ai_client.chat_completion(
                model=self.server.ai_model,
                messages=complete_messages,
                stream=False,
            )

        def _on_usage(usage, model):
            if not usage:
                return
            try:
                get_token_stats_manager().record_usage(
                    prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                    completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                    total_tokens=getattr(usage, "total_tokens", 0) or 0,
                    model=model or self.server.ai_model or "",
                    session_id=self.session_id,
                    channel_type="web",
                    source="web",
                    purpose=PURPOSE_COMPRESSION,
                )
            except Exception:
                pass

        self.server.socketio.emit(
            "context_auto_compress",
            {"session_id": self.session_id, "phase": "start"},
            room=self.session_id,
        )
        try:
            new_messages, info = auto_compress(
                messages,
                chat_complete=_chat_complete,
                max_context_chars=max(threshold_chars, 1),
                trigger_ratio=1.0,
                last_compress_ts=last_ts,
                on_usage=_on_usage,
                model_label=str(self.server.ai_model or ""),
            )
        finally:
            self.server.socketio.emit(
                "context_auto_compress",
                {"session_id": self.session_id, "phase": "end"},
                room=self.session_id,
            )

        if not info.get("compressed"):
            return messages

        self._archive_compressed_messages(messages, new_messages)
        self.session_store.replace_messages(self.session_id, new_messages)
        ts_map[self.session_id] = __import__("time").time()
        return new_messages

    def _archive_compressed_messages(self, old_messages: list, new_messages: list) -> None:
        """将被压缩的原始消息归档到会话的归档会话（复用压缩按钮的归档语义）。"""
        import time as _time

        new_ids = {str(m.get("id")) for m in new_messages if isinstance(m, dict) and m.get("id")}
        to_archive = [
            m for m in old_messages
            if isinstance(m, dict)
            and m.get("role") != "system"
            and (not m.get("id") or str(m.get("id")) not in new_ids)
        ]
        if not to_archive:
            return
        session = self.session_store.get_session(self.session_id)
        if not session:
            return

        archive_id = session.get("archive_session_id")
        archive = self.session_store.get_session(archive_id) if archive_id else None
        if not archive:
            from datetime import datetime as _dt
            now = _dt.now().isoformat()
            archive_id = str(uuid.uuid4())
            archive = {
                "id": archive_id,
                "name": f"📦 {session.get('name') or '会话'} - 归档",
                "type": "web",
                "created_at": now,
                "archived": True,
                "archived_at": now,
                "is_archive": True,
                "read_only": True,
                "source_session_id": self.session_id,
                "messages": [],
                "system_prompt": "",
                "character_id": session.get("character_id") or session.get("sender_name", ""),
                "sender_name": session.get("sender_name", ""),
                "sender_avatar": session.get("sender_avatar", ""),
                "sender_portrait": session.get("sender_portrait", ""),
                "scenario": session.get("scenario", ""),
            }
            session["archive_session_id"] = archive_id

        archive.setdefault("messages", []).append({
            "id": f"archive_divider_{int(_time.time())}",
            "role": "system",
            "content": f"━━━ 自动压缩于 {__import__('datetime').datetime.now().strftime('%Y-%m-%d %H:%M')} ({len(to_archive)} 条消息) ━━━",
            "timestamp": __import__("datetime").datetime.now().isoformat(),
        })
        archive["messages"].extend(to_archive)
        self.session_store.set_session(archive_id, archive)
        self.session_store.set_session(self.session_id, session)

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        session = self.session_store.get_session(self.session_id)
        # agent 模式不继承角色卡提示词
        if (session or {}).get("session_mode") == "agent":
            return str(session.get("system_prompt") or "").strip()
        if session and session.get("system_prompt"):
            return str(session.get("system_prompt") or "").strip()
        return str(
            getattr(self.server, "personality", {}).get("systemPrompt") or ""
        ).strip()

    # swipes：合并新候选时从新消息同步到原消息的字段
    _SWIPE_SYNC_KEYS = (
        "model",
        "thinking_cards",
        "todo_cards",
        "change_cards",
        "attachments",
        "tool_call_history",
        "can_continue",
        "token_usage",
        "perf",
        "citations",
    )

    def save_assistant_message(self, ctx: PipelineContext, message: dict) -> None:
        if not message.get("sender"):
            # 群聊模式：使用发言角色名称
            speaker_name = ctx.metadata.get("group_speaker_name", "")
            if speaker_name:
                message["sender"] = speaker_name
            else:
                session = self.session_store.get_session(self.session_id) or {}
                message["sender"] = session.get("sender_name") or "AI"
        if not message.get("timestamp"):
            message["timestamp"] = datetime.now().astimezone().isoformat()
        if not message.get("session_id"):
            message["session_id"] = self.session_id
        if not message.get("source"):
            message["source"] = "web"
        if ctx.metadata.get("is_proactive_chat"):
            message["source"] = "proactive_chat"
            message["is_proactive_chat"] = True
            if ctx.metadata.get("proactive_chat_triggered_at"):
                message["proactive_chat_triggered_at"] = ctx.metadata["proactive_chat_triggered_at"]
        self._attach_message_perf(ctx, message)
        self._attach_knowledge_citations(ctx, message)
        if self._merge_swipe_variant(ctx, message):
            self._try_auto_name_session()
            return
        self.session_store.append_message(self.session_id, message)
        self._try_auto_name_session()

    @staticmethod
    def _knowledge_citations(ctx: PipelineContext) -> list:
        """取出本次命中的知识库引用；没有/格式异常都返回空列表。"""
        try:
            citations = (ctx.metadata or {}).get("knowledge_citations") or []
            return list(citations) if isinstance(citations, list) else []
        except Exception:
            return []

    def _attach_knowledge_citations(self, ctx: PipelineContext, message: dict) -> None:
        """把知识库引用挂到消息上（前端气泡/引用面板读 message.citations）。"""
        citations = self._knowledge_citations(ctx)
        if citations:
            message["citations"] = citations

    def _attach_message_perf(self, ctx: PipelineContext, message: dict) -> None:
        """消息级性能指标：输出 tok/s 与生成耗时（气泡展示用）。"""
        usage = getattr(ctx, "usage", None) or {}
        duration_ms = ctx.metadata.get("duration_ms")
        if not usage and not duration_ms:
            return
        perf: dict = {}
        try:
            if duration_ms:
                perf["duration_ms"] = round(float(duration_ms))
        except (TypeError, ValueError):
            pass
        output_tokens = 0
        try:
            output_tokens = int(usage.get("completion_tokens") or 0)
        except (TypeError, ValueError):
            pass
        if output_tokens and duration_ms:
            try:
                perf["tokens_per_second"] = round(
                    output_tokens / (float(duration_ms) / 1000.0), 1
                )
            except (ZeroDivisionError, TypeError, ValueError):
                pass
        if usage:
            perf["token_usage"] = {
                "input": usage.get("prompt_tokens") or 0,
                "output": output_tokens,
                "total": usage.get("total_tokens") or 0,
            }
        if perf:
            message["perf"] = perf

    def _merge_swipe_variant(self, ctx: PipelineContext, message: dict) -> bool:
        """swipes：把新生成的内容作为候选合并回原消息，而不是追加新气泡。"""
        swipe_target_id = str(ctx.metadata.get("swipe_target_id") or "")
        if not swipe_target_id:
            return False
        session = self.session_store.get_session(self.session_id)
        if not session:
            return False
        messages = self.session_store.get_messages(self.session_id)
        target = next(
            (
                msg
                for msg in messages
                if isinstance(msg, dict) and str(msg.get("id") or "") == swipe_target_id
            ),
            None,
        )
        if target is None:
            return False
        # 同轮生成的后续保存（如剥离生图标签回写）更新最后一个候选而非追加
        update_last = bool(ctx.metadata.get("_swipe_merged", False))
        if not merge_message_variant(
            target,
            message,
            model_id=str(ctx.metadata.get("model_id") or ""),
            update_last=update_last,
        ):
            return False
        ctx.metadata["_swipe_merged"] = True
        for key in self._SWIPE_SYNC_KEYS:
            target.pop(key, None)
            if message.get(key):
                target[key] = message[key]
        self.session_store.set_session(self.session_id, session)
        self.server.socketio.emit(
            "ai_response",
            {
                "session_id": self.session_id,
                "message": target,
                "swipe": True,
                "system_prompt": ctx.metadata.get("composed_system_prompt", ""),
                "prompt_stack_debug": ctx.metadata.get("prompt_stack_debug", []),
                **(
                    {"citations": self._knowledge_citations(ctx)}
                    if self._knowledge_citations(ctx)
                    else {}
                ),
            },
            room=self.session_id,
        )
        return True

    # ---- AI 模型交互 ----

    def _session_model_configs(self, model_configs: list) -> list:
        """按会话级模型覆盖重排候选队列（未开启时原样返回）。"""
        try:
            from nbot.web.session_model import resolve_model_configs

            session = None
            if self.session_store:
                session = self.session_store.get_session(self.session_id)
            resolved = resolve_model_configs(
                session,
                model_configs,
                getattr(self.server, "ai_models", []) or [],
            )
            if resolved and model_configs and resolved[0] is not model_configs[0]:
                _log.info(
                    "[ModelOverride] session=%s -> %s",
                    str(self.session_id)[:8],
                    resolved[0].get("name") or resolved[0].get("model") or "",
                )
            return resolved or model_configs
        except Exception as exc:
            _log.debug("[ModelOverride] resolve failed: %s", exc)
            return model_configs

    def build_model_call(self, ctx, tools):
        """Web 频道使用服务器的 AI 方法，支持故障转移队列。"""
        server = self.server
        from nbot.services.ai import refresh_runtime_ai_config
        from nbot.web.utils.config_loader import get_model_configs_by_purpose

        runtime_ai = refresh_runtime_ai_config()
        purpose = runtime_ai.get("purpose", "chat")
        model_configs = get_model_configs_by_purpose(purpose)
        resolved_configs = self._session_model_configs(model_configs)
        override_active = bool(
            model_configs and resolved_configs and resolved_configs[0] is not model_configs[0]
        )

        if len(resolved_configs) > 1 or override_active:
            # 多模型或会话覆盖：统一走故障转移包装（覆盖模型排首位）
            from nbot.core.ai_pipeline import AIPipeline
            pipeline = AIPipeline()
            session_id = ""
            try:
                session_id = getattr(ctx, "chat_request", None).session_id if getattr(ctx, "chat_request", None) else ""
            except Exception:
                pass
            return pipeline._wrap_with_failover(
                resolved_configs, purpose, tools=tools, session_id=session_id
            )

        # Single model: direct call
        return lambda messages, stop_event=None: _call_web_ai(
            server, messages, tools, stop_event
        )

    def build_model_call_streaming(self, ctx, tools):
        """返回 provider 级流式迭代器，支持故障转移队列。"""
        server = self.server
        session_id = self.session_id
        from nbot.services.ai import refresh_runtime_ai_config
        from nbot.web.utils.config_loader import get_model_configs_by_purpose

        runtime_ai = refresh_runtime_ai_config()
        if not runtime_ai.get("supports_stream", True):
            return None
        if not runtime_ai.get("stream", True):
            return None

        purpose = runtime_ai.get("purpose", "chat")
        model_configs = get_model_configs_by_purpose(purpose)
        if not model_configs:
            model_configs = [runtime_ai]
        model_configs = self._session_model_configs(model_configs)

        def streamer(messages, stop_event=None):
            return _stream_to_web(
                server, messages, tools, session_id,
                stop_event, model_configs=model_configs,
            )

        return streamer

    # ---- 输出 / 回复 ----

    def send_response(self, ctx: PipelineContext, message: dict) -> None:
        if ctx.metadata.get("streamed"):
            # 流式已发送，无需再次发送
            return
        # 如果消息被过滤，不发送给客户端
        if message.get("filter_blocked"):
            return
        self.server.socketio.emit(
            "ai_response",
            {
                "session_id": self.session_id,
                "message": message,
                "system_prompt": ctx.metadata.get("composed_system_prompt", ""),
                "prompt_stack_debug": ctx.metadata.get("prompt_stack_debug", []),
                **(
                    {"citations": self._knowledge_citations(ctx)}
                    if self._knowledge_citations(ctx)
                    else {}
                ),
            },
            room=self.session_id,
        )
        self._try_send_push(message)

    def on_stream_start(self, ctx: PipelineContext, message: dict) -> None:
        # 群聊模式：在消息中附加 sender 以便前端区分不同角色的气泡
        if ctx and ctx.metadata:
            speaker_name = ctx.metadata.get("group_speaker_name", "")
            if speaker_name and not message.get("sender"):
                message["sender"] = speaker_name
        self.server.socketio.emit(
            "ai_stream_start",
            {"session_id": self.session_id, "message": message},
            room=self.session_id,
        )
        self.server.socketio.sleep(0)

    def on_stream_chunk(self, ctx: PipelineContext, chunk: str, message_id: str) -> None:
        payload = {
            "session_id": self.session_id,
            "message_id": message_id,
            "chunk": chunk,
            "is_end": False,
        }
        # 群聊模式：附加 sender 以便前端匹配到正确的流式气泡
        if ctx and ctx.metadata:
            speaker_name = ctx.metadata.get("group_speaker_name", "")
            if speaker_name:
                payload["sender"] = speaker_name
        self.server.socketio.emit(
            "ai_stream_chunk",
            payload,
            room=self.session_id,
        )
        self.server.socketio.sleep(0)

    def on_stream_end(self, ctx: PipelineContext, message_id: str) -> None:
        # 如果流式消息被过滤，通知客户端撤回
        streamed_msg = ctx.metadata.get("_streamed_message_ref")
        if streamed_msg and streamed_msg.get("filter_blocked"):
            self.server.socketio.emit(
                "message_filtered",
                {
                    "session_id": self.session_id,
                    "message_id": message_id,
                    "message": "当前内容被过滤",
                },
                room=self.session_id,
            )
            return
        end_payload = {
            "session_id": self.session_id,
            "message_id": message_id,
            "is_end": True,
            "system_prompt": ctx.metadata.get("composed_system_prompt", "") if ctx else "",
            "prompt_stack_debug": ctx.metadata.get("prompt_stack_debug", []) if ctx else [],
        }
        if ctx:
            citations = self._knowledge_citations(ctx)
            if citations:
                end_payload["citations"] = citations
        # 群聊模式：附加 sender 以便前端匹配到正确的流式气泡
        if ctx and ctx.metadata:
            speaker_name = ctx.metadata.get("group_speaker_name", "")
            if speaker_name:
                end_payload["sender"] = speaker_name
        self.server.socketio.emit(
            "ai_stream_end",
            end_payload,
            room=self.session_id,
        )
        self.server.socketio.sleep(0)
        if streamed_msg and streamed_msg.get("content"):
            self._try_send_push(streamed_msg)

    def _try_send_push(self, message: dict) -> None:
        """尝试发送浏览器推送通知（仅在用户不在页面时）。"""
        try:
            from nbot.web.routes.push import send_web_push
            content = str(message.get("content") or "").strip()
            if not content:
                return
            preview = content[:100]
            if len(content) > 100:
                preview += "..."
            sender = message.get("sender") or "AI"
            send_web_push(
                self.server,
                title=sender,
                body=preview,
                url=f"/?session_id={self.session_id}",
                session_id=self.session_id,
                tag="nekobot-message",
                skip_visible=True,
            )
        except Exception as exc:
            _log.debug("Push notification skipped: %s", exc)

    # ---- 表情包 ----

    def send_sticker(
        self, ctx: PipelineContext, sticker_info: dict[str, Any]
    ) -> None:
        """Web 频道通过 Socket.IO 发送表情包图片消息（裸图，无气泡包裹）"""
        import uuid
        sticker_message = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": "",
            "timestamp": __import__("datetime").datetime.now().astimezone().isoformat(),
            "sender": "AI",
            "source": "sticker",
            "sticker": {
                "url": sticker_info.get("url", ""),
                "anime_name": sticker_info.get("anime_name", ""),
                "artist_name": sticker_info.get("artist_name", ""),
            },
        }
        self.server.socketio.emit(
            "ai_response",
            {"session_id": self.session_id, "message": sticker_message},
            room=self.session_id,
        )
        # 同时保存到会话历史
        self.session_store.append_message(self.session_id, sticker_message)

    # ---- 角色生图 ----

    def send_image(
        self, ctx: PipelineContext, image_info: dict[str, Any]
    ) -> None:
        """Web 频道通过 Socket.IO 发送 AI 生成的图片（裸图，无气泡包裹）"""
        import uuid
        image_message = {
            "id": str(uuid.uuid4()),
            "role": "assistant",
            "content": "",
            "timestamp": __import__("datetime").datetime.now().astimezone().isoformat(),
            "sender": "AI",
            "source": "generated_image",
            "image": {
                "url": image_info.get("url", ""),
                "prompt": image_info.get("prompt", ""),
                "trigger": image_info.get("trigger", "tag"),
            },
        }
        self.server.socketio.emit(
            "ai_response",
            {"session_id": self.session_id, "message": image_message},
            room=self.session_id,
        )
        # 同时保存到会话历史
        self.session_store.append_message(self.session_id, image_message)

    # ---- 进度 ----

    def get_progress_reporter(self, ctx: PipelineContext):
        if self._progress:
            return self._progress
        from nbot.core.ai_pipeline import NoOpProgressReporter
        return NoOpProgressReporter()

    # ---- 工具确认 ----

    def on_confirmation_required(self, ctx: PipelineContext, request_id: str, command: str) -> None:
        from nbot.services.tools import get_exec_main_command

        self.server.socketio.emit(
            "exec_confirm_request",
            {
                "request_id": request_id,
                "command": command,
                "main_command": get_exec_main_command(command),
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

    def search_knowledge_with_citations(self, ctx: PipelineContext, query: str):
        """知识库检索：文本沿用既有路径，另取结构化 citations 随响应返回。

        空知识库 / 检索失败时返回 (文本, [])，不新增字段也不报错。
        """
        text = self.search_knowledge(ctx, query)
        if not text or not query or get_knowledge_manager is None:
            return text, []
        try:
            km = get_knowledge_manager()
            if not km:
                return text, []
            payload = km.search_with_citations(query, base_id=None, top_k=3)
            citations = list(payload.get("citations") or [])
            if not citations:
                return text, []
            ctx.metadata["knowledge_citations"] = citations
            return text, citations
        except Exception as exc:
            _log.debug("[Knowledge] citations 取回失败（忽略）: %s", exc)
            return text, []

    # ---- 工作区 ----

    def ensure_workspace(self, ctx: PipelineContext) -> str:
        if getattr(self.server, "WORKSPACE_AVAILABLE", False) and self.server.workspace_manager:
            return self.server.workspace_manager.get_or_create(
                self.session_id, "web"
            )
        return ""

    def get_workspace_context(self, ctx: PipelineContext) -> dict:
        # 从会话中获取角色名
        character_name = ""
        target_id = ""
        session = self.session_store.get_session(self.session_id)
        if session:
            character_name = _resolve_session_character_name(self.server, session)
            target_id = session.get("user_id") or session.get("qq_id") or ""

        # 群聊模式：优先使用当前发言角色名，避免记忆混用"群聊"角色名
        if ctx and ctx.metadata:
            speaker_name = str(ctx.metadata.get("group_speaker_name") or "").strip()
            if speaker_name:
                character_name = speaker_name

        # 只有拿不到会话时才回退到全局角色，避免旧会话被当前设置污染。
        if not session and not character_name:
            character_name = getattr(self.server, "personality", {}).get("name", "")

        context = {
            "session_id": self.session_id,
            "session_type": "web",
            "server": self.server,
        }
        if self.parent_message_id:
            context["parent_message_id"] = self.parent_message_id
        if self._progress:
            progress_card = getattr(self._progress, "progress_card", None)
            todo_card = getattr(self._progress, "todo_card", None)
            if progress_card:
                context["progress_card_id"] = progress_card.card_id
            if todo_card:
                context["todo_card_id"] = todo_card.card_id
        if target_id:
            context["target_id"] = str(target_id)
            context["user_id"] = str(target_id)
        if character_name:
            context["character_name"] = character_name
        return context

    # ---- 角色运行时 ----

    def get_character_context(self, ctx):
        """返回当前会话的角色身份标识（agent 模式默认跳过，开启继承后照常返回）"""
        session = self.session_store.get_session(self.session_id) if self.session_store else {}
        if not ModePolicy.character_features_enabled(session):
            return None
        from nbot.character.adapters.nekobot import get_web_character_context
        identity = get_web_character_context(
            self.server, self.session_store, self.session_id
        )
        speaker_id = str(ctx.metadata.get("group_speaker") or "").strip()
        if identity and speaker_id:
            identity.character_id = speaker_id
        return identity

    def get_character_runtime(self, ctx):
        """返回 CharacterRuntime 实例（agent 模式默认跳过，开启继承后照常返回）"""
        session = self.session_store.get_session(self.session_id) if self.session_store else {}
        if not ModePolicy.character_features_enabled(session):
            return None
        from nbot.character.adapters.nekobot import get_character_runtime_from_server
        return get_character_runtime_from_server(self.server)

    # ---- 响应完成 ----

    def _update_session_system_prompt(self, ctx: PipelineContext):
        """将 PromptStack 合成后的实时 system_prompt 回写到 session，供前端 i 按钮查看"""
        try:
            composed = ctx.metadata.get("composed_system_prompt")
            if not composed:
                return

            session = self.session_store.get_session(self.session_id)
            if not session:
                return

            session["system_prompt"] = composed

            # 同时保存角色运行时调试信息
            prompt_stack_debug = ctx.metadata.get("prompt_stack_debug") or []
            session["prompt_stack_debug"] = prompt_stack_debug

            # 保存角色运行时上下文摘要
            if hasattr(ctx, 'character_turn') and ctx.character_turn:
                turn = ctx.character_turn
                messages = session.get("messages", [])
                assistant_message_id = None
                assistant_message_index = None
                if isinstance(messages, list):
                    for idx in range(len(messages) - 1, -1, -1):
                        msg = messages[idx]
                        if isinstance(msg, dict) and msg.get("role") == "assistant":
                            assistant_message_id = msg.get("id")
                            assistant_message_index = idx
                            break
                snapshot = {
                    "character_id": getattr(turn.profile, "id", None) or getattr(turn.profile, "name", None),
                    "mood": turn.state.mood,
                    "mood_intensity": turn.state.mood_intensity,
                    "energy": turn.state.energy,
                    "affection": turn.relationship.affection,
                    "trust": turn.relationship.trust,
                    "familiarity": turn.relationship.familiarity,
                    "dependency": turn.relationship.dependency,
                    "security": turn.relationship.security,
                    "jealousy": turn.relationship.jealousy,
                    "plan_tone": turn.plan.tone,
                    "visible_emotion": turn.plan.visible_emotion,
                    "hidden_emotion": turn.plan.hidden_emotion,
                    "message_id": assistant_message_id,
                    "message_index": assistant_message_index,
                }
                session["character_runtime_snapshot"] = snapshot

                timeline = session.get("character_runtime_timeline", [])
                if not isinstance(timeline, list):
                    timeline = []
                entry = {
                    **{
                        key: snapshot.get(key)
                        for key in (
                            "character_id",
                            "mood",
                            "mood_intensity",
                            "energy",
                            "affection",
                            "trust",
                            "security",
                            "familiarity",
                            "dependency",
                            "jealousy",
                            "visible_emotion",
                            "hidden_emotion",
                            "message_id",
                            "message_index",
                        )
                    },
                    "timestamp": datetime.now().astimezone().isoformat(),
                }
                last = timeline[-1] if timeline else None
                if isinstance(last, dict) and _runtime_timeline_state_signature(last) == _runtime_timeline_state_signature(entry):
                    last.update(entry)
                else:
                    timeline.append(entry)
                session["character_runtime_timeline"] = timeline[-200:]

            self.session_store.set_session(self.session_id, session)
            try:
                self.server.socketio.emit(
                    "session_updated",
                    {
                        "action": "prompt_stack_updated",
                        "session_id": self.session_id,
                        "system_prompt": composed,
                        "prompt_stack_debug": prompt_stack_debug,
                    },
                    room=self.session_id,
                )
            except Exception:
                pass
        except Exception:
            pass

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
                or name.endswith("的对话")
            )

            # 会话级自动命名间隔：null=全局默认(10)；0=关闭自动命名；正整数=每 N 条
            # （PUT /api/sessions/<id> 的 auto_name_interval 字段设置）
            raw_interval = session.get("auto_name_interval")
            try:
                name_interval = int(raw_interval) if raw_interval is not None else 10
            except (TypeError, ValueError):
                name_interval = 10
            if name_interval <= 0:
                return

            # 首次命名：默认名称 + 至少一轮对话
            # 后续更新：按会话配置的间隔更新
            last_rename_count = session.get("_last_rename_count", 0)
            was_auto_named = bool(session.get("_auto_name_generated"))
            should_rename = is_default_name or (
                was_auto_named and total_count - last_rename_count >= name_interval
            )

            if not should_rename:
                return

            # 防止并发重复生成
            if getattr(self, "_naming_in_progress", False):
                return
            self._naming_in_progress = True
            unique_key = f"session_name:{self.session_id}"

            _log.info(f"开始为会话 {self.session_id[:8]} 自动生成名称 (当前: {name}, 消息数: {total_count})")

            import copy
            # 取最近对话作为上下文
            recent_msgs = copy.deepcopy(user_assistant_msgs[-10:])

            def generate_and_update():
                try:
                    new_name = self.server._generate_session_name(
                        recent_msgs,
                        session_id=self.session_id,
                    )
                    if new_name:
                        latest_session = (
                            self.session_store.get_session(self.session_id) or session
                        )
                        latest_session["name"] = new_name
                        latest_session["_auto_name_generated"] = True
                        latest_session["_last_rename_count"] = total_count
                        self.session_store.set_session(self.session_id, latest_session)
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

            future = submit_background_task(
                "session_auto_name",
                generate_and_update,
                unique_key=unique_key,
            )
            if future is None:
                self._naming_in_progress = False
        except Exception as e:
            _log.error(f"自动命名检查失败: {e}")

    # ---- 附件解析 ----

    def on_plot_choices_ready(
        self,
        ctx: PipelineContext,
        result: PipelineResult,
    ) -> None:
        plot_choices = (result.metadata or {}).get("plot_choices")
        if not plot_choices:
            return

        mermaid = ""
        graph = None
        try:
            from nbot.plot.graph_manager import get_plot_graph_manager

            plot_mgr = get_plot_graph_manager(
                data_dir=getattr(self.server, "data_dir", "data/web"),
            )
            mermaid = plot_mgr.generate_mermaid(self.session_id)
            graph = plot_mgr.get_graph(self.session_id)
        except Exception:
            pass

        self.server.socketio.emit(
            "plot_choices",
            {
                "session_id": self.session_id,
                "choices": plot_choices,
                "mermaid": mermaid,
                "graph": graph,
            },
            room=self.session_id,
        )
        try:
            self.server.socketio.sleep(0)
        except Exception:
            pass

    def resolve_attachment_data(self, ctx: PipelineContext, attachment: dict) -> dict | None:
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
            if path_to_use.startswith("/api/files/gateway/"):
                try:
                    from nbot.web.file_gateway import verify_file_token

                    token = path_to_use.split("/api/files/gateway/", 1)[1].split("?", 1)[0].split("/preview", 1)[0]
                    payload = verify_file_token(self.server, token)
                    file_path = payload.get("path")
                except Exception:
                    file_path = None
            elif os.path.isabs(path_to_use):
                try:
                    from nbot.web.file_gateway import is_allowed_file_path

                    if is_allowed_file_path(self.server, path_to_use):
                        file_path = path_to_use
                except Exception:
                    file_path = None
            elif path_to_use.startswith("/static/"):
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
                    with open(file_path, encoding="utf-8", errors="replace") as f:
                        result["text_content"] = f.read()
                return result
        except Exception:
            pass

        return None

    # ---- 后处理 ----

    def on_response_complete(self, ctx: PipelineContext, result: PipelineResult) -> None:
        # 自动重命名会话（首次命名 + 每5轮更新）
        self._try_auto_name_session()
        self._update_session_system_prompt(ctx)
        # 主动聊天回复完成后，清除 pending 状态，允许下次定时触发
        if ctx.metadata.get("is_proactive_chat"):
            session = self.session_store.get_session(self.session_id)
            if session and session.get("proactive_chat_pending_since"):
                session.pop("proactive_chat_pending_since", None)
                self.session_store.set_session(self.session_id, session)
        # 文件变更卡片
        if ctx.round_file_changes:
            _emit_change_card(
                self.server,
                self.session_store,
                session_id=self.session_id,
                parent_message_id=self.parent_message_id,
                file_changes=ctx.round_file_changes,
            )


# ---- Web 频道的 model_call 辅助函数 ----

def _call_web_ai(server, messages: list[dict], tools: list, stop_event=None) -> dict:
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
    append_base_url_path = runtime_ai.get("append_base_url_path", True)
    proxy_url = runtime_ai.get("proxy_url", "")

    from nbot.core.protocols import get_protocol
    protocol = get_protocol(provider_type)
    url = protocol.resolve_url(
        base_url,
        model=model,
        append_base_url_path=append_base_url_path,
        api_key=api_key,
    )
    headers = protocol.build_headers(api_key)
    payload = protocol.build_payload(
        model, messages,
        tools=tools if tools else None,
        tool_choice="auto" if tools else None,
        stream=False,
        base_url=base_url,
        provider_type=provider_type,
        extra_body=request_extra_body(runtime_ai) or None,
    )
    resp = requests.post(
        url,
        json=payload,
        headers=headers,
        timeout=120,
        **model_proxy_request_kwargs(proxy_url),
    )
    resp.raise_for_status()
    normalized = protocol.parse_response(
        response_json_utf8(resp),
        model=model,
        base_url=base_url,
        provider_type=provider_type,
    )
    return normalized.to_dict()


def _stream_to_web(
    server,
    messages: list[dict],
    tools: list,
    session_id: str,
    stop_event=None,
    model_configs: list = None,
):
    """Web 频道的 provider 级流式实现，含 chunk 去重和故障转移。

    当提供 model_configs 时，在连接阶段（首个 chunk 到达前）尝试
    按优先级依次使用不同模型。首个 chunk 到达后不再切换模型。
    """
    import requests

    from nbot.core.failover import (
        _extract_status_code,
        classify_http_error,
        get_failover_state,
    )
    from nbot.core.model_adapter import normalize_usage_dict
    from nbot.services.ai import apply_model_config, refresh_runtime_ai_config

    stream_started_at = time.perf_counter()
    runtime_ai = refresh_runtime_ai_config()
    base_url = runtime_ai.get("base_url") or ""
    model = runtime_ai.get("model") or ""
    provider_type = runtime_ai.get("provider_type") or "openai_compatible"
    api_key = runtime_ai.get("api_key") or ""
    append_base_url_path = runtime_ai.get("append_base_url_path", True)
    proxy_url = runtime_ai.get("proxy_url", "")

    from nbot.core.protocols import get_protocol
    protocol = get_protocol(provider_type)
    url = protocol.resolve_url(
        base_url,
        model=model,
        append_base_url_path=append_base_url_path,
        stream=True,
        api_key=api_key,
    )
    headers = protocol.build_headers(api_key, stream=True)

    def _build_stream_payload(include_usage: bool = True):
        extra_body = dict(request_extra_body(runtime_ai))
        if include_usage:
            extra_body["stream_options"] = {"include_usage": True}
        return protocol.build_payload(
            model, messages,
            tools=tools if tools else None,
            tool_choice="auto" if tools else None,
            stream=True,
            base_url=base_url,
            provider_type=provider_type,
            extra_body=extra_body or None,
        )

    # ---- 故障转移：连接阶段（首个 chunk 前） ----
    failover = get_failover_state()
    configs_to_try = model_configs if model_configs and len(model_configs) > 1 else None
    resp = None
    last_error = None
    used_model_id = model  # 默认为初始模型

    if configs_to_try:
        attempted = set()
        for _ in range(len(configs_to_try)):
            cfg = failover.select_model(configs_to_try, exclude_ids=attempted)
            if cfg is None:
                break
            mid = cfg.get("model_id", "")
            attempted.add(mid)

            # Apply this config
            apply_model_config(cfg)
            cfg_pt = cfg.get("provider_type", "openai_compatible")
            cfg_protocol = get_protocol(cfg_pt)
            cfg_url = cfg_protocol.resolve_url(
                cfg.get("base_url", ""),
                model=cfg.get("model", ""),
                append_base_url_path=cfg.get("append_base_url_path", True),
                stream=True,
                api_key=cfg.get("api_key", ""),
            )
            cfg_headers = cfg_protocol.build_headers(cfg.get("api_key", ""), stream=True)
            cfg_extra_body = dict(request_extra_body(cfg))
            cfg_extra_body["stream_options"] = {"include_usage": True}
            cfg_payload = cfg_protocol.build_payload(
                cfg.get("model", ""),
                messages,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                stream=True,
                base_url=cfg.get("base_url", ""),
                provider_type=cfg_pt,
                extra_body=cfg_extra_body,
            )
            try:
                _log.info(
                    "[StreamTiming] session=%s failover trying model=%s",
                    session_id, cfg.get("model", ""),
                )
                resp = requests.post(
                    cfg_url, json=cfg_payload, headers=cfg_headers,
                    stream=True, timeout=120,
                    **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
                )
                resp.raise_for_status()
                failover.record_success(mid)
                # Update local vars for the rest of the function
                model = cfg.get("model", "")
                used_model_id = mid
                base_url = cfg.get("base_url", "")
                provider_type = cfg.get("provider_type", "openai_compatible")
                protocol = cfg_protocol
                break
            except StopIteration:
                raise
            except Exception as e:
                status = _extract_status_code(e)
                category = classify_http_error(status)
                if category == "config":
                    raise
                failover.record_failure(mid, status)
                last_error = e
                _log.warning(
                    "[StreamTiming] session=%s model=%s failed (%s %d)",
                    session_id, cfg.get("model", ""), category, status,
                )
                resp = None
                continue

        if resp is None:
            raise last_error or RuntimeError("All streaming models failed")
    else:
        # Single model: original path
        payload = _build_stream_payload(include_usage=True)
        _log.info(
            "[StreamTiming] session=%s posting provider request model=%s messages=%s chars=%s tools=%s",
            session_id,
            model,
            len(messages),
            sum(len(str(msg.get("content", ""))) for msg in messages if isinstance(msg, dict)),
            bool(tools),
        )
        resp = requests.post(
            url,
            json=payload,
            headers=headers,
            stream=True,
            timeout=120,
            **model_proxy_request_kwargs(proxy_url),
        )
        try:
            resp.raise_for_status()
        except requests.HTTPError:
            if resp.status_code in (400, 422) and payload.get("stream_options"):
                payload = _build_stream_payload(include_usage=False)
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    stream=True,
                    timeout=120,
                    **model_proxy_request_kwargs(proxy_url),
                )
                resp.raise_for_status()
            else:
                raise

    _log.info(
        "[StreamTiming] session=%s provider headers after %.3fs",
        session_id,
        time.perf_counter() - stream_started_at,
    )

    # chunk 去重：部分提供商返回累积文本而非增量
    content_parts: list[str] = []

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

    first_chunk_logged = False
    for raw_line in resp.iter_lines(chunk_size=1, decode_unicode=False):
        if stop_event and stop_event.is_set():
            break
        line = raw_line.decode("utf-8", errors="replace") if isinstance(raw_line, bytes) else raw_line
        if not line:
            continue
        # 兼容 SSE（data: 前缀）与裸 JSON 两种格式
        if line.startswith("data: "):
            data_str = line[6:]
        elif line.startswith("data:"):
            data_str = line[5:]
        else:
            # 非 SSE 格式，跳过（如 Gemini 默认 JSON 数组流的括号行）
            continue
        data_str = data_str.strip()
        if not data_str:
            continue
        if data_str == "[DONE]":
            break
        try:
            data = json.loads(data_str)
        except json.JSONDecodeError:
            continue
        try:
            parsed = protocol.parse_stream_chunk(data)
            if parsed:
                ptype = parsed.get("type", "")
                if ptype == "content":
                    raw = repair_mojibake_text(parsed.get("content", ""))
                    if raw:
                        chunk = normalize_chunk(raw)
                        if chunk:
                            if not first_chunk_logged:
                                first_chunk_logged = True
                                _log.info(
                                    "[StreamTiming] session=%s first provider chunk after %.3fs",
                                    session_id,
                                    time.perf_counter() - stream_started_at,
                                )
                            content_parts.append(chunk)
                            yield {"content": chunk}
                elif ptype == "usage":
                    yield {"usage": parsed.get("usage", {})}
                elif ptype == "stop":
                    pass
            else:
                # 兼容：直接从 data 中提取 usage（OpenAI stream_options）
                usage = normalize_usage_dict(data.get("usage"))
                if usage:
                    yield {"usage": usage}
        except Exception as chunk_exc:
            # 单个 chunk 解析异常不应中断整个流
            _log.debug(
                "[StreamParse] session=%s chunk parse failed: %s | data=%s",
                session_id, chunk_exc, str(data)[:200],
            )
            continue
    # 流结束后注入模型信息
    yield {"_model_id": used_model_id, "_model_name": model}


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
    # Agent 模式强制开启进度卡片(即便当前渠道未声明 supports_progress_updates,
    # 也由前端 progress card 组件独立渲染);其他渠道需要渠道能力支持。
    _is_agent_session_for_progress = (session or {}).get("session_mode") == "agent"
    progress_reporter = None
    if (
        (channel_capabilities.supports_progress_updates or _is_agent_session_for_progress)
        and server.PROGRESS_CARD_AVAILABLE
        and server.progress_card_manager
        and server.socketio
    ):
        request_metadata = dict(chat_request.metadata or {})
        progress_card = None
        todo_card = None
        progress_card_id = str(request_metadata.get("resume_progress_card_id") or "")
        todo_card_id = str(request_metadata.get("resume_todo_card_id") or "")
        if progress_card_id:
            progress_card = server.progress_card_manager.get_card(progress_card_id)
            if progress_card and progress_card.session_id != session_id:
                progress_card = None
        if todo_card_id and server.TODO_CARD_AVAILABLE and server.todo_card_manager:
            todo_card = server.todo_card_manager.get_card(todo_card_id)
            if todo_card and todo_card.session_id != session_id:
                todo_card = None
        progress_reporter = WebProgressReporter(
            server, session_id, parent_message_id, session_store,
            is_agent=_is_agent_session_for_progress,
            progress_card=progress_card,
            todo_card=todo_card,
        )

    # 确定是否启用工具
    tools = None
    # Agent 模式始终启用工具；角色模式按消息内容判断
    _session_data = session_store.get_session(session_id) if session_store else {}
    _is_agent_session = (_session_data or {}).get("session_mode") == "agent"
    has_tools = (
        _is_agent_session
        or (
            _looks_like_tool_request(user_content)
            and getattr(server, "ai_config", {}).get("supports_tools", True)
        )
    )
    if has_tools:
        try:
            from nbot.services.tools import get_enabled_tools
            tools = get_enabled_tools()
            # 会话级工具集过滤（会话设置 enabled_tools 列表时仅保留其中工具）
            session_enabled = (_session_data or {}).get("enabled_tools")
            if isinstance(session_enabled, list) and session_enabled:
                allowed = {str(n) for n in session_enabled}
                tools = [
                    t for t in tools
                    if str((t.get("function") or {}).get("name") or "") in allowed
                ]
                if not tools:
                    tools = None
        except Exception:
            tools = None

    # 构建管道上下文和回调
    ctx = PipelineContext(
        chat_request=chat_request,
        adapter=adapter,
        stop_event=stop_event,
        metadata=dict(chat_request.metadata or {}),
    )
    ctx.metadata.setdefault("channel_type", "web")
    ctx.metadata.setdefault("source", "web")
    # 注入会话级提示词栈禁用列表
    if _session_data:
        # 同步 session_mode 到 ctx.metadata，让 ai_pipeline 能识别 agent 模式
        # 跳过角色记忆注入（character.memories_legacy / MemoryFS 等）
        if _session_data.get("session_mode"):
            ctx.metadata.setdefault("session_mode", _session_data["session_mode"])
        # agent 继承角色能力（P2-8）：会话级开关，默认关闭
        if _session_data.get("agent_inherit_character") is not None:
            ctx.metadata.setdefault(
                "agent_inherit_character", _session_data.get("agent_inherit_character")
            )
        if _session_data.get("plot_mode"):
            ctx.metadata.setdefault("plot_mode", True)
            if _session_data.get("plot_real_time_sync"):
                ctx.metadata.setdefault("plot_real_time_sync", True)
            if _session_data.get("plot_choice_style"):
                ctx.metadata.setdefault(
                    "plot_choice_style", _session_data.get("plot_choice_style")
                )
        ctx.metadata.setdefault("disabled_prompt_keys", _session_data.get("disabled_prompt_keys", []))
        # 注入用户自定义提示词
        ctx.metadata.setdefault("custom_prompts", _session_data.get("custom_prompts", []))
        # 注入会话级 auto_state 触发轮次（None 表示用全局默认）
        if _session_data.get("auto_state_interval") is not None:
            ctx.metadata.setdefault("auto_state_interval", _session_data["auto_state_interval"])
        # Agent 持久目标 / 规格（/goal、/spec 命令设置）
        if _session_data.get("agent_goal"):
            ctx.metadata.setdefault("agent_goal", _session_data["agent_goal"])
        if _session_data.get("agent_spec"):
            ctx.metadata.setdefault("agent_spec", _session_data["agent_spec"])
        # Author's Note（会话级附注，按深度插入消息序列）
        note = _session_data.get("authors_note")
        if note:
            ctx.metadata.setdefault("authors_note", note)
        # 用户人设（persona）：注入 system prompt，并作为 {{user}} 的默认名称
        from nbot.core.personas import session_persona

        persona = session_persona(_session_data)
        if persona:
            ctx.metadata.setdefault("user_persona", persona)
            if persona.get("name"):
                ctx.metadata["macro_user_name"] = str(persona["name"])
        # 宏上下文：用户名/角色名（{{user}} / {{char}}）
        ctx.metadata.setdefault(
            "macro_user_name", str(_session_data.get("user_id") or "")
        )
        ctx.metadata.setdefault(
            "macro_char_name",
            str(_session_data.get("sender_name") or _session_data.get("character_id") or ""),
        )
    # 注入价格信息供公共 token 统计使用
    try:
        active_model_id = getattr(server, "active_model_id", None)
        if active_model_id:
            for m in getattr(server, "ai_models", []) or []:
                if m.get("id") == active_model_id:
                    ctx.metadata.setdefault("input_price", m.get("input_price"))
                    ctx.metadata.setdefault("output_price", m.get("output_price"))
                    break
    except Exception:
        pass
    base_metadata = dict(ctx.metadata)
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

            # --- Hook runtime（Web 对话路径） ---
            hook_runtime = None
            try:
                from nbot.hooks.manager import get_hook_manager
                hook_runtime = get_hook_manager(data_dir=getattr(server, "data_dir", "data/web"))
                # 注入 event_bus 到 ReviewPipeline 和 PlotGraphManager，使标准化事件进入事件流
                if hook_runtime:
                    _ebus = getattr(hook_runtime, "_event_bus", None)
                    if _ebus:
                        from nbot.plot.graph_manager import get_plot_graph_manager
                        from nbot.review.pipeline import get_review_pipeline
                        get_review_pipeline(event_bus=_ebus)
                        get_plot_graph_manager(event_bus=_ebus)
                # Register Socket.IO notifier for hook trigger events
                if hook_runtime and hasattr(server, 'socketio') and not hook_runtime._event_notifier:
                    _sio = server.socketio
                    def _hook_notifier(info, _sio=_sio):
                        cid = info.get("conversation_id", "")
                        if cid:
                            _sio.emit("hook_notification", info, room=cid)
                    hook_runtime.set_event_notifier(_hook_notifier)

                # 注入 workflow trigger 回调，使 hook 的 workflow action 能路由到
                # web server 的真实工作流执行器（_execute_workflow），替代已删除的
                # core/workflow.py WorkflowEngine
                if hook_runtime and not hook_runtime._workflow_trigger:
                    def _workflow_trigger(workflow_name, context, _server=server):
                        # 查找 workflow_id（按 name 匹配）
                        wf_id = None
                        for wf in getattr(_server, "workflows", []) or []:
                            if wf.get("id") == workflow_name or wf.get("name") == workflow_name:
                                wf_id = wf.get("id")
                                break
                        if not wf_id:
                            return {"status": "error", "error": f"workflow '{workflow_name}' not found"}
                        trigger_data = {
                            "source": "hook",
                            "content": (context or {}).get("event", {}).get("event_type", ""),
                        }
                        _server._execute_workflow(wf_id, trigger_data)
                        return {"status": "triggered", "workflow_id": wf_id}
                    hook_runtime.set_workflow_trigger(_workflow_trigger)
            except Exception:
                pass

            # === 群聊模式：构建 group_context ===
            group_context = None
            _group_id = (_session_data or {}).get("group_id")
            if _group_id:
                try:
                    from nbot.group.manager import get_group_manager
                    gm = get_group_manager()
                    group = gm.get_group(_group_id)
                    if group and group.character_ids:
                        # 加载角色档案 - 同时从 ProfileRepository 和 custom_personality_presets 查找
                        profiles = {}
                        valid_character_ids = []
                        from nbot.character.repository import ProfileRepository
                        _base_dir = os.path.dirname(os.path.dirname(os.path.dirname(__file__)))
                        repo = ProfileRepository(_base_dir)

                        # 预加载 custom_personality_presets 作为备选数据源
                        custom_presets_map = {}
                        try:
                            custom_presets_file = os.path.join(server.data_dir, "custom_personality_presets.json")
                            if os.path.exists(custom_presets_file):
                                with open(custom_presets_file, encoding="utf-8") as f:
                                    custom_presets = json.load(f)
                                if isinstance(custom_presets, list):
                                    for preset in custom_presets:
                                        if isinstance(preset, dict):
                                            pid = preset.get("id", "")
                                            pname = preset.get("name", "")
                                            if pid:
                                                custom_presets_map[pid] = preset
                                            if pname and pname not in custom_presets_map:
                                                custom_presets_map[pname] = preset
                        except Exception as e:
                            _log.debug("Failed to load custom_personality_presets: %s", e)

                        for cid in group.character_ids:
                            profile = repo.get(cid)
                            if profile:
                                profiles[cid] = profile.to_personality_dict()
                                valid_character_ids.append(cid)
                            elif cid in custom_presets_map:
                                # 从 custom_personality_presets 中找到，使用它
                                profiles[cid] = custom_presets_map[cid]
                                valid_character_ids.append(cid)
                                _log.info("group %s: loaded character '%s' from custom_personality_presets", _group_id, cid)
                            else:
                                _log.warning("group %s: character profile not found for '%s', skipping", _group_id, cid)
                        # 过滤掉没有档案的角色ID，避免发言角色无法加载设定
                        if valid_character_ids and set(valid_character_ids) != set(group.character_ids):
                            group.character_ids = valid_character_ids
                        from nbot.group.narrator import NarratorCharacter
                        from nbot.group.scheduler import SpeakerScheduler
                        group_context = {
                            "group": group,
                            "character_profiles": profiles,
                            "custom_presets_map": custom_presets_map,  # 供 prompt_build 使用
                            "scheduler": SpeakerScheduler.instance(),
                            "narrator": NarratorCharacter.instance(),
                            "auto_narrate": group.config.auto_narrate,
                            "recent_messages": [],
                        }
                except Exception as e:
                    _log.debug("group context build failed for web session: %s", e)

            round_robin_speakers = []
            if group_context:
                group = group_context.get("group")
                if (
                    group
                    and getattr(group.config, "speaker_strategy", "") == "round_robin"
                    and len(group.character_ids) > 1
                ):
                    scheduler = group_context.get("scheduler")
                    last_speaker = group.active_speaker
                    seen = set()
                    for _ in group.character_ids:
                        speaker = scheduler.decide_next_speaker(
                            group,
                            user_content,
                            group.character_ids,
                            last_speaker=last_speaker,
                        )
                        if not speaker or speaker in seen:
                            break
                        round_robin_speakers.append(speaker)
                        seen.add(speaker)
                        last_speaker = speaker

            def _context_for_speaker(speaker_id: str = ""):
                run_ctx = PipelineContext(
                    chat_request=chat_request,
                    adapter=adapter,
                    stop_event=stop_event,
                    metadata=dict(base_metadata),
                )
                if speaker_id and group_context:
                    profiles = group_context.get("character_profiles", {})
                    group = group_context.get("group")
                    run_ctx.metadata["group_speaker"] = speaker_id
                    run_ctx.metadata["group_speaker_name"] = (
                        profiles.get(speaker_id, {}).get("name", speaker_id)
                    )
                    if group:
                        run_ctx.metadata["group_id"] = group.group_id
                return run_ctx

            if len(round_robin_speakers) > 1:
                _speaker_results = []
                _rr_mode = getattr(group_context.get("group").config, "round_robin_mode", "async") if group_context and group_context.get("group") else "async"

                if _rr_mode == "sequential":
                    # === 顺序回复：逐个执行，每个角色能看到前面角色的回复 ===
                    _log.info("group round_robin: sequential mode, %d speakers", len(round_robin_speakers))
                    for speaker in round_robin_speakers:
                        try:
                            sp_ctx = _context_for_speaker(speaker)
                            import time as _time
                            _sp_start = _time.perf_counter()
                            sp_result = pipeline.process(
                                sp_ctx, callbacks,
                                tools=tools,
                                max_context_chars=context_char_budget,
                                hook_runtime=hook_runtime,
                                group_context=group_context,
                            )
                            _sp_duration_ms = (_time.perf_counter() - _sp_start) * 1000.0
                            _speaker_results.append({
                                "speaker_id": speaker,
                                "speaker_name": sp_ctx.metadata.get("group_speaker_name", speaker),
                                "content": sp_result.final_content or "",
                                "assistant_message": sp_result.assistant_message,
                            })
                            if not sp_result.metadata:
                                sp_result.metadata = {}
                            sp_result.metadata.setdefault("duration_ms", _sp_duration_ms)
                            sp_result.metadata.setdefault("ttft_ms", _sp_duration_ms)
                            _update_web_token_stats(server, sp_result.usage, session_id, sp_result.metadata)
                        except Exception as sp_err:
                            _log.error("group sequential speaker %s failed: %s", speaker, sp_err)
                else:
                    # === 异步回复：并行发送所有角色的请求 ===
                    import threading as _threading
                    _speaker_errors = []

                    def _run_speaker(speaker_id):
                        try:
                            sp_ctx = _context_for_speaker(speaker_id)
                            import time as _time
                            _sp_start = _time.perf_counter()
                            sp_result = pipeline.process(
                                sp_ctx, callbacks,
                                tools=tools,
                                max_context_chars=context_char_budget,
                                hook_runtime=hook_runtime,
                                group_context=group_context,
                            )
                            _sp_duration_ms = (_time.perf_counter() - _sp_start) * 1000.0
                            _speaker_results.append({
                                "speaker_id": speaker_id,
                                "speaker_name": sp_ctx.metadata.get("group_speaker_name", speaker_id),
                                "content": sp_result.final_content or "",
                                "assistant_message": sp_result.assistant_message,
                            })
                            if not sp_result.metadata:
                                sp_result.metadata = {}
                            sp_result.metadata.setdefault("duration_ms", _sp_duration_ms)
                            sp_result.metadata.setdefault("ttft_ms", _sp_duration_ms)
                            _update_web_token_stats(server, sp_result.usage, session_id, sp_result.metadata)
                        except Exception as sp_err:
                            _speaker_errors.append((speaker_id, sp_err))
                            _log.error("group parallel speaker %s failed: %s", speaker_id, sp_err)

                    threads = []
                    for speaker in round_robin_speakers:
                        t = _threading.Thread(target=_run_speaker, args=(speaker,), daemon=True)
                        threads.append(t)
                        t.start()
                    for t in threads:
                        t.join()

                # 使用最后一个 speaker 的 metadata 更新 ctx，并标记轮次完成
                ctx.metadata.update(_context_for_speaker(round_robin_speakers[-1]).metadata)
                ctx.metadata["group_round_complete"] = True
                # 同时设置到 chat_request.metadata 中，供 auto_state 使用
                if not chat_request.metadata:
                    chat_request.metadata = {}
                chat_request.metadata["group_round_complete"] = True

                # === @mention 跨角色对话（轮次结束后）===
                if group_context and _speaker_results:
                    _group = group_context.get("group")
                    if _group and getattr(_group.config, "allow_character_cross_talk", True):
                        try:
                            from nbot.group.cross_talk import (
                                collect_mentions_from_round,
                                process_cross_talk,
                            )
                            _round_msgs = [
                                {"role": "assistant", "content": r["content"], "sender": r["speaker_name"]}
                                for r in _speaker_results
                            ]
                            _mentions = collect_mentions_from_round(
                                _round_msgs,
                                _group.character_ids,
                                group_context.get("character_profiles", {}),
                            )
                            if _mentions:
                                _max_mentions = getattr(_group.config, "cross_talk_max_mentions", 5)
                                _base_ctx = _context_for_speaker(round_robin_speakers[0])
                                process_cross_talk(
                                    _mentions, _max_mentions,
                                    pipeline=pipeline,
                                    callbacks=callbacks,
                                    group_context=group_context,
                                    base_metadata=dict(_base_ctx.metadata),
                                    chat_request=chat_request,
                                    adapter=adapter,
                                    stop_event=stop_event,
                                    tools=tools,
                                    max_context_chars=context_char_budget,
                                    hook_runtime=hook_runtime,
                                )
                        except Exception as ct_err:
                            _log.error("cross-talk after round failed: %s", ct_err)

                # 轮次已完成，跳过后续单独处理
                result = None
            else:
                # 单角色群聊或非群聊模式：单次回复即为一轮
                if group_context:
                    ctx.metadata["group_round_complete"] = True
                    # 同时设置到 chat_request.metadata 中，供 auto_state 使用
                    if not chat_request.metadata:
                        chat_request.metadata = {}
                    chat_request.metadata["group_round_complete"] = True
                import time as _time
                _pipe_start = _time.perf_counter()
                result = pipeline.process(
                    ctx, callbacks,
                    tools=tools,
                    max_context_chars=context_char_budget,
                    hook_runtime=hook_runtime,
                    group_context=group_context,
                )
                _pipe_duration_ms = (_time.perf_counter() - _pipe_start) * 1000.0

            # 更新 token 统计（并行发送模式下 result 为 None）
            if result:
                # 注入时间指标到 metadata，供 _update_web_token_stats 提取
                if not result.metadata:
                    result.metadata = {}
                result.metadata.setdefault("duration_ms", _pipe_duration_ms)
                # 非流式调用：TTFT ≈ 完整时间
                result.metadata.setdefault("ttft_ms", _pipe_duration_ms)
                _update_web_token_stats(server, result.usage, session_id, result.metadata)

            # === AI 完成后，记录 Gateway 事件（Web 异步场景）===
            reply_text = ""
            reply_metadata = {}
            if result and hasattr(result, 'final_content') and result.final_content:
                reply_text = str(result.final_content)
                reply_metadata = getattr(result, 'metadata', None) or {}
            elif group_context and _speaker_results:
                reply_parts = []
                speaker_names = []
                for item in _speaker_results:
                    content = str(item.get("content") or "").strip()
                    if not content:
                        continue
                    speaker_name = str(item.get("speaker_name") or "").strip()
                    if speaker_name:
                        speaker_names.append(speaker_name)
                        reply_parts.append(f"[{speaker_name}] {content}")
                    else:
                        reply_parts.append(content)
                reply_text = "\n".join(reply_parts)
                reply_metadata = {
                    "group_reply_count": len(reply_parts),
                    "group_speakers": speaker_names,
                }

            if reply_text:
                try:
                    from nbot.gateway.gateway import get_gateway as _get_gw
                    gw = _get_gw()
                    if gw and gw.event_store:
                        reply_preview = reply_text[:200]
                        trace_id = (ctx.chat_request.metadata or {}).get('_gateway_trace_id', '') or ''
                        # 获取会话名称
                        session_name = ""
                        try:
                            sess = server.sessions.get(session_id, {})
                            session_name = sess.get("name", "") if sess else ""
                        except Exception:
                            pass
                        # 记录模型信息事件
                        result_metadata = reply_metadata
                        used_model_id = result_metadata.get("model_id", "")
                        used_model_name = result_metadata.get("model_name", "")
                        # 先记录故障转移事件（发生在模型选择之前）
                        failover_events = result_metadata.get("failover_events", [])
                        for ev in failover_events:
                            gw.record_lifecycle_event(
                                trace_id=trace_id,
                                channel_id="web",
                                status="model_failover",
                                conversation_id=session_id,
                                metadata={
                                    k: v for k, v in {
                                        "failed_model_id": ev.get("model_id", ""),
                                        "failed_model_name": ev.get("model_name", ""),
                                        "status_code": ev.get("status_code", 0),
                                        "category": ev.get("category", ""),
                                        "session_name": session_name,
                                    }.items() if v
                                },
                            )
                        # 再记录最终使用的模型
                        if used_model_id or used_model_name:
                            gw.record_lifecycle_event(
                                trace_id=trace_id,
                                channel_id="web",
                                status="model_selected",
                                conversation_id=session_id,
                                metadata={
                                    k: v for k, v in {
                                        "model_id": used_model_id,
                                        "model_name": used_model_name,
                                        "session_name": session_name,
                                    }.items() if v
                                },
                            )
                        # 直接 record 完整的 delivered 事件（非 update）
                        gw.record_lifecycle_event(
                            trace_id=trace_id,
                            channel_id="web",
                            status="delivered",
                            conversation_id=session_id,
                            raw_event={"reply_preview": reply_preview} if reply_preview else None,
                            metadata={
                                k: v for k, v in {
                                    "reply_length": len(reply_text),
                                    "session_name": session_name,
                                    "group_reply_count": result_metadata.get("group_reply_count"),
                                    "group_speakers": result_metadata.get("group_speakers"),
                                }.items() if v not in (None, "", [])
                            },
                        )
                except Exception as exc:
                    _log.debug("[Gateway] Web 异步事件记录失败: %s", str(exc))

            # 发送剧情选项到前端（附带故事图数据）
            if result and getattr(result, 'metadata', None):
                plot_choices = result.metadata.get("plot_choices")
                if plot_choices:
                    mermaid = ""
                    graph = None
                    try:
                        from nbot.plot.graph_manager import get_plot_graph_manager
                        plot_mgr = get_plot_graph_manager(
                            data_dir=getattr(server, "data_dir", "data/web"),
                        )
                        mermaid = plot_mgr.generate_mermaid(session_id)
                        graph = plot_mgr.get_graph(session_id)
                    except Exception:
                        pass

                    server.socketio.emit(
                        "plot_choices",
                        {
                            "session_id": session_id,
                            "choices": plot_choices,
                            "mermaid": mermaid,
                            "graph": graph,
                        },
                        room=session_id,
                    )
                    server.socketio.sleep(0)
                elif result.metadata.get("plot_mode"):
                    _log.info(
                        "[PlotMode] no choices to emit for session=%s metadata_keys=%s",
                        session_id,
                        list(result.metadata.keys()),
                    )

            return result
        finally:
            server.stop_events.pop(session_id, None)
            if progress_reporter:
                progress_reporter.dispose()

    # 使用后台任务执行管道
    server.socketio.start_background_task(run_pipeline)
    return ChatResponse(metadata={"scheduled": True, "session_id": session_id})


def _update_web_token_stats(server, usage: dict, session_id: str, metadata: dict = None):
    """更新 Web 频道的 token 统计（统一持久化到磁盘）。"""
    try:
        if not usage:
            return
        total = usage.get("total_tokens", 0)
        if not total:
            return
        if (metadata or {}).get("token_recorded"):
            return

        from nbot.core.token_stats import get_token_stats_manager

        # 优先使用 API 实际返回的模型名，回退到配置的模型
        actual_model = (metadata or {}).get("model_name", "") or (metadata or {}).get("model_id", "") or getattr(server, "ai_model", "") or ""

        # 从当前活跃模型配置中查找价格
        input_price = None
        output_price = None
        cached_input_price = None
        try:
            active_model_id = getattr(server, "active_model_id", None)
            if active_model_id:
                for m in getattr(server, "ai_models", []) or []:
                    if m.get("id") == active_model_id:
                        input_price = m.get("input_price")
                        output_price = m.get("output_price")
                        cached_input_price = m.get("cached_input_price")
                        break
        except Exception:
            pass

        # 缓存命中 token（各协议在 model_adapter 里已归一化）
        try:
            cached_tokens = int(usage.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            cached_tokens = 0

        # 从 metadata 提取时间指标（由调用方测量并传入）
        duration_ms = (metadata or {}).get("duration_ms")
        ttft_ms = (metadata or {}).get("ttft_ms")

        get_token_stats_manager().record_usage(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            total_tokens=usage.get("total_tokens", 0),
            model=actual_model,
            session_id=session_id,
            channel_type="web",
            user_id=session_id,
            source="web",
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            input_price=input_price,
            output_price=output_price,
            cached_tokens=cached_tokens,
            cached_input_price=cached_input_price,
        )
    except Exception:
        pass


def get_ai_response(self, messages: list[dict]) -> str:
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


def stream_ai_response(self, messages: list[dict], session_id: str, callback):
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
    messages: list[dict],
    session_id: str,
    message: dict,
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
    server, session_id: str, message: dict, thinking_content: str = None
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
            server.socketio.sleep(0)

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
    server, messages: list[dict], image_urls: list[str], user_question: str = None
) -> str:
    """获取带图片的 AI 回复（多模态）"""
    try:
        # 获取图片理解模型配置（新架构）
        vision_config = get_vision_model_config()
        api_key = None
        base_url = ""
        model = "zai-org/GLM-4.6V"
        provider_type = "openai_compatible"
        append_base_url_path = True
        proxy_url = ""
        system_prompt = "请详细描述这张图片的内容。"

        if vision_config and vision_config.get("api_key"):
            # 使用新架构的配置
            api_key = vision_config.get("api_key")
            base_url = vision_config.get("base_url", "")
            model = vision_config.get("model", "zai-org/GLM-4.6V")
            provider_type = vision_config.get("provider_type", "openai_compatible")
            append_base_url_path = vision_config.get("append_base_url_path", True)
            proxy_url = vision_config.get("proxy_url", "")
            system_prompt = vision_config.get("system_prompt", "请详细描述这张图片的内容。")
        else:
            # 回退到旧的配置方式
            if not server.ai_client:
                return "AI 服务未配置，请在 AI 配置页面设置 API Key 和 Base URL。"

            api_key = getattr(server.ai_client, "api_key", None)
            base_url = getattr(server.ai_client, "base_url", None)
            model = getattr(server.ai_client, "pic_model", None) or "zai-org/GLM-4.6V"
            provider_type = getattr(server.ai_client, "provider_type", "openai_compatible")
            append_base_url_path = getattr(server.ai_client, "append_base_url_path", True)
            proxy_url = getattr(server.ai_client, "proxy_url", "")
            system_prompt = "请详细描述这张图片的内容。"

            # 尝试从config.ini获取api_key
            if not api_key:
                try:
                    import configparser
                    config = configparser.ConfigParser()
                    config.read("config.ini", encoding="utf-8")
                    api_key = config.get("ApiKey", "api_key", fallback="")
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
        from nbot.core.protocols import get_protocol as _gp
        _img_protocol = _gp(provider_type)
        url = _img_protocol.resolve_url(
            base_url,
            model=model,
            append_base_url_path=append_base_url_path,
            api_key=api_key,
        )

        headers = _img_protocol.build_headers(api_key)
        payload = _img_protocol.build_payload(
            model, multimodal_messages,
            stream=False,
            base_url=base_url,
            provider_type=provider_type,
        )

        response = requests.post(
            url,
            json=payload,
            headers=headers,
            timeout=120,
            **model_proxy_request_kwargs(proxy_url),
        )
        response.raise_for_status()
        data = response_json_utf8(response)
        normalized = _img_protocol.parse_response(
            data, model=model, base_url=base_url, provider_type=provider_type,
        )
        content = normalized.content or ""
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
    messages: list[dict],
    tools: list[dict],
    stop_event=None,
) -> dict:
    """调用 AI 并支持工具

    Args:
        messages: 消息列表
        tools: 工具定义列表
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

        # 故障转移：获取同用途模型列表
        from nbot.core.failover import (
            _extract_status_code,
            classify_http_error,
            get_failover_state,
        )
        from nbot.services.ai import apply_model_config
        from nbot.web.utils.config_loader import get_model_configs_by_purpose

        failover = get_failover_state()
        model_configs = get_model_configs_by_purpose("chat")

        # 使用主 API（支持故障转移）
        from nbot.core.protocols import get_protocol as _get_proto

        # 预处理消息（截断过长内容）—— 与模型无关，只做一次
        MAX_CONTENT_LENGTH = 12000
        MAX_ARGUMENTS_LENGTH = 50000
        processed_messages = []
        for msg in messages:
            msg_copy = msg.copy()
            if "content" in msg_copy and isinstance(msg_copy["content"], str):
                content_len = len(msg_copy["content"])
                if content_len > MAX_CONTENT_LENGTH:
                    truncated = msg_copy["content"][:MAX_CONTENT_LENGTH]
                    if truncated.strip().startswith(
                        "{"
                    ) or truncated.strip().startswith("["):
                        last_brace = max(
                            truncated.rfind("}"), truncated.rfind("]")
                        )
                        if last_brace > MAX_CONTENT_LENGTH - 500:
                            truncated = truncated[: last_brace + 1]
                    msg_copy["content"] = (
                        truncated
                        + f"\n... [内容过长，已截断，原始长度: {content_len} 字符]"
                    )

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

        def _attempt_api_call(config, proc_msgs, api_tools, timeout_sec, retries, stop_evt):
            """单模型 API 调用（含重试），返回 (data, protocol)。"""
            _srv_pt = config.get("provider_type", "openai_compatible")
            _srv_protocol = _get_proto(_srv_pt)
            _url = _srv_protocol.resolve_url(
                config.get("base_url", ""),
                model=config.get("model", ""),
                append_base_url_path=config.get("append_base_url_path", True),
                api_key=config.get("api_key", ""),
            )
            _headers = _srv_protocol.build_headers(config.get("api_key", ""))
            _payload = _srv_protocol.build_payload(
                config.get("model", ""),
                proc_msgs,
                tools=api_tools,
                tool_choice="auto",
                stream=False,
                base_url=config.get("base_url", ""),
                provider_type=_srv_pt,
                extra_body=request_extra_body(config) or None,
            )

            _payload_size = len(json.dumps(_payload, ensure_ascii=False))
            _log.info(
                "[AI] 发送请求到 %s, 模型=%s, 工具数=%d, payload大小=%d bytes",
                _url, config.get("model", ""), len(api_tools) if api_tools else 0, _payload_size,
            )
            if _payload_size > 500000:
                _log.warning("[AI] Payload 过大 (%d bytes)，可能导致 API 拒绝", _payload_size)

            _last_err = None
            for _attempt in range(retries):
                if stop_evt and stop_evt.is_set():
                    raise StopIteration("用户停止生成")
                try:
                    _log.info("[AI] 主 API 调用 (尝试 %d/%d)", _attempt + 1, retries)
                    _container = {"data": None, "error": None}

                    def _do_req():
                        try:
                            _resp = requests.post(
                                _url, json=_payload, headers=_headers, timeout=timeout_sec,
                                **model_proxy_request_kwargs(config.get("proxy_url", "")),
                            )
                            if not _resp.ok:
                                _log.error("[AI] API error %d: %s", _resp.status_code, _resp.text[:500])
                            _resp.raise_for_status()
                            _container["data"] = response_json_utf8(_resp)
                        except Exception as _e:
                            _container["error"] = _e

                    _t = threading.Thread(target=_do_req, daemon=True)
                    _t.start()
                    while _t.is_alive():
                        if stop_evt and stop_evt.is_set():
                            raise StopIteration("用户停止生成")
                        _t.join(timeout=0.5)

                    if _container["error"]:
                        raise _container["error"]
                    if _container["data"] is None:
                        raise Exception("请求未返回数据")
                    return _container["data"], _srv_protocol, _srv_pt
                except StopIteration:
                    raise
                except requests.exceptions.Timeout as _e:
                    _last_err = _e
                    _log.warning("[AI] 主 API 超时 (尝试 %d/%d): %s", _attempt + 1, retries, _e)
                    if _attempt < retries - 1:
                        time.sleep(min(2 ** _attempt, 2))
                except requests.exceptions.RequestException as _e:
                    _last_err = _e
                    _log.error("[AI] 主 API 错误 (尝试 %d/%d): %s", _attempt + 1, retries, _e)
                    if _attempt < retries - 1:
                        time.sleep(min(2 ** _attempt, 2))
            raise _last_err or Exception("API 调用失败")

        # ---- 故障转移循环 ----
        if model_configs and len(model_configs) > 1:
            attempted = set()
            failover_last_error = None
            for _ in range(len(model_configs)):
                cfg = failover.select_model(model_configs, exclude_ids=attempted)
                if cfg is None:
                    break
                mid = cfg.get("model_id", "")
                mname = cfg.get("model", "")
                attempted.add(mid)
                apply_model_config(cfg)
                try:
                    data, resp_protocol, resp_pt = _attempt_api_call(
                        cfg, processed_messages, tools, api_timeout, max_retries, stop_event,
                    )
                    failover.record_success(mid)
                    _log.info("[Failover] workflow model=%s succeeded", mname)
                    # 更新 server 状态供后续响应解析使用
                    server.ai_model = mname
                    server.ai_base_url = cfg.get("base_url", "")
                    server.ai_config["provider_type"] = resp_pt
                    break
                except StopIteration:
                    raise
                except Exception as e:
                    status = _extract_status_code(e)
                    category = classify_http_error(status)
                    if category == "config":
                        _log.warning("[Failover] workflow model=%s config error %d, not failing over", mname, status)
                        raise
                    failover.record_failure(mid, status)
                    failover_last_error = e
                    # 打印异常类型和内容，便于诊断空 choices / 解析失败等问题
                    _log.warning(
                        "[Failover] workflow model=%s failed (%s %d): %r, trying next",
                        mname, category, status, e,
                    )
                    continue
            else:
                raise failover_last_error or RuntimeError("All workflow models failed")
        else:
            # 单模型：直接调用，但仍接入健康追踪
            _single_cfg = (model_configs[0] if model_configs else {
                "model": server.ai_model,
                "base_url": server.ai_base_url,
                "api_key": server.ai_api_key,
                "provider_type": server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible")),
                "append_base_url_path": server.ai_config.get("append_base_url_path", True),
            })
            if model_configs:
                apply_model_config(_single_cfg)
            _single_mid = _single_cfg.get("model_id", "")
            try:
                data, resp_protocol, resp_pt = _attempt_api_call(
                    _single_cfg, processed_messages, tools, api_timeout, max_retries, stop_event,
                )
                if _single_mid:
                    failover.record_success(_single_mid)
            except StopIteration:
                raise
            except Exception as e:
                if _single_mid:
                    status = _extract_status_code(e)
                    failover.record_failure(_single_mid, status)
                raise

        from nbot.core.protocols import get_protocol as _gp2
        _resp_pt = server.ai_config.get("provider_type", server.ai_config.get("provider", "openai_compatible"))
        _resp_protocol = _gp2(_resp_pt)
        normalized = _resp_protocol.parse_response(
            data,
            model=server.ai_model or "",
            base_url=server.ai_base_url or "",
            provider_type=_resp_pt,
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
        # 回退到普通 AI 调用（遍历故障转移队列，不依赖全局配置）
        # 注意：必须传 tools，否则模型（尤其 Gemini）会把 tool_call 写成 JSON 文本
        # 而不是走结构化 tool_calling 协议，导致 run_tool_call_loop 把它当 final_content
        if model_configs:
            for cfg in model_configs:
                try:
                    cfg_pt = cfg.get("provider_type", "openai_compatible")
                    cfg_protocol = _get_proto(cfg_pt)
                    cfg_url = cfg_protocol.resolve_url(
                        cfg.get("base_url", ""),
                        model=cfg.get("model", ""),
                        append_base_url_path=cfg.get("append_base_url_path", True),
                        api_key=cfg.get("api_key", ""),
                    )
                    cfg_headers = cfg_protocol.build_headers(cfg.get("api_key", ""))
                    cfg_payload = cfg_protocol.build_payload(
                        cfg.get("model", ""),
                        messages,
                        tools=tools if tools else None,
                        tool_choice="auto" if tools else None,
                        stream=False,
                        base_url=cfg.get("base_url", ""),
                        provider_type=cfg_pt,
                        extra_body=request_extra_body(cfg) or None,
                    )
                    resp = requests.post(
                        cfg_url, json=cfg_payload, headers=cfg_headers,
                        timeout=cfg.get("failover_timeout", 0) or 120,
                        **model_proxy_request_kwargs(cfg.get("proxy_url", "")),
                    )
                    resp.raise_for_status()
                    data = response_json_utf8(resp)
                    normalized = cfg_protocol.parse_response(
                        data,
                        model=cfg.get("model", ""),
                        base_url=cfg.get("base_url", ""),
                        provider_type=cfg_pt,
                        fallback_tool_parser=server._parse_tool_call_from_text,
                    )
                    return normalized.to_dict()
                except Exception as fallback_err:
                    _log.warning(
                        "[Failover] workflow fallback model=%s failed: %s",
                        cfg.get("model", ""),
                        fallback_err,
                    )
                    continue
        # 所有模型都失败
        return {"content": f"AI 服务暂时不可用: {e}"}


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

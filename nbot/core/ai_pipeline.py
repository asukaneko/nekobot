"""
统一 AI 处理管道中间件

所有频道的 AI 请求经过此管道处理，提供：
- 知识库检索（RAG）
- 工具调用循环
- 工作区管理
- 附件解析
- 流式输出
- 进度报告

每个频道只需实现 PipelineCallbacks 的子类，提供频道特定的 I/O 操作。
"""

import copy
import json
import logging
import threading
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from nbot.core.chat_models import ChatRequest, ChatResponse

_log = logging.getLogger(__name__)

# ============================================================================
# 数据类
# ============================================================================


@dataclass
class PipelineContext:
    """贯穿 AI 管道的上下文，承载输入 → 中间状态 → 输出。"""

    # === 输入（由调用方设置） ===
    chat_request: ChatRequest
    adapter: Any = None  # BaseChannelAdapter

    # === 会话 / 消息准备 ===
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_history: Optional[List[Dict[str, Any]]] = None

    # === 知识库 ===
    knowledge_text: str = ""
    knowledge_retrieved: bool = False

    # === 附件处理 ===
    image_urls: List[str] = field(default_factory=list)
    file_contents: List[str] = field(default_factory=list)

    # === 工具上下文 ===
    tool_context: Dict[str, Any] = field(default_factory=dict)

    # === 停止控制 ===
    stop_event: Optional[threading.Event] = None

    # === 流式状态 ===
    streamed_message: Optional[Dict[str, Any]] = None

    # === 结果 ===
    final_content: str = ""
    stopped_prematurely: bool = False
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_errors: int = 0
    round_file_changes: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PipelineResult:
    """管道处理结果。"""

    final_content: str = ""
    assistant_message: Optional[Dict[str, Any]] = None
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    can_continue: bool = False
    stopped_prematurely: bool = False
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def to_chat_response(self) -> ChatResponse:
        """转换为 ChatResponse。"""
        return ChatResponse(
            final_content=self.final_content,
            assistant_message=self.assistant_message,
            tool_trace=self.tool_trace,
            can_continue=self.can_continue,
            usage=self.usage,
            error=self.error,
            metadata=self.metadata,
        )


# ============================================================================
# ProgressReporter 接口
# ============================================================================


class ProgressReporter(ABC):
    """抽象的进度报告接口。

    Web 频道通过 WebProgressReporter 实现，
    其他频道使用 NoOpProgressReporter。
    """

    def on_attachment_start(self, ctx: PipelineContext, count: int) -> None:
        pass

    def on_attachment_item(
        self, ctx: PipelineContext, name: str, item_type: str
    ) -> None:
        pass

    def on_attachment_item_done(
        self,
        ctx: PipelineContext,
        name: str,
        success: bool,
        result_preview: str = "",
    ) -> None:
        pass

    def on_attachments_done(self, ctx: PipelineContext) -> None:
        pass

    def on_knowledge_start(self, ctx: PipelineContext) -> None:
        pass

    def on_knowledge_done(self, ctx: PipelineContext, retrieved: bool) -> None:
        pass

    def on_thinking_start(self, ctx: PipelineContext) -> None:
        pass

    def on_thinking_content(self, ctx: PipelineContext, content: str) -> None:
        pass

    def on_tool_start(
        self,
        ctx: PipelineContext,
        tool_name: str,
        arguments: Dict[str, Any],
        thinking: str,
    ) -> None:
        pass

    def on_tool_done(
        self,
        ctx: PipelineContext,
        tool_name: str,
        result: Dict[str, Any],
        thinking: str,
    ) -> None:
        pass

    def on_tool_iteration(self, ctx: PipelineContext, iteration: int) -> None:
        pass

    def on_todo_updated(
        self, ctx: PipelineContext, tool_name: str, tool_result: Dict[str, Any]
    ) -> None:
        pass

    def on_send_message(self, ctx: PipelineContext, content: str) -> None:
        pass

    def on_send_file(
        self, ctx: PipelineContext, file_path: str, filename: str
    ) -> None:
        pass

    def on_done(self, ctx: PipelineContext) -> None:
        pass

    def on_waiting_confirmation(
        self, ctx: PipelineContext, command: str, request_id: str
    ) -> None:
        pass


class NoOpProgressReporter(ProgressReporter):
    """默认空实现，用于不支持进度的频道。"""
    pass


# ============================================================================
# PipelineCallbacks 基类
# ============================================================================


class PipelineCallbacks(ABC):
    """频道需实现的回调基类。

    所有方法都有默认实现，简单频道（Telegram、飞书）只需覆写约 2-4 个方法。
    """

    # ---- 会话 / 消息 I/O ----

    def load_messages(self, ctx: PipelineContext) -> List[Dict[str, Any]]:
        """返回会话的完整消息列表（包含 system prompt）。

        默认：用 get_system_prompt + 当前用户消息构建。
        """
        system = self.get_system_prompt(ctx)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})
        messages.append({"role": "user", "content": ctx.chat_request.content})
        return messages

    def get_system_prompt(self, ctx: PipelineContext) -> str:
        """返回此会话的系统提示词。"""
        return ""

    def save_assistant_message(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """持久化助手消息到会话存储。"""
        pass

    # ---- AI 模型交互 ----

    def build_model_call(
        self, ctx: PipelineContext, tools: List[Dict[str, Any]]
    ) -> Callable[..., Dict[str, Any]]:
        """返回 model_call 函数。

        默认实现使用全局 ai_client 和运行时配置。
        """
        from nbot.services.ai import ai_client, refresh_runtime_ai_config
        from nbot.core.model_adapter import (
            build_chat_completion_payload,
            normalize_chat_completion_data,
            resolve_chat_completion_url,
        )
        import requests

        def model_call(messages, stop_event=None):
            if stop_event and stop_event.is_set():
                raise StopIteration("User stopped")

            runtime_ai = refresh_runtime_ai_config()
            base_url = runtime_ai.get("base_url") or ""
            model = runtime_ai.get("model") or ai_client.model
            provider_type = runtime_ai.get("provider_type") or "openai_compatible"
            api_key = runtime_ai.get("api_key") or ""

            url = resolve_chat_completion_url(base_url, model=model, provider_type=provider_type)
            headers = {
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            }
            payload = build_chat_completion_payload(
                model,
                messages,
                base_url=base_url,
                provider_type=provider_type,
                tools=tools if tools else None,
                tool_choice="auto" if tools else None,
                stream=False,
            )
            resp = requests.post(url, json=payload, headers=headers, timeout=120)
            resp.raise_for_status()
            normalized = normalize_chat_completion_data(
                resp.json(),
                base_url=base_url,
                model=model,
                provider_type=provider_type,
            )
            return normalized.to_dict()

        return model_call

    def build_model_call_streaming(
        self, ctx: PipelineContext, tools: List[Dict[str, Any]]
    ) -> Optional[Callable]:
        """返回流式 model_call 或 None（不支持流式）。"""
        return None

    # ---- 输出 / 回复 ----

    def send_response(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        """发送最终助手消息给用户。必须覆写。"""
        raise NotImplementedError(
            "send_response must be implemented by the channel"
        )

    def on_stream_start(
        self, ctx: PipelineContext, message: Dict[str, Any]
    ) -> None:
        pass

    def on_stream_chunk(
        self, ctx: PipelineContext, chunk: str, message_id: str
    ) -> None:
        pass

    def on_stream_end(self, ctx: PipelineContext, message_id: str) -> None:
        pass

    # ---- 进度报告 ----

    def get_progress_reporter(self, ctx: PipelineContext) -> ProgressReporter:
        """返回 ProgressReporter 实例。默认返回空实现。"""
        return NoOpProgressReporter()

    # ---- 工具确认 ----

    def on_confirmation_required(
        self, ctx: PipelineContext, request_id: str, command: str
    ) -> None:
        """工具需要用户确认时调用。"""
        pass

    def check_confirmation(
        self, ctx: PipelineContext, user_input: str
    ) -> Optional[str]:
        """检查用户输入是否为确认/拒绝。返回 'confirm', 'reject', 或 None。"""
        return None

    # ---- 知识库 ----

    def search_knowledge(self, ctx: PipelineContext, query: str) -> str:
        """搜索知识库并返回格式化文本。默认不检索。"""
        return ""

    # ---- 工作区 ----

    def ensure_workspace(self, ctx: PipelineContext) -> str:
        """确保会话工作区存在。返回工作区路径。"""
        return ""

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        """返回工作区上下文字典（供工具使用）。"""
        return {}

    # ---- 附件解析 ----

    def resolve_attachment_data(
        self, ctx: PipelineContext, attachment: Dict[str, Any]
    ) -> Optional[Dict[str, Any]]:
        """解析单个附件，返回 {type, name, data, path, text_content, error} 或 None。"""
        return None

    # ---- 后处理 ----

    def on_response_complete(
        self, ctx: PipelineContext, result: PipelineResult
    ) -> None:
        """AI 响应完成后的回调。"""
        pass


# ============================================================================
# AIPipeline 主类
# ============================================================================


class AIPipeline:
    """统一 AI 处理管道。所有频道的单一入口点。"""

    # MIME 类型分类
    TEXT_MIME_TYPES = {
        "text/plain", "text/markdown", "text/csv", "text/html",
        "text/css", "text/javascript", "text/xml", "text/x-python",
        "text/x-shellscript", "text/x-sh", "text/x-bash", "text/x-c",
        "text/x-c++", "application/json", "application/xml",
        "application/javascript", "application/x-python",
        "application/x-shellscript",
    }

    TEXT_EXTENSIONS = {
        ".txt", ".md", ".csv", ".json", ".xml", ".html", ".css", ".js",
        ".py", ".sh", ".bash", ".c", ".h", ".cpp", ".hpp", ".java",
        ".go", ".rs", ".ts", ".tsx", ".jsx", ".yaml", ".yml", ".toml",
        ".ini", ".cfg", ".conf", ".log", ".sql", ".rb", ".php", ".pl",
        ".r", ".swift", ".kt", ".scala", ".lua", ".vim", ".tex",
    }

    DOCUMENT_MIME_TYPES = {
        "application/pdf",
        "application/msword",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-excel",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    }

    def process(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tool_iterations: int = 50,
        max_context_chars: int = 100000,
    ) -> PipelineResult:
        """执行完整的 AI 处理管道。

        Args:
            ctx: 管道上下文（含 ChatRequest 和 adapter）
            callbacks: 频道回调实现
            tools: 可用工具定义列表（None = 不启用工具）
            max_tool_iterations: 工具循环最大迭代次数
            max_context_chars: 上下文最大字符数

        Returns:
            PipelineResult 可转为 ChatResponse
        """
        progress = callbacks.get_progress_reporter(ctx)

        # Phase 1: 附件解析
        self._phase_attachments(ctx, callbacks, progress)

        # Phase 2: 知识库检索
        self._phase_knowledge(ctx, callbacks, progress)

        # Phase 3: 上下文准备
        self._phase_prepare_context(ctx, callbacks, tools, max_context_chars)

        # Phase 4: AI 响应（工具循环 或 直接补全 或 流式）
        self._phase_ai_response(ctx, callbacks, tools, max_tool_iterations, progress)

        # Phase 5: 结果组装
        result = self._phase_assemble_result(ctx, callbacks)
        callbacks.on_response_complete(ctx, result)

        return result

    # ------------------------------------------------------------------
    # Phase 1: 附件解析
    # ------------------------------------------------------------------

    def _phase_attachments(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        progress: ProgressReporter,
    ) -> None:
        attachments = ctx.chat_request.attachments
        if not attachments:
            return

        progress.on_attachment_start(ctx, len(attachments))

        for att in attachments:
            att_type = str(att.get("type", "")).lower()
            att_name = str(att.get("name", att.get("filename", "")))
            resolved = callbacks.resolve_attachment_data(ctx, att)

            if att_type.startswith("image/") or self._looks_like_image(att):
                self._handle_image_attachment(ctx, progress, att, resolved)
            elif self._is_text_type(att_type, att_name):
                self._handle_text_attachment(ctx, progress, att, resolved)
            elif self._is_document_type(att_type, att_name):
                self._handle_document_attachment(ctx, progress, att, resolved)

        progress.on_attachments_done(ctx)

    # ------------------------------------------------------------------
    # Phase 2: 知识库检索
    # ------------------------------------------------------------------

    def _phase_knowledge(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        progress: ProgressReporter,
    ) -> None:
        progress.on_knowledge_start(ctx)
        ctx.knowledge_text = callbacks.search_knowledge(
            ctx, ctx.chat_request.content
        )
        ctx.knowledge_retrieved = bool(ctx.knowledge_text)
        progress.on_knowledge_done(ctx, ctx.knowledge_retrieved)

    # ------------------------------------------------------------------
    # Phase 3: 上下文准备
    # ------------------------------------------------------------------

    def _phase_prepare_context(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        tools: Optional[List[Dict[str, Any]]],
        max_context_chars: int,
    ) -> None:
        from nbot.core.agent_service import prepare_chat_context

        # 加载消息历史
        messages_raw = callbacks.load_messages(ctx)
        messages_for_ai = copy.deepcopy(messages_raw)

        # 追加当前用户消息（如果 load_messages 未包含）
        user_content = ctx.chat_request.content
        if not messages_for_ai or messages_for_ai[-1].get("role") != "user" or messages_for_ai[-1].get("content") != user_content:
            messages_for_ai.append({"role": "user", "content": user_content})

        # 注入附件内容
        if ctx.file_contents:
            enhanced_content = user_content
            for fc in ctx.file_contents:
                if fc:
                    enhanced_content += "\n\n" + fc
            # 找到最后一条 user 消息并更新
            for msg in reversed(messages_for_ai):
                if msg.get("role") == "user":
                    msg["content"] = enhanced_content
                    break

        # 图片 URL 注入
        if ctx.image_urls:
            for msg in reversed(messages_for_ai):
                if msg.get("role") == "user":
                    msg["content"] = (
                        f"[附图片 {len(ctx.image_urls)} 张，已通过视觉模型识别]\n"
                        + msg.get("content", "")
                    )
                    break

        # 调用现有的上下文准备
        prepared = prepare_chat_context(
            messages_for_ai,
            user_content,
            knowledge_text=ctx.knowledge_text,
            max_total_chars=max_context_chars,
        )
        ctx.messages = prepared.messages
        ctx.tool_call_history = prepared.tool_call_history

    # ------------------------------------------------------------------
    # Phase 4: AI 响应
    # ------------------------------------------------------------------

    def _phase_ai_response(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        tools: Optional[List[Dict[str, Any]]],
        max_tool_iterations: int,
        progress: ProgressReporter,
    ) -> None:
        # 报告思考开始
        progress.on_thinking_start(ctx)

        # 尝试流式
        if tools is None:
            streamer = callbacks.build_model_call_streaming(ctx, tools or [])
            if streamer is not None:
                self._run_streaming(ctx, callbacks, streamer, progress)
                return

        # 尝试工具循环
        if tools:
            self._run_tool_loop(ctx, callbacks, tools, max_tool_iterations, progress)
            return

        # 简单路径：单次模型调用
        self._run_simple(ctx, callbacks)

    def _run_simple(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
    ) -> None:
        """简单的单次模型调用（无工具、无流式）。"""
        model_call = callbacks.build_model_call(ctx, [])
        try:
            response = model_call(ctx.messages, stop_event=ctx.stop_event)
        except StopIteration:
            ctx.stopped_prematurely = True
            ctx.final_content = "【生成已停止】"
            return
        except Exception as e:
            _log.error(f"Simple model call failed: {e}")
            ctx.error = str(e)
            ctx.final_content = f"AI 调用失败: {e}"
            return

        ctx.final_content = response.get("content", "")
        ctx.usage = response.get("usage", {})

    def _run_tool_loop(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        tools: List[Dict[str, Any]],
        max_tool_iterations: int,
        progress: ProgressReporter,
    ) -> None:
        """运行工具调用循环。"""
        from nbot.core.agent_service import (
            ToolLoopSession,
            ToolLoopHooks,
            run_tool_loop_session,
            ToolLoopExit,
            resolve_loop_final_content,
            build_continue_chat_response,
            extract_tool_call_history,
        )
        from nbot.services.tools import execute_tool

        model_call = callbacks.build_model_call(ctx, tools)
        ctx.tool_context = callbacks.get_workspace_context(ctx)

        # 工具执行器
        def tool_executor(tool_call, thinking_content, iteration, tool_messages):
            name = tool_call.get("name", "")
            args = tool_call.get("arguments", {})
            if isinstance(args, str):
                try:
                    args = json.loads(args)
                except json.JSONDecodeError:
                    args = {}

            result = execute_tool(name, args, ctx.tool_context)

            # 处理确认请求
            if result.get("require_confirmation"):
                request_id = result.get("request_id", "")
                command = result.get("command", "")
                callbacks.on_confirmation_required(ctx, request_id, command)
                progress.on_waiting_confirmation(ctx, command, request_id)
                raise ToolLoopExit(
                    result.get(
                        "message",
                        f"⚠️ 命令需要确认: {command}\n"
                        f"[请求ID: {request_id}]\n"
                        f"请回复「确认」执行，或「取消」放弃。",
                    )
                )

            return result

        # 工具循环钩子 → 进度报告
        def on_iteration_start(iteration, messages):
            progress.on_tool_iteration(ctx, iteration)

        def on_tool_start(tool_call, thinking, iteration, messages):
            name = tool_call.get("name", "")
            args = tool_call.get("arguments", {})
            progress.on_tool_start(ctx, name, args, thinking)

        def on_tool_result(tool_call, result, thinking, iteration, messages):
            name = tool_call.get("name", "")
            progress.on_tool_done(ctx, name, result, thinking)
            # 处理特殊工具结果
            if result.get("_send_message"):
                progress.on_send_message(ctx, result.get("_send_message", ""))
            if result.get("_file_path"):
                progress.on_send_file(
                    ctx,
                    result.get("_file_path", ""),
                    result.get("_file_name", ""),
                )
            return None  # 使用默认 tool message 格式

        hooks = ToolLoopHooks(
            on_iteration_start=on_iteration_start,
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,
        )

        session = ToolLoopSession(
            initial_messages=ctx.messages,
            model_call=model_call,
            tool_executor=tool_executor,
            tool_call_history=ctx.tool_call_history,
            max_iterations=max_tool_iterations,
            stop_event=ctx.stop_event,
            hooks=hooks,
        )

        try:
            execution_result = run_tool_loop_session(session)
        except Exception as e:
            _log.error(f"Tool loop failed: {e}")
            ctx.error = str(e)
            ctx.final_content = f"工具循环执行失败: {e}"
            return

        loop_result = execution_result.loop_result

        if loop_result.stopped:
            ctx.stopped_prematurely = True
            ctx.tool_trace = extract_tool_call_history(loop_result.tool_messages)
            ctx.final_content = "【生成已停止 - 工具调用记录已保存，回复「继续」可继续执行】"
            return

        ctx.final_content = resolve_loop_final_content(loop_result)

    def _run_streaming(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        streamer: Callable,
        progress: ProgressReporter,
    ) -> None:
        """运行流式模型调用。"""
        message_id = ""
        full_content = ""

        try:
            for event in streamer(ctx.messages, stop_event=ctx.stop_event):
                if ctx.stop_event and ctx.stop_event.is_set():
                    break

                chunk = event.get("content", "") if isinstance(event, dict) else str(event)
                if not chunk:
                    continue

                if not full_content:
                    # 首块
                    msg = {"role": "assistant", "content": "", "id": message_id}
                    callbacks.on_stream_start(ctx, msg)

                full_content += chunk
                callbacks.on_stream_chunk(ctx, chunk, message_id)
        except Exception as e:
            _log.error(f"Streaming failed: {e}")
            ctx.error = str(e)
            full_content = full_content or f"流式输出失败: {e}"

        callbacks.on_stream_end(ctx, message_id)
        ctx.final_content = full_content
        ctx.metadata["streamed"] = True

    # ------------------------------------------------------------------
    # Phase 5: 结果组装
    # ------------------------------------------------------------------

    def _phase_assemble_result(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
    ) -> PipelineResult:
        from nbot.core.agent_service import extract_tool_call_history

        if ctx.error:
            result = PipelineResult(
                final_content=ctx.final_content or ctx.error,
                error=ctx.error,
                metadata=ctx.metadata,
            )
            return result

        # 构建 assistant_message
        if ctx.adapter and hasattr(ctx.adapter, "build_assistant_message"):
            temp_response = ChatResponse(
                final_content=ctx.final_content,
                tool_trace=ctx.tool_trace,
                usage=ctx.usage,
            )
            assistant_message = ctx.adapter.build_assistant_message(
                temp_response,
                conversation_id=ctx.chat_request.conversation_id,
            )
        else:
            assistant_message = {
                "role": "assistant",
                "content": ctx.final_content,
            }

        # 添加工具调用历史（用于「继续」功能）
        if ctx.tool_trace:
            assistant_message["tool_call_history"] = ctx.tool_trace
            assistant_message["can_continue"] = True

        # 保存历史
        callbacks.save_assistant_message(ctx, assistant_message)

        # 发送回复
        callbacks.send_response(ctx, assistant_message)

        result = PipelineResult(
            final_content=ctx.final_content,
            assistant_message=assistant_message,
            tool_trace=ctx.tool_trace,
            can_continue=bool(ctx.tool_trace),
            stopped_prematurely=ctx.stopped_prematurely,
            usage=ctx.usage,
            error=ctx.error,
            metadata=ctx.metadata,
        )
        return result

    # ------------------------------------------------------------------
    # 附件辅助方法
    # ------------------------------------------------------------------

    @staticmethod
    def _looks_like_image(att: Dict[str, Any]) -> bool:
        """判断附件是否像图片。"""
        att_type = str(att.get("type", "")).lower()
        att_name = str(att.get("name", att.get("filename", ""))).lower()
        if att_type.startswith("image/"):
            return True
        image_ext = {".jpg", ".jpeg", ".png", ".gif", ".bmp", ".webp", ".svg"}
        _, ext = "", ""
        if "." in att_name:
            ext = "." + att_name.rsplit(".", 1)[-1]
        return ext in image_ext

    @classmethod
    def _is_text_type(cls, att_type: str, att_name: str) -> bool:
        """判断附件是否为文本类型。"""
        if att_type in cls.TEXT_MIME_TYPES:
            return True
        _, ext = "", ""
        if "." in att_name:
            ext = "." + att_name.rsplit(".", 1)[-1]
        return ext.lower() in cls.TEXT_EXTENSIONS

    @classmethod
    def _is_document_type(cls, att_type: str, att_name: str) -> bool:
        """判断附件是否为文档类型（PDF/DOCX/XLSX/PPT）。"""
        if att_type in cls.DOCUMENT_MIME_TYPES:
            return True
        doc_ext = {".pdf", ".docx", ".doc", ".xlsx", ".xls", ".pptx", ".ppt"}
        _, ext = "", ""
        if "." in att_name:
            ext = "." + att_name.rsplit(".", 1)[-1]
        return ext.lower() in doc_ext

    def _handle_image_attachment(
        self,
        ctx: PipelineContext,
        progress: ProgressReporter,
        att: Dict[str, Any],
        resolved: Optional[Dict[str, Any]],
    ) -> None:
        name = att.get("name", att.get("filename", "image"))
        progress.on_attachment_item(ctx, name, "image")

        if resolved and resolved.get("data"):
            ctx.image_urls.append(resolved["data"])
            progress.on_attachment_item_done(ctx, name, True)
        elif resolved and resolved.get("path"):
            ctx.image_urls.append(resolved["path"])
            progress.on_attachment_item_done(ctx, name, True)
        else:
            # 尝试从 attachment 直接获取 URL/path
            url = att.get("url") or att.get("path") or att.get("data")
            if url:
                ctx.image_urls.append(url)
                progress.on_attachment_item_done(ctx, name, True)
            else:
                progress.on_attachment_item_done(ctx, name, False, "无法解析图片")

    def _handle_text_attachment(
        self,
        ctx: PipelineContext,
        progress: ProgressReporter,
        att: Dict[str, Any],
        resolved: Optional[Dict[str, Any]],
    ) -> None:
        name = att.get("name", att.get("filename", "file"))
        progress.on_attachment_item(ctx, name, "file")

        content = None
        if resolved and resolved.get("text_content"):
            content = resolved["text_content"]
        elif resolved and resolved.get("data"):
            content = resolved["data"]

        if content:
            ctx.file_contents.append(
                f"【文件 {name} 内容】:\n{str(content)[:10000]}"
            )
            preview = str(content)[:200].replace("\n", " ")
            progress.on_attachment_item_done(ctx, name, True, preview)
        else:
            progress.on_attachment_item_done(ctx, name, False, "无法读取文件内容")

    def _handle_document_attachment(
        self,
        ctx: PipelineContext,
        progress: ProgressReporter,
        att: Dict[str, Any],
        resolved: Optional[Dict[str, Any]],
    ) -> None:
        name = att.get("name", att.get("filename", "document"))
        progress.on_attachment_item(ctx, name, "document")

        # 尝试用 file_parser 解析
        try:
            from nbot.core.file_parser import parse_file

            file_path = None
            if resolved and resolved.get("path"):
                file_path = resolved["path"]

            if file_path:
                parsed = parse_file(file_path)
                if parsed and parsed.get("content"):
                    ctx.file_contents.append(
                        f"【文档 {name} 解析内容】:\n{str(parsed['content'])[:10000]}"
                    )
                    progress.on_attachment_item_done(ctx, name, True, "文档已解析")
                    return
        except Exception:
            pass

        progress.on_attachment_item_done(ctx, name, True, "文档已记录（未提取文本）")


# ============================================================================
# 共用的确认处理（各频道入口调用）
# ============================================================================

_CONFIRM_KEYWORDS = {"确认", "同意", "确认执行", "是", "yes", "y", "ok", "执行"}
_REJECT_KEYWORDS = {"取消", "拒绝", "否", "不执行", "no", "n", "cancel"}


def handle_tool_confirmation(
    content: str,
    session_id: str,
    *,
    log_prefix: str = "",
) -> str:
    """检测并处理工具确认/拒绝。

    在各频道入口处调用，检测用户输入是否为确认/拒绝关键词。
    如果是确认，则执行待处理命令并返回执行结果文本。
    如果是拒绝，则拒绝待处理命令并返回拒绝文本。
    如果不是确认/拒绝，返回原始 content。

    Returns:
        替换后的消息内容（原始内容 或 确认/拒绝结果文本）
    """
    stripped = (content or "").strip().lower()
    is_confirm = stripped in _CONFIRM_KEYWORDS or (
        len(stripped) <= 4 and any(kw in stripped for kw in _CONFIRM_KEYWORDS)
    )
    is_reject = stripped in _REJECT_KEYWORDS or (
        len(stripped) <= 4 and any(kw in stripped for kw in _REJECT_KEYWORDS)
    )

    if not (is_confirm or is_reject):
        return content

    if is_confirm and is_reject:
        return content  # 歧义，不处理

    try:
        from nbot.services.tools import (
            get_pending_by_session,
            execute_pending_command,
            reject_pending_command,
        )

        if not get_pending_by_session:
            return content

        request_id = get_pending_by_session(session_id)
        if not request_id:
            return content

        if is_confirm:
            prefix = f"[{log_prefix}]" if log_prefix else ""
            print(f"{prefix} 用户确认执行待处理命令: session={session_id}")
            exec_result = execute_pending_command(request_id)
            if exec_result.get("executed"):
                cmd = exec_result.get("command", "")
                stdout = exec_result.get("stdout", "")
                stderr = exec_result.get("stderr", "")
                result_msg = f"[系统] 用户已确认执行命令 `{cmd}`。\n\n执行结果:\n{stdout}"
                if stderr:
                    result_msg += f"\n\n错误输出:\n{stderr}"
                return result_msg
            else:
                return f"[系统] 执行命令失败: {exec_result.get('error', '未知错误')}"
        else:
            prefix = f"[{log_prefix}]" if log_prefix else ""
            print(f"{prefix} 用户拒绝执行待处理命令: session={session_id}")
            reject_result = reject_pending_command(request_id)
            cmd = reject_result.get("command", "")
            return f"[系统] 用户已拒绝执行命令 `{cmd}`。"
    except Exception:
        pass

    return content


# ============================================================================
# 全局单例
# ============================================================================

ai_pipeline = AIPipeline()

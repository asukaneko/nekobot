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
import os
import threading
from abc import ABC
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Tuple

from nbot.core.background_tasks import submit_background_task
from nbot.core.chat_models import ChatRequest, ChatResponse
from nbot.core.model_proxy import model_proxy_request_kwargs
from nbot.core.sampling import request_extra_body

_log = logging.getLogger(__name__)

def _level_rank(level: str) -> int:
    """Rank plot choice levels for comparison."""
    return {"normal": 0, "important": 1, "turning_point": 2, "ending": 3}.get(level, 0)


def _plot_turn_context_dict(turn_context: Any) -> Dict[str, Any]:
    """Convert CharacterTurnContext to the dict expected by PlotChoiceGenerator."""
    if isinstance(turn_context, dict):
        return turn_context
    if not turn_context:
        return {}

    state = getattr(turn_context, "state", None)
    relationship = getattr(turn_context, "relationship", None)
    context = {
        "mood": getattr(state, "mood", "") if state else "",
    }
    if relationship:
        context["relationship"] = (
            f"好感 {getattr(relationship, 'affection', 0)}/100, "
            f"信任 {getattr(relationship, 'trust', 0)}/100, "
            f"熟悉 {getattr(relationship, 'familiarity', 0)}/100, "
            f"依赖 {getattr(relationship, 'dependency', 0)}/100, "
            f"安全感 {getattr(relationship, 'security', 0)}/100, "
            f"嫉妒 {getattr(relationship, 'jealousy', 0)}/100"
        )
    return context




def _custom_prompt_stack_key(custom_prompt: Dict[str, Any]) -> str:
    """Build a stable PromptStack key for a session custom prompt."""
    order = custom_prompt.get("order", 0)
    title = str(custom_prompt.get("title") or "").strip()
    if title:
        return f"custom:{order}:{title}"
    return f"custom:{order}"


def _annotate_group_message_senders(messages: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Make sender names visible to the model in group-chat history."""
    annotated = copy.deepcopy(messages)
    for message in annotated:
        role = message.get("role", "")
        if role not in ("assistant", "user"):
            continue
        sender = str(message.get("sender") or "").strip()
        content = str(message.get("content") or "")
        if not sender or not content:
            continue
        if role == "assistant" and sender == "AI":
            continue
        prefix = f"【{sender}】"
        if content.startswith(prefix):
            continue
        message["content"] = f"{prefix}{content}"
    return annotated


# ============================================================================
# 公共 Token 统计
# ============================================================================


def _record_channel_token_stats(ctx: "PipelineContext", result: "PipelineResult") -> None:
    """公共频道 token 用量记录，供所有频道自动调用。

    频道需在 ctx.metadata 中设置:
      - channel_type: 频道类型标识 (telegram / feishu / web / qq 等)
      - source: 来源标识
    可选:
      - input_price / output_price: 自定义价格（元/百万token）
      - duration_ms / ttft_ms: 时间指标（毫秒）
    """
    try:
        usage = result.usage if result else {}
        if not usage:
            return
        total = usage.get("total_tokens", 0)
        if not total:
            return

        meta = result.metadata or {}
        channel_type = meta.get("channel_type") or ctx.metadata.get("channel_type", "")
        source = meta.get("source") or ctx.metadata.get("source", "")
        if not channel_type:
            return

        from nbot.core.token_stats import get_token_stats_manager

        model = (
            meta.get("model_name", "")
            or meta.get("model_id", "")
            or ctx.metadata.get("model_name", "")
            or ctx.metadata.get("model_id", "")
            or ""
        )
        session_id = ctx.chat_request.conversation_id or ""
        user_id = ctx.chat_request.user_id or session_id

        input_price = ctx.metadata.get("input_price")
        output_price = ctx.metadata.get("output_price")
        cached_input_price = ctx.metadata.get("cached_input_price")
        try:
            cached_tokens = int(usage.get("cached_tokens") or 0)
        except (TypeError, ValueError):
            cached_tokens = 0
        # 时间指标：优先从 result.metadata，回退到 ctx.metadata
        duration_ms = meta.get("duration_ms") or ctx.metadata.get("duration_ms")
        ttft_ms = meta.get("ttft_ms") or ctx.metadata.get("ttft_ms")

        get_token_stats_manager().record_usage(
            usage.get("prompt_tokens", 0),
            usage.get("completion_tokens", 0),
            total_tokens=total,
            model=model,
            session_id=session_id,
            channel_type=channel_type,
            user_id=user_id,
            source=source,
            duration_ms=duration_ms,
            ttft_ms=ttft_ms,
            input_price=input_price,
            output_price=output_price,
            cached_tokens=cached_tokens,
            cached_input_price=cached_input_price,
        )
        ctx.metadata["token_recorded"] = True
    except Exception as e:
        _log.debug("Token stats recording failed: %s", e)


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

    # === 角色运行时 ===
    character_turn: Any = None

    # === 结果 ===
    final_content: str = ""
    stopped_prematurely: bool = False
    tool_trace: List[Dict[str, Any]] = field(default_factory=list)
    consecutive_errors: int = 0
    round_file_changes: List[Dict[str, Any]] = field(default_factory=list)
    usage: Dict[str, Any] = field(default_factory=dict)
    error: Optional[str] = None
    metadata: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        """延迟初始化 PromptStack，避免循环导入"""
        if not hasattr(self, '_prompt_stack'):
            from nbot.character.prompt_stack import PromptStack
            self._prompt_stack = PromptStack()

    @property
    def prompt_stack(self):
        if not hasattr(self, '_prompt_stack'):
            from nbot.character.prompt_stack import PromptStack
            self._prompt_stack = PromptStack()
        return self._prompt_stack

    @property
    def mode_policy(self) -> "ModePolicy":
        """从 metadata 派生的模式策略（每次调用都重新派生，确保 metadata 变化能反映）。"""
        return ModePolicy.from_metadata(self.metadata)


_TRUE_FLAGS = frozenset({"1", "true", "yes", "on", "y", "t"})


def parse_bool_flag(value: Any, default: bool = False) -> bool:
    """宽松解析会话里的布尔开关（True/1/"true"/"on" 等）。"""
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    return str(value).strip().lower() in _TRUE_FLAGS


class ModePolicy:
    """会话模式策略：集中 agent / character / group 模式的差异化决策。

    背景：
        原先通过 `ctx.metadata.get("session_mode") == "agent"` 在 ai_pipeline.py
        多处做散落 if/else 短路。这导致每加一个 character-only 功能就要在多处
        加判断，易遗漏且难维护。本类将所有 mode 相关决策集中到一处。

    使用方式：
        policy = ctx.mode_policy
        if policy.inject_character_memories_legacy:  # 替代 if ctx.metadata.get("session_mode") != "agent"
            ...

    agent 继承角色能力（P2-8）：
        会话级开关 ``agent_inherit_character`` 打开后，agent 会话在保留工具能力的
        同时加载角色卡 / 记忆 / 世界书 / 关系状态。默认关闭，行为与改动前一致。

    兼容性：
        - 向后兼容 ctx.metadata["session_mode"] 字符串判断
        - 所有原 == "agent" / != "agent" 的判断等价迁移到本类的属性
        - 未打开开关时行为完全等价，无功能变更
    """

    __slots__ = ("session_mode", "inherit_character")

    def __init__(self, session_mode: str = "character", inherit_character: bool = False):
        self.session_mode = session_mode or "character"
        self.inherit_character = bool(inherit_character)

    @classmethod
    def from_metadata(cls, metadata: Dict[str, Any]) -> "ModePolicy":
        """从 ctx.metadata 派生策略。"""
        metadata = metadata or {}
        return cls(
            session_mode=str(metadata.get("session_mode", "character")),
            inherit_character=parse_bool_flag(metadata.get("agent_inherit_character", False)),
        )

    @classmethod
    def character_features_enabled(cls, session: Any) -> bool:
        """会话是否应加载角色能力（角色模式恒 True；agent 模式看开关）。"""
        if not isinstance(session, dict):
            return False
        if str(session.get("session_mode") or "character") != "agent":
            return True
        return parse_bool_flag(session.get("agent_inherit_character", False))

    @property
    def is_agent(self) -> bool:
        """是否为 agent 模式（默认不跑角色运行时、无角色记忆）。"""
        return self.session_mode == "agent"

    @property
    def inherits_character(self) -> bool:
        """是否加载角色能力：角色模式恒真；agent 模式取决于会话开关。"""
        return (not self.is_agent) or self.inherit_character

    # ---- 上下文准备阶段（_phase_prepare_context） ----
    @property
    def inject_character_memories_legacy(self) -> bool:
        """是否注入跨会话角色记忆（character.memories_legacy）。"""
        return self.inherits_character

    @property
    def inject_memory_fs(self) -> bool:
        """是否注入 MemoryFS 结构化记忆。"""
        return self.inherits_character

    @property
    def inject_image_capability(self) -> bool:
        """是否注入角色生图能力说明。"""
        return self.inherits_character

    # ---- 角色运行时 hooks ----
    @property
    def inject_real_time_prompt_fallback(self) -> bool:
        """是否注入实时连续性 prompt（real_time.continuity）。"""
        return self.inherits_character

    # ---- 结果后处理阶段（_post_process_result） ----
    @property
    def run_auto_memory(self) -> bool:
        """是否执行 auto_memory 6 轮累积。"""
        return self.inherits_character

    @property
    def try_send_image(self) -> bool:
        """是否尝试解析 [send_image: ...] 标签触发角色生图。

        agent 会话即便继承角色能力也不主动发图：生图应走 generate_image 工具，
        避免自动冒出的图片打断工具型对话。
        """
        return not self.is_agent


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

    def auto_compress_context(
        self,
        ctx: PipelineContext,
        messages: List[Dict[str, Any]],
        threshold_chars: int,
    ) -> List[Dict[str, Any]]:
        """上下文达到阈值时的自动压缩钩子。

        达到 threshold_chars 时由通道实现执行摘要压缩并持久化；
        默认不压缩，原样返回。失败时应按未压缩继续（返回原列表）。
        """
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
        self,
        ctx: PipelineContext,
        tools: List[Dict[str, Any]],
        model_configs: Optional[List[Dict[str, Any]]] = None,
    ) -> Callable[..., Dict[str, Any]]:
        """返回 model_call 函数。

        默认实现使用全局 ai_client 和运行时配置。
        自动从配置加载 model_configs，多个模型时启用故障转移。
        """
        from nbot.services.ai import ai_client, refresh_runtime_ai_config
        from nbot.core.protocols import get_protocol
        from nbot.core.model_adapter import response_json_utf8
        import requests

        # 自动获取模型配置（当调用方未提供时）
        model_configs = self._ensure_model_configs(model_configs)

        # 如果有多个模型配置，启用故障转移
        if model_configs and len(model_configs) > 1:
            purpose = model_configs[0].get("purpose", "chat")
            ai_pipeline = AIPipeline()
            return ai_pipeline._wrap_with_failover(model_configs, purpose, tools=tools)

        def model_call(messages, stop_event=None):
            if stop_event and stop_event.is_set():
                raise StopIteration("User stopped")

            runtime_ai = refresh_runtime_ai_config()
            base_url = runtime_ai.get("base_url") or ""
            model = runtime_ai.get("model") or ai_client.model
            provider_type = runtime_ai.get("provider_type") or "openai_compatible"
            api_key = runtime_ai.get("api_key") or ""
            append_base_url_path = runtime_ai.get("append_base_url_path", True)
            proxy_url = runtime_ai.get("proxy_url", "")

            protocol = get_protocol(provider_type)
            url = protocol.resolve_url(
                base_url,
                model=model,
                append_base_url_path=append_base_url_path,
                api_key=api_key,
            )
            headers = protocol.build_headers(api_key)
            payload = protocol.build_payload(
                model,
                messages,
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
            result = normalized.to_dict()
            result["_model_id"] = model
            result["_model_name"] = model
            return result

        return model_call

    def build_model_call_streaming(
        self,
        ctx: PipelineContext,
        tools: List[Dict[str, Any]],
        model_configs: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[Callable]:
        """返回流式 model_call 或 None（不支持流式）。"""
        return None

    def _ensure_model_configs(
        self,
        model_configs: Optional[List[Dict[str, Any]]] = None,
    ) -> Optional[List[Dict[str, Any]]]:
        """确保 model_configs 已加载，供子类复用。"""
        if model_configs is None:
            from nbot.web.utils.config_loader import get_model_configs_by_purpose
            from nbot.services.ai import refresh_runtime_ai_config
            runtime_ai = refresh_runtime_ai_config()
            purpose = runtime_ai.get("purpose", "chat")
            model_configs = get_model_configs_by_purpose(purpose)
        return model_configs

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

    def search_knowledge_with_citations(
        self, ctx: PipelineContext, query: str
    ) -> Tuple[str, List[Dict[str, Any]]]:
        """检索知识库并返回 (文本, citations)；默认不支持引用信息。"""
        return self.search_knowledge(ctx, query), []

    # ---- 工作区 ----

    def ensure_workspace(self, ctx: PipelineContext) -> str:
        """确保会话工作区存在。返回工作区路径。"""
        return ""

    def get_workspace_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        """返回工作区上下文字典（供工具使用）。"""
        return {}

    def get_memory_context(self, ctx: PipelineContext) -> Dict[str, Any]:
        """返回自动记忆需要的频道上下文。"""
        return self.get_workspace_context(ctx)

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
        """AI 响应完成后的回调。默认自动记录 token 用量。"""
        # 计算 duration_ms / ttft_ms（非流式调用 TTFT ≈ duration）
        try:
            start_time = ctx.metadata.pop("_pipeline_start_time", None)
            if start_time is not None:
                import time as _time
                duration_ms = (_time.perf_counter() - start_time) * 1000.0
                if result.metadata is None:
                    result.metadata = {}
                result.metadata.setdefault("duration_ms", duration_ms)
                result.metadata.setdefault("ttft_ms", duration_ms)
        except Exception:
            pass
        _record_channel_token_stats(ctx, result)

    # ---- 表情包 ----

    def on_plot_choices_ready(
        self, ctx: PipelineContext, result: PipelineResult
    ) -> None:
        """Called when async plot choice generation finishes."""
        pass

    def send_sticker(
        self, ctx: PipelineContext, sticker_info: Dict[str, Any]
    ) -> None:
        """发送表情包图片（单独消息）。默认空实现，由各频道覆写。

        Args:
            ctx: 管道上下文
            sticker_info: 表情包信息字典 {url, artist_name, anime_name, endpoint}
        """
        pass

    # ---- 角色生图 ----

    def send_image(
        self, ctx: PipelineContext, image_info: Dict[str, Any]
    ) -> None:
        """发送 AI 生成的图片（单独消息）。默认空实现，由各频道覆写。

        Args:
            ctx: 管道上下文
            image_info: 图片信息字典 {url, prompt, trigger}
                - url: 可直接外链的图片 URL，或本地 /static/... 路径
                - prompt: 用于生成该图片的 prompt
                - trigger: "tag"（LLM 主动标签）或 "auto"（概率触发）
        """
        pass

    # ---- 角色运行时 ----

    def get_character_context(self, ctx: PipelineContext):
        """返回角色身份标识 (CharacterIdentity)，默认 None 表示不启用角色运行时。"""
        return None

    def get_character_runtime(self, ctx: PipelineContext):
        """返回 CharacterRuntime 实例，默认 None。"""
        return None


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

    _middleware_initialized = False

    @classmethod
    def _ensure_middleware_initialized(cls) -> None:
        if cls._middleware_initialized:
            return
        from nbot.core.message_middleware import MediaDescriber
        from nbot.services.ai import ai_client as _ai_client

        def _describe_image(url: str):
            return _ai_client.describe_image(url, "请描述这个图片的内容，仅作描述，不要分析内容")

        def _describe_video(url: str):
            return _ai_client.describe_video(url)

        def _describe_audio(url: str):
            return _ai_client.describe_audio(url) if hasattr(_ai_client, 'describe_audio') else None

        def _describe_gif(url: str):
            return _ai_client.describe_gif_as_video(url)

        MediaDescriber.register("image", _describe_image)
        MediaDescriber.register("gif", _describe_gif)
        MediaDescriber.register("video", _describe_video)
        MediaDescriber.register("audio", _describe_audio)
        cls._middleware_initialized = True

    def process(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        *,
        tools: Optional[List[Dict[str, Any]]] = None,
        max_tool_iterations: int = 50,
        max_context_chars: int = 100000,
        hook_runtime=None,
        group_context: Optional[Dict[str, Any]] = None,
    ) -> PipelineResult:
        """执行完整的 AI 处理管道。

        Args:
            ctx: 管道上下文（含 ChatRequest 和 adapter）
            callbacks: 频道回调实现
            tools: 可用工具定义列表（None = 不启用工具）
            max_tool_iterations: 工具循环最大迭代次数
            max_context_chars: 上下文最大字符数
            hook_runtime: HookManager 实例（可选，None 时零开销）
            group_context: 群聊上下文（可选，含 group, scheduler, characters 等）

        Returns:
            PipelineResult 可转为 ChatResponse
        """
        self._hook_runtime = hook_runtime
        self._group_context = group_context
        self._reset_hook_runtime_turn()
        # 记录管道起始时间，供 on_response_complete 计算 duration
        import time as _time
        ctx.metadata.setdefault("_pipeline_start_time", _time.perf_counter())

        # ask_user_question 等待恢复：把用户回答转换为带问题上下文的消息
        try:
            from nbot.services.ask_user_tools import handle_pending_user_answer

            session_id = str(getattr(ctx.chat_request, "session_id", "") or "")
            if session_id and ctx.chat_request.content:
                ctx.chat_request.content = handle_pending_user_answer(
                    ctx.chat_request.content, session_id
                )
        except Exception as exc:
            _log.debug("[AskUser] 答案恢复失败: %s", exc)

        self._emit_hook("conversation.before_receive", ctx)

        # Phase 0: 群聊发言角色选择（群聊模式下）
        if group_context:
            self._phase_group_speaker_select(ctx, group_context)

        # === 通用消息预处理（附件下载 + 媒体描述 + 工作区保存） ===
        self._ensure_middleware_initialized()
        from nbot.core.message_middleware import MessagePreprocessor
        ws_ctx = callbacks.get_workspace_context(ctx)
        MessagePreprocessor.process(ctx.chat_request, workspace_context=ws_ctx or None)

        progress = callbacks.get_progress_reporter(ctx)

        # Phase 1: 附件解析
        self._emit_hook("pipeline.before_attachments", ctx)
        self._phase_attachments(ctx, callbacks, progress)
        self._emit_hook("pipeline.after_attachments", ctx)

        # Phase 2: 知识库检索
        self._emit_hook("pipeline.before_knowledge", ctx)
        self._phase_knowledge(ctx, callbacks, progress)
        self._emit_hook("pipeline.after_knowledge", ctx)

        # Phase 2.5: 群聊 prompt 注入
        if self._group_context:
            self._phase_group_prompt_build(ctx, self._group_context)

        # Phase 3: 上下文准备
        self._phase_prepare_context(ctx, callbacks, tools, max_context_chars)

        # Phase 4: AI 响应（工具循环 或 直接补全 或 流式）
        self._emit_hook("model.before_call", ctx)
        self._phase_ai_response(ctx, callbacks, tools, max_tool_iterations, progress)
        self._emit_hook("model.after_call", ctx)

        # 命令授权是工具循环的暂停点，不是一次 assistant 回复。
        # 等待用户处理期间不落库、不推送空消息，也不触发回复后处理。
        if ctx.metadata.get("waiting_confirmation"):
            ctx.final_content = ""
            return PipelineResult(
                final_content="",
                tool_trace=ctx.tool_trace,
                usage=ctx.usage,
                metadata=ctx.metadata,
            )

        # Phase 5: 结果组装（内部包含角色运行时 after_turn 和自动记忆）
        self._emit_hook("reply.before_send", ctx)
        result = self._phase_assemble_result(ctx, callbacks)
        self._emit_hook("reply.after_send", ctx)

        # Phase 6: 剧情选项生成（异步，不阻塞主回复）
        self._generate_plot_choices_if_enabled(ctx, result, callbacks)

        # Phase 7: 群聊旁白生成（条件触发）
        if self._group_context and self._group_context.get("auto_narrate"):
            self._phase_group_narrator(ctx, result, self._group_context)

        return result

    def _emit_hook(self, event_type, ctx, extra_payload=None):
        """Emit a hook event if hook_runtime is available. Zero overhead when None."""
        if not getattr(self, '_hook_runtime', None):
            return
        try:
            from nbot.hooks.models import RuntimeEvent
            payload = {}
            if ctx and ctx.chat_request:
                payload["channel"] = getattr(ctx.chat_request, "channel", "")
                payload["content_preview"] = getattr(ctx.chat_request, "content", "")[:100]
            if extra_payload:
                payload.update(extra_payload)
            event = RuntimeEvent(
                type=event_type,
                source="ai_pipeline",
                conversation_id=getattr(ctx.chat_request, "conversation_id", "") if ctx and ctx.chat_request else "",
                user_id=getattr(ctx.chat_request, "user_id", "") if ctx and ctx.chat_request else "",
                payload=payload,
            )
            from nbot.hooks.async_utils import run_hook_coro
            run_hook_coro(self._hook_runtime.emit_event(event))
        except Exception:
            pass

    def _reset_hook_runtime_turn(self) -> None:
        hook_runtime = getattr(self, "_hook_runtime", None)
        if not hook_runtime or not hasattr(hook_runtime, "reset_turn"):
            return
        try:
            hook_runtime.reset_turn()
        except Exception as exc:
            _log.debug("[HookRuntime] reset_turn failed: %s", exc)

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

        # 获取工作区上下文，用于保存非媒体附件（文件、文档）
        ws_ctx = callbacks.get_workspace_context(ctx)

        progress.on_attachment_start(ctx, len(attachments))

        for att in attachments:
            att_type = str(att.get("type", "")).lower()
            att_name = str(att.get("name", att.get("filename", "")))
            resolved = callbacks.resolve_attachment_data(ctx, att)

            # 对于文件/文档类型，尝试保存到工作区并用本地路径解析
            if not resolved and ws_ctx and att_type not in ("image", "video", "audio"):
                resolved = self._resolve_via_workspace(att, ws_ctx)

            if att_type.startswith("image/") or self._looks_like_image(att):
                self._handle_image_attachment(ctx, progress, att, resolved)
            elif self._is_text_type(att_type, att_name):
                self._handle_text_attachment(ctx, progress, att, resolved)
            elif self._is_document_type(att_type, att_name):
                self._handle_document_attachment(ctx, progress, att, resolved)

        progress.on_attachments_done(ctx)

    @staticmethod
    def _resolve_via_workspace(
        attachment: Dict[str, Any],
        workspace_context: Dict[str, Any],
    ) -> Optional[Dict[str, Any]]:
        """将非媒体附件保存到工作区并返回 resolved dict。

        用于 QQ/Telegram/Feishu 等频道的文件/文档附件，
        当 callbacks.resolve_attachment_data() 返回 None 时的回退方案。
        """
        try:
            from nbot.core.message_middleware import MessagePreprocessor

            url = (
                attachment.get("url")
                or attachment.get("download_url")
                or attachment.get("path")
                or attachment.get("data")
            )
            if not url:
                return None

            ws_file = MessagePreprocessor._save_to_workspace(
                url, attachment, workspace_context
            )
            if not ws_file:
                return None

            result: Dict[str, Any] = {
                "type": attachment.get("type", ""),
                "name": attachment.get("name", "file"),
                "path": ws_file,
            }

            # 对文本文件，直接读取内容
            att_name = str(attachment.get("name", ""))
            if att_name.endswith((".txt", ".md", ".json", ".csv", ".xml", ".yaml", ".yml",
                                   ".py", ".js", ".ts", ".html", ".css", ".log", ".ini",
                                   ".conf", ".cfg", ".sh", ".bat", ".sql", ".toml")):
                try:
                    with open(ws_file, "r", encoding="utf-8", errors="replace") as f:
                        result["text_content"] = f.read(50000)
                except Exception:
                    pass

            return result
        except Exception as e:
            _log.debug(f"resolve_via_workspace failed: {e}")
            return None

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
        citations: List[Dict[str, Any]] = []
        # 通道若支持结构化引用，一次取回文本 + citations；否则退回纯文本检索
        with_citations = getattr(callbacks, "search_knowledge_with_citations", None)
        if callable(with_citations):
            try:
                outcome = with_citations(ctx, ctx.chat_request.content)
                if isinstance(outcome, tuple) and len(outcome) == 2:
                    ctx.knowledge_text, citations = outcome[0] or "", list(outcome[1] or [])
                else:
                    ctx.knowledge_text = outcome or ""
            except Exception as exc:  # 引用检索失败不阻断对话
                _log.debug("[Knowledge] 带引用检索失败，回退纯文本: %s", exc)
                ctx.knowledge_text = callbacks.search_knowledge(
                    ctx, ctx.chat_request.content
                )
        else:
            ctx.knowledge_text = callbacks.search_knowledge(
                ctx, ctx.chat_request.content
            )
        ctx.knowledge_retrieved = bool(ctx.knowledge_text)
        if citations:
            ctx.metadata["knowledge_citations"] = citations
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
        from nbot.character.prompt_stack import split_system_prompt

        # 加载消息历史（达到上下文阈值时由通道执行自动压缩）
        messages_raw = callbacks.load_messages(ctx)
        try:
            messages_raw = callbacks.auto_compress_context(
                ctx, messages_raw, int(max_context_chars * 0.8)
            )
        except Exception as exc:
            _log.warning("[AutoCompress] 自动压缩失败，按未压缩继续: %s", exc)
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

        # 分离原有 system prompt 和历史消息
        if ctx.metadata.get("group_id"):
            messages_for_ai = _annotate_group_message_senders(messages_for_ai)

        base_prompt, history_messages = split_system_prompt(messages_for_ai)

        # Prompt Manager preset（会话绑定才生效；未绑定返回 None，路径与改动前完全一致）
        from nbot.core.prompt_manager import apply_preset_base, resolve_session_preset

        prompt_preset = resolve_session_preset(
            getattr(ctx.chat_request, "conversation_id", "")
        )
        if prompt_preset is not None:
            base_prompt = apply_preset_base(prompt_preset, base_prompt)
            ctx.metadata["prompt_preset_id"] = prompt_preset.get("id", "")

        # 知识库注入 → PromptStack
        if ctx.knowledge_text:
            ctx.prompt_stack.add(
                "knowledge.rag",
                ctx.knowledge_text,
                priority=70,
            )

        # 跨会话角色记忆注入 → PromptStack（agent 模式跳过）
        if ctx.mode_policy.inject_character_memories_legacy:
            try:
                from nbot.core.auto_memory import (
                    build_memory_context,
                    load_character_memories,
                )

                memory_context = build_memory_context(ctx, callbacks)
                memory_text = load_character_memories(
                    memory_context.get("character_name", ""),
                    memory_context.get("target_id", ""),
                )
                if memory_text:
                    ctx.prompt_stack.add(
                        "character.memories_legacy",
                        memory_text,
                        priority=60,
                    )
            except Exception:
                pass

        # MemoryFS 结构化记忆直接注入（独立于角色运行时，agent 模式跳过）
        if ctx.mode_policy.inject_memory_fs:
            self._inject_memory_fs_direct(ctx, callbacks)

        # 角色运行时 before_turn hook
        self._phase_character_runtime_before_turn(ctx, callbacks)
        self._phase_real_time_prompt_fallback(ctx, messages_raw)
        if ctx.character_turn and getattr(ctx.character_turn, "memories", None):
            ctx.prompt_stack.remove("character.memories_legacy")

        # 应用会话级提示词栈禁用列表
        disabled_keys = set(ctx.metadata.get("disabled_prompt_keys", []))
        if disabled_keys:
            for item in ctx.prompt_stack.items:
                if item.key in disabled_keys:
                    item.enabled = False

        # 注入用户自定义提示词（从会话数据中读取，按 order 排序后逐条加入 PromptStack）
        custom_prompts = ctx.metadata.get("custom_prompts", [])
        if isinstance(custom_prompts, list) and custom_prompts:
            sorted_prompts = sorted(custom_prompts, key=lambda x: x.get("order", 0))
            for cp in sorted_prompts:
                content = (cp.get("content") or "").strip()
                if not content:
                    continue
                ctx.prompt_stack.add(
                    key=_custom_prompt_stack_key(cp),
                    content=content,
                    priority=35,  # 在角色卡(30)之后、角色状态(40)之前
                    scope="session",
                )

        # Agent 持久目标 / 规格 / 工作区 AGENTS.md 注入（/goal、/spec 命令设置）
        agent_goal = str(ctx.metadata.get("agent_goal") or "").strip()
        agent_spec = str(ctx.metadata.get("agent_spec") or "").strip()
        if agent_goal:
            ctx.prompt_stack.add(
                "agent.goal",
                f"当前会话的持久目标（每轮检查进度并主动推进）：\n{agent_goal[:1000]}",
                priority=22,
            )
        if agent_spec:
            ctx.prompt_stack.add(
                "agent.spec",
                f"会话规格说明（必须遵守，与用户冲突时先确认）：\n{agent_spec[:4000]}",
                priority=21,
            )
        agents_md_text = self._read_workspace_agents_md(ctx, callbacks)
        if agents_md_text:
            ctx.prompt_stack.add(
                "agent.workspace_instructions",
                agents_md_text,
                priority=23,
            )

        # 角色生图能力说明（仅在非 agent 模式、且功能开启时注入）
        if ctx.mode_policy.inject_image_capability:
            try:
                from nbot.services.image_service import (
                    build_image_capability_injection,
                )

                build_image_capability_injection(ctx.prompt_stack)
            except Exception as exc:
                _log.debug("[ImageGen] 注入生图能力说明失败: %s", exc)

        # PromptStack 合成最终 system prompt
        self._emit_hook("prompt.before_render", ctx)
        composed_system = ctx.prompt_stack.render(base_prompt)
        self._emit_hook("prompt.after_render", ctx)
        # 宏展开（{{user}}/{{char}}/{{time}}/{{random}}/{{getvar}} 等）
        # 只展开送给模型的那份，存储的 composed_system_prompt 保留模板形态，
        # 避免把 {{time}}/{{user}} 之类冻结成第一轮的值。
        rendered_system = self._expand_prompt_macros(ctx, composed_system)
        messages_for_ai = [
            {"role": "system", "content": rendered_system},
            *history_messages,
        ]

        # 调试快照先落库，后面的深度注入（世界书 / Author's Note）再追加
        ctx.metadata["prompt_stack_debug"] = ctx.prompt_stack.render_debug()

        # 用户人设（persona）：追加到 system prompt 末尾
        messages_for_ai = self._apply_user_persona(ctx, messages_for_ai)

        # 世界书 depth 类条目按位置插入消息序列
        messages_for_ai = self._apply_world_book_depth_positions(ctx, messages_for_ai)
        # Author's Note：按深度插入的会话级附注
        messages_for_ai = self._apply_authors_note(ctx, messages_for_ai)
        # Prompt Manager preset：非 system 段落与 depth 段落插入消息序列
        messages_for_ai = self._apply_prompt_preset(ctx, prompt_preset, messages_for_ai)

        # 将合成后的 system prompt 存入 metadata，供 on_response_complete 回写
        ctx.metadata["composed_system_prompt"] = composed_system

        # 调试日志
        _log.debug(
            "[PromptStack] 本轮注入 keys: %s",
            ctx.prompt_stack.keys,
        )

        # 调用现有的上下文准备
        prepared = prepare_chat_context(
            messages_for_ai,
            user_content,
            knowledge_text="",
            max_total_chars=max_context_chars,
        )
        ctx.messages = prepared.messages
        ctx.tool_call_history = prepared.tool_call_history

    def _macro_context(self, ctx):
        """构造本轮宏上下文（用户名/角色名/会话变量/稳定种子）。"""
        from nbot.core.macros import get_variable_store

        meta = ctx.metadata or {}
        request = ctx.chat_request
        user_name = str(
            meta.get("macro_user_name")
            or getattr(request, "user_id", "")
            or getattr(request, "sender", "")
            or ""
        ).strip()
        turn = ctx.character_turn
        profile_name = ""
        if turn is not None:
            profile_name = str(getattr(getattr(turn, "profile", None), "name", "") or "")
        char_name = str(
            meta.get("macro_char_name")
            or meta.get("group_speaker_name")
            or profile_name
            or ""
        ).strip()

        scope_id = str(getattr(request, "conversation_id", "") or "")
        variables = meta.get("macro_variables")
        store = None
        if not isinstance(variables, dict):
            try:
                store = get_variable_store()
                variables = store.get(scope_id)
            except Exception as exc:  # pragma: no cover - 存储异常不影响主流程
                _log.debug("[Macros] 读取会话变量失败: %s", exc)
                variables = {}
        seed = f"{scope_id}|{meta.get('group_speaker', '')}|{getattr(request, 'parent_message_id', '') or ''}"

        from nbot.core.macros import MacroContext

        context = MacroContext(
            user_name=user_name,
            char_name=char_name,
            variables=variables,
            seed=str(seed),
        )
        return context, store, scope_id, dict(variables)

    def _expand_prompt_macros(self, ctx, text: str) -> str:
        """展开提示词宏；变量有变化时回写会话级存储。"""
        from nbot.core.macros import expand, has_macros

        if not text or not has_macros(text):
            return text
        try:
            context, store, scope_id, before = self._macro_context(ctx)
            expanded = expand(text, context)
            if store is not None and context.variables != before:
                store.save(scope_id, context.variables)
                ctx.metadata["macro_variables"] = context.variables
            return expanded
        except Exception as exc:  # pragma: no cover - 宏异常不阻断对话
            _log.warning("[Macros] 展开失败，按原文继续: %s", exc)
            return text

    def _apply_user_persona(self, ctx, messages_for_ai):
        """用户人设（persona）：把「我是谁」的说明追加到 system prompt 末尾。

        只影响送给模型的副本，不改角色卡，也不回写存储的 system prompt。
        通道没有显式注入时，这里按会话记录兜底解析（QQ/TG/飞书 与 Web 一致）。
        """
        persona = (ctx.metadata or {}).get("user_persona")
        if not persona:
            try:
                persona = self._resolve_user_persona(ctx)
            except Exception as exc:  # 兜底解析失败只降级，不阻断对话
                _log.debug("[Persona] 兜底解析异常，跳过注入: %s", exc)
                persona = None
        if not persona:
            return messages_for_ai

        from nbot.core.personas import build_persona_block

        block = build_persona_block(persona)
        if not block:
            return messages_for_ai
        block = self._expand_prompt_macros(ctx, block)

        messages = list(messages_for_ai)
        if messages and messages[0].get("role") == "system":
            system_content = str(messages[0].get("content") or "")
            messages[0] = {
                **messages[0],
                "content": f"{system_content}\n\n{block}".strip() if system_content else block,
            }
        else:
            messages.insert(0, {"role": "system", "content": block})

        ctx.metadata.setdefault("prompt_stack_debug", []).append({
            "key": "user_persona",
            "role": "system",
            "depth": 0,
            "chars": len(block),
            "preview": block[:120],
        })
        return messages

    def _resolve_user_persona(self, ctx):
        """通道未注入人设时按会话记录兜底解析；失败返回 None（不阻断对话）。"""
        metadata = ctx.metadata if isinstance(ctx.metadata, dict) else {}
        try:
            from nbot.core.agent_service import apply_user_persona_metadata

            if apply_user_persona_metadata(ctx.chat_request, metadata):
                return metadata.get("user_persona")
        except Exception as exc:
            _log.debug("[Persona] 兜底解析失败，跳过注入: %s", exc)
        return None

    def _apply_authors_note(self, ctx, messages_for_ai):
        """Author's Note：按配置深度插入会话级附注。"""
        note = (ctx.metadata or {}).get("authors_note")
        if not note:
            return messages_for_ai

        from nbot.core.authors_note import build_insertion, insertion_index

        insertion = build_insertion(
            note,
            expander=lambda text: self._expand_prompt_macros(ctx, text),
        )
        if not insertion:
            return messages_for_ai

        depth, role, content = insertion
        messages = list(messages_for_ai)
        index = insertion_index(len(messages), depth)
        messages.insert(index, {"role": role, "content": content})
        ctx.metadata.setdefault("prompt_stack_debug", []).append({
            "key": f"authors_note@{depth}",
            "content": content[:200] + "..." if len(content) > 200 else content,
            "priority": 66,
            "role": role,
            "scope": "session",
            "enabled": True,
            "position": f"depth:{depth}",
        })
        _log.debug("[AuthorsNote] inserted at depth %s (role=%s, %s chars)", depth, role, len(content))
        return messages

    def _apply_prompt_preset(self, ctx, preset, messages_for_ai):
        """Prompt Manager preset：非 system 段落与 depth 段落插入消息序列。

        preset 为 None（会话未绑定）时原样返回，保证旧路径行为不变。
        """
        if not preset:
            return messages_for_ai

        from nbot.core.prompt_manager import (
            build_preset_injection,
            insert_preset_messages,
        )

        injection = build_preset_injection(
            preset,
            expander=lambda text: self._expand_prompt_macros(ctx, text),
            expand_system=False,
        )
        if injection.get("empty", True):
            return messages_for_ai

        messages = insert_preset_messages(injection, messages_for_ai)
        debug = ctx.metadata.setdefault("prompt_stack_debug", [])
        for entry in injection.get("debug", []):
            debug.append({
                "key": entry["key"],
                "content": entry.get("preview", ""),
                "priority": 36,
                "role": entry.get("role", "system"),
                "scope": "session",
                "enabled": True,
                "position": entry.get("position", "end"),
            })
        return messages

    def _apply_world_book_depth_positions(self, ctx, messages_for_ai):
        """世界书 depth 类条目按位置插入消息序列。

        - at_depth：按 entry.depth 插入距对话末尾第 N 条处
        - before_an/after_an：锚点深度（默认 4），前/后顺序保持
        使用 user 角色承载：Anthropic/Gemini 协议会把 system 消息折叠到顶部，
        破坏深度定位（对齐 SillyTavern 对 Claude 的深度注入方式）。
        """
        turn = ctx.character_turn
        entries = list(getattr(turn, "world_book_entries", []) or []) if turn else []
        if not entries:
            return messages_for_ai

        try:
            from nbot.character.world_book_injector import build_depth_insertions

            insertions = build_depth_insertions(entries)
        except Exception as exc:
            _log.debug("[WorldBook] depth insertion build failed: %s", exc)
            return messages_for_ai
        if not insertions:
            return messages_for_ai

        messages = list(messages_for_ai)
        for depth, content in insertions:
            # depth=0 → 对话最末尾；depth=N → 距末尾第 N 条之前（不早于 system 之后）
            index = max(1, len(messages) - depth)
            content = self._expand_prompt_macros(ctx, content)
            messages.insert(index, {"role": "user", "content": content})
            ctx.metadata.setdefault("prompt_stack_debug", []).append({
                "key": f"world_book.at_depth@{depth}",
                "content": content[:200] + "..." if len(content) > 200 else content,
                "priority": 65,
                "role": "user",
                "scope": "turn",
                "enabled": True,
                "position": f"depth:{depth}",
            })
        return messages


    # ------------------------------------------------------------------
    # Group Chat Phases
    # ------------------------------------------------------------------

    def _read_workspace_agents_md(self, ctx, callbacks) -> str:
        """读取会话工作区中的 AGENTS.md，存在则注入每轮提示词（上限 4000 字符）。"""
        try:
            from nbot.core.workspace import workspace_manager

            ws_ctx = {}
            if hasattr(callbacks, "get_workspace_context"):
                ws_ctx = callbacks.get_workspace_context(ctx) or {}
            session_id = (
                str(ws_ctx.get("session_id") or "")
                or str(getattr(ctx.chat_request, "session_id", "") or "")
            )
            if not session_id:
                return ""
            session_type = str(ws_ctx.get("session_type") or "")
            try:
                root = workspace_manager.get_or_create(session_id, session_type)
            except TypeError:
                root = workspace_manager.get_or_create(session_id, session_type or "web")
            if not root:
                return ""
            import os as _os

            for name in ("AGENTS.md", "agents.md"):
                path = _os.path.join(root, name)
                if _os.path.exists(path):
                    try:
                        with open(path, encoding="utf-8") as f:
                            text = f.read().strip()
                        if text:
                            return (
                                "以下是本会话工作区的 AGENTS.md 工作约定（每轮生效）：\n"
                                + text[:4000]
                            )
                    except OSError:
                        continue
        except Exception as exc:
            _log.debug("[Agent] 读取工作区 AGENTS.md 失败: %s", exc)
        return ""

    def _phase_group_speaker_select(self, ctx, group_context):
        """Select the speaking character for group chat."""
        try:
            from nbot.group.scheduler import SpeakerScheduler
            scheduler = group_context.get("scheduler") or SpeakerScheduler.instance()
            conversation = group_context["group"]
            message = ctx.chat_request.content or ""
            character_ids = conversation.character_ids
            last_speaker = conversation.active_speaker

            preset_speaker = str(ctx.metadata.get("group_speaker") or "")
            if preset_speaker and preset_speaker in character_ids:
                speaker = preset_speaker
            else:
                speaker = scheduler.decide_next_speaker(
                    conversation, message, character_ids,
                    last_speaker=last_speaker,
                    group_context=group_context,
                )
            conversation.active_speaker = speaker
            ctx.metadata["group_speaker"] = speaker
            ctx.metadata["group_id"] = conversation.group_id

            # Build character name mapping
            profiles = group_context.get("character_profiles", {})
            speaker_name = profiles.get(speaker, {}).get("name", speaker) if speaker else ""
            ctx.metadata["group_speaker_name"] = speaker_name

            _log.info("group %s: speaker selected = %s (%s)", conversation.group_id, speaker, speaker_name)

            from nbot.events import names as _E
            self._emit_hook(_E.GROUP_MESSAGE_RECEIVED, ctx, extra_payload={
                "group_id": conversation.group_id,
                "message": message[:200],
            })
            self._emit_hook(_E.GROUP_SPEAKER_SELECTED, ctx, extra_payload={
                "group_id": conversation.group_id,
                "speaker_id": speaker,
                "speaker_name": speaker_name,
                "strategy": conversation.config.speaker_strategy,
            })
        except Exception as e:
            _log.error("group speaker select failed: %s", e)

    def _phase_group_prompt_build(self, ctx, group_context):
        """Build group chat system prompt and inject into prompt_stack."""
        try:
            from nbot.group.scheduler import SpeakerScheduler
            scheduler = group_context.get("scheduler") or SpeakerScheduler.instance()
            conversation = group_context["group"]
            profiles = group_context.get("character_profiles", {})
            speaker_id = ctx.metadata.get("group_speaker", "")

            if not speaker_id:
                return

            # 加载当前发言角色的完整角色卡
            full_profile = None
            try:
                from nbot.character.repository import ProfileRepository
                import os
                base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
                repo = ProfileRepository(base_dir)
                full_profile = repo.get(speaker_id)
            except Exception as e:
                _log.debug("group prompt: failed to load full profile for %s: %s", speaker_id, e)

            # 如果 ProfileRepository 找不到，尝试从 custom_presets_map 中查找
            if not full_profile:
                custom_presets_map = group_context.get("custom_presets_map", {})
                preset_data = custom_presets_map.get(speaker_id)
                if preset_data:
                    from nbot.character.models import CharacterProfile
                    full_profile = CharacterProfile.from_personality_dict(preset_data)
                    _log.info("group prompt: loaded full profile for '%s' from custom_presets_map", speaker_id)

            group_prompt = scheduler.build_group_system_prompt(
                conversation, profiles, speaker_id,
                full_profile=full_profile,
            )

            # Inject into prompt_stack
            ctx.prompt_stack.add(
                "group.scene",
                group_prompt,
                priority=85,
            )

            # Inject relation matrix as context
            relations = conversation.get_relation_matrix()
            if relations:
                import json
                ctx.prompt_stack.add(
                    "group.relations",
                    "角色间关系数据:" + json.dumps(relations, ensure_ascii=False, indent=2),
                    priority=60,
                )

            _log.info("group %s: prompt built for speaker %s", conversation.group_id, speaker_id)
        except Exception as e:
            _log.error("group prompt build failed: %s", e)

    def _phase_group_narrator(self, ctx, result, group_context):
        """Generate narration if conditions are met."""
        try:
            from nbot.group.narrator import NarratorCharacter
            narrator = group_context.get("narrator") or NarratorCharacter.instance()
            conversation = group_context["group"]

            if not narrator.should_narrate(
                trigger="interval",
                turn_count=conversation.turn_count,
                narrate_interval=conversation.config.narrate_interval,
            ):
                return

            profiles = group_context.get("character_profiles", {})
            char_names = [profiles.get(c, {}).get("name", c) for c in conversation.character_ids]
            scene_ctx = narrator.build_scene_context(char_names)
            recent = narrator.build_recent_summary(group_context.get("recent_messages", []))

            narrate_prompt = narrator.build_narrate_prompt(
                trigger="interval",
                scene_context=scene_ctx,
                recent_summary=recent,
            )

            # Store narrator prompt for async generation
            if result.metadata is None:
                result.metadata = {}
            result.metadata["narrate_prompt"] = narrate_prompt
            result.metadata["narrate_pending"] = True
            narrator.mark_narrated()
            conversation.advance_turn()

            _log.info("group %s: narration triggered at turn %d", conversation.group_id, conversation.turn_count)

            from nbot.events import names as _E
            self._emit_hook(_E.GROUP_NARRATION_REQUESTED, ctx, extra_payload={
                "group_id": conversation.group_id,
                "trigger": "interval",
                "turn_count": conversation.turn_count,
            })
        except Exception as e:
            _log.error("group narrator failed: %s", e)

    def _generate_plot_choices_if_enabled(self, ctx, result, callbacks=None):
        """Generate plot choices if plot mode is enabled for this session."""
        if not ctx or not ctx.chat_request:
            _log.debug("[PlotMode] skipped: no ctx or chat_request")
            return
        try:
            conversation_id = ctx.chat_request.conversation_id
            if not conversation_id:
                _log.debug("[PlotMode] skipped: no conversation_id")
                return
            from nbot.plot.choice_generator import PlotChoiceGenerator

            # Check session for plot_mode flag
            metadata = getattr(ctx.chat_request, "metadata", {}) or {}
            session_plot = metadata.get("plot_mode", False)
            if not session_plot:
                return

            generator = PlotChoiceGenerator()
            turn_context = ctx.character_turn

            self._run_plot_choice_generation(
                generator, result, turn_context, conversation_id, ctx, callbacks
            )
        except Exception as e:
            _log.debug("[AIPipeline] plot choice generation skipped: %s", e)

    def _run_plot_choice_generation(
        self,
        generator,
        result,
        turn_context,
        conversation_id,
        ctx,
        callbacks=None,
    ):
        """Schedule plot choice generation without blocking the main reply."""
        import asyncio

        result.metadata["plot_mode"] = True
        result.metadata["plot_choices_pending"] = True

        def run_plot_generation() -> None:
            try:
                asyncio.run(
                    self._do_generate_plot_choices(
                        generator, result, turn_context, conversation_id, ctx
                    )
                )
                if callbacks and result.metadata.get("plot_choices"):
                    callbacks.on_plot_choices_ready(ctx, result)
            finally:
                result.metadata["plot_choices_pending"] = False

        submit_background_task(
            "plot_choice_generation",
            run_plot_generation,
            serial_key=f"plot:{conversation_id}",
        )

    def _build_plot_runtime_snapshots(self, turn_context):
        """从 turn_context 提取角色运行时状态/关系快照，供分支切换与回溯定位。

        返回 (state_snapshot, relationship_snapshot)；缺失时返回空 dict。
        """
        state_snapshot: dict = {}
        relationship_snapshot: dict = {}
        try:
            state = getattr(turn_context, "state", None)
            if state is not None:
                state_snapshot = {
                    "mood": getattr(state, "mood", ""),
                    "mood_intensity": getattr(state, "mood_intensity", 0.5),
                    "energy": getattr(state, "energy", 70),
                }
            rel = getattr(turn_context, "relationship", None)
            if rel is not None:
                relationship_snapshot = {
                    "affection": getattr(rel, "affection", 50),
                    "trust": getattr(rel, "trust", 50),
                    "familiarity": getattr(rel, "familiarity", 30),
                    "dependency": getattr(rel, "dependency", 30),
                    "security": getattr(rel, "security", 50),
                    "jealousy": getattr(rel, "jealousy", 0),
                }
        except Exception as e:
            _log.debug("[PlotMode] runtime snapshot build skipped: %s", e)
        return state_snapshot, relationship_snapshot

    @staticmethod
    def _resolve_plot_node_level(graph_mgr, choice_id: str) -> str:
        """节点级别继承"到达本节点所选选项"的级别。

        手动回复 / 根节点（无 choice_id）或选项缺失时回退 normal；
        hidden 级别按 important 展示（hidden 仅用于选项可见性，节点不该是隐藏态）。
        """
        if not choice_id:
            return "normal"
        try:
            choice = graph_mgr.get_choice(choice_id)
        except Exception:
            choice = None
        level = getattr(choice, "level", "") or "normal"
        return "important" if level == "hidden" else level

    @staticmethod
    def _extract_plot_recent_history(ctx) -> List[Dict[str, Any]]:
        """提取最近几轮 user/assistant 对话，供剧情选项生成避免重复并取材。

        从 ctx.messages 取最后若干条，剥离 system，仅保留 role/content。
        """
        try:
            messages = getattr(ctx, "messages", None) or []
        except Exception:
            return []
        history: List[Dict[str, Any]] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            role = msg.get("role", "")
            if role not in ("user", "assistant"):
                continue
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            history.append({"role": role, "content": content})
        return history[-8:]

    async def _do_generate_plot_choices(self, generator, result, turn_context, conversation_id, ctx):
        """Async plot choice generation."""
        try:
            response_text = result.final_content or ""
            recent_history = self._extract_plot_recent_history(ctx)
            metadata = getattr(ctx.chat_request, "metadata", {}) or {}
            is_silent_proactive = bool(
                metadata.get("is_proactive_chat") and metadata.get("silent_trigger")
            )
            if is_silent_proactive and recent_history:
                trigger_content = getattr(ctx.chat_request, "content", "") or ""
                last_message = recent_history[-1]
                if (
                    last_message.get("role") == "user"
                    and last_message.get("content") == trigger_content
                ):
                    # 剧情选项可以根据主动回复生成，但不应把内部触发词
                    # 当作故事中的用户发言。
                    recent_history = recent_history[:-1]
            plot_choice_style = str(metadata.get("plot_choice_style") or "")
            plot_outline = str(metadata.get("plot_outline") or "")
            choices = await generator.generate(
                response_text,
                _plot_turn_context_dict(turn_context),
                recent_history=recent_history,
                session_id=conversation_id or "",
                style=plot_choice_style,
                outline=plot_outline,
            )
            if not choices:
                return

            # Attach to result metadata
            result.metadata["plot_choices"] = choices

            # Create plot node for important+ choices
            from nbot.plot.graph_manager import get_plot_graph_manager
            from nbot.plot.models import PlotNode, PlotChoice as PlotChoiceModel

            graph_mgr = get_plot_graph_manager()
            char_id = ""
            if turn_context and hasattr(turn_context, 'profile'):
                char_id = turn_context.profile.id

            # 读取分支元数据：若本轮是从某节点的某个选择"创建分支"触发，
            # 则父节点用 branch_from_node，并对该 choice 建边。
            req_meta = getattr(ctx.chat_request, "metadata", {}) or {}
            branch_from_node_id = req_meta.get("plot_branch_from_node_id", "")
            branch_choice_id = req_meta.get("plot_branch_choice_id", "")

            # 构造本轮消息快照（用于会话内分支物化）
            user_content = getattr(ctx.chat_request, "content", "") or ""
            user_snapshot = (
                {}
                if is_silent_proactive
                else {"role": "user", "content": user_content}
            )
            assistant_snapshot = {"role": "assistant", "content": response_text}
            if is_silent_proactive:
                assistant_snapshot.update(
                    {
                        "source": "proactive_chat",
                        "is_proactive_chat": True,
                    }
                )
            # 记录助手消息真实 id：分支切换/回溯后仍能把后到的 TTS 音频
            # 回填到本节点快照（TTS 是异步的，建节点时往往还没有 audio_url）。
            am_obj = getattr(result, "assistant_message", None)
            if isinstance(am_obj, dict):
                if am_obj.get("id"):
                    assistant_snapshot["id"] = am_obj["id"]
                if am_obj.get("audio_url"):
                    assistant_snapshot["audio_url"] = am_obj["audio_url"]

            # 捕获本轮结束后的角色运行时状态快照（用于切换/回溯时定位状态）
            state_snapshot, relationship_snapshot = self._build_plot_runtime_snapshots(turn_context)

            if branch_from_node_id and branch_choice_id:
                # 分支模式：父节点 = branch_from_node_id
                # 节点级别继承"到达本节点所选选项"的级别
                node_level = self._resolve_plot_node_level(graph_mgr, branch_choice_id)
                node = PlotNode(
                    conversation_id=conversation_id,
                    character_id=char_id,
                    title=response_text[:30] + "..." if len(response_text) > 30 else response_text,
                    summary=response_text[:100],
                    level=node_level,
                    parent_node_id=branch_from_node_id,
                    user_message=user_snapshot,
                    assistant_message=assistant_snapshot,
                    state_snapshot=state_snapshot,
                    relationship_snapshot=relationship_snapshot,
                )
                graph_mgr.branch_from(branch_choice_id, node)
            else:
                # 正常模式：沿"当前激活节点"延伸（切换/回溯后能正确续接），
                # 回退到最新节点。
                active_id = graph_mgr.get_active_node_id(conversation_id)
                prev_node = graph_mgr.get_node(active_id) if active_id else None
                if prev_node is None:
                    prev_node = graph_mgr.get_latest_node(conversation_id)
                pending_choice_id = ""
                if prev_node and getattr(prev_node, "selected_choice_id", ""):
                    pending_choice_id = prev_node.selected_choice_id

                # 节点级别继承"到达本节点所选选项"的级别（手动回复/根节点回退 normal）
                node_level = self._resolve_plot_node_level(graph_mgr, pending_choice_id)
                node = PlotNode(
                    conversation_id=conversation_id,
                    character_id=char_id,
                    title=response_text[:30] + "..." if len(response_text) > 30 else response_text,
                    summary=response_text[:100],
                    level=node_level,
                    parent_node_id=prev_node.id if prev_node else "",
                    user_message=user_snapshot,
                    assistant_message=assistant_snapshot,
                    state_snapshot=state_snapshot,
                    relationship_snapshot=relationship_snapshot,
                )
                graph_mgr.add_node(node)

                # 与父节点建边（优先用 pending 选择，否则建无选择的延续边）
                if prev_node:
                    existing_edges = graph_mgr.get_graph(conversation_id).get("edges", [])
                    if pending_choice_id:
                        if not any(e.get("choice_id") == pending_choice_id for e in existing_edges):
                            graph_mgr.create_edge_for_choice(pending_choice_id, node.id)
                    elif not any(
                        e.get("from_node_id") == prev_node.id and e.get("to_node_id") == node.id
                        for e in existing_edges
                    ):
                        from nbot.plot.models import PlotEdge
                        graph_mgr.add_edge(PlotEdge(
                            from_node_id=prev_node.id, to_node_id=node.id, choice_id="",
                        ))

            # 更新激活节点为本轮新节点（单一真相来源）
            graph_mgr.set_active_node(conversation_id, node.id)

            # Create choice entries
            for choice_data in choices:
                level = choice_data.get("level", "normal")
                pc = PlotChoiceModel(
                    node_id=node.id,
                    text=choice_data.get("text", ""),
                    level=level,
                    intent=choice_data.get("intent", ""),
                )
                graph_mgr.add_choice(pc)
                choice_data["id"] = pc.id

            _log.debug("[AIPipeline] generated %d plot choices", len(choices))

            # Trigger multimedia effects for the highest-level choice
            try:
                from nbot.plot.multimedia_bridge import MultimediaBridge
                mm = MultimediaBridge.instance()
                max_level = "normal"
                for cd in choices:
                    lv = cd.get("level", "normal")
                    if _level_rank(lv) > _level_rank(max_level):
                        max_level = lv
                # Build a mock choice object for the bridge
                mock_choice = type("C", (), {"level": max_level, "text": response_text[:80]})()
                mm_ctx = {
                    "mood": ctx.metadata.get("mood", "calm"),
                    "reply_text": response_text[:200],
                    "location": ctx.metadata.get("location", ""),
                }
                mm_actions = mm.on_plot_choice(mock_choice, mm_ctx)
                if mm_actions:
                    result.metadata["multimedia_actions"] = mm_actions
            except Exception as mm_e:
                _log.debug("[AIPipeline] multimedia trigger skipped: %s", mm_e)
        except Exception as e:
            _log.error("[PlotMode] choice generation failed: %s", e)

    # ------------------------------------------------------------------
    # 角色运行时 hooks
    # ------------------------------------------------------------------

    def _phase_real_time_prompt_fallback(
        self,
        ctx: PipelineContext,
        messages_raw: list[dict[str, Any]],
    ) -> None:
        """Inject real-time continuity if CharacterRuntime did not add it."""
        if not ctx.mode_policy.inject_real_time_prompt_fallback:
            return
        if ctx.prompt_stack.get("real_time.continuity"):
            return
        try:
            from nbot.character.prompt_stack import PromptStack
            from nbot.review.models import ReviewInput
            from nbot.review.rule_review import build_offline_plot_update
            from nbot.review.time_context import (
                build_current_real_time_context,
                format_real_time_prompt_context,
            )

            previous_turn_time = self._extract_previous_message_time(
                messages_raw,
                getattr(ctx.chat_request, "content", "") or "",
            )
            real_time_context = build_current_real_time_context(previous_turn_time)
            ctx.metadata["real_time_context"] = real_time_context
            request_metadata = getattr(ctx.chat_request, "metadata", None)
            if not isinstance(request_metadata, dict):
                request_metadata = {}
                ctx.chat_request.metadata = request_metadata
            request_metadata["real_time_context"] = real_time_context

            ctx.prompt_stack.add(
                key="real_time.continuity",
                content=format_real_time_prompt_context(real_time_context),
                priority=PromptStack.PRIORITY_CHARACTER_STATE + 1,
                scope="turn",
            )

            if (
                ctx.metadata.get("plot_mode")
                and ctx.metadata.get("plot_real_time_sync")
                and not ctx.prompt_stack.get("plot.real_time_sync")
            ):
                update = build_offline_plot_update(
                    ReviewInput(
                        conversation_id=getattr(ctx.chat_request, "conversation_id", "") or "",
                        character_id=str(ctx.metadata.get("character_id") or ""),
                        user_id=getattr(ctx.chat_request, "user_id", "") or "",
                        user_message=getattr(ctx.chat_request, "content", "") or "",
                        real_time_context=real_time_context,
                        plot_mode=True,
                        plot_real_time_sync=True,
                    )
                )
                if update and update.should_inject:
                    ctx.prompt_stack.add(
                        key="plot.real_time_sync",
                        content=update.prompt_text or update.summary,
                        priority=PromptStack.PRIORITY_REACTION_PLAN + 1,
                        scope="turn",
                    )
        except Exception as exc:
            _log.warning("[AIPipeline] real-time prompt fallback failed: %s", exc, exc_info=True)

    @staticmethod
    def _extract_previous_message_time(
        messages: list[dict[str, Any]],
        current_content: str,
    ) -> str:
        history = list(messages or [])
        if history:
            last = history[-1]
            if (
                isinstance(last, dict)
                and last.get("role") == "user"
                and str(last.get("content") or "") == current_content
            ):
                history = history[:-1]
        for message in reversed(history):
            if not isinstance(message, dict):
                continue
            timestamp = (
                message.get("timestamp")
                or message.get("created_at")
                or message.get("time")
            )
            if timestamp:
                return str(timestamp)
        return ""

    def _phase_character_runtime_before_turn(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
    ) -> None:
        """角色运行时 before_turn：读取状态、生成 ReactionPlan、注册 PromptStack 注入项"""
        request_metadata = getattr(ctx.chat_request, "metadata", None)
        if not isinstance(request_metadata, dict):
            request_metadata = {}
            ctx.chat_request.metadata = request_metadata
        request_metadata.update(ctx.metadata)

        runtime = callbacks.get_character_runtime(ctx)
        identity = callbacks.get_character_context(ctx)

        if not runtime:
            _log.debug("[CharacterRuntime] before_turn skipped: runtime is None")
            return
        self._attach_hook_runtime_to_character_runtime(runtime)
        if not identity:
            _log.debug("[CharacterRuntime] before_turn skipped: identity is None")
            return

        # --- 通过 dispatcher 统一处理 is_enabled / trigger / scope_id ---
        # source = 频道标识 (qq/telegram/feishu/web)
        # channel_type = 场景标识 (private/group/web/telegram/feishu)
        channel = (
            ctx.metadata.get("source", "")
            or getattr(ctx.chat_request, "channel", "")
            or ctx.metadata.get("channel_type", "")
        )
        try:
            from nbot.character.dispatcher import CharacterRuntimeContextDispatcher, build_scope_id
            from nbot.character.channel_context import ChannelRuntimeContext
            from nbot.web.utils.config_loader import get_character_runtime_config

            config = get_character_runtime_config()
            dispatcher = CharacterRuntimeContextDispatcher(runtime=runtime, config=config)

            # 优先使用 adapter.build_runtime_context()，fallback 手动构造
            req_meta = getattr(ctx.chat_request, "metadata", {}) or {}
            if ctx.adapter and hasattr(ctx.adapter, "build_runtime_context"):
                try:
                    runtime_ctx = ctx.adapter.build_runtime_context(ctx.chat_request)
                except Exception:
                    runtime_ctx = None
            else:
                runtime_ctx = None

            if runtime_ctx is None:
                # fallback: 手动构造
                scene_raw = ctx.metadata.get("channel_type", "")
                if scene_raw in ("private", "group"):
                    scene = scene_raw
                else:
                    scene = "private" if not ctx.metadata.get("group_id") else "group"
                runtime_ctx = ChannelRuntimeContext(
                    channel=channel,
                    conversation_id=getattr(ctx.chat_request, "conversation_id", "") or "",
                    scene=scene,
                    user_id=getattr(ctx.chat_request, "user_id", "") or "",
                    group_id=ctx.metadata.get("group_id", ""),
                    thread_id=ctx.metadata.get("thread_id", ""),
                )

            # is_enabled 检查
            if not dispatcher.is_enabled(runtime_ctx):
                _log.debug("[CharacterRuntime] before_turn skipped: channel %s disabled", channel)
                return

            # trigger 检查
            trigger = dispatcher.get_trigger_strategy(runtime_ctx)
            if not dispatcher._should_trigger(trigger, runtime_ctx, ctx.chat_request):
                _log.debug("[CharacterRuntime] before_turn skipped: trigger %s not met", trigger)
                return

            # scope_id 修正
            memory_scope = dispatcher.get_memory_scope(runtime_ctx)
            # 私聊场景下 group/group_user scope 无意义，强制降级为 user
            if memory_scope in ("group", "group_user") and runtime_ctx.scene == "private":
                memory_scope = "user"
            if memory_scope:
                corrected_scope_id = build_scope_id(runtime_ctx, memory_scope)
                if corrected_scope_id and corrected_scope_id != identity.scope_id:
                    _log.debug(
                        "[CharacterRuntime] scope_id corrected: %s -> %s",
                        identity.scope_id, corrected_scope_id,
                    )
                    identity.scope_id = corrected_scope_id
        except Exception as cfg_exc:
            _log.debug("[CharacterRuntime] config check failed, proceeding without: %s", cfg_exc)
        # --- dispatcher 集成结束 ---

        try:
            # 加载最近消息用于世界书多源召回
            recent_messages = []
            try:
                recent_messages = callbacks.load_messages(ctx) or []
            except Exception:
                pass

            turn = runtime.before_turn(ctx.chat_request, identity, recent_messages=recent_messages)
            ctx.character_turn = turn
            request_metadata = getattr(ctx.chat_request, "metadata", {}) or {}
            ctx.metadata.update(request_metadata)
            if request_metadata.get("real_time_context"):
                ctx.metadata["real_time_context"] = request_metadata.get("real_time_context")

            from nbot.character.prompt_builder import build_character_injections

            build_character_injections(
                ctx.prompt_stack,
                profile=turn.profile,
                state=turn.state,
                relationship=turn.relationship,
                memories=turn.memories,
                plan=turn.plan,
            )

            # 注入世界书（depth 类条目由消息序列插入阶段处理）
            if turn.world_book_entries:
                from nbot.character.world_book_injector import inject_world_book
                inject_world_book(
                    ctx.prompt_stack,
                    turn.world_book_entries,
                    include_depth_entries=False,
                )

            if hasattr(runtime, "inject_prompt_extras"):
                runtime.inject_prompt_extras(
                    ctx.prompt_stack,
                    identity=identity,
                    chat_request=ctx.chat_request,
                    real_time_context=request_metadata.get("real_time_context"),
                )

            _log.debug(
                "[CharacterRuntime] before_turn executed: character=%s target=%s scope=%s "
                "rel(affection=%s trust=%s familiarity=%s dependency=%s security=%s)",
                identity.character_id,
                identity.target_id,
                identity.scope_id,
                turn.relationship.affection if turn.relationship else "N/A",
                turn.relationship.trust if turn.relationship else "N/A",
                turn.relationship.familiarity if turn.relationship else "N/A",
                turn.relationship.dependency if turn.relationship else "N/A",
                turn.relationship.security if turn.relationship else "N/A",
            )
        except Exception as exc:
            _log.warning(
                "[CharacterRuntime] before_turn 异常: %s", exc, exc_info=True
            )

    def _phase_character_runtime_after_turn(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        """角色运行时 after_turn：更新情绪、关系、写入事件、抽取记忆"""
        request_metadata = getattr(ctx.chat_request, "metadata", None)
        if not isinstance(request_metadata, dict):
            request_metadata = {}
            ctx.chat_request.metadata = request_metadata
        request_metadata.update(ctx.metadata)

        # 静默的内部触发不是用户交互，不应因为内部指令改变
        # 角色对用户的情绪、好感和关系状态。
        if ctx.metadata.get("skip_character_after_turn"):
            _log.debug("[CharacterRuntime] after_turn skipped: internal silent trigger")
            return

        runtime = callbacks.get_character_runtime(ctx)
        identity = callbacks.get_character_context(ctx)

        if not runtime:
            _log.debug("[CharacterRuntime] after_turn skipped: runtime is None")
            return
        self._attach_hook_runtime_to_character_runtime(runtime)
        if not identity:
            _log.debug("[CharacterRuntime] after_turn skipped: identity is None")
            return
        if not ctx.character_turn:
            _log.debug("[CharacterRuntime] after_turn skipped: ctx.character_turn is None")
            return

        try:
            runtime.after_turn(
                chat_request=ctx.chat_request,
                result=result,
                turn_context=ctx.character_turn,
            )
            _log.debug(
                "[CharacterRuntime] after_turn executed: character=%s scope=%s",
                identity.character_id,
                identity.scope_id,
            )
        except Exception as exc:
            _log.warning(
                "[CharacterRuntime] after_turn 异常: %s", exc, exc_info=True
            )

    def _attach_hook_runtime_to_character_runtime(self, runtime) -> None:
        hook_runtime = getattr(self, "_hook_runtime", None)
        if not hook_runtime or runtime is None:
            return
        try:
            runtime._hook_runtime = hook_runtime
        except Exception as exc:
            _log.debug("[CharacterRuntime] hook runtime attach skipped: %s", exc)

    def _phase_auto_memory(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        result.metadata["auto_memory_pending"] = True

        def run_auto_memory() -> None:
            try:
                from nbot.core.auto_memory import extract_and_save_turn_memories

                saved_count = extract_and_save_turn_memories(ctx, callbacks, result)
                if saved_count:
                    result.metadata["auto_memory_saved"] = saved_count
            except Exception as exc:
                _log.warning("[AutoMemory] async task failed: %s", exc, exc_info=True)
            finally:
                result.metadata["auto_memory_pending"] = False

        submit_background_task(
            "auto_memory",
            run_auto_memory,
            serial_key="auto_memory",
        )

    # ------------------------------------------------------------------
    # MemoryFS 直接注入（独立于角色运行时）
    # ------------------------------------------------------------------

    def _inject_memory_fs_direct(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
    ) -> None:
        """将 MemoryFS 结构化记忆直接注入 PromptStack。

        不依赖 CharacterRuntime，作为 _phase_prepare_context 的独立步骤。
        如果后续角色运行时成功执行了 _inject_memory_fs（key=memory_fs_context），
        本步骤注入的 memory_fs.context 会被移除以避免重复。
        """
        try:
            from nbot.memory.fs import get_memory_fs

            mfs = get_memory_fs()

            # 从 metadata 和 callbacks 提取标识
            memory_ctx = callbacks.get_memory_context(ctx) if hasattr(callbacks, "get_memory_context") else {}
            metadata = getattr(ctx.chat_request, "metadata", {}) or {}

            char_id = (
                str(memory_ctx.get("character_id") or "").strip()
                or str(metadata.get("character_id") or "").strip()
                or str(memory_ctx.get("character_name") or "").strip()
            )
            user_id = (
                str(memory_ctx.get("target_id") or "").strip()
                or str(memory_ctx.get("user_id") or "").strip()
                or str(getattr(ctx.chat_request, "user_id", "") or "").strip()
            )
            conv_id = str(getattr(ctx.chat_request, "conversation_id", "") or "").strip()

            if not char_id:
                return

            # 检索化注入：以最近用户消息为查询，按相关性+重要度挑选小节
            query = str(ctx.chat_request.content or "")
            ctx_text = mfs.build_prompt_context(
                char_id, user_id, conv_id, query=query, max_chars=2000
            )
            if ctx_text:
                ctx.prompt_stack.add(
                    key="memory_fs.context",
                    content=ctx_text,
                    priority=58,  # 在旧记忆(60)之前、关系(50)之后
                    scope="turn",
                )
                _log.debug(
                    "[MemoryFS] direct inject %d chars for char=%s user=%s",
                    len(ctx_text), char_id, user_id,
                )
        except Exception as exc:
            _log.debug("[MemoryFS] direct inject failed: %s", exc)

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
                progress.on_done(ctx)
                return

        # 尝试工具循环
        if tools:
            self._run_tool_loop(ctx, callbacks, tools, max_tool_iterations, progress)
            if ctx.metadata.get("waiting_confirmation"):
                return
            progress.on_done(ctx)
            return

        # 简单路径：单次模型调用
        self._run_simple(ctx, callbacks)
        progress.on_done(ctx)

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

        # 提取模型追踪信息
        for key in ("_model_id", "_model_name", "_failover_events"):
            value = response.pop(key, None)
            if value is not None:
                ctx.metadata[key.lstrip("_")] = value

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
        import time as _time

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

        # 工具轨迹实时落库：每完成一步就 append，进程中断也留下已完成步骤
        from nbot.core import session_store

        trace_key = session_store.tool_trace_key(
            str((ctx.tool_context or {}).get("session_id", "") or ""),
            str(getattr(ctx.chat_request, "conversation_id", "") or ""),
        )
        trace_turn = _time.strftime("%Y%m%d%H%M%S") + "-" + str(id(ctx))
        ctx.metadata["_tool_trace_key"] = trace_key
        ctx.metadata["_tool_trace_turn"] = trace_turn
        session_store.begin_tool_trace(trace_key)

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
                confirmation_message = result.get("message") or (
                    f"⚠️ 命令需要确认: {command}\n"
                    f"[请求ID: {request_id}]\n"
                    f"请回复「确认」执行，或「取消」放弃。"
                )
                ctx.metadata["waiting_confirmation"] = True
                ctx.metadata["pending_exec_request_id"] = request_id
                ctx.metadata["pending_exec_command"] = command
                ctx.metadata["confirmation_message"] = confirmation_message
                callbacks.on_confirmation_required(ctx, request_id, command)
                progress.on_waiting_confirmation(ctx, command, request_id)
                raise ToolLoopExit(confirmation_message)

            # 处理向用户提问（ask_user_question）：展示问题并等待回答
            if result.get("require_user_answer"):
                request_id = result.get("request_id", "")
                question = result.get("question", "")
                options = result.get("options") or []
                question_message = f"❓ **需要你的输入**\n\n{question}"
                if options:
                    question_message += "\n\n可选回答：" + " / ".join(options)
                ctx.metadata["waiting_user_answer"] = True
                ctx.metadata["pending_question_id"] = request_id
                callbacks.on_waiting_confirmation(ctx, question, request_id)
                progress.on_waiting_confirmation(ctx, question, request_id)
                raise ToolLoopExit(question_message)

            return result

        # 工具循环钩子 → 进度报告
        def on_iteration_start(iteration, messages):
            progress.on_tool_iteration(ctx, iteration)

        def on_tool_start(tool_call, thinking, iteration, messages):
            name = tool_call.get("name", "")
            args = tool_call.get("arguments", {})
            ctx.metadata["_tool_start_ts"] = _time.perf_counter()
            self._emit_hook("tool.before_call", ctx, extra_payload={"tool_name": name, "tool_args": args})
            progress.on_tool_start(ctx, name, args, thinking)

        def on_tool_result(tool_call, result, thinking, iteration, messages):
            name = tool_call.get("name", "")
            start_ts = ctx.metadata.pop("_tool_start_ts", None)
            if start_ts is not None and isinstance(result, dict) and "_duration_ms" not in result:
                try:
                    result["_duration_ms"] = round((_time.perf_counter() - start_ts) * 1000.0, 1)
                except Exception:
                    pass
            self._emit_hook("tool.after_call", ctx, extra_payload={"tool_name": name, "tool_result": str(result)[:200]})
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
            if result.get("_send_image"):
                # 工具触发的图片发送（如 send_sticker）
                if hasattr(callbacks, "send_image"):
                    try:
                        callbacks.send_image(ctx, result["_send_image"])
                    except Exception as exc:
                        _log.debug("[Tool] send_image failed: %s", exc)
            return None  # 使用默认 tool message 格式

        def on_tool_trace(trace):
            """单个工具步骤实时落库（内存 + JSONL），同时同步到 ctx.tool_trace。"""
            step = {
                "role": "tool",
                "tool_call_id": trace.tool_call_id,
                "name": trace.tool_name,
                "content": json.dumps(trace.result, ensure_ascii=False, default=str),
                "turn": trace_turn,
                "iteration": trace.iteration,
                "ts": _time.time(),
            }
            ctx.tool_trace.append(step)
            session_store.append_inflight_tool_step(trace_key, step)

        hooks = ToolLoopHooks(
            on_iteration_start=on_iteration_start,
            on_tool_start=on_tool_start,
            on_tool_result=on_tool_result,
            on_tool_trace=on_tool_trace,
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
            error_str = str(e)
            _log.error(f"Tool loop failed: {e}")
            # 仅当首次模型调用就失败（iteration==0，工具从未被调用过）时，
            # 才回退到无工具的普通对话；否则保留已有的工具调用进度
            iteration = getattr(e, "iteration", -1)
            if "400" in error_str and iteration <= 0:
                _log.warning(
                    "首次模型调用返回400错误（iteration=%d），"
                    "回退到无工具对话",
                    iteration,
                )
                progress.on_thinking_start(ctx)
                self._run_simple(ctx, callbacks)
                progress.on_done(ctx)
                return
            ctx.error = error_str
            ctx.final_content = f"工具循环执行失败: {error_str}"
            return

        loop_result = execution_result.loop_result
        ctx.usage = dict(loop_result.usage or {})

        # 提取模型追踪信息
        if loop_result.model_id:
            ctx.metadata["model_id"] = loop_result.model_id
        if loop_result.model_name:
            ctx.metadata["model_name"] = loop_result.model_name
        if loop_result.failover_events:
            ctx.metadata["failover_events"] = list(loop_result.failover_events)

        # 工具轨迹：整轮结束后用完整协议消息覆盖「每步追加」的增量记录
        # （增量记录只用于中断/异常兜底，完整记录才能支撑「继续」）
        full_trace = extract_tool_call_history(loop_result.tool_messages)
        if full_trace:
            ctx.tool_trace = full_trace
        waiting_followup = bool(
            ctx.metadata.get("waiting_confirmation")
            or ctx.metadata.get("waiting_user_answer")
        )
        if not loop_result.stopped and not waiting_followup:
            # 正常收尾才清理暂存；中断/待确认的轨迹留着，供继续或恢复时补全
            session_store.clear_inflight_tool_steps(trace_key)

        if loop_result.stopped:
            ctx.stopped_prematurely = True
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
        import uuid
        import time as _time
        # swipes：重新生成时复用目标消息 id，前端流式气泡原地替换旧内容
        message_id = str(ctx.metadata.get("swipe_target_id") or "") or str(uuid.uuid4())
        full_content = ""
        stream_start = _time.perf_counter()
        ttft_ms = None

        try:
            for event in streamer(ctx.messages, stop_event=ctx.stop_event):
                if ctx.stop_event and ctx.stop_event.is_set():
                    break

                if isinstance(event, dict):
                    # 提取模型追踪信息
                    for key in ("_model_id", "_model_name", "_failover_events"):
                        value = event.get(key)
                        if value is not None:
                            ctx.metadata[key.lstrip("_")] = value

                    usage = {}
                    try:
                        from nbot.core.model_adapter import normalize_usage_dict

                        usage = normalize_usage_dict(event.get("usage"))
                    except Exception:
                        if isinstance(event.get("usage"), dict):
                            usage = dict(event.get("usage"))
                    if usage:
                        ctx.usage = usage
                    chunk = event.get("content", "")
                else:
                    chunk = str(event)
                if not chunk:
                    continue

                if not full_content:
                    # 首块：记录 TTFT（Time To First Token）
                    ttft_ms = (_time.perf_counter() - stream_start) * 1000.0
                    msg = {"role": "assistant", "content": "", "id": message_id}
                    self._emit_hook("model.on_stream_chunk", ctx)
                    callbacks.on_stream_start(ctx, msg)
                    ctx.streamed_message = msg

                full_content += chunk
                callbacks.on_stream_chunk(ctx, chunk, message_id)
        except Exception as e:
            _log.error(f"Streaming failed: {e}")
            # Debug: log message count and content preview
            try:
                _log.warning("[StreamDebug] msg_count=%d msgs=%s", len(ctx.messages), str([{"role": m.get("role"), "len": len(str(m.get("content", "")))} for m in ctx.messages[-3:]])[:300])
            except Exception:
                pass
            ctx.error = str(e)
            full_content = full_content or f"流式输出失败: {e}"

        ctx.final_content = full_content
        # 记录流式响应的时间指标
        duration_ms = (_time.perf_counter() - stream_start) * 1000.0
        ctx.metadata["duration_ms"] = duration_ms
        if ttft_ms is not None:
            ctx.metadata["ttft_ms"] = ttft_ms
        else:
            # 没收到任何 chunk（失败兜底），TTFT ≈ duration
            ctx.metadata["ttft_ms"] = duration_ms
        # 流式在首块到达前就失败（如 400），需要创建消息并把错误文本送到前端
        if full_content and not ctx.streamed_message:
            msg = {"role": "assistant", "content": "", "id": message_id}
            callbacks.on_stream_start(ctx, msg)
            callbacks.on_stream_chunk(ctx, full_content, message_id)
            ctx.streamed_message = msg
        if ctx.streamed_message:
            ctx.metadata["streamed"] = True
            ctx.metadata["stream_end_pending"] = True
            ctx.metadata["stream_message_id"] = message_id
        else:
            ctx.metadata.pop("streamed", None)
            ctx.metadata.pop("stream_end_pending", None)
            ctx.metadata.pop("stream_message_id", None)

    # ------------------------------------------------------------------
    # Failover wrapper
    # ------------------------------------------------------------------

    def _wrap_with_failover(
        self,
        model_configs: list,
        purpose: str = "chat",
        tools: Optional[List[Dict[str, Any]]] = None,
        session_id: str = "",
    ) -> Callable:
        """Wrap model call with automatic failover across model queue.

        Returns a model_call(messages, stop_event) that tries models
        in priority order, failing over on recoverable HTTP errors.

        Args:
            model_configs: Ordered list of model config dicts (by priority).
            purpose: Model purpose for logging.
            session_id: 会话 ID（用于智能路由 A/B 分流与决策日志）。
        """
        from nbot.core.failover import (
            classify_http_error,
            get_failover_state,
            _extract_status_code,
        )
        from nbot.services.ai import apply_model_config
        from nbot.core.protocols import get_protocol
        from nbot.core.model_adapter import response_json_utf8
        import requests
        import time as _time

        failover = get_failover_state()

        # 智能路由：按成本/质量/速度/上下文打分重排模型队列
        _router = None
        _decision_id = None
        _scored_candidates = []
        try:
            from nbot.core.smart_routing import get_smart_router
            _router = get_smart_router()
            _router.load_settings()
            model_configs, _scored_candidates = _router.route_with_scores(
                model_configs,
                purpose=purpose,
                session_id=session_id,
                has_tools=bool(tools),
            )
        except Exception as exc:
            _log.debug("[SmartRouting] route failed: %s", exc)

        def _build_call_for_config(config: dict):
            """Create a single-shot model_call for a specific config."""
            def _call(messages, stop_event=None):
                if stop_event and stop_event.is_set():
                    raise StopIteration("User stopped")

                base_url = config.get("base_url") or ""
                model_name = config.get("model") or ""
                provider_type = config.get(
                    "provider_type", "openai_compatible"
                )
                key = config.get("api_key") or ""
                append_path = config.get("append_base_url_path", True)

                protocol = get_protocol(provider_type)
                url = protocol.resolve_url(
                    base_url,
                    model=model_name,
                    append_base_url_path=append_path,
                    api_key=key,
                )
                headers = protocol.build_headers(key)
                payload = protocol.build_payload(
                    model_name,
                    messages,
                    tools=tools if tools else None,
                    tool_choice="auto" if tools else None,
                    stream=False,
                    base_url=base_url,
                    provider_type=provider_type,
                    extra_body=request_extra_body(config) or None,
                )
                # 使用模型配置的 failover_timeout，0 表示使用默认 120s
                request_timeout = config.get("failover_timeout", 0) or 120
                _log.info("[FailoverDebug] model=%s msg_count=%d has_tools=%s payload_keys=%s",
                    model_name, len(messages), str(bool(tools)), str(list(payload.keys())))
                resp = requests.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                    **model_proxy_request_kwargs(config.get("proxy_url", "")),
                )
                if resp.status_code != 200:
                    _log.info("[FailoverDebug] url=%s status=%d body=%s", url, resp.status_code, resp.text[:500])
                resp.raise_for_status()
                normalized = protocol.parse_response(
                    response_json_utf8(resp),
                    model=model_name,
                    base_url=base_url,
                    provider_type=provider_type,
                )
                return normalized.to_dict()

            return _call

        def _context_chars(msgs) -> int:
            total = 0
            for msg in msgs or []:
                if isinstance(msg, dict):
                    total += len(str(msg.get("content") or ""))
            return total

        def _looks_like_refusal(result: dict) -> bool:
            """拒绝/过滤回复检测：短文本拒绝话术或显式 content_filter。"""
            if not isinstance(result, dict):
                return False
            finish = str(result.get("finish_reason") or "")
            if finish == "content_filter":
                return True
            if result.get("tool_calls"):
                return False
            content = str(result.get("content") or "").strip()
            if not content or len(content) > 200:
                return False
            patterns = (
                "无法回答", "不能提供", "无法提供", "我不能协助", "无法协助",
                "我不能帮助", "无法帮助", "抱歉，我不能", "对不起，我不能",
                "违反了", "内容政策", "使用政策",
                "i can't", "i cannot", "i'm unable", "i am unable",
                "against my", "content policy", "as an ai",
            )
            low = content.lower()
            return any(p in low for p in patterns)

        def failover_model_call(messages, stop_event=None):
            last_error = None
            attempted = set()
            failover_events = []
            nonlocal _decision_id

            for _ in range(len(model_configs)):
                config = failover.select_model(
                    model_configs, exclude_ids=attempted
                )
                if config is None:
                    break

                model_id = config.get("model_id", "")
                model_name = config.get("model", "")
                attempted.add(model_id)

                # 首次选择时记录智能路由决策
                if _router is not None and _decision_id is None:
                    try:
                        from nbot.core.smart_routing import ab_group_for
                        _decision_id = _router.record_decision(
                            session_id=session_id,
                            purpose=purpose,
                            candidates=_scored_candidates,
                            selected_id=model_id,
                            selected_name=model_name,
                            reason="smart_route",
                            ab_group=ab_group_for(session_id, (_router.settings_dict().get("ab_test") or {}).get("ratio", 50))
                            if (_router.settings_dict().get("ab_test") or {}).get("enabled") else "",
                        )
                    except Exception as exc:
                        _log.debug("[SmartRouting] record decision failed: %s", exc)

                # Apply this config to global AIClient
                apply_model_config(config)

                # 调用前上下文预校验：超过该模型上下文窗口直接换下一个模型
                model_context_limit = int(config.get("max_context_length") or 0)
                if model_context_limit > 0 and _context_chars(messages) > model_context_limit:
                    failover.record_failure(model_id, 400)
                    last_error = RuntimeError(
                        f"上下文长度超出模型 {model_name} 限制 ({model_context_limit})"
                    )
                    failover_events.append({
                        "model_id": model_id,
                        "model_name": model_name,
                        "status_code": 400,
                        "category": "context_overflow",
                    })
                    _log.warning(
                        "[Failover] %s model=%s context=%d > limit=%d, trying next model",
                        purpose, model_id, _context_chars(messages), model_context_limit,
                    )
                    continue

                try:
                    started = _time.time()
                    call = _build_call_for_config(config)
                    result = call(messages, stop_event=stop_event)

                    # 拒绝/过滤回复检测：换下一个模型（最后一个模型仍返回原结果）
                    if _looks_like_refusal(result) and len(attempted) < len(model_configs):
                        failover.record_failure(model_id, 451)
                        last_error = RuntimeError(
                            f"模型 {model_name} 返回拒绝/被过滤回复"
                        )
                        failover_events.append({
                            "model_id": model_id,
                            "model_name": model_name,
                            "status_code": 451,
                            "category": "refusal",
                        })
                        _log.warning(
                            "[Failover] %s model=%s returned refusal, trying next model",
                            purpose, model_id,
                        )
                        continue

                    failover.record_success(model_id)
                    # 注入模型信息和故障转移记录到响应
                    result["_model_id"] = model_id
                    result["_model_name"] = model_name
                    if failover_events:
                        result["_failover_events"] = failover_events
                    if _router is not None and _decision_id:
                        try:
                            _router.backfill_decision(
                                _decision_id,
                                success=True,
                                duration_ms=round((_time.time() - started) * 1000, 2),
                            )
                        except Exception:
                            pass
                    return result
                except StopIteration:
                    raise
                except Exception as e:
                    status = _extract_status_code(e)
                    category = classify_http_error(status)
                    if category == "config":
                        _log.warning(
                            "[Failover] %s model=%s config error %d, "
                            "not failing over",
                            purpose, model_id, status,
                        )
                        raise
                    failover.record_failure(model_id, status)
                    last_error = e
                    failover_events.append({
                        "model_id": model_id,
                        "model_name": model_name,
                        "status_code": status,
                        "category": category,
                    })
                    if _router is not None and _decision_id:
                        try:
                            _router.backfill_decision(_decision_id, success=False)
                        except Exception:
                            pass
                    _log.warning(
                        "[Failover] %s model=%s failed (%s %d), "
                        "trying next model",
                        purpose, model_id, category, status,
                    )
                    continue

            # All models exhausted
            if last_error:
                raise last_error
            raise RuntimeError(
                f"No models available for purpose '{purpose}'"
            )

        return failover_model_call

    # ------------------------------------------------------------------
    # Phase 5: 结果组装
    # ------------------------------------------------------------------

    def _post_process_result(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        """角色运行时 after_turn → 自动记忆 → on_response_complete → 表情包 → 角色生图。"""
        self._phase_character_runtime_after_turn(ctx, callbacks, result)
        if ctx.mode_policy.run_auto_memory:
            self._phase_auto_memory(ctx, callbacks, result)
        callbacks.on_response_complete(ctx, result)

        # 表情包发送：基于角色心情，按配置概率单独发送
        self._try_send_sticker(ctx, callbacks, result)

        # 角色生图：解析 [send_image: ...] 内联标签，按概率兜底
        self._try_send_image(ctx, callbacks, result)

        # 自定义表情包：QQ 频道解析 [名称] 标签并发送原图（Web 端由前端内联渲染）
        self._try_send_named_stickers(ctx, callbacks, result)

    def _try_send_named_stickers(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        """回复文本中的 [名称] 命中自定义表情库时，QQ 频道发送原图。"""
        if result.error:
            return
        channel = (
            ctx.metadata.get("source", "")
            or getattr(ctx.chat_request, "channel", "")
            or ctx.metadata.get("channel_type", "")
        )
        if channel != "qq":
            return
        content = str(result.final_content or "")
        if "[" not in content or "]" not in content:
            return
        try:
            from nbot.services.sticker_library import extract_sticker_tags

            hits = extract_sticker_tags(content)
        except Exception as exc:
            _log.debug("[StickerLib] tag extract failed: %s", exc)
            return
        for hit in hits:
            try:
                callbacks.send_image(
                    ctx,
                    {"url": hit["url"], "prompt": hit["name"], "trigger": "sticker_tag"},
                )
                _log.info("[StickerLib] 已发送自定义表情: %s", hit["name"])
            except Exception as exc:
                _log.warning("[StickerLib] 发送失败: %s", exc)

    def _try_send_sticker(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        """根据角色心情发送表情包（仅 web / qq 频道，通过频道回调单独发送）"""
        # 仅在 web 和 qq 频道启用表情包
        channel = (
            ctx.metadata.get("source", "")
            or getattr(ctx.chat_request, "channel", "")
            or ctx.metadata.get("channel_type", "")
        )
        if channel not in ("web", "qq"):
            return

        # 有错误时不发表情包
        if result.error:
            return

        # 检查表情包功能开关
        from nbot.services.sticker_service import is_sticker_enabled

        if not is_sticker_enabled():
            return

        # 从角色运行时上下文获取当前心情
        mood = ""
        if hasattr(ctx, "character_turn") and ctx.character_turn:
            turn = ctx.character_turn
            if hasattr(turn, "state") and turn.state:
                mood = getattr(turn.state, "mood", "") or ""

        if not mood:
            return

        # 按配置概率触发
        from nbot.services.sticker_service import should_send_sticker, get_sticker_for_mood

        if not should_send_sticker():
            _log.debug("[Sticker] 概率未命中，跳过: mood=%s", mood)
            return

        # 获取表情包
        sticker = get_sticker_for_mood(mood)
        if not sticker:
            return

        # 通过频道回调单独发送表情包消息
        try:
            callbacks.send_sticker(ctx, sticker)
            _log.info(
                "[Sticker] 表情包已发送: channel=%s mood=%s url=%s",
                channel, mood, sticker.get("url", "")[:80],
            )
        except Exception as exc:
            _log.warning("[Sticker] 发送失败: %s", exc)

    def _try_send_image(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
        result: PipelineResult,
    ) -> None:
        """解析 LLM 回复中的 [send_image: ...] 标签，按概率兜底触发角色生图。

        触发逻辑：
          1. 若 final_content 含 [send_image: <prompt>] 标签，剥离后逐条生图。
          2. 标签不存在时，按 settings.json 中的 probability 概率兜底生图（参照 sticker）。
          3. agent 会话模式、错误结果、不支持的频道、不在线的图片生成模型：均跳过。
        """
        from nbot.services.image_service import (
            call_image_generation,
            extract_send_image_tags,
            get_image_generation_config,
            get_image_generation_size,
            is_image_generation_enabled,
            is_image_generation_feature_enabled,
            save_image_to_uploads,
            should_send_image_probability,
        )

        # agent 模式不触发（避免误生图）
        if not ctx.mode_policy.try_send_image:
            return

        # 出错时不生图
        if result.error:
            return

        # 仅 web / qq 频道支持（与 sticker 一致）
        channel = (
            ctx.metadata.get("source", "")
            or getattr(ctx.chat_request, "channel", "")
            or ctx.metadata.get("channel_type", "")
        )
        if channel not in ("web", "qq"):
            return

        # 功能开关
        if not is_image_generation_feature_enabled():
            return

        # 模型配置
        config = get_image_generation_config()
        if not config or not is_image_generation_enabled():
            _log.debug("[ImageGen] 未配置图片生成模型，跳过")
            return

        # 解析 LLM 显式标签
        prompts: List[str] = []
        if result.final_content:
            cleaned_content, prompts = extract_send_image_tags(result.final_content)
            if cleaned_content != result.final_content:
                # 回写剥离后的内容，保持与 sticker 类似的轻量处理
                result.final_content = cleaned_content
                if result.assistant_message and isinstance(result.assistant_message, dict):
                    result.assistant_message["content"] = cleaned_content
                if ctx.streamed_message and isinstance(ctx.streamed_message, dict):
                    ctx.streamed_message["content"] = cleaned_content
                if ctx.metadata.get("streamed") and hasattr(callbacks, "save_assistant_message"):
                    try:
                        callbacks.save_assistant_message(ctx, ctx.streamed_message)
                    except Exception as exc:
                        _log.debug("[ImageGen] 流式消息内容回写失败: %s", exc)

        # 兜底概率触发：仅在没有显式标签时考虑
        if not prompts:
            if not should_send_image_probability():
                return
            # 取最近的用户消息作为生图灵感来源
            user_text = ""
            for msg in reversed(ctx.messages or []):
                if msg.get("role") == "user":
                    user_text = str(msg.get("content") or "").strip()
                    if user_text:
                        break
            if not user_text:
                return
            # 限制 prompt 长度，避免 prompt 过载
            user_text = user_text[:200]
            prompt = (
                f"Anime-style illustration inspired by the conversation context: {user_text}"
            )
            prompts = [prompt]
            triggers = ["auto"]
        else:
            triggers = ["tag"] * len(prompts)

        size = get_image_generation_size()

        for prompt, trigger in zip(prompts, triggers):
            if not prompt:
                continue
            image_url_or_b64 = call_image_generation(prompt, config, size=size)
            if not image_url_or_b64:
                _log.warning("[ImageGen] prompt 生成失败: %s", prompt[:80])
                continue

            # 若返回的是远程 URL，下载到本地 uploads 以保证 QQ 也能稳定发送
            final_url = image_url_or_b64
            if image_url_or_b64.startswith("http"):
                try:
                    upload_dir = os.path.join(
                        os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
                        "nbot", "web", "static", "uploads", "character_images",
                    )
                    saved = save_image_to_uploads(image_url_or_b64, upload_dir, prefix="char")
                    if saved:
                        final_url = saved
                except Exception as exc:
                    _log.debug("[ImageGen] 本地化图片失败，使用远程 URL: %s", exc)

            try:
                callbacks.send_image(
                    ctx,
                    {"url": final_url, "prompt": prompt, "trigger": trigger},
                )
                _log.info(
                    "[ImageGen] 图片已发送: channel=%s trigger=%s url=%s",
                    channel,
                    trigger,
                    final_url[:80],
                )
            except Exception as exc:
                _log.warning("[ImageGen] 发送失败: %s", exc)

    def _phase_assemble_result(
        self,
        ctx: PipelineContext,
        callbacks: PipelineCallbacks,
    ) -> PipelineResult:
        from nbot.core.agent_service import extract_tool_call_history

        # Streaming messages are visible in the UI before the final pipeline
        # result is assembled. Persist them before emitting stream_end so a
        # message-list refresh cannot drop the temporary bubble.
        if ctx.metadata.get("streamed") and ctx.streamed_message:
            ctx.streamed_message["content"] = ctx.final_content
            callbacks.save_assistant_message(ctx, ctx.streamed_message)
            # 存储引用以便 on_stream_end 检查过滤状态
            ctx.metadata["_streamed_message_ref"] = ctx.streamed_message
            result = PipelineResult(
                final_content=ctx.final_content,
                assistant_message=ctx.streamed_message,
                tool_trace=ctx.tool_trace,
                can_continue=bool(ctx.tool_trace),
                stopped_prematurely=ctx.stopped_prematurely,
                usage=ctx.usage,
                error=ctx.error,
                metadata=ctx.metadata,
            )
            if ctx.metadata.pop("stream_end_pending", False):
                callbacks.on_stream_end(
                    ctx,
                    ctx.metadata.get("stream_message_id") or ctx.streamed_message.get("id", ""),
                )
            self._post_process_result(ctx, callbacks, result)
            return result

        if ctx.error:
            error_content = ctx.final_content or ctx.error
            # 构建并发送错误回复，确保用户能看到反馈
            if ctx.adapter and hasattr(ctx.adapter, "build_assistant_message"):
                temp_response = ChatResponse(
                    final_content=error_content,
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
                    "content": error_content,
                }
            if ctx.tool_trace:
                # 工具循环中途失败：已完成的工具步骤随错误消息一起保存，不再整轮丢弃
                assistant_message["tool_call_history"] = ctx.tool_trace
            callbacks.save_assistant_message(ctx, assistant_message)
            callbacks.send_response(ctx, assistant_message)
            result = PipelineResult(
                final_content=error_content,
                assistant_message=assistant_message,
                error=ctx.error,
                metadata=ctx.metadata,
            )
            self._post_process_result(ctx, callbacks, result)
            return result

        # 非流式：通过适配器构建 assistant_message
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
        self._post_process_result(ctx, callbacks, result)
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
# 单字/单字符关键词容易误判（如"是"、"y"），单独维护并要求精确匹配
_CONFIRM_EXACT = {"是", "y", "ok", "执行"}
_REJECT_EXACT = {"否", "n", "cancel"}


def handle_tool_confirmation(
    content: str,
    session_id: str,
    *,
    log_prefix: str = "",
    session_type: str = "",
) -> str:
    """检测并处理工具确认/拒绝。

    在各频道入口处调用，检测用户输入是否为确认/拒绝关键词。
    如果是确认，则执行待处理命令并返回执行结果文本。
    如果是拒绝，则拒绝待处理命令并返回拒绝文本。
    如果不是确认/拒绝，返回原始 content。

    匹配规则（严格，避免误判）：
    - 用户输入去空白并转小写后，必须完全等于某个关键词；
    - 或者是关键词加上标点（如"确认。"、"yes!"）。

    Args:
        session_type: 可选的频道类型（如 "qq_private"/"qq_group"/"web"/"feishu"），
            用于构造复合 key 避免跨频道 session_id 冲突。

    Returns:
        替换后的消息内容（原始内容 或 确认/拒绝结果文本）
    """
    stripped = (content or "").strip().lower()
    if not stripped:
        return content

    # 去掉末尾常见标点，便于匹配"确认。"、"yes!"等
    stripped_clean = stripped.rstrip("。.!！?？~~")

    # 多字关键词允许包含匹配（但要求整句就是该词或其加标点形式）
    multi_confirm = _CONFIRM_KEYWORDS - _CONFIRM_EXACT
    multi_reject = _REJECT_KEYWORDS - _REJECT_EXACT

    is_confirm = (
        stripped in _CONFIRM_KEYWORDS
        or stripped_clean in _CONFIRM_KEYWORDS
        or any(stripped_clean == kw for kw in multi_confirm)
    )
    # 单字/单字符关键词仅精确匹配，避免"是否""boy"等误判
    if not is_confirm:
        is_confirm = stripped_clean in _CONFIRM_EXACT

    is_reject = (
        stripped in _REJECT_KEYWORDS
        or stripped_clean in _REJECT_KEYWORDS
        or any(stripped_clean == kw for kw in multi_reject)
    )
    if not is_reject:
        is_reject = stripped_clean in _REJECT_EXACT

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

        request_id = get_pending_by_session(session_id, session_type=session_type)
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

import copy
import json
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Callable, Dict, List, Optional, Sequence

from nbot.core.chat_models import ChatRequest, ChatResponse
from nbot.channels.registry import get_channel_handler

if TYPE_CHECKING:
    from nbot.channels.base import BaseChannelAdapter

_log = logging.getLogger(__name__)

DEFAULT_CONTINUE_TOKENS = ("\u7ee7\u7eed", "\u7ee7\u7eed\u6267\u884c", "continue")


class ToolLoopExit(Exception):
    def __init__(self, final_content: str):
        super().__init__(final_content)
        self.final_content = final_content


class ToolLoopModelError(Exception):
    """Wraps a model_call error inside the tool loop with iteration context.

    Attributes:
        iteration: The iteration index where the error occurred (0-based).
                   0 means the very first model call failed.
    """

    def __init__(self, original: Exception, iteration: int):
        super().__init__(str(original))
        self.original = original
        self.iteration = iteration


@dataclass
class ToolLoopHooks:
    on_iteration_start: Optional[Callable[[int, List[Dict[str, Any]]], None]] = None
    on_tool_start: Optional[
        Callable[[Dict[str, Any], str, int, List[Dict[str, Any]]], None]
    ] = None
    on_tool_result: Optional[
        Callable[
            [Dict[str, Any], Dict[str, Any], str, int, List[Dict[str, Any]]],
            Optional[Dict[str, Any]],
        ]
    ] = None
    # 单个工具步骤落库回调（实时追加轨迹，中断也不丢）
    on_tool_trace: Optional[Callable[["ToolCallTrace"], None]] = None


@dataclass
class ToolLoopResult:
    final_content: str = ""
    tool_messages: List[Dict[str, Any]] = field(default_factory=list)
    stopped: bool = False
    iterations: int = 0
    consecutive_errors: int = 0
    usage: Dict[str, int] = field(default_factory=dict)
    model_id: str = ""
    model_name: str = ""
    failover_events: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolExecutionResult:
    loop_result: ToolLoopResult
    prepared_messages: List[Dict[str, Any]] = field(default_factory=list)


@dataclass
class ToolLoopSession:
    initial_messages: List[Dict[str, Any]]
    model_call: Callable[..., Dict[str, Any]]
    tool_executor: Callable[
        [Dict[str, Any], str, int, List[Dict[str, Any]]], Dict[str, Any]
    ]
    tool_call_history: Optional[List[Dict[str, Any]]] = None
    max_iterations: int = 50
    max_consecutive_errors: int = 3
    stop_event: Any = None
    hooks: Optional["ToolLoopHooks"] = None
    # 新增：单工具执行超时（秒）。None 表示不限制
    tool_timeout: Optional[float] = None
    # 新增：是否并发执行同一轮的多个 tool_calls
    parallel_tool_execution: bool = True
    # 新增：只读工具判定器（只读并发、写/副作用串行）。None 时用内置判定
    read_only_classifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None


@dataclass
class HarnessState:
    """AgentHarness 运行时状态快照（可序列化、可观测）。"""

    iteration: int = 0
    consecutive_errors: int = 0
    stopped: bool = False
    paused: bool = False
    finished: bool = False
    final_content: str = ""
    usage: Dict[str, int] = field(default_factory=dict)
    model_id: str = ""
    model_name: str = ""
    failover_events: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_count: int = 0


@dataclass
class ToolCallTrace:
    """单次工具调用轨迹。"""

    iteration: int
    tool_name: str
    arguments: Dict[str, Any]
    result: Dict[str, Any]
    success: bool
    error: Optional[str] = None
    tool_call_id: str = ""


@dataclass
class _ToolLoopExitMarker:
    """内部辅助类：并发执行时包装 ToolLoopExit 异常。"""
    exc: ToolLoopExit


_READ_ONLY_CLASSIFIER: Optional[Callable[[str, Dict[str, Any]], bool]] = None
_READ_ONLY_CLASSIFIER_LOADED = False


def _default_read_only_classifier() -> Callable[[str, Dict[str, Any]], bool]:
    """懒加载只读工具判定器；加载失败时一律按“有副作用”处理（串行）。"""
    global _READ_ONLY_CLASSIFIER, _READ_ONLY_CLASSIFIER_LOADED
    if not _READ_ONLY_CLASSIFIER_LOADED:
        _READ_ONLY_CLASSIFIER_LOADED = True
        try:
            from nbot.services.tools import is_read_only_tool

            _READ_ONLY_CLASSIFIER = is_read_only_tool
        except Exception as exc:  # 保持安全默认：不并发
            _log.warning("[AgentHarness] 只读工具判定器加载失败，工具将串行执行: %s", exc)
    if _READ_ONLY_CLASSIFIER is None:
        return lambda name, arguments: False
    return _READ_ONLY_CLASSIFIER


class AgentHarness:
    """Agent 执行框架：状态化、可观测、可复用。

    将 run_tool_call_loop 的函数式实现包装为有状态对象，提供：
    - 运行时状态导出 (get_state)
    - 工具调用轨迹 (trace)
    - 暂停/恢复语义 (pause/resume)
    - 完整生命周期管理

    向后兼容：旧的 run_tool_call_loop / run_tool_loop_session 函数仍保留，
    内部委托给本类。新代码应直接使用 AgentHarness。
    """

    def __init__(
        self,
        initial_messages: List[Dict[str, Any]],
        model_call: Callable[..., Dict[str, Any]],
        tool_executor: Callable[
            [Dict[str, Any], str, int, List[Dict[str, Any]]], Dict[str, Any]
        ],
        *,
        tool_call_history: Optional[List[Dict[str, Any]]] = None,
        max_iterations: int = 50,
        max_consecutive_errors: int = 3,
        stop_event: Any = None,
        hooks: Optional[ToolLoopHooks] = None,
        # 单工具执行超时（秒）。None 表示不限制
        tool_timeout: Optional[float] = None,
        # 同一轮多个 tool_calls 是否并发执行
        parallel_tool_execution: bool = True,
        # 只读工具判定器（只读并发、写/副作用串行）
        read_only_classifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
    ):
        self._initial_messages = copy.deepcopy(initial_messages)
        self._model_call = model_call
        self._tool_executor = tool_executor
        self._tool_call_history = (
            copy.deepcopy(tool_call_history) if tool_call_history else None
        )
        self._max_iterations = max_iterations
        self._max_consecutive_errors = max_consecutive_errors
        self._stop_event = stop_event
        self._hooks = hooks
        self._tool_timeout = tool_timeout
        self._parallel_tool_execution = parallel_tool_execution
        self._read_only_classifier = read_only_classifier

        # 运行时状态（可观测）
        self._tool_messages: List[Dict[str, Any]] = []
        self._final_content: str = ""
        self._consecutive_errors: int = 0
        self._usage_total: Dict[str, int] = {}
        self._current_model_id: str = ""
        self._current_model_name: str = ""
        self._all_failover_events: List[Dict[str, Any]] = []
        self._trace: List[ToolCallTrace] = []

        # 生命周期标志
        self._iteration: int = 0
        self._stopped: bool = False
        self._paused: bool = False
        self._finished: bool = False
        self._prepared: bool = False

    # ------------------------------------------------------------------
    # 状态导出
    # ------------------------------------------------------------------
    @property
    def state(self) -> HarnessState:
        """当前运行时状态快照。"""
        return HarnessState(
            iteration=self._iteration,
            consecutive_errors=self._consecutive_errors,
            stopped=self._stopped,
            paused=self._paused,
            finished=self._finished,
            final_content=self._final_content,
            usage=dict(self._usage_total),
            model_id=self._current_model_id,
            model_name=self._current_model_name,
            failover_events=list(self._all_failover_events),
            tool_call_count=len(self._trace),
        )

    @property
    def trace(self) -> List[ToolCallTrace]:
        """工具调用轨迹列表（按时间顺序）。"""
        return list(self._trace)

    @property
    def tool_messages(self) -> List[Dict[str, Any]]:
        """当前消息历史（含工具调用）。"""
        return list(self._tool_messages)

    @property
    def final_content(self) -> str:
        return self._final_content

    def get_state(self) -> HarnessState:
        """显式获取状态快照（与 state 属性等价）。"""
        return self.state

    # ------------------------------------------------------------------
    # 生命周期控制
    # ------------------------------------------------------------------
    def pause(self) -> None:
        """标记暂停。下一次迭代前检查 _paused 会跳出循环。

        注意：本实现为协作式暂停，不会中断正在执行的 model_call / tool_executor。
        """
        self._paused = True

    def resume(self) -> ToolLoopResult:
        """恢复执行（重新进入循环）。"""
        if not self._paused:
            raise RuntimeError("Harness is not paused")
        if self._finished:
            raise RuntimeError("Harness already finished")
        self._paused = False
        return self._run_loop()

    def run(self) -> ToolLoopResult:
        """启动或继续执行。返回最终结果。"""
        if self._finished:
            # 已完成则直接返回最终状态
            return self._build_result()
        if self._paused:
            raise RuntimeError("Harness is paused; call resume() instead")
        if not self._prepared:
            # 首次运行：合并 tool_call_history
            self._tool_messages = apply_tool_call_history(
                self._initial_messages, self._tool_call_history
            )
            self._prepared = True
        return self._run_loop()

    # ------------------------------------------------------------------
    # 内部实现
    # ------------------------------------------------------------------
    def _build_result(self, **kwargs) -> ToolLoopResult:
        kwargs.setdefault("final_content", self._final_content)
        kwargs.setdefault("tool_messages", self._tool_messages)
        kwargs.setdefault("iterations", self._iteration)
        kwargs.setdefault("consecutive_errors", self._consecutive_errors)
        kwargs.setdefault("usage", dict(self._usage_total))
        kwargs.setdefault("model_id", self._current_model_id)
        kwargs.setdefault("model_name", self._current_model_name)
        kwargs.setdefault("failover_events", list(self._all_failover_events))
        return ToolLoopResult(**kwargs)

    def _run_loop(self) -> ToolLoopResult:
        """主循环实现（从 run_tool_call_loop 迁移，保持行为等价）。"""
        while self._iteration < self._max_iterations:
            # 暂停检查
            if self._paused:
                return self._build_result(stopped=False)

            # 停止事件检查
            if self._stop_event and self._stop_event.is_set():
                self._stopped = True
                return self._build_result(stopped=True)

            if self._hooks and self._hooks.on_iteration_start:
                self._hooks.on_iteration_start(self._iteration, self._tool_messages)

            try:
                response = self._model_call(
                    self._tool_messages, stop_event=self._stop_event
                )
            except StopIteration:
                self._stopped = True
                return self._build_result(stopped=True)
            except ToolLoopExit as exc:
                self._final_content = exc.final_content
                self._iteration += 1
                self._finished = True
                return self._build_result()
            except Exception as exc:
                raise ToolLoopModelError(exc, self._iteration) from exc

            # 提取模型追踪信息
            resp_model_id = response.pop("_model_id", None)
            resp_model_name = response.pop("_model_name", None)
            resp_failover = response.pop("_failover_events", None)
            if resp_model_id:
                self._current_model_id = resp_model_id
            if resp_model_name:
                self._current_model_name = resp_model_name
            if resp_failover:
                self._all_failover_events.extend(resp_failover)

            _merge_usage(self._usage_total, response.get("usage"))
            tool_calls = response.get("tool_calls") or []
            thinking_content = response.get("thinking_content") or response.get(
                "content", ""
            )

            if tool_calls:
                self._handle_tool_calls(
                    tool_calls, response, thinking_content
                )
                self._iteration += 1
                if self._finished:
                    return self._build_result()
                continue

            # 无工具调用：检查停止条件
            self._final_content = response.get("content", "")
            finish_reason = response.get("finish_reason", "")
            self._consecutive_errors = (
                0 if self._final_content else self._consecutive_errors + 1
            )

            if finish_reason == "content_filter":
                _log.warning("[AgentHarness] 内容被安全策略过滤 (content_filter)")
                if not self._final_content:
                    self._final_content = (
                        "抱歉，我的回答触发了内容安全过滤，请换个话题试试。"
                    )
                self._iteration += 1
                self._finished = True
                return self._build_result()

            if should_stop_tool_loop(
                self._final_content,
                finish_reason,
                self._iteration,
                self._max_iterations,
                self._consecutive_errors,
                self._max_consecutive_errors,
            ):
                if self._final_content.rstrip().endswith("break"):
                    self._final_content = self._final_content.rstrip()[:-5].rstrip()
                self._iteration += 1
                self._finished = True
                return self._build_result()

            self._tool_messages.append(
                {"role": "assistant", "content": self._final_content}
            )
            _log.info(
                "[AgentHarness] continue thinking, finish_reason=%s, iteration=%s",
                finish_reason,
                self._iteration,
            )
            self._iteration += 1

        # 达到最大迭代次数
        if not self._final_content:
            for message in reversed(self._tool_messages):
                if message.get("role") == "assistant":
                    self._final_content = message.get("content", "")
                    break

        self._finished = True
        return self._build_result()

    def _handle_tool_calls(
        self,
        tool_calls: List[Dict[str, Any]],
        response: Dict[str, Any],
        thinking_content: str,
    ) -> None:
        """处理本轮工具调用。

        - 支持 parallel_tool_execution=True 时用 ThreadPoolExecutor 并发执行同一轮多个 tool_calls
        - 支持 tool_timeout 设置单工具执行超时（None 不限制）
        - 超时或异常时返回 error result，不中断整个循环
        """
        # 构造 assistant 消息（含 tool_calls 索引）并追加到历史
        tool_call_entries = []
        for tool_call in tool_calls:
            entry = {
                "id": tool_call.get("id"),
                "type": "function",
                "function": {
                    "name": tool_call.get("name"),
                    "arguments": json.dumps(
                        tool_call.get("arguments", {}), ensure_ascii=False
                    ),
                },
            }
            sig = tool_call.get("_thought_signature")
            if sig:
                entry["_thought_signature"] = sig
            tool_call_entries.append(entry)
        assistant_message = {
            "role": "assistant",
            "content": response.get("content", ""),
            "tool_calls": tool_call_entries,
        }
        self._tool_messages.append(assistant_message)

        # 触发 on_tool_start hook（即使并发执行，hook 也按顺序同步触发以保留可观测性）
        for tool_call in tool_calls:
            if self._hooks and self._hooks.on_tool_start:
                self._hooks.on_tool_start(
                    tool_call, thinking_content, self._iteration, self._tool_messages
                )

        # 单工具场景：直接同步执行，避免线程池开销
        if len(tool_calls) == 1 or not self._parallel_tool_execution:
            for tool_call in tool_calls:
                self._execute_single_tool(tool_call, thinking_content)
            return

        # 多工具：按只读/写分批（只读并发，写串行）
        self._execute_tools_batched(tool_calls, thinking_content)

    def _is_read_only_call(self, tool_call: Dict[str, Any]) -> bool:
        """判断单个 tool_call 是否为只读（判定异常时按写工具处理）。"""
        classifier = self._read_only_classifier or _default_read_only_classifier()
        arguments = tool_call.get("arguments")
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}
        try:
            return bool(classifier(tool_call.get("name", ""), arguments))
        except Exception as exc:
            _log.debug(
                "[AgentHarness] 只读判定失败，按串行处理: %s (%s)",
                tool_call.get("name", ""),
                exc,
            )
            return False

    def _plan_tool_batches(
        self, tool_calls: List[Dict[str, Any]]
    ) -> List[List[Dict[str, Any]]]:
        """把本轮工具调用切成执行批次，保持原始顺序。

        - 连续的只读调用合并成一批并发执行；
        - 每个有副作用的调用独占一批，串行执行；
        - 批次之间严格按原顺序推进，写操作不会与前后调用乱序。
        """
        batches: List[List[Dict[str, Any]]] = []
        read_only_buffer: List[Dict[str, Any]] = []
        for tool_call in tool_calls:
            if self._is_read_only_call(tool_call):
                read_only_buffer.append(tool_call)
                continue
            if read_only_buffer:
                batches.append(read_only_buffer)
                read_only_buffer = []
            batches.append([tool_call])
        if read_only_buffer:
            batches.append(read_only_buffer)
        return batches

    def _execute_tools_batched(
        self, tool_calls: List[Dict[str, Any]], thinking_content: str
    ) -> None:
        """按批次执行工具：只读批并发，写工具逐个串行。"""
        for batch in self._plan_tool_batches(tool_calls):
            if self._finished:
                return
            if len(batch) == 1:
                self._execute_single_tool(batch[0], thinking_content)
            else:
                self._execute_tools_parallel(batch, thinking_content)

    def _execute_single_tool(
        self, tool_call: Dict[str, Any], thinking_content: str
    ) -> None:
        """同步执行单个工具调用（带超时）。"""
        try:
            if self._tool_timeout is not None:
                # 用 ThreadPoolExecutor 实现同步超时
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                    future = executor.submit(
                        self._tool_executor,
                        tool_call,
                        thinking_content,
                        self._iteration,
                        self._tool_messages,
                    )
                    try:
                        tool_result = future.result(timeout=self._tool_timeout)
                    except concurrent.futures.TimeoutError:
                        _log.warning(
                            "[AgentHarness] 工具 %s 执行超时 (%ss)",
                            tool_call.get("name", ""),
                            self._tool_timeout,
                        )
                        tool_result = {
                            "success": False,
                            "error": f"工具执行超时（{self._tool_timeout}s）",
                            "timeout": True,
                        }
            else:
                tool_result = self._tool_executor(
                    tool_call,
                    thinking_content,
                    self._iteration,
                    self._tool_messages,
                )
        except ToolLoopExit as exc:
            self._final_content = exc.final_content
            self._finished = True
            return
        except Exception as exc:
            _log.error(
                "[AgentHarness] 工具 %s 执行异常: %s",
                tool_call.get("name", ""),
                exc,
            )
            tool_result = {"success": False, "error": f"工具执行异常: {exc}"}

        self._record_tool_result(tool_call, tool_result, thinking_content)

    def _execute_tools_parallel(
        self, tool_calls: List[Dict[str, Any]], thinking_content: str
    ) -> None:
        """并发执行多个工具调用（带超时）。"""
        import concurrent.futures

        # 为每个工具创建独立副本，避免并发写入共享状态
        def _run_one(tc):
            try:
                return tc, self._tool_executor(
                    tc,
                    thinking_content,
                    self._iteration,
                    list(self._tool_messages),
                )
            except ToolLoopExit as exc:
                return tc, _ToolLoopExitMarker(exc)
            except Exception as exc:
                _log.error(
                    "[AgentHarness] 工具 %s 执行异常: %s",
                    tc.get("name", ""),
                    exc,
                )
                return tc, {"success": False, "error": f"工具执行异常: {exc}"}

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=min(len(tool_calls), 8)
        ) as executor:
            futures = [
                executor.submit(_run_one, tc) for tc in tool_calls
            ]
            results = []
            for fut in futures:
                try:
                    if self._tool_timeout is not None:
                        results.append(fut.result(timeout=self._tool_timeout))
                    else:
                        results.append(fut.result())
                except concurrent.futures.TimeoutError:
                    _log.warning(
                        "[AgentHarness] 并发工具执行超时 (%ss)",
                        self._tool_timeout,
                    )
                    results.append(({}, {"success": False, "error": "工具执行超时", "timeout": True}))

        # 按原始顺序记录结果（保持消息历史一致性）
        for tc, tool_result in results:
            if isinstance(tool_result, _ToolLoopExitMarker):
                self._final_content = tool_result.exc.final_content
                self._finished = True
                return
            self._record_tool_result(tc, tool_result, thinking_content)

    def _record_tool_result(
        self,
        tool_call: Dict[str, Any],
        tool_result: Dict[str, Any],
        thinking_content: str,
    ) -> None:
        """记录工具结果到消息历史和 trace。"""
        tool_history_message = None
        if self._hooks and self._hooks.on_tool_result:
            tool_history_message = self._hooks.on_tool_result(
                tool_call,
                tool_result,
                thinking_content,
                self._iteration,
                self._tool_messages,
            )

        if tool_history_message is None:
            tool_history_message = {
                "role": "tool",
                "tool_call_id": tool_call.get("id", ""),
                "name": tool_call.get("name", ""),
                "content": json.dumps(tool_result, ensure_ascii=False),
            }

        if tool_history_message:
            self._tool_messages.append(tool_history_message)

        # 记录工具调用轨迹（可观测性）
        trace = ToolCallTrace(
            iteration=self._iteration,
            tool_name=tool_call.get("name", ""),
            arguments=copy.deepcopy(tool_call.get("arguments", {})),
            result=copy.deepcopy(tool_result),
            success=bool(tool_result.get("success", True))
            if isinstance(tool_result, dict)
            else True,
            error=str(tool_result.get("error")) if isinstance(tool_result, dict) else None,
            tool_call_id=str(tool_call.get("id") or ""),
        )
        self._trace.append(trace)

        # 实时落库：每步追加，进程中断也能留下已完成的轨迹
        if self._hooks and self._hooks.on_tool_trace:
            try:
                self._hooks.on_tool_trace(trace)
            except Exception as exc:
                _log.debug("[AgentHarness] 工具轨迹落库回调失败: %s", exc)


# ---------------------------------------------------------------------------
# 用户人设（persona）跨通道解析
# ---------------------------------------------------------------------------
#
# 人设数据由 nbot/core/personas.py 管理（data/web/personas.json），会话通过
# persona_id 绑定。Web 端在 nbot/web/ai_service.py 里注入，QQ/TG/飞书 走这里
# 的通用解析，保证各通道行为一致；任何异常都降级为“不注入”，不阻断对话。


def _lookup_session_record(chat_request: Any) -> Optional[Dict[str, Any]]:
    """按 conversation_id 查运行时会话记录（各通道共用同一份会话存储）。

    懒加载 Web 服务实例；没有 Web 实例或读取失败时返回 None。
    """
    conversation_id = str(getattr(chat_request, "conversation_id", "") or "").strip()
    if not conversation_id:
        return None
    try:
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        store = getattr(server, "session_store", None) if server else None
        session = store.get_session(conversation_id) if store is not None else None
        return session if isinstance(session, dict) else None
    except Exception as exc:  # 核心管道不依赖 Web，这里失败只代表没有人设
        _log.debug("[Persona] 会话记录查询失败: %s", exc)
        return None


def resolve_user_persona(
    chat_request: Any,
    session: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """解析当前会话的人设：persona_id 优先，失效/未绑定回退全局默认。"""
    session_data = session if isinstance(session, dict) else _lookup_session_record(chat_request)
    if not isinstance(session_data, dict):
        return None
    try:
        from nbot.core.personas import session_persona

        return session_persona(session_data)
    except Exception as exc:
        _log.debug("[Persona] 人设解析失败: %s", exc)
        return None


def apply_user_persona_metadata(
    chat_request: Any,
    metadata: Optional[Dict[str, Any]] = None,
    session: Optional[Dict[str, Any]] = None,
) -> bool:
    """把人设写入请求/管道元数据，返回是否命中了人设。

    - ``user_persona``：供 AIPipeline._apply_user_persona 渲染成 prompt 块；
    - ``macro_user_name``：{{user}} 的默认名称（不覆盖调用方已设置的值）。
    """
    target = metadata if isinstance(metadata, dict) else {}
    if target.get("user_persona"):
        return True
    persona = resolve_user_persona(chat_request, session)
    if not persona:
        return False
    target.setdefault("user_persona", persona)
    name = str(persona.get("name") or "").strip()
    if name:
        target.setdefault("macro_user_name", name)
    return True


@dataclass
class PreparedChatContext:
    messages: List[Dict[str, Any]] = field(default_factory=list)
    tool_call_history: Optional[List[Dict[str, Any]]] = None


class AgentService:
    def __init__(self):
        self._handlers: Dict[str, Callable[..., Any]] = {}

    def register_handler(self, channel: str, handler: Callable[..., Any]) -> None:
        self._handlers[channel] = handler

    def process(
        self,
        chat_request: ChatRequest,
        *,
        adapter: Optional["BaseChannelAdapter"] = None,
        **kwargs,
    ) -> ChatResponse:
        try:
            from nbot.message_filter import message_filter

            filter_result = message_filter.filter_content(
                chat_request.content,
                channel=chat_request.channel,
                session_id=chat_request.conversation_id,
            )
            if filter_result.get("blocked"):
                return ChatResponse(
                    final_content="当前内容被过滤",
                    metadata={
                        "filtered": True,
                        "filter_rule_count": len(filter_result.get("rules", [])),
                    },
                )
            if filter_result.get("filtered"):
                chat_request.content = filter_result.get("content", "")
                chat_request.metadata = dict(chat_request.metadata or {})
                chat_request.metadata["filtered"] = True
                chat_request.metadata["filter_rule_count"] = len(
                    filter_result.get("rules", [])
                )
        except Exception as filter_err:
            _log.warning("[消息过滤] AgentService 检查异常: %s", filter_err)

        # === 用户人设（全通道一致） ===
        # 会话绑定了 persona_id 或存在全局默认人设时，把说明带进本次请求；
        # 解析失败只降级为不注入，不影响对话。
        try:
            chat_request.metadata = dict(chat_request.metadata or {})
            apply_user_persona_metadata(chat_request, chat_request.metadata)
        except Exception as persona_err:
            _log.debug("[Persona] 请求级注入失败（忽略）: %s", persona_err)

        # === 路由到频道处理器 ===
        handler = self._handlers.get(chat_request.channel) or get_channel_handler(
            chat_request.channel
        )
        if not handler:
            raise ValueError(f"No agent handler registered for channel: {chat_request.channel}")

        if adapter is not None:
            kwargs.setdefault("adapter", adapter)

        result = handler(chat_request, **kwargs)
        return normalize_chat_response(result)


def normalize_chat_response(result: Any) -> ChatResponse:
    if isinstance(result, ChatResponse):
        return result

    if isinstance(result, str):
        return ChatResponse(final_content=result)

    if isinstance(result, dict):
        if "assistant_message" in result or "final_content" in result or "error" in result:
            return ChatResponse(
                final_content=result.get("final_content", ""),
                assistant_message=result.get("assistant_message"),
                tool_trace=list(result.get("tool_trace", result.get("tool_call_history", []))),
                can_continue=bool(result.get("can_continue", False)),
                usage=dict(result.get("usage", {})),
                error=result.get("error"),
                metadata=dict(result.get("metadata", {})),
            )
        if "content" in result:
            return ChatResponse(final_content=str(result.get("content", "")))

    raise TypeError(f"Unsupported chat response type: {type(result)!r}")


def is_continue_request(
    user_content: str, continue_tokens: Optional[Sequence[str]] = None
) -> bool:
    if continue_tokens is None:
        tokens = ("\u7ee7\u7eed", "\u7ee7\u7eed\u6267\u884c", "continue")
    else:
        tokens = tuple(continue_tokens)
    return (user_content or "").strip().lower() in {
        token.lower() for token in tokens
    }


def restore_continue_messages(
    messages: List[Dict[str, Any]],
    user_content: str,
    continue_tokens: Optional[Sequence[str]] = None,
) -> tuple[List[Dict[str, Any]], Optional[List[Dict[str, Any]]]]:
    working_messages = copy.deepcopy(messages)
    if not working_messages or not is_continue_request(user_content, continue_tokens):
        return working_messages, None

    marker_message = None

    if (
        len(working_messages) >= 2
        and working_messages[-1].get("role") == "user"
        and str(working_messages[-1].get("content", "")).strip() == user_content.strip()
        and working_messages[-2].get("can_continue")
        and working_messages[-2].get("tool_call_history")
    ):
        marker_message = working_messages[-2]
        working_messages = working_messages[:-2]
    elif (
        working_messages[-1].get("can_continue")
        and working_messages[-1].get("tool_call_history")
    ):
        marker_message = working_messages[-1]
        working_messages = working_messages[:-1]

    if not marker_message:
        return copy.deepcopy(messages), None

    return working_messages, copy.deepcopy(marker_message["tool_call_history"])


def trim_messages(
    messages: List[Dict[str, Any]],
    max_history: int = 20,
    max_total_chars: int = 30000,
) -> List[Dict[str, Any]]:
    trimmed_messages = copy.deepcopy(messages)

    # 仅按 token/字符数裁剪，不再限制消息条数
    total_chars = sum(len(str(msg.get("content", ""))) for msg in trimmed_messages)
    if total_chars <= max_total_chars:
        return trimmed_messages

    # 从最早的消息开始移除（保留 system 消息和最近的消息）
    system_message = (
        trimmed_messages[0]
        if trimmed_messages and trimmed_messages[0].get("role") == "system"
        else None
    )
    non_system = trimmed_messages[1:] if system_message else trimmed_messages

    # 逐步移除最早的非 system 消息，直到总字符数在预算内
    while non_system and total_chars > max_total_chars:
        removed = non_system.pop(0)
        total_chars -= len(str(removed.get("content", "")))

    if system_message:
        return [system_message] + non_system
    return non_system


def inject_knowledge_context(
    messages: List[Dict[str, Any]], knowledge_text: str
) -> List[Dict[str, Any]]:
    updated_messages = copy.deepcopy(messages)
    if not knowledge_text:
        return updated_messages

    if updated_messages and updated_messages[0].get("role") == "system":
        updated_messages[0]["content"] += f"\n\n{knowledge_text}"
    else:
        updated_messages.insert(0, {"role": "system", "content": knowledge_text})
    return updated_messages


def prepare_chat_context(
    messages: List[Dict[str, Any]],
    user_content: str,
    *,
    knowledge_text: str = "",
    max_history: int = 20,
    max_total_chars: int = 30000,
    continue_tokens: Optional[Sequence[str]] = None,
) -> PreparedChatContext:
    restored_messages, tool_call_history = restore_continue_messages(
        messages, user_content, continue_tokens
    )
    expanded_messages = expand_hidden_tool_history(restored_messages)
    prepared_messages = inject_knowledge_context(
        trim_messages(
            expanded_messages,
            max_total_chars=max_total_chars,
        ),
        knowledge_text,
    )
    return PreparedChatContext(
        messages=prepared_messages,
        tool_call_history=tool_call_history,
    )


def apply_tool_call_history(
    messages: List[Dict[str, Any]],
    tool_call_history: Optional[List[Dict[str, Any]]],
) -> List[Dict[str, Any]]:
    merged_messages = copy.deepcopy(messages)
    if tool_call_history:
        merged_messages.extend(copy.deepcopy(tool_call_history))
    return merged_messages


def expand_hidden_tool_history(
    messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    expanded_messages: List[Dict[str, Any]] = []
    for message in copy.deepcopy(messages):
        hidden_tool_history = message.pop("tool_call_history", None)
        expanded_messages.append(message)
        if not isinstance(hidden_tool_history, list):
            continue
        for hidden_message in hidden_tool_history:
            if not isinstance(hidden_message, dict):
                continue
            if hidden_message.get("role") not in ("assistant", "tool"):
                continue
            expanded_messages.append(copy.deepcopy(hidden_message))
    return expanded_messages


def extract_tool_call_history(
    messages: List[Dict[str, Any]]
) -> List[Dict[str, Any]]:
    return [
        copy.deepcopy(message)
        for message in messages
        if message.get("role") in ("assistant", "tool")
    ]


def _legacy_build_continue_chat_response(
    final_content: str = "【生成已停止 - 工具调用记录已保存，回复「继续」可继续执行】",
    *,
    tool_messages: Optional[List[Dict[str, Any]]] = None,
    tool_trace: Optional[List[Dict[str, Any]]] = None,
) -> ChatResponse:
    if tool_trace is None:
        tool_trace = extract_tool_call_history(tool_messages or [])
    return ChatResponse(
        final_content=final_content,
        can_continue=True,
        tool_trace=tool_trace,
    )


def clean_response_content(content: str) -> str:
    cleaned = (content or "").strip()
    if cleaned.startswith("```json"):
        cleaned = cleaned[7:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    elif cleaned.startswith("```"):
        cleaned = cleaned[3:]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
    return cleaned.strip().lstrip()


def extract_display_text(content: str) -> str:
    cleaned = clean_response_content(content)
    if cleaned and cleaned.startswith("{"):
        try:
            fixed = (
                cleaned.replace(chr(8220), '"')
                .replace(chr(8221), '"')
                .replace(chr(65306), ":")
            )
            parsed = json.loads(fixed)
            if isinstance(parsed, dict) and "msg" in parsed:
                return str(parsed["msg"])
        except Exception:
            return cleaned
    return cleaned


def should_stop_tool_loop(
    final_content: str,
    finish_reason: str,
    iteration: int,
    max_iterations: int,
    consecutive_errors: int,
    max_consecutive_errors: int = 3,
) -> bool:
    # content_filter 表示内容被安全策略过滤，应立即停止循环
    if finish_reason == "content_filter":
        return True
    return (
        finish_reason == "stop"
        or (not finish_reason and bool(final_content))
        or final_content.rstrip().endswith("break")
        or iteration >= max_iterations - 1
        or consecutive_errors >= max_consecutive_errors
    )


def _merge_usage(target: Dict[str, int], usage: Any) -> None:
    if not isinstance(usage, dict) or not usage:
        return
    try:
        from nbot.core.model_adapter import normalize_usage_dict

        usage = normalize_usage_dict(usage)
    except Exception:
        usage = dict(usage)

    for key, value in usage.items():
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount:
            target[key] = int(target.get(key, 0)) + amount


def run_tool_call_loop(
    initial_messages: List[Dict[str, Any]],
    model_call: Callable[..., Dict[str, Any]],
    tool_executor: Callable[
        [Dict[str, Any], str, int, List[Dict[str, Any]]], Dict[str, Any]
    ],
    *,
    max_iterations: int = 50,
    max_consecutive_errors: int = 3,
    stop_event=None,
    hooks: Optional[ToolLoopHooks] = None,
    tool_timeout: Optional[float] = None,
    parallel_tool_execution: bool = True,
    read_only_classifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
) -> ToolLoopResult:
    """[已废弃] 函数式入口，内部委托给 AgentHarness。保留以保持向后兼容。"""
    harness = AgentHarness(
        initial_messages=initial_messages,
        model_call=model_call,
        tool_executor=tool_executor,
        max_iterations=max_iterations,
        max_consecutive_errors=max_consecutive_errors,
        stop_event=stop_event,
        hooks=hooks,
        tool_timeout=tool_timeout,
        parallel_tool_execution=parallel_tool_execution,
        read_only_classifier=read_only_classifier,
    )
    # 旧 API 不合并 tool_call_history（旧函数签名不含该参数），等价于直接 run
    harness._tool_messages = copy.deepcopy(initial_messages)
    harness._prepared = True
    return harness.run()


def run_tool_loop_session(session: ToolLoopSession) -> ToolExecutionResult:
    """[已废弃] 函数式入口，内部委托给 AgentHarness。保留以保持向后兼容。"""
    harness = AgentHarness(
        initial_messages=session.initial_messages,
        model_call=session.model_call,
        tool_executor=session.tool_executor,
        tool_call_history=session.tool_call_history,
        max_iterations=session.max_iterations,
        max_consecutive_errors=session.max_consecutive_errors,
        stop_event=session.stop_event,
        hooks=session.hooks,
        tool_timeout=session.tool_timeout,
        parallel_tool_execution=session.parallel_tool_execution,
        read_only_classifier=session.read_only_classifier,
    )
    loop_result = harness.run()
    return ToolExecutionResult(
        loop_result=loop_result,
        prepared_messages=harness.tool_messages,
    )


def execute_tool_loop_session(
    initial_messages: List[Dict[str, Any]],
    model_call: Callable[..., Dict[str, Any]],
    tool_executor: Callable[
        [Dict[str, Any], str, int, List[Dict[str, Any]]], Dict[str, Any]
    ],
    *,
    tool_call_history: Optional[List[Dict[str, Any]]] = None,
    max_iterations: int = 50,
    max_consecutive_errors: int = 3,
    stop_event=None,
    hooks: Optional[ToolLoopHooks] = None,
    tool_timeout: Optional[float] = None,
    parallel_tool_execution: bool = True,
    read_only_classifier: Optional[Callable[[str, Dict[str, Any]], bool]] = None,
) -> ToolExecutionResult:
    """[已废弃] 便捷入口，内部委托给 AgentHarness。保留以保持向后兼容。"""
    return run_tool_loop_session(
        ToolLoopSession(
            initial_messages=initial_messages,
            model_call=model_call,
            tool_executor=tool_executor,
            tool_call_history=tool_call_history,
            max_iterations=max_iterations,
            max_consecutive_errors=max_consecutive_errors,
            stop_event=stop_event,
            hooks=hooks,
            tool_timeout=tool_timeout,
            parallel_tool_execution=parallel_tool_execution,
            read_only_classifier=read_only_classifier,
        )
    )


def resolve_loop_final_content(
    loop_result: ToolLoopResult,
    default_content: str = "",
) -> str:
    if loop_result.final_content:
        return loop_result.final_content

    if loop_result.tool_messages:
        last_msg = loop_result.tool_messages[-1]
        if last_msg.get("role") == "assistant":
            return last_msg.get("content", default_content)

    return default_content


def build_continue_chat_response(
    final_content: str = "\u3010\u751f\u6210\u5df2\u505c\u6b62 - \u5de5\u5177\u8c03\u7528\u8bb0\u5f55\u5df2\u4fdd\u5b58\uff0c\u56de\u590d\u300c\u7ee7\u7eed\u300d\u53ef\u7ee7\u7eed\u6267\u884c\u3011",
    *,
    tool_messages: Optional[List[Dict[str, Any]]] = None,
    tool_trace: Optional[List[Dict[str, Any]]] = None,
) -> ChatResponse:
    if tool_trace is None:
        tool_trace = extract_tool_call_history(tool_messages or [])
    return ChatResponse(
        final_content=final_content,
        can_continue=True,
        tool_trace=tool_trace,
    )

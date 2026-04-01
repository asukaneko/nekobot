import copy
import json
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence

_log = logging.getLogger(__name__)

DEFAULT_CONTINUE_TOKENS = ("\u7ee7\u7eed", "\u7ee7\u7eed\u6267\u884c", "continue")


class ToolLoopExit(Exception):
    def __init__(self, final_content: str):
        super().__init__(final_content)
        self.final_content = final_content


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


@dataclass
class ToolLoopResult:
    final_content: str = ""
    tool_messages: List[Dict[str, Any]] = field(default_factory=list)
    stopped: bool = False
    iterations: int = 0
    consecutive_errors: int = 0


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

    if len(trimmed_messages) > max_history:
        trimmed_messages = [trimmed_messages[0]] + trimmed_messages[-max_history:]

    total_chars = sum(len(str(msg.get("content", ""))) for msg in trimmed_messages)
    if total_chars <= max_total_chars:
        return trimmed_messages

    system_message = (
        trimmed_messages[0]
        if trimmed_messages and trimmed_messages[0].get("role") == "system"
        else None
    )
    recent_messages = trimmed_messages[-max_history:] if trimmed_messages else []
    if system_message:
        return [system_message] + recent_messages
    return recent_messages


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


def should_stop_tool_loop(
    final_content: str,
    finish_reason: str,
    iteration: int,
    max_iterations: int,
    consecutive_errors: int,
    max_consecutive_errors: int = 3,
) -> bool:
    return (
        finish_reason == "stop"
        or (not finish_reason and bool(final_content))
        or final_content.rstrip().endswith("break")
        or iteration >= max_iterations - 1
        or consecutive_errors >= max_consecutive_errors
    )


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
) -> ToolLoopResult:
    tool_messages = copy.deepcopy(initial_messages)
    final_content = ""
    consecutive_errors = 0

    for iteration in range(max_iterations):
        if stop_event and stop_event.is_set():
            return ToolLoopResult(
                tool_messages=tool_messages,
                stopped=True,
                iterations=iteration,
                consecutive_errors=consecutive_errors,
            )

        if hooks and hooks.on_iteration_start:
            hooks.on_iteration_start(iteration, tool_messages)

        try:
            response = model_call(tool_messages, stop_event=stop_event)
        except StopIteration:
            return ToolLoopResult(
                tool_messages=tool_messages,
                stopped=True,
                iterations=iteration,
                consecutive_errors=consecutive_errors,
            )
        except ToolLoopExit as exc:
            return ToolLoopResult(
                final_content=exc.final_content,
                tool_messages=tool_messages,
                iterations=iteration + 1,
                consecutive_errors=consecutive_errors,
            )

        tool_calls = response.get("tool_calls") or []
        thinking_content = response.get("thinking_content") or response.get("content", "")

        if tool_calls:
            assistant_message = {
                "role": "assistant",
                "content": response.get("content", ""),
                "tool_calls": [
                    {
                        "id": tool_call.get("id"),
                        "type": "function",
                        "function": {
                            "name": tool_call.get("name"),
                            "arguments": json.dumps(
                                tool_call.get("arguments", {}), ensure_ascii=False
                            ),
                        },
                    }
                    for tool_call in tool_calls
                ],
            }
            tool_messages.append(assistant_message)

            for tool_call in tool_calls:
                if hooks and hooks.on_tool_start:
                    hooks.on_tool_start(tool_call, thinking_content, iteration, tool_messages)

                try:
                    tool_result = tool_executor(
                        tool_call, thinking_content, iteration, tool_messages
                    )
                except ToolLoopExit as exc:
                    return ToolLoopResult(
                        final_content=exc.final_content,
                        tool_messages=tool_messages,
                        iterations=iteration + 1,
                        consecutive_errors=consecutive_errors,
                    )

                tool_history_message = None
                if hooks and hooks.on_tool_result:
                    tool_history_message = hooks.on_tool_result(
                        tool_call,
                        tool_result,
                        thinking_content,
                        iteration,
                        tool_messages,
                    )

                if tool_history_message is None:
                    tool_history_message = {
                        "role": "tool",
                        "tool_call_id": tool_call.get("id", ""),
                        "content": json.dumps(tool_result, ensure_ascii=False),
                    }

                if tool_history_message:
                    tool_messages.append(tool_history_message)

            continue

        final_content = response.get("content", "")
        finish_reason = response.get("finish_reason", "")
        consecutive_errors = 0 if final_content else consecutive_errors + 1

        if should_stop_tool_loop(
            final_content,
            finish_reason,
            iteration,
            max_iterations,
            consecutive_errors,
            max_consecutive_errors,
        ):
            if final_content.rstrip().endswith("break"):
                final_content = final_content.rstrip()[:-5].rstrip()
            return ToolLoopResult(
                final_content=final_content,
                tool_messages=tool_messages,
                iterations=iteration + 1,
                consecutive_errors=consecutive_errors,
            )

        tool_messages.append({"role": "assistant", "content": final_content})
        _log.info(
            "[AgentLoop] continue thinking, finish_reason=%s, iteration=%s",
            finish_reason,
            iteration,
        )

    if not final_content:
        for message in reversed(tool_messages):
            if message.get("role") == "assistant":
                final_content = message.get("content", "")
                break

    return ToolLoopResult(
        final_content=final_content,
        tool_messages=tool_messages,
        iterations=max_iterations,
        consecutive_errors=consecutive_errors,
    )

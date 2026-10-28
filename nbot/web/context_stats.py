"""上下文占比分类统计（7 类统一口径）

供 ``GET /api/sessions/<id>/context-stats`` 与前端圆环/面板共用同一套会计口径：
类别行相加必须等于总量，关闭工具后「工具定义」占比随之下降。

行分类：
- ``system_prompt``：首条主 system 提示词
- ``tool_definitions``：会话启用工具时的工具定义 JSON（仅 agent 会话）
- ``compression_summary``：``summary_*`` 自动压缩摘要
- ``user_messages`` / ``assistant_messages``：可见对话消息
- ``tool_traces``：``role=tool`` 的消息 + assistant 上挂的工具轨迹
- ``other``：归档分隔行等其余 system/未知角色消息
"""

import json
import re
from typing import Any, Dict

DEFAULT_MAX_CONTEXT = 100000


def estimate_text_tokens(text: str) -> int:
    """对齐前端 ``estimateTextTokens``：CJK×1.05 + 词元×0.75。"""
    value = str(text or "")
    if not value:
        return 0
    cjk = 0
    chars = []
    for ch in value:
        if "\u3400" <= ch <= "\u9fff" or "\uf900" <= ch <= "\ufaff":
            cjk += 1
            chars.append(" ")
        else:
            chars.append(ch)
    words = len(re.findall(r"[A-Za-z0-9_]+|[^\sA-Za-z0-9_]", "".join(chars)))
    return int(cjk * 1.05 + words * 0.75) + 1


def message_content(msg: Dict[str, Any]) -> str:
    """消息参与计费的文本（正文 + 角色名 + 附件元信息）。"""
    content = msg.get("content") if isinstance(msg.get("content"), str) else ""
    parts = [str(content or ""), str(msg.get("role") or "")]
    attachments = msg.get("attachments")
    if isinstance(attachments, list):
        for file in attachments:
            if isinstance(file, dict):
                parts.append(
                    " ".join(
                        str(file.get(k) or "")
                        for k in (
                            "name",
                            "filename",
                            "type",
                            "mime_type",
                            "extracted_text",
                            "content",
                        )
                    )
                )
    return "\n".join(part for part in parts if part)


def empty_categories() -> Dict[str, Dict[str, Any]]:
    return {
        "system_prompt": {"label": "系统提示词", "chars": 0, "tokens": 0, "messages": 0},
        "tool_definitions": {"label": "工具定义", "chars": 0, "tokens": 0, "messages": 0},
        "compression_summary": {"label": "压缩摘要", "chars": 0, "tokens": 0, "messages": 0},
        "user_messages": {"label": "用户消息", "chars": 0, "tokens": 0, "messages": 0},
        "assistant_messages": {"label": "助手消息", "chars": 0, "tokens": 0, "messages": 0},
        "tool_traces": {"label": "工具轨迹", "chars": 0, "tokens": 0, "messages": 0},
        "other": {"label": "其他", "chars": 0, "tokens": 0, "messages": 0},
    }


def build_context_stats(
    session: Dict[str, Any],
    max_context_length: int = DEFAULT_MAX_CONTEXT,
    tools: Any = None,
) -> Dict[str, Any]:
    """计算上下文分类统计。

    Args:
        session: 会话字典（需包含 messages / session_mode 等）
        max_context_length: 模型上限，用于前端百分比分母
        tools: 可选的工具定义列表（传入则计入「工具定义」，便于测试与复用）
    """
    from nbot.core.session_store import visible_messages

    categories = empty_categories()

    def _add(category: str, text: str, message_count: int = 0) -> None:
        text = str(text or "")
        if not text and not message_count:
            return
        bucket = categories[category]
        bucket["chars"] += len(text)
        bucket["tokens"] += estimate_text_tokens(text)
        bucket["messages"] += message_count

    messages = visible_messages(session.get("messages", []))
    for index, msg in enumerate(messages):
        role = str(msg.get("role") or "")
        msg_id = str(msg.get("id") or "")
        is_summary = role == "system" and msg_id.startswith("summary_")
        if is_summary:
            _add("compression_summary", message_content(msg), 1)
        elif role == "system":
            if index == 0 and not msg_id.startswith("archive_divider_"):
                _add("system_prompt", message_content(msg), 1)
            else:
                _add("other", message_content(msg), 1)
        elif role == "user":
            _add("user_messages", message_content(msg), 1)
        elif role == "assistant":
            _add("assistant_messages", message_content(msg), 1)
            trace = msg.get("tool_call_history")
            if isinstance(trace, list) and trace:
                trace_chars = sum(
                    len(json.dumps(entry, ensure_ascii=False, default=str))
                    for entry in trace
                    if entry
                )
                _add("tool_traces", "x" * trace_chars, 0)
        elif role == "tool":
            _add("tool_traces", message_content(msg), 1)
        else:
            _add("other", message_content(msg), 1)

    # 工具定义：仅 agent 会话；工具关闭时自然为 0
    if session.get("session_mode") == "agent":
        if tools is None:
            try:
                from nbot.services.tools import get_enabled_tools

                tools = get_enabled_tools()
            except Exception:
                tools = []
        if tools:
            tools_chars = len(json.dumps(tools, ensure_ascii=False, default=str))
            _add("tool_definitions", "x" * tools_chars, 0)

    total = {
        "chars": sum(item["chars"] for item in categories.values()),
        "tokens": sum(item["tokens"] for item in categories.values()),
        "messages": sum(item["messages"] for item in categories.values()),
    }

    return {
        "categories": categories,
        "total": total,
        "max_context_length": int(max_context_length or DEFAULT_MAX_CONTEXT),
    }

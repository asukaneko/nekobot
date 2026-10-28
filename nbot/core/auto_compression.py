"""
自动上下文压缩引擎

当会话上下文超过模型上下文预算的阈值（默认 80%）时，将早期消息
滚动合并为一份结构化摘要，替换原始历史（最近若干条保留）。

设计要点：
- 与通道无关：消息进、消息出，模型调用由调用方注入（chat_complete）
- 结构化摘要模板：目标 / 重要细节 / 工作状态 / 下一步 / 相关文件
- 滚动合并旧摘要（冲突以新历史为准），总长限制 2000 字
- 失败时抛出异常由调用方决定按未压缩继续
"""

import logging
import re
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

_log = logging.getLogger(__name__)

DEFAULT_TRIGGER_RATIO = 0.8
SUMMARY_MAX_CHARS = 2000
SUMMARY_MARKER = "【对话总结】"

# 结构化摘要模板的段落说明
_SUMMARY_TEMPLATE = (
    "请将以上对话融合为一份结构化总结，严格控制在{max_chars}字以内，"
    "使用以下小节组织（与本次对话无关的小节可省略）：\n"
    "目标：对话正在推进的主要事情\n"
    "重要细节：人物、地点、承诺、数字等不可丢失的关键信息\n"
    "工作状态：当前进行中的任务/情绪/关系状态\n"
    "下一步：悬而未决的事项或可能的走向\n"
    "相关文件：对话中提到的文件/链接（如有）"
)

# 旧摘要 + 新历史的滚动合并提示词
_ROLLING_TEMPLATE = (
    "以下是上一轮对话总结，以及总结之后的新对话。"
    "请将它们融合成一份新的总结：保留旧总结中仍然重要的信息，"
    "与新对话内容整合；新旧信息冲突时，以新对话为准。\n\n"
    "{body}\n\n" + _SUMMARY_TEMPLATE
)

_FRESH_TEMPLATE = (
    "请总结以下对话，供 AI 在后续对话中恢复上下文：\n\n"
    "{body}\n\n" + _SUMMARY_TEMPLATE
)


def count_context_chars(messages: List[Dict[str, Any]]) -> int:
    """统计非 system 消息的上下文总字符数。"""
    return sum(
        len(str(msg.get("content") or ""))
        for msg in messages or []
        if isinstance(msg, dict) and msg.get("role") != "system"
    )


def is_summary_message(msg: Dict[str, Any]) -> bool:
    return (
        isinstance(msg, dict)
        and msg.get("role") == "system"
        and str(msg.get("id") or "").startswith("summary_")
    )


def find_existing_summary(messages: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    for msg in messages or []:
        if is_summary_message(msg):
            return msg
    return None


def clean_summary_text(text: str) -> str:
    """清理模型输出中的嵌套标记与加粗。"""
    summary = str(text or "").strip()
    summary = re.sub(r"\*\*?" + re.escape(SUMMARY_MARKER) + r"\*\*?\s*", "", summary)
    summary = re.sub(re.escape(SUMMARY_MARKER) + r"\s*", "", summary)
    return summary.strip()


def build_summary_prompt(
    messages_to_compress: List[Dict[str, Any]],
    old_summary_text: str = "",
) -> str:
    """构建结构化摘要提示词；有旧摘要时走滚动合并。"""
    lines = []
    if old_summary_text:
        lines.append(f"上一轮总结：\n{old_summary_text}")
    body = "\n".join(
        f"[{msg.get('role', 'user')}]: {str(msg.get('content') or '')[:500]}"
        for msg in messages_to_compress
        if isinstance(msg, dict) and msg.get("content")
    )
    if old_summary_text:
        lines.append(f"总结之后的新对话：\n{body}")
        template = _ROLLING_TEMPLATE
    else:
        lines.append(body)
        template = _FRESH_TEMPLATE
    return template.format(
        body="\n\n".join(lines), max_chars=SUMMARY_MAX_CHARS
    )


def apply_compression(
    messages: List[Dict[str, Any]],
    summary: str,
    *,
    keep_recent: int = 5,
) -> List[Dict[str, Any]]:
    """应用压缩结果：保留 system 与最近 keep_recent 条，其余替换为摘要消息。

    摘要长度超限时截断到 SUMMARY_MAX_CHARS。
    """
    summary = clean_summary_text(summary)[:SUMMARY_MAX_CHARS]
    non_system = [m for m in messages or [] if isinstance(m, dict) and m.get("role") != "system"]
    system_msgs = [m for m in messages or [] if isinstance(m, dict) and m.get("role") == "system"]

    keep = min(keep_recent, len(non_system))
    recent = non_system[-keep:] if keep else []

    new_messages: List[Dict[str, Any]] = []
    # 主 system 提示词在前，旧摘要消息随后
    for msg in system_msgs:
        if not is_summary_message(msg):
            new_messages.append(msg)
    if summary:
        new_messages.append(
            {
                "id": f"summary_{int(time.time())}",
                "role": "system",
                "content": f"{SUMMARY_MARKER}{summary}",
                "timestamp": time.time(),
            }
        )
    new_messages.extend(recent)
    return new_messages


def auto_compress(
    messages: List[Dict[str, Any]],
    *,
    chat_complete: Callable[[List[Dict[str, Any]]], Any],
    max_context_chars: int,
    trigger_ratio: float = DEFAULT_TRIGGER_RATIO,
    min_messages: int = 10,
    keep_recent: int = 5,
    cooldown_seconds: int = 60,
    last_compress_ts: float = 0.0,
    on_usage: Optional[Callable[[Any, str], None]] = None,
    model_label: str = "",
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """达到阈值时自动压缩上下文。

    Args:
        messages: 当前上下文消息列表（含 system）
        chat_complete: 模型补全函数，接收 messages，返回含
            choices[0].message.content / usage 的响应对象
        max_context_chars: 模型上下文字符预算
        trigger_ratio: 触发比例（默认 80%）
        min_messages: 触发所需最少消息条数（已压缩过的会话减半）
        keep_recent: 压缩后保留的最近消息条数
        cooldown_seconds: 距上次自动压缩的最小间隔
        last_compress_ts: 上次自动压缩时间戳
        on_usage: token 用量记录回调 (usage, model)
        model_label: 用量记录用模型名

    Returns:
        (消息列表, 信息) —— 未触发/失败时返回原列表；info 含
        compressed / reason / error / summary / compressed_count。
    """
    info: Dict[str, Any] = {
        "compressed": False,
        "reason": "",
        "error": "",
        "summary": "",
        "compressed_count": 0,
    }
    msgs = list(messages or [])
    old_summary = find_existing_summary(msgs)
    min_required = (min_messages // 2) if old_summary else min_messages

    if len(msgs) < min_required:
        info["reason"] = "消息数量不足"
        return msgs, info
    if cooldown_seconds > 0 and (time.time() - last_compress_ts) < cooldown_seconds:
        info["reason"] = "冷却期内"
        return msgs, info

    threshold = int(max(1000, max_context_chars) * trigger_ratio)
    total_chars = count_context_chars(msgs)
    if total_chars <= threshold:
        info["reason"] = "未达阈值"
        return msgs, info

    keep = min(keep_recent, len(msgs) - 2)
    compress_start = 0
    compress_end = len(msgs) - keep
    messages_to_compress = [
        m for m in msgs[compress_start:compress_end] if not is_summary_message(m)
    ]
    if not messages_to_compress:
        info["reason"] = "没有早期消息需要压缩"
        return msgs, info

    old_summary_text = ""
    if old_summary:
        old_summary_text = str(old_summary.get("content") or "").replace(SUMMARY_MARKER, "").strip()
    prompt = build_summary_prompt(messages_to_compress, old_summary_text)

    response = chat_complete([{"role": "user", "content": prompt}])
    if on_usage is not None:
        try:
            on_usage(getattr(response, "usage", None), model_label)
        except Exception as exc:
            _log.debug("[AutoCompress] 用量记录失败: %s", exc)

    summary = clean_summary_text(getattr(response.choices[0].message, "content", "") or "")
    if not summary:
        info["error"] = "摘要为空"
        return msgs, info

    new_messages = apply_compression(msgs, summary, keep_recent=keep_recent)
    info.update(
        compressed=True,
        summary=summary,
        compressed_count=len(messages_to_compress),
    )
    _log.info(
        "[AutoCompress] 压缩 %d 条消息（%d 字符 → 摘要 %d 字符）",
        len(messages_to_compress),
        total_chars,
        len(summary),
    )
    return new_messages, info

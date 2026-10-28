"""
ask_user_question 工具 - Agent 向用户提问并等待回答

流程：
1. AI 调用 ask_user_question 工具 → 存储待回答问题（request_id）
2. 管道将问题作为回复展示给用户；Web 端同时收到弹窗事件
3. 用户下一条消息由 handle_pending_user_answer 捕获，转换为
   带问题上下文的答案消息，AI 据此继续（等待恢复）
"""

import logging
import time
import uuid
from typing import Any, Dict, Optional

from nbot.services.tool_registry import register_tool

_log = logging.getLogger(__name__)

_PENDING_QUESTION_TTL = 600  # 10 分钟未回答自动过期

_pending_questions: Dict[str, Dict[str, Any]] = {}
_session_questions: Dict[str, str] = {}


def _session_key(session_id: str, session_type: str = "") -> str:
    return f"{session_type}:{session_id}" if session_type else session_id


def _cleanup_expired() -> None:
    if not _pending_questions:
        return
    now = time.time()
    expired = [rid for rid, info in _pending_questions.items() if now - info["ts"] > _PENDING_QUESTION_TTL]
    for rid in expired:
        info = _pending_questions.pop(rid, {})
        key = _session_key(info.get("session_id", ""), info.get("session_type", ""))
        if _session_questions.get(key) == rid:
            del _session_questions[key]


def store_pending_question(
    session_id: str,
    question: str,
    options: Optional[list] = None,
    session_type: str = "",
    parent_message_id: str = "",
) -> str:
    """存储待回答问题，返回 request_id。同一会话仅保留最新一条。"""
    _cleanup_expired()
    request_id = uuid.uuid4().hex
    _pending_questions[request_id] = {
        "session_id": session_id,
        "session_type": session_type,
        "question": question,
        "options": list(options or []),
        "parent_message_id": parent_message_id,
        "ts": time.time(),
    }
    key = _session_key(session_id, session_type)
    old = _session_questions.get(key)
    if old and old != request_id:
        _pending_questions.pop(old, None)
    _session_questions[key] = request_id
    return request_id


def get_pending_question(session_id: str, session_type: str = "") -> Optional[Dict[str, Any]]:
    """获取会话当前待回答问题（不消费）。"""
    _cleanup_expired()
    key = _session_key(session_id, session_type)
    request_id = _session_questions.get(key)
    return _pending_questions.get(request_id) if request_id else None


def consume_pending_question(session_id: str, session_type: str = "") -> Optional[Dict[str, Any]]:
    """消费会话当前待回答问题（取出并移除）。"""
    _cleanup_expired()
    key = _session_key(session_id, session_type)
    request_id = _session_questions.pop(key, None)
    if not request_id:
        return None
    return _pending_questions.pop(request_id, None)


@register_tool(
    name="ask_user_question",
    description=(
        "向用户提出澄清性问题并暂停等待回答。当关键信息缺失、需求含糊、"
        "需要在多个方案之间做选择时使用。问题应简洁具体；可提供候选项。"
        "不要对可以从上下文推断的内容提问。"
    ),
    parameters={
        "type": "object",
        "properties": {
            "question": {"type": "string", "description": "要问用户的问题"},
            "options": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选的候选项列表（最多 6 项），用户也可以自由回答",
            },
        },
        "required": ["question"],
    },
)
def ask_user_question_tool(args: Dict, context: Dict = None) -> Dict[str, Any]:
    question = str(args.get("question") or "").strip()
    options = [str(o).strip() for o in (args.get("options") or []) if str(o).strip()][:6]
    if not question:
        return {"success": False, "error": "question 不能为空"}
    session_id = str((context or {}).get("session_id") or "")
    if not session_id:
        return {"success": False, "error": "缺少会话信息，无法向用户提问"}

    session_type = str((context or {}).get("session_type") or "")
    parent_message_id = str((context or {}).get("parent_message_id") or "")
    request_id = store_pending_question(
        session_id, question, options,
        session_type=session_type, parent_message_id=parent_message_id,
    )
    return {
        "success": True,
        "require_user_answer": True,
        "request_id": request_id,
        "question": question,
        "options": options,
        "message": "已向用户提问，等待回答中",
    }


def handle_pending_user_answer(content: str, session_id: str, session_type: str = "") -> str:
    """若会话存在待回答问题，把用户输入转换为带问题上下文的答案消息。

    无待回答问题时原样返回。
    """
    if not content:
        return content
    info = consume_pending_question(session_id, session_type)
    if not info:
        return content
    question = info.get("question", "")
    options = info.get("options") or []
    answer_line = content.strip()
    matched = next((o for o in options if o == answer_line), None)
    answer_text = f"{matched}（选项回答）" if matched else answer_line
    _log.info("[AskUser] 用户回答了提问: session=%s question=%.40s", session_id, question)
    return f"[系统] 这是用户对提问「{question}」的回答：\n{answer_text}"

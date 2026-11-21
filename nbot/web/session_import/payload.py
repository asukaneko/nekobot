"""会话导入落库：把 ``nbot_session_export`` 会话对象归一化后写入会话库。

Web 面板的「导入会话」、``/api/sessions/import``、DeepSeek 官方导出归档以及客户端上传的
导出包都走这里，保证不同来源的会话在字段与约束上完全一致。
"""

from __future__ import annotations

import logging
import uuid
from datetime import datetime
from typing import Any

from nbot.web.persistence import is_web_visible_session

_log = logging.getLogger(__name__)

EXPORT_TYPE = "nbot_session_export"
_ALLOWED_SESSION_TYPES = frozenset({"web", "cli"})


def extract_export_sessions(payload: Any) -> Any:
    """从请求体里取出会话列表，兼容三种历史写法。

    - ``{"sessions": [...]}``：标准导出包；
    - ``{"session": {...}}``：单会话包装；
    - ``{"name": ..., "messages": [...]}``：裸会话对象。

    无法识别时返回 ``None``（交给调用方按 400 处理）。
    """
    if not isinstance(payload, dict):
        return None
    if payload.get("sessions") is not None:
        return payload.get("sessions")
    single = payload.get("session")
    if isinstance(single, dict):
        return [single]
    if payload.get("type") != EXPORT_TYPE:
        return [payload]
    return None


def normalize_imported_session(raw_session: Any) -> tuple[str, dict]:
    """校验并补全会话记录，返回 ``(新会话 ID, 会话字典)``。

    导入总是生成新 ID：导出包里的 ID 可能与本机已有会话冲突。

    Raises:
        ValueError: 记录不是对象，或不是可在 Web 面板展示的会话。
    """
    if not isinstance(raw_session, dict):
        raise ValueError("session must be an object")

    old_id = str(raw_session.get("id") or "").strip()
    new_id = str(uuid.uuid4())
    now = datetime.now().isoformat()
    session = dict(raw_session)
    session["id"] = new_id
    session["name"] = session.get("name") or f"Imported {new_id[:8]}"
    if old_id:
        session["imported_from"] = old_id
    session["imported_at"] = now
    session["type"] = session.get("type") or "web"
    if session["type"] not in _ALLOWED_SESSION_TYPES:
        session["type"] = "web"

    messages = session.get("messages")
    if not isinstance(messages, list):
        messages = []
    session["messages"] = [message for message in messages if isinstance(message, dict)]

    system_prompt = session.get("system_prompt")
    if not system_prompt:
        system_message = next(
            (message for message in session["messages"] if message.get("role") == "system"),
            None,
        )
        system_prompt = (system_message or {}).get("content", "")
    session["system_prompt"] = system_prompt or ""

    if not session.get("character_id") and session.get("sender_name"):
        session["character_id"] = session["sender_name"]
    if session["system_prompt"] and not any(
        message.get("role") == "system" for message in session["messages"]
    ):
        session["messages"].insert(0, {"role": "system", "content": session["system_prompt"]})

    session["created_at"] = session.get("created_at") or now
    session["archived"] = bool(session.get("archived"))
    session["archived_at"] = session.get("archived_at") if session["archived"] else None
    if not is_web_visible_session(new_id, session):
        raise ValueError("invalid session type")
    return new_id, session


def import_export_payload(server, session_store, sessions_payload: list) -> dict[str, Any]:
    """把会话列表写入会话库，返回 ``{imported, failed, sessions, errors}``。

    单条会话失败只记入 ``errors``，不影响其余会话。
    """
    workspace_manager = getattr(server, "workspace_manager", None)
    workspace_available = bool(getattr(server, "WORKSPACE_AVAILABLE", False))
    imported: list[dict[str, str]] = []
    errors: list[dict[str, Any]] = []

    for index, raw_session in enumerate(sessions_payload):
        try:
            session_id, session = normalize_imported_session(raw_session)
            session_store.set_session(session_id, session)
            if workspace_available and workspace_manager is not None:
                try:
                    workspace_manager.get_or_create(
                        session_id,
                        session.get("type", "web"),
                        session.get("name", ""),
                    )
                except Exception as exc:  # pragma: no cover - 工作空间失败不应阻断导入
                    _log.debug("[SessionImport] 创建工作空间失败 %s: %s", session_id, exc)
            imported.append({"id": session_id, "name": session.get("name")})
        except Exception as exc:
            errors.append({"index": index, "error": str(exc)})

    return {
        "imported": len(imported),
        "failed": len(errors),
        "sessions": imported,
        "errors": errors,
    }

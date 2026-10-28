"""MCP Web Tools

注册所有 Web 功能相关的 MCP Tools。
覆盖：会话、角色卡、世界书、记忆、知识库、AI 模型、Token 用量。

安全流程（每个操作型工具统一走）：
  1. 权限检查 (check_permission)
  2. 工具启用检查 (is_tool_enabled)
  3. 高危确认检查 (requires_confirmation + confirm 参数)
  4. 输入校验 (Pydantic Schema)
  5. 执行 + 审计
"""

import contextlib
import json
import logging
import os
import tempfile
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from nbot.mcp.context import MCPContext
from nbot.mcp.errors import format_mcp_error
from nbot.mcp.logging import MCPToolLogger
from nbot.mcp.permissions import audit_log_entry, check_permission
from nbot.mcp.schemas import (
    WebAddKnowledgeInput,
    WebAddMemoryInput,
    WebAddWorldBookEntryInput,
    WebCreateCharacterInput,
    WebCreateSessionInput,
    WebCreateWorldBookInput,
    WebDeleteCharacterInput,
    WebDeleteKnowledgeInput,
    WebDeleteMemoryInput,
    WebDeleteSessionInput,
    WebDeleteWorldBookInput,
    WebGetCharacterInput,
    WebGetMessagesInput,
    WebGetSessionInput,
    WebGetTokenUsageInput,
    WebGetWorldBookInput,
    WebListMemoriesInput,
    WebSearchKnowledgeInput,
    WebSendMessageInput,
    WebUpdateCharacterInput,
    WebUpdateMemoryInput,
)

_log = logging.getLogger(__name__)

# 进程内线程锁，防止同一进程内多线程并发写同一文件
_file_locks: dict[str, threading.Lock] = {}
_file_locks_guard = threading.Lock()


@contextlib.contextmanager
def _file_lock(name: str):
    """按文件名获取线程锁，保证同进程内串行读写"""
    with _file_locks_guard:
        if name not in _file_locks:
            _file_locks[name] = threading.Lock()
        lock = _file_locks[name]
    lock.acquire()
    try:
        yield
    finally:
        lock.release()


# ========================
# 公共 Guard
# ========================


def _preflight(
    ctx: MCPContext,
    tool_name: str,
    confirm: bool = False,
) -> dict[str, Any] | None:
    """操作型工具的统一流程守卫"""
    if not check_permission(tool_name, _get_scopes(ctx)):
        return {"ok": False, "error": {"code": "permission_denied", "message": "no permission"}}
    if not ctx.is_tool_enabled(tool_name):
        return {"ok": False, "error": {"code": "tool_disabled", "message": f"{tool_name} is disabled"}}
    if ctx.requires_confirmation(tool_name) and not confirm:
        return {
            "ok": False,
            "error": {
                "code": "confirmation_required",
                "message": f"{tool_name} requires confirm=true",
                "tool": tool_name,
            },
        }
    return None


def _validate_input(schema_class: type, **kwargs: Any) -> dict[str, Any] | None:
    """用 Pydantic Schema 校验输入"""
    try:
        schema_class(**kwargs)
        return None
    except ValidationError as e:
        first_error = e.errors()[0]
        field = " → ".join(str(loc) for loc in first_error["loc"])
        return {
            "ok": False,
            "error": {
                "code": "invalid_input",
                "message": f"{field}: {first_error['msg']}",
            },
        }


def _err_json(err: dict[str, Any]) -> str:
    """统一 JSON 序列化错误返回"""
    return json.dumps(err, ensure_ascii=False)


def _get_scopes(ctx: MCPContext) -> list[str]:
    """获取当前上下文的权限列表"""
    default = list(ctx.config.get("permissions", {}).get("default_scopes", []))
    if ctx.config.get("permissions", {}).get("admin", False):
        default.append("admin")
    return list(set(default))


def _audit(ctx: MCPContext, tool_name: str, args: dict, result: dict) -> None:
    """记录审计日志"""
    audit_config = ctx.config.get("audit", {})
    if not audit_config.get("enabled", True):
        return
    entry = audit_log_entry(
        tool_name,
        args,
        result,
        redact_fields=audit_config.get("redact_fields"),
    )
    _log.info("[MCP Audit] %s", json.dumps(entry, ensure_ascii=False))


def _save_error(resource: str) -> str:
    """统一保存失败错误返回"""
    return _err_json({"ok": False, "error": {"code": "save_failed", "message": f"Failed to save {resource}"}})


# ========================
# 数据访问桥接
# ========================


def _get_data_dir(ctx: MCPContext) -> str:
    """获取项目数据根目录 data/

    memories、characters、world_books 等存储在此。
    """
    base_dir = ctx.config.get("base_dir", "")
    if base_dir:
        return os.path.join(base_dir, "data")
    return "data"


def _get_web_data_dir(ctx: MCPContext) -> str:
    """获取 Web 前端数据目录 data/web/

    sessions.db、ai_models.json 等存储在此。
    """
    base_dir = ctx.config.get("base_dir", "")
    if base_dir:
        return os.path.join(base_dir, "data", "web")
    return os.path.join("data", "web")


def _get_saved_message_dir(ctx: MCPContext) -> str:
    """获取 saved_message 目录

    知识库文档存储在 saved_message/knowledge/documents/ 下。
    """
    base_dir = ctx.config.get("base_dir", "")
    if base_dir:
        return os.path.join(base_dir, "saved_message")
    return "saved_message"


def _load_json_file(filepath: str) -> Any:
    """加载 JSON 文件

    文件不存在返回 None；JSON 格式损坏时抛出 RuntimeError 而非静默吞掉。
    """
    if not os.path.exists(filepath):
        return None
    try:
        with open(filepath, encoding="utf-8") as f:
            return json.load(f)
    except json.JSONDecodeError as e:
        raise RuntimeError(f"Corrupted JSON file: {filepath} — {e}") from e


def _save_json_file(filepath: str, data: Any) -> bool:
    """原子写入 JSON 文件

    先写入临时文件，再 os.replace 替换，避免写入中途崩溃导致文件损坏。
    """
    try:
        dir_name = os.path.dirname(filepath)
        os.makedirs(dir_name, exist_ok=True)
        fd, tmp_path = tempfile.mkstemp(dir=dir_name, suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(data, f, ensure_ascii=False, indent=2)
            os.replace(tmp_path, filepath)
            return True
        except BaseException:
            os.unlink(tmp_path)
            raise
    except Exception:
        _log.exception("[MCP Web] Failed to save %s", filepath)
        return False


def _get_sessions(ctx: MCPContext) -> dict:
    """获取所有会话

    优先从 SQLite 数据库 (data/web/sessions.db) 读取；
    若数据库不存在或为空，回退到 JSON 文件 (data/web/sessions.json)。
    """
    web_dir = _get_web_data_dir(ctx)
    try:
        from nbot.web.sessions_db import load_sessions

        sessions = load_sessions(web_dir)
        if sessions:
            return sessions
    except Exception:
        _log.debug("[MCP Web] SQLite 读取失败，回退到 JSON", exc_info=True)

    # 回退：legacy JSON 文件
    json_path = os.path.join(web_dir, "sessions.json")
    sessions = _load_json_file(json_path)
    return sessions if isinstance(sessions, dict) else {}


def _with_sessions(ctx: MCPContext, mutator):
    """原子读-改-写会话

    优先使用 SQLite (upsert)，回退到 JSON 文件。
    mutator(sessions) 在锁内执行。

    Returns:
        (True, mutator_return) on success, (False, None) on save failure.
    """
    web_dir = _get_web_data_dir(ctx)

    # 尝试 SQLite 路径
    try:
        from nbot.web.sessions_db import load_sessions, upsert_session

        sessions = load_sessions(web_dir)
        if not isinstance(sessions, dict):
            sessions = {}
        result = mutator(sessions)
        # 将变更写回 DB
        for sid, session_data in sessions.items():
            upsert_session(web_dir, sid, session_data)
        return True, result
    except Exception:
        _log.debug("[MCP Web] SQLite 写入失败，回退到 JSON", exc_info=True)

    # 回退：JSON 文件
    path = os.path.abspath(os.path.join(web_dir, "sessions.json"))
    with _file_lock(path):
        sessions = _load_json_file(path)
        if not isinstance(sessions, dict):
            sessions = {}
        result = mutator(sessions)
        if not _save_json_file(path, sessions):
            return False, None
        return True, result


def _get_ai_models(ctx: MCPContext) -> list:
    """获取所有 AI 模型

    优先从加密文件 (data/web/ai_models.json) 读取；
    若解密失败，回退到明文 JSON (data/ai_models.json)。
    """
    web_dir = _get_web_data_dir(ctx)
    filepath = os.path.join(web_dir, "ai_models.json")
    try:
        from nbot.web.secure_store import read_secure_json

        payload, _was_plaintext = read_secure_json(filepath, web_dir, None)
        if isinstance(payload, dict):
            models = payload.get("models", [])
            return models if isinstance(models, list) else []
    except Exception:
        _log.debug("[MCP Web] 加密读取失败，回退到明文 JSON", exc_info=True)

    # 回退：明文 JSON
    data_dir = _get_data_dir(ctx)
    models = _load_json_file(os.path.join(data_dir, "ai_models.json"))
    return models if isinstance(models, list) else []


def _get_memories(ctx: MCPContext) -> list:
    """获取所有记忆（只读，不加锁，仅用于 list）"""
    data_dir = _get_data_dir(ctx)
    memories = _load_json_file(os.path.join(data_dir, "memories.json"))
    return memories if isinstance(memories, list) else []


def _with_memories(ctx: MCPContext, mutator):
    """原子读-改-写 memories.json，mutator(memories) 在锁内执行

    Returns:
        (True, mutator_return) on success, (False, None) on save failure.
    """
    data_dir = _get_data_dir(ctx)
    path = os.path.abspath(os.path.join(data_dir, "memories.json"))
    with _file_lock(path):
        memories = _load_json_file(path)
        if not isinstance(memories, list):
            memories = []
        result = mutator(memories)
        if not _save_json_file(path, memories):
            return False, None
        return True, result


def _get_character_profiles(ctx: MCPContext) -> dict:
    """获取所有角色卡

    优先读取 data/web/custom_personality_presets.json（列表格式，Web 管理的角色卡），
    回退到 data/character/profiles.json（运行时数据）。
    返回 dict，key 为角色 ID。
    """
    data_dir = _get_data_dir(ctx)
    # 优先读 Web 管理的角色卡
    filepath = os.path.join(data_dir, "web", "custom_personality_presets.json")
    presets = _load_json_file(filepath)
    if isinstance(presets, list) and presets:
        return {p.get("id", ""): p for p in presets if isinstance(p, dict) and p.get("id")}
    # 回退到运行时 profiles
    filepath = os.path.join(data_dir, "character", "profiles.json")
    profiles = _load_json_file(filepath)
    return profiles if isinstance(profiles, dict) else {}


def _save_character_profile(ctx: MCPContext, profile: dict) -> bool:
    """保存角色卡到 data/web/custom_personality_presets.json（原子读-改-写）"""
    data_dir = _get_data_dir(ctx)
    profile_id = profile.get("id", "")
    if not profile_id:
        return False
    filepath = os.path.abspath(os.path.join(data_dir, "web", "custom_personality_presets.json"))
    with _file_lock(filepath):
        presets = _load_json_file(filepath)
        if not isinstance(presets, list):
            presets = []
        # 按 id 查找并替换，或追加
        found = False
        for i, p in enumerate(presets):
            if isinstance(p, dict) and p.get("id") == profile_id:
                presets[i] = profile
                found = True
                break
        if not found:
            presets.append(profile)
        return _save_json_file(filepath, presets)


def _validate_id_path(filepath: str, expected_dir: str) -> bool:
    """校验拼接后的路径确实在预期目录内，防止路径穿越"""
    base = os.path.realpath(expected_dir)
    target = os.path.realpath(filepath)
    return os.path.commonpath([base, target]) == base


def _delete_character_profile(ctx: MCPContext, character_id: str) -> bool:
    """从 data/web/custom_personality_presets.json 删除角色卡"""
    data_dir = _get_data_dir(ctx)
    filepath = os.path.abspath(os.path.join(data_dir, "web", "custom_personality_presets.json"))
    with _file_lock(filepath):
        presets = _load_json_file(filepath)
        if not isinstance(presets, list):
            return False
        # 按 id 查找
        idx = None
        for i, p in enumerate(presets):
            if isinstance(p, dict) and p.get("id") == character_id:
                idx = i
                break
        if idx is None:
            # 也尝试按 name 匹配
            for i, p in enumerate(presets):
                if isinstance(p, dict) and p.get("name") == character_id:
                    idx = i
                    break
        if idx is None:
            return False
        presets.pop(idx)
        return _save_json_file(filepath, presets)


def _get_world_books(ctx: MCPContext) -> dict:
    """获取所有世界书

    读取 data/world_books.json，格式为 {"world_books": {book_id: book, ...}}。
    entries 从 dict 转为 list 以统一 API 返回格式。
    """
    data_dir = _get_data_dir(ctx)
    filepath = os.path.join(data_dir, "world_books.json")
    raw = _load_json_file(filepath)
    if not isinstance(raw, dict):
        return {}
    books_raw = raw.get("world_books", {})
    if not isinstance(books_raw, dict):
        return {}
    # 将 entries 从 dict 转为 list
    books = {}
    for bid, book in books_raw.items():
        if not isinstance(book, dict):
            continue
        book_copy = dict(book)
        entries = book_copy.get("entries", {})
        if isinstance(entries, dict):
            book_copy["entries"] = list(entries.values())
        books[bid] = book_copy
    return books


def _save_world_book(ctx: MCPContext, book: dict) -> bool:
    """保存世界书到 data/world_books.json（原子读-改-写）"""
    data_dir = _get_data_dir(ctx)
    book_id = book.get("id", "")
    if not book_id:
        return False
    filepath = os.path.abspath(os.path.join(data_dir, "world_books.json"))
    with _file_lock(filepath):
        raw = _load_json_file(filepath)
        if not isinstance(raw, dict):
            raw = {}
        if "world_books" not in raw or not isinstance(raw["world_books"], dict):
            raw["world_books"] = {}
        # 保存时将 entries list 转回 dict
        book_copy = dict(book)
        entries = book_copy.get("entries", [])
        if isinstance(entries, list):
            book_copy["entries"] = {e.get("id", ""): e for e in entries if isinstance(e, dict)}
        raw["world_books"][book_id] = book_copy
        return _save_json_file(filepath, raw)


def _delete_world_book(ctx: MCPContext, book_id: str) -> bool:
    """从 data/world_books.json 删除世界书"""
    data_dir = _get_data_dir(ctx)
    filepath = os.path.abspath(os.path.join(data_dir, "world_books.json"))
    with _file_lock(filepath):
        raw = _load_json_file(filepath)
        if not isinstance(raw, dict) or "world_books" not in raw:
            return False
        if book_id not in raw["world_books"]:
            return False
        del raw["world_books"][book_id]
        return _save_json_file(filepath, raw)


def _get_knowledge_docs(ctx: MCPContext) -> list:
    """获取知识库文档列表

    读取 saved_message/knowledge/documents/ 目录下的 JSON 文件。
    回退到 data/web/knowledge.json（单文件数组）。
    """
    # 优先：per-file 目录
    sm_dir = _get_saved_message_dir(ctx)
    docs_dir = os.path.join(sm_dir, "knowledge", "documents")
    if os.path.isdir(docs_dir):
        docs = []
        for filename in os.listdir(docs_dir):
            if filename.endswith(".json"):
                filepath = os.path.join(docs_dir, filename)
                doc = _load_json_file(filepath)
                if isinstance(doc, dict) and doc.get("id"):
                    docs.append(doc)
        if docs:
            return docs

    # 回退：data/web/knowledge.json 单文件
    web_dir = _get_web_data_dir(ctx)
    legacy = _load_json_file(os.path.join(web_dir, "knowledge.json"))
    return legacy if isinstance(legacy, list) else []


# ========================
# Tool 注册
# ========================


def register_web_tools(mcp_server: Any, ctx: MCPContext) -> None:
    """注册所有 Web MCP Tools"""

    # ========================
    # 会话 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_sessions() -> str:
        """列出所有 Web 会话

        返回会话列表，包含 ID、名称、消息数、创建时间等摘要信息。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_list_sessions", {})

        err = _preflight(ctx, "web_list_sessions")
        if err:
            mcp_log.denied("web_list_sessions", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            sessions = _get_sessions(ctx)
            result_list = []
            for sid, session in sessions.items():
                if not isinstance(session, dict):
                    continue
                result_list.append({
                    "id": sid,
                    "name": session.get("name", f"会话 {sid[:8]}"),
                    "type": session.get("type", "web"),
                    "sender_name": session.get("sender_name", ""),
                    "character_id": session.get("character_id", ""),
                    "message_count": len(session.get("messages", [])),
                    "created_at": session.get("created_at", ""),
                    "archived": bool(session.get("archived")),
                    "session_mode": session.get("session_mode", "character"),
                })
            result = {"ok": True, "count": len(result_list), "sessions": result_list}
            mcp_log.completed("web_list_sessions", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_sessions", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_session(session_id: str) -> str:
        """获取会话详情

        返回指定会话的完整信息，包括系统提示词、角色绑定、消息列表等。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"session_id": session_id}
        mcp_log.called("web_get_session", args)

        err = _preflight(ctx, "web_get_session")
        if err:
            mcp_log.denied("web_get_session", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebGetSessionInput, session_id=session_id)
        if err:
            mcp_log.validation_failed("web_get_session", args, err)
            return _err_json(err)

        try:
            sessions = _get_sessions(ctx)
            session = sessions.get(session_id)
            if not session or not isinstance(session, dict):
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Session not found"}})

            messages = session.get("messages", [])
            display_messages = [m for m in messages if isinstance(m, dict) and m.get("role") != "system"]
            result = {
                "ok": True,
                "session": {
                    "id": session_id,
                    "name": session.get("name", ""),
                    "type": session.get("type", "web"),
                    "sender_name": session.get("sender_name", ""),
                    "character_id": session.get("character_id", ""),
                    "system_prompt": session.get("system_prompt", ""),
                    "scenario": session.get("scenario", ""),
                    "message_count": len(display_messages),
                    "messages": display_messages[-20:],
                    "created_at": session.get("created_at", ""),
                    "archived": bool(session.get("archived")),
                    "session_mode": session.get("session_mode", "character"),
                    "tags": session.get("tags", []),
                },
            }
            mcp_log.completed("web_get_session", args, {"ok": True, "session_id": session_id})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_session", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_create_session(
        name: str = "",
        session_mode: str = "character",
        sender_name: str = "",
        system_prompt: str = "",
        first_message: str = "",
        character_id: str = "",
        confirm: bool = False,
    ) -> str:
        """创建新的 Web 会话

        创建一个新会话并返回会话 ID。可指定角色名称、系统提示词和开场白。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"name": name, "session_mode": session_mode, "sender_name": sender_name}
        mcp_log.called("web_create_session", args)

        err = _preflight(ctx, "web_create_session", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_create_session", args)
            else:
                mcp_log.denied("web_create_session", code, args)
            return _err_json(err)
        err = _validate_input(
            WebCreateSessionInput,
            name=name, session_mode=session_mode, sender_name=sender_name,
            system_prompt=system_prompt, first_message=first_message, character_id=character_id,
        )
        if err:
            mcp_log.validation_failed("web_create_session", args, err)
            return _err_json(err)

        try:
            session_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            messages = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            if first_message:
                messages.append({
                    "id": str(uuid.uuid4()),
                    "role": "assistant",
                    "content": first_message,
                    "sender": sender_name or "AI",
                    "timestamp": now,
                })

            session = {
                "id": session_id,
                "name": name or f"新会话 {session_id[:8]}",
                "type": "web",
                "created_at": now,
                "archived": False,
                "archived_at": None,
                "messages": messages,
                "system_prompt": system_prompt,
                "character_id": character_id,
                "sender_name": sender_name,
                "sender_avatar": "",
                "sender_portrait": "",
                "scenario": "",
                "tags": [],
                "favorite": False,
                "message_favorites": [],
                "pinned": False,
                "session_mode": session_mode,
                "character_runtime_timeline": [],
            }

            def _create(sessions):
                sessions[session_id] = session

            ok, _ = _with_sessions(ctx, _create)
            if not ok:
                return _save_error("sessions")

            result = {"ok": True, "session_id": session_id, "name": session["name"]}
            _audit(ctx, "web_create_session", args, result)
            mcp_log.completed("web_create_session", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_create_session", args, err)
            mcp_log.failed("web_create_session", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_send_message(
        session_id: str,
        content: str,
        sender: str = "mcp_user",
        confirm: bool = False,
    ) -> str:
        """向指定会话发送一条用户消息

        消息会被追加到会话的消息列表中。不会触发 AI 回复（需要通过 Web 端触发）。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"session_id": session_id, "content": content[:100], "sender": sender}
        mcp_log.called("web_send_message", args)

        err = _preflight(ctx, "web_send_message", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_send_message", args)
            else:
                mcp_log.denied("web_send_message", code, args)
            return _err_json(err)
        err = _validate_input(WebSendMessageInput, session_id=session_id, content=content, sender=sender)
        if err:
            mcp_log.validation_failed("web_send_message", args, err)
            return _err_json(err)

        try:
            message_id_holder = {}

            def _append(sessions):
                session = sessions.get(session_id)
                if not session or not isinstance(session, dict):
                    return "not_found"
                message = {
                    "id": str(uuid.uuid4()),
                    "role": "user",
                    "content": content,
                    "timestamp": datetime.now().isoformat(),
                    "sender": sender,
                    "source": "mcp",
                    "session_id": session_id,
                }
                message_id_holder["id"] = message["id"]
                messages = session.get("messages", [])
                if not isinstance(messages, list):
                    messages = []
                messages.append(message)
                session["messages"] = messages
                sessions[session_id] = session
                return None

            ok, err_code = _with_sessions(ctx, _append)
            if not ok:
                return _save_error("sessions")
            if err_code == "not_found":
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Session not found"}})

            result = {"ok": True, "message_id": message_id_holder["id"], "session_id": session_id}
            _audit(ctx, "web_send_message", args, result)
            mcp_log.completed("web_send_message", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_send_message", args, err)
            mcp_log.failed("web_send_message", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_messages(session_id: str, limit: int = 50) -> str:
        """获取会话的消息列表

        返回指定会话的最近 N 条消息（排除系统消息）。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"session_id": session_id, "limit": limit}
        mcp_log.called("web_get_messages", args)

        err = _preflight(ctx, "web_get_messages")
        if err:
            mcp_log.denied("web_get_messages", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebGetMessagesInput, session_id=session_id, limit=limit)
        if err:
            mcp_log.validation_failed("web_get_messages", args, err)
            return _err_json(err)

        try:
            sessions = _get_sessions(ctx)
            session = sessions.get(session_id)
            if not session or not isinstance(session, dict):
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Session not found"}})

            messages = session.get("messages", [])
            display_messages = [
                m for m in messages
                if isinstance(m, dict) and m.get("role") != "system"
            ]
            recent = display_messages[-limit:]

            result = {"ok": True, "count": len(recent), "total": len(display_messages), "messages": recent}
            mcp_log.completed("web_get_messages", args, {"ok": True, "count": len(recent)})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_messages", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_delete_session(session_id: str, confirm: bool = False) -> str:
        """删除指定会话

        永久删除一个会话及其所有消息。不可恢复。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"session_id": session_id}
        mcp_log.called("web_delete_session", args)

        err = _preflight(ctx, "web_delete_session", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_delete_session", args)
            else:
                mcp_log.denied("web_delete_session", code, args)
            return _err_json(err)
        err = _validate_input(WebDeleteSessionInput, session_id=session_id)
        if err:
            mcp_log.validation_failed("web_delete_session", args, err)
            return _err_json(err)

        try:
            def _delete(sessions):
                if session_id not in sessions:
                    return "not_found"
                del sessions[session_id]
                return None

            ok, err_code = _with_sessions(ctx, _delete)
            if not ok:
                return _save_error("sessions")
            if err_code == "not_found":
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Session not found"}})

            result = {"ok": True, "session_id": session_id}
            _audit(ctx, "web_delete_session", args, result)
            mcp_log.completed("web_delete_session", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_delete_session", args, err)
            mcp_log.failed("web_delete_session", args, err)
            return _err_json(err)

    # ========================
    # 角色卡 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_characters() -> str:
        """列出所有角色卡

        返回角色卡列表，包含 ID、名称、描述、标签等摘要信息。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_list_characters", {})

        err = _preflight(ctx, "web_list_characters")
        if err:
            mcp_log.denied("web_list_characters", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            profiles = _get_character_profiles(ctx)
            result_list = []
            for pid, profile in profiles.items():
                result_list.append({
                    "id": pid,
                    "name": profile.get("name", ""),
                    "description": profile.get("description", ""),
                    "avatar": profile.get("avatar", ""),
                    "portrait": profile.get("portrait", ""),
                    "tags": profile.get("tags", []),
                    "basicInfo": profile.get("basicInfo", ""),
                    "personality": profile.get("personality", ""),
                    "scenario": profile.get("scenario", ""),
                    "firstMessage": profile.get("firstMessage", ""),
                    "rules": profile.get("rules", []),
                    "state": profile.get("state", {}),
                    "systemPrompt": profile.get("systemPrompt", ""),
                    "greeting": profile.get("greeting", ""),
                    "responseFormat": profile.get("responseFormat", ""),
                    "exampleDialogues": profile.get("exampleDialogues", ""),
                    "created_at": profile.get("created_at", ""),
                })
            result = {"ok": True, "count": len(result_list), "characters": result_list}
            mcp_log.completed("web_list_characters", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_characters", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_character(character_id: str) -> str:
        """获取角色卡详情

        返回指定角色卡的完整信息，包括描述、性格、背景设定、规则等。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"character_id": character_id}
        mcp_log.called("web_get_character", args)

        err = _preflight(ctx, "web_get_character")
        if err:
            mcp_log.denied("web_get_character", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebGetCharacterInput, character_id=character_id)
        if err:
            mcp_log.validation_failed("web_get_character", args, err)
            return _err_json(err)

        try:
            profiles = _get_character_profiles(ctx)
            # 先按 key 查找，再按 profile 内 id/name 字段匹配
            profile = profiles.get(character_id)
            if not profile:
                for val in profiles.values():
                    if isinstance(val, dict) and (
                        val.get("id") == character_id or val.get("name") == character_id
                    ):
                        profile = val
                        break
            if not profile:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Character not found"}})

            result = {"ok": True, "character": profile}
            mcp_log.completed("web_get_character", args, {"ok": True})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_character", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_create_character(
        name: str,
        description: str = "",
        personality: str = "",
        scenario: str = "",
        system_prompt: str = "",
        first_message: str = "",
        rules: str = "",
        tags: str = "",
        confirm: bool = False,
    ) -> str:
        """创建新角色卡

        创建一个新角色卡并返回角色 ID。规则和标签用逗号分隔的字符串传入。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"name": name, "description": description[:100]}
        mcp_log.called("web_create_character", args)

        err = _preflight(ctx, "web_create_character", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_create_character", args)
            else:
                mcp_log.denied("web_create_character", code, args)
            return _err_json(err)

        rules_list = [r.strip() for r in rules.split(",") if r.strip()] if rules else []
        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        err = _validate_input(
            WebCreateCharacterInput,
            name=name, description=description, personality=personality,
            scenario=scenario, system_prompt=system_prompt, first_message=first_message,
            rules=rules_list, tags=tags_list,
        )
        if err:
            mcp_log.validation_failed("web_create_character", args, err)
            return _err_json(err)

        try:
            character_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            profile = {
                "id": character_id,
                "name": name,
                "description": description,
                "personality": personality,
                "scenario": scenario,
                "systemPrompt": system_prompt,
                "firstMessage": first_message,
                "rules": rules_list,
                "tags": tags_list,
                "created_at": now,
                "updated_at": now,
            }

            if not _save_character_profile(ctx, profile):
                return _save_error("character")

            result = {"ok": True, "character_id": character_id, "name": name}
            _audit(ctx, "web_create_character", args, result)
            mcp_log.completed("web_create_character", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_create_character", args, err)
            mcp_log.failed("web_create_character", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_update_character(
        character_id: str,
        name: str | None = None,
        description: str | None = None,
        personality: str | None = None,
        scenario: str | None = None,
        system_prompt: str | None = None,
        first_message: str | None = None,
        rules: str | None = None,
        tags: str | None = None,
        confirm: bool = False,
    ) -> str:
        """更新角色卡

        更新指定角色卡的信息。只更新传入的字段，未传入的字段保持不变。
        规则和标签用逗号分隔的字符串传入。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"character_id": character_id, "name": name}
        mcp_log.called("web_update_character", args)

        err = _preflight(ctx, "web_update_character", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_update_character", args)
            else:
                mcp_log.denied("web_update_character", code, args)
            return _err_json(err)

        rules_list = [r.strip() for r in rules.split(",") if r.strip()] if rules is not None else None
        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags is not None else None

        err = _validate_input(
            WebUpdateCharacterInput,
            character_id=character_id, name=name, description=description,
            personality=personality, scenario=scenario, system_prompt=system_prompt,
            first_message=first_message, rules=rules_list, tags=tags_list,
        )
        if err:
            mcp_log.validation_failed("web_update_character", args, err)
            return _err_json(err)

        try:
            profiles = _get_character_profiles(ctx)
            # 先按 key 查找，再按 profile 内 id/name 字段匹配
            profile_key = character_id
            profile = profiles.get(character_id)
            if not profile:
                for key, val in profiles.items():
                    if isinstance(val, dict) and (
                        val.get("id") == character_id or val.get("name") == character_id
                    ):
                        profile = val
                        profile_key = key
                        break
            if not profile:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Character not found"}})

            # None = 不更新，空字符串/空列表 = 清空
            if name is not None:
                profile["name"] = name
            if description is not None:
                profile["description"] = description
            if personality is not None:
                profile["personality"] = personality
            if scenario is not None:
                profile["scenario"] = scenario
            if system_prompt is not None:
                profile["systemPrompt"] = system_prompt
            if first_message is not None:
                profile["firstMessage"] = first_message
            if rules is not None:
                profile["rules"] = rules_list
            if tags is not None:
                profile["tags"] = tags_list
            profile["updated_at"] = datetime.now().isoformat()

            if not _save_character_profile(ctx, profile):
                return _save_error("character")

            result = {"ok": True, "character_id": character_id, "name": profile["name"]}
            _audit(ctx, "web_update_character", args, result)
            mcp_log.completed("web_update_character", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_update_character", args, err)
            mcp_log.failed("web_update_character", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_delete_character(character_id: str, confirm: bool = False) -> str:
        """删除角色卡

        永久删除指定角色卡。不可恢复。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"character_id": character_id}
        mcp_log.called("web_delete_character", args)

        err = _preflight(ctx, "web_delete_character", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_delete_character", args)
            else:
                mcp_log.denied("web_delete_character", code, args)
            return _err_json(err)
        err = _validate_input(WebDeleteCharacterInput, character_id=character_id)
        if err:
            mcp_log.validation_failed("web_delete_character", args, err)
            return _err_json(err)

        try:
            if _delete_character_profile(ctx, character_id):
                result = {"ok": True, "character_id": character_id}
                _audit(ctx, "web_delete_character", args, result)
                mcp_log.completed("web_delete_character", args, result)
                return json.dumps(result, ensure_ascii=False)
            else:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Character not found"}})
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_delete_character", args, err)
            mcp_log.failed("web_delete_character", args, err)
            return _err_json(err)

    # ========================
    # 世界书 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_world_books() -> str:
        """列出所有世界书

        返回世界书列表，包含 ID、名称、描述、条目数等摘要信息。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_list_world_books", {})

        err = _preflight(ctx, "web_list_world_books")
        if err:
            mcp_log.denied("web_list_world_books", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            books = _get_world_books(ctx)
            result_list = []
            for bid, book in books.items():
                entries = book.get("entries", [])
                if isinstance(entries, dict):
                    entries = list(entries.values())
                result_list.append({
                    "id": bid,
                    "name": book.get("name", ""),
                    "description": book.get("description", ""),
                    "entry_count": len(entries) if isinstance(entries, list) else 0,
                    "character_ids": book.get("character_ids", []),
                    "enabled": book.get("enabled", True),
                    "created_at": book.get("created_at", ""),
                })
            result = {"ok": True, "count": len(result_list), "world_books": result_list}
            mcp_log.completed("web_list_world_books", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_world_books", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_world_book(book_id: str) -> str:
        """获取世界书详情

        返回指定世界书的完整信息，包括所有条目。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"book_id": book_id}
        mcp_log.called("web_get_world_book", args)

        err = _preflight(ctx, "web_get_world_book")
        if err:
            mcp_log.denied("web_get_world_book", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebGetWorldBookInput, book_id=book_id)
        if err:
            mcp_log.validation_failed("web_get_world_book", args, err)
            return _err_json(err)

        try:
            books = _get_world_books(ctx)
            book = books.get(book_id)
            if not book:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "World book not found"}})

            result = {"ok": True, "world_book": book}
            mcp_log.completed("web_get_world_book", args, {"ok": True})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_world_book", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_create_world_book(
        name: str,
        description: str = "",
        character_ids: str = "",
        confirm: bool = False,
    ) -> str:
        """创建新世界书

        创建一个新世界书并返回世界书 ID。角色 ID 列表用逗号分隔传入。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"name": name, "description": description[:100]}
        mcp_log.called("web_create_world_book", args)

        err = _preflight(ctx, "web_create_world_book", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_create_world_book", args)
            else:
                mcp_log.denied("web_create_world_book", code, args)
            return _err_json(err)

        char_ids = [c.strip() for c in character_ids.split(",") if c.strip()] if character_ids else []

        err = _validate_input(
            WebCreateWorldBookInput,
            name=name, description=description, character_ids=char_ids,
        )
        if err:
            mcp_log.validation_failed("web_create_world_book", args, err)
            return _err_json(err)

        try:
            book_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            book = {
                "id": book_id,
                "name": name,
                "description": description,
                "character_ids": char_ids,
                "entries": [],
                "enabled": True,
                "created_at": now,
                "updated_at": now,
            }

            if not _save_world_book(ctx, book):
                return _save_error("world_book")

            result = {"ok": True, "book_id": book_id, "name": name}
            _audit(ctx, "web_create_world_book", args, result)
            mcp_log.completed("web_create_world_book", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_create_world_book", args, err)
            mcp_log.failed("web_create_world_book", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_add_world_book_entry(
        book_id: str,
        content: str,
        name: str = "",
        keywords: str = "",
        priority: int = 50,
        entry_type: str = "lore",
        always_on: bool = False,
        confirm: bool = False,
    ) -> str:
        """为世界书添加条目

        向指定世界书中添加一个新条目。关键词用逗号分隔传入。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"book_id": book_id, "name": name, "entry_type": entry_type}
        mcp_log.called("web_add_world_book_entry", args)

        err = _preflight(ctx, "web_add_world_book_entry", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_add_world_book_entry", args)
            else:
                mcp_log.denied("web_add_world_book_entry", code, args)
            return _err_json(err)

        keywords_list = [k.strip() for k in keywords.split(",") if k.strip()] if keywords else []

        err = _validate_input(
            WebAddWorldBookEntryInput,
            book_id=book_id, content=content, name=name,
            keywords=keywords_list, priority=priority, entry_type=entry_type, always_on=always_on,
        )
        if err:
            mcp_log.validation_failed("web_add_world_book_entry", args, err)
            return _err_json(err)

        try:
            books = _get_world_books(ctx)
            book = books.get(book_id)
            if not book:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "World book not found"}})

            entry_id = str(uuid.uuid4())
            entry = {
                "id": entry_id,
                "name": name or f"条目 {entry_id[:8]}",
                "keywords": keywords_list,
                "content": content,
                "priority": priority,
                "entry_type": entry_type,
                "always_on": always_on,
                "enabled": True,
                "match_mode": "any",
                "created_at": datetime.now().isoformat(),
            }

            entries = book.get("entries", [])
            if not isinstance(entries, list):
                entries = []
            entries.append(entry)
            book["entries"] = entries
            book["updated_at"] = datetime.now().isoformat()

            if not _save_world_book(ctx, book):
                return _save_error("world_book")

            result = {"ok": True, "entry_id": entry_id, "book_id": book_id}
            _audit(ctx, "web_add_world_book_entry", args, result)
            mcp_log.completed("web_add_world_book_entry", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_add_world_book_entry", args, err)
            mcp_log.failed("web_add_world_book_entry", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_delete_world_book(book_id: str, confirm: bool = False) -> str:
        """删除世界书

        永久删除指定世界书及其所有条目。不可恢复。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"book_id": book_id}
        mcp_log.called("web_delete_world_book", args)

        err = _preflight(ctx, "web_delete_world_book", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_delete_world_book", args)
            else:
                mcp_log.denied("web_delete_world_book", code, args)
            return _err_json(err)
        err = _validate_input(WebDeleteWorldBookInput, book_id=book_id)
        if err:
            mcp_log.validation_failed("web_delete_world_book", args, err)
            return _err_json(err)

        try:
            if _delete_world_book(ctx, book_id):
                result = {"ok": True, "book_id": book_id}
                _audit(ctx, "web_delete_world_book", args, result)
                mcp_log.completed("web_delete_world_book", args, result)
                return json.dumps(result, ensure_ascii=False)
            else:
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "World book not found"}})
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_delete_world_book", args, err)
            mcp_log.failed("web_delete_world_book", args, err)
            return _err_json(err)

    # ========================
    # 记忆 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_memories(
        target_id: str = "",
        character_name: str = "",
        mem_type: str = "all",
    ) -> str:
        """列出记忆

        返回记忆列表。可按目标 ID、角色名、记忆类型筛选。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"target_id": target_id, "character_name": character_name, "mem_type": mem_type}
        mcp_log.called("web_list_memories", args)

        err = _preflight(ctx, "web_list_memories")
        if err:
            mcp_log.denied("web_list_memories", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebListMemoriesInput, target_id=target_id, character_name=character_name, mem_type=mem_type)
        if err:
            mcp_log.validation_failed("web_list_memories", args, err)
            return _err_json(err)

        try:
            memories = _get_memories(ctx)

            if target_id:
                memories = [m for m in memories if m.get("target_id", "") == target_id]
            if character_name:
                memories = [m for m in memories if m.get("character_name", "") == character_name]
            if mem_type != "all":
                memories = [m for m in memories if m.get("type", "long") == mem_type]

            result = {"ok": True, "count": len(memories), "memories": memories}
            mcp_log.completed("web_list_memories", args, {"ok": True, "count": len(memories)})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_memories", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_add_memory(
        title: str,
        content: str,
        target_id: str = "",
        character_name: str = "",
        mem_type: str = "long",
        expire_days: int = 7,
        confirm: bool = False,
    ) -> str:
        """添加记忆

        添加一条新的记忆。记忆类型可选 long（长期）或 short（短期）。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"title": title, "mem_type": mem_type, "target_id": target_id}
        mcp_log.called("web_add_memory", args)

        err = _preflight(ctx, "web_add_memory", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_add_memory", args)
            else:
                mcp_log.denied("web_add_memory", code, args)
            return _err_json(err)
        err = _validate_input(
            WebAddMemoryInput,
            title=title, content=content, target_id=target_id,
            character_name=character_name, mem_type=mem_type, expire_days=expire_days,
        )
        if err:
            mcp_log.validation_failed("web_add_memory", args, err)
            return _err_json(err)

        try:
            memory_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            memory = {
                "id": memory_id,
                "title": title,
                "content": content,
                "target_id": target_id,
                "character_name": character_name,
                "type": mem_type,
                "expire_days": expire_days,
                "priority": "normal",
                "created_at": now,
                "updated_at": now,
            }

            def _add(memories):
                memories.append(memory)

            ok, _ = _with_memories(ctx, _add)
            if not ok:
                return _save_error("memories")

            result = {"ok": True, "memory_id": memory_id}
            _audit(ctx, "web_add_memory", args, result)
            mcp_log.completed("web_add_memory", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_add_memory", args, err)
            mcp_log.failed("web_add_memory", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_update_memory(
        memory_id: str,
        title: str | None = None,
        content: str | None = None,
        mem_type: str | None = None,
        target_id: str | None = None,
        character_name: str | None = None,
        confirm: bool = False,
    ) -> str:
        """更新记忆

        更新指定记忆的内容。只更新传入的字段。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"memory_id": memory_id, "title": title}
        mcp_log.called("web_update_memory", args)

        err = _preflight(ctx, "web_update_memory", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_update_memory", args)
            else:
                mcp_log.denied("web_update_memory", code, args)
            return _err_json(err)
        err = _validate_input(
            WebUpdateMemoryInput,
            memory_id=memory_id, title=title, content=content,
            mem_type=mem_type, target_id=target_id, character_name=character_name,
        )
        if err:
            mcp_log.validation_failed("web_update_memory", args, err)
            return _err_json(err)

        try:
            def _update(memories):
                target = next((m for m in memories if m.get("id") == memory_id), None)
                if not target:
                    return "not_found"
                if title is not None:
                    target["title"] = title
                if content is not None:
                    target["content"] = content
                if mem_type is not None:
                    target["type"] = mem_type
                if target_id is not None:
                    target["target_id"] = target_id
                if character_name is not None:
                    target["character_name"] = character_name
                target["updated_at"] = datetime.now().isoformat()
                return None

            ok, err_code = _with_memories(ctx, _update)
            if not ok:
                return _save_error("memories")
            if err_code == "not_found":
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Memory not found"}})

            result = {"ok": True, "memory_id": memory_id}
            _audit(ctx, "web_update_memory", args, result)
            mcp_log.completed("web_update_memory", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_update_memory", args, err)
            mcp_log.failed("web_update_memory", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_delete_memory(memory_id: str, confirm: bool = False) -> str:
        """删除记忆

        永久删除指定记忆。不可恢复。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"memory_id": memory_id}
        mcp_log.called("web_delete_memory", args)

        err = _preflight(ctx, "web_delete_memory", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_delete_memory", args)
            else:
                mcp_log.denied("web_delete_memory", code, args)
            return _err_json(err)
        err = _validate_input(WebDeleteMemoryInput, memory_id=memory_id)
        if err:
            mcp_log.validation_failed("web_delete_memory", args, err)
            return _err_json(err)

        try:
            def _delete(memories):
                original_len = len(memories)
                memories[:] = [m for m in memories if m.get("id") != memory_id]
                return "not_found" if len(memories) == original_len else None

            ok, err_code = _with_memories(ctx, _delete)
            if not ok:
                return _save_error("memories")
            if err_code == "not_found":
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Memory not found"}})

            result = {"ok": True, "memory_id": memory_id}
            _audit(ctx, "web_delete_memory", args, result)
            mcp_log.completed("web_delete_memory", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_delete_memory", args, err)
            mcp_log.failed("web_delete_memory", args, err)
            return _err_json(err)

    # ========================
    # 知识库 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_knowledge() -> str:
        """列出知识库文档

        返回知识库中的所有文档列表，包含 ID、标题、来源、标签等。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_list_knowledge", {})

        err = _preflight(ctx, "web_list_knowledge")
        if err:
            mcp_log.denied("web_list_knowledge", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            docs = _get_knowledge_docs(ctx)
            result_list = []
            for doc in docs:
                content = doc.get("content", "")
                result_list.append({
                    "id": doc.get("id", ""),
                    "title": doc.get("title") or doc.get("name", ""),
                    "source": doc.get("source", ""),
                    "tags": doc.get("tags", []),
                    "size": len(content),
                    "preview": content[:200] + "..." if len(content) > 200 else content,
                    "created_at": doc.get("created_at", ""),
                })
            result = {"ok": True, "count": len(result_list), "documents": result_list}
            mcp_log.completed("web_list_knowledge", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_knowledge", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_search_knowledge(query: str, top_k: int = 5) -> str:
        """搜索知识库

        使用关键词在知识库中查找相关文档。返回最相关的 top_k 个结果。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"query": query, "top_k": top_k}
        mcp_log.called("web_search_knowledge", args)

        err = _preflight(ctx, "web_search_knowledge")
        if err:
            mcp_log.denied("web_search_knowledge", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebSearchKnowledgeInput, query=query, top_k=top_k)
        if err:
            mcp_log.validation_failed("web_search_knowledge", args, err)
            return _err_json(err)

        try:
            docs = _get_knowledge_docs(ctx)
            query_lower = query.lower()
            scored = []
            for doc in docs:
                content = doc.get("content", "").lower()
                title = (doc.get("title") or doc.get("name", "")).lower()
                score = 0
                if query_lower in title:
                    score += 10
                if query_lower in content:
                    score += 5
                for word in query_lower.split():
                    if word in title:
                        score += 3
                    if word in content:
                        score += 1
                if score > 0:
                    scored.append((score, doc))

            scored.sort(key=lambda x: x[0], reverse=True)
            results = []
            for score, doc in scored[:top_k]:
                content = doc.get("content", "")
                results.append({
                    "doc_id": doc.get("id", ""),
                    "title": doc.get("title") or doc.get("name", ""),
                    "score": score,
                    "preview": content[:300] + "..." if len(content) > 300 else content,
                })

            result = {"ok": True, "query": query, "count": len(results), "results": results}
            mcp_log.completed("web_search_knowledge", args, {"ok": True, "count": len(results)})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_search_knowledge", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_add_knowledge(
        title: str,
        content: str,
        source: str = "",
        tags: str = "",
        confirm: bool = False,
    ) -> str:
        """添加知识库文档

        向知识库中添加一篇新文档。标签用逗号分隔传入。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"title": title, "source": source}
        mcp_log.called("web_add_knowledge", args)

        err = _preflight(ctx, "web_add_knowledge", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_add_knowledge", args)
            else:
                mcp_log.denied("web_add_knowledge", code, args)
            return _err_json(err)

        tags_list = [t.strip() for t in tags.split(",") if t.strip()] if tags else []

        err = _validate_input(
            WebAddKnowledgeInput,
            title=title, content=content, source=source, tags=tags_list,
        )
        if err:
            mcp_log.validation_failed("web_add_knowledge", args, err)
            return _err_json(err)

        try:
            sm_dir = _get_saved_message_dir(ctx)
            docs_dir = os.path.join(sm_dir, "knowledge", "documents")
            os.makedirs(docs_dir, exist_ok=True)

            doc_id = str(uuid.uuid4())
            now = datetime.now().isoformat()

            doc = {
                "id": doc_id,
                "title": title,
                "content": content,
                "source": source,
                "tags": tags_list,
                "created_at": now,
                "updated_at": now,
            }

            filepath = os.path.join(docs_dir, f"{doc_id}.json")
            if not _save_json_file(filepath, doc):
                return _save_error("knowledge document")

            result = {"ok": True, "doc_id": doc_id, "title": title}
            _audit(ctx, "web_add_knowledge", args, result)
            mcp_log.completed("web_add_knowledge", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_add_knowledge", args, err)
            mcp_log.failed("web_add_knowledge", args, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_delete_knowledge(doc_id: str, confirm: bool = False) -> str:
        """删除知识库文档

        永久删除指定知识库文档。不可恢复。

        Args:
            confirm: 高危操作需显式确认 (confirm=true)
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"doc_id": doc_id}
        mcp_log.called("web_delete_knowledge", args)

        err = _preflight(ctx, "web_delete_knowledge", confirm)
        if err:
            code = err.get("error", {}).get("code", "")
            if code == "confirmation_required":
                mcp_log.confirmation_required("web_delete_knowledge", args)
            else:
                mcp_log.denied("web_delete_knowledge", code, args)
            return _err_json(err)
        err = _validate_input(WebDeleteKnowledgeInput, doc_id=doc_id)
        if err:
            mcp_log.validation_failed("web_delete_knowledge", args, err)
            return _err_json(err)

        try:
            sm_dir = _get_saved_message_dir(ctx)
            docs_dir = os.path.join(sm_dir, "knowledge", "documents")
            filepath = os.path.join(docs_dir, f"{doc_id}.json")

            if not _validate_id_path(filepath, docs_dir):
                _log.warning("[MCP Web] Path traversal attempt blocked: %s", doc_id)
                return _err_json({"ok": False, "error": {"code": "invalid_input", "message": "Invalid document ID"}})

            if not os.path.exists(filepath):
                return _err_json({"ok": False, "error": {"code": "not_found", "message": "Document not found"}})

            os.remove(filepath)

            result = {"ok": True, "doc_id": doc_id}
            _audit(ctx, "web_delete_knowledge", args, result)
            mcp_log.completed("web_delete_knowledge", args, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            _audit(ctx, "web_delete_knowledge", args, err)
            mcp_log.failed("web_delete_knowledge", args, err)
            return _err_json(err)

    # ========================
    # AI 模型 Tools
    # ========================

    @mcp_server.tool()
    async def web_list_ai_models() -> str:
        """列出所有 AI 模型配置

        返回 AI 模型列表，包含 ID、名称、提供商、模型名、用途、启用状态等。
        API Key 会被脱敏显示。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_list_ai_models", {})

        err = _preflight(ctx, "web_list_ai_models")
        if err:
            mcp_log.denied("web_list_ai_models", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            models = _get_ai_models(ctx)
            result_list = []
            for model in models:
                if not isinstance(model, dict):
                    continue
                model_copy = model.copy()
                if "api_key" in model_copy:
                    model_copy["api_key"] = "********" if model_copy["api_key"] else ""
                result_list.append({
                    "id": model_copy.get("id", ""),
                    "name": model_copy.get("name", ""),
                    "provider": model_copy.get("provider", ""),
                    "model": model_copy.get("model", ""),
                    "purpose": model_copy.get("purpose", "chat"),
                    "enabled": model_copy.get("enabled", True),
                    "base_url": model_copy.get("base_url", ""),
                    "temperature": model_copy.get("temperature", 0.7),
                    "max_tokens": model_copy.get("max_tokens", 8192),
                    "priority": model_copy.get("priority", 0),
                    "input_price": model_copy.get("input_price"),
                    "output_price": model_copy.get("output_price"),
                })
            result = {"ok": True, "count": len(result_list), "models": result_list}
            mcp_log.completed("web_list_ai_models", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_list_ai_models", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_active_models() -> str:
        """获取各用途的当前活跃模型

        返回每种模型用途（chat、vision、tts 等）当前使用的模型配置。
        """
        mcp_log = MCPToolLogger(ctx)
        mcp_log.called("web_get_active_models", {})

        err = _preflight(ctx, "web_get_active_models")
        if err:
            mcp_log.denied("web_get_active_models", err.get("error", {}).get("code", ""), {})
            return _err_json(err)

        try:
            models = _get_ai_models(ctx)

            # 按 priority 排序（数值越小优先级越高），每个用途取优先级最高的
            enabled = [m for m in models if isinstance(m, dict) and m.get("enabled", True)]
            enabled.sort(key=lambda m: m.get("priority", 0))

            purposes = {}
            for model in enabled:
                purpose = model.get("purpose", "chat")
                if purpose not in purposes:
                    purposes[purpose] = {
                        "id": model.get("id", ""),
                        "name": model.get("name", ""),
                        "model": model.get("model", ""),
                        "provider": model.get("provider", ""),
                        "purpose": purpose,
                        "priority": model.get("priority", 0),
                    }

            result = {"ok": True, "active_models": purposes}
            mcp_log.completed("web_get_active_models", {}, result)
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_active_models", {}, err)
            return _err_json(err)

    @mcp_server.tool()
    async def web_get_token_usage(model_name: str = "") -> str:
        """获取 Token 用量统计

        返回指定模型或所有模型的 Token 使用量统计。
        包含输入 token、输出 token、请求次数等信息。
        """
        mcp_log = MCPToolLogger(ctx)
        args = {"model_name": model_name}
        mcp_log.called("web_get_token_usage", args)

        err = _preflight(ctx, "web_get_token_usage")
        if err:
            mcp_log.denied("web_get_token_usage", err.get("error", {}).get("code", ""), args)
            return _err_json(err)
        err = _validate_input(WebGetTokenUsageInput, model_name=model_name)
        if err:
            mcp_log.validation_failed("web_get_token_usage", args, err)
            return _err_json(err)

        try:
            # 优先 data/web/token_stats.json，回退 data/token_stats.json
            web_dir = _get_web_data_dir(ctx)
            stats = _load_json_file(os.path.join(web_dir, "token_stats.json"))
            if stats is None:
                data_dir = _get_data_dir(ctx)
                stats = _load_json_file(os.path.join(data_dir, "token_stats.json"))

            if not stats:
                stats = {}

            # 统计文件有两种格式：
            # 新格式：{"today", "month", "total", "estimated_cost", "history", "models": {name: {input, output, ...}}}
            # 旧格式：{"model_name": {"input_tokens", "output_tokens", "requests"}}
            models_dict = stats.get("models", {})
            is_new_format = isinstance(models_dict, dict) and any(
                isinstance(v, dict) and "input" in v for v in models_dict.values()
            )

            if model_name:
                if is_new_format:
                    mstats = models_dict.get(model_name, {})
                    result = {"ok": True, "model_name": model_name, "usage": {
                        "input_tokens": mstats.get("input", 0),
                        "output_tokens": mstats.get("output", 0),
                        "total_tokens": mstats.get("total", 0),
                        "requests": mstats.get("message_count", 0),
                        "cost": mstats.get("cost", 0),
                    }}
                else:
                    mstats = stats.get(model_name, {})
                    result = {"ok": True, "model_name": model_name, "usage": mstats}
            else:
                if is_new_format:
                    summary = {}
                    for mname, mstats in models_dict.items():
                        if isinstance(mstats, dict):
                            summary[mname] = {
                                "input_tokens": mstats.get("input", 0),
                                "output_tokens": mstats.get("output", 0),
                                "total_tokens": mstats.get("total", 0),
                                "requests": mstats.get("message_count", 0),
                                "cost": mstats.get("cost", 0),
                            }
                    result = {
                        "ok": True,
                        "today": stats.get("today", 0),
                        "month": stats.get("month", 0),
                        "total": stats.get("total", 0),
                        "estimated_cost": stats.get("estimated_cost", 0),
                        "history": stats.get("history", []),
                        "by_model": summary,
                    }
                else:
                    summary = {}
                    total_input = 0
                    total_output = 0
                    total_requests = 0
                    for mname, mstats in stats.items():
                        if isinstance(mstats, dict):
                            input_tokens = mstats.get("input_tokens", 0)
                            output_tokens = mstats.get("output_tokens", 0)
                            requests = mstats.get("requests", 0)
                            summary[mname] = {
                                "input_tokens": input_tokens,
                                "output_tokens": output_tokens,
                                "total_tokens": input_tokens + output_tokens,
                                "requests": requests,
                            }
                            total_input += input_tokens
                            total_output += output_tokens
                            total_requests += requests
                    result = {
                        "ok": True,
                        "total": {
                            "input_tokens": total_input,
                            "output_tokens": total_output,
                            "total_tokens": total_input + total_output,
                            "requests": total_requests,
                        },
                        "by_model": summary,
                    }

            mcp_log.completed("web_get_token_usage", args, {"ok": True})
            return json.dumps(result, ensure_ascii=False)
        except Exception as e:
            err = format_mcp_error(e)
            mcp_log.failed("web_get_token_usage", args, err)
            return _err_json(err)

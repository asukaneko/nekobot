"""全局搜索（Global Search）后端。

聚合五类数据源：会话 / 消息 / 角色卡 / 世界书 / 记忆。
每个数据源独立 try/except，单个数据源失败只写入 ``errors``，其它分组照常返回。

路由::

    GET /api/search?q=...&types=session,message,character,world_book,memory&limit=10
    GET /api/search?...&max_sessions=&max_messages_per_session=   # 0/不传 = 不限制扫描范围

数据源与依赖（均为只读）：
    session     nbot/core/session_store.py::WebSessionStore.iter_sessions + sessions.db
    message     同上会话的 messages 字段（默认全量扫描，按相关度排序）
    character   data/web/custom_personality_presets.json + data/character/profiles.json
    world_book  nbot/character/storage/world_book_store.py::WorldBookStore.list_all
    memory      nbot/memory/fs.py::MemoryFS.list_all + data/memories.json
"""

from __future__ import annotations

import inspect
import json
import logging
import os
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Tuple

from flask import jsonify, request

from nbot.core.session_store import visible_messages
from nbot.web.persistence import is_web_visible_session

_log = logging.getLogger(__name__)

GROUP_LABELS: Dict[str, str] = {
    "session": "会话",
    "message": "消息",
    "character": "角色",
    "world_book": "世界书",
    "memory": "记忆",
}

MAX_QUERY_LENGTH = 100
DEFAULT_LIMIT = 10
MIN_LIMIT = 1
MAX_LIMIT = 50

# 命中位置前后各保留的字符数
SNIPPET_RADIUS = 60
# 消息扫描范围：默认 0 = 不限（历史行为是 200 会话 × 500 条）。
# 可在 settings.json 的 global_search 段、环境变量或请求参数里收紧，见 _scan_limit。
DEFAULT_MAX_SCAN_SESSIONS = 0
DEFAULT_MAX_MESSAGES_PER_SESSION = 0
SCAN_ENV_SESSIONS = "NBOT_GLOBAL_SEARCH_MAX_SESSIONS"
SCAN_ENV_MESSAGES = "NBOT_GLOBAL_SEARCH_MAX_MESSAGES_PER_SESSION"

_ROLE_LABELS = {"user": "用户", "assistant": "角色", "system": "系统"}

_SearchHandler = Callable[..., List[Dict[str, Any]]]


# ----------------------------------------------------------------------
# 通用工具
# ----------------------------------------------------------------------


def _clamp_limit(limit: Any) -> int:
    """把 limit 夹取到 1..50，非法值回落到默认值。"""
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return DEFAULT_LIMIT
    return max(MIN_LIMIT, min(MAX_LIMIT, value))


def _normalize_query(query: Any) -> str:
    return str(query or "").strip()


def _non_negative_int(value: Any, default: int = 0) -> int:
    """转成 >=0 的整数（0 表示不限）；非法值回落默认。"""
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(0, number)


def _scan_limit(server: Any, options: Dict[str, Any] | None, key: str, env_name: str) -> int:
    """解析扫描上限：请求参数 > settings.json ``global_search`` 段 > 环境变量 > 默认 0（不限）。"""
    options = options or {}
    if options.get(key) not in (None, ""):
        return _non_negative_int(options.get(key), 0)

    settings = getattr(server, "settings", None)
    if isinstance(settings, dict):
        section = settings.get("global_search")
        if isinstance(section, dict) and section.get(key) not in (None, ""):
            return _non_negative_int(section.get(key), 0)

    raw = os.getenv(env_name)
    if raw not in (None, ""):
        return _non_negative_int(raw, 0)
    return 0


def _query_terms(query_lower: str) -> List[str]:
    """把查询拆成词（中英文标点/空白分隔），用于相关度加分。"""
    parts = [part for part in re.split(r"[\s,，、;；/|]+", query_lower) if part]
    return parts or ([query_lower] if query_lower else [])


def _parse_types(raw: Any) -> List[str]:
    """解析 types 参数，返回顺序稳定的已知分组名列表。"""
    if raw is None:
        return list(GROUP_LABELS)
    if isinstance(raw, str):
        parts = [part.strip().lower() for part in raw.split(",")]
    elif isinstance(raw, (list, tuple, set)):
        parts = [str(part).strip().lower() for part in raw]
    else:
        return list(GROUP_LABELS)
    return [name for name in GROUP_LABELS if name in parts]


def _base_dir(server: Any) -> str:
    base_dir = getattr(server, "base_dir", "") or "."
    return str(base_dir)


def _data_dir(server: Any) -> str:
    data_dir = getattr(server, "data_dir", "")
    if data_dir:
        return str(data_dir)
    return os.path.join(_base_dir(server), "data", "web")


def _as_text(value: Any) -> str:
    """把任意字段转成可搜索文本（中文按子串匹配）。"""
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, (list, tuple, set)):
        return " ".join(_as_text(item) for item in value)
    if isinstance(value, dict):
        return " ".join(_as_text(item) for item in value.values())
    return str(value)


def _matches(query_lower: str, *values: Any) -> bool:
    """任一字段包含查询串（大小写不敏感）即命中。"""
    for value in values:
        if query_lower in _as_text(value).lower():
            return True
    return False


def _snippet(text: Any, query_lower: str, radius: int = SNIPPET_RADIUS) -> str:
    """取命中位置前后约 radius 个字符，两侧用省略号标注被截断。"""
    compact = " ".join(_as_text(text).split())
    if not compact:
        return ""
    position = compact.lower().find(query_lower) if query_lower else -1
    if position < 0:
        head = compact[: radius * 2]
        return head + ("…" if len(compact) > len(head) else "")
    start = max(0, position - radius)
    end = min(len(compact), position + len(query_lower) + radius)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(compact) else ""
    return f"{prefix}{compact[start:end]}{suffix}"


def _matched_snippet(query_lower: str, *values: Any) -> str:
    """优先返回真正命中的字段片段，否则回落到第一个非空字段。"""
    for value in values:
        text = _as_text(value)
        if text and query_lower in text.lower():
            return _snippet(text, query_lower)
    for value in values:
        text = _as_text(value)
        if text:
            return _snippet(text, query_lower)
    return ""


def _tag_list(value: Any) -> List[str]:
    if isinstance(value, str):
        return [value.strip()] if value.strip() else []
    if isinstance(value, (list, tuple, set)):
        return [str(item).strip() for item in value if str(item).strip()]
    return []


def _load_json(path: str) -> Any:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


# ----------------------------------------------------------------------
# 会话
# ----------------------------------------------------------------------


def _load_sessions_from_db(data_dir: str) -> Dict[str, Dict[str, Any]]:
    """只读方式读取 sessions.db（不建表、不写盘）。"""
    db_path = os.path.join(data_dir, "sessions.db")
    if not os.path.exists(db_path):
        return {}
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT session_id, data_json FROM sessions").fetchall()
    finally:
        conn.close()
    sessions: Dict[str, Dict[str, Any]] = {}
    for session_id, data_json in rows:
        try:
            payload = json.loads(data_json)
        except (TypeError, ValueError):
            continue
        if isinstance(payload, dict):
            sessions[str(session_id)] = payload
    return sessions


def _session_records(server: Any) -> Dict[str, Dict[str, Any]]:
    """收集会话：内存 session_store → server.sessions → sessions.db（按 id 去重）。"""
    records: Dict[str, Dict[str, Any]] = {}

    def _add(session_id: Any, session: Any) -> None:
        key = str(session_id or "").strip()
        if not key or key in records or not isinstance(session, dict):
            return
        records[key] = session

    store = getattr(server, "session_store", None)
    if store is not None and hasattr(store, "iter_sessions"):
        try:
            for session_id, session in store.iter_sessions():
                _add(session_id, session)
        except Exception as exc:
            _log.debug("[global-search] session_store 遍历失败: %s", exc)

    sessions = getattr(server, "sessions", None)
    if isinstance(sessions, dict):
        for session_id, session in sessions.items():
            _add(session_id, session)

    if not records:
        try:
            for session_id, session in _load_sessions_from_db(_data_dir(server)).items():
                _add(session_id, session)
        except Exception as exc:
            _log.debug("[global-search] sessions.db 读取失败: %s", exc)

    return records


def _session_display_name(session_id: str, session: Dict[str, Any]) -> str:
    name = str(session.get("name") or "").strip()
    return name or f"会话 {session_id[:8]}"


def _session_updated_at(session: Dict[str, Any]) -> str:
    for key in ("updated_at", "last_message_at", "created_at"):
        value = session.get(key)
        if value:
            return str(value)
    messages = visible_messages(session.get("messages"))
    if messages:
        last = messages[-1]
        return str(last.get("timestamp") or last.get("created_at") or "")
    return ""


def _session_subtitle(session: Dict[str, Any]) -> str:
    parts: List[str] = [str(session.get("type") or "web")]
    user_id = str(session.get("user_id") or "").strip()
    if user_id:
        parts.append(f"用户 {user_id}")
    message_count = len(visible_messages(session.get("messages")))
    if message_count:
        parts.append(f"{message_count} 条消息")
    tags = _tag_list(session.get("tags"))
    if tags:
        parts.append("标签 " + "、".join(tags))
    return " · ".join(parts)


def _ordered_sessions(server: Any) -> List[Tuple[str, Dict[str, Any]]]:
    """按更新时间倒序排列会话（新的优先命中）。"""
    ordered = [(session_id, session) for session_id, session in _session_records(server).items()]
    ordered.sort(key=lambda pair: _session_updated_at(pair[1]), reverse=True)
    return ordered


def _search_sessions(
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for session_id, session in _ordered_sessions(server):
        if not is_web_visible_session(session_id, session):
            continue
        name = _session_display_name(session_id, session)
        tags = session.get("tags")
        user_id = session.get("user_id")
        if not _matches(query_lower, name, tags, user_id, session_id):
            continue
        items.append(
            {
                "id": session_id,
                "session_id": session_id,
                "title": name,
                "subtitle": _session_subtitle(session),
                "snippet": _matched_snippet(query_lower, name, tags, user_id, session_id),
                "updated_at": _session_updated_at(session),
                "route": {"page": "chat", "session_id": session_id},
            }
        )
        if len(items) >= limit:
            break
    return items


# ----------------------------------------------------------------------
# 消息
# ----------------------------------------------------------------------


def _message_timestamp(message: Dict[str, Any]) -> str:
    for key in ("timestamp", "created_at", "updated_at"):
        value = message.get(key)
        if value:
            return str(value)
    return ""


def _message_speaker(character_name: str, message: Dict[str, Any]) -> str:
    sender = str(message.get("sender_name") or message.get("sender") or "").strip()
    if sender:
        return sender
    role = str(message.get("role") or "").strip().lower()
    if role == "assistant" and character_name:
        return character_name
    return _ROLE_LABELS.get(role, role or "消息")


def _message_relevance(
    content_lower: str,
    query_lower: str,
    terms: List[str],
    recency_ratio: float,
) -> float:
    """消息相关度：命中次数 + 命中位置 + 多词覆盖 + 新近程度。

    只对已确认包含完整查询串的消息打分（匹配语义不变），分数用于排序。
    """
    score = 0.0
    occurrences = content_lower.count(query_lower) if query_lower else 0
    if occurrences:
        score += 3.0 * min(occurrences, 5)
        first = content_lower.find(query_lower)
        if first >= 0 and content_lower:
            # 越靠近开头命中越集中（长消息里的边角命中降权）
            score += 1.5 * (1.0 - first / max(len(content_lower), 1))
    if len(terms) > 1:
        for term in terms:
            if not term:
                continue
            count = content_lower.count(term)
            if count:
                score += min(count, 3)
    score += 0.5 * max(0.0, min(1.0, recency_ratio))
    return score


def _search_messages(
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    """扫描消息并按相关度排序返回。

    扫描范围默认不限（旧行为是最近 200 会话 × 每会话 500 条），可用请求参数
    ``max_sessions`` / ``max_messages_per_session``（0 或不传 = 不限）、
    ``settings.json`` 的 ``global_search`` 段、或环境变量收紧。
    """
    options = options or {}
    max_sessions = _scan_limit(server, options, "max_sessions", SCAN_ENV_SESSIONS)
    max_messages = _scan_limit(
        server, options, "max_messages_per_session", SCAN_ENV_MESSAGES
    )
    terms = _query_terms(query_lower)

    sessions = _ordered_sessions(server)
    if max_sessions:
        sessions = sessions[:max_sessions]

    hits: List[Dict[str, Any]] = []
    for session_id, session in sessions:
        if not is_web_visible_session(session_id, session):
            continue
        session_name = _session_display_name(session_id, session)
        character_name = str(
            session.get("character_name") or session.get("sender_name") or ""
        ).strip()
        messages = visible_messages(session.get("messages"))
        if max_messages:
            messages = messages[-max_messages:]
        total = max(len(messages), 1)
        for index, message in enumerate(messages):
            content = _as_text(message.get("content"))
            if not content:
                continue
            content_lower = content.lower()
            if query_lower not in content_lower:
                continue
            message_id = str(message.get("id") or f"{session_id}#{index}")
            score = _message_relevance(
                content_lower,
                query_lower,
                terms,
                index / total,
            )
            hits.append(
                {
                    "id": message_id,
                    "session_id": session_id,
                    "message_id": message_id,
                    "title": session_name,
                    "subtitle": _message_speaker(character_name, message),
                    "snippet": _snippet(content, query_lower),
                    "updated_at": _message_timestamp(message),
                    "score": round(score, 3),
                    "route": {
                        "page": "chat",
                        "session_id": session_id,
                        "message_id": message_id,
                    },
                }
            )

    # 相关度优先，同分按时间新的在前（原顺序是会话/消息时间序，稳定排序保持可预期）
    hits.sort(key=lambda item: (item["score"], item["updated_at"] or ""), reverse=True)
    return hits[:limit]


# ----------------------------------------------------------------------
# 角色卡
# ----------------------------------------------------------------------


def _character_records(server: Any) -> List[Dict[str, Any]]:
    """角色卡完整数据源 + data/character/profiles.json 兜底。"""
    data_dir = _data_dir(server)
    presets: Any = None
    presets_path = os.path.join(data_dir, "custom_personality_presets.json")
    if os.path.exists(presets_path):
        presets = _load_json(presets_path)
    elif isinstance(getattr(server, "custom_personality_presets", None), list):
        presets = server.custom_personality_presets

    records: List[Dict[str, Any]] = []
    seen_ids = set()
    seen_names = set()
    for item in presets if isinstance(presets, list) else []:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        key = str(item.get("id") or name)
        if key in seen_ids or (name and name in seen_names):
            continue
        seen_ids.add(key)
        if name:
            seen_names.add(name)
        records.append(item)

    profiles_path = os.path.join(_base_dir(server), "data", "character", "profiles.json")
    if not os.path.exists(profiles_path):
        return records
    profiles = _load_json(profiles_path)
    if isinstance(profiles, dict):
        candidates = [(str(key), value) for key, value in profiles.items()]
    elif isinstance(profiles, list):
        candidates = [("", value) for value in profiles]
    else:
        return records
    for key, value in candidates:
        if not isinstance(value, dict):
            continue
        name = str(value.get("name") or key or "").strip()
        record_key = str(value.get("id") or name)
        if not record_key or record_key in seen_ids or (name and name in seen_names):
            continue
        record = dict(value)
        record.setdefault("name", name)
        seen_ids.add(record_key)
        if name:
            seen_names.add(name)
        records.append(record)
    return records


def _search_characters(
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    items: List[Dict[str, Any]] = []
    for record in _character_records(server):
        name = str(record.get("name") or "").strip()
        description = str(record.get("description") or "")
        tags = record.get("tags")
        if not _matches(query_lower, name, description, tags):
            continue
        character_id = str(record.get("id") or name)
        items.append(
            {
                "id": character_id,
                "title": name or "(未命名角色)",
                "subtitle": "、".join(_tag_list(tags)) or "角色卡",
                "snippet": _matched_snippet(query_lower, description, tags, name),
                "updated_at": str(record.get("updated_at") or record.get("created_at") or ""),
                "route": {"page": "characters", "character_id": character_id},
            }
        )
        if len(items) >= limit:
            break
    return items


# ----------------------------------------------------------------------
# 世界书
# ----------------------------------------------------------------------


def _search_world_books(
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    from nbot.character.storage.world_book_store import WorldBookStore

    store = WorldBookStore(_base_dir(server))
    items: List[Dict[str, Any]] = []
    for book in store.list_all():
        book_id = str(getattr(book, "id", "") or "")
        book_name = str(getattr(book, "name", "") or "")
        description = str(getattr(book, "description", "") or "")
        if _matches(query_lower, book_name, description):
            items.append(
                {
                    "id": book_id,
                    "book_id": book_id,
                    "entry_id": "",
                    "title": book_name or "(未命名世界书)",
                    "subtitle": "世界书",
                    "snippet": _matched_snippet(query_lower, description, book_name),
                    "updated_at": str(getattr(book, "updated_at", "") or ""),
                    "route": {"page": "world-book", "book_id": book_id},
                }
            )
            if len(items) >= limit:
                return items
        for entry in getattr(book, "entries", None) or []:
            entry_id = str(getattr(entry, "id", "") or "")
            entry_name = str(getattr(entry, "name", "") or "")
            keywords = getattr(entry, "keywords", None)
            content = str(getattr(entry, "content", "") or "")
            if not _matches(query_lower, entry_name, keywords, content):
                continue
            items.append(
                {
                    "id": f"{book_id}:{entry_id}" if book_id else entry_id,
                    "book_id": book_id,
                    "entry_id": entry_id,
                    "title": entry_name or "(未命名条目)",
                    "subtitle": f"世界书「{book_name}」" if book_name else "世界书条目",
                    "snippet": _matched_snippet(query_lower, keywords, content, entry_name),
                    "updated_at": str(getattr(entry, "updated_at", "") or ""),
                    "route": {
                        "page": "world-book",
                        "book_id": book_id,
                        "entry_id": entry_id,
                    },
                }
            )
            if len(items) >= limit:
                return items
    return items


# ----------------------------------------------------------------------
# 记忆
# ----------------------------------------------------------------------


def _memory_fs_records(server: Any) -> List[Dict[str, Any]]:
    """MemoryFS 逻辑记忆（data/web/memory_fs.json）。

    走 MemoryFS 的公开列表接口（``list_all``），不再读私有 ``_index``；
    读不到时抛异常，由 _search_memories 记录并继续用 memories.json。
    """
    from nbot.memory.fs import MemoryFS

    memory_fs = MemoryFS(data_dir=_data_dir(server))
    records: List[Dict[str, Any]] = []
    for memory_file in memory_fs.list_all(order_by="updated_at", descending=True):
        path = str(memory_file.path or "")
        records.append(
            {
                "id": path,
                "title": str(memory_file.title or path),
                "summary": str(memory_file.summary or ""),
                "content": str(memory_file.content or ""),
                "tags": memory_file.tags,
                "updated_at": str(memory_file.updated_at or ""),
                "character_id": str(memory_file.character_id or ""),
                "target_id": str(memory_file.target_id or ""),
                "importance": float(memory_file.importance or 0.0),
                "route": {"page": "memory", "path": path},
            }
        )
    return records


def _global_memory_records(server: Any) -> List[Dict[str, Any]]:
    """全局记忆列表：优先 server.memories 缓存，否则读 data/memories.json。"""
    memories = getattr(server, "memories", None)
    if isinstance(memories, list) and memories:
        raw: Any = memories
    else:
        path = os.path.join(_base_dir(server), "data", "memories.json")
        raw = _load_json(path) if os.path.exists(path) else []
    if not isinstance(raw, list):
        return []
    records: List[Dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        memory_id = str(item.get("id") or "")
        records.append(
            {
                "id": memory_id,
                "title": str(item.get("title") or ""),
                "summary": str(item.get("summary") or ""),
                "content": str(item.get("content") or ""),
                "tags": item.get("tags"),
                "updated_at": str(item.get("created_at") or item.get("updated_at") or ""),
                "character_id": str(item.get("character_name") or item.get("target_id") or ""),
                "route": {"page": "memory", "memory_id": memory_id},
            }
        )
    return records


def _search_memories(
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any] | None = None,
) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    for loader, source_name in (
        (_memory_fs_records, "memory_fs"),
        (_global_memory_records, "memories"),
    ):
        try:
            records.extend(loader(server))
        except Exception as exc:
            _log.warning("[global-search] 记忆源 %s 读取失败: %s", source_name, exc)
            errors.append(f"memory: {source_name} 读取失败: {exc}")

    items: List[Dict[str, Any]] = []
    seen = set()
    for record in records:
        if not _matches(
            query_lower,
            record.get("title"),
            record.get("summary"),
            record.get("content"),
            record.get("tags"),
        ):
            continue
        key = str(record.get("id") or "")
        if not key:
            key = f"{record.get('title')}|{str(record.get('content') or '')[:40]}"
        if key in seen:
            continue
        seen.add(key)
        items.append(
            {
                "id": key,
                "title": record.get("title") or "(无标题记忆)",
                "subtitle": record.get("character_id") or "记忆",
                "snippet": _matched_snippet(
                    query_lower,
                    record.get("content"),
                    record.get("summary"),
                    record.get("title"),
                ),
                "updated_at": record.get("updated_at") or "",
                "route": record.get("route") or {"page": "memory"},
            }
        )
        if len(items) >= limit:
            break
    return items


# ----------------------------------------------------------------------
# 聚合与路由
# ----------------------------------------------------------------------


_SEARCH_HANDLERS: Dict[str, _SearchHandler] = {
    "session": _search_sessions,
    "message": _search_messages,
    "character": _search_characters,
    "world_book": _search_world_books,
    "memory": _search_memories,
}


def _empty_groups(names: List[str]) -> Dict[str, Dict[str, Any]]:
    return {name: {"label": GROUP_LABELS[name], "items": []} for name in names}


def _call_handler(
    handler: _SearchHandler,
    server: Any,
    query_lower: str,
    limit: int,
    errors: List[str],
    options: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """调用数据源 handler，兼容只接受 4 个位置参数的旧签名/自定义 handler。"""
    try:
        parameters = inspect.signature(handler).parameters
    except (TypeError, ValueError):
        parameters = {}
    accepts_options = len(parameters) >= 5 or any(
        parameter.kind is inspect.Parameter.VAR_POSITIONAL
        for parameter in parameters.values()
    )
    if accepts_options:
        return handler(server, query_lower, limit, errors, options)
    return handler(server, query_lower, limit, errors)


def search_all(
    server: Any,
    query: str,
    types: List[str] | None = None,
    limit: int = 10,
    **kwargs: Any,
) -> Dict[str, Any]:
    """跨数据源搜索，返回 ``{"success", "query", "took_ms", "total", "groups", "errors"}``。

    单个数据源异常只记录在 ``errors``（格式 ``"{group}: {message}"``），不影响其它分组。
    空查询 / 超长查询返回 ``success: False``（路由层转 400）。
    """
    text = _normalize_query(query)
    if not text:
        return {
            "success": False,
            "query": text,
            "error": "query is required",
            "took_ms": 0,
            "total": 0,
            "groups": {},
            "errors": [],
        }
    if len(text) > MAX_QUERY_LENGTH:
        return {
            "success": False,
            "query": text,
            "error": f"query is too long (max {MAX_QUERY_LENGTH})",
            "took_ms": 0,
            "total": 0,
            "groups": {},
            "errors": [],
        }

    query_lower = text.lower()
    safe_limit = _clamp_limit(limit)
    selected = _parse_types(types)
    groups = _empty_groups(selected)
    errors: List[str] = []
    started = time.perf_counter()
    scan_options = {
        key: kwargs[key]
        for key in ("max_sessions", "max_messages_per_session")
        if kwargs.get(key) not in (None, "")
    }

    for name in selected:
        handler = _SEARCH_HANDLERS[name]
        try:
            items = _call_handler(
                handler, server, query_lower, safe_limit, errors, scan_options
            ) or []
        except Exception as exc:
            _log.warning("[global-search] %s 搜索失败: %s", name, exc)
            errors.append(f"{name}: {exc}")
            items = []
        groups[name]["items"] = list(items)[:safe_limit]

    took_ms = int(round((time.perf_counter() - started) * 1000))
    return {
        "success": True,
        "query": text,
        "took_ms": took_ms,
        "total": sum(len(group["items"]) for group in groups.values()),
        "groups": groups,
        "errors": errors,
        "scan": {
            "max_sessions": _scan_limit(
                server, scan_options, "max_sessions", SCAN_ENV_SESSIONS
            ),
            "max_messages_per_session": _scan_limit(
                server, scan_options, "max_messages_per_session", SCAN_ENV_MESSAGES
            ),
        },
    }


def register_global_search_routes(app, server):
    @app.route("/api/search")
    def api_global_search():
        result = search_all(
            server,
            request.args.get("q", ""),
            types=request.args.get("types"),
            limit=request.args.get("limit", DEFAULT_LIMIT),
            max_sessions=request.args.get("max_sessions"),
            max_messages_per_session=request.args.get("max_messages_per_session"),
        )
        status = 200 if result.get("success") else 400
        return jsonify(result), status

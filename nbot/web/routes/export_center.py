"""数据导出中心：按分类勾选导出为 ZIP，并从 ZIP 导入。

ZIP 结构::

    manifest.json
    data/sessions.json
    data/characters.json
    ...

设计要点：
- 导出只读取现有存储，不修改任何数据；
- 导入只读取 manifest 中声明的 ``data/<category>.json``，拒绝路径穿越与未知分类；
- 单个分类解析失败只记录 warnings，不影响其它分类；
- 敏感数据（api_keys / 加密配置 / 证书）不参与导出。
"""

import io
import json
import logging
import os
import uuid
import zipfile
from copy import deepcopy
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

from flask import jsonify, request, send_file

_log = logging.getLogger(__name__)

EXPORT_FORMAT = "nekobot-export"
EXPORT_VERSION = 1
APP_NAME = "NcatBot"
MANIFEST_NAME = "manifest.json"
DATA_DIR_NAME = "data"

# 解压前的体积上限，避免 zip 炸弹
MAX_TOTAL_UNCOMPRESSED = 100 * 1024 * 1024
MAX_SINGLE_FILE = 20 * 1024 * 1024
_READ_CHUNK = 64 * 1024

CATEGORY_ORDER: Tuple[str, ...] = (
    "sessions",
    "characters",
    "world_books",
    "memories",
    "settings",
    "hooks",
    "workflows",
)

CATEGORY_META: Dict[str, Dict[str, str]] = {
    "sessions": {
        "label": "会话记录",
        "description": "Web / CLI 会话及其全部消息（sessions.db）",
    },
    "characters": {
        "label": "角色卡",
        "description": "自定义人格预设 custom_personality_presets.json",
    },
    "world_books": {
        "label": "世界书",
        "description": "世界书及条目 data/world_books.json",
    },
    "memories": {
        "label": "记忆",
        "description": "Web 端记忆列表 memories.json",
    },
    "settings": {
        "label": "面板设置",
        "description": "Web 面板全局设置 settings.json",
    },
    "hooks": {
        "label": "Hook 规则",
        "description": "Hook Runtime 规则 hooks.json",
    },
    "workflows": {
        "label": "工作流",
        "description": "工作流配置 workflows.json",
    },
}


class ExportCenterError(ValueError):
    """ZIP 结构、manifest 或导入参数非法（路由层按 400 返回）。"""


# ---------------------------------------------------------------------------
# 路径与 JSON 工具
# ---------------------------------------------------------------------------


def _data_dir(server) -> str:
    return str(getattr(server, "data_dir", "") or os.path.join("data", "web"))


def _base_dir(server) -> str:
    return str(getattr(server, "base_dir", "") or ".")


def _web_file(server, filename: str) -> str:
    return os.path.join(_data_dir(server), filename)


def _world_books_file(server) -> str:
    return os.path.join(_base_dir(server), "data", "world_books.json")


def _read_json_file(path: str, default: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception as exc:
        _log.debug("[ExportCenter] 读取 %s 失败: %s", path, exc)
        return deepcopy(default)


def _write_json_file(path: str, payload: Any) -> None:
    directory = os.path.dirname(path)
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp_path = f"{path}.{uuid.uuid4().hex}.tmp"
    try:
        with open(tmp_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)
    finally:
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError:
                pass


def _dump_json(payload: Any) -> bytes:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    return text.encode("utf-8")


def _as_dict_list(value: Any) -> List[Dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [deepcopy(item) for item in value if isinstance(item, dict)]


def _item_id(item: Dict[str, Any]) -> str:
    return str(item.get("id") or "").strip()


# ---------------------------------------------------------------------------
# 各类别的读取（导出）
# ---------------------------------------------------------------------------


def _loaded_sessions(server) -> List[Tuple[str, Dict[str, Any]]]:
    """内存里已加载的会话（session_store / server.sessions）。"""
    store = getattr(server, "session_store", None)
    iter_sessions = getattr(store, "iter_sessions", None)
    if callable(iter_sessions):
        try:
            pairs = list(iter_sessions())
        except Exception as exc:
            _log.warning("[ExportCenter] 遍历 session_store 失败: %s", exc)
            pairs = []
        sessions = [(str(sid), session) for sid, session in pairs if isinstance(session, dict)]
        if sessions:
            return sessions

    sessions_dict = getattr(server, "sessions", None)
    if isinstance(sessions_dict, dict):
        return [
            (str(sid), session)
            for sid, session in sessions_dict.items()
            if isinstance(session, dict)
        ]
    return []


def _disk_sessions(server) -> List[Tuple[str, Dict[str, Any]]]:
    """磁盘上的会话：web sessions.db 全量 + 旧 sessions.json 元数据 + 全局 sessions.db。"""
    data_dir = _data_dir(server)
    merged: Dict[str, Dict[str, Any]] = {}

    try:
        from nbot.web.sessions_db import load_sessions

        on_disk = (
            load_sessions(data_dir)
            if os.path.isfile(os.path.join(data_dir, "sessions.db"))
            else {}
        )
    except Exception as exc:
        _log.warning("[ExportCenter] 读取 sessions.db 失败: %s", exc)
        on_disk = {}
    if isinstance(on_disk, dict):
        for sid, session in on_disk.items():
            if isinstance(session, dict):
                merged[str(sid)] = deepcopy(session)

    legacy = _read_json_file(_web_file(server, "sessions.json"), None)
    if isinstance(legacy, list):
        for item in legacy:
            if not isinstance(item, dict):
                continue
            sid = str(item.get("id") or item.get("session_id") or "").strip()
            if not sid:
                continue
            merged.setdefault(sid, deepcopy(item))

    global_db = os.path.join(_base_dir(server), "data", "sessions.db")
    if os.path.isfile(global_db):
        try:
            import sqlite3

            with sqlite3.connect(global_db) as conn:
                rows = conn.execute("SELECT id, data FROM sessions").fetchall()
            for sid, payload in rows:
                session_id = str(sid or "").strip()
                if not session_id or session_id in merged:
                    continue
                try:
                    data = json.loads(payload)
                except (TypeError, ValueError):
                    continue
                if isinstance(data, dict):
                    merged[session_id] = data
        except Exception as exc:
            _log.debug("[ExportCenter] 读取 data/sessions.db 失败: %s", exc)

    return [(sid, session) for sid, session in merged.items()]


def _session_sort_key(item: Tuple[str, Dict[str, Any]]) -> str:
    session = item[1]
    for key in ("updated_at", "last_message_at", "created_at", "timestamp"):
        value = session.get(key)
        if value:
            return str(value)
    return ""


def _iter_sessions(
    server,
    scope: str = "all",
    limit: int = 0,
) -> Tuple[List[Tuple[str, Dict[str, Any]]], Dict[str, int]]:
    """合并内存与磁盘会话。

    scope: ``loaded`` 只导内存里已加载的；``all``（默认）额外并入
    ``data/web/sessions.db`` / ``data/sessions.db`` / ``sessions.json`` 里未加载的会话。
    limit>0 时按最近更新时间取前 N 个（0 表示不限）。
    """
    normalized_scope = str(scope or "all").strip().lower()
    if normalized_scope not in ("all", "loaded"):
        raise ExportCenterError("sessions_scope 只能是 all 或 loaded")

    loaded = _loaded_sessions(server)
    merged: Dict[str, Dict[str, Any]] = {sid: session for sid, session in loaded}
    loaded_count = len(merged)

    if normalized_scope == "all":
        for sid, session in _disk_sessions(server):
            if sid not in merged:
                merged[sid] = session

    items = list(merged.items())
    if limit and limit > 0:
        items.sort(key=_session_sort_key, reverse=True)
        items = items[:limit]
    stats = {
        "loaded": loaded_count,
        "total": len(merged),
        "disk_only": max(0, len(merged) - loaded_count),
        "exported": len(items),
    }
    return items, stats


def _collect_sessions(
    server, scope: str = "all", limit: int = 0
) -> List[Dict[str, Any]]:
    pairs, _stats = _iter_sessions(server, scope=scope, limit=limit)
    payload: List[Dict[str, Any]] = []
    for session_id, session in pairs:
        item = deepcopy(session)
        item["id"] = str(item.get("id") or session_id)
        payload.append(item)
    return payload


def _collect_characters(server) -> List[Dict[str, Any]]:
    data = _read_json_file(_web_file(server, "custom_personality_presets.json"), None)
    if not isinstance(data, list):
        data = getattr(server, "custom_personality_presets", None)
    return _as_dict_list(data)


def _memory_fs(server):
    """打开 MemoryFS 索引（data/web/memory_fs.json）。"""
    from nbot.memory.fs import MemoryFS

    return MemoryFS(_data_dir(server))


def _collect_memories(server) -> Dict[str, Any]:
    """记忆全量：MemoryFS 逻辑文件 + web memories.json 列表，合并成一个分类。"""
    entries = _read_json_file(_web_file(server, "memories.json"), None)
    if not isinstance(entries, list):
        entries = getattr(server, "memories", None)
    entries = _as_dict_list(entries)

    memory_files: List[Dict[str, Any]] = []
    try:
        memory_files = [item.to_dict() for item in _memory_fs(server).list_all()]
    except Exception as exc:
        _log.warning("[ExportCenter] 读取 MemoryFS 索引失败: %s", exc)

    return {
        "version": 1,
        "entries": entries,
        "memory_fs": memory_files,
        "sources": {"entries": len(entries), "memory_fs": len(memory_files)},
    }


def _without_settings_secrets(value: Any) -> Any:
    """普通数据导出不携带连接凭据；完整配置迁移走加密配置包。"""
    if isinstance(value, dict):
        return {
            key: _without_settings_secrets(item)
            for key, item in value.items()
            if not _is_secret_setting(key)
        }
    if isinstance(value, list):
        return [_without_settings_secrets(item) for item in value]
    return deepcopy(value)


def _is_secret_setting(key: Any) -> bool:
    name = str(key).lower().replace("-", "_")
    return name in {
        "password", "token", "secret", "api_key", "authorization", "private_key",
        "access_token", "refresh_token", "client_secret", "encryption_password",
    } or name.endswith(("_password", "_secret", "_api_key", "_private_key", "_token"))


def _preserve_local_settings_secrets(incoming: dict, current: dict) -> dict:
    """导入普通设置时保留目标实例的凭据，连接目标变化时要求重新配置。"""
    result = _without_settings_secrets(incoming)
    endpoint_keys = ("url", "base_url", "host", "endpoint", "provider")
    if any(incoming.get(key) != current.get(key) for key in endpoint_keys):
        return result
    for key, value in current.items():
        if _is_secret_setting(key):
            result[key] = deepcopy(value)
        elif key in result and isinstance(value, dict) and isinstance(result[key], dict):
            result[key] = _preserve_local_settings_secrets(result[key], value)
    return result


def _collect_settings(server) -> Dict[str, Any]:
    data = _read_json_file(_web_file(server, "settings.json"), None)
    if isinstance(data, dict):
        return _without_settings_secrets(data)
    memory = getattr(server, "settings", None)
    return _without_settings_secrets(memory) if isinstance(memory, dict) else {}


def _collect_hooks(server) -> List[Dict[str, Any]]:
    data = _read_json_file(_web_file(server, "hooks.json"), [])
    return _as_dict_list(data)


def _collect_workflows(server) -> List[Dict[str, Any]]:
    data = _read_json_file(_web_file(server, "workflows.json"), None)
    if not isinstance(data, list):
        data = getattr(server, "workflows", None)
    return _as_dict_list(data)


def _collect_world_books(server) -> Dict[str, Any]:
    try:
        from nbot.character.storage.world_book_store import WorldBookStore

        books: Dict[str, Any] = {}
        for book in WorldBookStore(_base_dir(server)).list_all():
            data = book.to_dict()
            book_id = str(data.get("id") or "").strip()
            if book_id:
                books[book_id] = data
        return {"world_books": books}
    except Exception as exc:
        _log.warning("[ExportCenter] 读取世界书失败，回退原始文件: %s", exc)
        raw = _read_json_file(_world_books_file(server), {})
        if isinstance(raw, dict) and isinstance(raw.get("world_books"), dict):
            return deepcopy(raw)
        return {"world_books": {}}


def _build_payload(
    server,
    category: str,
    *,
    sessions_scope: str = "all",
    session_limit: int = 0,
) -> Any:
    if category == "sessions":
        return _collect_sessions(server, scope=sessions_scope, limit=session_limit)
    if category == "characters":
        return _collect_characters(server)
    if category == "world_books":
        return _collect_world_books(server)
    if category == "memories":
        return _collect_memories(server)
    if category == "settings":
        return _collect_settings(server)
    if category == "hooks":
        return _collect_hooks(server)
    if category == "workflows":
        return _collect_workflows(server)
    raise ValueError(f"未知的导出分类: {category}")


def _payload_count(category: str, payload: Any) -> int:
    if category == "world_books":
        books = payload.get("world_books") if isinstance(payload, dict) else None
        return len(books) if isinstance(books, dict) else 0
    if category == "memories" and isinstance(payload, dict):
        entries = payload.get("entries")
        memory_fs = payload.get("memory_fs")
        return (len(entries) if isinstance(entries, list) else 0) + (
            len(memory_fs) if isinstance(memory_fs, list) else 0
        )
    if isinstance(payload, (list, dict)):
        return len(payload)
    return 0


def _payload_detail(
    server, category: str, payload: Any, sessions_scope: str
) -> Dict[str, Any]:
    """manifest 里每个分类的明细（导入侧只读 count/已知字段，多余字段会被忽略）。"""
    detail: Dict[str, Any] = {"count": _payload_count(category, payload)}
    if category == "sessions":
        _items, stats = _iter_sessions(server, scope=sessions_scope, limit=0)
        detail.update(
            {
                "scope": sessions_scope,
                "loaded": stats["loaded"],
                "disk_only": stats["disk_only"],
                "total": stats["total"],
            }
        )
    elif category == "memories" and isinstance(payload, dict):
        detail.update(
            {
                "entries": len(payload.get("entries") or []),
                "memory_fs": len(payload.get("memory_fs") or []),
            }
        )
    return detail


def _normalize_category_keys(categories: Any) -> List[str]:
    if isinstance(categories, str):
        categories = [categories]
    if not isinstance(categories, (list, tuple, set)):
        raise ExportCenterError("categories 必须是字符串数组")
    keys: List[str] = []
    for raw in categories:
        key = str(raw or "").strip()
        if not key:
            continue
        if key not in CATEGORY_META:
            raise ExportCenterError(f"未知的导出分类: {key}")
        if key not in keys:
            keys.append(key)
    if not keys:
        raise ExportCenterError("至少需要选择一个导出分类")
    return keys


# ---------------------------------------------------------------------------
# 导出
# ---------------------------------------------------------------------------


def list_export_categories(server, sessions_scope: str = "all") -> List[Dict[str, Any]]:
    """返回可导出分类及其真实条目数 / 序列化后字节数（不抛异常）。"""
    categories: List[Dict[str, Any]] = []
    for key in CATEGORY_ORDER:
        meta = CATEGORY_META[key]
        item: Dict[str, Any] = {
            "key": key,
            "label": meta["label"],
            "count": 0,
            "bytes": 0,
            "description": meta["description"],
        }
        try:
            payload = _build_payload(server, key, sessions_scope=sessions_scope)
            item["count"] = _payload_count(key, payload)
            item["bytes"] = len(_dump_json(payload))
            item.update(_payload_detail(server, key, payload, sessions_scope))
        except Exception as exc:
            _log.warning("[ExportCenter] 统计分类 %s 失败: %s", key, exc)
        categories.append(item)
    return categories


def build_export_zip(
    server,
    categories: List[str],
    sessions_scope: str = "all",
    session_limit: int = 0,
) -> bytes:
    """按分类构建导出 ZIP（只读现有存储，不做任何写入）。

    sessions_scope: ``all``（默认，含磁盘上未加载的会话）/ ``loaded``（仅内存已加载）。
    session_limit: >0 时只导最近活跃的前 N 个会话，便于大库分页导出。
    """
    keys = _normalize_category_keys(categories)
    manifest: Dict[str, Any] = {
        "format": EXPORT_FORMAT,
        "version": EXPORT_VERSION,
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "app": APP_NAME,
        "sessions_scope": sessions_scope,
        "session_limit": session_limit,
        "categories": {},
    }
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for key in keys:
            try:
                payload = _build_payload(
                    server,
                    key,
                    sessions_scope=sessions_scope,
                    session_limit=session_limit,
                )
            except Exception as exc:
                _log.warning("[ExportCenter] 导出分类 %s 失败，写入空数据: %s", key, exc)
                payload = (
                    {}
                    if key in ("world_books", "settings")
                    else {"entries": [], "memory_fs": []}
                    if key == "memories"
                    else []
                )
            archive.writestr(f"{DATA_DIR_NAME}/{key}.json", _dump_json(payload))
            manifest["categories"][key] = _payload_detail(
                server, key, payload, sessions_scope
            )
        archive.writestr(MANIFEST_NAME, _dump_json(manifest))
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# 导入：ZIP 校验
# ---------------------------------------------------------------------------


def _empty_stats(error: Optional[str] = None) -> Dict[str, Any]:
    stats: Dict[str, Any] = {"added": 0, "updated": 0, "skipped": 0}
    if error:
        stats["error"] = error
    return stats


def _is_safe_member_name(name: str) -> bool:
    if not name or name.startswith("/") or name.startswith("\\"):
        return False
    normalized = name.replace("\\", "/")
    parts = [part for part in normalized.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return False
    if ":" in parts[0]:
        return False
    return True


def _check_archive_limits(archive: zipfile.ZipFile) -> None:
    total = 0
    for info in archive.infolist():
        if info.is_dir():
            continue
        size = int(info.file_size or 0)
        if size > MAX_SINGLE_FILE:
            raise ExportCenterError(
                f"ZIP 内文件过大（上限 {MAX_SINGLE_FILE // (1024 * 1024)}MB）: {info.filename}"
            )
        total += size
    if total > MAX_TOTAL_UNCOMPRESSED:
        limit = MAX_TOTAL_UNCOMPRESSED // (1024 * 1024)
        raise ExportCenterError(f"ZIP 解压后总大小超过限制（上限 {limit}MB）")


def _read_member(archive: zipfile.ZipFile, name: str, limit: int) -> bytes:
    chunks: List[bytes] = []
    total = 0
    with archive.open(name) as handle:
        while True:
            chunk = handle.read(_READ_CHUNK)
            if not chunk:
                break
            total += len(chunk)
            if total > limit:
                raise ExportCenterError(f"ZIP 内文件解压后超过 {limit} 字节限制: {name}")
            chunks.append(chunk)
    return b"".join(chunks)


def _parse_manifest(raw: bytes) -> Dict[str, Any]:
    try:
        manifest = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise ExportCenterError(f"manifest.json 解析失败: {exc}") from exc
    if not isinstance(manifest, dict):
        raise ExportCenterError("manifest.json 必须是 JSON 对象")
    if manifest.get("format") != EXPORT_FORMAT:
        raise ExportCenterError("manifest.json format 不匹配，不是有效的导出包")
    version = manifest.get("version")
    if not isinstance(version, int) or version < 1:
        raise ExportCenterError("manifest.json version 无效")
    if version > EXPORT_VERSION:
        raise ExportCenterError(
            f"导出包版本 {version} 高于当前支持的版本 {EXPORT_VERSION}"
        )
    if not isinstance(manifest.get("categories"), dict):
        raise ExportCenterError("manifest.json 缺少 categories 字段")
    return manifest


# ---------------------------------------------------------------------------
# 导入：合并策略
# ---------------------------------------------------------------------------


def _merge_list(
    existing: Any, incoming: List[Any], mode: str
) -> Tuple[List[Dict[str, Any]], int, int, int]:
    items: List[Dict[str, Any]] = _as_dict_list(existing)
    index: Dict[str, int] = {}
    for position, item in enumerate(items):
        key = _item_id(item)
        if key:
            index[key] = position

    added = 0
    updated = 0
    skipped = 0
    for raw in incoming:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        item = deepcopy(raw)
        key = _item_id(item)
        if key and key in index:
            if mode == "overwrite":
                items[index[key]] = item
                updated += 1
            else:
                skipped += 1
            continue
        if not key and mode == "merge" and items:
            skipped += 1
            continue
        items.append(item)
        if key:
            index[key] = len(items) - 1
        added += 1
    return items, added, updated, skipped


def _find_session(server, session_id: str) -> Optional[Dict[str, Any]]:
    store = getattr(server, "session_store", None)
    getter = getattr(store, "get_session", None)
    if callable(getter):
        try:
            session = getter(session_id)
        except Exception:
            session = None
        if isinstance(session, dict):
            return session

    sessions = getattr(server, "sessions", None)
    if isinstance(sessions, dict):
        session = sessions.get(session_id)
        if isinstance(session, dict):
            return session
    return None


def _store_session(server, session_id: str, session: Dict[str, Any]) -> None:
    store = getattr(server, "session_store", None)
    setter = getattr(store, "set_session", None)
    if callable(setter):
        setter(session_id, session)
        return

    from nbot.web.sessions_db import upsert_session

    upsert_session(_data_dir(server), session_id, session)


def _import_sessions(server, payload: Any, mode: str) -> Dict[str, int]:
    if not isinstance(payload, list):
        raise ValueError("会话数据必须是 JSON 数组")
    added = 0
    updated = 0
    skipped = 0
    disk_sessions = dict(_disk_sessions(server))
    hydrated = False
    for raw in payload:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        session_id = str(raw.get("id") or raw.get("session_id") or "").strip()
        if not session_id:
            skipped += 1
            continue
        existing = _find_session(server, session_id)
        if existing is None:
            existing = disk_sessions.get(session_id)
        if existing is not None and mode != "overwrite":
            skipped += 1
            continue
        session = deepcopy(raw)
        session["id"] = session_id
        if not hydrated:
            # set_session 的保存回调会重写整张会话表，先补齐尚未加载的磁盘记录。
            store = getattr(server, "session_store", None)
            live = getattr(store, "sessions", None)
            if not isinstance(live, dict):
                live = getattr(server, "sessions", None)
            db_path = os.path.join(_data_dir(server), "sessions.db")
            if isinstance(live, dict) and os.path.isfile(db_path):
                from nbot.web.sessions_db import load_sessions

                for sid, record in load_sessions(_data_dir(server)).items():
                    live.setdefault(sid, record)
            hydrated = True
        _store_session(server, session_id, session)
        disk_sessions[session_id] = session
        if existing is not None:
            updated += 1
        else:
            added += 1
    return {"added": added, "updated": updated, "skipped": skipped}


def _import_list_category(
    server,
    payload: Any,
    mode: str,
    filename: str,
    attr: Optional[str],
    label: str,
) -> Dict[str, int]:
    if not isinstance(payload, list):
        raise ValueError(f"{label}数据必须是 JSON 数组")
    path = _web_file(server, filename)
    current = _read_json_file(path, None)
    if not isinstance(current, list):
        memory = getattr(server, attr, None) if attr else None
        current = deepcopy(memory) if isinstance(memory, list) else []

    merged, added, updated, skipped = _merge_list(current, payload, mode)
    _write_json_file(path, merged)
    if attr and hasattr(server, attr):
        try:
            setattr(server, attr, deepcopy(merged))
        except Exception as exc:
            _log.debug("[ExportCenter] 同步内存属性 %s 失败: %s", attr, exc)
    return {"added": added, "updated": updated, "skipped": skipped}


def _import_settings(server, payload: Any, mode: str) -> Dict[str, int]:
    if not isinstance(payload, dict):
        raise ValueError("面板设置必须是 JSON 对象")
    path = _web_file(server, "settings.json")
    current = _read_json_file(path, None)
    if not isinstance(current, dict):
        memory = getattr(server, "settings", None)
        current = dict(memory) if isinstance(memory, dict) else {}

    if mode == "overwrite":
        stats = {
            "added": 0 if current else 1,
            "updated": 1 if current else 0,
            "skipped": 0,
        }
        data_to_save = _preserve_local_settings_secrets(payload, current)
    else:
        if current:
            return {"added": 0, "updated": 0, "skipped": 1}
        stats = {"added": 1, "updated": 0, "skipped": 0}
        data_to_save = _without_settings_secrets(payload)

    _write_json_file(path, data_to_save)
    try:
        server.settings = deepcopy(data_to_save)
    except Exception as exc:
        _log.debug("[ExportCenter] 同步 server.settings 失败: %s", exc)
    return stats


def _normalize_world_book(book_id: str, book: Dict[str, Any]) -> Dict[str, Any]:
    data = deepcopy(book)
    data["id"] = str(data.get("id") or book_id)

    entries = data.get("entries")
    entry_map: Dict[str, Any] = {}
    if isinstance(entries, dict):
        for entry_id, entry in entries.items():
            if isinstance(entry, dict):
                entry_map[str(entry_id)] = entry
    elif isinstance(entries, list):
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            entry_id = str(entry.get("id") or "").strip() or uuid.uuid4().hex[:12]
            normalized_entry = dict(entry)
            normalized_entry["id"] = entry_id
            entry_map[entry_id] = normalized_entry
    data["entries"] = entry_map
    return data


def _import_world_books(server, payload: Any, mode: str) -> Dict[str, int]:
    if not isinstance(payload, dict):
        raise ValueError("世界书数据必须是 JSON 对象")
    books_payload = payload.get("world_books")
    if not isinstance(books_payload, dict) and "world_books" not in payload:
        books_payload = payload
    if not isinstance(books_payload, dict):
        raise ValueError("世界书数据必须是 world_books 对象结构")

    path = _world_books_file(server)
    raw = _read_json_file(path, {})
    current = raw.get("world_books") if isinstance(raw, dict) else None
    merged: Dict[str, Any] = {}
    if isinstance(current, dict):
        merged = {
            str(book_id): deepcopy(book)
            for book_id, book in current.items()
            if isinstance(book, dict)
        }

    added = 0
    updated = 0
    skipped = 0
    for raw_id, book in books_payload.items():
        book_id = str(raw_id or "").strip()
        if not book_id or not isinstance(book, dict):
            skipped += 1
            continue
        normalized = _normalize_world_book(book_id, book)
        if book_id in merged:
            if mode != "overwrite":
                skipped += 1
                continue
            merged[book_id] = normalized
            updated += 1
        else:
            merged[book_id] = normalized
            added += 1

    if added or updated:
        # 与 WorldBookStore 共用同一文件与同一数据格式（{"world_books": {id: book}}）
        from nbot.character.storage.json_store import JsonStore

        JsonStore(path).set("world_books", merged)

    return {"added": added, "updated": updated, "skipped": skipped}


def _sync_hook_runtime(server) -> None:
    """尽力让已初始化的 HookManager 立即看到导入的 Hook（不新建单例）。"""
    try:
        from nbot.hooks import manager as hook_manager_module

        manager = getattr(hook_manager_module, "_hook_manager", None)
        if manager is None:
            return
        target = os.path.abspath(_web_file(server, "hooks.json"))
        if os.path.abspath(str(getattr(manager, "_hooks_file", ""))) != target:
            return

        from nbot.hooks.models import ConversationHook

        hooks = _read_json_file(target, [])
        if not isinstance(hooks, list):
            return
        for item in hooks:
            if isinstance(item, dict):
                manager.add_hook(ConversationHook.from_dict(item))
    except Exception as exc:
        _log.debug("[ExportCenter] 同步 HookManager 失败: %s", exc)


def _is_safe_memory_path(path: str) -> bool:
    """MemoryFS 路径校验：相对路径、无 ..、无盘符、无空段。"""
    text = str(path or "").strip().replace("\\", "/")
    if not text or text.startswith("/"):
        return False
    parts = [part for part in text.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts):
        return False
    return ":" not in parts[0]


def _import_memory_fs(server, items: List[Any], mode: str) -> Dict[str, int]:
    """把 MemoryFS 逻辑文件写回 ``data/web/memory_fs.json``。"""
    added = 0
    updated = 0
    skipped = 0
    if not items:
        return {"added": 0, "updated": 0, "skipped": 0}

    try:
        memory_fs = _memory_fs(server)
    except Exception as exc:
        _log.warning("[ExportCenter] MemoryFS 不可用，跳过 memory_fs 导入: %s", exc)
        return {"added": 0, "updated": 0, "skipped": len(items)}

    for raw in items:
        if not isinstance(raw, dict):
            skipped += 1
            continue
        path = str(raw.get("path") or "").strip()
        if not _is_safe_memory_path(path):
            skipped += 1
            continue
        try:
            existing = memory_fs.read(path)
        except Exception:
            existing = None
        if existing is not None and mode != "overwrite":
            skipped += 1
            continue
        tags = raw.get("tags")
        memory_ids = raw.get("memory_ids")
        try:
            importance = float(raw.get("importance") or 0.0)
        except (TypeError, ValueError):
            importance = 0.0
        try:
            memory_fs.write(
                path,
                character_id=str(raw.get("character_id") or ""),
                target_id=str(raw.get("target_id") or ""),
                title=str(raw.get("title") or ""),
                content=str(raw.get("content") or ""),
                summary=str(raw.get("summary") or ""),
                tags=[str(item) for item in tags] if isinstance(tags, list) else [],
                importance=importance,
                memory_ids=(
                    [str(item) for item in memory_ids] if isinstance(memory_ids, list) else []
                ),
            )
        except Exception as exc:
            _log.warning("[ExportCenter] 写入 MemoryFS 失败 %s: %s", path, exc)
            skipped += 1
            continue
        if existing is not None:
            updated += 1
        else:
            added += 1
    return {"added": added, "updated": updated, "skipped": skipped}


def _split_memories_payload(payload: Any) -> Tuple[List[Any], List[Any]]:
    """兼容新旧格式：旧版是数组，新版是 {entries, memory_fs}。"""
    if isinstance(payload, list):
        return payload, []
    if isinstance(payload, dict):
        entries = payload.get("entries")
        memory_fs = payload.get("memory_fs")
        return (
            entries if isinstance(entries, list) else [],
            memory_fs if isinstance(memory_fs, list) else [],
        )
    raise ValueError("记忆数据必须是 JSON 数组或 {entries, memory_fs} 对象")


def _import_memories(server, payload: Any, mode: str) -> Dict[str, Any]:
    """记忆导入：web memories.json 列表 + MemoryFS 逻辑文件，两条通道都写。"""
    entries, memory_fs_items = _split_memories_payload(payload)
    entry_stats = {"added": 0, "updated": 0, "skipped": 0}
    if entries:
        entry_stats = _import_list_category(
            server, entries, mode, "memories.json", "memories", "记忆"
        )
    fs_stats = _import_memory_fs(server, memory_fs_items, mode)
    return {
        "added": entry_stats["added"] + fs_stats["added"],
        "updated": entry_stats["updated"] + fs_stats["updated"],
        "skipped": entry_stats["skipped"] + fs_stats["skipped"],
        "entries": entry_stats,
        "memory_fs": fs_stats,
    }


def _import_category(server, category: str, payload: Any, mode: str) -> Dict[str, int]:
    if category == "sessions":
        return _import_sessions(server, payload, mode)
    if category == "settings":
        return _import_settings(server, payload, mode)
    if category == "world_books":
        return _import_world_books(server, payload, mode)
    if category == "characters":
        return _import_list_category(
            server,
            payload,
            mode,
            "custom_personality_presets.json",
            "custom_personality_presets",
            "角色卡",
        )
    if category == "memories":
        return _import_memories(server, payload, mode)
    if category == "workflows":
        return _import_list_category(
            server,
            payload,
            mode,
            "workflows.json",
            "workflows",
            "工作流",
        )
    if category == "hooks":
        stats = _import_list_category(server, payload, mode, "hooks.json", None, "Hook 规则")
        _sync_hook_runtime(server)
        return stats
    raise ValueError(f"未知的导出分类: {category}")


def import_export_zip(server, blob: bytes, mode: str = "merge") -> Dict[str, Any]:
    """导入导出 ZIP。ZIP 本身损坏 / manifest 非法时抛 ExportCenterError。"""
    normalized_mode = str(mode or "merge").strip().lower()
    if normalized_mode not in ("merge", "overwrite"):
        raise ExportCenterError("mode 只能是 merge 或 overwrite")
    if not blob:
        raise ExportCenterError("导入文件为空")

    try:
        archive = zipfile.ZipFile(io.BytesIO(blob))
    except (zipfile.BadZipFile, EOFError, OSError, ValueError) as exc:
        raise ExportCenterError("ZIP 文件损坏或格式不正确") from exc

    warnings: List[str] = []
    results: Dict[str, Dict[str, Any]] = {}
    with archive:
        _check_archive_limits(archive)
        names = set(archive.namelist())
        if MANIFEST_NAME not in names:
            raise ExportCenterError("缺少 manifest.json，不是有效的导出包")
        manifest = _parse_manifest(_read_member(archive, MANIFEST_NAME, MAX_SINGLE_FILE))
        declared = manifest.get("categories")
        if not isinstance(declared, dict):
            raise ExportCenterError("manifest.json 缺少 categories 字段")

        for raw_key in declared:
            key = str(raw_key)
            if key not in CATEGORY_META:
                warnings.append(f"忽略未知分类: {key}")
                continue

            member = f"{DATA_DIR_NAME}/{key}.json"
            if not _is_safe_member_name(member) or member not in names:
                warnings.append(f"分类 {key} 缺少数据文件 {member}，已跳过")
                results[key] = _empty_stats(error="missing")
                continue

            try:
                payload = json.loads(_read_member(archive, member, MAX_SINGLE_FILE).decode("utf-8"))
            except ExportCenterError:
                raise
            except Exception as exc:
                warnings.append(f"分类 {key} 的 JSON 解析失败: {exc}")
                results[key] = _empty_stats(error="invalid-json")
                continue

            try:
                results[key] = _import_category(server, key, payload, normalized_mode)
            except Exception as exc:
                _log.warning("[ExportCenter] 导入分类 %s 失败: %s", key, exc)
                warnings.append(f"分类 {key} 导入失败: {exc}")
                results[key] = _empty_stats(error=str(exc))

    raw_scope = str(manifest.get("sessions_scope") or "").strip().lower()
    sessions_scope = raw_scope if raw_scope in ("all", "loaded") else ""
    try:
        session_limit = int(manifest.get("session_limit") or 0)
    except (TypeError, ValueError):
        session_limit = 0

    return {
        "success": True,
        "mode": normalized_mode,
        "created_at": manifest.get("created_at"),
        "sessions_scope": sessions_scope,
        "session_limit": max(0, session_limit),
        "imported": results,
        "warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Flask 路由
# ---------------------------------------------------------------------------


def register_export_center_routes(app, server):
    @app.route("/api/export-center/categories", methods=["GET"])
    def export_center_categories():
        scope = str(request.args.get("sessions_scope") or "all").strip().lower()
        if scope not in ("all", "loaded"):
            return jsonify({"success": False, "error": "sessions_scope 只能是 all 或 loaded"}), 400
        try:
            categories = list_export_categories(server, sessions_scope=scope)
        except Exception as exc:
            _log.error("[ExportCenter] 统计导出分类失败: %s", exc)
            return jsonify({"success": False, "error": f"读取导出分类失败: {exc}"}), 500
        return jsonify(
            {"success": True, "sessions_scope": scope, "categories": categories}
        )

    @app.route("/api/export-center/export", methods=["POST"])
    def export_center_export():
        data = request.get_json(silent=True) or {}
        categories = data.get("categories")
        if categories is None:
            categories = data.get("keys")
        scope = str(data.get("sessions_scope") or "all").strip().lower()
        try:
            limit = int(data.get("session_limit") or 0)
        except (TypeError, ValueError):
            return jsonify({"success": False, "error": "session_limit 必须是整数"}), 400
        if limit < 0:
            return jsonify({"success": False, "error": "session_limit 不能为负数"}), 400
        try:
            blob = build_export_zip(
                server,
                categories if categories is not None else [],
                sessions_scope=scope,
                session_limit=limit,
            )
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            _log.error("[ExportCenter] 导出失败: %s", exc)
            return jsonify({"success": False, "error": f"导出失败: {exc}"}), 500

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        filename = f"nekobot-export-{stamp}.zip"
        return send_file(
            io.BytesIO(blob),
            mimetype="application/zip",
            as_attachment=True,
            download_name=filename,
        )

    @app.route("/api/export-center/import", methods=["POST"])
    def export_center_import():
        upload = request.files.get("file") if request.files else None
        if upload is None:
            return jsonify({"success": False, "error": "未选择导入文件"}), 400

        mode = str(request.form.get("mode") or request.args.get("mode") or "merge")
        blob = upload.read()
        try:
            result = import_export_zip(server, blob, mode=mode)
        except ValueError as exc:
            return jsonify({"success": False, "error": str(exc)}), 400
        except Exception as exc:
            _log.error("[ExportCenter] 导入失败: %s", exc)
            return jsonify({"success": False, "error": f"导入失败: {exc}"}), 500

        log_message = getattr(server, "log_message", None)
        if callable(log_message):
            try:
                imported_keys = ",".join(sorted(result.get("imported", {})))
                log_message("info", f"数据导出中心导入完成: {imported_keys}", important=True)
            except Exception:
                pass
        return jsonify(result)

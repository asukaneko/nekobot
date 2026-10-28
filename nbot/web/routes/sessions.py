import logging
import os
import uuid
from copy import deepcopy
from datetime import datetime

from flask import jsonify, request
from nbot.web.message_pagination import message_page

from nbot.core import WebSessionStore
from nbot.core.prompt_format import format_skills_prompt
from nbot.core.session_store import (
    ensure_message_variants,
    is_message_deleted,
    select_message_variant,
    visible_messages,
)
from nbot.web.persistence import is_web_visible_session
from nbot.web.sessions_db import get_session as get_session_from_db

_log = logging.getLogger(__name__)


def _expand_session_macros(text, user_name="", char_name=""):
    """展开开场白/背景设定中的宏（{{user}}/{{char}}/{{time}}/{{random}} 等）。"""
    if not text:
        return text
    try:
        from nbot.core.macros import build_context, expand

        return expand(
            text,
            build_context(user_name=user_name or "", char_name=char_name or ""),
        )
    except Exception as exc:  # pragma: no cover - 宏异常不应阻断建会话
        _log.warning("展开会话宏失败: %s", exc)
        return text


def _normalize_tags(tags):
    if isinstance(tags, str):
        tags = [part.strip() for part in tags.replace("，", ",").split(",")]
    if not isinstance(tags, list):
        return []
    normalized = []
    seen = set()
    for tag in tags:
        tag = str(tag or "").strip()
        if not tag:
            continue
        tag = tag[:24]
        key = tag.lower()
        if key in seen:
            continue
        seen.add(key)
        normalized.append(tag)
        if len(normalized) >= 20:
            break
    return normalized


def _resolve_session_character_name(server, session):
    if not isinstance(session, dict):
        session = {}

    sender_name = str(session.get("sender_name") or "").strip()
    if sender_name:
        return sender_name

    character_id = str(session.get("character_id") or "").strip()
    if character_id:
        try:
            from nbot.character.repository import ProfileRepository

            base_dir = getattr(server, "base_dir", None) or os.path.abspath(
                os.path.join(os.path.dirname(__file__), "..", "..", "..")
            )
            profile = ProfileRepository(base_dir).get(character_id)
            if profile and profile.name:
                return str(profile.name).strip()
        except Exception:
            pass

    personality = getattr(server, "personality", {}) or {}
    if isinstance(personality, dict):
        return str(personality.get("name") or "").strip()
    return ""


def _static_portrait_exists(server, portrait_url):
    portrait_url = str(portrait_url or "").strip()
    if not portrait_url:
        return False
    if portrait_url.startswith(("http://", "https://", "data:", "blob:")):
        return True
    if not portrait_url.startswith("/static/"):
        return False
    base_dir = getattr(server, "base_dir", None) or os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )
    static_rel = portrait_url[len("/static/") :].replace("/", os.sep)
    return os.path.exists(os.path.join(base_dir, "nbot", "web", "static", static_rel))


def _find_character_visuals(server, session, sessions_data=None):
    session = session if isinstance(session, dict) else {}
    sessions_data = sessions_data or {}
    candidates = []

    source_id = str(session.get("source_session_id") or "").strip()
    if source_id and isinstance(sessions_data.get(source_id), dict):
        candidates.append(sessions_data[source_id])

    character_id = str(session.get("character_id") or "").strip()
    sender_name = str(session.get("sender_name") or "").strip()
    for preset in getattr(server, "custom_personality_presets", []) or []:
        if not isinstance(preset, dict):
            continue
        if character_id and str(preset.get("id") or "") == character_id:
            candidates.append(preset)
            break
    for preset in getattr(server, "custom_personality_presets", []) or []:
        if not isinstance(preset, dict):
            continue
        names = {
            str(preset.get("name") or "").strip(),
            str(preset.get("sender_name") or "").strip(),
        }
        if sender_name and sender_name in names:
            candidates.append(preset)
            break

    personality = getattr(server, "personality", {}) or {}
    if isinstance(personality, dict):
        personality_name = str(personality.get("name") or "").strip()
        if sender_name and sender_name == personality_name:
            candidates.append(personality)

    first_avatar = ""
    for candidate in candidates:
        portrait = str(
            candidate.get("sender_portrait")
            or candidate.get("portrait")
            or ""
        ).strip()
        avatar = str(
            candidate.get("sender_avatar")
            or candidate.get("avatar")
            or ""
        ).strip()
        if portrait and _static_portrait_exists(server, portrait):
            return portrait, avatar
        if avatar and not first_avatar:
            first_avatar = avatar
    return "", first_avatar


def _repair_session_visuals(server, session, sessions_data=None):
    if not isinstance(session, dict):
        return "", "", False

    portrait = str(session.get("sender_portrait") or "").strip()
    avatar = str(session.get("sender_avatar") or "").strip()
    changed = False

    if portrait and not _static_portrait_exists(server, portrait):
        portrait = ""
        changed = True

    if not portrait:
        fallback_portrait, fallback_avatar = _find_character_visuals(
            server, session, sessions_data
        )
        if fallback_portrait:
            portrait = fallback_portrait
            changed = True
        if not avatar and fallback_avatar:
            avatar = fallback_avatar
            changed = True

    if session.get("sender_portrait", "") != portrait:
        session["sender_portrait"] = portrait
        changed = True
    if avatar and session.get("sender_avatar", "") != avatar:
        session["sender_avatar"] = avatar
        changed = True

    return portrait, avatar, changed


def _resolve_session_runtime_character_id(session):
    if not isinstance(session, dict):
        return ""

    snapshot = session.get("character_runtime_snapshot")
    if isinstance(snapshot, dict):
        character_id = str(snapshot.get("character_id") or "").strip()
        if character_id:
            return character_id

    timeline = session.get("character_runtime_timeline")
    if isinstance(timeline, list):
        for entry in reversed(timeline):
            if not isinstance(entry, dict):
                continue
            character_id = str(entry.get("character_id") or "").strip()
            if character_id:
                return character_id

    return str(session.get("character_id") or session.get("sender_name") or "").strip()


def _is_same_session_character(old_character_id, old_name, new_character_id, new_name):
    old_values = {
        str(value or "").strip()
        for value in (old_character_id, old_name)
        if str(value or "").strip()
    }
    new_values = {
        str(value or "").strip()
        for value in (new_character_id, new_name)
        if str(value or "").strip()
    }
    return bool(old_values and new_values and old_values.intersection(new_values))


def _retarget_session_runtime_character(server, session_id, session, old_character_id, new_character_id):
    old_character_id = str(old_character_id or "").strip()
    new_character_id = str(new_character_id or "").strip()
    if not old_character_id or not new_character_id or old_character_id == new_character_id:
        return

    snapshot = session.get("character_runtime_snapshot")
    if isinstance(snapshot, dict) and str(snapshot.get("character_id") or "").strip() == old_character_id:
        snapshot["character_id"] = new_character_id

    timeline = session.get("character_runtime_timeline")
    if isinstance(timeline, list):
        for entry in timeline:
            if isinstance(entry, dict) and str(entry.get("character_id") or "").strip() == old_character_id:
                entry["character_id"] = new_character_id

    try:
        from nbot.character.repository import (
            CharacterStateRepository,
            RelationshipRepository,
        )

        base_dir = _get_base_dir(server)
        target_scope = f"web:{session_id}"

        state_repo = CharacterStateRepository(base_dir)
        state = state_repo.get(old_character_id, target_scope)
        if state:
            state.character_id = new_character_id
            state_repo.save(state)

        relationship_repo = RelationshipRepository(base_dir)
        relationship = relationship_repo.get(old_character_id, target_scope)
        if relationship:
            relationship.character_id = new_character_id
            relationship_repo.save(relationship)
    except Exception as exc:
        _log.warning(
            "[CharacterRuntime] failed to retarget runtime state %s -> %s for session %s: %s",
            old_character_id,
            new_character_id,
            session_id,
            exc,
            exc_info=True,
        )


def _runtime_snapshot_signature(snapshot):
    if not isinstance(snapshot, dict):
        return {}
    keys = (
        "character_id",
        "mood",
        "mood_intensity",
        "energy",
        "affection",
        "trust",
        "security",
        "familiarity",
        "dependency",
        "jealousy",
        "visible_emotion",
        "hidden_emotion",
        "message_id",
        "message_index",
    )
    return {key: snapshot.get(key) for key in keys if key in snapshot}


def _normalize_runtime_timeline_entry(snapshot, timestamp=None):
    entry = _runtime_snapshot_signature(snapshot)
    entry["timestamp"] = timestamp or datetime.now().isoformat()
    return entry


def _runtime_timeline_state_signature(entry):
    if not isinstance(entry, dict):
        return None
    ignore_keys = {"timestamp", "message_id", "message_index"}
    return tuple(
        (key, entry.get(key))
        for key in sorted(entry.keys())
        if key not in ignore_keys
    )


def _ensure_runtime_timeline_from_snapshot(session):
    timeline = session.get("character_runtime_timeline", [])
    if not isinstance(timeline, list):
        timeline = []

    if timeline:
        return timeline, False

    snapshot = session.get("character_runtime_snapshot")
    if not isinstance(snapshot, dict):
        return timeline, False

    timestamp = (
        session.get("updated_at")
        or session.get("created_at")
        or datetime.now().isoformat()
    )
    timeline = [_normalize_runtime_timeline_entry(snapshot, timestamp)]
    session["character_runtime_timeline"] = timeline
    return timeline, True


def _normalize_proactive_chat_config(config):
    defaults = {
        "enabled": False,
        "interval_minutes": 60,
        "idle_minutes": 10,
        "visible_only": True,
    }
    if isinstance(config, dict):
        defaults.update(config)
    try:
        defaults["interval_minutes"] = max(1, int(defaults.get("interval_minutes", 60)))
    except (TypeError, ValueError):
        defaults["interval_minutes"] = 60
    try:
        defaults["idle_minutes"] = max(1, int(defaults.get("idle_minutes", 10)))
    except (TypeError, ValueError):
        defaults["idle_minutes"] = 10
    defaults["enabled"] = bool(defaults.get("enabled", False))
    defaults["visible_only"] = bool(defaults.get("visible_only", True))
    return defaults


def _sync_proactive_chat_to_heartbeat(server, session_id: str, proactive_chat_config):
    """把会话详情的"主动聊天"桥接到 SessionHeartbeatManager。

    proactive_chat 复用统一的心跳调度系统，配置 key 为 "{session_id}__proactive"，
    与"同步现实时间"的 __life_sim 以及用户手动配置的心跳隔离。
    与 life_sim 不同，proactive_chat 会向会话发送 AI 回复，但触发指令本身不写入会话。
    """
    try:
        from nbot.gateway.heartbeat import get_session_heartbeat_manager

        manager = get_session_heartbeat_manager()
        if not manager:
            _log.debug("[Sessions] SessionHeartbeatManager not available, skip proactive chat")
            return

        config = proactive_chat_config if isinstance(proactive_chat_config, dict) else {}
        enabled = bool(config.get("enabled", False))
        interval_minutes = config.get("interval_minutes", 60) or 60
        try:
            interval_minutes = max(1, int(interval_minutes))
        except (TypeError, ValueError):
            interval_minutes = 60

        proactive_key = f"{session_id}__proactive"
        if enabled:
            manager.set_config(proactive_key, {
                "enabled": True,
                # silent 专用于 life_sim（完全不发消息）。主动聊天由
                # proactive_chat 分支执行：静默注入内部指令，只保存 AI 回复。
                "silent": False,
                "proactive_chat": True,
                "target_session_id": session_id,
                "interval_minutes": interval_minutes,
                "internal_prompt": str(config.get("prompt") or "").strip(),
            })
            _log.info(
                "[Sessions] proactive chat heartbeat registered for %s (interval=%dmin)",
                session_id, interval_minutes,
            )
        else:
            manager.set_config(proactive_key, {
                "enabled": False,
                "silent": False,
                "proactive_chat": True,
                "target_session_id": session_id,
            })
            _log.info("[Sessions] proactive chat heartbeat disabled for %s", session_id)

        # 确保调度器在运行（各会话按自己的 interval 判定是否到期）
        if enabled and manager.any_enabled() and hasattr(server, "_start_heartbeat_job"):
            server._start_heartbeat_job(1)
        elif not manager.any_enabled() and hasattr(server, "_stop_heartbeat_job"):
            server._stop_heartbeat_job()
    except Exception as exc:
        _log.warning("[Sessions] proactive chat heartbeat sync failed: %s", exc, exc_info=True)


def _normalize_tts_config(data):
    defaults = {"enabled": False, "model_id": "", "voice": ""}
    if isinstance(data, dict):
        defaults["enabled"] = bool(data.get("enabled", False))
        defaults["model_id"] = str(data.get("model_id", ""))
        defaults["voice"] = str(data.get("voice", ""))
    return defaults


def _normalize_message_favorite_collections(raw_favorites):
    if not isinstance(raw_favorites, list):
        return []

    collections = []
    legacy_messages = []
    for item in raw_favorites:
        if not isinstance(item, dict):
            continue
        if isinstance(item.get("messages"), list):
            messages = [deepcopy(msg) for msg in item.get("messages", []) if isinstance(msg, dict)]
            collections.append(
                {
                    "id": str(item.get("id") or item.get("favorite_id") or uuid.uuid4()),
                    "title": str(item.get("title") or "未命名收藏"),
                    "created_at": item.get("created_at") or item.get("favorited_at") or datetime.now().isoformat(),
                    "messages": messages,
                }
            )
        elif item.get("message_id") or item.get("id"):
            legacy_messages.append(deepcopy(item))

    if legacy_messages:
        collections.insert(
            0,
            {
                "id": str(uuid.uuid4()),
                "title": "旧版收藏",
                "created_at": legacy_messages[0].get("favorited_at") or datetime.now().isoformat(),
                "messages": legacy_messages,
            },
        )
    return collections


def _skills_prompt_injection_enabled(settings):
    features = (settings or {}).get("features") or {}
    return bool(features.get("skills_prompt_injection", False))


def _get_base_dir(server):
    return getattr(server, "base_dir", os.getcwd())


def _save_character_runtime_snapshot(server, character_id, target_session_id, snapshot):
    if not character_id or not target_session_id or not isinstance(snapshot, dict):
        return

    from nbot.character.models import CharacterState, RelationshipState
    from nbot.character.repository import CharacterStateRepository, RelationshipRepository

    base_dir = _get_base_dir(server)
    target_scope = f"web:{target_session_id}"
    now = datetime.now().isoformat()

    state_repo = CharacterStateRepository(base_dir)
    state_repo.save(
        CharacterState(
            character_id=character_id,
            scope_id=target_scope,
            mood=snapshot.get("mood", "平静"),
            mood_intensity=snapshot.get("mood_intensity", 0.5),
            energy=snapshot.get("energy", 70),
            updated_at=now,
        )
    )

    relationship_repo = RelationshipRepository(base_dir)
    relationship_repo.save(
        RelationshipState(
            character_id=character_id,
            target_id=target_scope,
            affection=snapshot.get("affection", 50),
            trust=snapshot.get("trust", 50),
            familiarity=snapshot.get("familiarity", 30),
            dependency=snapshot.get("dependency", 30),
            security=snapshot.get("security", 50),
            jealousy=snapshot.get("jealousy", 0),
            updated_at=now,
        )
    )


def _copy_character_runtime_state(
    server,
    character_id,
    source_session_id,
    target_session_id,
    snapshot=None,
):
    if not source_session_id or not target_session_id:
        return

    try:
        source_session = getattr(server, "sessions", {}).get(source_session_id, {}) or {}
        character_id = str(
            (
                snapshot.get("character_id")
                if isinstance(snapshot, dict)
                else None
            )
            or character_id
            or _resolve_session_runtime_character_id(source_session)
        ).strip()
        if not character_id:
            return

        if isinstance(snapshot, dict):
            _save_character_runtime_snapshot(
                server,
                character_id,
                target_session_id,
                snapshot,
            )
            return

        from nbot.character.repository import (
            CharacterStateRepository,
            RelationshipRepository,
        )

        base_dir = _get_base_dir(server)
        source_scope = f"web:{source_session_id}"
        target_scope = f"web:{target_session_id}"

        state_repo = CharacterStateRepository(base_dir)
        state = state_repo.get(character_id, source_scope)
        if state:
            state.scope_id = target_scope
            state_repo.save(state)

        relationship_repo = RelationshipRepository(base_dir)
        source_targets = [
            source_scope,
            source_session.get("user_id"),
            source_session.get("qq_id"),
            source_session_id,
        ]
        relationship = None
        for source_target in source_targets:
            if not source_target:
                continue
            relationship = relationship_repo.get(character_id, str(source_target))
            if relationship:
                break
        if relationship:
            relationship.target_id = target_scope
            relationship_repo.save(relationship)
    except Exception as exc:
        _log.warning(
            "[CharacterRuntime] failed to copy fork runtime state %s -> %s: %s",
            source_session_id,
            target_session_id,
            exc,
            exc_info=True,
        )


def restore_runtime_state_to_path(server, session_id, session, path_nodes):
    """将会话的角色运行时状态/时间线回退/定位到给定节点路径的末端。

    供剧情"切换分支"与"回溯"复用：
    - 用路径末节点的 state/relationship 快照重建 character_runtime_snapshot；
    - 用路径上每个带快照的节点重建 character_runtime_timeline（保持历史）；
    - 同步写回 live 仓库（CharacterStateRepository/RelationshipRepository）。

    path_nodes: 节点 dict 列表（含 state_snapshot/relationship_snapshot/id）。
    返回 True 表示有状态被恢复。
    """
    if not isinstance(session, dict) or not path_nodes:
        return False

    def _combined(node):
        snap = {}
        snap.update(node.get("state_snapshot") or {})
        snap.update(node.get("relationship_snapshot") or {})
        return snap

    # 末端节点的状态快照
    tail = path_nodes[-1]
    tail_snapshot = _combined(tail)
    if not tail_snapshot:
        # 路径末端没有运行时快照（如历史节点）——不强行改写，避免污染
        return False

    character_id = str(_resolve_session_runtime_character_id(session) or "").strip()
    if not character_id:
        character_id = str(session.get("character_id") or "").strip()

    # 1) 重建时间线（仅含有快照的节点，按路径顺序）
    timeline = []
    for node in path_nodes:
        snap = _combined(node)
        if not snap:
            continue
        entry = _normalize_runtime_timeline_entry(snap)
        entry["message_id"] = f"pm_a_{node.get('id', '')}"
        timeline.append(entry)
    session["character_runtime_timeline"] = timeline[-200:]

    # 2) 写回会话快照
    tail_snapshot = dict(tail_snapshot)
    if character_id:
        tail_snapshot.setdefault("character_id", character_id)
    session["character_runtime_snapshot"] = tail_snapshot

    # 3) 同步 live 仓库
    if character_id:
        try:
            _save_character_runtime_snapshot(server, character_id, session_id, tail_snapshot)
        except Exception as exc:
            _log.warning(
                "[CharacterRuntime] restore live state failed for %s: %s",
                session_id, exc, exc_info=True,
            )
    return True


def register_session_routes(app, server):
    session_store = WebSessionStore(
        server.sessions, save_callback=lambda: server._save_data("sessions")
    )

    def _get_web_session(session_id):
        session = session_store.get_session(session_id)
        if not session:
            session = get_session_from_db(server.data_dir, session_id)
        if not session or not is_web_visible_session(session_id, session):
            return None
        return session

    def _find_message_index(messages, message_id):
        if not message_id:
            return -1
        return next(
            (idx for idx, msg in enumerate(messages) if str(msg.get("id")) == str(message_id)),
            -1,
        )

    def _parse_iso_datetime(value):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except Exception:
            return None

    def _message_timestamp(message):
        if not isinstance(message, dict):
            return None
        return (
            message.get("timestamp")
            or message.get("created_at")
            or message.get("updated_at")
        )

    def _select_runtime_timeline_for_fork(session, messages, message_index):
        timeline, _ = _ensure_runtime_timeline_from_snapshot(session)
        if not timeline:
            return [], None

        message_ids = {
            str(msg.get("id"))
            for msg in messages[: message_index + 1]
            if isinstance(msg, dict) and msg.get("id")
        }
        exact_entries = [
            entry
            for entry in timeline
            if isinstance(entry, dict) and str(entry.get("message_id") or "") in message_ids
        ]
        if exact_entries:
            return exact_entries, deepcopy(exact_entries[-1])

        fork_message = messages[message_index] if 0 <= message_index < len(messages) else {}
        fork_time = _parse_iso_datetime(_message_timestamp(fork_message))
        if fork_time:
            by_time = []
            for entry in timeline:
                entry_time = _parse_iso_datetime(entry.get("timestamp") if isinstance(entry, dict) else None)
                if not entry_time:
                    continue
                if entry_time <= fork_time:
                    by_time.append(entry)
            if by_time:
                return by_time, deepcopy(by_time[-1])

        assistant_count = sum(
            1
            for msg in messages[: message_index + 1]
            if isinstance(msg, dict) and msg.get("role") == "assistant"
        )
        if assistant_count > 0:
            selected = timeline[: min(assistant_count, len(timeline))]
            if selected:
                return selected, deepcopy(selected[-1])

        return [], None

    def _ensure_mutable_session(session_id, session):
        if not session_store.get_session(session_id):
            session_store.set_session(session_id, session)
        return session

    @app.route("/api/sessions")
    def get_sessions():
        # 导入公开会话管理模块
        from nbot.web.routes.public_sessions import _public_sessions, _generate_public_id
        
        current_time = server.time.time() if hasattr(server, "time") else None
        if current_time is None:
            import time

            current_time = time.time()
        if not hasattr(server, "_sessions_cache"):
            server._sessions_cache = []
            server._sessions_cache_time = 0

        sessions_data = dict(server.sessions)

        sessions = []
        for sid, session in sessions_data.items():
            if not is_web_visible_session(sid, session):
                continue
            timeline, timeline_changed = _ensure_runtime_timeline_from_snapshot(session)
            if timeline_changed:
                session_store.set_session(sid, session)
            archived = bool(session.get("archived"))
            sender_portrait, sender_avatar, visuals_changed = _repair_session_visuals(
                server, session, sessions_data
            )
            if visuals_changed:
                session_store.set_session(sid, session)
            favorite_collections = _normalize_message_favorite_collections(
                session.get("message_favorites", [])
            )
            if favorite_collections != session.get("message_favorites", []):
                session["message_favorites"] = favorite_collections
                session_store.set_session(sid, session)
            # 检查会话是否已公开
            public_id = _generate_public_id(sid)
            is_public = public_id in _public_sessions
            
            sessions.append(
                {
                    "id": sid,
                    "name": session.get("name", f"会话 {sid[:8]}"),
                    "type": session.get("type", "web"),
                    "user_id": session.get("user_id"),
                    "qq_id": session.get("qq_id"),
                    "channel_id": session.get("channel_id"),
                    "created_at": session.get("created_at"),
                    "archived": archived,
                    "archived_at": session.get("archived_at") if archived else None,
                    "is_archive": bool(session.get("is_archive")),
                    "read_only": bool(session.get("read_only")),
                    "archive_session_id": session.get("archive_session_id"),
                    "source_session_id": session.get("source_session_id"),
                    "message_count": len(visible_messages(session.get("messages", []))),
                    "system_prompt": session.get("system_prompt", ""),
                    "character_id": session.get("character_id", ""),
                    "sender_name": session.get("sender_name", ""),
                    "sender_avatar": sender_avatar,
                    "sender_portrait": sender_portrait,
                    "scenario": session.get("scenario", ""),
                    "tags": _normalize_tags(session.get("tags", [])),
                    "favorite": bool(session.get("favorite")),
                    "message_favorites_count": len(favorite_collections),
                    "pinned": bool(session.get("pinned")),
                    "is_public": is_public,
                    "proactive_chat": _normalize_proactive_chat_config(session.get("proactive_chat")),
                    "tts_config": _normalize_tts_config(session.get("tts_config")),
                    "session_mode": session.get("session_mode", "character"),
                    "agent_inherit_character": bool(
                        session.get("agent_inherit_character")
                    ),
                    "group_id": session.get("group_id", ""),
                    "character_ids": session.get("character_ids", []),
                    "character_runtime_snapshot": session.get("character_runtime_snapshot"),
                    "character_runtime_timeline": timeline,
                    "custom_prompts": session.get("custom_prompts", []),
                    "disabled_prompt_keys": session.get("disabled_prompt_keys", []),
                    "prompt_stack_debug": session.get("prompt_stack_debug", []),
                    "plot_mode": bool(session.get("plot_mode")),
                    "plot_real_time_sync": bool(session.get("plot_real_time_sync")),
                    "plot_choice_style": str(session.get("plot_choice_style") or ""),
                    "plot_outline": str(session.get("plot_outline") or ""),
                }
            )

        server._sessions_cache = sessions
        server._sessions_cache_time = current_time
        return jsonify(sessions)


    @app.route("/api/sessions", methods=["POST"])
    def create_session():
        data = request.json
        session_id = str(uuid.uuid4())
        session_mode = data.get("session_mode", "character")

        if session_mode == "group":
            # 群聊模式：创建 GroupConversation 并关联到会话
            system_prompt = data.get("system_prompt", "")
            user_id = data.get("user_id", "")
            char_name = ""
            sender_name = data.get("sender_name") or "群聊"
            sender_avatar = data.get("sender_avatar") or ""
            sender_portrait = data.get("sender_portrait") or ""
            character_id = data.get("character_id") or ""

            # 创建或关联群聊会话
            group_id = data.get("group_id", "")
            try:
                from nbot.group.manager import get_group_manager
                from nbot.group.models import GroupConfig
                gm = get_group_manager()
                character_ids = data.get("character_ids", [])
                narrator_id = data.get("narrator_id")

                if group_id:
                    # 使用已有的 GroupConversation
                    existing = gm.get_group(group_id)
                    if not existing:
                        _log.warning("group_id %s not found, creating new group", group_id)
                        group_id = ""
                    else:
                        character_ids = existing.character_ids
                if not group_id:
                    config_data = data.get("group_config")
                    config = GroupConfig.from_dict(config_data) if config_data else None
                    group = gm.create_group(
                        data.get("name", "群聊"),
                        character_ids,
                        narrator_id=narrator_id,
                        config=config,
                    )
                    group_id = group.group_id
            except Exception as exc:
                _log.warning("Failed to create group conversation: %s", exc)
                group_id = ""

            # 构建群聊系统提示词（列出参与角色）
            if character_ids and not system_prompt:
                names = ", ".join(character_ids)
                system_prompt = (
                    f"你正在参与一场群聊对话，参与者包括：{names}。"
                    f"请根据对话内容自然地参与讨论。"
                )
        elif session_mode == "agent":
            # Agent 模式：不继承任何角色卡配置
            system_prompt = data.get("system_prompt", "")
            user_id = data.get("user_id", "")
            char_name = ""
            sender_name = data.get("sender_name") or "Agent"
            sender_avatar = data.get("sender_avatar") or ""
            sender_portrait = data.get("sender_portrait") or ""
            character_id = data.get("character_id") or ""
        else:
            # 角色模式：使用人格提示词作为默认系统提示词
            system_prompt = data.get(
                "system_prompt", server.personality.get("systemPrompt", "")
            )

            # 替换模板变量 {{user}} -> 当前用户名, {{char}} -> 角色名称
            user_id = data.get("user_id", "")
            char_name = data.get("sender_name") or server.personality.get("name", "")
            if user_id:
                system_prompt = system_prompt.replace('{{user}}', user_id)
            if char_name:
                system_prompt = system_prompt.replace('{{char}}', char_name)

            # 获取角色信息
            sender_name = data.get("sender_name") or server.personality.get("name", "AI")
            character_id = data.get("character_id") or sender_name

            if any(key in data for key in ("state", "initial_state", "initialState", "relationship", "initialRelationship")):
                try:
                    from nbot.character.models import CharacterProfile
                    from nbot.character.repository import ProfileRepository

                    profile_data = dict(getattr(server, "personality", {}) or {})
                    profile_data.update(data)
                    profile = CharacterProfile.from_personality_dict(profile_data)
                    profile.id = str(character_id)
                    if not profile.name:
                        profile.name = sender_name
                    ProfileRepository(_get_base_dir(server)).save(profile)
                except Exception as exc:
                    _log.warning(
                        "[CharacterRuntime] failed to sync session character profile %s: %s",
                        character_id,
                        exc,
                        exc_info=True,
                    )

            # 获取角色其他信息
            sender_avatar = data.get("sender_avatar") or server.personality.get("avatar", "")
            sender_portrait = data.get("sender_portrait") or server.personality.get("portrait", "")

        # 记忆由 ai_pipeline.py 中的 PromptStack 动态注入，不在此处重复添加

        # 添加 Skills 到系统提示词（两种模式都添加）
        if _skills_prompt_injection_enabled(server.settings):
            enabled_skills = [s for s in server.skills_config if s.get("enabled", True)]
            system_prompt += format_skills_prompt(server.skills_config)
            _log.info(f"已添加 {len(enabled_skills)} 个技能到会话 {session_id[:8]}")
        else:
            _log.info(f"Skills prompt injection disabled for session {session_id[:8]}")

        session = {
            "id": session_id,
            "name": data.get("name", f"新会话 {session_id[:8]}"),
            "type": data.get("type", "web"),
            "user_id": data.get("user_id"),
            "created_at": datetime.now().isoformat(),
            "archived": False,
            "archived_at": None,
            "messages": [{"role": "system", "content": system_prompt}],
            "system_prompt": system_prompt,
            "character_id": character_id,
            "sender_name": sender_name,
            "sender_avatar": sender_avatar,
            "sender_portrait": sender_portrait,
            "tags": _normalize_tags(data.get("tags", [])),
            "favorite": bool(data.get("favorite")),
            "message_favorites": [],
            "pinned": bool(data.get("pinned")),
            "is_public": bool(data.get("is_public")),
            "proactive_chat": _normalize_proactive_chat_config(data.get("proactive_chat")),
            "tts_config": _normalize_tts_config(data.get("tts_config")),
            "character_runtime_timeline": [],
            "session_mode": data.get("session_mode", "character"),
            # agent 会话是否继承角色能力（角色卡/记忆/世界书/关系状态），默认关闭
            "agent_inherit_character": bool(data.get("agent_inherit_character")),
            "plot_mode": bool(data.get("plot_mode")),
            "plot_real_time_sync": bool(data.get("plot_real_time_sync")),
            "plot_choice_style": str(data.get("plot_choice_style") or ""),
            "plot_outline": str(data.get("plot_outline") or ""),
        }
        # Author's Note（会话级附注，按深度插入消息序列）
        from nbot.core.authors_note import normalize_authors_note

        authors_note = normalize_authors_note(data.get("authors_note"))
        if authors_note:
            session["authors_note"] = authors_note

        from nbot.web.session_model import normalize_model_override

        model_override = normalize_model_override(data.get("model_override"))
        if model_override:
            session["model_override"] = model_override

        persona_id = str(data.get("persona_id") or "").strip()[:64]
        if persona_id:
            session["persona_id"] = persona_id

        # 群聊模式：附加群聊关联字段
        if session_mode == "group" and group_id:
            session["group_id"] = group_id
            session["character_ids"] = data.get("character_ids", [])

        # 如果有开场白，添加为第一条 assistant 消息（agent / group 模式跳过）
        if session_mode in ("agent", "group"):
            first_message = data.get("first_message", "")
        else:
            first_message = data.get("first_message") or server.personality.get("firstMessage", "")
        if first_message:
            first_message = _expand_session_macros(first_message, user_id, char_name)
            session["messages"].append({
                "id": str(uuid.uuid4()),
                "role": "assistant",
                "content": first_message,
                "sender": sender_name,
                "timestamp": datetime.now().isoformat(),
            })
        
        # 获取背景设定，存储在会话中用于前端展示
        # group 模式不使用当前角色的 scenario
        if session_mode == "group":
            scenario = data.get("scenario", "")
        else:
            scenario = data.get("scenario") or server.personality.get("scenario", "")
        if scenario:
            # 替换模板变量 {{user}} / {{char}} / {{time}} 等
            scenario = _expand_session_macros(scenario, user_id, char_name)
            session["scenario"] = scenario
        if not is_web_visible_session(session_id, session):
            return jsonify({"error": "Invalid session type"}), 400
    
        session_store.set_session(session_id, session)

        # 记录会话创建操作到 Gateway 日志
        try:
            server.record_operation(
                module="session",
                action="create",
                description=f"创建会话 → {session.get('name', '未命名')}",
                detail=f"会话ID={session_id[:8]}, 类型={session.get('type', 'web')}",
                metadata={"session_id": session_id, "session_name": session.get("name", ""), "type": session.get("type", "web")},
            )
        except Exception:
            pass

        # 创建对应的工作区
        if server.WORKSPACE_AVAILABLE:
            server.workspace_manager.get_or_create(
                session_id, session.get("type", "web"), session.get("name", "")
            )
    
        return jsonify({"id": session_id, "session": session})
    @app.route("/api/sessions/<session_id>")
    def get_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        payload = dict(session)
        visible = visible_messages(session.get("messages", []))
        payload["messages"] = visible
        payload["message_count"] = len(visible)
        if request.args.get("include_messages", "true").lower() in {"false", "0"}:
            payload.pop("messages", None)
        session["proactive_chat"] = _normalize_proactive_chat_config(
            session.get("proactive_chat")
        )
        payload["proactive_chat"] = session["proactive_chat"]
        session["tts_config"] = _normalize_tts_config(session.get("tts_config"))
        payload["tts_config"] = session["tts_config"]
        if not isinstance(session.get("message_favorites"), list):
            session["message_favorites"] = []
        else:
            session["message_favorites"] = _normalize_message_favorite_collections(
                session.get("message_favorites", [])
            )
        payload["message_favorites"] = session["message_favorites"]
        return jsonify(payload)

    @app.route("/api/sessions/<session_id>/debug")
    def debug_session(session_id):
        """调试 API：查看会话详细信息"""
        raw_session = session_store.get_session(session_id)
        db_session = get_session_from_db(server.data_dir, session_id)
        
        result = {
            "session_id": session_id,
            "in_memory": raw_session is not None,
            "in_db": db_session is not None,
            "is_visible": False,
        }
        
        if raw_session:
            result["memory_session"] = {
                "type": raw_session.get("type"),
                "channel_id": raw_session.get("channel_id"),
                "message_count": len(raw_session.get("messages", [])),
                "first_message_source": raw_session.get("messages", [{}])[0].get("source") if raw_session.get("messages") else None,
            }
            result["is_visible"] = is_web_visible_session(session_id, raw_session)
        
        if db_session:
            result["db_session"] = {
                "type": db_session.get("type"),
                "channel_id": db_session.get("channel_id"),
            }
        
        return jsonify(result)

    @app.route("/api/sessions/<session_id>", methods=["PUT"])
    def update_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json
        old_name = session.get("name", "")
        if "name" in data:
            session["name"] = data.get("name", session["name"])
            if session.get("name") != old_name:
                session["_auto_name_generated"] = False
                session["_last_rename_count"] = len(
                    [
                        msg
                        for msg in session.get("messages", [])
                        if msg.get("role") in ("user", "assistant")
                    ]
                )
        if "tags" in data:
            session["tags"] = _normalize_tags(data.get("tags"))
        if "favorite" in data:
            session["favorite"] = bool(data.get("favorite"))
        if "pinned" in data:
            session["pinned"] = bool(data.get("pinned"))
        if "proactive_chat" in data:
            session["proactive_chat"] = _normalize_proactive_chat_config(
                data.get("proactive_chat")
            )
            # 桥接到 SessionHeartbeatManager：会话级 heartbeat
            _sync_proactive_chat_to_heartbeat(server, session_id, session["proactive_chat"])
        if "disabled_prompt_keys" in data:
            session["disabled_prompt_keys"] = data.get("disabled_prompt_keys") or []
        if "plot_mode" in data:
            session["plot_mode"] = bool(data.get("plot_mode"))
        if "plot_real_time_sync" in data:
            session["plot_real_time_sync"] = bool(data.get("plot_real_time_sync"))
        if "plot_choice_style" in data:
            session["plot_choice_style"] = str(data.get("plot_choice_style") or "")
        if "plot_outline" in data:
            session["plot_outline"] = str(data.get("plot_outline") or "")
        if "auto_state_interval" in data:
            # 会话级 auto_state 触发轮次；null 表示使用全局默认，0 表示禁用，正整数表示每 N 轮触发
            raw_val = data.get("auto_state_interval")
            if raw_val is None:
                session["auto_state_interval"] = None
            else:
                try:
                    val = int(raw_val)
                    session["auto_state_interval"] = max(0, min(50, val))
                except (TypeError, ValueError):
                    session["auto_state_interval"] = None
        if "enabled_tools" in data:
            # 会话级工具集：工具名列表，空列表/null 表示使用全局配置
            names = data.get("enabled_tools")
            if isinstance(names, list):
                session["enabled_tools"] = [str(n).strip() for n in names if str(n).strip()][:100]
            else:
                session["enabled_tools"] = None
        if "agent_goal" in data:
            session["agent_goal"] = str(data.get("agent_goal") or "").strip()[:2000]
        if "agent_spec" in data:
            session["agent_spec"] = str(data.get("agent_spec") or "").strip()[:8000]
        if "auto_name_interval" in data:
            # 会话级自动命名间隔（消息条数），null=全局默认
            raw_val = data.get("auto_name_interval")
            if raw_val is None:
                session["auto_name_interval"] = None
            else:
                try:
                    session["auto_name_interval"] = max(0, min(100, int(raw_val)))
                except (TypeError, ValueError):
                    session["auto_name_interval"] = None
        if "tts_config" in data:
            session["tts_config"] = _normalize_tts_config(data.get("tts_config"))
        if "authors_note" in data:
            from nbot.core.authors_note import normalize_authors_note

            note = normalize_authors_note(data.get("authors_note"))
            if note:
                session["authors_note"] = note
            else:
                session.pop("authors_note", None)
        if "model_override" in data:
            from nbot.web.session_model import normalize_model_override

            override = normalize_model_override(data.get("model_override"))
            if override:
                session["model_override"] = override
            else:
                session.pop("model_override", None)
        if "persona_id" in data:
            # 用户人设：空值表示解绑（运行时回退全局默认人设）
            persona_id = str(data.get("persona_id") or "").strip()[:64]
            if persona_id:
                session["persona_id"] = persona_id
            else:
                session.pop("persona_id", None)
        if "agent_inherit_character" in data:
            # agent 会话继承角色能力（P2-8）：只在 agent 会话上有意义，默认关闭
            session["agent_inherit_character"] = bool(data.get("agent_inherit_character"))

        # 群聊模式：更新参与角色列表
        if "character_ids" in data and session.get("session_mode") == "group":
            new_ids = data["character_ids"]
            if isinstance(new_ids, list):
                session["character_ids"] = new_ids
                # 同步到 GroupManager
                group_id = session.get("group_id")
                if group_id:
                    try:
                        from nbot.group.manager import get_group_manager
                        gm = get_group_manager()
                        group = gm.get_group(group_id)
                        if group:
                            group.character_ids = list(new_ids)
                            gm._save()
                    except Exception as exc:
                        _log.warning("Failed to sync group character_ids: %s", exc)

        new_prompt = data.get("system_prompt", session.get("system_prompt", ""))
        if new_prompt != session.get("system_prompt", ""):
            session["system_prompt"] = new_prompt
            if session["messages"] and session["messages"][0].get("role") == "system":
                session["messages"][0]["content"] = new_prompt
            else:
                session["messages"].insert(0, {"role": "system", "content": new_prompt})

        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    # ---- 自定义提示词 CRUD ----

    @app.route("/api/sessions/<session_id>/macro-variables", methods=["GET"])
    def get_macro_variables(session_id):
        """读取会话的宏变量（{{setvar}}/{{getvar}} 持久化的键值）。"""
        if not _get_web_session(session_id):
            return jsonify({"error": "Session not found"}), 404
        from nbot.core.macros import get_variable_store

        try:
            variables = get_variable_store(server.base_dir).get(session_id)
        except Exception as exc:
            _log.warning("读取宏变量失败: %s", exc)
            variables = {}
        return jsonify({"success": True, "variables": variables})

    @app.route("/api/sessions/<session_id>/macro-variables", methods=["DELETE"])
    def clear_macro_variables(session_id):
        """清空会话的宏变量。"""
        if not _get_web_session(session_id):
            return jsonify({"error": "Session not found"}), 404
        from nbot.core.macros import get_variable_store

        try:
            get_variable_store(server.base_dir).clear(session_id)
        except Exception as exc:
            _log.warning("清空宏变量失败: %s", exc)
            return jsonify({"success": False, "error": str(exc)}), 500
        return jsonify({"success": True})

    @app.route("/api/sessions/<session_id>/custom-prompts", methods=["GET"])
    def get_custom_prompts(session_id):
        """获取会话的自定义提示词列表"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        return jsonify({"success": True, "custom_prompts": session.get("custom_prompts", [])})

    @app.route("/api/sessions/<session_id>/custom-prompts", methods=["PUT"])
    def update_custom_prompts(session_id):
        """更新会话的自定义提示词列表（全量替换）"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json or {}
        custom_prompts = data.get("custom_prompts", [])
        if not isinstance(custom_prompts, list):
            return jsonify({"error": "custom_prompts must be a list"}), 400

        # 校验并清洗每条自定义提示词
        cleaned = []
        for idx, item in enumerate(custom_prompts):
            if not isinstance(item, dict):
                continue
            content = str(item.get("content", "")).strip()
            if not content:
                continue
            title = str(item.get("title", "")).strip()
            cleaned.append({
                "order": int(item.get("order", idx + 1)),
                "title": title,
                "content": content,
            })

        # 按 order 排序
        cleaned.sort(key=lambda x: x["order"])

        session["custom_prompts"] = cleaned
        session_store.set_session(session_id, session)
        if hasattr(server, "_invalidate_sessions_cache"):
            server._invalidate_sessions_cache()
        return jsonify({"success": True, "custom_prompts": cleaned})

    @app.route("/api/sessions/<session_id>/runtime-timeline", methods=["GET"])
    def get_runtime_timeline(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        timeline, changed = _ensure_runtime_timeline_from_snapshot(session)
        if changed:
            session_store.set_session(session_id, session)

        # 附加对话内容到timeline节点
        messages = session.get("messages", [])
        for entry in timeline:
            if not isinstance(entry, dict):
                continue
            msg_index = entry.get("message_index")
            msg_id = entry.get("message_id")
            user_msg = None
            assistant_msg = None

            # 根据message_index查找对应的用户和AI消息
            if isinstance(msg_index, int) and msg_index >= 0 and msg_index < len(messages):
                # 查找当前assistant消息及之前的user消息
                for i in range(msg_index, -1, -1):
                    msg = messages[i]
                    if isinstance(msg, dict):
                        if msg.get("role") == "assistant" and not assistant_msg:
                            assistant_msg = msg
                        elif msg.get("role") == "user" and not user_msg:
                            user_msg = msg
                        if assistant_msg and user_msg:
                            break

            # 如果通过index没找到，尝试用message_id
            if (not assistant_msg or not user_msg) and msg_id:
                msg_id_str = str(msg_id)
                found_assistant = False
                found_user = False
                for i in range(len(messages) - 1, -1, -1):
                    msg = messages[i]
                    if isinstance(msg, dict) and str(msg.get("id")) == msg_id_str:
                        if msg.get("role") == "assistant":
                            assistant_msg = msg
                            found_assistant = True
                        for j in range(i - 1, -1, -1):
                            prev_msg = messages[j]
                            if isinstance(prev_msg, dict) and prev_msg.get("role") == "user":
                                user_msg = prev_msg
                                found_user = True
                                break
                    if found_assistant and found_user:
                        break

            # 策略3：时间戳邻近匹配（处理旧数据、snapshot fallback、索引失效等情况）
            if (not assistant_msg or not user_msg) and entry.get("timestamp") and len(messages) >= 2:
                try:
                    from datetime import datetime as dt
                    entry_time = dt.fromisoformat(entry["timestamp"].replace("Z", "+00:00"))
                    if entry_time.tzinfo:
                        entry_time = entry_time.astimezone().replace(tzinfo=None)

                    best_pair = None
                    min_time_diff = None

                    for i in range(len(messages) - 1, 0, -1):
                        curr_msg = messages[i]
                        prev_msg = messages[i - 1]
                        if not isinstance(curr_msg, dict) or not isinstance(prev_msg, dict):
                            continue

                        is_valid_pair = (
                            (curr_msg.get("role") == "assistant" and prev_msg.get("role") == "user") or
                            (curr_msg.get("role") == "user" and prev_msg.get("role") == "assistant")
                        )
                        if not is_valid_pair:
                            continue

                        curr_time_str = _message_timestamp(curr_msg)
                        prev_time_str = _message_timestamp(prev_msg)
                        if not curr_time_str or not prev_time_str:
                            continue

                        curr_time = _parse_iso_datetime(curr_time_str)
                        prev_time = _parse_iso_datetime(prev_time_str)
                        pair_time = max(curr_time, prev_time) if curr_time and prev_time else None
                        if not pair_time:
                            continue

                        time_diff = abs((entry_time - pair_time).total_seconds())
                        if min_time_diff is None or time_diff < min_time_diff:
                            min_time_diff = time_diff
                            if curr_msg.get("role") == "assistant":
                                best_pair = (prev_msg, curr_msg)
                            else:
                                best_pair = (curr_msg, prev_msg)

                    if best_pair and min_time_diff is not None and min_time_diff <= 300:
                        if not user_msg:
                            user_msg = best_pair[0]
                        if not assistant_msg:
                            assistant_msg = best_pair[1]

                except Exception:
                    pass

            # 提取文本内容（限制长度避免数据过大）
            def extract_text(msg):
                if not msg or not isinstance(msg, dict):
                    return ""
                content = msg.get("content", "")
                if isinstance(content, list):
                    texts = []
                    for part in content:
                        if isinstance(part, dict) and part.get("type") == "text":
                            texts.append(part.get("text", ""))
                        elif isinstance(part, str):
                            texts.append(part)
                    content = "\n".join(texts)
                return str(content)[:500] if content else ""

            entry["user_message"] = extract_text(user_msg)
            entry["assistant_message"] = extract_text(assistant_msg)

        return jsonify({"success": True, "timeline": timeline})

    @app.route("/api/sessions/<session_id>/runtime-timeline", methods=["POST"])
    def add_runtime_timeline(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json or {}
        snapshot = data.get("snapshot") if isinstance(data, dict) else None
        if not isinstance(snapshot, dict):
            return jsonify({"error": "Invalid runtime snapshot"}), 400

        entry = _normalize_runtime_timeline_entry(snapshot)
        timeline = session.get("character_runtime_timeline", [])
        if not isinstance(timeline, list):
            timeline = []

        last = timeline[-1] if timeline else None
        if isinstance(last, dict):
            last_signature = _runtime_timeline_state_signature(last)
            entry_signature = _runtime_timeline_state_signature(entry)
            if last_signature == entry_signature:
                last.update(entry)
            else:
                timeline.append(entry)
        else:
            timeline.append(entry)

        session["character_runtime_timeline"] = timeline[-200:]
        session_store.set_session(session_id, session)
        return jsonify({"success": True, "timeline": session["character_runtime_timeline"]})

    @app.route("/api/sessions/<session_id>/archive", methods=["POST"])
    def archive_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        existing_archive_id = str(session.get("archive_session_id") or "").strip()
        if existing_archive_id:
            existing_archive = session_store.get_session(existing_archive_id)
            if not existing_archive:
                existing_archive = get_session_from_db(server.data_dir, existing_archive_id)
            if existing_archive and _merge_bound_archive_into_session_messages(session, existing_archive):
                session.pop("archive_session_id", None)
                session["archived"] = True
                session["archived_at"] = datetime.now().isoformat()
                session_store.set_session(session_id, session)
                session_store.delete_session(existing_archive_id)
                return jsonify({
                    "success": True,
                    "session": session,
                    "archive_session_id": None,
                })

        # 普通归档：只标记归档状态，不创建归档会话（归档会话仅由压缩创建）
        session["archived"] = True
        session["archived_at"] = datetime.now().isoformat()
        session_store.set_session(session_id, session)

        return jsonify({
            "success": True,
            "session": session,
            "archive_session_id": session.get("archive_session_id"),
        })

    @app.route("/api/sessions/<session_id>/restore", methods=["POST"])
    def restore_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        session["archived"] = False
        session["archived_at"] = None
        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    @app.route("/api/sessions/<session_id>", methods=["DELETE"])
    def delete_session(session_id):
        if not _get_web_session(session_id):
            _log.warning(f"[DeleteSession] 会话不存在: {session_id[:8]}")
            return jsonify({"error": "Session not found"}), 404
        if session_store.delete_session(session_id):
            _log.info(f"[DeleteSession] 已删除: {session_id[:8]}")
            if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                server.workspace_manager.delete_workspace(session_id)
            server.log_message("info", f"删除了会话 {session_id[:8]}...", important=True)

            # 记录会话删除操作到 Gateway 日志
            try:
                server.record_operation(
                    module="session",
                    action="delete",
                    description=f"删除会话 → {session_id[:8]}",
                    detail=f"已删除会话 ID: {session_id}",
                    metadata={"session_id": session_id},
                )
            except Exception:
                pass

            return jsonify({"success": True})
        _log.warning(f"[DeleteSession] 删除会话失败: {session_id}")
        return jsonify({"error": "Session not found"}), 404

    def _export_session_payload(sessions):
        return {
            "version": 1,
            "type": "nbot_session_export",
            "exported_at": datetime.now().isoformat(),
            "total": len(sessions),
            "sessions": sessions,
        }

    def _normalize_imported_session(raw_session):
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
        if session["type"] not in {"web", "cli"}:
            session["type"] = "web"
        messages = session.get("messages")
        if not isinstance(messages, list):
            messages = []
        session["messages"] = [m for m in messages if isinstance(m, dict)]
        system_prompt = session.get("system_prompt")
        if not system_prompt:
            system_msg = next((m for m in session["messages"] if m.get("role") == "system"), None)
            system_prompt = (system_msg or {}).get("content", "")
        session["system_prompt"] = system_prompt or ""
        if not session.get("character_id") and session.get("sender_name"):
            session["character_id"] = session["sender_name"]
        if session["system_prompt"] and not any(m.get("role") == "system" for m in session["messages"]):
            session["messages"].insert(0, {"role": "system", "content": session["system_prompt"]})
        session["created_at"] = session.get("created_at") or now
        session["archived"] = bool(session.get("archived"))
        session["archived_at"] = session.get("archived_at") if session["archived"] else None
        if not is_web_visible_session(new_id, session):
            raise ValueError("invalid session type")
        return new_id, session

    def _merge_session_with_archive(session):
        """将归档会话的消息拼到主会话前面，导出完整对话历史。"""
        archive_id = session.get("archive_session_id")
        if not archive_id:
            return session

        archive = session_store.get_session(archive_id)
        if not archive:
            archive = get_session_from_db(server.data_dir, archive_id)
        if not archive:
            return session

        archive_messages = archive.get("messages", [])
        if not archive_messages:
            return session

        # 只取归档中的实际对话消息，排除 system prompt / summary / divider 标记
        meaningful = [
            m for m in archive_messages
            if m.get("role") != "system"
            or m.get("id", "").startswith("archive_divider_")
        ]

        if not meaningful:
            return session

        main_messages = list(session.get("messages", []))
        # 去掉主会话的 system prompt（如果有），用归档或主会话的保留一个即可
        main_system_idx = -1
        archive_system_idx = -1
        for i, m in enumerate(meaningful):
            if m.get("role") == "system":
                archive_system_idx = i
                break
        for i, m in enumerate(main_messages):
            if m.get("role") == "system":
                main_system_idx = i
                break

        # 归档中的 system 消息只保留 divider，去掉纯 system prompt
        if archive_system_idx >= 0:
            msg = meaningful[archive_system_idx]
            if not str(msg.get("id", "")).startswith("archive_divider_"):
                meaningful.pop(archive_system_idx)

        merged = dict(session)
        merged["messages"] = meaningful + main_messages
        return merged

    def _merge_bound_archive_into_session_messages(session, archive):
        archive_messages = archive.get("messages", []) if isinstance(archive, dict) else []
        if not archive_messages:
            return False

        meaningful_archive = [
            deepcopy(msg)
            for msg in archive_messages
            if isinstance(msg, dict)
            and (
                msg.get("role") != "system"
                or str(msg.get("id", "")).startswith("archive_divider_")
            )
        ]
        if not meaningful_archive:
            return False

        main_messages = [deepcopy(msg) for msg in session.get("messages", []) if isinstance(msg, dict)]
        if main_messages and main_messages[0].get("role") == "system":
            session["messages"] = [main_messages[0]] + meaningful_archive + main_messages[1:]
        else:
            session["messages"] = meaningful_archive + main_messages
        return True

    def _copy_archive_for_fork(source_session, target_session_id, target_name, now):
        source_archive_id = source_session.get("archive_session_id") if isinstance(source_session, dict) else None
        if not source_archive_id:
            return ""

        source_archive = session_store.get_session(source_archive_id)
        if not source_archive:
            source_archive = get_session_from_db(server.data_dir, source_archive_id)
        if not source_archive:
            _log.warning(
                "[ForkSession] source archive not found for fork: %s",
                source_archive_id,
            )
            return ""

        archive_id = str(uuid.uuid4())
        archive = deepcopy(source_archive)
        archive["id"] = archive_id
        archive["name"] = f"📦 {target_name} - 归档"
        archive["type"] = "web"
        archive["created_at"] = now
        archive["archived"] = True
        archive["archived_at"] = now
        archive["is_archive"] = True
        archive["read_only"] = True
        archive["source_session_id"] = target_session_id
        archive["forked_from_archive_session_id"] = source_archive_id
        archive["messages"] = deepcopy(source_archive.get("messages", []))

        _repair_session_visuals(
            server,
            archive,
            {
                str(source_archive_id): source_archive,
                target_session_id: source_session,
                archive_id: archive,
            },
        )
        session_store.set_session(archive_id, archive)
        return archive_id

    @app.route("/api/sessions/<session_id>/export")
    def export_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        session = _merge_session_with_archive(session)
        return jsonify(_export_session_payload([session]))

    @app.route("/api/sessions/export")
    def export_sessions():
        ids = [sid.strip() for sid in request.args.get("ids", "").split(",") if sid.strip()]
        sessions = []
        if ids:
            for sid in ids:
                session = _get_web_session(sid)
                if session:
                    sessions.append(_merge_session_with_archive(session))
        else:
            for sid, session in (server.sessions or {}).items():
                if is_web_visible_session(sid, session):
                    sessions.append(_merge_session_with_archive(session))
        return jsonify(_export_session_payload(sessions))

    @app.route("/api/sessions/import", methods=["POST"])
    def import_sessions():
        data = request.get_json(silent=True) or {}
        sessions_payload = data.get("sessions")
        if sessions_payload is None and isinstance(data.get("session"), dict):
            sessions_payload = [data["session"]]
        if sessions_payload is None and isinstance(data, dict) and data.get("type") != "nbot_session_export":
            sessions_payload = [data]
        if not isinstance(sessions_payload, list):
            return jsonify({"success": False, "error": "sessions must be a list"}), 400

        imported = []
        errors = []
        for idx, raw_session in enumerate(sessions_payload):
            try:
                session_id, session = _normalize_imported_session(raw_session)
                session_store.set_session(session_id, session)
                if server.WORKSPACE_AVAILABLE and server.workspace_manager:
                    server.workspace_manager.get_or_create(
                        session_id, session.get("type", "web"), session.get("name", "")
                    )
                imported.append({"id": session_id, "name": session.get("name")})
            except Exception as exc:
                errors.append({"index": idx, "error": str(exc)})

        return jsonify({
            "success": True,
            "imported": len(imported),
            "failed": len(errors),
            "sessions": imported,
            "errors": errors,
        })

    @app.route("/api/sessions/<session_id>/fork", methods=["POST"])
    def fork_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        message_id = data.get("message_id")
        messages = session.get("messages", [])
        message_index = _find_message_index(messages, message_id)
        if message_index < 0:
            return jsonify({"success": False, "error": "Message not found"}), 404

        now = datetime.now().isoformat()
        new_id = str(uuid.uuid4())
        base_name = session.get("name") or f"Session {session_id[:8]}"
        forked_messages = deepcopy(messages[: message_index + 1])
        fork_timeline, fork_snapshot = _select_runtime_timeline_for_fork(
            session,
            messages,
            message_index,
        )
        system_prompt = session.get("system_prompt", "")
        if not system_prompt:
            system_msg = next((m for m in forked_messages if m.get("role") == "system"), None)
            system_prompt = (system_msg or {}).get("content", "")
        if system_prompt and not any(m.get("role") == "system" for m in forked_messages):
            forked_messages.insert(0, {"role": "system", "content": system_prompt})

        new_session = {
            "id": new_id,
            "name": f"{base_name} - Fork",
            "type": session.get("type") if session.get("type") in {"web", "cli"} else "web",
            "user_id": session.get("user_id"),
            "qq_id": session.get("qq_id"),
            "created_at": now,
            "archived": False,
            "archived_at": None,
            "messages": forked_messages,
            "system_prompt": system_prompt,
            "character_id": _resolve_session_runtime_character_id(
                {
                    **session,
                    "character_runtime_snapshot": fork_snapshot,
                    "character_runtime_timeline": fork_timeline,
                }
            ),
            "sender_name": session.get("sender_name", ""),
            "sender_avatar": session.get("sender_avatar", ""),
            "sender_portrait": session.get("sender_portrait", ""),
            "scenario": session.get("scenario", ""),
            "character_runtime_snapshot": fork_snapshot,
            "character_runtime_timeline": deepcopy(fork_timeline),
            "forked_from": {
                "session_id": session_id,
                "message_id": message_id,
                "message_index": message_index,
                "created_at": now,
            },
        }
        archive_id = _copy_archive_for_fork(
            session,
            new_id,
            new_session["name"],
            now,
        )
        if archive_id:
            new_session["archive_session_id"] = archive_id
        session_store.set_session(new_id, new_session)
        _copy_character_runtime_state(
            server,
            new_session.get("character_id"),
            session_id,
            new_id,
            snapshot=fork_snapshot,
        )
        if server.WORKSPACE_AVAILABLE and server.workspace_manager:
            server.workspace_manager.get_or_create(
                new_id, new_session.get("type", "web"), new_session.get("name", "")
            )
        return jsonify({"success": True, "id": new_id, "session": new_session})

    @app.route("/api/sessions/<session_id>/bind-character", methods=["PUT"])
    def bind_character_to_session(session_id):
        """为会话绑定角色属性"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json or {}
        old_name = session.get("sender_name", "")
        old_character_id = str(session.get("character_id") or old_name).strip()
        sender_name = (data.get("sender_name") or "").strip()
        if not sender_name:
            return jsonify({"error": "角色名称不能为空"}), 400

        session["sender_name"] = sender_name
        session["character_id"] = (data.get("character_id") or sender_name).strip()
        new_character_id = str(session.get("character_id") or sender_name).strip()
        if "sender_avatar" in data:
            session["sender_avatar"] = data.get("sender_avatar") or ""
        if "sender_portrait" in data:
            session["sender_portrait"] = data.get("sender_portrait") or ""
        if "scenario" in data:
            scenario = data.get("scenario") or ""
            user_id = session.get("user_id", "")
            if user_id:
                scenario = scenario.replace("{{user}}", user_id)
            if sender_name:
                scenario = scenario.replace("{{char}}", sender_name)
            session["scenario"] = scenario

        # 更新系统提示词中的角色信息
        system_prompt = session.get("system_prompt", "")
        if system_prompt and sender_name:
            if old_name and old_name != sender_name:
                system_prompt = system_prompt.replace(f'你是角色 "{old_name}"', f'你是角色 "{sender_name}"')
        if "system_prompt" in data:
            system_prompt = data.get("system_prompt") or ""
        session["system_prompt"] = system_prompt

        # 更新 system 消息
        messages = session.get("messages", [])
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = system_prompt

        same_character = _is_same_session_character(
            old_character_id,
            old_name,
            new_character_id,
            sender_name,
        )
        if same_character:
            _retarget_session_runtime_character(
                server,
                session_id,
                session,
                old_character_id,
                new_character_id,
            )
        else:
            session["character_runtime_timeline"] = []
            session["character_runtime_snapshot"] = None

        session_store.set_session(session_id, session)
        return jsonify({"success": True, "session": session})

    @app.route("/api/sessions/<session_id>/regenerate", methods=["POST"])
    def regenerate_message(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        message_id = data.get("message_id")
        messages = session.get("messages", [])
        message_index = _find_message_index(messages, message_id)
        if message_index < 0:
            return jsonify({"success": False, "error": "Message not found"}), 404

        target = messages[message_index]
        if is_message_deleted(target):
            return jsonify({"success": False, "error": "Message not found"}), 404
        if target.get("role") != "assistant":
            return jsonify({"success": False, "error": "Only assistant messages can be regenerated"}), 400

        previous_user = next(
            (
                msg
                for msg in reversed(messages[:message_index])
                if msg.get("role") == "user" and not is_message_deleted(msg)
            ),
            None,
        )
        if not previous_user or not previous_user.get("content"):
            return jsonify({"success": False, "error": "Previous user message not found"}), 400

        _ensure_mutable_session(session_id, session)
        # swipes：原消息保留，新回复作为追加候选合并回原消息（variants）。
        # 上下文裁剪由 WebCallbacks.load_messages 按 swipe_target_id 处理。
        previous_user_id = previous_user.get("id")
        previous_user.pop("thinking_cards", None)
        previous_user.pop("todo_cards", None)
        previous_user.pop("change_cards", None)
        ensure_message_variants(target)
        target.pop("thinking_cards", None)
        target.pop("todo_cards", None)
        target.pop("change_cards", None)
        session_store.set_session(session_id, session)
        trigger = getattr(server, "_trigger_ai_response", None)
        if not trigger:
            return jsonify({"success": False, "error": "AI trigger is unavailable"}), 500

        try:
            trigger(
                session_id,
                previous_user.get("content", ""),
                previous_user.get("sender", "web_user"),
                previous_user.get("attachments") or [],
                previous_user.get("id"),
                {"swipe_target_id": str(target.get("id") or "")},
            )
        except Exception as exc:
            _log.error("Failed to trigger regenerated response: %s", exc, exc_info=True)
            return jsonify({"success": False, "error": "Failed to trigger AI response"}), 500
        return jsonify({
            "success": True,
            "session_id": session_id,
            "swipe": True,
            "message_id": target.get("id"),
            "prompt_message_id": previous_user.get("id"),
        })

    @app.route("/api/sessions/<session_id>/messages/<message_id>/variant", methods=["POST"])
    def switch_message_variant(session_id, message_id):
        """切换消息当前选中的候选回复（swipes）。"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        index = data.get("index")
        if not isinstance(index, int):
            return jsonify({"success": False, "error": "index must be an integer"}), 400

        messages = session.get("messages", [])
        message_index = _find_message_index(messages, message_id)
        if message_index < 0 or is_message_deleted(messages[message_index]):
            return jsonify({"success": False, "error": "Message not found"}), 404

        _ensure_mutable_session(session_id, session)
        message = messages[message_index]
        if not select_message_variant(message, index):
            return jsonify({"success": False, "error": "Variant index out of range"}), 400
        session_store.set_session(session_id, session)
        server.socketio.emit(
            "message_variant_switched",
            {"session_id": session_id, "message": message},
            room=session_id,
        )
        return jsonify({"success": True, "message": message})

    @app.route("/api/stop", methods=["POST"])
    def stop_generation():
        data = request.json or {}
        session_id = data.get("session_id")
        adapter = getattr(server, "web_channel_adapter", None)
        capabilities = adapter.get_capabilities() if adapter else None

        if not session_id:
            return jsonify({"error": "session_id is required"}), 400

        if capabilities is not None and not capabilities.supports_stop:
            return jsonify({"success": False, "error": "Stop is not supported for this channel"}), 400

        if session_id in server.stop_events:
            server.stop_events[session_id].set()
            _log.info(f"[Stop] stop requested for session: {session_id}")
            return jsonify({"success": True, "message": "已请求停止生成"})

        return jsonify(
            {"success": False, "error": "No active generation for this session"}
        ), 404

    @app.route("/api/sessions/<session_id>/messages", methods=["GET"])
    def get_messages(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = visible_messages(session.get("messages", []))
        display_messages = [m for m in messages if m.get("role") != "system"]
        if "limit" in request.args:
            try:
                return jsonify(message_page(
                    display_messages,
                    request.args["limit"],
                    request.args.get("before_message_id"),
                ))
            except (ValueError, TypeError) as error:
                return jsonify({"error": str(error)}), 400
            except LookupError as error:
                return jsonify({"error": str(error)}), 404
        return jsonify(display_messages)

    @app.route("/api/sessions/<session_id>/context-stats", methods=["GET"])
    def get_context_stats(session_id):
        """上下文占比分类统计（7 类），统一会计口径供前端圆环/面板共用。"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        from nbot.web.context_stats import build_context_stats

        try:
            max_context_length = int(session.get("max_context_length") or 0) or int(
                getattr(server, "ai_config", {}).get("max_context_length", 100000) or 100000
            )
        except (TypeError, ValueError):
            max_context_length = 100000

        payload = build_context_stats(session, max_context_length=max_context_length)
        return jsonify({
            "success": True,
            "session_id": session_id,
            "categories": payload["categories"],
            "total": payload["total"],
            "max_context_length": payload["max_context_length"],
        })

    @app.route("/api/sessions/<session_id>/message-favorites", methods=["GET"])
    def get_message_favorites(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        favorites = _normalize_message_favorite_collections(
            session.get("message_favorites", [])
        )
        if favorites != session.get("message_favorites", []):
            session["message_favorites"] = favorites
            session_store.set_session(session_id, session)
        return jsonify({"success": True, "collections": favorites, "favorites": favorites})

    @app.route("/api/sessions/<session_id>/message-favorites", methods=["PUT"])
    def update_message_favorites(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        message_ids = data.get("message_ids", [])
        if not isinstance(message_ids, list):
            return jsonify({"error": "message_ids must be a list"}), 400
        title = str(data.get("title") or "").strip()[:80]
        collection_id = str(data.get("collection_id") or "").strip()

        seen = set()
        for message_id in message_ids:
            message_id = str(message_id or "").strip()
            if not message_id or message_id in seen:
                continue
            seen.add(message_id)
        if not seen:
            return jsonify({"error": "请选择至少一条对话"}), 400

        collections = _normalize_message_favorite_collections(
            session.get("message_favorites", [])
        )

        messages = session.get("messages", [])
        now = datetime.now().isoformat()
        snapshots = []
        for msg in messages:
            if not isinstance(msg, dict) or msg.get("role") == "system":
                continue
            message_id = str(msg.get("id") or "")
            if message_id not in seen:
                continue

            item = deepcopy(msg)
            item["message_id"] = message_id
            snapshots.append(item)

        if not snapshots:
            return jsonify({"error": "Message not found"}), 404
        if not title:
            title = f"收藏 {datetime.now().strftime('%Y-%m-%d %H:%M')} ({len(snapshots)}条)"

        if collection_id:
            target = next((item for item in collections if str(item.get("id")) == collection_id), None)
            if not target:
                return jsonify({"error": "Favorite collection not found"}), 404
            target["title"] = title
            target["updated_at"] = now
            target["messages"] = snapshots
        else:
            collections.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": title,
                    "created_at": now,
                    "messages": snapshots,
                }
            )

        session["message_favorites"] = collections
        session_store.set_session(session_id, session)
        return jsonify({"success": True, "collections": collections, "favorites": collections})

    @app.route(
        "/api/sessions/<session_id>/message-favorites/<collection_id>",
        methods=["DELETE"],
    )
    def delete_message_favorite(session_id, collection_id):
        """删除指定收藏夹。

        与 PUT /message-favorites 解耦：PUT 端点要求 message_ids 非空，
        无法用于纯删除场景。此端点直接按 collection_id 移除收藏夹。
        """
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        collection_id = str(collection_id or "").strip()
        if not collection_id:
            return jsonify({"error": "collection_id is required"}), 400

        collections = _normalize_message_favorite_collections(
            session.get("message_favorites", [])
        )
        target_idx = next(
            (
                i
                for i, item in enumerate(collections)
                if str(item.get("id")) == collection_id
            ),
            -1,
        )
        if target_idx < 0:
            return jsonify({"error": "Favorite collection not found"}), 404

        removed = collections.pop(target_idx)
        session["message_favorites"] = collections
        session_store.set_session(session_id, session)
        return jsonify(
            {
                "success": True,
                "deleted": True,
                "collection_id": collection_id,
                "title": removed.get("title", ""),
                "collections": collections,
                "favorites": collections,
            }
        )

    @app.route("/api/sessions/<session_id>/messages", methods=["POST"])
    def add_message(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        role = str(data.get("role", "user") or "user").strip()
        if role not in {"user", "assistant", "system"}:
            return jsonify({"error": "Invalid role"}), 400
        message = {
            "id": str(uuid.uuid4()),
            "role": role,
            "content": data.get("content", ""),
            "timestamp": datetime.now().isoformat(),
            "sender": data.get("sender", "web_user"),
            "session_id": session_id,
            "source": session.get("type", "web"),
        }

        if "insert_index" in data:
            messages = session.setdefault("messages", [])
            visible_count = len(
                [
                    msg
                    for msg in messages
                    if isinstance(msg, dict)
                    and msg.get("role") != "system"
                    and not is_message_deleted(msg)
                ]
            )
            try:
                insert_index = int(data.get("insert_index"))
            except (TypeError, ValueError):
                insert_index = visible_count
            insert_index = max(0, min(insert_index, visible_count))

            actual_index = len(messages)
            seen_visible = 0
            for idx, existing in enumerate(messages):
                if (
                    not isinstance(existing, dict)
                    or existing.get("role") == "system"
                    or is_message_deleted(existing)
                ):
                    continue
                if seen_visible == insert_index:
                    actual_index = idx
                    break
                seen_visible += 1

            messages.insert(actual_index, message)
            session_store.set_session(session_id, session)
        else:
            session_store.append_message(session_id, message)
        return jsonify(message)

    @app.route("/api/sessions/<session_id>/messages/<message_id>", methods=["PUT"])
    def update_message(session_id, message_id):
        """更新单条消息的内容，支持截断后续消息（用于用户编辑重发等场景）"""
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json
        messages = session.get("messages", [])

        target_idx = None
        for idx, msg in enumerate(messages):
            if str(msg.get("id", "")) == str(message_id):
                target_idx = idx
                break

        if target_idx is None or is_message_deleted(messages[target_idx]):
            return jsonify({"error": "Message not found"}), 404

        if "role" in data:
            role = str(data.get("role") or "").strip()
            if role not in {"user", "assistant", "system"}:
                return jsonify({"error": "Invalid role"}), 400
            messages[target_idx]["role"] = role

        if "content" in data:
            messages[target_idx]["content"] = data["content"]
        if "sender" in data:
            messages[target_idx]["sender"] = data.get("sender") or ""
        if "timestamp" in data:
            messages[target_idx]["timestamp"] = data.get("timestamp") or datetime.now().isoformat()
        if "audio_url" in data:
            audio_url = data.get("audio_url") or ""
            messages[target_idx]["audio_url"] = audio_url
            # 同步写入剧情节点快照，使分支切换/回溯后 TTS 不丢失
            if messages[target_idx].get("role") == "assistant":
                try:
                    from nbot.plot.graph_manager import get_plot_graph_manager

                    data_dir = getattr(server, "data_dir", "data/web")
                    get_plot_graph_manager(data_dir=data_dir).update_assistant_audio(
                        str(message_id), audio_url, conversation_id=session_id,
                    )
                except Exception:
                    _log.debug("[Sessions] plot snapshot audio sync skipped", exc_info=True)

        # 截断该消息之后的所有消息
        if data.get("truncate_after"):
            messages[:] = messages[: target_idx + 1]

        session_store.set_session(session_id, session)
        return jsonify({"success": True, "message": messages[target_idx]})

    @app.route("/api/sessions/<session_id>/messages/<message_id>", methods=["DELETE"])
    def delete_message(session_id, message_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = session.get("messages", [])
        target_idx = _find_message_index(messages, message_id)
        if target_idx < 0:
            return jsonify({"error": "Message not found"}), 404
        if messages[target_idx].get("role") == "system":
            return jsonify({"error": "System message cannot be deleted here"}), 400

        removed = messages[target_idx]
        removed["deleted"] = True
        removed["deleted_at"] = datetime.now().astimezone().isoformat()
        removed.pop("thinking_cards", None)
        removed.pop("todo_cards", None)
        removed.pop("change_cards", None)
        session_store.set_session(session_id, session)
        return jsonify(
            {
                "success": True,
                "message": removed,
                "message_count": len(visible_messages(messages)),
            }
        )

    @app.route("/api/sessions/<session_id>/messages", methods=["DELETE"])
    def clear_messages(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        system_msg = None
        if session["messages"] and session["messages"][0].get("role") == "system":
            system_msg = session["messages"][0]

        session_store.replace_messages(session_id, [system_msg] if system_msg else [])
        server.log_message("info", f"清空了会话 {session_id[:8]} 的消息", important=True)
        return jsonify({"success": True})

    def _get_or_create_archive_session(source_session_id, source_session):
        archive_id = source_session.get("archive_session_id")
        if archive_id:
            archive = session_store.get_session(archive_id)
            if archive:
                _, _, visuals_changed = _repair_session_visuals(
                    server,
                    archive,
                    {
                        source_session_id: source_session,
                        archive_id: archive,
                    },
                )
                if visuals_changed:
                    session_store.set_session(archive_id, archive)
                return archive
        now = datetime.now().isoformat()
        base_name = source_session.get("name", f"会话 {source_session_id[:8]}")
        archive_id = str(uuid.uuid4())
        sender_name = source_session.get("sender_name", "")
        archive = {
            "id": archive_id,
            "name": f"📦 {base_name} - 归档",
            "type": "web",
            "created_at": now,
            "archived": True,
            "archived_at": now,
            "is_archive": True,
            "read_only": True,
            "source_session_id": source_session_id,
            "messages": [],
            "system_prompt": "",
            "character_id": source_session.get("character_id") or sender_name,
            "sender_name": sender_name,
            "sender_avatar": source_session.get("sender_avatar", ""),
            "sender_portrait": source_session.get("sender_portrait", ""),
            "scenario": source_session.get("scenario", ""),
        }
        _repair_session_visuals(
            server,
            archive,
            {
                source_session_id: source_session,
                archive_id: archive,
            },
        )
        session_store.set_session(archive_id, archive)
        source_session["archive_session_id"] = archive_id
        session_store.set_session(source_session_id, source_session)
        return archive

    def _append_to_archive(archive_session, messages_to_add, label=""):
        existing = archive_session.get("messages", [])
        if label:
            existing.append({
                "id": f"archive_divider_{int(__import__('time').time())}",
                "role": "system",
                "content": f"━━━ {label} ━━━",
                "timestamp": datetime.now().isoformat(),
            })
        for msg in messages_to_add:
            if msg.get("role") == "system" and msg.get("id", "").startswith("summary_"):
                continue
            existing.append(msg)
        archive_session["messages"] = existing
        session_store.set_session(archive_session["id"], archive_session)

    def _is_archive_divider_message(msg):
        return (
            isinstance(msg, dict)
            and msg.get("role") == "system"
            and str(msg.get("id", "")).startswith("archive_divider_")
        )

    def _is_summary_message(msg):
        return (
            isinstance(msg, dict)
            and msg.get("role") == "system"
            and str(msg.get("id", "")).startswith("summary_")
        )

    def _split_archive_into_turns(messages):
        turns = []
        current = []
        for msg in messages or []:
            if not isinstance(msg, dict) or msg.get("role") == "system":
                continue
            role = msg.get("role")
            if role == "user" and current:
                turns.append(current)
                current = []
            current.append(msg)
        if current:
            turns.append(current)
        return turns

    def _find_insert_index_after_summary(messages):
        idx = 0
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system" and not _is_summary_message(messages[0]):
            idx = 1
        while idx < len(messages) and _is_summary_message(messages[idx]):
            idx += 1
        return idx

    def _cleanup_archive_messages_after_extract(messages):
        cleaned = list(messages or [])
        while cleaned and _is_archive_divider_message(cleaned[-1]):
            cleaned.pop()
        compact = []
        previous_divider = False
        for msg in cleaned:
            is_divider = _is_archive_divider_message(msg)
            if is_divider and previous_divider:
                continue
            compact.append(msg)
            previous_divider = is_divider
        return compact

    @app.route("/api/sessions/<session_id>/restore-from-archive", methods=["POST"])
    def restore_from_archive(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404
        if session.get("is_archive"):
            return jsonify({"success": False, "error": "归档会话不能执行此操作"}), 400

        data = request.get_json(silent=True) or {}
        try:
            turn_count = int(data.get("turns", data.get("count", 1)))
        except Exception:
            return jsonify({"success": False, "error": "turns must be an integer"}), 400
        if turn_count <= 0:
            return jsonify({"success": False, "error": "提取轮数必须大于 0"}), 400
        turn_count = min(turn_count, 100)

        archive_id = session.get("archive_session_id")
        if not archive_id:
            return jsonify({"success": False, "error": "当前会话没有关联归档"}), 400
        archive = session_store.get_session(archive_id) or get_session_from_db(server.data_dir, archive_id)
        if not archive:
            return jsonify({"success": False, "error": "归档会话不存在"}), 404

        archive_messages = list(archive.get("messages", []))
        turns = _split_archive_into_turns(archive_messages)
        if not turns:
            return jsonify({"success": False, "error": "归档中没有可提取的对话"}), 400

        selected_turns = turns[-turn_count:]
        selected_ids = {
            id(msg) for turn in selected_turns for msg in turn
        }
        selected_messages = [deepcopy(msg) for turn in selected_turns for msg in turn]
        selected_message_ids = {
            str(msg.get("id")) for turn in selected_turns for msg in turn if msg.get("id")
        }

        remaining_archive = []
        for msg in archive_messages:
            if id(msg) in selected_ids:
                continue
            if msg.get("id") and str(msg.get("id")) in selected_message_ids and msg.get("role") != "system":
                continue
            remaining_archive.append(msg)
        remaining_archive = _cleanup_archive_messages_after_extract(remaining_archive)

        current_messages = list(session.get("messages", []))
        insert_idx = _find_insert_index_after_summary(current_messages)
        inserted_at = datetime.now().isoformat()
        for msg in selected_messages:
            msg["restored_from_archive"] = archive_id
            msg["restored_at"] = inserted_at
        new_messages = current_messages[:insert_idx] + selected_messages + current_messages[insert_idx:]

        session["messages"] = new_messages
        archive["messages"] = remaining_archive
        session_store.set_session(archive_id, archive)
        session_store.set_session(session_id, session)

        return jsonify({
            "success": True,
            "turns_requested": turn_count,
            "turns_restored": len(selected_turns),
            "messages_restored": len(selected_messages),
            "archive_session_id": archive_id,
            "archive_messages_remaining": len([m for m in remaining_archive if isinstance(m, dict) and m.get("role") != "system"]),
            "insert_index": insert_idx,
        })

    @app.route("/api/sessions/<session_id>/compress", methods=["POST"])
    def compress_context(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = session.get("messages", [])
        # 已经压缩过的会话允许更少消息即可再次压缩，确保归档可以持续追加
        has_been_compressed = any(
            m.get("role") == "system" and m.get("id", "").startswith("summary_")
            for m in messages
        )
        min_messages = 5 if has_been_compressed else 10
        if len(messages) < min_messages:
            return jsonify({"success": False, "error": "消息数量不足，无需压缩"}), 400

        system_msg = None
        if messages and messages[0].get("role") == "system":
            system_msg = messages[0]

        keep_count = min(3 if has_been_compressed else 5, len(messages) - 2)
        recent_messages = messages[-keep_count:] if messages else []

        compress_start = 1 if system_msg else 0
        compress_end = len(messages) - keep_count

        if compress_end <= compress_start:
            return jsonify(
                {"success": False, "error": "没有足够的早期消息需要压缩"}
            ), 400

        messages_to_compress = messages[compress_start:compress_end]
        if not messages_to_compress:
            return jsonify({"success": False, "error": "没有消息需要压缩"}), 400

        # 过滤掉已有的summary消息，避免【对话总结】标记重复嵌套
        messages_to_compress = [
            msg for msg in messages_to_compress
            if not (msg.get("role") == "system" and str(msg.get("id", "")).startswith("summary_"))
        ]

        if not messages_to_compress and not has_been_compressed:
            return jsonify({"success": False, "error": "没有消息需要压缩"}), 400

        conversation_text = "\n".join(
            [
                f"[{msg.get('role', 'user')}]: {msg.get('content', '')[:500]}"
                for msg in messages_to_compress
                if msg.get("content")
            ]
        )

        if has_been_compressed:
            summary_prompt = f"""以下内容包含上一轮的对话摘要和后续新的对话，请将它们融合成一份新的简洁总结。旧摘要中以"【对话总结】"开头的内容是之前对话的要点，请吸收保留其中仍然重要的信息，与新对话内容整合，不要丢失关键上下文：

{conversation_text}

请融合以上所有信息，用80-150字总结："""
        else:
            summary_prompt = f"""请简洁地总结以下对话的主要内容，保留关键信息和结论：

{conversation_text}

请用50-100字总结："""

        try:
            if not server.ai_client:
                return jsonify(
                    {"success": False, "error": "AI服务不可用，请先配置AI"}
                ), 503

            _log.info(f"[Compress] 开始压缩会话 {session_id[:8]}... 的上下文")

            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[{"role": "user", "content": summary_prompt}],
                stream=False,
            )

            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_WEB_FEATURE
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        session_id=session_id,
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_WEB_FEATURE,
                    )
            except Exception:
                pass

            summary = response.choices[0].message.content.strip()

            # 清理AI输出中可能包含的嵌套【对话总结】标记，避免重复嵌套
            import re
            summary = re.sub(r'【对话总结】\s*', '', summary).strip()
            # 去除可能的markdown加粗标记
            summary = re.sub(r'\*\*【对话总结】\*\*\s*', '', summary).strip()

            archive = _get_or_create_archive_session(session_id, session)
            compress_label = f"压缩于 {datetime.now().strftime('%Y-%m-%d %H:%M')} ({len(messages_to_compress)} 条消息)"
            _append_to_archive(archive, messages_to_compress, label=compress_label)

            new_messages = [system_msg] if system_msg else []
            summary_msg = {
                "id": f"summary_{int(__import__('time').time())}",
                "role": "system",
                "content": f"【对话总结】{summary}",
                "timestamp": __import__('time').time(),
            }
            new_messages.append(summary_msg)
            new_messages.extend(recent_messages)

            session_store.replace_messages(session_id, new_messages)

            # 更新主动聊天计时，防止压缩后因消息时间戳变旧而立即触发主动聊天
            session = session_store.get_session(session_id)
            if session and isinstance(session.get("proactive_chat"), dict) and session["proactive_chat"].get("enabled"):
                session["proactive_chat_last_run"] = datetime.now().isoformat()
                session.pop("proactive_chat_pending_since", None)
                session_store.set_session(session_id, session)

            _log.info(
                f"[Compress] 上下文压缩完成: {session_id[:8]}... ({len(messages_to_compress)} 条消息被压缩，已归档到 {archive['id'][:8]}...)"
            )

            return jsonify(
                {
                    "success": True,
                    "compressed_count": len(messages_to_compress),
                    "summary": summary[:200],
                    "archive_session_id": archive["id"],
                }
            )
        except Exception as e:
            _log.error(f"[Compress] 压缩上下文失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"压缩失败: {str(e)}"}), 500

    @app.route("/api/sessions/<session_id>/archive-branch", methods=["POST"])
    def archive_plot_branch(session_id):
        """归档剧情某个分支：物化根→指定节点的对话，追加到归档会话。

        复用既有归档逻辑（_get_or_create_archive_session + _append_to_archive），
        因此归档结果与压缩归档一致，都会写入同一个归档会话。
        """
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.get_json(silent=True) or {}
        node_id = str(data.get("node_id") or "").strip()
        if not node_id:
            return jsonify({"success": False, "error": "node_id is required"}), 400

        try:
            from nbot.plot.graph_manager import get_plot_graph_manager
            data_dir = getattr(server, "data_dir", "data/web")
            manager = get_plot_graph_manager(data_dir=data_dir)
            node = manager.get_node(node_id)
            if node is None:
                return jsonify({"success": False, "error": "node not found"}), 404

            system_prompt = str(session.get("system_prompt") or "").strip()
            materialized = manager.materialize_path(session_id, node_id, system_prompt)
            branch_messages = [m for m in materialized if m.get("role") != "system"]
            if not branch_messages:
                return jsonify({"success": False, "error": "该分支没有可归档的对话"}), 400

            archive = _get_or_create_archive_session(session_id, session)
            label = (
                f"分支归档 {datetime.now().strftime('%Y-%m-%d %H:%M')} "
                f"（节点：{node.title or node_id[:8]}，{len(branch_messages)} 条）"
            )
            _append_to_archive(archive, deepcopy(branch_messages), label=label)

            _log.info(
                "[ArchiveBranch] 会话 %s 分支 %s 已归档到 %s（%d 条）",
                session_id[:8], node_id, archive["id"][:8], len(branch_messages),
            )
            return jsonify({
                "success": True,
                "node_id": node_id,
                "archived_count": len(branch_messages),
                "archive_session_id": archive["id"],
            })
        except Exception as e:
            _log.error("[ArchiveBranch] 归档分支失败: %s", e, exc_info=True)
            return jsonify({"success": False, "error": f"归档失败: {str(e)}"}), 500

    @app.route("/api/sessions/<session_id>/ai-summary", methods=["POST"])
    def ai_summary_session(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        messages = session.get("messages", [])
        non_system = [m for m in messages if m.get("role") != "system"]
        if not non_system:
            return jsonify({"success": False, "error": "没有可总结的对话内容"}), 400

        character_name = _resolve_session_character_name(server, session)
        user_name = session.get("user_id", "")
        memory_target_id = (
            session.get("user_id")
            or session.get("qq_id")
            or session.get("target_id")
            or ""
        )

        conversation_text = "\n".join(
            [
                f"[{msg.get('role', 'user')}]: {msg.get('content', '')[:800]}"
                for msg in non_system
                if msg.get("content")
            ]
        )

        summary_prompt = f"""请对以下完整对话进行深度总结，提取出值得长期记住的关键信息。

角色名: {character_name or '未知'}
用户名: {user_name or '用户'}

对话内容:
{conversation_text}

请按以下格式输出总结：

## 对话总结
（用2-3段话概括对话的主要内容和走向）

## 关键信息
（列出3-5条最值得记住的关键事实、关系变化、重要决定等）

## 角色记忆
（提取2-3条与角色"{character_name}"直接相关的、值得保存为长期记忆的信息，每条包含标题和内容）"""

        try:
            if not server.ai_client:
                return jsonify({"success": False, "error": "AI服务不可用"}), 503

            _log.info(f"[AISummary] 开始总结会话 {session_id[:8]}...")

            response = server.ai_client.chat_completion(
                model=server.ai_model,
                messages=[{"role": "user", "content": summary_prompt}],
                stream=False,
            )

            # 记录 token 用量
            try:
                from nbot.core.token_stats import get_token_stats_manager, PURPOSE_MEMORY
                usage = getattr(response, "usage", None)
                if usage:
                    stats_mgr = get_token_stats_manager()
                    stats_mgr.record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=server.ai_model or "",
                        session_id=session_id,
                        channel_type="web",
                        source="web",
                        purpose=PURPOSE_MEMORY,
                    )
            except Exception:
                pass

            summary_text = response.choices[0].message.content.strip()

            saved_memories = 0
            if character_name and server.PROMPT_MANAGER_AVAILABLE and server.prompt_manager:
                memory_prompt = f"""从以下对话总结中，提取与角色"{character_name}"直接相关的、值得长期保存的记忆。
每条记忆必须与角色"{character_name}"有关，不要提取通用信息。
返回JSON数组，每项包含 title、content、type("long"/"short") 字段。如果没有值得保存的记忆，返回[]。

对话总结:
{summary_text}"""
                try:
                    mem_response = server.ai_client.chat_completion(
                        model=server.ai_model,
                        messages=[{"role": "user", "content": memory_prompt}],
                        stream=False,
                    )
                    # 记录 token 用量
                    try:
                        from nbot.core.token_stats import get_token_stats_manager, PURPOSE_MEMORY
                        usage = getattr(mem_response, "usage", None)
                        if usage:
                            stats_mgr = get_token_stats_manager()
                            stats_mgr.record_usage(
                                prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                                completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                                total_tokens=getattr(usage, "total_tokens", 0) or 0,
                                model=server.ai_model or "",
                                session_id=session_id,
                                channel_type="web",
                                source="web",
                                purpose=PURPOSE_MEMORY,
                            )
                    except Exception:
                        pass
                    import json
                    mem_text = mem_response.choices[0].message.content.strip()
                    json_start = mem_text.find("[")
                    json_end = mem_text.rfind("]") + 1
                    if json_start >= 0 and json_end > json_start:
                        mem_items = json.loads(mem_text[json_start:json_end])
                        for item in mem_items[:3]:
                            title = str(item.get("title", "")).strip()
                            content = str(item.get("content", "")).strip()
                            if not title or not content:
                                continue
                            if server.prompt_manager.add_memory(
                                title, content,
                                memory_target_id,
                                None,
                                item.get("type", "long"),
                                7,
                                character_name,
                            ):
                                saved_memories += 1
                        if saved_memories:
                            server.memories = server.prompt_manager.get_memories()
                            server._save_data("memories")
                except Exception as mem_exc:
                    _log.warning(f"[AISummary] 保存记忆失败: {mem_exc}")

            _log.info(f"[AISummary] 总结完成: {session_id[:8]}... (保存了 {saved_memories} 条记忆)")

            return jsonify({
                "success": True,
                "summary": summary_text,
                "saved_memories": saved_memories,
            })
        except Exception as e:
            _log.error(f"[AISummary] 总结失败: {e}", exc_info=True)
            return jsonify({"success": False, "error": f"总结失败: {str(e)}"}), 500

    @app.route("/api/sessions/<session_id>/chat", methods=["POST"])
    def chat_with_ai(session_id):
        session = _get_web_session(session_id)
        if not session:
            return jsonify({"error": "Session not found"}), 404

        data = request.json or {}
        user_content = (data.get("content") or "").strip()
        if not user_content:
            return jsonify({"error": "Content is required"}), 400

        try:
            from nbot.message_filter import message_filter

            filter_session_id = f"web:{session_id}"
            matched_rules = message_filter.match_all(
                user_content,
                channel="web",
                session_id=filter_session_id,
            )
            if matched_rules:
                recall_rules = [r for r in matched_rules if r.get("action") == "recall"]
                strip_rules = [r for r in matched_rules if r.get("action") != "recall"]
                if recall_rules:
                    return jsonify({
                        "success": True,
                        "filtered": True,
                        "message": "当前内容被过滤",
                    })
                if strip_rules:
                    user_content = message_filter.strip_content(user_content, strip_rules)
                    if not user_content:
                        return jsonify({
                            "success": True,
                            "filtered": True,
                            "message": "当前内容被过滤",
                        })
        except Exception as filter_err:
            _log.warning("[消息过滤] Web 检查异常: %s", filter_err)

        user_message = {
            "id": str(uuid.uuid4()),
            "role": "user",
            "content": user_content,
            "timestamp": datetime.now().isoformat(),
            "sender": data.get("sender", "web_user"),
            "source": "web",
            "session_id": session_id,
        }

        session_store.append_message(session_id, user_message)

        server._trigger_ai_response(
            session_id,
            user_content,
            user_message["sender"],
            data.get("attachments", []),
            user_message["id"],
        )

        return jsonify({"success": True, "user_message": user_message})

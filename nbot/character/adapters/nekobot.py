"""
NekoBot 适配器

将 NekoBot 现有的 personality / session 系统桥接到角色运行时引擎。
提供 WebCallbacks 和 QQ 频道的角色身份解析。
"""

import logging
from typing import Any, Dict, Optional

from nbot.character.channel_context import ChannelRuntimeContext
from nbot.character.dispatcher import build_scope_id
from nbot.character.models import CharacterIdentity
from nbot.character.repository import ProfileRepository

_log = logging.getLogger(__name__)


def _get_base_dir(server) -> str:
    base_dir = getattr(server, "base_dir", None)
    if base_dir:
        return base_dir

    import os

    return os.path.abspath(
        os.path.join(os.path.dirname(__file__), "..", "..", "..")
    )


def _load_character_runtime_config() -> dict[str, Any]:
    try:
        from nbot.web.utils.config_loader import get_character_runtime_config

        return get_character_runtime_config() or {}
    except Exception as exc:
        _log.debug("[CharacterRuntime] failed to load runtime config: %s", exc)
        return {}


def _get_channel_runtime_config(channel: str) -> dict[str, Any]:
    config = _load_character_runtime_config()
    return config.get("channels", {}).get(channel, {}).get("character_runtime", {})


def _get_global_runtime_config() -> dict[str, Any]:
    config = _load_character_runtime_config()
    return config.get("character_runtime", {})


def _is_channel_runtime_enabled(channel: str) -> bool:
    runtime_config = _get_channel_runtime_config(channel)
    if "enabled" in runtime_config:
        return bool(runtime_config["enabled"])
    return bool(_get_global_runtime_config().get("default_enabled", True))


def _resolve_configured_memory_scope(channel: str, default_scope: str) -> str:
    runtime_config = _get_channel_runtime_config(channel)
    if "memory_scope" in runtime_config:
        return str(runtime_config.get("memory_scope") or "").strip() or "conversation"
    return default_scope


def _make_scope_id(
    *,
    channel: str,
    default_scope: str,
    conversation_id: str,
    user_id: str = "",
    group_id: str = "",
    thread_id: str = "",
) -> str:
    memory_scope = _resolve_configured_memory_scope(channel, default_scope)
    context = ChannelRuntimeContext(
        channel=channel,
        conversation_id=conversation_id,
        scene="group" if group_id else "private",
        user_id=user_id,
        group_id=group_id,
        thread_id=thread_id,
    )
    # 私聊场景下 group/group_user scope 无意义，降级为 user
    if memory_scope in ("group", "group_user") and context.scene == "private":
        memory_scope = "user"
    return build_scope_id(context, memory_scope)


def _resolve_web_character_id(
    server,
    session: Dict[str, Any],
    session_id: str = "",
) -> str:
    runtime_snapshot = session.get("character_runtime_snapshot")
    timeline = session.get("character_runtime_timeline")
    timeline_snapshot = timeline[-1] if isinstance(timeline, list) and timeline else {}
    runtime_candidates = [
        runtime_snapshot.get("character_id") if isinstance(runtime_snapshot, dict) else None,
        timeline_snapshot.get("character_id") if isinstance(timeline_snapshot, dict) else None,
    ]
    session_candidates = [
        session.get("character_id"),
        session.get("sender_name"),
    ]

    repo = ProfileRepository(_get_base_dir(server))
    for candidate in runtime_candidates:
        candidate = str(candidate or "").strip()
        if candidate and repo.get(candidate):
            return candidate

    for candidate in runtime_candidates:
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate

    if session_id:
        try:
            from nbot.character.repository import RelationshipRepository

            relationship = RelationshipRepository(_get_base_dir(server)).get_by_target(
                f"web:{session_id}"
            )
            if relationship and relationship.character_id:
                return str(relationship.character_id)
        except Exception:
            pass

    for candidate in session_candidates:
        candidate = str(candidate or "").strip()
        if candidate and repo.get(candidate):
            return candidate

    for candidate in session_candidates:
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate

    personality = getattr(server, "personality", {}) or {}
    for candidate in (personality.get("id"), personality.get("name")):
        candidate = str(candidate or "").strip()
        if candidate:
            return candidate
    return "default"


def get_web_character_context(
    server,
    session_store,
    session_id: str,
) -> Optional[CharacterIdentity]:
    """从 Web 会话中解析角色身份"""
    if not _is_channel_runtime_enabled("web"):
        return None

    session = session_store.get_session(session_id) if session_store else {}
    if not session:
        session = {}

    # 角色ID：优先从会话中获取，避免全局当前角色切换后污染旧会话
    character_id = _resolve_web_character_id(server, session, session_id=session_id)

    # 目标ID：Web 会话标识。关系状态需要按会话隔离，避免新会话继承旧会话的六维进行时。
    target_id = f"web:{session_id}"

    return CharacterIdentity(
        character_id=str(character_id),
        target_id=target_id,
        scope_id=f"web:{session_id}",
        channel="web",
    )


def get_qq_character_context(
    user_id: str,
    group_id: Optional[str] = None,
    personality_name: str = "default",
) -> Optional[CharacterIdentity]:
    """从 QQ 消息中解析角色身份"""
    if not _is_channel_runtime_enabled("qq"):
        return None

    user_id = str(user_id or "anonymous")
    group_id = str(group_id or "")
    conversation_id = f"qq:group:{group_id}" if group_id else f"qq:private:{user_id}"

    if group_id:
        # 群聊：允许配置覆盖 memory_scope
        scope_id = _make_scope_id(
            channel="qq",
            default_scope="group_user",
            conversation_id=conversation_id,
            user_id=user_id,
            group_id=group_id,
        )
    else:
        # 私聊：固定 user scope，不走配置覆盖，避免 group_user 导致 scope_id 畸形
        from nbot.character.dispatcher import build_scope_id as _build_scope_id
        from nbot.character.channel_context import ChannelRuntimeContext
        scope_id = _build_scope_id(
            ChannelRuntimeContext(
                channel="qq", conversation_id=conversation_id,
                scene="private", user_id=user_id,
            ),
            "user",
        )

    return CharacterIdentity(
        character_id=personality_name,
        target_id=user_id,
        scope_id=scope_id,
        channel="qq",
    )


def get_qqbot_character_context(
    user_id: str,
    group_openid: Optional[str] = None,
    personality_name: str = "default",
) -> Optional[CharacterIdentity]:
    """从 QQ Bot 官方接口消息中解析角色身份"""
    if not _is_channel_runtime_enabled("qqbot"):
        return None

    user_id = str(user_id or "anonymous")
    group_openid = str(group_openid or "")
    conversation_id = (
        f"qqbot:group:{group_openid}" if group_openid else f"qqbot:private:{user_id}"
    )

    if group_openid:
        scope_id = _make_scope_id(
            channel="qqbot",
            default_scope="group_user",
            conversation_id=conversation_id,
            user_id=user_id,
            group_id=group_openid,
        )
    else:
        scope_id = build_scope_id(
            ChannelRuntimeContext(
                channel="qqbot", conversation_id=conversation_id,
                scene="private", user_id=user_id,
            ),
            "user",
        )

    return CharacterIdentity(
        character_id=personality_name,
        target_id=user_id,
        scope_id=scope_id,
        channel="qqbot",
    )


def get_feishu_character_context(
    user_id: str,
    chat_id: Optional[str] = None,
    open_id: Optional[str] = None,
    personality_name: str = "default",
) -> Optional[CharacterIdentity]:
    """从飞书消息中解析角色身份"""
    if not _is_channel_runtime_enabled("feishu"):
        return None

    target_id = str(user_id or open_id or "anonymous")
    chat_id = str(chat_id or "")
    conversation_id = f"feishu:{chat_id or target_id}"
    scope_id = _make_scope_id(
        channel="feishu",
        default_scope="chat_user" if chat_id else "user",
        conversation_id=conversation_id,
        user_id=target_id,
    )

    return CharacterIdentity(
        character_id=personality_name,
        target_id=target_id,
        scope_id=scope_id,
        channel="feishu",
    )


def get_telegram_character_context(
    user_id: str,
    chat_id: Optional[str] = None,
    thread_id: Optional[str] = None,
    personality_name: str = "default",
) -> Optional[CharacterIdentity]:
    """从 Telegram 消息中解析角色身份"""
    if not _is_channel_runtime_enabled("telegram"):
        return None

    user_id = str(user_id or "anonymous")
    chat_id = str(chat_id or "")
    thread_id = str(thread_id or "")
    conversation_id = f"telegram:{chat_id or user_id}"
    scope_id = _make_scope_id(
        channel="telegram",
        default_scope="thread" if thread_id else ("chat_user" if chat_id else "user"),
        conversation_id=conversation_id,
        user_id=user_id,
        thread_id=thread_id,
    )

    return CharacterIdentity(
        character_id=personality_name,
        target_id=user_id,
        scope_id=scope_id,
        channel="telegram",
    )


def get_character_runtime_from_server(server):
    """从 NBotWebServer 获取 CharacterRuntime 实例

    Args:
        server: NBotWebServer 实例

    Returns:
        CharacterRuntime 实例或 None
    """
    runtime = getattr(server, "character_runtime", None)

    try:
        from nbot.character.memory import PromptManagerMemoryAdapter
        from nbot.character.planner import ReactionPlanner
        from nbot.character.policies import SignalAnalyzer
        from nbot.character.repository import (
            CharacterStateRepository,
            ProfileRepository,
            RelationshipRepository,
        )
        from nbot.character.runtime import CharacterRuntime
        from nbot.character.state_machine import StateMachine

        base_dir = _get_base_dir(server)

        profile_repo = ProfileRepository(base_dir)
        personality = getattr(server, "personality", {}) or {}
        if isinstance(personality, dict) and personality:
            # 始终同步最新的 personality 到 profiles.json，确保 initial_state 是最新的
            from nbot.character.models import CharacterProfile
            profile = CharacterProfile.from_personality_dict(personality)
            if not profile.id:
                profile.id = profile.name or "default"
            profile_repo.save(profile)

        if runtime:
            # runtime 已存在，更新所有 repo 引用，确保数据同步
            runtime.profile_repo = profile_repo
            # 同时刷新 relationship_repo 和 state_repo，避免缓存旧数据
            runtime.relationship_repo = RelationshipRepository(base_dir)
            runtime.state_repo = CharacterStateRepository(base_dir)
            if not getattr(runtime, '_world_book_store', None):
                from nbot.character.storage.world_book_store import WorldBookStore
                runtime._world_book_store = WorldBookStore(base_dir)
            if not getattr(runtime, "history_store", None):
                from nbot.character.state_history import StateHistoryStore
                runtime.history_store = StateHistoryStore(base_dir)
            _log.debug("[CharacterRuntime] refreshed repos for character=%s",
                       getattr(server, "personality", {}).get("name", "unknown"))
            return runtime

        from nbot.character.storage.world_book_store import WorldBookStore
        from nbot.character.state_history import StateHistoryStore

        _hook_rt = None
        try:
            from nbot.hooks.manager import get_hook_manager
            _hook_rt = get_hook_manager()
        except Exception:
            pass
        runtime = CharacterRuntime(
            profile_repo=profile_repo,
            state_repo=CharacterStateRepository(base_dir),
            relationship_repo=RelationshipRepository(base_dir),
            memory_service=PromptManagerMemoryAdapter(),
            signal_analyzer=SignalAnalyzer(),
            planner=ReactionPlanner(),
            state_machine=StateMachine(),
            world_book_store=WorldBookStore(base_dir),
            hook_runtime=_hook_rt,
            history_store=StateHistoryStore(base_dir),
        )
        server.character_runtime = runtime
        _log.info("[CharacterRuntime] initialized lazily from Web adapter")
        return runtime
    except Exception as exc:
        _log.warning(
            "[CharacterRuntime] lazy initialization failed: %s",
            exc,
            exc_info=True,
        )
        return None
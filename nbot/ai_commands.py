import asyncio
import json
import os
import time
import uuid
from datetime import datetime

from nbot.services.chat_service import (
    delete_session_workspace,
    generate_today_summary,
    get_qq_session_id,
    group_messages,
    load_canonical_qq_messages,
    summarize_group_text,
    user_messages,
)

# 模块级别的 bot 实例引用，用于群聊历史记录功能
_bot_instance = None


def _extract_history_text_item(item):
    """从历史记录项中提取文本内容"""
    if isinstance(item, dict):
        message = item.get("message", {})
        if isinstance(message, dict):
            text = message.get("text", "")
            if text:
                return text
            # 处理消息段数组
            segments = message.get("data", [])
            if isinstance(segments, list):
                texts = []
                for seg in segments:
                    if isinstance(seg, dict) and seg.get("type") == "text":
                        seg_data = seg.get("data", {})
                        if isinstance(seg_data, dict):
                            texts.append(seg_data.get("text", ""))
                return " ".join(texts)
        return str(message)
    return (
        getattr(item, "text", None)
        or getattr(item, "content", None)
        or ""
    ).strip()


def _history_items_to_text(items):
    """将历史记录项列表转换为文本格式"""
    lines = []
    for item in items:
        user_id = None
        nickname = ""
        if isinstance(item, dict):
            user_id = item.get("user_id")
            sender = item.get("sender")
            if isinstance(sender, dict):
                nickname = sender.get("nickname", "") or ""
        else:
            user_id = getattr(item, "user_id", None)
            sender = getattr(item, "sender", None)
            if sender is not None:
                try:
                    nickname = sender.nickname
                except Exception:
                    if isinstance(sender, dict):
                        nickname = sender.get("nickname", "") or ""
        text = _extract_history_text_item(item)
        uid_str = str(user_id) if user_id is not None else ""
        name_part = nickname or uid_str
        lines.append(f"{name_part}: {text}" if name_part else text)
    return "\n".join(lines)


async def get_group_history_items(group_id, count, bot=None):
    """获取群聊历史消息记录

    Args:
        group_id: 群ID
        count: 获取消息数量
        bot: 可选的 bot 实例，如果不提供则使用模块级别的 _bot_instance

    Returns:
        消息列表
    """
    bot_instance = bot or _bot_instance
    if not bot_instance:
        return []

    try:
        history = await bot_instance.api.get_group_msg_history(
            group_id,
            message_seq=0,
            count=count,
            reverse_order=True,
        )
        if isinstance(history, list):
            return history
        if isinstance(history, dict):
            data = history.get("data")
            if isinstance(data, list):
                return data
            if isinstance(data, dict) and isinstance(data.get("messages"), list):
                return data["messages"]
        return []
    except Exception as e:
        return []


def history_items_to_text(items):
    """将历史记录项列表转换为文本格式（模块级公开函数）"""
    return _history_items_to_text(items)


def _reset_canonical_qq_session_messages(
    user_id=None,
    group_id=None,
    group_user_id=None,
    keep_system_prompt=True,
):
    session_id = get_qq_session_id(
        user_id=str(user_id) if user_id else None,
        group_id=str(group_id) if group_id else None,
        group_user_id=str(group_user_id) if group_user_id else None,
    )
    if not session_id:
        return
    try:
        from nbot.core.session_store import WebSessionStore
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        if not server or not hasattr(server, "sessions"):
            return

        save_callback = getattr(server, "_save_data", lambda _name: None)
        session_store = WebSessionStore(
            server.sessions,
            save_callback=lambda: save_callback("sessions"),
        )
        session = session_store.get_session(session_id)
        if not session:
            return

        if keep_system_prompt:
            system_msg = next(
                (m for m in session.get("messages", []) if m.get("role") == "system"),
                None,
            )
            session_store.replace_messages(session_id, [system_msg] if system_msg else [])
        else:
            session_store.replace_messages(session_id, [])
        session["last_message"] = ""
        session["updated_at"] = datetime.now().isoformat()
        session_store.set_session(session_id, session)
    except Exception:
        return


def _replace_canonical_qq_session_messages(
    *,
    user_id=None,
    group_id=None,
    group_user_id=None,
    messages=None,
    system_prompt=None,
    character=None,
):
    session_id = get_qq_session_id(
        user_id=str(user_id) if user_id else None,
        group_id=str(group_id) if group_id else None,
        group_user_id=str(group_user_id) if group_user_id else None,
    )
    if not session_id:
        return
    try:
        from nbot.core.session_store import WebSessionStore
        from nbot.web.server import WebChatServer

        server = WebChatServer.get_instance()
        if not server or not hasattr(server, "sessions"):
            return

        save_callback = getattr(server, "_save_data", lambda _name: None)
        session_store = WebSessionStore(
            server.sessions,
            save_callback=lambda: save_callback("sessions"),
        )
        session = session_store.get_session(session_id)
        if not session:
            return

        session_messages = [dict(message) for message in (messages or []) if isinstance(message, dict)]
        if system_prompt is not None and not any(m.get("role") == "system" for m in session_messages):
            session_messages.insert(0, {"role": "system", "content": system_prompt})
        session_store.replace_messages(session_id, session_messages)
        if system_prompt is not None:
            session["system_prompt"] = system_prompt
        elif session_messages and session_messages[0].get("role") == "system":
            session["system_prompt"] = session_messages[0].get("content", "")
        else:
            session["system_prompt"] = session.get("system_prompt", "")
        if character:
            session["character_id"] = character.get("id") or character.get("name") or ""
            session["sender_name"] = character.get("name") or session.get("sender_name") or "AI"
            session["sender_avatar"] = character.get("avatar", "")
            session["sender_portrait"] = character.get("portrait", "")
        session["last_message"] = next(
            (
                str(m.get("content") or "")[:100]
                for m in reversed(session_messages)
                if isinstance(m, dict) and m.get("role") != "system"
            ),
            "",
        )
        session["updated_at"] = datetime.now().isoformat()
        session_store.set_session(session_id, session)
    except Exception:
        return


def _replace_direct_session_messages(
    session_id,
    messages,
    system_prompt=None,
    character=None,
    server=None,
):
    """直接按 session_id 更新 session store（用于飞书/Web 等非 QQ 渠道）"""
    try:
        from nbot.core.session_store import WebSessionStore
        if server is None:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
        if not server or not hasattr(server, "sessions"):
            return

        save_callback = getattr(server, "_save_data", lambda _name: None)
        session_store = WebSessionStore(
            server.sessions,
            save_callback=lambda: save_callback("sessions"),
        )
        session = session_store.get_session(session_id)
        if not session:
            return

        session_messages = [dict(m) for m in (messages or []) if isinstance(m, dict)]
        if system_prompt is not None and not any(m.get("role") == "system" for m in session_messages):
            session_messages.insert(0, {"role": "system", "content": system_prompt})
        session_store.replace_messages(session_id, session_messages)
        if system_prompt is not None:
            session["system_prompt"] = system_prompt
        elif session_messages and session_messages[0].get("role") == "system":
            session["system_prompt"] = session_messages[0].get("content", "")
        if character:
            session["character_id"] = character.get("id") or character.get("name") or ""
            session["sender_name"] = character.get("name") or session.get("sender_name") or "AI"
            session["sender_avatar"] = character.get("avatar", "")
            session["sender_portrait"] = character.get("portrait", "")
        session["updated_at"] = datetime.now().isoformat()
        session_store.set_session(session_id, session)
    except Exception:
        return


def register_ai_commands(
    *,
    register_command,
    bot,
    log,
    project_root,
    admin,
    read_secure_json,
    write_secure_json,
    workspace_available,
    switch,
    running,
    write_running,
    normalize_timestamp,
    normalize_file_path,
    load_address,
):
    """Register AI/session commands that are shared by QQ and Web history."""
    # 设置模块级别的 bot 实例引用
    global _bot_instance
    _bot_instance = bot

    def web_data_dir():
        return os.path.join(project_root(), "data", "web")

    def resume_bindings_path():
        return os.path.join(web_data_dir(), "qq_web_session_bindings.json")

    def load_resume_bindings():
        path = resume_bindings_path()
        try:
            if os.path.exists(path):
                with open(path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                return data if isinstance(data, dict) else {}
        except Exception as e:
            log.warning(f"Load resume bindings failed: {e}")
        return {}

    def save_resume_bindings(bindings):
        os.makedirs(web_data_dir(), exist_ok=True)
        with open(resume_bindings_path(), "w", encoding="utf-8") as f:
            json.dump(bindings, f, ensure_ascii=False, indent=2)

    def save_qq_histories():
        os.makedirs("saved_message", exist_ok=True)
        with open("saved_message/user_messages.json", "w", encoding="utf-8") as f:
            json.dump(user_messages, f, ensure_ascii=False, indent=4)
        with open("saved_message/group_messages.json", "w", encoding="utf-8") as f:
            json.dump(group_messages, f, ensure_ascii=False, indent=4)

    async def reply_current_channel(msg, is_group, text):
        if is_group:
            await msg.reply(text=text)
        else:
            # 用 _bot_instance 而非闭包 bot，确保外部 patch 生效（如飞书命令适配）
            _api = getattr(_bot_instance, "api", None)
            if _api:
                await _api.post_private_msg(msg.user_id, text=text)
            else:
                await msg.reply(text=text)

    def is_direct_channel_context(msg):
        session_id = str(getattr(msg, "session_id", "") or "").strip()
        return bool(session_id and getattr(msg, "server", None) is not None)

    def needs_legacy_group_admin(msg, is_group):
        return (
            bool(is_group)
            and not is_direct_channel_context(msg)
            and str(getattr(msg, "user_id", "")) not in admin
        )

    def needs_legacy_admin(msg):
        return (
            not is_direct_channel_context(msg)
            and str(getattr(msg, "user_id", "")) not in admin
        )

    def resolve_current_session_id(msg, is_group):
        session_id = str(getattr(msg, "session_id", "") or "").strip()
        if session_id:
            return session_id
        if is_group:
            return get_qq_session_id(
                group_id=str(getattr(msg, "group_id", "") or ""),
                group_user_id=str(getattr(msg, "user_id", "") or ""),
            )
        return get_qq_session_id(user_id=str(getattr(msg, "user_id", "") or ""))

    def load_character_options():
        characters = []
        seen = set()

        def add_character(data):
            if not isinstance(data, dict):
                return
            name = str(data.get("name") or "").strip()
            character_id = str(data.get("id") or name).strip()
            if not name and not character_id:
                return
            key = character_id or name
            if key in seen:
                return
            seen.add(key)
            item = dict(data)
            item["id"] = character_id or name
            item["name"] = name or character_id
            characters.append(item)

        base_dir = project_root()

        try:
            from nbot.character.repository import ProfileRepository

            for profile in ProfileRepository(base_dir).list_all():
                add_character(profile.to_personality_dict())
        except Exception as e:
            log.warning(f"Load character profiles failed: {e}")

        for file_path in (
            os.path.join(base_dir, "resources", "prompts", "personality.json"),
            os.path.join(base_dir, "data", "web", "custom_personality_presets.json"),
        ):
            try:
                if not os.path.exists(file_path):
                    continue
                with open(file_path, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, list):
                    for item in data:
                        add_character(item)
                elif isinstance(data, dict):
                    add_character(data)
            except Exception as e:
                log.warning(f"Load character file failed: {file_path}, {e}")

        return characters

    def compile_character_prompt(character):
        if character.get("systemPrompt"):
            return str(character.get("systemPrompt") or "")
        from nbot.character.compiler import compile_personality_prompt

        return compile_personality_prompt(character)

    def resolve_character_session(msg, is_group):
        session_id = (
            getattr(msg, "session_id", None)
            or getattr(msg, "conversation_id", None)
        )
        if session_id:
            return "channel", str(session_id)

        if is_group and getattr(msg, "group_id", None):
            return "group", str(msg.group_id)
        if not is_group and getattr(msg, "user_id", None):
            return "user", str(msg.user_id)

        session_id = getattr(msg, "chat_id", None)
        if session_id:
            return "channel", str(session_id)

        if getattr(msg, "user_id", None):
            return "user", str(msg.user_id)
        return "", ""

    def get_session_prompt(scope, target_id, msg=None):
        if scope == "channel":
            session_id = getattr(msg, "session_id", None) or target_id
            server = getattr(msg, "server", None)
            session = None
            if server and hasattr(server, "sessions"):
                session = server.sessions.get(str(session_id))
            if session:
                prompt = session.get("system_prompt") or session.get("prompt") or ""
                if prompt:
                    return str(prompt)
                for item in session.get("messages", []):
                    if item.get("role") == "system":
                        return str(item.get("content") or "")

        prefix = "group" if scope == "group" else "user" if scope == "user" else "channel"
        prompt_file = os.path.join(
            project_root(),
            "resources",
            "prompts",
            prefix,
            f"{prefix}_{target_id}.txt",
        )
        try:
            if os.path.exists(prompt_file):
                with open(prompt_file, "r", encoding="utf-8") as f:
                    return f.read()
        except Exception as e:
            log.warning(f"Load session prompt failed: {e}")
        return ""

    def get_active_character():
        personality_file = os.path.join(project_root(), "resources", "prompts", "personality.json")
        try:
            if os.path.exists(personality_file):
                with open(personality_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                if isinstance(data, dict):
                    return data
        except Exception as e:
            log.warning(f"Load active character failed: {e}")
        return {}

    def find_session_character(characters, scope, target_id, msg=None):
        current_prompt = get_session_prompt(scope, target_id, msg).strip()
        if current_prompt:
            for character in characters:
                try:
                    if compile_character_prompt(character).strip() == current_prompt:
                        return character
                except Exception:
                    continue
        return get_active_character()

    def find_character(characters, arg):
        if not arg:
            return None
        try:
            index = int(arg)
            if 1 <= index <= len(characters):
                return characters[index - 1]
        except ValueError:
            pass

        needle = arg.casefold()
        for character in characters:
            if needle in {
                str(character.get("id") or "").casefold(),
                str(character.get("name") or "").casefold(),
            }:
                return character

        matches = [
            c for c in characters
            if needle in str(c.get("id") or "").casefold()
            or needle in str(c.get("name") or "").casefold()
        ]
        return matches[0] if len(matches) == 1 else None

    def set_current_session_character(msg, is_group, character):
        base_dir = project_root()
        prompt = compile_character_prompt(dict(character))
        scope, target_id = resolve_character_session(msg, is_group)
        if not scope or not target_id:
            raise ValueError("无法识别当前会话")

        prefix = "group" if scope == "group" else "user" if scope == "user" else "channel"
        if scope != "channel":
            prompt_dir = os.path.join(base_dir, "resources", "prompts", prefix)
            prompt_file = os.path.join(prompt_dir, f"{prefix}_{target_id}.txt")

            os.makedirs(prompt_dir, exist_ok=True)
            with open(prompt_file, "w", encoding="utf-8") as f:
                f.write(prompt)

        if scope in {"group", "user"}:
            messages = group_messages if scope == "group" else user_messages
            messages[target_id] = [{"role": "system", "content": prompt}]
            save_qq_histories()
            _replace_canonical_qq_session_messages(
                group_id=target_id if scope == "group" else None,
                user_id=target_id if scope == "user" else None,
                messages=[{"role": "system", "content": prompt}],
                system_prompt=prompt,
                character=character,
            )

            if scope == "group":
                delete_session_workspace(
                    group_id=target_id,
                    group_user_id=str(getattr(msg, "user_id", "")),
                )
            else:
                delete_session_workspace(user_id=target_id)
        else:
            server = getattr(msg, "server", None)
            session_id = getattr(msg, "session_id", None) or target_id
            if server and hasattr(server, "sessions"):
                from nbot.core.session_store import WebSessionStore

                save_callback = getattr(server, "_save_data", lambda _name: None)
                session_store = WebSessionStore(
                    server.sessions,
                    save_callback=lambda: save_callback("sessions"),
                )
                session = session_store.get_session(str(session_id))
                if session is not None:
                    system_msg = {"role": "system", "content": prompt}
                    session["system_prompt"] = prompt
                    session["character_id"] = character.get("id") or character.get("name") or ""
                    session["sender_name"] = character.get("name") or "AI"
                    session["sender_avatar"] = character.get("avatar", "")
                    session["sender_portrait"] = character.get("portrait", "")
                    session_store.replace_messages(str(session_id), [system_msg])
                    session_store.set_session(str(session_id), session)
            if workspace_available:
                try:
                    from nbot.core.workspace import workspace_manager

                    workspace_manager.delete_workspace(str(session_id))
                except Exception as e:
                    log.debug(f"Delete channel workspace skipped: {e}")

        try:
            from nbot.core.prompt import prompt_manager

            prompt_manager._prompt_cache = {}
        except Exception:
            pass

    def current_qq_session_info(msg, is_group):
        # 飞书/Web 适配器自带 session_id，直接使用
        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        if direct_session_id:
            user_id = str(getattr(msg, "user_id", "") or "")
            group_id = str(getattr(msg, "group_id", "") or "") if is_group else ""
            return {
                "store": None,
                "history_key": direct_session_id,
                "session_id": direct_session_id,
                "direct": True,
                "server": getattr(msg, "server", None),
                "user_id": "" if is_group else user_id,
                "group_id": group_id if is_group else "",
                "group_user_id": user_id if is_group else "",
                "label": f"channel {direct_session_id}",
            }

        if is_group:
            group_id = str(getattr(msg, "group_id", "") or "")
            user_id = str(getattr(msg, "user_id", "") or "")
            history_key = f"{group_id}_{user_id}" if group_id and user_id else group_id
            session_id = get_qq_session_id(group_id=group_id, group_user_id=user_id)
            return {
                "store": group_messages,
                "history_key": history_key,
                "session_id": session_id,
                "user_id": None,
                "group_id": group_id,
                "group_user_id": user_id,
                "label": f"群 {group_id} / 用户 {user_id}" if user_id else f"群 {group_id}",
            }

        user_id = str(getattr(msg, "user_id", "") or "")
        return {
            "store": user_messages,
            "history_key": user_id,
            "session_id": get_qq_session_id(user_id=user_id),
            "user_id": user_id,
            "group_id": None,
            "group_user_id": None,
            "label": f"私聊 {user_id}",
        }

    def copy_chat_messages(messages):
        return [dict(message) for message in messages or [] if isinstance(message, dict)]

    def load_current_qq_session_messages(info):
        session_id = str(info.get("session_id") or "")
        if info.get("direct") and session_id:
            server = info.get("server")
            if server and hasattr(server, "sessions"):
                session = server.sessions.get(session_id) or {}
                return copy_chat_messages(session.get("messages", []))
            try:
                from nbot.web.server import WebChatServer

                server = WebChatServer.get_instance()
                if server and hasattr(server, "sessions"):
                    session = server.sessions.get(session_id) or {}
                    return copy_chat_messages(session.get("messages", []))
            except Exception:
                return []

        return load_canonical_qq_messages(
            user_id=info.get("user_id"),
            group_id=info.get("group_id"),
            group_user_id=info.get("group_user_id"),
        )

    def replace_current_qq_session_messages(info, messages, system_prompt=None, character=None):
        store = info.get("store")
        history_key = info.get("history_key")
        if store is not None and history_key:
            store[history_key] = copy_chat_messages(messages)
            save_qq_histories()

        session_id = str(info.get("session_id") or "")
        if session_id and info.get("direct"):
            _replace_direct_session_messages(
                session_id,
                messages,
                system_prompt,
                character,
                server=info.get("server"),
            )
            return

        # 飞书/Web 直接 session_id（非 qq_ 前缀），直接更新 session store
        if session_id and not session_id.startswith("qq_"):
            _replace_direct_session_messages(session_id, messages, system_prompt, character)
            return

        _replace_canonical_qq_session_messages(
            user_id=info.get("user_id"),
            group_id=info.get("group_id"),
            group_user_id=info.get("group_user_id"),
            messages=messages,
            system_prompt=system_prompt,
            character=character,
        )

    def delete_current_session_workspace(info):
        if not workspace_available:
            return
        try:
            if info.get("direct") and info.get("session_id"):
                from nbot.core.workspace import workspace_manager

                workspace_manager.delete_workspace(str(info["session_id"]))
                return

            delete_session_workspace(
                user_id=info.get("user_id"),
                group_id=info.get("group_id"),
                group_user_id=info.get("group_user_id"),
            )
        except Exception as exc:
            log.debug(f"Delete current session workspace skipped: {exc}")

    def session_system_prompt(session, messages):
        prompt = str((session or {}).get("system_prompt") or "")
        if prompt:
            return prompt
        for message in messages or []:
            if isinstance(message, dict) and message.get("role") == "system":
                return str(message.get("content") or "")
        return ""

    def session_matches_character(session, active):
        active_ids = {
            str((active or {}).get("id") or "").strip().casefold(),
            str((active or {}).get("name") or "").strip().casefold(),
        }
        active_ids.discard("")
        if not active_ids:
            return True
        session_ids = {
            str((session or {}).get("character_id") or "").strip().casefold(),
            str((session or {}).get("sender_name") or "").strip().casefold(),
        }
        session_ids.discard("")
        return bool(active_ids & session_ids)

    def load_web_sessions():
        from nbot.web.sessions_db import load_sessions as load_sessions_from_db

        sessions = load_sessions_from_db(web_data_dir())
        return sessions if isinstance(sessions, dict) else {}

    def save_web_sessions(sessions):
        from nbot.web.sessions_db import save_sessions as save_sessions_to_db

        save_sessions_to_db(web_data_dir(), sessions)

    def mcp_config_path():
        return os.path.join(web_data_dir(), "mcp_servers.json")

    def load_mcp_servers():
        path = mcp_config_path()
        try:
            if not os.path.exists(path):
                return []
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, list) else []
        except Exception as e:
            log.warning(f"Load MCP servers failed: {e}")
            return []

    def save_mcp_servers(configs):
        path = mcp_config_path()
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(configs, f, ensure_ascii=False, indent=2)

    def create_web_agent_session(owner_user_id="", name="Agent 对话"):
        session_id = str(uuid.uuid4())
        now = datetime.now().isoformat()
        session = {
            "id": session_id,
            "name": name,
            "type": "web",
            "user_id": str(owner_user_id or ""),
            "created_at": now,
            "updated_at": now,
            "archived": False,
            "archived_at": None,
            "messages": [{"role": "system", "content": ""}],
            "system_prompt": "",
            "character_id": "",
            "sender_name": "Agent",
            "sender_avatar": "",
            "sender_portrait": "",
            "tags": [],
            "favorite": False,
            "message_favorites": [],
            "pinned": False,
            "is_public": False,
            "proactive_chat": {},
            "character_runtime_timeline": [],
            "session_mode": "agent",
            "last_message": "",
        }

        try:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            if server and hasattr(server, "sessions"):
                from nbot.core.session_store import WebSessionStore

                save_callback = getattr(server, "_save_data", lambda _name: None)
                session_store = WebSessionStore(
                    server.sessions,
                    save_callback=lambda: save_callback("sessions"),
                )
                session_store.set_session(session_id, session)
                if workspace_available:
                    try:
                        from nbot.core.workspace import workspace_manager

                        workspace_manager.get_or_create(session_id, "web", name)
                    except Exception as exc:
                        log.debug(f"Create agent workspace skipped: {exc}")
                return session
        except Exception as e:
            log.warning(f"Create agent session in runtime store failed: {e}")

        sessions = load_web_sessions()
        sessions[session_id] = session
        save_web_sessions(sessions)
        if workspace_available:
            try:
                from nbot.core.workspace import workspace_manager

                workspace_manager.get_or_create(session_id, "web", name)
            except Exception as exc:
                log.debug(f"Create agent workspace skipped: {exc}")
        return session

    def set_current_session_prompt(msg, is_group, prompt):
        # 飞书/Web 适配器：使用 channel 作用域
        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        if direct_session_id:
            _replace_direct_session_messages(
                direct_session_id,
                [{"role": "system", "content": prompt}],
                system_prompt=prompt,
                server=getattr(msg, "server", None),
            )
            try:
                from nbot.core.prompt import prompt_manager

                prompt_manager._prompt_cache = {}
            except Exception:
                pass
            return

        id_str = str(msg.group_id if is_group else msg.user_id)
        prefix = "group" if is_group else "user"
        os.makedirs(f"resources/prompts/{prefix}", exist_ok=True)
        with open(f"resources/prompts/{prefix}/{prefix}_{id_str}.txt", "w", encoding="utf-8") as file:
            file.write(prompt)

        try:
            from nbot.core.prompt import prompt_manager

            prompt_manager._prompt_cache = {}
        except Exception:
            pass

    def update_current_qq_session_metadata(info, **fields):
        session_id = str(info.get("session_id") or "")
        if not session_id:
            return
        try:
            from nbot.core.session_store import WebSessionStore

            server = info.get("server") if info.get("direct") else None
            if server is None:
                from nbot.web.server import WebChatServer

                server = WebChatServer.get_instance()
            if not server or not hasattr(server, "sessions"):
                return
            save_callback = getattr(server, "_save_data", lambda _name: None)
            session_store = WebSessionStore(
                server.sessions,
                save_callback=lambda: save_callback("sessions"),
            )
            session = session_store.get_session(session_id)
            if not session:
                return
            session.update(fields)
            session["updated_at"] = datetime.now().isoformat()
            session_store.set_session(session_id, session)
        except Exception as e:
            log.debug(f"Update QQ session metadata skipped: {e}")

    def find_mcp_server(configs, needle):
        needle = str(needle or "").strip()
        if not needle:
            return None

        lowered = needle.casefold()
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            server_id = str(cfg.get("id") or "")
            name = str(cfg.get("name") or "")
            if lowered in {server_id.casefold(), name.casefold()}:
                return cfg

        partial_matches = []
        for cfg in configs:
            if not isinstance(cfg, dict):
                continue
            server_id = str(cfg.get("id") or "")
            name = str(cfg.get("name") or "")
            if lowered in server_id.casefold() or lowered in name.casefold():
                partial_matches.append(cfg)
        return partial_matches[0] if len(partial_matches) == 1 else None

    def format_mcp_server_summary(cfg, connected=None, tool_count=None):
        server_id = str(cfg.get("id") or "")
        name = str(cfg.get("name") or server_id[:8] or "未命名")
        transport = str(cfg.get("transport") or "streamable-http")
        target = cfg.get("url") or cfg.get("command") or ""
        enabled = "on" if cfg.get("enabled", True) else "off"
        if connected is None:
            connected = bool(cfg.get("connected"))
        if tool_count is None:
            tool_count = int(cfg.get("tool_count") or 0)
        state = "已连接" if connected else "未连接"
        suffix = f" -> {target}" if target else ""
        return f"- {name} [{server_id[:8]}] {transport} {state} 工具:{tool_count} enabled:{enabled}{suffix}"

    def refresh_mcp_runtime_status(configs):
        try:
            from nbot.services.mcp_bridge import get_mcp_bridge

            bridge = get_mcp_bridge()
            for cfg in configs:
                if not isinstance(cfg, dict):
                    continue
                server_id = str(cfg.get("id") or "")
                connected = bridge.is_connected(server_id)
                cfg["connected"] = connected
                cfg["tool_count"] = len(bridge.get_server_tools(server_id)) if connected else 0
        except Exception as e:
            log.warning(f"Refresh MCP runtime status failed: {e}")
        return configs

    def build_quick_mcp_entry(url):
        host_part = url.split("://", 1)[-1].split("/", 1)[0] or "server"
        safe_host = "".join(ch if ch.isalnum() else "-" for ch in host_part).strip("-") or "server"
        return {
            "id": str(uuid.uuid4()),
            "name": f"quick-{safe_host}",
            "transport": "streamable-http",
            "description": "通过聊天命令快速添加",
            "enabled": True,
            "auto_connect": False,
            "connected": False,
            "tool_count": 0,
            "created_at": datetime.now().isoformat(),
            "last_connected_at": None,
            "url": url,
        }

    def current_character(msg, is_group):
        characters = load_character_options()
        scope, target_id = resolve_character_session(msg, is_group)
        return find_session_character(characters, scope, target_id, msg)

    def candidate_resume_sessions(msg, is_group):
        active = current_character(msg, is_group)
        sessions = load_web_sessions()
        candidates = []
        for session_id, session in sessions.items():
            if not isinstance(session, dict):
                continue
            session_type = str(session.get("type") or "web").lower()
            if session_type.startswith("qq_") or session_type == "cli":
                continue
            if session.get("archived"):
                continue
            if not session_matches_character(session, active):
                continue
            messages = session.get("messages") if isinstance(session.get("messages"), list) else []
            candidates.append((session_id, session, len(messages)))

        candidates.sort(
            key=lambda item: str(item[1].get("updated_at") or item[1].get("created_at") or ""),
            reverse=True,
        )
        return candidates

    def find_resume_session(candidates, arg):
        arg = (arg or "").strip()
        if not arg:
            return None
        try:
            index = int(arg)
            if 1 <= index <= len(candidates):
                return candidates[index - 1]
        except ValueError:
            pass

        needle = arg.casefold()
        matches = []
        for item in candidates:
            session_id, session, _ = item
            name = str(session.get("name") or "")
            if needle == str(session_id).casefold() or needle == name.casefold():
                return item
            if needle in str(session_id).casefold() or needle in name.casefold():
                matches.append(item)
        return matches[0] if len(matches) == 1 else None

    def format_resume_session_list(candidates, active):
        active_name = (active or {}).get("name") or (active or {}).get("id") or "当前角色"
        lines = [f"可载入的 Web 会话（角色：{active_name}）："]
        if not candidates:
            lines.append("没有找到匹配当前角色的 Web 会话。")
            return "\n".join(lines)
        for index, (session_id, session, message_count) in enumerate(candidates[:20], 1):
            name = session.get("name") or session_id[:8]
            updated = session.get("updated_at") or session.get("created_at") or ""
            suffix = f" - {updated[:16]}" if updated else ""
            lines.append(f"{index}. {name} [{session_id[:8]}] {message_count} 条{suffix}")
        lines.append("")
        lines.append("用法：/resume <编号|会话ID|名称>")
        return "\n".join(lines)

    def extract_history_text_item(item):
        def parts_from_segments(segments):
            parts = []
            for seg in segments or []:
                seg_type = getattr(seg, "type", None)
                data = getattr(seg, "data", None)
                if isinstance(seg, dict):
                    seg_type = seg.get("type")
                    data = seg.get("data")
                data = data or {}
                if seg_type == "text":
                    parts.append(str(data.get("text") or ""))
                elif seg_type == "image":
                    parts.append("[图片]")
                elif seg_type:
                    parts.append(f"[{seg_type}]")
            return "".join(parts).strip()

        if isinstance(item, dict):
            message = item.get("message")
            if isinstance(message, list):
                text = parts_from_segments(message)
                if text:
                    return text
            return str(item.get("raw_message") or item.get("message") or item.get("content") or "").strip()

        message = getattr(item, "message", None)
        if isinstance(message, list):
            text = parts_from_segments(message)
            if text:
                return text
        return str(
            getattr(item, "raw_message", None)
            or getattr(item, "content", None)
            or ""
        ).strip()

    async def get_group_today_summary_text(group_id):
        try:
            items = await get_group_history_items(group_id, 500, bot)
        except Exception as e:
            log.error(f"获取群聊历史失败：{e}")
            return None
        if not items:
            return "今天群里还没有记录到消息喔~"

        today = datetime.now().date()
        filtered = []
        for item in items:
            t = item.get("time") if isinstance(item, dict) else getattr(item, "time", None)
            try:
                dt = datetime.fromtimestamp(int(t)) if t is not None else None
            except Exception:
                dt = None
            if dt is not None and dt.date() == today:
                filtered.append(item)
        if not filtered:
            return "今天群里还没有记录到消息喔~"
        return summarize_group_text(_history_items_to_text(filtered))


    @register_command(
        "/summary_recent",
        "/sr",
        help_text="/summary_recent [数量] 或 /sr [数量] -> 总结最近若干条群聊消息",
        category="2",
    )
    async def handle_summary_recent(msg, is_group=True):
        if not is_group:
            await reply_current_channel(msg, is_group, "请在群聊中使用该命令喔~")
            return

        raw = getattr(msg, "raw_message", "") or ""
        prefix = "/summary_recent" if raw.startswith("/summary_recent") else "/sr"
        arg = raw[len(prefix):].strip()
        try:
            count = int(arg) if arg else 100
        except ValueError:
            await msg.reply(text="格式错误喔，请输入 /summary_recent [数量] 或 /sr [数量]")
            return
        count = max(1, min(count, 500))

        try:
            items = await get_group_history_items(msg.group_id, count, bot)
        except Exception as e:
            await msg.reply(text=f"获取群聊历史失败喔：{e}")
            return
        if not items:
            await msg.reply(text="没有获取到群聊历史消息喔~")
            return
        await msg.reply(text=summarize_group_text(_history_items_to_text(items)))

    @register_command(
        "/summary_today",
        help_text="/summary_today -> 总结今天与机器人的聊天内容",
        category="2",
    )
    async def handle_summary_today(msg, is_group=True):
        if is_group:
            summary = await get_group_today_summary_text(msg.group_id)
            if summary:
                await msg.reply(text=summary)
        else:
            summary = generate_today_summary(user_id=msg.user_id)
            await reply_current_channel(msg, is_group, summary)

    @register_command(
        "/summary_auto",
        help_text="/summary_auto -> 开启或关闭每日自动总结群聊记录(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_summary_auto(msg, is_group=True):
        if not is_group:
            await reply_current_channel(msg, is_group, "请在群聊中使用该命令喔~")
            return
        if str(msg.user_id) not in admin:
            await msg.reply(text="你没有权限开启自动总结喔~")
            return

        state = switch.toggle_switch("summary_auto", group_id=str(msg.group_id))
        text = "已开启每日自动总结喔（将在每天23:55发送）" if state else "已关闭每日自动总结喔"
        await msg.reply(text=text)
        switch.save_switches()

    @register_command(
        "/compact",
        help_text="/compact -> 压缩当前会话上下文（早期消息总结为摘要，保留最近若干条）(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_compact_context(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        info = current_qq_session_info(msg, is_group)
        session_id = str(info.get("session_id") or "")
        if not session_id:
            await reply_current_channel(msg, is_group, "当前会话没有可用的 session_id，无法压缩。")
            return

        messages = load_current_qq_session_messages(info)
        if not messages:
            await reply_current_channel(msg, is_group, "当前会话没有消息，无需压缩。")
            return

        has_been_compressed = any(
            isinstance(m, dict)
            and m.get("role") == "system"
            and str(m.get("id", "")).startswith("summary_")
            for m in messages
        )
        min_messages = 5 if has_been_compressed else 10
        if len(messages) < min_messages:
            await reply_current_channel(
                msg,
                is_group,
                f"消息数量不足（{len(messages)}/{min_messages}），无需压缩。",
            )
            return

        system_msg = None
        if messages and isinstance(messages[0], dict) and messages[0].get("role") == "system":
            system_msg = messages[0]

        keep_count = min(3 if has_been_compressed else 5, len(messages) - 2)
        recent_messages = messages[-keep_count:] if messages else []

        compress_start = 1 if system_msg else 0
        compress_end = len(messages) - keep_count
        if compress_end <= compress_start:
            await reply_current_channel(msg, is_group, "没有足够的早期消息需要压缩。")
            return

        messages_to_compress = [
            m for m in messages[compress_start:compress_end]
            if isinstance(m, dict)
            and not (m.get("role") == "system" and str(m.get("id", "")).startswith("summary_"))
        ]
        if not messages_to_compress:
            await reply_current_channel(msg, is_group, "没有消息需要压缩。")
            return

        conversation_text = "\n".join(
            f"[{m.get('role', 'user')}]: {str(m.get('content', ''))[:500]}"
            for m in messages_to_compress
            if m.get("content")
        )

        if has_been_compressed:
            summary_prompt = (
                '以下内容包含上一轮的对话摘要和后续新的对话，请将它们融合成一份新的简洁总结。'
                '旧摘要中以"【对话总结】"开头的内容是之前对话的要点，'
                '请吸收保留其中仍然重要的信息，与新对话内容整合，不要丢失关键上下文：\n\n'
                f"{conversation_text}\n\n请融合以上所有信息，用80-150字总结："
            )
        else:
            summary_prompt = (
                "请简洁地总结以下对话的主要内容，保留关键信息和结论：\n\n"
                f"{conversation_text}\n\n请用50-100字总结："
            )

        try:
            from nbot.services.ai import ai_client

            if not ai_client or not getattr(ai_client, "base_url", ""):
                await reply_current_channel(msg, is_group, "AI 服务未配置，无法压缩。")
                return

            await reply_current_channel(
                msg,
                is_group,
                f"正在压缩 {len(messages_to_compress)} 条早期消息...",
            )

            response = ai_client.chat_completion(
                model=None,
                messages=[{"role": "user", "content": summary_prompt}],
                stream=False,
            )

            try:
                from nbot.core.token_stats import (
                    PURPOSE_UTILITY,
                    get_token_stats_manager,
                )

                usage = getattr(response, "usage", None)
                if usage:
                    get_token_stats_manager().record_usage(
                        prompt_tokens=getattr(usage, "prompt_tokens", 0) or 0,
                        completion_tokens=getattr(usage, "completion_tokens", 0) or 0,
                        total_tokens=getattr(usage, "total_tokens", 0) or 0,
                        model=getattr(ai_client, "model", "") or "",
                        session_id=session_id,
                        channel_type="command",
                        source="command",
                        purpose=PURPOSE_UTILITY,
                    )
            except Exception as stats_err:
                log.debug(f"[Compact] 记录 token 用量失败: {stats_err}")

            import re
            import time as _time

            summary = str(response.choices[0].message.content or "").strip()
            summary = re.sub(r"【对话总结】\s*", "", summary).strip()
            summary = re.sub(r"\*\*【对话总结】\*\*\s*", "", summary).strip()

            new_messages = [system_msg] if system_msg else []
            new_messages.append({
                "id": f"summary_{int(_time.time())}",
                "role": "system",
                "content": f"【对话总结】{summary}",
                "timestamp": _time.time(),
            })
            new_messages.extend(recent_messages)

            cur_system_prompt = session_system_prompt(None, [system_msg] if system_msg else [])
            replace_current_qq_session_messages(
                info,
                new_messages,
                system_prompt=cur_system_prompt,
            )

            await reply_current_channel(
                msg,
                is_group,
                f"上下文压缩完成：已将 {len(messages_to_compress)} 条早期消息总结为摘要，"
                f"保留最近 {keep_count} 条消息。\n摘要预览：{summary}",
            )
        except Exception as e:
            log.error(f"Compact context failed: {e}", exc_info=True)
            await reply_current_channel(msg, is_group, f"压缩失败：{e}")

    def _mutate_current_session_field(msg, is_group, mutator) -> tuple[bool, str]:
        """修改当前会话的字段（agent_goal/agent_spec 等）并持久化。"""
        info = current_qq_session_info(msg, is_group)
        session_id = str(info.get("session_id") or "")
        if not session_id:
            return False, "当前会话没有可用的 session_id"
        try:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            session = None
            if server and hasattr(server, "sessions"):
                session = server.sessions.get(session_id)
            if session is None and server and hasattr(server, "session_store"):
                session = server.session_store.get_session(session_id)
            if session is None:
                return False, "当前会话不存在或尚未初始化"
            mutator(session)
            if hasattr(server, "session_store") and server.session_store:
                server.session_store.set_session(session_id, session)
            elif hasattr(server, "_save_sessions"):
                server._save_sessions()
            return True, session_id
        except Exception as e:
            log.error(f"Mutate session field failed: {e}", exc_info=True)
            return False, str(e)

    @register_command(
        "/goal",
        help_text="/goal <目标> -> 为会话设置持久目标，每轮注入提示词；/goal 查看；/goal clear 清除",
        category="2",
    )
    async def handle_goal_command(msg, is_group=True):
        info = current_qq_session_info(msg, is_group)
        session_id = str(info.get("session_id") or "")

        def _mutate(session):
            if not goal_text or goal_text.lower() == "clear":
                session.pop("agent_goal", None)
            else:
                session["agent_goal"] = goal_text

        raw = str(getattr(msg, "message", "") or getattr(msg, "content", "") or "").strip()
        # 兼容不同的命令参数传递方式：取 /goal 之后的全部文本
        goal_text = raw[len("/goal"):].strip() if raw.lower().startswith("/goal") else raw.strip()

        if not session_id:
            await reply_current_channel(msg, is_group, "当前会话没有可用的 session_id，无法设置目标。")
            return

        if not goal_text:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            session = (server.sessions.get(session_id) or {}) if server else {}
            current_goal = str(session.get("agent_goal") or "")
            if current_goal:
                await reply_current_channel(msg, is_group, f"当前会话目标：\n{current_goal}")
            else:
                await reply_current_channel(msg, is_group, "当前会话未设置目标。用法：/goal <目标描述>，/goal clear 清除。")
            return

        ok, result = _mutate_current_session_field(msg, is_group, _mutate)
        if not ok:
            await reply_current_channel(msg, is_group, f"设置目标失败：{result}")
            return
        if goal_text.lower() == "clear":
            await reply_current_channel(msg, is_group, "已清除会话目标。")
        else:
            await reply_current_channel(msg, is_group, f"已设置会话目标（每轮自动注入）：\n{goal_text}")

    @register_command(
        "/spec",
        help_text="/spec <规格> -> 为会话设置规格说明（Agent 规格驱动），每轮注入；/spec 查看；/spec clear 清除",
        category="2",
    )
    async def handle_spec_command(msg, is_group=True):
        info = current_qq_session_info(msg, is_group)
        session_id = str(info.get("session_id") or "")

        def _mutate(session):
            if not spec_text or spec_text.lower() == "clear":
                session.pop("agent_spec", None)
            else:
                session["agent_spec"] = spec_text

        raw = str(getattr(msg, "message", "") or getattr(msg, "content", "") or "").strip()
        spec_text = raw[len("/spec"):].strip() if raw.lower().startswith("/spec") else raw.strip()

        if not session_id:
            await reply_current_channel(msg, is_group, "当前会话没有可用的 session_id，无法设置规格。")
            return

        if not spec_text:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            session = (server.sessions.get(session_id) or {}) if server else {}
            current_spec = str(session.get("agent_spec") or "")
            if current_spec:
                await reply_current_channel(msg, is_group, f"当前会话规格：\n{current_spec[:1500]}")
            else:
                await reply_current_channel(msg, is_group, "当前会话未设置规格。用法：/spec <规格说明>，/spec clear 清除。")
            return

        ok, result = _mutate_current_session_field(msg, is_group, _mutate)
        if not ok:
            await reply_current_channel(msg, is_group, f"设置规格失败：{result}")
            return
        if spec_text.lower() == "clear":
            await reply_current_channel(msg, is_group, "已清除会话规格。")
        else:
            await reply_current_channel(msg, is_group, f"已设置会话规格（每轮自动注入）：\n{spec_text[:500]}")

    @register_command(
        "/context",
        help_text="/context -> 查看当前会话上下文使用情况",
        category="2",
    )
    async def handle_context_info(msg, is_group=True):
        info = current_qq_session_info(msg, is_group)
        session_id = str(info.get("session_id") or "")
        if not session_id:
            await reply_current_channel(msg, is_group, "当前会话没有可用的 session_id。")
            return

        messages = load_current_qq_session_messages(info)
        if not messages:
            await reply_current_channel(msg, is_group, "当前会话没有消息记录。")
            return

        total_messages = len(messages)
        system_messages = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "system"
        )
        summary_messages = sum(
            1 for m in messages
            if isinstance(m, dict)
            and m.get("role") == "system"
            and str(m.get("id", "")).startswith("summary_")
        )
        user_count = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "user"
        )
        assistant_count = sum(
            1 for m in messages if isinstance(m, dict) and m.get("role") == "assistant"
        )

        total_chars = sum(len(str(m.get("content", ""))) for m in messages if isinstance(m, dict))
        estimated_tokens = int(total_chars / 2.5)

        max_context = 0
        model_name = ""
        try:
            from nbot.services.ai import _load_shared_web_ai_config

            config = _load_shared_web_ai_config() or {}
            max_context = int(config.get("max_context_length", 0) or 0)
            model_name = str(config.get("model", "") or "")
        except Exception:
            pass

        lines = [
            f"会话：{info.get('label', session_id[:8])}",
            f"消息总数：{total_messages} "
            f"(user:{user_count} assistant:{assistant_count} system:{system_messages})",
        ]
        if summary_messages > 0:
            lines.append(f"已含摘要：{summary_messages} 条")
        lines.append(f"估算 tokens：约 {estimated_tokens:,}")
        if model_name:
            lines.append(f"当前模型：{model_name}")
        if max_context > 0:
            usage_percent = (estimated_tokens / max_context) * 100
            lines.append(f"模型上限：{max_context:,} tokens")
            lines.append(f"使用率：{usage_percent:.1f}%")
            bar_len = 20
            filled = int(bar_len * min(usage_percent, 100.0) / 100.0)
            bar = "█" * filled + "░" * (bar_len - filled)
            lines.append(f"[{bar}]")
        else:
            lines.append("模型上限：未配置")

        last_msg = None
        for m in reversed(messages):
            if isinstance(m, dict) and m.get("role") != "system" and m.get("content"):
                last_msg = m
                break
        if last_msg:
            preview = str(last_msg.get("content", ""))[:50].replace("\n", " ")
            lines.append(f"最近消息：[{last_msg.get('role')}] {preview}...")

        await reply_current_channel(msg, is_group, "\n".join(lines))

    @register_command(
        "/heartbeat",
        help_text="/heartbeat -> 查看当前会话心跳\n/heartbeat on [分钟] -> 开启当前会话心跳\n/heartbeat off -> 关闭当前会话心跳\n/heartbeat run -> 立即执行一次",
        category="2",
    )
    async def handle_session_heartbeat(msg, is_group=True):
        from nbot.gateway.heartbeat import get_session_heartbeat_manager

        manager = get_session_heartbeat_manager()
        if not manager:
            await reply_current_channel(msg, is_group, "当前未启用统一心跳管理器。")
            return

        session_id = resolve_current_session_id(msg, is_group)
        if not session_id:
            await reply_current_channel(msg, is_group, "当前会话没有可用的 session_id。")
            return

        raw = (getattr(msg, "raw_message", "") or "")[len("/heartbeat"):].strip()
        parts = raw.split() if raw else []

        if not parts:
            config = manager.get_config(session_id)
            state = "开启" if config.get("enabled") else "关闭"
            await reply_current_channel(
                msg,
                is_group,
                f"当前会话心跳：{state}\n间隔：{config.get('interval_minutes', 60)} 分钟\n内容文件：{config.get('content_file', 'heartbeat.md')}",
            )
            return

        action = parts[0].lower()
        if action == "run":
            await manager.execute_session(
                session_id,
                force=True,
                trigger_source="command",
            )
            await reply_current_channel(msg, is_group, "已立即执行当前会话的 heartbeat。")
            return

        current = manager.get_config(session_id)
        update = {
            "target_session_id": session_id,
            "content_file": current.get("content_file", "heartbeat.md"),
            "interval_minutes": current.get("interval_minutes", 60),
        }

        if action in ("off", "0", "false"):
            update["enabled"] = False
        elif action in ("on", "1", "true"):
            update["enabled"] = True
            if len(parts) > 1:
                update["interval_minutes"] = max(1, int(float(parts[1])))
        else:
            update["enabled"] = True
            update["interval_minutes"] = max(1, int(float(action)))

        manager.set_config(session_id, update)
        try:
            from nbot.web.server import WebChatServer

            server = WebChatServer.get_instance()
            if server:
                server._refresh_heartbeat_summary_config()
                if manager.any_enabled():
                    server._start_heartbeat_job(server.heartbeat_config.get("interval_minutes", 60))
                else:
                    server._stop_heartbeat_job()
                server._save_data("heartbeat")
        except Exception:
            pass

        config = manager.get_config(session_id)
        state = "开启" if config.get("enabled") else "关闭"
        await reply_current_channel(
            msg,
            is_group,
            f"已更新当前会话 heartbeat：{state}，间隔 {config.get('interval_minutes', 60)} 分钟。",
        )

    @register_command(
        "/auto_reply",
        help_text="/auto_reply [on|off|话痨程度0-1] -> 开启/关闭或设置群聊智能自动回复(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_auto_reply(msg, is_group=True):
        if not is_group:
            await reply_current_channel(msg, is_group, "请在群聊中使用该命令喔~")
            return
        if str(msg.user_id) not in admin:
            await msg.reply(text="你没有权限开启智能自动回复喔~")
            return
        group_id_str = str(msg.group_id)
        raw = (getattr(msg, "raw_message", "") or "")[len("/auto_reply"):].strip()
        level = None

        if not raw:
            state = switch.toggle_switch("auto_reply", group_id=group_id_str)
        else:
            parts = raw.split()
            action = parts[0].lower()
            desired_state = True
            level_text = parts[0]

            if action in {"on", "enable", "start", "开启", "打开"}:
                desired_state = True
                level_text = parts[1] if len(parts) > 1 else None
            elif action in {"off", "disable", "stop", "关闭", "关掉"}:
                desired_state = False
                level_text = parts[1] if len(parts) > 1 else None

            if level_text:
                try:
                    level = max(0.0, min(float(level_text), 1.0))
                except ValueError:
                    await msg.reply(text="格式错误喔，请输入 on/off，或 0~1 之间的小数，例如 0.3 或 0.8")
                    return
                switch.group_switches.setdefault(group_id_str, {})["auto_reply_level"] = level

            switch.set_switch_state("auto_reply", desired_state, group_id=group_id_str)
            state = desired_state

        if level is None:
            try:
                level = float(switch.group_switches.get(group_id_str, {}).get("auto_reply_level", 0.5))
            except Exception:
                level = 0.5
        text = ("已开启群聊智能自动回复喔~" if state else "已关闭群聊智能自动回复喔~") + f" 当前话痨程度：{level:.2f}"
        await msg.reply(text=text)
        switch.save_switches()

    @register_command(
        "/主动聊天",
        help_text="/主动聊天 [是否开启(1/0)] -> 开启/关闭主动聊天（AI将自行决定聊天频率）",
        category="2",
    )
    async def handle_active_chat(msg, is_group=True):
        await reply_current_channel(
            msg,
            is_group,
            "旧的 /主动聊天 已停用。请改用 /heartbeat 管理当前会话的心跳。",
        )
        return

    @register_command(
        "/show_chat",
        "/sc",
        help_text="/show_chat 或 /sc -> 发送完整聊天记录(仅群admin)",
        category="2",
    )
    async def handle_show_chat(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限发送聊天记录喔~")
            return

        cache_dir = normalize_file_path(os.path.join(load_address(), "聊天记录.txt"))
        is_web = hasattr(msg, "send_file") and not hasattr(msg, "message_type")

        if is_group and not is_web:
            try:
                text = str(load_current_qq_session_messages(current_qq_session_info(msg, True)))
            except KeyError:
                text = "该群没有聊天记录喔~"
            with open(cache_dir, "w", encoding="utf-8") as f:
                f.write(text)
            _api = getattr(_bot_instance, "api", None)
            if _api and hasattr(_api, "post_group_file"):
                await _api.post_group_file(msg.group_id, file=cache_dir)
            else:
                await msg.reply(text=text[:2000] if len(text) > 2000 else text)
        else:
            try:
                text = str(load_current_qq_session_messages(current_qq_session_info(msg, False)))
            except KeyError:
                text = "你没有聊天记录喔~"
            with open(cache_dir, "w", encoding="utf-8") as f:
                f.write(text)
            if is_web and hasattr(msg, "send_file"):
                await msg.send_file(cache_dir, "聊天记录.txt")
            elif hasattr(msg, "send_file"):
                await msg.send_file(cache_dir, "聊天记录.txt")
            else:
                _api = getattr(_bot_instance, "api", None)
                if _api and hasattr(_api, "upload_private_file"):
                    await _api.upload_private_file(msg.user_id, file=cache_dir, name="聊天记录.txt")
                else:
                    await msg.reply(text=text[:2000] if len(text) > 2000 else text)

        try:
            os.remove(cache_dir)
        except OSError:
            pass

    @register_command(
        "/set_prompt",
        "/sp",
        help_text="/set_prompt 或 /sp <提示词> -> 设定当前会话提示词(群聊仅admin)",
        category="2",
    )
    async def handle_set_prompt(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        raw = getattr(msg, "raw_message", "") or ""
        if raw.startswith("/set_prompt"):
            prompt_content = raw[len("/set_prompt"):].strip()
        else:
            prompt_content = raw[len("/sp"):].strip()

        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        if direct_session_id:
            _replace_direct_session_messages(
                direct_session_id,
                [{"role": "system", "content": prompt_content}],
                system_prompt=prompt_content,
                server=getattr(msg, "server", None),
            )
            reply_text = (
                "群组提示词已更新，对话记录已清除喔~"
                if is_group
                else "个人提示词已更新，对话记录已清除喔~"
            )
            await reply_current_channel(msg, is_group, reply_text)
            return

        id_str = str(msg.group_id if is_group else msg.user_id)
        prefix = "group" if is_group else "user"
        os.makedirs(f"resources/prompts/{prefix}", exist_ok=True)
        with open(f"resources/prompts/{prefix}/{prefix}_{id_str}.txt", "w", encoding="utf-8") as file:
            file.write(prompt_content)

        messages = group_messages if is_group else user_messages
        messages[id_str] = [{"role": "system", "content": prompt_content}]
        save_qq_histories()
        _replace_canonical_qq_session_messages(
            group_id=id_str if is_group else None,
            user_id=id_str if not is_group else None,
            messages=[{"role": "system", "content": prompt_content}],
            system_prompt=prompt_content,
        )

        reply_text = "群组提示词已更新，对话记录已清除喔~" if is_group else "个人提示词已更新，对话记录已清除喔~"
        await reply_current_channel(msg, is_group, reply_text)

    @register_command(
        "/del_prompt",
        "/dp",
        help_text="/del_prompt 或 /dp -> 删除当前会话提示词(群聊仅admin)",
        category="2",
    )
    async def handle_del_prompt(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        if direct_session_id:
            try:
                from nbot.core.prompt import prompt_manager

                prompt = prompt_manager.load_base_prompt()
            except Exception as e:
                log.warning(f"Reload base prompt failed: {e}")
                prompt = ""
            _replace_direct_session_messages(
                direct_session_id,
                [{"role": "system", "content": prompt}],
                system_prompt=prompt,
                server=getattr(msg, "server", None),
            )
            await reply_current_channel(msg, is_group, "提示词已删除喔~")
            return

        id_str = str(msg.group_id if is_group else msg.user_id)
        prefix = "group" if is_group else "user"
        messages = group_messages if is_group else user_messages
        prompt_path = f"resources/prompts/{prefix}/{prefix}_{id_str}.txt"

        if not direct_session_id:
            messages.pop(id_str, None)
        try:
            os.remove(prompt_path)
        except FileNotFoundError:
            await reply_current_channel(msg, is_group, "没有可以删除的提示词喔~")
            return

        try:
            from nbot.core.prompt import prompt_manager

            if direct_session_id:
                prompt = prompt_manager.load_base_prompt()
                _replace_direct_session_messages(
                    direct_session_id,
                    [{"role": "system", "content": prompt}],
                    system_prompt=prompt,
                )
            else:
                kwargs = {"group_id": id_str} if is_group else {"user_id": id_str}
                prompt = prompt_manager.load_base_prompt(**kwargs)
                messages[id_str] = [{"role": "system", "content": prompt}]
                save_qq_histories()
                _replace_canonical_qq_session_messages(
                    group_id=id_str if is_group else None,
                    user_id=id_str if not is_group else None,
                    messages=[{"role": "system", "content": prompt}],
                    system_prompt=prompt,
            )
        except Exception as e:
            log.warning(f"Reload base prompt failed: {e}")

        await reply_current_channel(msg, is_group, "提示词已删除喔~")

    @register_command(
        "/get_prompt",
        "/gp",
        help_text="/get_prompt 或 /gp -> 获取当前会话提示词(群聊仅admin)",
        category="2",
    )
    async def handle_get_prompt(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        if direct_session_id:
            prompt = get_session_prompt("channel", direct_session_id, msg)
            if not prompt:
                prompt = "没有找到提示词喔~"
            await reply_current_channel(msg, is_group, prompt)
            return

        id_str = str(msg.group_id if is_group else msg.user_id)
        prefix = "group" if is_group else "user"
        try:
            with open(f"resources/prompts/{prefix}/{prefix}_{id_str}.txt", "r", encoding="utf-8") as file:
                prompt = file.read()
        except FileNotFoundError:
            prompt = "没有找到提示词喔~"
        await reply_current_channel(msg, is_group, prompt)

    @register_command(
        "/new",
        help_text="/new -> 创建新的对话会话 (清空当前对话历史)(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_new_session(msg, is_group=True):
        direct_session_id = str(getattr(msg, "session_id", "") or "").strip()
        direct_server = getattr(msg, "server", None)
        if direct_session_id and direct_server is not None:
            if not hasattr(direct_server, "sessions"):
                await msg.reply(text="当前平台不支持会话重置喔~")
                return

            from nbot.core.session_store import WebSessionStore

            save_callback = getattr(direct_server, "_save_data", lambda _name: None)
            session_store = WebSessionStore(
                direct_server.sessions,
                save_callback=lambda: save_callback("sessions"),
            )
            session = session_store.get_session(direct_session_id)
            if session:
                msg_count_before = len(session.get("messages", []))
                system_msg = next(
                    (m for m in session.get("messages", []) if m.get("role") == "system"),
                    None,
                )
                new_msgs = [system_msg] if system_msg else []
                session_store.replace_messages(direct_session_id, new_msgs)
                session["updated_at"] = datetime.now().isoformat()
                session["last_message"] = ""
                log.info(
                    "[ChannelChat] /new cleared session %s: %s -> %s messages",
                    direct_session_id,
                    msg_count_before,
                    len(new_msgs),
                )
            else:
                log.info("[ChannelChat] /new session not found: %s", direct_session_id)

            if workspace_available:
                try:
                    from nbot.core.workspace import workspace_manager

                    workspace_manager.delete_workspace(direct_session_id)
                except Exception as exc:
                    log.debug(f"Delete channel workspace skipped: {exc}")
            await msg.reply(text="已创建新会话喔，之前的对话历史已清空")
            return

        # 权限检查：仅管理员可使用
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        if is_group:
            group_id = getattr(msg, "group_id", None)
            if group_id:
                info = current_qq_session_info(msg, True)
                gid = str(group_id)
                history_key = str(info.get("history_key") or gid)
                group_messages.pop(history_key, None)
                if history_key != gid:
                    group_messages.pop(gid, None)
                save_qq_histories()
                _reset_canonical_qq_session_messages(
                    group_id=gid,
                    group_user_id=info.get("group_user_id"),
                )
                delete_session_workspace(group_id=gid, group_user_id=str(msg.user_id))
            else:
                await msg.reply(text="当前平台不支持会话重置喔~")
                return
            await msg.reply(text="已创建新会话喔，之前的对话历史已清空")
            return

        user_id = str(msg.user_id)
        user_messages.pop(user_id, None)
        save_qq_histories()
        _reset_canonical_qq_session_messages(user_id=user_id)
        delete_session_workspace(user_id=user_id)
        await reply_current_channel(msg, is_group, "已创建新会话喔，之前的对话历史已清空")

    @register_command(
        "/new_agent",
        help_text="/new_agent -> 像 Web 一样创建一个新的 Agent 对话并切换到当前频道(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_new_agent_session(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        info = current_qq_session_info(msg, is_group)
        agent_session = create_web_agent_session(owner_user_id=str(getattr(msg, "user_id", "") or ""))
        agent_messages = [{"role": "system", "content": ""}]

        replace_current_qq_session_messages(
            info,
            agent_messages,
            system_prompt="",
        )
        set_current_session_prompt(msg, is_group, "")
        update_current_qq_session_metadata(
            info,
            system_prompt="",
            character_id="",
            sender_name="Agent",
            sender_avatar="",
            sender_portrait="",
            session_mode="agent",
            last_message="",
        )

        bindings = load_resume_bindings()
        bindings[info["session_id"]] = {
            "web_session_id": agent_session["id"],
            "web_session_name": agent_session.get("name") or "",
            "loaded_at": datetime.now().isoformat(),
            "channel": info["label"],
            "character_id": "",
        }
        save_resume_bindings(bindings)

        delete_current_session_workspace(info)

        await reply_current_channel(
            msg,
            is_group,
            f"已创建新的 Agent 对话「{agent_session.get('name') or agent_session['id'][:8]}」，并切换到当前频道。",
        )

    @register_command(
        "/new_character",
        help_text="/new_character -> 退出 Agent 模式并恢复为新的角色对话(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_new_character_session(msg, is_group=True):
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        characters = load_character_options()
        session_scope, session_target_id = resolve_character_session(msg, is_group)
        target = find_session_character(characters, session_scope, session_target_id, msg)

        if not target:
            await reply_current_channel(msg, is_group, "没有找到可恢复的角色配置。请先在 Web 角色卡里设置当前角色。")
            return

        info = current_qq_session_info(msg, is_group)
        bindings = load_resume_bindings()
        if info["session_id"] in bindings:
            bindings.pop(info["session_id"], None)
            save_resume_bindings(bindings)

        try:
            set_current_session_character(msg, is_group, target)
            update_current_qq_session_metadata(
                info,
                session_mode="character",
                last_message="",
            )
        except Exception as e:
            log.error(f"Restore character session failed: {e}", exc_info=True)
            await reply_current_channel(msg, is_group, f"恢复角色对话失败：{e}")
            return

        name = target.get("name") or target.get("id") or "当前角色"
        await reply_current_channel(
            msg,
            is_group,
            f"已退出 Agent 模式，并恢复为新的角色对话：{name}",
        )

    @register_command(
        "/character",
        "/char",
        help_text="/character -> 查看/切换角色(admin)\n/character list -> 列出角色\n/character <编号|id|名称> -> 切换角色",
        category="2",
        admin_show=True,
    )
    async def handle_character_switch(msg, is_group=True):
        # 权限检查：仅管理员可使用
        if needs_legacy_group_admin(msg, is_group):
            await msg.reply(text="你没有权限喔~")
            return

        raw = (getattr(msg, "raw_message", "") or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        characters = load_character_options()
        session_scope, session_target_id = resolve_character_session(msg, is_group)
        active = find_session_character(characters, session_scope, session_target_id, msg)
        active_id = str(active.get("id") or active.get("name") or "")
        active_name = str(active.get("name") or active_id or "未设置")

        if not characters:
            await reply_current_channel(msg, is_group, "还没有可切换的角色。请先在 Web 角色卡里创建或导入角色。")
            return

        if not arg or arg.lower() == "list":
            lines = [f"当前角色：{active_name}", "", "可用角色："]
            for i, character in enumerate(characters, 1):
                name = character.get("name") or character.get("id") or "未命名"
                character_id = character.get("id") or name
                marker = "（当前）" if str(character_id) == active_id or str(name) == active_name else ""
                desc = str(character.get("description") or "").strip()
                suffix = f" - {desc[:40]}" if desc else ""
                lines.append(f"{i}. {name} [{character_id}]{marker}{suffix}")
            lines.append("")
            lines.append("用法：/character <编号|id|名称>")
            await reply_current_channel(msg, is_group, "\n".join(lines))
            return

        target = find_character(characters, arg)
        if not target:
            await reply_current_channel(msg, is_group, f"没有找到角色：{arg}\n发送 /character list 查看可用角色。")
            return

        try:
            set_current_session_character(msg, is_group, target)
        except Exception as e:
            log.error(f"Switch character failed: {e}", exc_info=True)
            await reply_current_channel(msg, is_group, f"切换角色失败：{e}")
            return

        name = target.get("name") or target.get("id") or arg
        await reply_current_channel(
            msg,
            is_group,
            f"已切换当前会话角色：{name}\n当前会话历史已清空，提示词已更新。",
        )

    @register_command(
        "/model",
        help_text="/model -> 查看当前模型(admin)\n/model <编号> -> 切换到指定模型\n/model list -> 列出所有可用模型",
        category="2",
        admin_show=True,
    )
    async def handle_model_switch(msg, is_group=True):
        # 权限检查：仅管理员可使用
        if str(msg.user_id) not in admin:
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        data_dir = os.path.join("data", "web")
        models_file = os.path.join(data_dir, "ai_models.json")

        if not os.path.exists(models_file):
            await reply_current_channel(msg, is_group, "暂无可用模型配置，请先在 Web 控制台添加模型喔~")
            return

        try:
            models_data, was_plaintext = read_secure_json(models_file, data_dir, {})
            if was_plaintext:
                write_secure_json(models_file, data_dir, models_data)
            if not isinstance(models_data, dict):
                models_data = {}
        except Exception:
            await reply_current_channel(msg, is_group, "读取模型配置失败喔~")
            return

        all_models = models_data.get("models", [])
        active_id = models_data.get("active_model_id")
        chat_models = [
            m for m in all_models
            if m.get("purpose", "chat") == "chat" and m.get("enabled", True)
        ]

        if not chat_models:
            await reply_current_channel(msg, is_group, "暂无启用的对话模型，请先在 Web 控制台启用一个对话模型喔~")
            return

        raw = getattr(msg, "raw_message", "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""

        if arg == "" or arg == "list":
            lines = ["可用对话模型"]
            for i, model in enumerate(chat_models, 1):
                marker = " (当前)" if model.get("id") == active_id else ""
                name = model.get("name", "未命名")
                model_name = model.get("model", "?")
                lines.append(f"{i}. {name} - `{model_name}`{marker}")
            lines.append("")
            lines.append("输入 /model <编号> 切换模型")
            await reply_current_channel(msg, is_group, "\n".join(lines))
            return

        try:
            idx = int(arg)
        except ValueError:
            idx = 0

        if 1 <= idx <= len(chat_models):
            target = chat_models[idx - 1]
            models_data["active_model_id"] = target.get("id")
            write_secure_json(models_file, data_dir, models_data)
            from nbot.services.ai import refresh_runtime_ai_config

            refresh_runtime_ai_config()
            await reply_current_channel(
                msg,
                is_group,
                f"已切换到模型: {target.get('name', '未命名')} (`{target.get('model', '?')}`)",
            )
            return

        current = next((m for m in chat_models if m.get("id") == active_id), None)
        if current:
            reply = (
                f"当前模型: {current.get('name', '未命名')}\n"
                f"模型: `{current.get('model', '?')}`\n"
                f"厂商: {current.get('provider', '?')}\n"
                f"上下文: {current.get('max_context_length', 100000):,} tokens\n\n"
                "输入 /model list 查看所有可用模型"
            )
        else:
            reply = "未找到当前模型信息喔~"
        await reply_current_channel(msg, is_group, reply)

    @register_command(
        "/resume",
        help_text="/resume [编号|会话ID|名称] -> 从当前角色的 Web 会话载入到当前频道(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_resume_session(msg, is_group=True):
        # 权限检查：仅管理员可使用
        if needs_legacy_admin(msg):
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        raw = (getattr(msg, "raw_message", "") or "").strip()
        parts = raw.split(maxsplit=1)
        arg = parts[1].strip() if len(parts) > 1 else ""
        active = current_character(msg, is_group)
        candidates = candidate_resume_sessions(msg, is_group)

        if not arg:
            await reply_current_channel(
                msg,
                is_group,
                format_resume_session_list(candidates, active),
            )
            return

        selected = find_resume_session(candidates, arg)
        if not selected:
            await reply_current_channel(
                msg,
                is_group,
                "没有找到匹配的 Web 会话。发送 /resume 查看当前角色可载入的会话。",
            )
            return

        web_session_id, web_session, _ = selected
        info = current_qq_session_info(msg, is_group)
        messages = copy_chat_messages(web_session.get("messages", []))
        system_prompt = session_system_prompt(web_session, messages)
        if system_prompt and not any(m.get("role") == "system" for m in messages):
            messages.insert(0, {"role": "system", "content": system_prompt})

        replace_current_qq_session_messages(
            info,
            messages,
            system_prompt=system_prompt,
            character=active,
        )

        bindings = load_resume_bindings()
        bindings[info["session_id"]] = {
            "web_session_id": web_session_id,
            "web_session_name": web_session.get("name") or "",
            "loaded_at": datetime.now().isoformat(),
            "channel": info["label"],
            "character_id": web_session.get("character_id") or web_session.get("sender_name") or "",
        }
        save_resume_bindings(bindings)

        delete_current_session_workspace(info)

        await reply_current_channel(
            msg,
            is_group,
            f"已从 Web 会话「{web_session.get('name') or web_session_id[:8]}」载入 {len(messages)} 条消息到当前频道。\n之后 /push 会回写到该 Web 会话。",
        )

    @register_command(
        "/push",
        help_text="/push -> 将当前频道会话上传到 /resume 绑定的 Web 会话(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_push_session(msg, is_group=True):
        # 权限检查：仅管理员可使用
        if needs_legacy_admin(msg):
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        info = current_qq_session_info(msg, is_group)
        bindings = load_resume_bindings()
        binding = bindings.get(info["session_id"])
        if not binding:
            await reply_current_channel(
                msg,
                is_group,
                "当前频道还没有绑定 Web 会话。请先发送 /resume 选择一个会话。",
            )
            return

        web_session_id = binding.get("web_session_id")
        sessions = load_web_sessions()
        web_session = sessions.get(web_session_id)
        if not isinstance(web_session, dict):
            await reply_current_channel(
                msg,
                is_group,
                "绑定的 Web 会话不存在了。请重新发送 /resume 选择会话。",
            )
            return

        current_messages = copy_chat_messages(load_current_qq_session_messages(info))
        if not current_messages:
            await reply_current_channel(msg, is_group, "当前频道没有可上传的会话内容。")
            return

        system_prompt = session_system_prompt(web_session, current_messages)
        if system_prompt and not any(m.get("role") == "system" for m in current_messages):
            current_messages.insert(0, {"role": "system", "content": system_prompt})

        web_session["messages"] = current_messages
        web_session["system_prompt"] = session_system_prompt(web_session, current_messages)
        web_session["updated_at"] = datetime.now().isoformat()
        web_session["last_message"] = next(
            (
                str(m.get("content") or "")[:100]
                for m in reversed(current_messages)
                if isinstance(m, dict) and m.get("role") != "system"
            ),
            "",
        )
        sessions[web_session_id] = web_session
        save_web_sessions(sessions)

        binding["pushed_at"] = web_session["updated_at"]
        bindings[info["session_id"]] = binding
        save_resume_bindings(bindings)

        await reply_current_channel(
            msg,
            is_group,
            f"已上传当前频道的 {len(current_messages)} 条消息到 Web 会话「{web_session.get('name') or web_session_id[:8]}」。",
        )


    @register_command(
        "/mcp",
        help_text=(
            "/mcp -> 查看 MCP 连接状态(admin)\n"
            "/mcp list -> 列出 MCP 服务\n"
            "/mcp tools <名称|ID> -> 查看服务工具\n"
            "/mcp add_http <名称> <URL> -> 添加 HTTP MCP 服务\n"
            "/mcp add_stdio <名称> <command> [args_json] -> 添加 stdio MCP 服务\n"
            "/mcp del <名称|ID> -> 删除 MCP 服务"
        ),
        category="2",
        admin_show=True,
    )
    async def handle_mcp_command(msg, is_group=True):
        if str(msg.user_id) not in admin:
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        raw = (getattr(msg, "raw_message", "") or "").strip()
        parts = raw.split(maxsplit=3)
        action = parts[1].strip().lower() if len(parts) > 1 else "status"
        configs = refresh_mcp_runtime_status(load_mcp_servers())

        if action in {"status", "list"}:
            lines = ["MCP 服务列表："]
            if not configs:
                lines.append("还没有 MCP 服务配置。")
            else:
                for cfg in configs:
                    lines.append(format_mcp_server_summary(cfg))
            lines.append("")
            lines.append("用法：/mcp tools <名称|ID> | /mcp_connect <名称|ID|URL>")
            await reply_current_channel(msg, is_group, "\n".join(lines))
            return

        if action == "tools":
            target = parts[2].strip() if len(parts) > 2 else ""
            if not target:
                await reply_current_channel(msg, is_group, "用法：/mcp tools <名称|ID>")
                return
            cfg = find_mcp_server(configs, target)
            if not cfg:
                await reply_current_channel(msg, is_group, f"没有找到 MCP 服务：{target}")
                return

            try:
                from nbot.services.mcp_bridge import get_mcp_bridge

                bridge = get_mcp_bridge()
                tools = bridge.get_server_tools(str(cfg.get("id") or ""))
            except Exception as e:
                await reply_current_channel(msg, is_group, f"读取 MCP 工具失败：{e}")
                return

            if not tools:
                await reply_current_channel(
                    msg,
                    is_group,
                    f"MCP 服务「{cfg.get('name') or cfg.get('id')}」当前没有已缓存工具。\n请先发送 /mcp_connect {cfg.get('name') or cfg.get('id')}",
                )
                return

            lines = [f"MCP 工具列表：{cfg.get('name') or cfg.get('id')}"]
            for tool in tools[:50]:
                name = str(tool.get("name") or "unknown")
                desc = str(tool.get("description") or "").strip()
                lines.append(f"- {name}" + (f" -> {desc[:60]}" if desc else ""))
            if len(tools) > 50:
                lines.append(f"... 共 {len(tools)} 个工具")
            await reply_current_channel(msg, is_group, "\n".join(lines))
            return

        if action == "add_http":
            if len(parts) < 4:
                await reply_current_channel(msg, is_group, "用法：/mcp add_http <名称> <URL>")
                return
            name = parts[2].strip()
            url = parts[3].strip()
            if not name or not url.startswith(("http://", "https://")):
                await reply_current_channel(msg, is_group, "参数无效。URL 需要以 http:// 或 https:// 开头。")
                return

            entry = {
                "id": str(uuid.uuid4()),
                "name": name,
                "transport": "streamable-http",
                "description": "通过聊天命令添加",
                "enabled": True,
                "auto_connect": False,
                "connected": False,
                "tool_count": 0,
                "created_at": datetime.now().isoformat(),
                "last_connected_at": None,
                "url": url,
            }
            configs.append(entry)
            save_mcp_servers(configs)
            await reply_current_channel(msg, is_group, f"已添加 MCP 服务：{name}\n发送 /mcp_connect {name} 进行连接。")
            return

        if action == "add_stdio":
            if len(parts) < 4:
                await reply_current_channel(
                    msg,
                    is_group,
                    "用法：/mcp add_stdio <名称> <command> [args_json]",
                )
                return

            name = parts[2].strip()
            command_part = parts[3].strip()
            command = command_part
            args = []
            if " " in command_part:
                command, raw_args = command_part.split(" ", 1)
                raw_args = raw_args.strip()
                if raw_args:
                    try:
                        parsed_args = json.loads(raw_args)
                        if isinstance(parsed_args, list):
                            args = [str(item) for item in parsed_args]
                        else:
                            await reply_current_channel(msg, is_group, "args_json 必须是 JSON 数组。")
                            return
                    except json.JSONDecodeError:
                        args = raw_args.split()

            entry = {
                "id": str(uuid.uuid4()),
                "name": name,
                "transport": "stdio",
                "description": "通过聊天命令添加",
                "enabled": True,
                "auto_connect": False,
                "connected": False,
                "tool_count": 0,
                "created_at": datetime.now().isoformat(),
                "last_connected_at": None,
                "command": command,
                "args": args,
            }
            configs.append(entry)
            save_mcp_servers(configs)
            await reply_current_channel(msg, is_group, f"已添加 stdio MCP 服务：{name}\n发送 /mcp_connect {name} 进行连接。")
            return

        if action == "del":
            target = parts[2].strip() if len(parts) > 2 else ""
            if not target:
                await reply_current_channel(msg, is_group, "用法：/mcp del <名称|ID>")
                return
            cfg = find_mcp_server(configs, target)
            if not cfg:
                await reply_current_channel(msg, is_group, f"没有找到 MCP 服务：{target}")
                return
            if cfg.get("_builtin"):
                await reply_current_channel(msg, is_group, "内置 MCP 服务不允许删除。")
                return

            server_id = str(cfg.get("id") or "")
            try:
                from nbot.services.mcp_bridge import get_mcp_bridge

                bridge = get_mcp_bridge()
                bridge.run_async(bridge.disconnect_server(server_id))
            except Exception:
                pass

            new_configs = [item for item in configs if str(item.get("id") or "") != server_id]
            save_mcp_servers(new_configs)
            await reply_current_channel(msg, is_group, f"已删除 MCP 服务：{cfg.get('name') or server_id[:8]}")
            return

        await reply_current_channel(
            msg,
            is_group,
            "未知子命令。可用：list、tools、add_http、add_stdio、del",
        )

    @register_command(
        "/mcp_connect",
        help_text="/mcp_connect <名称|ID|URL> -> 连接 MCP 服务(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_mcp_connect(msg, is_group=True):
        if str(msg.user_id) not in admin:
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        raw = (getattr(msg, "raw_message", "") or "").strip()
        target = raw[len("/mcp_connect"):].strip()
        if not target:
            await reply_current_channel(msg, is_group, "用法：/mcp_connect <名称|ID|URL>")
            return

        configs = load_mcp_servers()
        cfg = None
        if target.startswith(("http://", "https://")):
            cfg = next(
                (item for item in configs if str(item.get("url") or "").strip() == target),
                None,
            )
            if cfg is None:
                cfg = build_quick_mcp_entry(target)
                configs.append(cfg)
                save_mcp_servers(configs)
        else:
            cfg = find_mcp_server(configs, target)

        if not cfg:
            await reply_current_channel(msg, is_group, f"没有找到 MCP 服务：{target}")
            return

        server_id = str(cfg.get("id") or "")
        try:
            from nbot.services.mcp_bridge import get_mcp_bridge

            bridge = get_mcp_bridge()
            result = bridge.run_async(bridge.connect_server(server_id, cfg))
        except Exception as e:
            await reply_current_channel(msg, is_group, f"MCP 连接失败：{e}")
            return

        if not result.get("ok"):
            await reply_current_channel(msg, is_group, f"MCP 连接失败：{result.get('error', 'unknown error')}")
            return

        for item in configs:
            if str(item.get("id") or "") == server_id:
                item["connected"] = True
                item["tool_count"] = int(result.get("tool_count") or 0)
                item["last_connected_at"] = datetime.now().isoformat()
                break
        save_mcp_servers(configs)
        await reply_current_channel(
            msg,
            is_group,
            f"已连接 MCP 服务：{cfg.get('name') or server_id[:8]}\n工具数量：{int(result.get('tool_count') or 0)}",
        )

    @register_command(
        "/mcp_disconnect",
        help_text="/mcp_disconnect <名称|ID> -> 断开 MCP 服务(admin)",
        category="2",
        admin_show=True,
    )
    async def handle_mcp_disconnect(msg, is_group=True):
        if str(msg.user_id) not in admin:
            await reply_current_channel(msg, is_group, "你没有权限使用该命令喔~")
            return

        raw = (getattr(msg, "raw_message", "") or "").strip()
        target = raw[len("/mcp_disconnect"):].strip()
        if not target:
            await reply_current_channel(msg, is_group, "用法：/mcp_disconnect <名称|ID>")
            return

        configs = load_mcp_servers()
        cfg = find_mcp_server(configs, target)
        if not cfg:
            await reply_current_channel(msg, is_group, f"没有找到 MCP 服务：{target}")
            return

        server_id = str(cfg.get("id") or "")
        try:
            from nbot.services.mcp_bridge import get_mcp_bridge

            bridge = get_mcp_bridge()
            bridge.run_async(bridge.disconnect_server(server_id))
        except Exception as e:
            await reply_current_channel(msg, is_group, f"MCP 断开失败：{e}")
            return

        for item in configs:
            if str(item.get("id") or "") == server_id:
                item["connected"] = False
                item["tool_count"] = 0
                break
        save_mcp_servers(configs)
        await reply_current_channel(msg, is_group, f"已断开 MCP 服务：{cfg.get('name') or server_id[:8]}")

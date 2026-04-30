import json
import uuid
from typing import Any, Callable, Dict, List, Optional


def build_qq_session_id(
    user_id: Optional[str] = None,
    group_id: Optional[str] = None,
    group_user_id: Optional[str] = None,
) -> str:
    if user_id:
        return f"qq_private_{user_id}"
    if group_id and group_user_id:
        return f"qq_group_{group_id}_{group_user_id}"
    if group_id:
        return f"qq_group_{group_id}"
    return ""


def build_qq_history_key(
    user_id: Optional[str] = None,
    group_id: Optional[str] = None,
    group_user_id: Optional[str] = None,
) -> str:
    if user_id:
        return str(user_id)
    if group_id and group_user_id:
        return f"{group_id}_{group_user_id}"
    if group_id:
        return str(group_id)
    return ""


def build_cli_session_id() -> str:
    return f"cli_{uuid.uuid4().hex}"


def build_chat_message(
    role: str,
    content: str,
    *,
    extra: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    message = {"role": role, "content": content}
    if extra:
        message.update(extra)
    return message


class QQSessionStore:
    def __init__(
        self,
        *,
        user_messages: Dict[str, List[Dict[str, Any]]],
        group_messages: Dict[str, List[Dict[str, Any]]],
        prompt_loader: Callable[..., str],
        max_history: int,
        save_callback: Optional[Callable[[], None]] = None,
    ):
        self.user_messages = user_messages
        self.group_messages = group_messages
        self.prompt_loader = prompt_loader
        self.max_history = max_history
        self.save_callback = save_callback

    def get_history_key(
        self,
        *,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        group_user_id: Optional[str] = None,
    ) -> str:
        return build_qq_history_key(user_id, group_id, group_user_id)

    def get_session_id(
        self,
        *,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        group_user_id: Optional[str] = None,
    ) -> str:
        return build_qq_session_id(user_id, group_id, group_user_id)

    def ensure_history(
        self,
        *,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        group_user_id: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        if user_id:
            user_id = str(user_id)
            prompt = self.prompt_loader(user_id=user_id)
            return self._ensure_bucket(self.user_messages, user_id, prompt)

        if group_id:
            group_id = str(group_id)
            prompt = self.prompt_loader(group_id=group_id)
            history_key = build_qq_history_key(
                group_id=group_id, group_user_id=group_user_id
            )
            return self._ensure_bucket(self.group_messages, history_key, prompt)

        return []

    def append_message(
        self,
        *,
        role: str,
        content: str,
        user_id: Optional[str] = None,
        group_id: Optional[str] = None,
        group_user_id: Optional[str] = None,
        extra: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        messages = self.ensure_history(
            user_id=user_id, group_id=group_id, group_user_id=group_user_id
        )
        messages.append(build_chat_message(role, content, extra=extra))
        self._trim(messages)
        self.save()
        return messages

    def save(self) -> None:
        if self.save_callback:
            self.save_callback()

    def _ensure_bucket(
        self,
        store: Dict[str, List[Dict[str, Any]]],
        key: str,
        prompt: str,
    ) -> List[Dict[str, Any]]:
        if key not in store:
            store[key] = [build_chat_message("system", prompt)]
            return store[key]

        messages = store[key]
        if messages and messages[0].get("role") == "system":
            messages[0]["content"] = prompt
        else:
            messages.insert(0, build_chat_message("system", prompt))

        return messages

    def _trim(self, messages: List[Dict[str, Any]]) -> None:
        # 不再按条数裁剪，由 prepare_chat_context 按 token 预算裁剪
        pass


class WebSessionStore:
    def __init__(
        self,
        sessions: Dict[str, Dict[str, Any]],
        save_callback: Optional[Callable[[], None]] = None,
    ):
        self.sessions = sessions
        self.save_callback = save_callback

    def get_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        return self.sessions.get(session_id)

    def iter_sessions(self):
        return self.sessions.items()

    def find_session_id(self, predicate: Callable[[str, Dict[str, Any]], bool]) -> Optional[str]:
        for session_id, session in self.sessions.items():
            if predicate(session_id, session):
                return session_id
        return None

    def set_session(self, session_id: str, session: Dict[str, Any]) -> Dict[str, Any]:
        self.sessions[session_id] = session
        if self.save_callback:
            self.save_callback()
        return session

    def delete_session(self, session_id: str) -> Optional[Dict[str, Any]]:
        session = self.sessions.pop(session_id, None)
        if session is not None and self.save_callback:
            self.save_callback()
        return session

    def get_messages(self, session_id: str) -> List[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return []
        session.setdefault("messages", [])
        return session["messages"]

    def append_message(self, session_id: str, message: Dict[str, Any]) -> List[Dict[str, Any]]:
        messages = self.get_messages(session_id)
        messages.append(message)
        if self.save_callback:
            self.save_callback()
        return messages

    def replace_messages(
        self, session_id: str, messages: List[Dict[str, Any]]
    ) -> List[Dict[str, Any]]:
        session = self.get_session(session_id)
        if not session:
            return []
        session["messages"] = messages
        if self.save_callback:
            self.save_callback()
        return session["messages"]


def dump_json(filepath: str, data: Any) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

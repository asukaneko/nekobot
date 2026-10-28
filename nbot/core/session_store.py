import hashlib
import json
import logging
import os
import threading
import uuid
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

_log = logging.getLogger(__name__)

# 工具轨迹实时暂存目录（每步 append，进程中断也能留下已完成步骤）
_TOOL_TRACE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))),
    "data",
    "tool_traces",
)
_TOOL_TRACE_LOCK = threading.RLock()
_TOOL_TRACE_STEPS: dict[str, list[dict[str, Any]]] = {}
_TOOL_TRACE_MAX_STEPS = 500


def tool_trace_key(session_id: str = "", conversation_id: str = "") -> str:
    """工具轨迹暂存键：优先会话 id，回退对话 id。"""
    key = str(session_id or "").strip() or str(conversation_id or "").strip()
    return key


def _tool_trace_path(key: str) -> str:
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:24]
    return os.path.join(_TOOL_TRACE_DIR, f"{digest}.jsonl")


def begin_tool_trace(key: str) -> None:
    """开始一轮工具轨迹记录（仅清理内存缓存，磁盘残留交给下一次成功收尾清理）。"""
    if not key:
        return
    with _TOOL_TRACE_LOCK:
        _TOOL_TRACE_STEPS.pop(key, None)


def append_inflight_tool_step(key: str, step: dict[str, Any]) -> None:
    """追加一个工具步骤：内存留一份，同时以 JSONL 落盘（不阻塞、异常吞掉）。"""
    if not key or not isinstance(step, dict):
        return
    with _TOOL_TRACE_LOCK:
        steps = _TOOL_TRACE_STEPS.setdefault(key, [])
        steps.append(step)
        if len(steps) > _TOOL_TRACE_MAX_STEPS:
            del steps[: len(steps) - _TOOL_TRACE_MAX_STEPS]
        path = _tool_trace_path(key)
    try:
        os.makedirs(_TOOL_TRACE_DIR, exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(step, ensure_ascii=False) + "\n")
    except Exception as exc:  # 落盘失败不影响对话主流程
        _log.debug("[ToolTrace] 追加工具轨迹失败: %s", exc)


def load_inflight_tool_steps(key: str, turn: str = "") -> list[dict[str, Any]]:
    """读取暂存的工具步骤；给了 turn 只返回该回合。优先内存，回退磁盘。"""
    if not key:
        return []
    with _TOOL_TRACE_LOCK:
        steps = list(_TOOL_TRACE_STEPS.get(key) or [])
    if not steps:
        try:
            path = _tool_trace_path(key)
            if os.path.exists(path):
                with open(path, encoding="utf-8") as handle:
                    for line in handle:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            steps.append(json.loads(line))
                        except json.JSONDecodeError:
                            continue
        except Exception as exc:
            _log.debug("[ToolTrace] 读取工具轨迹失败: %s", exc)
    if turn:
        steps = [step for step in steps if str(step.get("turn", "")) == str(turn)]
    return steps


def clear_inflight_tool_steps(key: str) -> None:
    """收尾：清空内存缓存并删除落盘文件。"""
    if not key:
        return
    with _TOOL_TRACE_LOCK:
        _TOOL_TRACE_STEPS.pop(key, None)
        path = _tool_trace_path(key)
    try:
        if os.path.exists(path):
            os.remove(path)
    except Exception as exc:
        _log.debug("[ToolTrace] 清理工具轨迹失败: %s", exc)


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
    extra: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    message = {"role": role, "content": content}
    if extra:
        message.update(extra)
    return message


def is_message_deleted(message: Any) -> bool:
    """判断消息是否被软删除（保留记录，但所有展示与模型上下文均忽略）。"""
    return bool(isinstance(message, dict) and message.get("deleted"))


def visible_messages(messages: Any) -> list[dict[str, Any]]:
    """过滤软删除消息，返回可展示/可送入模型的消息列表。"""
    if not isinstance(messages, list):
        return []
    return [msg for msg in messages if isinstance(msg, dict) and not is_message_deleted(msg)]


def deleted_message_ids(messages: Any) -> set:
    """收集消息列表中的软删除消息 id，用于阻止异步回写复活。"""
    if not isinstance(messages, list):
        return set()
    return {
        str(msg.get("id"))
        for msg in messages
        if isinstance(msg, dict) and is_message_deleted(msg) and msg.get("id")
    }


# swipes：切换候选时需要一并失效的派生内容（旧语音不再匹配新文本）
_VARIANT_DERIVED_KEYS = ("audio_url", "tts_audio")


def ensure_message_variants(message: dict[str, Any]) -> list[dict[str, Any]]:
    """回填消息的 variants 候选列表（swipes）。

    未携带 variants 的历史消息以当前 content 作为第一个候选；
    content 为空的消息（如表情包/图片气泡）不参与候选。
    """
    if not isinstance(message, dict):
        return []
    variants = message.get("variants")
    if not isinstance(variants, list):
        variants = []
    variants = [v for v in variants if isinstance(v, dict)]
    if not variants and str(message.get("content") or ""):
        variants = [
            {
                "content": str(message.get("content") or ""),
                "model": str(message.get("model") or ""),
                "created_at": str(message.get("timestamp") or ""),
            }
        ]
    message["variants"] = variants
    index = message.get("variant_index")
    if not isinstance(index, int) or index < 0 or index >= len(variants):
        index = len(variants) - 1 if variants else 0
        message["variant_index"] = index
    return variants


def select_message_variant(message: dict[str, Any], index: int) -> bool:
    """切换当前选中候选：variant_index 指向新候选，content 镜像其文本。

    上下文组装、导出、公开分享均读取 content，因此无需各自适配。
    """
    variants = ensure_message_variants(message)
    if index < 0 or index >= len(variants):
        return False
    message["variant_index"] = index
    message["content"] = str(variants[index].get("content") or "")
    for key in _VARIANT_DERIVED_KEYS:
        message.pop(key, None)
    return True


def merge_message_variant(
    message: dict[str, Any],
    incoming: dict[str, Any],
    *,
    model_id: str = "",
    update_last: bool = False,
) -> bool:
    """将新生成的内容作为新候选追加到消息（swipes 重新生成）。

    消息身份（id/位置/时间戳）保持不变，content 镜像新候选。
    update_last=True 时回写当前选中候选（同轮生成内的二次保存，如剥离生图标签）。
    """
    if not isinstance(message, dict) or is_message_deleted(message):
        return False
    incoming = incoming if isinstance(incoming, dict) else {}
    content = str(incoming.get("content") or "")
    variants = ensure_message_variants(message)
    if update_last and variants:
        index = message.get("variant_index")
        if not isinstance(index, int) or index < 0 or index >= len(variants):
            index = len(variants) - 1
        variants[index]["content"] = content
        variants[index]["model"] = str(model_id or incoming.get("model") or "")
    else:
        variants.append(
            {
                "content": content,
                "model": str(model_id or incoming.get("model") or ""),
                "created_at": datetime.now().astimezone().isoformat(),
            }
        )
        message["variant_index"] = len(variants) - 1
    message["content"] = content
    for key in _VARIANT_DERIVED_KEYS:
        message.pop(key, None)
    return True


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
        include_memories: bool = True,
    ) -> List[Dict[str, Any]]:
        if user_id:
            user_id = str(user_id)
            prompt = self.prompt_loader(user_id=user_id, include_memories=include_memories)
            return self._ensure_bucket(self.user_messages, user_id, prompt)

        if group_id:
            group_id = str(group_id)
            prompt = self.prompt_loader(group_id=group_id, include_memories=include_memories)
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
        # 过滤检查
        filtered_content = content
        try:
            from nbot.message_filter import message_filter

            if user_id:
                filter_session_id = f"qq:private:{user_id}"
            elif group_id:
                filter_session_id = f"qq:group:{group_id}"
            else:
                filter_session_id = ""

            message_dict = {"role": role, "content": content}
            result = message_filter.filter_message(
                message_dict,
                channel="qq",
                session_id=filter_session_id,
            )
            if result.get("blocked"):
                messages = self.ensure_history(
                    user_id=user_id, group_id=group_id, group_user_id=group_user_id
                )
                return messages
            if result.get("filtered"):
                filtered_content = result["message"].get("content", content)
        except Exception as e:
            _log.debug("[QQSessionStore] 过滤检查异常: %s", e)

        messages = self.ensure_history(
            user_id=user_id, group_id=group_id, group_user_id=group_user_id
        )
        messages.append(build_chat_message(role, filtered_content, extra=extra))
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
        self._filter_session_messages(session_id, session)
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
        session = self.get_session(session_id) or {}
        if self._filter_message(session_id, message, session) is None:
            return messages
        # 幂等：若同 id 消息已存在（如流式消息在剥离 [send_image:] 标签后被再次持久化），
        # 更新原条目而非追加，避免出现两个内容相同的气泡。
        msg_id = message.get("id")
        if msg_id:
            for idx, existing in enumerate(messages):
                if existing.get("id") == msg_id:
                    if is_message_deleted(existing):
                        # 已软删除的消息不允许被异步回写复活
                        return messages
                    messages[idx] = message
                    if self.save_callback:
                        self.save_callback()
                    return messages
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
        vanished_ids = deleted_message_ids(session.get("messages"))
        if vanished_ids:
            # 传入列表若来自软删除之前的快照，同 id 消息不得复活
            messages = [
                msg
                for msg in messages
                if not (
                    isinstance(msg, dict)
                    and str(msg.get("id") or "") in vanished_ids
                    and not is_message_deleted(msg)
                )
            ]
        session["messages"] = self._filter_messages(session_id, messages, session)
        if self.save_callback:
            self.save_callback()
        return session["messages"]

    def _filter_session_messages(self, session_id: str, session: Dict[str, Any]) -> None:
        if isinstance(session.get("messages"), list):
            session["messages"] = self._filter_messages(
                session_id,
                session["messages"],
                session,
            )

    def _filter_messages(
        self,
        session_id: str,
        messages: List[Dict[str, Any]],
        session: Optional[Dict[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        filtered_messages = []
        for message in messages:
            filtered_message = self._filter_message(session_id, message, session)
            if filtered_message is not None:
                filtered_messages.append(filtered_message)
        return filtered_messages

    def _filter_message(
        self,
        session_id: str,
        message: Dict[str, Any],
        session: Optional[Dict[str, Any]] = None,
    ) -> Optional[Dict[str, Any]]:
        try:
            from nbot.message_filter import message_filter

            channel, filter_session_id = self._filter_target(session_id, session)
            result = message_filter.filter_message(
                message,
                channel=channel,
                session_id=filter_session_id,
            )
            if result.get("blocked"):
                return None
        except Exception:
            pass
        return message

    def _filter_target(
        self,
        session_id: str,
        session: Optional[Dict[str, Any]] = None,
    ) -> tuple[str, str]:
        session = session or self.get_session(session_id) or {}
        session_type = str(session.get("type") or "")
        qq_id = str(session.get("qq_id") or "").strip()

        if session_type == "qq_group" and qq_id:
            return "qq", f"qq:group:{qq_id}"
        if session_type == "qq_private" and qq_id:
            return "qq", f"qq:private:{qq_id}"
        if session_id.startswith("qq_group_"):
            group_id = session_id[len("qq_group_"):].split("_", 1)[0]
            return "qq", f"qq:group:{group_id}"
        if session_id.startswith("qq_private_"):
            user_id = session_id[len("qq_private_"):].split("_", 1)[0]
            return "qq", f"qq:private:{user_id}"

        return "web", f"web:{session_id}"


def dump_json(filepath: str, data: Any) -> None:
    with open(filepath, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=4)

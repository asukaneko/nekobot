"""读取 DeepSeek 官方「导出数据」ZIP（``deepseek_data-*.zip``）。

归档结构（``export_format_version = 3``）::

    manifest.json                       {"export_format_version": 3}
    user.json                           账号信息（不读取，避免把隐私带进会话库）
    chat_session_index.json             [{id,title,pinned,message_count,inserted_at,updated_at}]
    chat_sessions/<id>.json             {id,title,pinned,messages:[...]}
    chat_sessions/<id>_files/...        附件原文件（本读取器不读取）

消息正文由 ``fragments`` 拼装：``REQUEST`` → 用户消息，``RESPONSE``/``TEMPLATE_RESPONSE`` →
助手正文，``THINK`` → 推理内容，``FILE`` → 附件文件名（折叠成正文字符串），
联网搜索 / 工具 / 风险提示类片段不进入聊天记录。

解析结果转换成 ``nbot_session_export`` 的会话对象，交给统一的导入通道落库，
因此 Web 面板、客户端与服务端的导入走同一份归一化逻辑。
"""

from __future__ import annotations

import io
import json
import os
import uuid
import zipfile
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, BinaryIO

from nbot.web.session_import.errors import ArchiveTooLarge, UnsupportedArchive

FORMAT_ID = "deepseek"

_READ_CHUNK = 64 * 1024
_MAX_MANIFEST_BYTES = 256 * 1024
_MAX_INDEX_BYTES = 32 * 1024 * 1024
_MAX_SESSION_BYTES = 64 * 1024 * 1024
_MAX_TOTAL_READ_BYTES = 1024 * 1024 * 1024

#: 相邻消息的最小时间间隔：只用于纠正导出中「回复早于提问」的时间戳。
_MIN_MESSAGE_GAP = timedelta(milliseconds=1)

_USER_CONTENT_TYPES = frozenset({"REQUEST"})
_ASSISTANT_CONTENT_TYPES = frozenset({"RESPONSE", "TEMPLATE_RESPONSE"})
_ATTACHMENT_PREFIX = "📎 "


@dataclass(frozen=True)
class ImportableConversation:
    """导出归档中的一条会话概要，用于导入前的勾选列表。"""

    id: str
    title: str
    message_count: int
    pinned: bool
    updated_at: str | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "message_count": self.message_count,
            "pinned": self.pinned,
            "updated_at": self.updated_at,
        }


@dataclass(frozen=True)
class _DraftMessage:
    """待写入的一条消息：正文/推理已拼好，时间戳在 :meth:`_build_messages` 里统一规整。"""

    role: str
    content: str
    reasoning: str
    inserted_at: str | None


class DeepSeekArchiveReader:
    """DeepSeek 官方导出 ZIP 的只读解析器。

    Args:
        archive: 已打开的 ZIP 句柄，由本对象负责关闭。
    """

    ENTRY_INDEX = "chat_session_index.json"
    ENTRY_MANIFEST = "manifest.json"
    SESSION_DIR = "chat_sessions/"

    #: 当前已验证的导出格式版本；更高版本结构未知，明确拒绝而不是导入错误数据。
    SUPPORTED_FORMAT_VERSION = 3

    def __init__(self, archive: zipfile.ZipFile) -> None:
        self._archive = archive
        self._closed = False
        self._consumed_bytes = 0
        self._format_version: int | None = None
        self._format_version_loaded = False

        # 归档内文件的规范化名称（统一用 ``/``）→ ZIP 中的真实 entry 名。
        self._entries: dict[str, str] = {}
        for info in archive.infolist():
            if info.is_dir():
                continue
            self._entries[info.filename.replace("\\", "/")] = info.filename

        self._root_prefix = self._detect_root_prefix()

    # ------------------------------------------------------------------ 生命周期

    @classmethod
    def open(
        cls,
        source: str | os.PathLike[str] | bytes | bytearray | BinaryIO,
    ) -> DeepSeekArchiveReader:
        """打开归档。

        Args:
            source: 文件路径、ZIP 字节内容或可读的二进制文件对象。

        Raises:
            UnsupportedArchive: 不是 DeepSeek 导出包，或格式版本不受支持。
            ArchiveTooLarge: 单文件/累计读取超出上限。
        """
        reader = cls(_open_zip(source))
        try:
            if not reader._has_session_data():
                raise UnsupportedArchive(FORMAT_ID, "no chat session data")
            version = reader.export_format_version
            if version is not None and version > reader.SUPPORTED_FORMAT_VERSION:
                raise UnsupportedArchive(FORMAT_ID, f"format version {version}")
        except Exception:
            reader.close()
            raise
        return reader

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        try:
            self._archive.close()
        except Exception:  # pragma: no cover - 关闭失败不影响导入结果
            pass

    def __enter__(self) -> DeepSeekArchiveReader:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    # ------------------------------------------------------------------ 索引

    @property
    def export_format_version(self) -> int | None:
        """导出格式版本；没有 manifest 时为 ``None``。"""
        if self._format_version_loaded:
            return self._format_version
        self._format_version_loaded = True
        manifest = self._read_json(self.ENTRY_MANIFEST, _MAX_MANIFEST_BYTES)
        if isinstance(manifest, dict):
            self._format_version = _as_int(manifest.get("export_format_version"))
        return self._format_version

    def list_conversations(self) -> list[ImportableConversation]:
        """列出归档中的会话（只读索引，不解析消息正文），按更新时间倒序。"""
        indexed = [item for item in self._read_index() if self._session_entry_name(item.id)]
        if indexed:
            conversations = indexed
        else:
            # 索引缺失时的兜底：直接读每个会话文件的头部与消息数。
            conversations = []
            for conversation_id in self._session_ids():
                header = self._read_header(conversation_id)
                if header is not None:
                    conversations.append(header)
        return sorted(conversations, key=lambda item: item.updated_at or "", reverse=True)

    def _read_index(self) -> list[ImportableConversation]:
        payload = self._read_json(self.ENTRY_INDEX, _MAX_INDEX_BYTES)
        if not isinstance(payload, list):
            return []
        seen: set[str] = set()
        conversations: list[ImportableConversation] = []
        for item in payload:
            entry = _as_dict(item)
            if entry is None:
                continue
            conversation_id = _as_text(entry.get("id"))
            if not conversation_id or conversation_id in seen:
                continue
            seen.add(conversation_id)
            conversations.append(
                ImportableConversation(
                    id=conversation_id,
                    title=_as_text(entry.get("title")) or _default_title(conversation_id),
                    message_count=_as_int(entry.get("message_count")) or 0,
                    pinned=bool(entry.get("pinned")),
                    updated_at=_as_text(entry.get("updated_at"))
                    or _as_text(entry.get("inserted_at")),
                )
            )
        return conversations

    def _read_header(self, conversation_id: str) -> ImportableConversation | None:
        entry_name = self._session_entry_name(conversation_id)
        if not entry_name:
            return None
        raw = self._read_json_entry(entry_name, _MAX_SESSION_BYTES)
        if not isinstance(raw, dict):
            return None
        messages = raw.get("messages")
        return ImportableConversation(
            id=conversation_id,
            title=_as_text(raw.get("title")) or _default_title(conversation_id),
            message_count=len(messages) if isinstance(messages, list) else 0,
            pinned=bool(raw.get("pinned")),
            updated_at=_as_text(raw.get("updated_at")) or _as_text(raw.get("inserted_at")),
        )

    # ------------------------------------------------------------------ 会话

    def build_session(self, conversation_id: str) -> dict[str, Any] | None:
        """把一个会话转换成 ``nbot_session_export`` 格式的会话对象。

        会话正文为空（例如只生成到一半的提问、纯工具调用）时返回 ``None``，
        调用方按跳过计数。
        """
        entry_name = self._session_entry_name(conversation_id)
        if not entry_name:
            return None
        raw = self._read_json_entry(entry_name, _MAX_SESSION_BYTES)
        if not isinstance(raw, dict):
            return None

        drafts: list[_DraftMessage] = []
        messages_payload = raw.get("messages")
        if isinstance(messages_payload, list):
            for item in messages_payload:
                message = _as_dict(item)
                if message is None:
                    continue
                draft = self._to_draft_message(message)
                if draft is not None:
                    drafts.append(draft)
        if not drafts:
            return None

        messages = self._build_messages(raw, drafts)
        if not messages:
            return None

        source_id = _as_text(raw.get("id")) or str(conversation_id)
        title = _as_text(raw.get("title")) or _default_title(source_id)
        created_at = (
            _parse_iso(raw.get("inserted_at"))
            or _parse_iso(raw.get("updated_at"))
            or _parse_iso(messages[-1].get("timestamp"))
            or datetime.now()
        )
        updated_at = (
            _parse_iso(raw.get("updated_at"))
            or _parse_iso(messages[-1].get("timestamp"))
            or created_at
        )
        return {
            "id": source_id,  # 导出包内的会话 ID：导入时会被替换成新 ID 并记入 imported_from
            "name": title,
            "session_mode": "agent",  # 普通 AI 对话：没有绑定角色卡，按助手会话导入才能续聊
            "import_source": FORMAT_ID,
            "source_conversation_id": source_id,
            "pinned": bool(raw.get("pinned")),
            "created_at": _format_timestamp(created_at),
            "updated_at": _format_timestamp(updated_at),
            "messages": messages,
        }

    def _build_messages(
        self,
        raw: dict[str, Any],
        drafts: list[_DraftMessage],
    ) -> list[dict[str, Any]]:
        """按归档顺序强制时间戳严格递增。

        DeepSeek 的 ``inserted_at`` 存在「助手回复早于提问」的情况（同一轮最多相差几十毫秒），
        而界面按时间戳升序展示消息，直接透传会把用户消息排到 AI 回复之后。
        这里保留原时间，只在必要时向后微调。
        """
        base = (
            _parse_iso(raw.get("inserted_at"))
            or _parse_iso(raw.get("updated_at"))
            or datetime.now()
        )
        messages: list[dict[str, Any]] = []
        previous: datetime | None = None
        for draft in drafts:
            original = _parse_iso(draft.inserted_at)
            if previous is None:
                moment = original or base
            elif original is None:
                moment = previous + _MIN_MESSAGE_GAP
            else:
                moment = max(original, previous + _MIN_MESSAGE_GAP)
            previous = moment
            messages.append(_to_export_message(draft, moment))
        return messages

    def _to_draft_message(self, message: dict[str, Any]) -> _DraftMessage | None:
        """把 DeepSeek 消息（fragments 结构）解析为草稿。

        正文为空的消息返回 ``None``（思考中断、纯工具调用等没有可读内容）。
        """
        role = str(message.get("role") or "").strip().upper()
        if role == "USER":
            role_id = "user"
            content_types = _USER_CONTENT_TYPES
        elif role == "ASSISTANT":
            role_id = "assistant"
            content_types = _ASSISTANT_CONTENT_TYPES
        else:
            return None

        content_parts: list[str] = []
        reasoning_parts: list[str] = []
        attachments: list[str] = []
        fragments = message.get("fragments")
        if isinstance(fragments, list):
            for item in fragments:
                fragment = _as_dict(item)
                if fragment is None:
                    continue
                kind = str(fragment.get("type") or "").strip()
                text = _as_text(fragment.get("content"))
                if kind in content_types and text:
                    content_parts.append(text)
                elif kind == "THINK" and text:
                    reasoning_parts.append(text)
                elif kind == "FILE":
                    attachments.extend(_attachment_names(fragment))

        body = "\n\n".join(content_parts).strip()
        attachment_text = "\n".join(
            f"{_ATTACHMENT_PREFIX}{name}" for name in _dedupe(attachments)
        )
        content = "\n\n".join(part for part in (body, attachment_text) if part).strip()
        if not content:
            return None

        return _DraftMessage(
            role=role_id,
            content=content,
            reasoning="\n\n".join(reasoning_parts).strip(),
            inserted_at=_as_text(message.get("inserted_at")),
        )

    # ------------------------------------------------------------------ ZIP 读取

    def _detect_root_prefix(self) -> str:
        """识别单层包装目录。

        归档可能被用户重新打包成 ``deepseek_data-xxx/`` 之类的单层目录，
        这里识别共同前缀，避免整包导入时报「不是有效的导出包」。
        """
        if self.ENTRY_INDEX in self._entries:
            return ""
        suffix = f"/{self.ENTRY_INDEX}"
        for name in self._entries:
            if name.endswith(suffix):
                return name[: -len(self.ENTRY_INDEX)]
        return ""

    def _has_session_data(self) -> bool:
        return (
            bool(self._session_ids())
            or self._resolve(self.ENTRY_INDEX) is not None
            or self._resolve(self.ENTRY_MANIFEST) is not None
        )

    def _resolve(self, relative_path: str) -> str | None:
        return self._entries.get(self._root_prefix + relative_path)

    def _session_entry_name(self, conversation_id: str) -> str | None:
        text = str(conversation_id or "").strip()
        if not text or "/" in text or "\\" in text or text in (".", ".."):
            return None
        return self._resolve(f"{self.SESSION_DIR}{text}.json")

    def _session_ids(self) -> list[str]:
        prefix = self._root_prefix + self.SESSION_DIR
        ids: list[str] = []
        for name in self._entries:
            if not name.startswith(prefix) or not name.endswith(".json"):
                continue
            candidate = name[len(prefix) : -len(".json")]
            if not candidate or "/" in candidate or "\\" in candidate:
                continue
            ids.append(candidate)
        return ids

    def _read_json(self, relative_path: str, max_bytes: int) -> Any:
        entry_name = self._resolve(relative_path)
        if not entry_name:
            return None
        return self._read_json_entry(entry_name, max_bytes)

    def _read_json_entry(self, entry_name: str, max_bytes: int) -> Any:
        text = self._read_text(entry_name, max_bytes)
        if text is None:
            return None
        try:
            return json.loads(text)
        except (TypeError, ValueError):
            return None

    def _read_text(self, entry_name: str, max_bytes: int) -> str | None:
        try:
            info = self._archive.getinfo(entry_name)
        except KeyError:
            return None
        if info.file_size > max_bytes:
            raise ArchiveTooLarge(
                f"归档中的 {entry_name} 超过单文件体积上限（{_mib(max_bytes)} MiB）"
            )

        chunks: list[bytes] = []
        total = 0
        try:
            with self._archive.open(info) as handle:
                while True:
                    chunk = handle.read(_READ_CHUNK)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > max_bytes or self._consumed_bytes + total > _MAX_TOTAL_READ_BYTES:
                        raise ArchiveTooLarge()
                    chunks.append(chunk)
        except zipfile.BadZipFile as exc:
            raise UnsupportedArchive(FORMAT_ID, "corrupted entry") from exc
        self._consumed_bytes += total
        return b"".join(chunks).decode("utf-8", errors="replace")


# ---------------------------------------------------------------------- 工具函数


def _open_zip(source: Any) -> zipfile.ZipFile:
    try:
        if isinstance(source, (bytes, bytearray)):
            return zipfile.ZipFile(io.BytesIO(bytes(source)))
        if isinstance(source, (str, os.PathLike)):
            return zipfile.ZipFile(os.fspath(source))
        return zipfile.ZipFile(source)
    except (zipfile.BadZipFile, EOFError, OSError, ValueError) as exc:
        raise UnsupportedArchive(FORMAT_ID, "not a zip archive") from exc


def _to_export_message(draft: _DraftMessage, moment: datetime) -> dict[str, Any]:
    stamp = _format_timestamp(moment)
    message: dict[str, Any] = {
        "id": uuid.uuid4().hex,
        "role": draft.role,
        "content": draft.content,
        "created_at": stamp,
        "timestamp": stamp,
    }
    if draft.reasoning:
        message["reasoning_content"] = draft.reasoning
    return message


def _attachment_names(fragment: dict[str, Any]) -> list[str]:
    """``FILE`` 片段里的附件名（文件本身留在 ZIP 内，不导入）。"""
    names: list[str] = []
    files = fragment.get("files")
    if not isinstance(files, list):
        return names
    for item in files:
        file_info = _as_dict(item)
        if file_info is None:
            continue
        name = _as_text(file_info.get("file_name"))
        if not name:
            export_path = _as_text(file_info.get("export_path")) or ""
            name = export_path.rsplit("/", 1)[-1].strip()
        if name:
            names.append(name)
    return names


def _as_dict(value: Any) -> dict[str, Any] | None:
    return value if isinstance(value, dict) else None


def _as_text(value: Any) -> str | None:
    if isinstance(value, str):
        text = value.strip()
        return text or None
    return None


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _dedupe(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        if not value or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _parse_iso(value: Any) -> datetime | None:
    """解析 ISO-8601 时间戳；无法解析时返回 ``None``。

    导出里的时间统一是 UTC，这里转换成服务器本地时间（与 ``datetime.now()`` 一致），
    避免前端按本地时间渲染时出现时区偏移。
    """
    text = _as_text(value)
    if not text:
        return None
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if moment.tzinfo is not None:
        moment = moment.astimezone().replace(tzinfo=None)
    return moment


def _format_timestamp(moment: datetime) -> str:
    return moment.isoformat(timespec="milliseconds")


def _mib(size: int) -> int:
    return max(1, int(size) // (1024 * 1024))


def _default_title(conversation_id: str) -> str:
    return f"DeepSeek {str(conversation_id)[:8]}"

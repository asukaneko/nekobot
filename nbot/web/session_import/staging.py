"""暂存上传/下载的会话归档。

「先扫描、再勾选导入」需要两次请求，而 DeepSeek 导出包可能很大（含图片附件），
因此把 ZIP 落到临时目录并返回一次性 token，避免重复上传。
token 只保存在内存里：进程重启后失效，需要重新上传。
"""

from __future__ import annotations

import os
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, BinaryIO
from urllib.parse import urlsplit

from nbot.web.session_import.downloader import download_archive
from nbot.web.session_import.errors import ArchiveExpired, ArchiveTooLarge, UnsupportedArchive

DEFAULT_TTL_SECONDS = 30 * 60
DEFAULT_MAX_ARCHIVE_BYTES = 2 * 1024 * 1024 * 1024
_TMP_ENV = "NEKOBOT_SESSION_IMPORT_TMP"
_READ_CHUNK = 1024 * 1024


@dataclass(frozen=True)
class StagedArchive:
    """已落盘的导入归档。"""

    token: str
    path: str
    name: str
    size: int
    created_at: float


class ArchiveStaging:
    """临时归档仓库（内存索引 + 磁盘文件）。"""

    def __init__(
        self,
        root: str,
        *,
        ttl_seconds: float = DEFAULT_TTL_SECONDS,
        max_bytes: int = DEFAULT_MAX_ARCHIVE_BYTES,
    ) -> None:
        self.root = os.path.abspath(root)
        self.ttl_seconds = float(ttl_seconds)
        self.max_bytes = int(max_bytes)
        self._lock = threading.Lock()
        self._entries: dict[str, StagedArchive] = {}

    # ------------------------------------------------------------------ 写入

    def stage_upload(self, upload: Any, name: str | None = None) -> StagedArchive:
        """把上传的文件流写进临时目录（超过体积上限即中止）。"""
        filename = str(name or getattr(upload, "filename", "") or "").strip() or "archive.zip"
        stream = getattr(upload, "stream", None) or upload
        target = self._new_target()
        try:
            size = _copy_limited(stream, target, self.max_bytes)
        except Exception:
            _remove_quietly(target)
            raise
        if size <= 0:
            _remove_quietly(target)
            raise UnsupportedArchive("deepseek", "empty file")
        return self._register(target, filename, size)

    def stage_download(
        self,
        url: str,
        *,
        downloader: Callable[..., int] = download_archive,
    ) -> StagedArchive:
        """从链接下载归档到临时目录。"""
        target = self._new_target()
        try:
            size = downloader(url, target, max_bytes=self.max_bytes)
        except Exception:
            _remove_quietly(target)
            raise
        name = os.path.basename(urlsplit(str(url)).path) or "archive.zip"
        return self._register(target, name, size)

    # ------------------------------------------------------------------ 读取

    def get(self, token: str) -> StagedArchive:
        """按 token 取回归档；未知、过期或文件已丢失时抛 :class:`ArchiveExpired`。"""
        text = str(token or "").strip()
        with self._lock:
            entry = self._entries.get(text)
        if entry is None:
            raise ArchiveExpired()
        if self.ttl_seconds > 0 and (time.time() - entry.created_at) > self.ttl_seconds:
            self.discard(text)
            raise ArchiveExpired()
        if not os.path.isfile(entry.path):
            self.discard(text)
            raise ArchiveExpired()
        return entry

    def discard(self, token: str) -> None:
        """删除 token 对应的临时文件（token 未知时静默忽略）。"""
        text = str(token or "").strip()
        with self._lock:
            entry = self._entries.pop(text, None)
        if entry is not None:
            _remove_quietly(entry.path)

    def cleanup(self, now: float | None = None) -> int:
        """清理过期条目与遗留文件，返回删除的文件数。"""
        current = time.time() if now is None else float(now)
        removed = 0
        with self._lock:
            expired = [
                token
                for token, entry in self._entries.items()
                if self.ttl_seconds > 0 and (current - entry.created_at) > self.ttl_seconds
            ]
            for token in expired:
                entry = self._entries.pop(token, None)
                if entry is not None:
                    _remove_quietly(entry.path)
                    removed += 1
            known = {entry.path for entry in self._entries.values()}
        removed += self._remove_orphans(known, current)
        return removed

    # ------------------------------------------------------------------ 内部

    def _register(self, path: str, name: str, size: int) -> StagedArchive:
        entry = StagedArchive(
            token=uuid.uuid4().hex,
            path=path,
            name=str(name or "archive.zip"),
            size=int(size),
            created_at=time.time(),
        )
        with self._lock:
            self._entries[entry.token] = entry
        return entry

    def _new_target(self) -> str:
        os.makedirs(self.root, exist_ok=True)
        handle, path = tempfile.mkstemp(prefix="session-import-", suffix=".zip", dir=self.root)
        os.close(handle)
        return path

    def _remove_orphans(self, known: set[str], current: float) -> int:
        """删除目录里不属于任何 token 的旧文件（例如进程重启前遗留的）。"""
        removed = 0
        try:
            names = os.listdir(self.root)
        except OSError:
            return 0
        for name in names:
            path = os.path.join(self.root, name)
            if path in known or not os.path.isfile(path):
                continue
            try:
                age = current - os.path.getmtime(path)
            except OSError:
                continue
            if self.ttl_seconds > 0 and age < self.ttl_seconds:
                continue
            _remove_quietly(path)
            removed += 1
        return removed


def _copy_limited(source: BinaryIO, target: str, max_bytes: int) -> int:
    """把流写入目标文件并返回字节数；超出上限时抛 :class:`ArchiveTooLarge`。"""
    total = 0
    with open(target, "wb") as handle:
        while True:
            chunk = source.read(_READ_CHUNK)
            if not chunk:
                break
            if isinstance(chunk, str):
                chunk = chunk.encode("utf-8")
            total += len(chunk)
            if total > max_bytes:
                raise ArchiveTooLarge()
            handle.write(chunk)
    return total


def _remove_quietly(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:  # pragma: no cover - 清理失败不影响主流程
        pass


_STAGING_LOCK = threading.Lock()
_STAGING: ArchiveStaging | None = None


def get_staging() -> ArchiveStaging:
    """返回进程级暂存仓库（首次调用时按环境变量创建）。"""
    global _STAGING
    with _STAGING_LOCK:
        if _STAGING is None:
            root = os.getenv(_TMP_ENV) or os.path.join(
                tempfile.gettempdir(), "nekobot-session-import"
            )
            _STAGING = ArchiveStaging(root)
        return _STAGING

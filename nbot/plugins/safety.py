"""插件路径与 ZIP 包安全校验。

所有插件文件访问都必须先经过本模块：

- :func:`normalize_relative` 拒绝绝对路径、盘符、``..``、空字节；
- :func:`resolve_inside` 用 ``realpath`` 二次校验，确保结果落在插件根目录内（防软链逃逸）；
- :func:`preflight_zip` 在解压前检查文件数、单文件/总大小、后缀白名单与软链条目。
"""

import os
import zipfile
from typing import List

#: 插件包最多包含的文件数
MAX_PLUGIN_FILES = 500
#: 单个插件解压后的总大小上限（20MB）
MAX_PLUGIN_TOTAL_BYTES = 20 * 1024 * 1024
#: 插件包内单个文件大小上限（4MB）
MAX_PLUGIN_FILE_BYTES = 4 * 1024 * 1024
#: 上传的 ZIP 本身大小上限（20MB）
MAX_PLUGIN_ZIP_BYTES = 20 * 1024 * 1024

#: 允许出现在插件目录中的文件后缀（小写，含点）
ALLOWED_SUFFIXES = frozenset(
    {
        ".py",
        ".json",
        ".md",
        ".txt",
        ".yaml",
        ".yml",
        ".csv",
        ".html",
        ".htm",
        ".css",
        ".js",
        ".mjs",
        ".map",
        ".svg",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".gif",
        ".ico",
        ".woff",
        ".woff2",
        ".ttf",
    }
)

#: 无后缀但允许的固定文件名
ALLOWED_BARE_NAMES = frozenset({"LICENSE", "NOTICE", "README", "CHANGELOG"})


class UnsafePathError(ValueError):
    """路径越界、非法文件名或 ZIP 内容不安全。"""


class ZipLimitError(UnsafePathError):
    """插件包超出文件数 / 体积 / 后缀限制。"""


def normalize_relative(relative: str) -> str:
    """把插件内的相对路径规范化为 ``a/b/c`` 形式，非法则抛错。"""
    text = str(relative or "").strip()
    if not text:
        raise UnsafePathError("路径不能为空")
    if "\x00" in text:
        raise UnsafePathError("路径包含空字节")
    text = text.replace("\\", "/")
    if text.startswith("/"):
        raise UnsafePathError(f"不允许绝对路径: {relative}")
    if len(text) >= 2 and text[1] == ":":
        raise UnsafePathError(f"不允许盘符路径: {relative}")

    parts: List[str] = []
    for part in text.split("/"):
        if part in ("", "."):
            continue
        if part == "..":
            raise UnsafePathError(f"不允许上级目录: {relative}")
        parts.append(part)
    if not parts:
        raise UnsafePathError(f"路径无效: {relative}")
    return "/".join(parts)


def _is_within(base: str, target: str) -> bool:
    base_norm = os.path.normcase(os.path.abspath(base))
    target_norm = os.path.normcase(os.path.abspath(target))
    if target_norm == base_norm:
        return True
    return target_norm.startswith(base_norm + os.sep)


def resolve_inside(base: str, relative: str) -> str:
    """把 ``relative`` 解析到 ``base`` 内，越界（含软链逃逸）抛 :class:`UnsafePathError`。"""
    text = str(relative or "").strip()
    base_real = os.path.realpath(base)
    if text in ("", "."):
        return base_real
    rel = normalize_relative(relative)
    target = os.path.realpath(os.path.join(base_real, rel))
    if not _is_within(base_real, target):
        raise UnsafePathError(f"路径越界: {relative}")
    return target


def is_allowed_suffix(relative: str) -> bool:
    """判断文件后缀是否在白名单内。"""
    name = os.path.basename(relative.replace("\\", "/"))
    if not name or name.startswith("."):
        return False
    _, suffix = os.path.splitext(name)
    if not suffix:
        return name.upper() in ALLOWED_BARE_NAMES
    return suffix.lower() in ALLOWED_SUFFIXES


def _is_symlink_entry(info: zipfile.ZipInfo) -> bool:
    mode = (int(info.external_attr or 0) >> 16) & 0o170000
    return mode == 0o120000


def preflight_zip(archive: zipfile.ZipFile) -> List[zipfile.ZipInfo]:
    """解压前预检，返回需要写入的文件条目；任何违规直接抛错。"""
    infos = archive.infolist()
    if len(infos) > MAX_PLUGIN_FILES:
        raise ZipLimitError(f"插件包文件数超过上限（{MAX_PLUGIN_FILES}）")

    files: List[zipfile.ZipInfo] = []
    total = 0
    for info in infos:
        if info.is_dir():
            normalize_relative(info.filename)
            continue
        if _is_symlink_entry(info):
            raise UnsafePathError(f"插件包不允许软链接: {info.filename}")
        normalized = normalize_relative(info.filename)
        if not is_allowed_suffix(normalized):
            raise ZipLimitError(f"插件包含不允许的文件类型: {info.filename}")
        size = int(info.file_size or 0)
        if size > MAX_PLUGIN_FILE_BYTES:
            limit_mb = MAX_PLUGIN_FILE_BYTES // (1024 * 1024)
            raise ZipLimitError(f"单个文件超过 {limit_mb}MB 上限: {info.filename}")
        total += size
        if total > MAX_PLUGIN_TOTAL_BYTES:
            limit_mb = MAX_PLUGIN_TOTAL_BYTES // (1024 * 1024)
            raise ZipLimitError(f"插件包解压后总大小超过 {limit_mb}MB 上限")
        files.append(info)
    if not files:
        raise ZipLimitError("插件包为空")
    return files


def extract_zip(archive: zipfile.ZipFile, dest: str) -> List[str]:
    """把插件包安全解压到 ``dest``，返回写入的相对路径列表。"""
    os.makedirs(dest, exist_ok=True)
    written: List[str] = []
    for info in preflight_zip(archive):
        relative = normalize_relative(info.filename)
        target = resolve_inside(dest, relative)
        os.makedirs(os.path.dirname(target), exist_ok=True)
        with archive.open(info) as source, open(target, "wb") as sink:
            remaining = int(info.file_size or 0)
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                remaining -= len(chunk)
                if remaining < 0:
                    raise ZipLimitError(f"插件包内文件声明大小与实际不符: {info.filename}")
                sink.write(chunk)
        written.append(relative)
    return written

"""
自定义表情包库

提供本地表情包的存储与管理：
- 上传/ZIP 批量导入（文件名即表情名）
- 改名/删除/列表
- 聊天文本中的 [名称] 标记匹配

存储位置：nbot/web/static/uploads/stickers/（静态可访问，QQ/Web 均可发送）。
"""

import logging
import os
import re
import time
import zipfile
from typing import Any, Dict, List

_log = logging.getLogger(__name__)

# 表情包存储目录（静态资源可直接访问）
_STICKER_DIRNAME = "stickers"
_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
_MAX_NAME_LEN = 40
_MAX_FILE_BYTES = 10 * 1024 * 1024  # 单文件 10MB
_MAX_ZIP_ENTRIES = 300

# 文本中的 [名称] 标记
_STICKER_TAG_RE = re.compile(r"\[([^\[\]]{1,40})\]")


def get_sticker_dir() -> str:
    base_dir = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    path = os.path.join(base_dir, "nbot", "web", "static", "uploads", _STICKER_DIRNAME)
    os.makedirs(path, exist_ok=True)
    return path


def sanitize_sticker_name(name: str) -> str:
    """表情名：保留中英文/数字/空格/下划线/连字符，去除路径分隔等危险字符。"""
    value = str(name or "").strip()
    value = re.sub(r"[\r\n\t]", "", value)
    value = re.sub(r"[\\/:*?\"<>|]", "", value)
    value = re.sub(r"\s+", " ", value).strip().strip(".")
    return value[:_MAX_NAME_LEN]


def _sticker_url(filename: str) -> str:
    return f"/static/uploads/{_STICKER_DIRNAME}/{filename}"


def list_stickers() -> List[Dict[str, Any]]:
    """列出所有自定义表情包，按名称排序。"""
    directory = get_sticker_dir()
    items: List[Dict[str, Any]] = []
    try:
        for filename in os.listdir(directory):
            filepath = os.path.join(directory, filename)
            if not os.path.isfile(filepath):
                continue
            ext = os.path.splitext(filename)[1].lower()
            if ext not in _IMAGE_EXTS:
                continue
            name = os.path.splitext(filename)[0]
            stat = os.stat(filepath)
            items.append(
                {
                    "name": name,
                    "url": _sticker_url(filename),
                    "ext": ext.lstrip("."),
                    "size": stat.st_size,
                    "created_at": int(stat.st_mtime),
                }
            )
    except OSError as exc:
        _log.warning("[StickerLib] list failed: %s", exc)
    items.sort(key=lambda x: x["name"])
    return items


def get_sticker_set() -> Dict[str, str]:
    """返回 {name: url} 映射（前端渲染/文本匹配用）。"""
    return {item["name"]: item["url"] for item in list_stickers()}


def get_sticker(name: str) -> Dict[str, Any] | None:
    """按名称取单个表情包。"""
    name = sanitize_sticker_name(name)
    if not name:
        return None
    for item in list_stickers():
        if item["name"] == name:
            return item
    return None


def add_sticker(name: str, data: bytes, ext: str = "") -> Dict[str, Any]:
    """保存一张表情包图片。ext 缺省时尝试从图片魔数推断。"""
    name = sanitize_sticker_name(name)
    if not name:
        raise ValueError("表情名称无效")
    if not data:
        raise ValueError("图片内容为空")
    if len(data) > _MAX_FILE_BYTES:
        raise ValueError("图片超过 10MB 限制")
    ext = (ext or _sniff_ext(data) or ".png").lower()
    if not ext.startswith("."):
        ext = f".{ext}"
    if ext not in _IMAGE_EXTS:
        raise ValueError(f"不支持的图片格式: {ext}")

    directory = get_sticker_dir()
    filename = f"{name}{ext}"
    filepath = os.path.join(directory, filename)
    # 同名不同扩展名的旧文件删除，保证改名/覆盖语义
    for old_ext in _IMAGE_EXTS:
        old_path = os.path.join(directory, f"{name}{old_ext}")
        if old_path != filepath and os.path.exists(old_path):
            os.remove(old_path)
    with open(filepath, "wb") as f:
        f.write(data)
    return {"name": name, "url": _sticker_url(filename), "ext": ext.lstrip(".")}


def _sniff_ext(data: bytes) -> str:
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return ".gif"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return ".webp"
    return ""


def rename_sticker(old_name: str, new_name: str) -> bool:
    old_name = sanitize_sticker_name(old_name)
    new_name = sanitize_sticker_name(new_name)
    if not old_name or not new_name:
        return False
    directory = get_sticker_dir()
    for ext in _IMAGE_EXTS:
        old_path = os.path.join(directory, f"{old_name}{ext}")
        if os.path.exists(old_path):
            new_path = os.path.join(directory, f"{new_name}{ext}")
            if os.path.exists(new_path):
                return False
            os.rename(old_path, new_path)
            return True
    return False


def delete_sticker(name: str) -> bool:
    name = sanitize_sticker_name(name)
    if not name:
        return False
    directory = get_sticker_dir()
    removed = False
    for ext in _IMAGE_EXTS:
        path = os.path.join(directory, f"{name}{ext}")
        if os.path.exists(path):
            os.remove(path)
            removed = True
    return removed


def import_stickers_zip(zip_bytes: bytes) -> Dict[str, Any]:
    """从 ZIP 批量导入表情包。文件名（去扩展名）即表情名，重名覆盖。"""
    import io

    imported: List[str] = []
    failed: List[Dict[str, str]] = []
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as zf:
        entries = [
            info for info in zf.infolist()
            if not info.is_dir() and os.path.splitext(info.filename)[1].lower() in _IMAGE_EXTS
            and not os.path.basename(info.filename).startswith((".", "__MACOSX"))
        ]
        for info in entries[:_MAX_ZIP_ENTRIES]:
            base = os.path.basename(info.filename)
            name = sanitize_sticker_name(os.path.splitext(base)[0])
            if not name:
                failed.append({"file": base, "error": "名称无效"})
                continue
            try:
                data = zf.read(info)
                add_sticker(name, data, ext=os.path.splitext(base)[1].lower())
                imported.append(name)
            except Exception as exc:
                failed.append({"file": base, "error": str(exc)})
    return {"imported": imported, "failed": failed, "count": len(imported)}


def extract_sticker_tags(text: str, available: Dict[str, str] | None = None) -> List[Dict[str, str]]:
    """从文本中提取命中表情库的 [名称] 标记。

    Returns:
        [{name, url}]（按出现顺序去重）
    """
    if not text:
        return []
    mapping = available if available is not None else get_sticker_set()
    if not mapping:
        return []
    hits: List[Dict[str, str]] = []
    seen = set()
    for match in _STICKER_TAG_RE.finditer(str(text)):
        name = match.group(1).strip()
        if name in mapping and name not in seen:
            seen.add(name)
            hits.append({"name": name, "url": mapping[name]})
    return hits

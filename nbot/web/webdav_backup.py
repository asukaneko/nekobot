"""WebDAV 配置备份/同步模块。

用户只需填写 WebDAV 根地址（如 https://dav.jianguoyun.com/dav/），
本模块会在该地址下自动创建 ``nekobot`` 文件夹，并将加密后的
``config.nbotcfg`` 配置包放置其中，实现多端备份与同步。

可选包含立绘：勾选后立绘文件会上传到 ``nekobot/portraits/`` 子目录，
同步时下载并恢复到本地 ``static/uploads/portraits/``。
"""

import hashlib
import json
import logging
import os
import re
import uuid
from copy import deepcopy
from datetime import datetime, timedelta
from typing import Any
from urllib.parse import urlparse

import requests

from nbot.web.config_transfer import (
    _PORTRAIT_URL_PREFIX,
    ConfigTransferError,
    apply_bundle,
    build_plain_bundle,
    decrypt_bundle,
    encrypt_bundle,
)

_log = logging.getLogger(__name__)

# 默认请求超时（秒）
DEFAULT_TIMEOUT = 30
# 视为大文件的上传阈值（50MB）
LARGE_FILE_THRESHOLD = 50 * 1024 * 1024

# 远程文件夹名与配置文件名（用户无需填写）
BACKUP_FOLDER = "nekobot"
BACKUP_FILENAME = "config.nbotcfg"
# 立绘子目录名
PORTRAITS_SUBDIR = "portraits"
# 立绘清单文件名（记录立绘文件列表，PROPFIND 不可用时回退使用）
PORTRAITS_MANIFEST_FILENAME = "portraits-manifest.json"

# 增量修订：每次备份单独存一份带时间戳的 .nbotcfg
REVISIONS_SUBDIR = "revisions"
REVISIONS_MANIFEST_FILENAME = "manifest.json"
# 修订 id：YYYYMMDD-HHMMSS-<4位随机>
REVISION_ID_PATTERN = re.compile(r"^\d{8}-\d{6}-[0-9a-f]{4}$")
# 同步资产（TTS 缓存 / 表情包）
ASSETS_SUBDIR = "assets"
ASSETS_MANIFEST_FILENAME = "manifest.json"
TTS_CACHE_SUBDIR = "tts_cache"
STICKERS_SUBDIR = "stickers"
ASSET_MAX_FILES = 500
ASSET_MAX_FILE_BYTES = 20 * 1024 * 1024
ASSET_ALLOWED_SUFFIXES = frozenset(
    {
        ".png",
        ".jpg",
        ".jpeg",
        ".gif",
        ".webp",
        ".mp3",
        ".wav",
        ".ogg",
        ".opus",
        ".silk",
        ".amr",
        ".m4a",
        ".aac",
        ".json",
    }
)

# 保留策略默认值：最多 20 份 / 最多 30 天 / 至少保留 1 份
DEFAULT_RETENTION = {"enabled": True, "max_count": 20, "max_days": 30}
MAX_RETENTION_COUNT = 200


class WebDAVBackupError(RuntimeError):
    """WebDAV 备份/同步过程中出现的错误"""


def _normalize_base_url(url: str) -> str:
    """规范化 WebDAV 根地址：去除末尾斜杠，校验协议。"""
    url = (url or "").strip()
    if not url:
        raise WebDAVBackupError("WebDAV 根地址不能为空")
    if not (url.startswith("http://") or url.startswith("https://")):
        raise WebDAVBackupError("WebDAV 根地址必须以 http:// 或 https:// 开头")
    return url.rstrip("/")


def _resolve_folder_url(base_url: str) -> str:
    """获取 nekobot 文件夹的完整 URL（带末尾斜杠）。"""
    return f"{_normalize_base_url(base_url)}/{BACKUP_FOLDER}/"


def _resolve_file_url(base_url: str) -> str:
    """获取 cfg 文件的完整 URL。"""
    return f"{_normalize_base_url(base_url)}/{BACKUP_FOLDER}/{BACKUP_FILENAME}"


def _build_auth(config: dict[str, Any]):
    username = str(config.get("username") or "").strip()
    password = str(config.get("password") or "")
    if username:
        return (username, password)
    return None


def _mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 4:
        return "*" * len(value)
    return value[:2] + "*" * (len(value) - 4) + value[-2:]


def _try_resolve_file_url_safely(base_url: str) -> str:
    """安全版本：base_url 非法时返回空字符串而非抛异常（用于 get_config）。"""
    try:
        return _resolve_file_url(base_url)
    except WebDAVBackupError:
        return ""


def get_config(server) -> dict[str, Any]:
    """读取 WebDAV 备份配置（密码字段会被脱敏）。"""
    raw = (server.settings.get("webdav_backup") or {}) if isinstance(server.settings, dict) else {}
    base_url = str(raw.get("url") or "")
    return {
        "enabled": bool(raw.get("enabled")),
        "url": base_url,
        "resolved_file_url": _try_resolve_file_url_safely(base_url),
        "folder": BACKUP_FOLDER,
        "filename": BACKUP_FILENAME,
        "username": str(raw.get("username") or ""),
        "password": _mask(str(raw.get("password") or "")),
        "encryption_password": _mask(str(raw.get("encryption_password") or "")),
        "has_password": bool(raw.get("password")),
        "has_encryption_password": bool(raw.get("encryption_password")),
        "last_backup_at": str(raw.get("last_backup_at") or ""),
        "last_sync_at": str(raw.get("last_sync_at") or ""),
        "last_error": str(raw.get("last_error") or ""),
        "last_file_size": int(raw.get("last_file_size") or 0),
        "last_modified": str(raw.get("last_modified") or ""),
        "last_revision_id": str(raw.get("last_revision_id") or ""),
        "revisions_folder": f"{BACKUP_FOLDER}/{REVISIONS_SUBDIR}",
        "assets_folder": f"{BACKUP_FOLDER}/{ASSETS_SUBDIR}",
        "retention": _normalize_retention(raw.get("retention")),
        "sync_tts_cache": bool(raw.get("sync_tts_cache")),
        "sync_stickers": bool(raw.get("sync_stickers")),
    }


def _normalize_retention(raw: Any) -> dict[str, Any]:
    """归一化保留策略：数量 1-200、天数 1-3650，非法值回落默认。"""
    data = raw if isinstance(raw, dict) else {}
    defaults = dict(DEFAULT_RETENTION)

    def _int(key: str, low: int, high: int) -> int:
        try:
            value = int(data.get(key, defaults[key]))
        except (TypeError, ValueError):
            return defaults[key]
        return max(low, min(high, value))

    return {
        "enabled": bool(data.get("enabled", defaults["enabled"])),
        "max_count": _int("max_count", 1, MAX_RETENTION_COUNT),
        "max_days": _int("max_days", 1, 3650),
    }


def save_config(server, payload: dict[str, Any]) -> dict[str, Any]:
    """保存 WebDAV 备份配置。

    密码字段为空字符串或形如 "****" 时视为不修改，保留原值。
    """
    if not isinstance(server.settings, dict):
        server.settings = {}
    current = dict(server.settings.get("webdav_backup") or {})

    if "enabled" in payload:
        current["enabled"] = bool(payload.get("enabled"))
    if "url" in payload:
        current["url"] = str(payload.get("url") or "")
    if "username" in payload:
        current["username"] = str(payload.get("username") or "")
    if "retention" in payload:
        current["retention"] = _normalize_retention(payload.get("retention"))
    if "sync_tts_cache" in payload:
        current["sync_tts_cache"] = bool(payload.get("sync_tts_cache"))
    if "sync_stickers" in payload:
        current["sync_stickers"] = bool(payload.get("sync_stickers"))

    # 密码字段：空字符串或掩码字符串视为不修改
    new_password = payload.get("password")
    if new_password is None:
        pass
    elif new_password == "" and payload.get("clear_password"):
        current["password"] = ""
    elif isinstance(new_password, str) and new_password and "*" not in new_password:
        current["password"] = new_password

    new_enc = payload.get("encryption_password")
    if new_enc is None:
        pass
    elif new_enc == "" and payload.get("clear_encryption_password"):
        current["encryption_password"] = ""
    elif isinstance(new_enc, str) and new_enc and "*" not in new_enc:
        current["encryption_password"] = new_enc

    # 重置状态字段
    for key in (
        "last_backup_at",
        "last_sync_at",
        "last_error",
        "last_file_size",
        "last_modified",
    ):
        if key not in current:
            current[key] = "" if key != "last_file_size" else 0

    server.settings["webdav_backup"] = current
    server._save_data("settings")
    return get_config(server)


def _webdav_settings(server) -> dict[str, Any]:
    """读取 webdav_backup 配置段（缺失返回空 dict）。"""
    if not isinstance(server.settings, dict):
        return {}
    return server.settings.get("webdav_backup") or {}


def _resolve_raw_config(server) -> dict[str, Any]:
    """读取未脱敏的配置（仅用于内部调用）。"""
    raw = (server.settings.get("webdav_backup") or {}) if isinstance(server.settings, dict) else {}
    return {
        "enabled": bool(raw.get("enabled")),
        "url": str(raw.get("url") or ""),
        "username": str(raw.get("username") or ""),
        "password": str(raw.get("password") or ""),
        "encryption_password": str(raw.get("encryption_password") or ""),
    }


def _update_status(server, **fields) -> None:
    if not isinstance(server.settings, dict):
        server.settings = {}
    wd = dict(server.settings.get("webdav_backup") or {})
    wd.update(fields)
    server.settings["webdav_backup"] = wd
    server._save_data("settings")


def _ensure_folder_exists(
    base_url: str,
    auth,
    headers,
    subfolder: str | None = None,
) -> dict[str, Any]:
    """确保目标文件夹存在，不存在则通过 MKCOL 创建。

    subfolder=None 时操作 {base}/nekobot/；
    subfolder="portraits" 时操作 {base}/nekobot/portraits/，
    且会先确保父目录 nekobot/ 存在。

    返回 {ok, exists, created, message, status_code}。

    注意：某些 WebDAV 服务器（如坚果云）对 PROPFIND 返回 403，
    但 MKCOL/PUT 仍然可用，因此 PROPFIND 403 时不直接判定失败，
    会继续尝试 MKCOL。只有 MKCOL 返回 401/403 才视为真正的权限失败。
    """
    # 如有子目录，先确保父目录 nekobot/ 存在
    if subfolder:
        parent_result = _ensure_folder_exists(base_url, auth, headers, subfolder=None)
        if not parent_result.get("ok"):
            return parent_result

    folder_url = (
        f"{base_url}/{BACKUP_FOLDER}/{subfolder}/"
        if subfolder
        else _resolve_folder_url(base_url)
    )
    folder_name = f"{BACKUP_FOLDER}/{subfolder}" if subfolder else BACKUP_FOLDER
    result = {"ok": False, "exists": False, "created": False, "message": "", "status_code": None}

    # 1. 先用 PROPFIND 检查文件夹是否已存在
    try:
        resp = requests.request(
            "PROPFIND",
            folder_url,
            auth=auth,
            headers={**headers, "Depth": "0"},
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        result["status_code"] = resp.status_code
        if resp.status_code in (207, 200):
            result["ok"] = True
            result["exists"] = True
            result["message"] = f"文件夹 {folder_name}/ 已存在"
            return result
        if resp.status_code == 401:
            # 401 是明确的认证失败
            result["message"] = "认证失败 (HTTP 401)"
            return result
        # 403 / 404 / 其他状态码都继续尝试 MKCOL
        # （403 可能是因为服务器不允许 PROPFIND，但 MKCOL 仍可能成功）
        _log.debug(f"[WebDAV] PROPFIND 返回 {resp.status_code}, 继续 MKCOL 尝试")
    except requests.exceptions.ConnectionError as exc:
        result["message"] = f"无法连接到 WebDAV 服务器: {exc}"
        return result
    except requests.exceptions.Timeout:
        result["message"] = "连接超时"
        return result
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"检查文件夹失败: {exc}"
        return result

    # 2. 文件夹不存在或 PROPFIND 被拒绝，尝试 MKCOL 创建
    try:
        mkcol_resp = requests.request(
            "MKCOL",
            folder_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        result["status_code"] = mkcol_resp.status_code
        if mkcol_resp.status_code in (200, 201):
            result["ok"] = True
            result["exists"] = True
            result["created"] = True
            result["message"] = f"文件夹 {folder_name}/ 已创建"
            return result
        if mkcol_resp.status_code == 405:
            # Method Not Allowed 通常表示文件夹已存在
            result["ok"] = True
            result["exists"] = True
            result["message"] = f"文件夹 {BACKUP_FOLDER}/ 已存在"
            return result
        if mkcol_resp.status_code == 409:
            result["message"] = "父目录不存在，请检查 WebDAV 根地址是否正确"
            return result
        if mkcol_resp.status_code in (401, 403):
            result["message"] = f"无权限创建文件夹 (HTTP {mkcol_resp.status_code})"
            return result
        result["message"] = f"创建文件夹失败 (HTTP {mkcol_resp.status_code})"
        return result
    except requests.exceptions.ConnectionError as exc:
        result["message"] = f"无法连接到 WebDAV 服务器: {exc}"
        return result
    except requests.exceptions.Timeout:
        result["message"] = "创建文件夹超时"
        return result
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"创建文件夹失败: {exc}"
        return result


def test_connection(server, config_override: dict[str, Any] | None = None) -> dict[str, Any]:
    """测试 WebDAV 连接。

    流程：
      1. 校验根地址格式
      2. 对根地址执行 HEAD 请求，验证连通性（403 不视为失败，因为某些
         WebDAV 服务器不允许 HEAD 根目录，但子路径操作可用）
      3. 检查/创建 nekobot 文件夹
      4. 对 cfg 文件执行 HEAD，检查文件是否存在

    返回 {ok, status_code, exists, message, last_modified, content_length,
          folder_exists, folder_created, resolved_file_url}
    """
    cfg = config_override if config_override is not None else _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    file_url = _resolve_file_url(base_url)
    auth = _build_auth(cfg)
    headers = {"User-Agent": "NekoBot-WebDAV/1.0"}

    result = {
        "ok": False,
        "status_code": None,
        "exists": False,
        "message": "",
        "last_modified": "",
        "content_length": 0,
        "folder_exists": False,
        "folder_created": False,
        "resolved_file_url": file_url,
    }

    # 1. 对根地址执行 HEAD 请求验证连通性
    #    某些 WebDAV 服务器（如坚果云）对根目录 HEAD 返回 403，
    #    但子路径仍然可用，因此 403 不直接判定失败，继续后续测试。
    try:
        root_resp = requests.request(
            "HEAD",
            base_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        if root_resp.status_code == 401:
            # 401 是明确的认证失败
            result["message"] = "认证失败 (HTTP 401)"
            return result
        # 403 / 200 / 404 都继续后续测试
    except requests.exceptions.ConnectionError as exc:
        result["message"] = f"无法连接到 WebDAV 服务器: {exc}"
        return result
    except requests.exceptions.Timeout:
        result["message"] = "连接超时，请检查地址或网络"
        return result
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"测试失败: {exc}"
        return result

    # 2. 确保 nekobot 文件夹存在
    folder_result = _ensure_folder_exists(base_url, auth, headers)
    result["folder_exists"] = folder_result.get("exists", False)
    result["folder_created"] = folder_result.get("created", False)
    if not folder_result.get("ok"):
        result["message"] = folder_result.get("message") or "文件夹创建失败"
        return result

    # 3. 对 cfg 文件执行 HEAD 请求
    try:
        resp = requests.request(
            "HEAD",
            file_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        result["status_code"] = resp.status_code
        if resp.status_code == 200:
            result["exists"] = True
            result["last_modified"] = resp.headers.get("Last-Modified", "")
            try:
                result["content_length"] = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                result["content_length"] = 0
            result["message"] = "连接成功，远程配置文件已存在"
            result["ok"] = True
            return result
        if resp.status_code == 404:
            result["message"] = (
                f"连接成功，{BACKUP_FOLDER}/ 文件夹已就绪，配置文件尚未创建（备份后会自动生成）"
            )
            result["ok"] = True
            return result
        if resp.status_code == 401:
            result["message"] = "认证失败 (HTTP 401)"
            return result
        if resp.status_code == 403:
            # 403 可能是因为服务器不允许 HEAD 文件（如坚果云），
            # 但 PUT/GET 可能仍然可用。给出警告但判定为连接 OK。
            result["message"] = (
                "连接可达，但服务器拒绝 HEAD 请求 (HTTP 403)。"
                "备份/同步操作可能仍然可用，建议直接尝试备份。"
            )
            result["ok"] = True
            return result
        result["message"] = f"服务器返回异常状态码: HTTP {resp.status_code}"
        return result
    except requests.exceptions.ConnectionError as exc:
        result["message"] = f"无法连接到 WebDAV 服务器: {exc}"
        return result
    except requests.exceptions.Timeout:
        result["message"] = "连接超时"
        return result
    except Exception as exc:  # noqa: BLE001
        result["message"] = f"测试失败: {exc}"
        return result


def upload_backup(
    server,
    password_override: str | None = None,
    include_portraits: bool = False,
    include_tts_cache: bool | None = None,
    include_stickers: bool | None = None,
    note: str = "",
) -> dict[str, Any]:
    """构建加密 .nbotcfg 配置包并上传到 WebDAV 服务器。

    - 覆盖式最新包写到 ``nekobot/config.nbotcfg``（兼容旧版）；
    - 同时在 ``nekobot/revisions/<时间戳-id>.nbotcfg`` 存一份增量修订，并按保留策略清理；
    - ``include_portraits`` 上传立绘；``include_tts_cache`` / ``include_stickers`` 为
      None 时取配置里的开关，``note`` 记在修订条目上（如 manual/auto）。
    """
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    file_url = _resolve_file_url(base_url)
    auth = _build_auth(cfg)
    headers = _dav_headers()

    enc_password = (password_override or cfg.get("encryption_password") or "").strip()
    if not enc_password:
        raise WebDAVBackupError("未设置加密密码，请先在配置中填写或本次提供")

    # 1. 确保 nekobot 文件夹存在
    folder_result = _ensure_folder_exists(base_url, auth, headers)
    if not folder_result.get("ok"):
        msg = folder_result.get("message") or "文件夹创建失败"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg)

    # 2. 构建加密包
    try:
        plain_bundle = build_plain_bundle(server)
        content_sha256 = _revision_content_hash(plain_bundle)
        encrypted = encrypt_bundle(server, enc_password, plain_bundle=plain_bundle)
    except ConfigTransferError as exc:
        raise WebDAVBackupError(str(exc)) from exc

    payload_bytes = json.dumps(encrypted, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    payload_size = len(payload_bytes)
    _log.info(f"[WebDAV] 上传备份到 {file_url}, 加密包大小 {payload_size} bytes")

    # 3. PUT 上传配置包
    put_headers = {
        "User-Agent": "NekoBot-WebDAV/1.0",
        "Content-Type": "application/octet-stream",
    }
    try:
        resp = requests.request(
            "PUT",
            file_url,
            auth=auth,
            headers=put_headers,
            data=payload_bytes,
            timeout=DEFAULT_TIMEOUT * 2 if payload_size > LARGE_FILE_THRESHOLD else DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.ConnectionError as exc:
        _update_status(server, last_error=f"上传失败: {exc}")
        raise WebDAVBackupError(f"无法连接到 WebDAV 服务器: {exc}") from exc
    except requests.exceptions.Timeout:
        _update_status(server, last_error="上传超时")
        raise WebDAVBackupError("上传超时，请检查网络或减小配置体积") from None
    except Exception as exc:  # noqa: BLE001
        _update_status(server, last_error=f"上传失败: {exc}")
        raise WebDAVBackupError(f"上传失败: {exc}") from exc

    if resp.status_code not in (200, 201, 204):
        msg = f"WebDAV 服务器拒绝上传 (HTTP {resp.status_code})"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg)

    # 4. 可选：上传立绘文件
    portraits_result = None
    if include_portraits:
        try:
            portraits_result = _upload_portraits(server, base_url, auth, headers)
        except WebDAVBackupError as exc:
            # 立绘上传失败不中断主流程，仅记录错误
            _log.warning(f"[WebDAV] 立绘上传失败: {exc}")
            portraits_result = {"ok": False, "error": str(exc), "uploaded": 0, "total": 0}

    now = datetime.now().isoformat()
    last_modified = resp.headers.get("Last-Modified", "")

    # 5. 增量修订：另存一份带时间戳的副本 + 更新 manifest + 保留策略
    revision_entry: dict[str, Any] | None = None
    retention_result: dict[str, Any] | None = None
    deduplicated = False
    try:
        upload_result = _upload_revision(
            server, base_url, auth, headers, payload_bytes, note=note,
            content_sha256=content_sha256, force_new=bool(password_override),
        )
        revision_entry = upload_result["entry"]
        deduplicated = upload_result["deduplicated"]
        retention_result = prune_revisions(
            server, base_url, auth, headers, upload_result["manifest"]
        )
    except WebDAVBackupError as exc:
        _log.warning("[WebDAV] 增量修订写入失败（最新包已上传）: %s", exc)
        retention_result = {"error": str(exc)}

    _update_status(
        server,
        last_backup_at=now,
        last_error="",
        last_file_size=payload_size,
        last_modified=last_modified,
        last_revision_id=(revision_entry or {}).get("id", ""),
    )

    # 6. 可选：同步 TTS 缓存 / 表情包
    asset_results: dict[str, Any] = {}
    for kind in _selected_asset_kinds(server, include_tts_cache, include_stickers):
        try:
            asset_results[kind] = sync_assets_upload(server, kind, base_url=base_url)
        except WebDAVBackupError as exc:
            _log.warning("[WebDAV] 上传 %s 失败: %s", kind, exc)
            asset_results[kind] = {"ok": False, "error": str(exc)}

    # 触发内部日志
    try:
        log_msg = f"WebDAV 备份已上传 ({payload_size} bytes)"
        if revision_entry:
            log_msg += f", 修订 {revision_entry['id']}"
        if portraits_result:
            uploaded = portraits_result.get("uploaded", 0)
            total = portraits_result.get("total", 0)
            log_msg += f", 立绘 {uploaded}/{total}"
        server.log_message("info", log_msg, important=False)
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "size": payload_size,
        "uploaded_at": now,
        "status_code": resp.status_code,
        "last_modified": last_modified,
        "file_url": file_url,
        "portraits": portraits_result,
        "revision": revision_entry,
        "deduplicated": deduplicated,
        "retention": retention_result,
        "assets": asset_results,
    }


def pull_backup(
    server,
    password_override: str | None = None,
    include_portraits: bool = False,
    revision_id: str | None = None,
    include_tts_cache: bool | None = None,
    include_stickers: bool | None = None,
) -> dict[str, Any]:
    """从 WebDAV 拉取配置包，解密并应用到本地。

    - 不传 ``revision_id``：拉取最新包 ``nekobot/config.nbotcfg``；
    - 传 ``revision_id``：从 ``nekobot/revisions/`` 恢复指定版本；
    - ``include_portraits`` 恢复立绘；``include_tts_cache`` / ``include_stickers`` 为
      None 时取配置开关。
    """
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    if revision_id:
        rid = _validate_revision_id(revision_id)
        file_url = _revision_file_url(base_url, _revision_filename(rid))
    else:
        rid = ""
        file_url = _resolve_file_url(base_url)
    auth = _build_auth(cfg)

    enc_password = (password_override or cfg.get("encryption_password") or "").strip()
    if not enc_password:
        raise WebDAVBackupError("未设置加密密码，请先在配置中填写或本次提供")

    headers = _dav_headers()
    try:
        resp = requests.request(
            "GET",
            file_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.ConnectionError as exc:
        _update_status(server, last_error=f"拉取失败: {exc}")
        raise WebDAVBackupError(f"无法连接到 WebDAV 服务器: {exc}") from exc
    except requests.exceptions.Timeout:
        _update_status(server, last_error="拉取超时")
        raise WebDAVBackupError("拉取超时，请检查网络或 WebDAV 服务器") from None
    except Exception as exc:  # noqa: BLE001
        _update_status(server, last_error=f"拉取失败: {exc}")
        raise WebDAVBackupError(f"拉取失败: {exc}") from exc

    if resp.status_code == 404:
        msg = "远程备份文件不存在，请先执行备份"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg)
    if resp.status_code != 200:
        msg = f"WebDAV 服务器返回异常 (HTTP {resp.status_code})"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg)

    raw_bytes = resp.content
    if not raw_bytes:
        msg = "远程备份文件内容为空"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg)

    # 解析 JSON 配置包
    try:
        raw_bundle = json.loads(raw_bytes.decode("utf-8"))
    except Exception as exc:
        msg = f"远程备份文件不是有效的 JSON: {exc}"
        _update_status(server, last_error=msg)
        raise WebDAVBackupError(msg) from exc

    # 解密并应用
    try:
        bundle = decrypt_bundle(raw_bundle, enc_password)
    except ConfigTransferError as exc:
        _update_status(server, last_error=str(exc))
        raise WebDAVBackupError(str(exc)) from exc

    try:
        result = apply_bundle(server, bundle, overwrite=True)
    except ConfigTransferError as exc:
        _update_status(server, last_error=str(exc))
        raise WebDAVBackupError(str(exc)) from exc
    except Exception as exc:  # noqa: BLE001
        _update_status(server, last_error=f"应用配置失败: {exc}")
        raise WebDAVBackupError(f"应用配置失败: {exc}") from exc

    # 可选：拉取立绘文件
    portraits_result = None
    if include_portraits:
        try:
            portraits_result = _download_portraits(server, base_url, auth, headers)
        except WebDAVBackupError as exc:
            _log.warning(f"[WebDAV] 立绘拉取失败: {exc}")
            portraits_result = {"ok": False, "error": str(exc), "downloaded": 0, "total": 0}

    now = datetime.now().isoformat()
    last_modified = resp.headers.get("Last-Modified", "")
    _update_status(
        server,
        last_sync_at=now,
        last_error="",
        last_file_size=len(raw_bytes),
        last_modified=last_modified,
    )

    try:
        log_msg = f"WebDAV 同步完成，已导入配置项 {len(result.get('imported', []))} 个"
        if portraits_result:
            log_msg += (
                f", 立绘 {portraits_result.get('downloaded', 0)}/"
                f"{portraits_result.get('total', 0)}"
            )
        server.log_message("info", log_msg, important=True)
    except Exception:  # noqa: BLE001
        pass

    return {
        "ok": True,
        "size": len(raw_bytes),
        "synced_at": now,
        "last_modified": last_modified,
        "imported": result.get("imported", []),
        "skipped": result.get("skipped", []),
        "exported_at": result.get("exported_at"),
        "file_url": file_url,
        "portraits": portraits_result,
    }


def _parse_propfind_size(resp) -> int:
    """从 PROPFIND 响应中提取 getcontentlength 属性值。

    WebDAV PROPFIND 返回多状态 XML，包含 getcontentlength 等属性。
    某些服务器（如坚果云）HEAD 不返回 Content-Length，但 PROPFIND 会返回。
    """
    try:
        import xml.etree.ElementTree as ET

        text = resp.text or ""
        root = ET.fromstring(text)
        # 查找所有 propstat/prop/getcontentlength
        for prop in root.iter():
            tag = prop.tag
            if tag.endswith("getcontentlength"):
                if prop.text:
                    return int(prop.text.strip())
        return 0
    except Exception:  # noqa: BLE001
        return 0


def _parse_propfind_last_modified(resp) -> str:
    """从 PROPFIND 响应中提取 getlastmodified 属性值。"""
    try:
        import xml.etree.ElementTree as ET

        text = resp.text or ""
        root = ET.fromstring(text)
        for prop in root.iter():
            tag = prop.tag
            if tag.endswith("getlastmodified"):
                if prop.text:
                    return prop.text.strip()
        return ""
    except Exception:  # noqa: BLE001
        return ""


def get_remote_info(server) -> dict[str, Any]:
    """查询远程备份文件的元信息。

    优先用 PROPFIND（可获取真实 Content-Length 和 Last-Modified），
    某些 WebDAV 服务器（如坚果云）HEAD 不返回这些字段。
    PROPFIND 不可用时回退到 HEAD。
    """
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    file_url = _resolve_file_url(base_url)
    auth = _build_auth(cfg)
    headers = {"User-Agent": "NekoBot-WebDAV/1.0"}

    info = {
        "ok": False,
        "exists": False,
        "size": 0,
        "last_modified": "",
        "status_code": None,
        "message": "",
        "file_url": file_url,
    }

    # 1. 优先尝试 PROPFIND（更可靠地获取大小）
    try:
        propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:getcontentlength/>
    <D:getlastmodified/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>"""
        resp = requests.request(
            "PROPFIND",
            file_url,
            auth=auth,
            headers={**headers, "Depth": "0", "Content-Type": "application/xml"},
            data=propfind_body,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        info["status_code"] = resp.status_code
        if resp.status_code in (207, 200):
            info["ok"] = True
            info["exists"] = True
            info["size"] = _parse_propfind_size(resp)
            info["last_modified"] = _parse_propfind_last_modified(resp)
            if not info["last_modified"]:
                info["last_modified"] = resp.headers.get("Last-Modified", "")
            return info
        if resp.status_code == 404:
            info["ok"] = True
            info["exists"] = False
            info["message"] = "远程备份文件尚未创建"
            return info
        # PROPFIND 失败（如 403），继续尝试 HEAD
        _log.debug(f"[WebDAV] PROPFIND 返回 {resp.status_code}, 回退到 HEAD")
    except requests.exceptions.ConnectionError as exc:
        info["message"] = f"无法连接: {exc}"
        return info
    except requests.exceptions.Timeout:
        info["message"] = "查询超时"
        return info
    except Exception as exc:  # noqa: BLE001
        _log.debug(f"[WebDAV] PROPFIND 异常: {exc}, 回退到 HEAD")

    # 2. 回退到 HEAD 请求
    try:
        resp = requests.request(
            "HEAD",
            file_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        info["status_code"] = resp.status_code
        if resp.status_code == 200:
            info["ok"] = True
            info["exists"] = True
            info["last_modified"] = resp.headers.get("Last-Modified", "")
            try:
                info["size"] = int(resp.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                info["size"] = 0
        elif resp.status_code == 404:
            info["ok"] = True
            info["exists"] = False
            info["message"] = "远程备份文件尚未创建"
        elif resp.status_code == 403:
            # HEAD 被拒绝但文件可能存在
            info["ok"] = True
            info["exists"] = True
            info["message"] = "服务器拒绝 HEAD 请求，文件大小未知（备份/同步仍可使用）"
        else:
            info["message"] = f"HTTP {resp.status_code}"
    except requests.exceptions.ConnectionError as exc:
        info["message"] = f"无法连接: {exc}"
    except Exception as exc:  # noqa: BLE001
        info["message"] = f"查询失败: {exc}"

    return info


# ============================================================
# 增量修订（revisions）：按时间戳存多份 + manifest + 保留策略
# ============================================================


def _dav_request(method: str, url: str, **kwargs):
    """统一请求入口（测试可替换 ``requests.request``）。"""
    return requests.request(method, url, **kwargs)


def _revisions_dir_url(base_url: str) -> str:
    return f"{_normalize_base_url(base_url)}/{BACKUP_FOLDER}/{REVISIONS_SUBDIR}/"


def _revision_file_url(base_url: str, filename: str) -> str:
    return f"{_revisions_dir_url(base_url)}{filename}"


def _revisions_manifest_url(base_url: str) -> str:
    return f"{_revisions_dir_url(base_url)}{REVISIONS_MANIFEST_FILENAME}"


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _revision_content_hash(bundle: dict[str, Any]) -> str:
    """按配置内容去重，忽略导出时间、来源机器和备份自身的状态变化。"""
    configs = deepcopy(bundle.get("configs") or {})
    settings = configs.get("settings") or {}
    webdav = settings.get("webdav_backup") or {}
    for key in (
        "last_backup_at", "last_sync_at", "last_error", "last_file_size",
        "last_modified", "last_revision_id",
    ):
        webdav.pop(key, None)
    canonical = json.dumps(configs, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return _sha256(canonical.encode("utf-8"))


def _new_revision_id(now: datetime | None = None) -> str:
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{stamp}-{uuid.uuid4().hex[:4]}"


def _validate_revision_id(revision_id: str) -> str:
    rid = str(revision_id or "").strip()
    if not REVISION_ID_PATTERN.match(rid):
        raise WebDAVBackupError("修订 id 非法（应形如 20261026-153000-ab12）")
    return rid


def _revision_filename(revision_id: str) -> str:
    return f"{revision_id}.nbotcfg"


def _dav_headers() -> dict[str, str]:
    return {"User-Agent": "NekoBot-WebDAV/1.0"}


def _parse_propfind_entries(resp) -> list[dict[str, Any]]:
    """解析 PROPFIND 响应为 [{name, size, last_modified}]（只取文件）。"""
    try:
        import xml.etree.ElementTree as ET
        from urllib.parse import unquote

        root = ET.fromstring(resp.text or "")
        ns = {"D": "DAV:"}
        entries: list[dict[str, Any]] = []
        for response in root.findall(".//D:response", ns):
            href_elem = response.find(".//D:href", ns)
            if href_elem is None or not href_elem.text:
                continue
            href = unquote(href_elem.text)
            if href.endswith("/"):
                continue
            name = href.rstrip("/").split("/")[-1]
            if not name:
                continue
            size = 0
            modified = ""
            for prop in response.iter():
                tag = prop.tag
                if tag.endswith("getcontentlength") and prop.text:
                    try:
                        size = int(prop.text.strip())
                    except (TypeError, ValueError):
                        size = 0
                elif tag.endswith("getlastmodified") and prop.text:
                    modified = prop.text.strip()
            entries.append({"name": name, "size": size, "last_modified": modified})
        return entries
    except Exception as exc:  # noqa: BLE001
        _log.warning("[WebDAV] 解析 PROPFIND 列表失败: %s", exc)
        return []


def _load_remote_manifest(base_url: str, auth, headers) -> dict[str, Any]:
    """读取 ``revisions/manifest.json``；不存在返回空结构。"""
    try:
        resp = _dav_request(
            "GET",
            _revisions_manifest_url(base_url),
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        raise WebDAVBackupError(f"读取修订清单失败: {exc}") from exc

    if resp.status_code == 404:
        return {"version": 1, "updated_at": "", "revisions": []}
    if resp.status_code != 200:
        raise WebDAVBackupError(f"读取修订清单失败 (HTTP {resp.status_code})")

    try:
        data = json.loads(resp.text or "{}")
    except json.JSONDecodeError as exc:
        raise WebDAVBackupError(f"修订清单不是合法 JSON: {exc}") from exc
    raw_revisions = data.get("revisions") if isinstance(data, dict) else None
    if not isinstance(raw_revisions, list):
        raw_revisions = []

    revisions: list[dict[str, Any]] = []
    for item in raw_revisions:
        if not isinstance(item, dict):
            continue
        rid = str(item.get("id") or "").strip()
        if not REVISION_ID_PATTERN.match(rid):
            continue
        try:
            size = int(item.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        revisions.append(
            {
                "id": rid,
                "filename": str(item.get("filename") or _revision_filename(rid)),
                "created_at": str(item.get("created_at") or ""),
                "size": size,
                "sha256": str(item.get("sha256") or ""),
                "content_sha256": str(item.get("content_sha256") or ""),
                "note": str(item.get("note") or ""),
            }
        )
    return {
        "version": 1,
        "updated_at": str((data or {}).get("updated_at") or ""),
        "revisions": revisions,
    }


def _save_remote_manifest(base_url: str, auth, headers, manifest: dict[str, Any]) -> None:
    payload = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    try:
        resp = _dav_request(
            "PUT",
            _revisions_manifest_url(base_url),
            auth=auth,
            headers={**headers, "Content-Type": "application/json"},
            data=payload,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        raise WebDAVBackupError(f"写入修订清单失败: {exc}") from exc
    if resp.status_code not in (200, 201, 204):
        raise WebDAVBackupError(f"写入修订清单失败 (HTTP {resp.status_code})")


def _ensure_nested_folder(base_url: str, auth, headers, parts: list[str]) -> None:
    """逐级确保 ``nekobot/<parts...>/`` 存在（PROPFIND 不行就 MKCOL）。"""
    folder_result = _ensure_folder_exists(base_url, auth, headers)
    if not folder_result.get("ok"):
        raise WebDAVBackupError(folder_result.get("message") or "备份根目录创建失败")

    current = _resolve_folder_url(base_url)
    for part in parts:
        current = f"{current}{part}/"
        exists = False
        try:
            resp = _dav_request(
                "PROPFIND",
                current,
                auth=auth,
                headers={**headers, "Depth": "0"},
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            exists = resp.status_code in (200, 207)
        except requests.exceptions.RequestException:
            exists = False
        if exists:
            continue
        try:
            mkcol = _dav_request(
                "MKCOL",
                current,
                auth=auth,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            raise WebDAVBackupError(f"创建目录 {part} 失败: {exc}") from exc
        if mkcol.status_code not in (200, 201, 405):
            raise WebDAVBackupError(f"创建目录 {part} 失败 (HTTP {mkcol.status_code})")


def _ensure_revisions_folder(base_url: str, auth, headers) -> None:
    _ensure_nested_folder(base_url, auth, headers, [REVISIONS_SUBDIR])


def _list_revisions_by_propfind(base_url: str, auth, headers) -> list[dict[str, Any]]:
    body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:"><D:prop>
<D:getcontentlength/><D:getlastmodified/><D:resourcetype/>
</D:prop></D:propfind>"""
    try:
        resp = _dav_request(
            "PROPFIND",
            _revisions_dir_url(base_url),
            auth=auth,
            headers={**headers, "Depth": "1", "Content-Type": "application/xml"},
            data=body,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        _log.warning("[WebDAV] PROPFIND 修订目录失败: %s", exc)
        return []
    if resp.status_code not in (200, 207):
        return []

    revisions: list[dict[str, Any]] = []
    for entry in _parse_propfind_entries(resp):
        name = entry["name"]
        if name == REVISIONS_MANIFEST_FILENAME or not name.endswith(".nbotcfg"):
            continue
        rid = name[: -len(".nbotcfg")]
        if not REVISION_ID_PATTERN.match(rid):
            continue
        revisions.append(
            {
                "id": rid,
                "filename": name,
                "created_at": entry.get("last_modified") or "",
                "size": int(entry.get("size") or 0),
                "sha256": "",
                "note": "",
            }
        )
    return revisions


def _sort_revisions(revisions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return sorted(
        revisions,
        key=lambda item: (item.get("created_at") or "", item.get("id") or ""),
        reverse=True,
    )


def list_revisions(server) -> dict[str, Any]:
    """列出远程修订（manifest 为准，缺失时回退 PROPFIND）。"""
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    auth = _build_auth(cfg)
    headers = _dav_headers()

    manifest = _load_remote_manifest(base_url, auth, headers)
    revisions = manifest["revisions"]
    source = "manifest"
    if not revisions:
        revisions = _list_revisions_by_propfind(base_url, auth, headers)
        source = "propfind"
    raw_settings = _webdav_settings(server)
    return {
        "ok": True,
        "revisions": _sort_revisions(revisions),
        "source": source,
        "folder": f"{BACKUP_FOLDER}/{REVISIONS_SUBDIR}",
        "retention": _normalize_retention(raw_settings.get("retention")),
    }


def prune_revisions(
    server, base_url: str, auth, headers, manifest: dict[str, Any] | None = None
) -> dict[str, Any]:
    """按保留策略删除旧修订；最新一份永远保留。"""
    retention = _normalize_retention(_webdav_settings(server).get("retention"))
    current = manifest or _load_remote_manifest(base_url, auth, headers)

    items = _sort_revisions(current["revisions"])
    if not retention["enabled"] or not items:
        return {"deleted": [], "kept": len(items), "retention": retention}

    now = datetime.now()
    keep: list[dict[str, Any]] = []
    deleted: list[str] = []
    for index, item in enumerate(items):
        if index == 0:
            keep.append(item)
            continue
        too_many = len(keep) >= retention["max_count"]
        too_old = False
        created_at = str(item.get("created_at") or "")
        if created_at:
            try:
                parsed = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                if parsed.tzinfo is not None:
                    parsed = parsed.astimezone().replace(tzinfo=None)
                too_old = (now - parsed) > timedelta(days=retention["max_days"])
            except ValueError:
                too_old = False
        if not (too_many or too_old):
            keep.append(item)
            continue

        try:
            resp = _dav_request(
                "DELETE",
                _revision_file_url(base_url, item["filename"]),
                auth=auth,
                headers=headers,
                timeout=DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
            if resp.status_code not in (200, 204, 404):
                _log.warning(
                    "[WebDAV] 删除过期修订失败 %s: HTTP %s", item["id"], resp.status_code
                )
                keep.append(item)
                continue
        except requests.exceptions.RequestException as exc:
            _log.warning("[WebDAV] 删除过期修订失败 %s: %s", item["id"], exc)
            keep.append(item)
            continue
        deleted.append(item["id"])

    if deleted:
        current["revisions"] = keep
        current["updated_at"] = datetime.now().isoformat()
        try:
            _save_remote_manifest(base_url, auth, headers, current)
        except WebDAVBackupError as exc:
            _log.warning("[WebDAV] 保存修订清单失败（保留策略）: %s", exc)
    return {"deleted": deleted, "kept": len(keep), "retention": retention}


def _upload_revision(
    server,
    base_url: str,
    auth,
    headers,
    payload_bytes: bytes,
    *,
    note: str = "",
    content_sha256: str = "",
    force_new: bool = False,
) -> dict[str, Any]:
    """上传一份带时间戳的修订并更新 manifest。"""
    _ensure_revisions_folder(base_url, auth, headers)
    manifest = _load_remote_manifest(base_url, auth, headers)
    payload_hash = _sha256(payload_bytes)
    revisions = _sort_revisions(manifest["revisions"])
    latest = revisions[0] if revisions else None
    hash_key = "content_sha256" if content_sha256 else "sha256"
    expected_hash = content_sha256 or payload_hash
    if not force_new and latest and latest.get(hash_key) == expected_hash:
        try:
            existing = _dav_request(
                "HEAD", _revision_file_url(base_url, latest["filename"]),
                auth=auth, headers=headers, timeout=DEFAULT_TIMEOUT, allow_redirects=True,
            )
            if existing.status_code == 200:
                return {"entry": latest, "manifest": manifest, "deduplicated": True}
        except requests.exceptions.RequestException:
            _log.debug("[WebDAV] 无法确认原修订存在，重新创建修订")
    revision_id = _new_revision_id()
    filename = _revision_filename(revision_id)
    try:
        resp = _dav_request(
            "PUT",
            _revision_file_url(base_url, filename),
            auth=auth,
            headers={**headers, "Content-Type": "application/octet-stream"},
            data=payload_bytes,
            timeout=DEFAULT_TIMEOUT * 2,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        raise WebDAVBackupError(f"上传修订失败: {exc}") from exc
    if resp.status_code not in (200, 201, 204):
        raise WebDAVBackupError(f"上传修订被拒绝 (HTTP {resp.status_code})")

    entry = {
        "id": revision_id,
        "filename": filename,
        "created_at": datetime.now().isoformat(),
        "size": len(payload_bytes),
        "sha256": payload_hash,
        "content_sha256": content_sha256,
        "note": str(note or "")[:120],
    }
    manifest["version"] = 1
    manifest["revisions"] = [
        item for item in manifest["revisions"] if item.get("id") != revision_id
    ] + [entry]
    manifest["updated_at"] = entry["created_at"]
    _save_remote_manifest(base_url, auth, headers, manifest)
    return {"entry": entry, "manifest": manifest, "deduplicated": False}


def delete_revision(server, revision_id: str) -> dict[str, Any]:
    """删除指定修订（文件 + manifest 条目）。"""
    rid = _validate_revision_id(revision_id)
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    auth = _build_auth(cfg)
    headers = _dav_headers()
    filename = _revision_filename(rid)

    try:
        resp = _dav_request(
            "DELETE",
            _revision_file_url(base_url, filename),
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        raise WebDAVBackupError(f"删除修订失败: {exc}") from exc
    if resp.status_code == 404:
        raise WebDAVBackupError("修订不存在")
    if resp.status_code not in (200, 204):
        raise WebDAVBackupError(f"删除修订被拒绝 (HTTP {resp.status_code})")

    manifest = _load_remote_manifest(base_url, auth, headers)
    remaining = [item for item in manifest["revisions"] if item.get("id") != rid]
    manifest["revisions"] = remaining
    manifest["updated_at"] = datetime.now().isoformat()
    try:
        _save_remote_manifest(base_url, auth, headers, manifest)
    except WebDAVBackupError as exc:
        _log.warning("[WebDAV] 修订已删除但清单未更新: %s", exc)
    return {"ok": True, "id": rid, "removed": True, "remaining": len(remaining)}


def download_revision(server, revision_id: str) -> dict[str, Any]:
    """下载指定修订的原始字节（供前端另存或恢复）。"""
    rid = _validate_revision_id(revision_id)
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(cfg.get("url"))
    auth = _build_auth(cfg)
    headers = _dav_headers()
    filename = _revision_filename(rid)

    try:
        resp = _dav_request(
            "GET",
            _revision_file_url(base_url, filename),
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT * 2,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        raise WebDAVBackupError(f"下载修订失败: {exc}") from exc
    if resp.status_code == 404:
        raise WebDAVBackupError("修订不存在")
    if resp.status_code != 200:
        raise WebDAVBackupError(f"下载修订失败 (HTTP {resp.status_code})")
    content = resp.content or b""
    if not content:
        raise WebDAVBackupError("修订文件内容为空")
    return {"ok": True, "id": rid, "filename": filename, "content": content, "size": len(content)}


def restore_revision(
    server, revision_id: str, password_override: str | None = None
) -> dict[str, Any]:
    """下载指定修订、解密并应用到本地配置。"""
    rid = _validate_revision_id(revision_id)
    downloaded = download_revision(server, rid)
    cfg = _resolve_raw_config(server)
    enc_password = (password_override or cfg.get("encryption_password") or "").strip()
    if not enc_password:
        raise WebDAVBackupError("未设置加密密码，请先在配置中填写或本次提供")

    try:
        raw_bundle = json.loads(downloaded["content"].decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        raise WebDAVBackupError(f"修订文件不是有效的 JSON: {exc}") from exc

    try:
        bundle = decrypt_bundle(raw_bundle, enc_password)
        result = apply_bundle(server, bundle, overwrite=True)
    except ConfigTransferError as exc:
        _update_status(server, last_error=str(exc))
        raise WebDAVBackupError(str(exc)) from exc

    now = datetime.now().isoformat()
    _update_status(
        server,
        last_sync_at=now,
        last_error="",
        last_revision_id=rid,
        last_file_size=int(downloaded["size"]),
    )
    try:
        server.log_message(
            "info",
            f"WebDAV 已恢复到修订 {rid}（导入 {len(result.get('imported', []))} 项）",
            important=True,
        )
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True,
        "revision_id": rid,
        "size": downloaded["size"],
        "synced_at": now,
        "imported": result.get("imported", []),
        "skipped": result.get("skipped", []),
        "exported_at": result.get("exported_at"),
    }


# ============================================================
# 资产同步：TTS 缓存 / 表情包
# ============================================================

_ASSET_KINDS = {
    TTS_CACHE_SUBDIR: {"label": "TTS 缓存"},
    STICKERS_SUBDIR: {"label": "表情包"},
}


def _asset_local_dir(server, kind: str) -> str:
    if kind == STICKERS_SUBDIR:
        from nbot.services.sticker_library import get_sticker_dir

        return get_sticker_dir()
    if kind == TTS_CACHE_SUBDIR:
        path = os.path.join(server.data_dir, TTS_CACHE_SUBDIR)
        os.makedirs(path, exist_ok=True)
        return path
    raise WebDAVBackupError(f"未知资产类型: {kind}")


def _asset_dir_url(base_url: str, kind: str) -> str:
    return f"{_normalize_base_url(base_url)}/{BACKUP_FOLDER}/{ASSETS_SUBDIR}/{kind}/"


def _asset_manifest_url(base_url: str, kind: str) -> str:
    return f"{_asset_dir_url(base_url, kind)}{ASSETS_MANIFEST_FILENAME}"


def _is_allowed_asset_name(name: str) -> bool:
    if not name or name.startswith(".") or "/" in name or "\\" in name:
        return False
    _, suffix = os.path.splitext(name)
    return suffix.lower() in ASSET_ALLOWED_SUFFIXES


def _collect_asset_files(server, kind: str) -> list[dict[str, Any]]:
    directory = _asset_local_dir(server, kind)
    files: list[dict[str, Any]] = []
    try:
        names = sorted(os.listdir(directory))
    except OSError as exc:
        raise WebDAVBackupError(f"读取{_ASSET_KINDS[kind]['label']}目录失败: {exc}") from exc
    for name in names:
        if len(files) >= ASSET_MAX_FILES:
            break
        if not _is_allowed_asset_name(name):
            continue
        path = os.path.join(directory, name)
        if not os.path.isfile(path):
            continue
        try:
            size = os.path.getsize(path)
        except OSError:
            continue
        if size > ASSET_MAX_FILE_BYTES:
            continue
        files.append({"name": name, "path": path, "size": size})
    return files


def sync_assets_upload(server, kind: str, base_url: str | None = None) -> dict[str, Any]:
    """上传某类资产（TTS 缓存 / 表情包）到 WebDAV。"""
    if kind not in _ASSET_KINDS:
        raise WebDAVBackupError(f"未知资产类型: {kind}")
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(base_url or cfg.get("url"))
    auth = _build_auth(cfg)
    headers = _dav_headers()

    files = _collect_asset_files(server, kind)
    _ensure_nested_folder(base_url, auth, headers, [ASSETS_SUBDIR, kind])

    uploaded = 0
    failed = 0
    errors: list[str] = []
    for item in files:
        try:
            with open(item["path"], "rb") as handle:
                payload = handle.read()
        except OSError as exc:
            failed += 1
            errors.append(f"{item['name']}: 读取失败 {exc}")
            continue
        try:
            resp = _dav_request(
                "PUT",
                f"{_asset_dir_url(base_url, kind)}{item['name']}",
                auth=auth,
                headers={**headers, "Content-Type": "application/octet-stream"},
                data=payload,
                timeout=DEFAULT_TIMEOUT * 2
                if item["size"] > LARGE_FILE_THRESHOLD
                else DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            failed += 1
            errors.append(f"{item['name']}: {exc}")
            continue
        if resp.status_code in (200, 201, 204):
            uploaded += 1
        else:
            failed += 1
            errors.append(f"{item['name']}: HTTP {resp.status_code}")

    manifest = {
        "version": 1,
        "kind": kind,
        "updated_at": datetime.now().isoformat(),
        "files": [{"name": item["name"], "size": item["size"]} for item in files],
    }
    try:
        _dav_request(
            "PUT",
            _asset_manifest_url(base_url, kind),
            auth=auth,
            headers={**headers, "Content-Type": "application/json"},
            data=json.dumps(manifest, ensure_ascii=False).encode("utf-8"),
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        errors.append(f"清单写入失败: {exc}")

    return {
        "kind": kind,
        "label": _ASSET_KINDS[kind]["label"],
        "uploaded": uploaded,
        "failed": failed,
        "total": len(files),
        "errors": errors[:10],
    }


def _load_asset_manifest(base_url: str, kind: str, auth, headers) -> dict[str, Any] | None:
    try:
        resp = _dav_request(
            "GET",
            _asset_manifest_url(base_url, kind),
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        _log.warning("[WebDAV] 读取 %s 清单失败: %s", kind, exc)
        return None
    if resp.status_code != 200:
        return None
    try:
        data = json.loads(resp.text or "{}")
    except json.JSONDecodeError:
        return None
    files = data.get("files") if isinstance(data, dict) else None
    if not isinstance(files, list):
        return None
    return {"files": [item for item in files if isinstance(item, dict)]}


def _list_remote_asset_names(base_url: str, kind: str, auth, headers) -> list[str]:
    manifest = _load_asset_manifest(base_url, kind, auth, headers)
    if manifest is not None:
        return [
            str(item.get("name"))
            for item in manifest["files"]
            if _is_allowed_asset_name(str(item.get("name") or ""))
        ]
    body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:"><D:prop><D:getcontentlength/><D:resourcetype/></D:prop></D:propfind>"""
    try:
        resp = _dav_request(
            "PROPFIND",
            _asset_dir_url(base_url, kind),
            auth=auth,
            headers={**headers, "Depth": "1", "Content-Type": "application/xml"},
            data=body,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException:
        return []
    if resp.status_code not in (200, 207):
        return []
    names = []
    for entry in _parse_propfind_entries(resp):
        name = entry["name"]
        if name == ASSETS_MANIFEST_FILENAME:
            continue
        if _is_allowed_asset_name(name):
            names.append(name)
    return names


def sync_assets_download(server, kind: str, base_url: str | None = None) -> dict[str, Any]:
    """从 WebDAV 拉取某类资产并写入本地目录（已存在则覆盖）。"""
    if kind not in _ASSET_KINDS:
        raise WebDAVBackupError(f"未知资产类型: {kind}")
    cfg = _resolve_raw_config(server)
    base_url = _normalize_base_url(base_url or cfg.get("url"))
    auth = _build_auth(cfg)
    headers = _dav_headers()

    names = _list_remote_asset_names(base_url, kind, auth, headers)
    directory = _asset_local_dir(server, kind)
    downloaded = 0
    failed = 0
    errors: list[str] = []

    for name in names:
        try:
            resp = _dav_request(
                "GET",
                f"{_asset_dir_url(base_url, kind)}{name}",
                auth=auth,
                headers=headers,
                timeout=DEFAULT_TIMEOUT * 2,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            failed += 1
            errors.append(f"{name}: {exc}")
            continue
        if resp.status_code != 200:
            failed += 1
            errors.append(f"{name}: HTTP {resp.status_code}")
            continue
        payload = resp.content or b""
        if len(payload) > ASSET_MAX_FILE_BYTES:
            failed += 1
            errors.append(f"{name}: 超过单文件上限")
            continue
        target = os.path.join(directory, name)
        tmp_path = f"{target}.part"
        try:
            with open(tmp_path, "wb") as handle:
                handle.write(payload)
            os.replace(tmp_path, target)
            downloaded += 1
        except OSError as exc:
            failed += 1
            errors.append(f"{name}: 写入失败 {exc}")
            if os.path.exists(tmp_path):
                try:
                    os.remove(tmp_path)
                except OSError:
                    pass

    return {
        "kind": kind,
        "label": _ASSET_KINDS[kind]["label"],
        "downloaded": downloaded,
        "failed": failed,
        "total": len(names),
        "errors": errors[:10],
    }


def _selected_asset_kinds(
    server,
    include_tts: bool | None,
    include_stickers: bool | None,
) -> list[str]:
    raw_settings = _webdav_settings(server)
    kinds: list[str] = []
    config_tts = bool(raw_settings.get("sync_tts_cache"))
    config_stickers = bool(raw_settings.get("sync_stickers"))
    want_tts = config_tts if include_tts is None else bool(include_tts)
    want_stickers = config_stickers if include_stickers is None else bool(include_stickers)
    if want_tts:
        kinds.append(TTS_CACHE_SUBDIR)
    if want_stickers:
        kinds.append(STICKERS_SUBDIR)
    return kinds


# ============================================================
# 立绘文件处理
# ============================================================


def _resolve_portraits_dir_url(base_url: str) -> str:
    """返回立绘子目录的完整 URL：{base}/nekobot/portraits/"""
    return f"{base_url}/{BACKUP_FOLDER}/{PORTRAITS_SUBDIR}/"


def _resolve_portrait_file_url(base_url: str, filename: str) -> str:
    """返回单个立绘文件的完整 URL"""
    return f"{base_url}/{BACKUP_FOLDER}/{PORTRAITS_SUBDIR}/{filename}"


def _resolve_manifest_url(base_url: str) -> str:
    """返回立绘清单文件的完整 URL"""
    return f"{base_url}/{BACKUP_FOLDER}/{PORTRAITS_MANIFEST_FILENAME}"


def _collect_local_portraits(server) -> list[dict[str, str]]:
    """收集本地所有被引用的立绘文件。

    返回 [{url, filename, local_path}] 列表。
    基于 config_transfer._collect_portrait_paths 的逻辑，但返回更详细的信息。
    """
    from nbot.web.config_transfer import _collect_portrait_paths

    bundle = build_plain_bundle(server)
    portraits_map = _collect_portrait_paths(server, bundle)

    result = []
    for zip_path, local_path in portraits_map.items():
        # zip_path 形如 "portraits/xxx.png"，提取文件名
        filename = zip_path.split("/")[-1] if "/" in zip_path else zip_path
        url = f"{_PORTRAIT_URL_PREFIX}{filename}"
        result.append({
            "url": url,
            "filename": filename,
            "local_path": local_path,
        })
    return result


def _upload_portraits(server, base_url, auth, headers) -> dict[str, Any]:
    """上传本地立绘文件到 WebDAV 服务器的 nekobot/portraits/ 子目录。

    同时上传一个 portraits-manifest.json 清单文件，用于同步时回退查询。
    """
    portraits = _collect_local_portraits(server)
    if not portraits:
        _log.info("[WebDAV] 没有立绘文件需要上传")
        return {"ok": True, "uploaded": 0, "total": 0, "skipped": 0}

    # 1. 确保 portraits 子目录存在
    folder_result = _ensure_folder_exists(base_url, auth, headers, subfolder=PORTRAITS_SUBDIR)
    if not folder_result.get("ok"):
        raise WebDAVBackupError(folder_result.get("message") or "立绘子目录创建失败")

    # 2. 逐个上传立绘文件
    uploaded = 0
    skipped = 0
    failed = 0
    errors = []
    put_headers = {
        "User-Agent": "NekoBot-WebDAV/1.0",
        "Content-Type": "application/octet-stream",
    }

    for item in portraits:
        filename = item["filename"]
        local_path = item["local_path"]

        if not os.path.isfile(local_path):
            _log.warning(f"[WebDAV] 立绘文件不存在: {local_path}")
            skipped += 1
            continue

        file_url = _resolve_portrait_file_url(base_url, filename)
        try:
            file_size = os.path.getsize(local_path)
            with open(local_path, "rb") as f:
                data = f.read()
        except OSError as exc:
            _log.warning(f"[WebDAV] 读取立绘文件失败 {local_path}: {exc}")
            failed += 1
            errors.append(f"{filename}: 读取失败")
            continue

        try:
            resp = requests.request(
                "PUT",
                file_url,
                auth=auth,
                headers=put_headers,
                data=data,
                timeout=DEFAULT_TIMEOUT * 2 if file_size > LARGE_FILE_THRESHOLD else DEFAULT_TIMEOUT,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            _log.warning(f"[WebDAV] 上传立绘失败 {filename}: {exc}")
            failed += 1
            errors.append(f"{filename}: {exc}")
            continue

        if resp.status_code in (200, 201, 204):
            uploaded += 1
            _log.debug(f"[WebDAV] 立绘已上传: {filename} ({file_size} bytes)")
        else:
            _log.warning(f"[WebDAV] 立绘上传被拒绝 {filename}: HTTP {resp.status_code}")
            failed += 1
            errors.append(f"{filename}: HTTP {resp.status_code}")

    # 3. 上传清单文件
    manifest = {
        "version": 1,
        "exported_at": datetime.now().isoformat(),
        "portraits": [item["filename"] for item in portraits],
        "count": len(portraits),
    }
    manifest_url = _resolve_manifest_url(base_url)
    try:
        manifest_bytes = json.dumps(manifest, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        requests.request(
            "PUT",
            manifest_url,
            auth=auth,
            headers=put_headers,
            data=manifest_bytes,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
    except requests.exceptions.RequestException as exc:
        _log.warning(f"[WebDAV] 上传立绘清单失败: {exc}")

    _log.info(
        f"[WebDAV] 立绘上传完成: {uploaded}/{len(portraits)} 成功, "
        f"{skipped} 跳过, {failed} 失败"
    )

    return {
        "ok": failed == 0,
        "uploaded": uploaded,
        "skipped": skipped,
        "failed": failed,
        "total": len(portraits),
        "errors": errors[:10],  # 最多返回 10 条错误
    }


def _list_remote_portraits(base_url, auth, headers) -> list[str]:
    """列出远程 nekobot/portraits/ 目录下的所有立绘文件名。

    优先使用 PROPFIND，失败时回退到 portraits-manifest.json。
    返回文件名列表（不含路径前缀）。
    """
    portraits_dir_url = _resolve_portraits_dir_url(base_url)
    filenames = []

    # 1. 尝试 PROPFIND
    propfind_body = """<?xml version="1.0" encoding="utf-8"?>
<D:propfind xmlns:D="DAV:">
  <D:prop>
    <D:displayname/>
    <D:resourcetype/>
  </D:prop>
</D:propfind>"""
    try:
        resp = requests.request(
            "PROPFIND",
            portraits_dir_url,
            auth=auth,
            headers={**headers, "Depth": "1", "Content-Type": "application/xml"},
            data=propfind_body,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code == 207:
            filenames = _parse_propfind_filenames(resp)
            if filenames:
                _log.info(f"[WebDAV] PROPFIND 列出 {len(filenames)} 个立绘文件")
                return filenames
    except requests.exceptions.RequestException as exc:
        _log.warning(f"[WebDAV] PROPFIND 立绘目录失败: {exc}")

    # 2. 回退：读取清单文件
    manifest_url = _resolve_manifest_url(base_url)
    try:
        resp = requests.request(
            "GET",
            manifest_url,
            auth=auth,
            headers=headers,
            timeout=DEFAULT_TIMEOUT,
            allow_redirects=True,
        )
        if resp.status_code == 200:
            manifest = json.loads(resp.text or "{}")
            filenames = manifest.get("portraits") or []
            _log.info(f"[WebDAV] 从清单文件读取到 {len(filenames)} 个立绘文件")
            return filenames
    except (requests.exceptions.RequestException, json.JSONDecodeError) as exc:
        _log.warning(f"[WebDAV] 读取立绘清单失败: {exc}")

    return []


def _parse_propfind_filenames(resp) -> list[str]:
    """从 PROPFIND 响应中解析立绘文件名列表（排除目录本身）。"""
    try:
        import xml.etree.ElementTree as ET

        root = ET.fromstring(resp.text or "")
        # 命名空间
        ns = {"D": "DAV:"}
        filenames = []

        for response in root.findall(".//D:response", ns):
            href_elem = response.find(".//D:href", ns)
            if href_elem is None or not href_elem.text:
                continue
            href = href_elem.text

            # 跳过目录本身（以 / 结尾）
            if href.endswith("/"):
                continue

            # 提取文件名（URL 解码后取最后一段）
            from urllib.parse import unquote
            decoded = unquote(href)
            filename = decoded.rstrip("/").split("/")[-1]

            # 跳过清单文件本身
            if filename == PORTRAITS_MANIFEST_FILENAME:
                continue

            if filename:
                filenames.append(filename)

        return filenames
    except Exception as exc:  # noqa: BLE001
        _log.warning(f"[WebDAV] 解析 PROPFIND 立绘列表失败: {exc}")
        return []


def _download_portraits(server, base_url, auth, headers) -> dict[str, Any]:
    """从 WebDAV 服务器下载立绘文件并恢复到本地。

    策略：
    1. PROPFIND 列出 nekobot/portraits/ 下所有文件
    2. 回退到 portraits-manifest.json 清单
    3. 逐个 GET 下载并写入 static/uploads/portraits/
    """
    # 1. 列出远程立绘文件
    filenames = _list_remote_portraits(base_url, auth, headers)
    if not filenames:
        _log.info("[WebDAV] 远程没有立绘文件可下载")
        return {"ok": True, "downloaded": 0, "total": 0, "skipped": 0}

    # 2. 确保本地立绘目录存在
    static_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "static")
    upload_dir = os.path.join(static_root, "uploads", "portraits")
    os.makedirs(upload_dir, exist_ok=True)

    # 3. 逐个下载
    downloaded = 0
    failed = 0
    errors = []

    for filename in filenames:
        file_url = _resolve_portrait_file_url(base_url, filename)
        try:
            resp = requests.request(
                "GET",
                file_url,
                auth=auth,
                headers=headers,
                timeout=DEFAULT_TIMEOUT * 2,
                allow_redirects=True,
            )
        except requests.exceptions.RequestException as exc:
            _log.warning(f"[WebDAV] 下载立绘失败 {filename}: {exc}")
            failed += 1
            errors.append(f"{filename}: {exc}")
            continue

        if resp.status_code != 200:
            _log.warning(f"[WebDAV] 立绘下载失败 {filename}: HTTP {resp.status_code}")
            failed += 1
            errors.append(f"{filename}: HTTP {resp.status_code}")
            continue

        # 写入本地文件
        local_path = os.path.join(upload_dir, filename)
        try:
            with open(local_path, "wb") as f:
                f.write(resp.content)
            downloaded += 1
            _log.debug(f"[WebDAV] 立绘已下载: {filename} ({len(resp.content)} bytes)")
        except OSError as exc:
            _log.warning(f"[WebDAV] 写入立绘文件失败 {filename}: {exc}")
            failed += 1
            errors.append(f"{filename}: 写入失败")

    _log.info(
        f"[WebDAV] 立绘下载完成: {downloaded}/{len(filenames)} 成功, {failed} 失败"
    )

    return {
        "ok": failed == 0,
        "downloaded": downloaded,
        "failed": failed,
        "total": len(filenames),
        "errors": errors[:10],
    }


def host_from_url(url: str) -> str:
    """从 URL 中提取 host，用于显示提示信息。"""
    try:
        parsed = urlparse(url)
        return parsed.hostname or ""
    except Exception:  # noqa: BLE001
        return ""


__all__ = [
    "WebDAVBackupError",
    "BACKUP_FOLDER",
    "BACKUP_FILENAME",
    "get_config",
    "save_config",
    "test_connection",
    "upload_backup",
    "pull_backup",
    "get_remote_info",
    "host_from_url",
]

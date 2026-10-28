"""通用消息预处理中间件

为所有频道提供统一的附件处理流水线：
1. AttachmentResolver ── 频道特定 → 可访问 URL / data URL
2. MediaDescriber ── 媒体类型 → AI 文字描述
3. MessagePreprocessor ── 编排，注入 content

附件标准格式：
    {
        "type": "image" | "file" | "video" | "audio",
        "url": "https://..." (可选，直接可访问的地址),
        "source": "qq" | "feishu" | ...,
        "source_ref": "频道的引用 key",
        "mime_type": "image/png",
        "name": "文件名",
        ...频道特定字段
    }
"""

import base64
import logging
import mimetypes
import os
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import requests

try:
    from nbot.core.workspace import workspace_manager
    _WORKSPACE_AVAILABLE = True
except ImportError:
    workspace_manager = None
    _WORKSPACE_AVAILABLE = False

_log = logging.getLogger(__name__)


def _normalize_media_type(att_type: str) -> str:
    value = str(att_type or "").lower()
    if "/" in value:
        return value.split("/", 1)[0]
    return value

# 支持描述的标准附件类型
DESCRIBABLE_TYPES = {"image", "video", "audio"}
TYPE_LABELS = {"image": "图片", "gif": "动图", "video": "视频", "audio": "音频", "file": "文件"}


def _is_gif_attachment(attachment: Dict[str, Any], url: str) -> bool:
    """判断图片附件是否为 GIF 动图（需要多帧理解）。"""
    mime = str(attachment.get("mime_type") or attachment.get("mime") or "").lower()
    if mime == "image/gif":
        return True
    if url.lower().startswith("data:image/gif"):
        return True
    for value in [str(attachment.get(key) or "") for key in ("name", "filename", "file")] + [str(url or "")]:
        if value.lower().split("?", 1)[0].endswith(".gif"):
            return True
    return False

# 描述失败时的错误标识，匹配到这些前缀的描述不会注入用户消息
_DESC_ERROR_PREFIXES = ("链接失效", "解析失败")


# ---------------------------------------------------------------------------
# AttachmentResolver ── 频道特定附件解析
# ---------------------------------------------------------------------------

class AttachmentResolver:
    """频道附件解析器注册表。

    每个频道注册一个 handler，接收 attachment dict，返回可直接访问的 URL
    （http URL 或 data: URL）。
    """

    _handlers: Dict[str, Callable[[Dict[str, Any]], Optional[str]]] = {}

    @classmethod
    def register(cls, channel: str, handler: Callable[[Dict[str, Any]], Optional[str]]) -> None:
        cls._handlers[channel] = handler

    @classmethod
    def resolve(cls, channel: str, attachment: Dict[str, Any]) -> Optional[str]:
        """解析附件为可访问的 URL。"""
        # 1. 频道特定处理器
        handler = cls._handlers.get(channel)
        if handler:
            result = handler(attachment)
            if result:
                return result
        # 2. 兜底：直接 URL
        return attachment.get("url")


# ---------------------------------------------------------------------------
# MediaDescriber ── AI 媒体描述
# ---------------------------------------------------------------------------

class MediaDescriber:
    """媒体描述器。按类型注册 AI 描述函数。"""

    _describers: Dict[str, Callable[[str], Optional[str]]] = {}

    @classmethod
    def register(cls, media_type: str, func: Callable[[str], Optional[str]]) -> None:
        """注册某种媒体类型的描述函数。func 接收 URL，返回文字描述。"""
        cls._describers[media_type] = func

    @classmethod
    def describe(cls, media_type: str, url: str) -> Optional[str]:
        func = cls._describers.get(media_type)
        if func is None:
            return None
        try:
            return func(url)
        except Exception as e:
            _log.warning(f"Media describe failed [{media_type}]: {e}")
            return None


# ---------------------------------------------------------------------------
# MessagePreprocessor ── 编排
# ---------------------------------------------------------------------------

class MessagePreprocessor:
    """消息预处理流水线：工作区保存 → 解析 → 描述 → 注入内容。"""

    @staticmethod
    def process(chat_request: Any, workspace_context: Optional[Dict[str, Any]] = None) -> None:
        """处理 ChatRequest.attachments，将媒体描述注入 content。

        Args:
            chat_request: 聊天请求对象
            workspace_context: 可选的工作区上下文，提供时会将附件保存到会话工作区
        """
        attachments = getattr(chat_request, "attachments", None)
        if not attachments:
            return

        descriptions = []
        processed_indices = []

        for i, att in enumerate(attachments):
            att_type = _normalize_media_type(att.get("type", ""))
            if att_type not in DESCRIBABLE_TYPES:
                continue

            channel = att.get("source", getattr(chat_request, "channel", "unknown"))
            url = AttachmentResolver.resolve(channel, att)
            if not url:
                _log.warning(f"Preprocessor: cannot resolve {att_type} attachment, source={channel}")
                continue

            # 尝试保存到会话工作区
            if workspace_context and _WORKSPACE_AVAILABLE:
                ws_file = MessagePreprocessor._save_to_workspace(
                    url, att, workspace_context
                )
                if ws_file:
                    att["_workspace_path"] = ws_file
                    # 用本地文件路径生成 data URL 供视觉模型使用
                    try:
                        mime_type, _ = mimetypes.guess_type(ws_file)
                        mime_type = mime_type or "application/octet-stream"
                        with open(ws_file, "rb") as f:
                            b64 = base64.b64encode(f.read()).decode("utf-8")
                        url = f"data:{mime_type};base64,{b64}"
                    except Exception as e:
                        _log.warning(f"Preprocessor: failed to read workspace file for AI: {e}")

            describer_type = att_type
            if att_type == "image" and _is_gif_attachment(att, url):
                describer_type = "gif"

            desc = MediaDescriber.describe(describer_type, url)
            if desc:
                # 过滤描述失败时的错误提示，避免将"链接失效"等注入用户消息
                if any(desc.startswith(p) for p in _DESC_ERROR_PREFIXES):
                    _log.warning(
                        f"Preprocessor: describe returned error for {describer_type}: {desc}"
                    )
                    continue
                label = TYPE_LABELS.get(describer_type, describer_type)
                descriptions.append(f"[{label}{len(descriptions)+1}描述]: {desc}")
                processed_indices.append(i)
            else:
                _log.warning(f"Preprocessor: describe failed for {describer_type} attachment")

        if descriptions:
            desc_block = "\n".join(descriptions)
            original = chat_request.content or ""
            if original.strip():
                chat_request.content = f"{desc_block}\n\n用户消息: {original}"
            else:
                chat_request.content = desc_block
            _log.info(f"Preprocessor: {len(descriptions)} attachment(s) described and injected")

            remaining = [a for idx, a in enumerate(attachments) if idx not in processed_indices]
            attachments.clear()
            attachments.extend(remaining)

    @staticmethod
    def _save_to_workspace(
        url: str,
        attachment: Dict[str, Any],
        workspace_context: Dict[str, Any],
    ) -> Optional[str]:
        """尝试将附件保存到会话工作区。

        Args:
            url: 附件的可访问 URL（http/https 或 data: URL）
            attachment: 附件元数据字典
            workspace_context: 工作区上下文（含 session_id, session_type）

        Returns:
            保存成功返回文件绝对路径，失败返回 None
        """
        if not workspace_context or not _WORKSPACE_AVAILABLE or not workspace_manager:
            return None

        session_id = workspace_context.get("session_id", "")
        session_type = workspace_context.get("session_type", "unknown")
        if not session_id:
            return None

        # 确定文件名
        filename = (
            attachment.get("name")
            or attachment.get("filename")
            or attachment.get("file", "")
        )
        if not filename:
            # 从 URL 或 mime 推断
            att_type = _normalize_media_type(attachment.get("type", "file"))
            ext_map = {"image": ".png", "video": ".mp4", "audio": ".mp3", "file": ".bin"}
            filename = f"attachment{ext_map.get(att_type, '.bin')}"

        # 文件名添加时间戳前缀，避免同名文件覆盖
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        name, ext = os.path.splitext(filename)
        filename = f"{timestamp}_{name}{ext}"

        # 检查工作区是否已有同名文件（去重）
        try:
            ws_path = workspace_manager.get_or_create(session_id, session_type)
            safe_name = workspace_manager._safe_filename(filename)
            existing_path = os.path.join(ws_path, safe_name)
            if os.path.exists(existing_path):
                _log.info(f"Preprocessor: file already in workspace, skip download: {safe_name}")
                return existing_path
        except Exception:
            pass

        # 下载文件内容
        file_data = None
        try:
            if url.startswith("data:"):
                # data: URL，提取 base64 内容
                _, encoded = url.split(",", 1)
                file_data = base64.b64decode(encoded)
            elif url.startswith(("http://", "https://")):
                # HTTP URL，使用浏览器风格请求头下载
                download_headers = {
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/120.0.0.0 Safari/537.36"
                    ),
                    "Accept": "*/*",
                }
                resp = requests.get(url, headers=download_headers, timeout=60, stream=True)
                resp.raise_for_status()
                file_data = resp.content
            elif os.path.isfile(url):
                # 本地文件路径
                with open(url, "rb") as f:
                    file_data = f.read()
            else:
                return None
        except Exception as e:
            _log.warning(f"Preprocessor: download attachment failed: {e}")
            return None

        if not file_data:
            return None

        # 保存到工作区
        try:
            result = workspace_manager.save_uploaded_file(
                session_id, file_data, filename, session_type
            )
            if result.get("success"):
                saved_path = result["path"]
                _log.info(f"Preprocessor: attachment saved to workspace: {os.path.basename(saved_path)}")
                return saved_path
        except Exception as e:
            _log.warning(f"Preprocessor: save to workspace failed: {e}")

        return None


# ---------------------------------------------------------------------------
# 内置频道解析器
# ---------------------------------------------------------------------------

TELEGRAM_API_BASE = "https://api.telegram.org"


def _resolve_telegram_attachment(attachment: Dict[str, Any]) -> Optional[str]:
    """Telegram：通过 getFile API 获取文件路径，下载后返回 base64 data URL。"""
    file_id = attachment.get("source_ref") or attachment.get("file_id")
    bot_token = attachment.get("bot_token")

    if not file_id or not bot_token:
        return None

    try:
        # 1. 获取文件路径
        resp = requests.get(
            f"{TELEGRAM_API_BASE}/bot{bot_token}/getFile",
            params={"file_id": file_id},
            timeout=30,
        )
        if resp.status_code != 200:
            _log.warning("Telegram getFile failed: HTTP %d — %s", resp.status_code, resp.text[:200])
            return None

        result = resp.json().get("result", {})
        file_path = result.get("file_path")
        if not file_path:
            return None

        # 2. 下载文件内容
        dl_resp = requests.get(
            f"{TELEGRAM_API_BASE}/file/bot{bot_token}/{file_path}",
            timeout=120,
        )
        if dl_resp.status_code != 200:
            _log.warning("Telegram file download failed: HTTP %d", dl_resp.status_code)
            return None

        mime = attachment.get("mime_type") or dl_resp.headers.get("Content-Type", "application/octet-stream")
        b64 = base64.b64encode(dl_resp.content).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        _log.warning("Failed to resolve Telegram attachment %s: %s", file_id, e)
        return None


def _resolve_feishu_attachment(attachment: Dict[str, Any]) -> Optional[str]:
    """飞书：通过 API 下载消息中的附件，返回 base64 data URL。"""
    ref = attachment.get("source_ref") or attachment.get("image_key") or attachment.get("file_key")
    message_id = attachment.get("message_id", "")
    app_id = attachment.get("app_id")
    app_secret = attachment.get("app_secret")

    if not ref:
        return None

    token = None
    if app_id and app_secret:
        try:
            from nbot.services.feishu_service import get_tenant_access_token
            token = get_tenant_access_token(app_id, app_secret)
        except Exception as e:
            _log.warning(f"Failed to get Feishu token: {e}")

    if not token:
        _log.warning("Feishu token is None, cannot download attachment")
        return None

    att_type = attachment.get("type", "image")
    if att_type == "image":
        resource_type = "image"
    else:
        resource_type = "file"

    if message_id:
        url = (f"https://open.feishu.cn/open-apis/im/v1/messages/"
               f"{message_id}/resources/{ref}?type={resource_type}")
    else:
        api_path = "images" if att_type == "image" else "files"
        url = f"https://open.feishu.cn/open-apis/im/v1/{api_path}/{ref}"

    try:
        resp = requests.get(url, headers={"Authorization": f"Bearer {token}"}, timeout=60)
        if resp.status_code != 200:
            _log.warning(f"Feishu download failed: HTTP {resp.status_code} — {resp.text[:200]}")
            return None
        mime = resp.headers.get("Content-Type", "application/octet-stream")
        b64 = base64.b64encode(resp.content).decode("utf-8")
        return f"data:{mime};base64,{b64}"
    except Exception as e:
        _log.warning(f"Failed to download Feishu attachment {ref}: {e}")
        return None


def _resolve_direct_attachment(attachment: Dict[str, Any]) -> Optional[str]:
    return (
        attachment.get("data")
        or attachment.get("url")
        or attachment.get("download_url")
        or attachment.get("path")
    )


def _resolve_qq_attachment(attachment: Dict[str, Any]) -> Optional[str]:
    """QQ 频道附件解析器：优先通过 NapCat 本地 API 获取文件，降级为 HTTP 下载。

    三级策略：
    1. bot.api.get_file() — 获取 NapCat 缓存的本地文件路径（最可靠）
    2. bot.api.download_file() — 通过 NapCat 内部下载器下载（带正确签名）
    3. requests 直接下载 — 带浏览器请求头作为最后兜底
    """
    # 优先使用已有 data（base64 或本地路径）
    data = attachment.get("data")
    if data:
        return data

    file_id = attachment.get("file", "")
    url = attachment.get("url") or ""

    # CQ 码中的 URL 可能被 HTML 转义（& → &amp;），需要还原
    if "&amp;" in url:
        import html as _html
        url = _html.unescape(url)
        _log.debug(f"[QQ Attachment] URL 已反转义: {url[:80]}...")

    att_type = _normalize_media_type(attachment.get("type", "file"))
    mime_map = {
        "video": "video/mp4",
        "image": "image/png",
        "audio": "audio/mpeg",
    }
    fallback_mime = mime_map.get(att_type, "application/octet-stream")

    # ===== 策略1: 通过 NapCat get_file API 获取本地文件路径 =====
    if file_id:
        local_path = _try_get_file_via_napcat(file_id, fallback_mime)
        if local_path:
            return local_path

    # ===== 策略2: 通过 NapCat download_file API 下载 =====
    if url and url.startswith("http"):
        downloaded = _try_download_via_napcat(url)
        if downloaded:
            return downloaded

    # ===== 策略3: 直接 HTTP 下载（带浏览器请求头）=====
    if url and url.startswith("http"):
        direct = _try_download_direct(url, fallback_mime)
        if direct:
            return direct

    # 全部失败：返回原始 URL 让后续流程尝试
    _log.warning(f"[QQ Attachment] 所有策略均失败, file={file_id[:30] if file_id else '无'}, "
                 f"url={url[:60] if url else '无'}")
    return url or None


def _try_get_file_via_napcat(file_id: str, fallback_mime: str = "application/octet-stream") -> Optional[str]:
    """策略1: 调用 NapCat 的 get_file API 获取本地缓存文件的 data URL。

    NapCat 会将收到的多媒体文件缓存在本地磁盘，
    get_file 返回的 file_info 中包含文件路径或 base64 数据。
    ncatbot 3.8.5 的签名: get_file_sync(file_id: str) → POST /get_file {file_id}
    """
    try:
        from nbot.commands import bot
        if not hasattr(bot, "api") or not bot.api:
            return None

        # 使用同步版本（ncatbot BotAPI 继承 SYNC_API_MIXIN）
        file_info = None
        try:
            file_info = bot.api.get_file_sync(file_id)
        except Exception as e:
            _log.debug(f"[QQ Attachment] get_file_sync 异常: {e}")
            return None

        if not file_info:
            return None

        # 返回值可能是 dict 或对象，统一按 dict 处理
        if isinstance(file_info, dict):
            info = file_info
        else:
            info = getattr(file_info, "__dict__", {}) or {}

        # 优先查找本地文件路径
        local_path = info.get("path") or info.get("file")
        if local_path and os.path.isfile(local_path):
            mime_type, _ = mimetypes.guess_type(local_path)
            mime_type = mime_type or fallback_mime
            with open(local_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            _log.info(
                f"[QQ Attachment] 策略1成功: NapCat本地路径={local_path}, "
                f"size={os.path.getsize(local_path)}, mime={mime_type}"
            )
            return f"data:{mime_type};base64,{b64}"

        # 某些版本可能返回 base64 数据或文件路径字符串
        file_data = info.get("data") or info.get("base64")
        if file_data:
            data_size = len(file_data) if isinstance(file_data, (str, bytes, bytearray)) else len(str(file_data))

            # 如果数据很小（<10KB），很可能是文件路径或元数据，不是真正的媒体内容
            # 视频文件通常 >10KB，图片也通常 >1KB
            if data_size < 1024:
                _log.warning(
                    f"[QQ Attachment] 策略1: get_file 返回数据过小 ({data_size} bytes)，"
                    f"可能是文件路径而非媒体内容，内容预览: {str(file_data)[:120]}"
                )
                # 尝试将返回值当作本地路径读取
                path_candidate = str(file_data).strip()
                if os.path.isfile(path_candidate):
                    mime_type, _ = mimetypes.guess_type(path_candidate)
                    mime_type = mime_type or fallback_mime
                    with open(path_candidate, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode("utf-8")
                    _log.info(
                        f"[QQ Attachment] 策略1成功: 将返回数据解析为本地路径={path_candidate}, "
                        f"size={os.path.getsize(path_candidate)}, mime={mime_type}"
                    )
                    return f"data:{mime_type};base64,{b64}"
                # 不是有效路径，返回 None 让后续策略处理
                return None

            _log.info(f"[QQ Attachment] 策略1成功: get_file返回内联数据, size={data_size}")
            if isinstance(file_data, (bytes, bytearray)):
                b64 = base64.b64encode(file_data).decode("utf-8")
                return f"data:{fallback_mime};base64,{b64}"
            else:
                return f"data:{fallback_mime};base64,{file_data}"

        _log.debug(f"[QQ Attachment] get_file 返回但无可用的 path/data: {info}")
        return None

    except ImportError:
        _log.debug("[QQ Attachment] 无法导入 bot（可能在纯 Web 模式运行）")
        return None
    except Exception as e:
        _log.debug(f"[QQ Attachment] 策略1失败: {e}")
        return None


def _try_download_via_napcat(url: str) -> Optional[str]:
    """策略2: 调用 NapCat 的 download_file API 下载文件。

    ncatbot 3.8.5 签名: download_file_sync(thread_count, headers, url=..., name=...)
    headers 是必填参数，传空 dict 让 NapCat 使用默认请求头。
    """
    try:
        from nbot.commands import bot
        if not hasattr(bot, "api") or not bot.api:
            return None

        _log.info(f"[QQ Attachment] 策略2: 尝试 NapCat download_file_sync...")
        result = bot.api.download_file_sync(thread_count=1, headers="", url=url)

        if not result:
            return None

        # 返回值可能是 dict 或对象
        if isinstance(result, dict):
            info = result
        elif isinstance(result, str) and os.path.isfile(result):
            # 直接返回了本地文件路径
            mime_type, _ = mimetypes.guess_type(result)
            mime_type = mime_type or "application/octet-stream"
            with open(result, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            _log.info(
                f"[QQ Attachment] 策略2成功: NapCat下载到本地={result}, "
                f"size={os.path.getsize(result)}"
            )
            return f"data:{mime_type};base64,{b64}"
        else:
            info = getattr(result, "__dict__", {}) or {}

        # 从返回值中提取路径或数据
        dl_path = info.get("path") or info.get("file") or info.get("filepath")
        if dl_path and os.path.isfile(dl_path):
            mime_type, _ = mimetypes.guess_type(dl_path)
            mime_type = mime_type or "application/octet-stream"
            with open(dl_path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            _log.info(
                f"[QQ Attachment] 策略2成功: download_file返回路径={dl_path}, "
                f"size={os.path.getsize(dl_path)}"
            )
            return f"data:{mime_type};base64,{b64}"

        _log.debug(f"[QQ Attachment] download_file 返回: {info}")
        return None

    except Exception as e:
        _log.debug(f"[QQ Attachment] 策略2失败: {e}")
        return None


def _try_download_direct(url: str, fallback_mime: str) -> Optional[str]:
    """策略3: 使用 requests 直接下载，带完整的浏览器请求头。"""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept": "*/*",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Referer": "https://q.qq.com/",
            "Origin": "https://q.qq.com",
        }
        _log.info(f"[QQ Attachment] 策略3: 直接HTTP下载: {url[:100]}...")
        resp = requests.get(url, headers=headers, timeout=60, stream=True, allow_redirects=True)

        # 记录重定向链（用于诊断）
        if len(resp.history) > 0:
            redirect_chain = [r.url for r in resp.history] + [resp.url]
            _log.info(f"[QQ Attachment] 重定向链 ({len(resp.history)} 次): "
                       f"{' -> '.join(r[:60] for r in redirect_chain)}")

        resp.raise_for_status()

        file_data = resp.content
        if not file_data:
            _log.warning(f"[QQ Attachment] 策略3: 下载内容为空")
            return None

        content_type = resp.headers.get("Content-Type", "")
        _log.info(f"[QQ Attachment] 策略3: 下载完成 {len(file_data)} bytes, Content-Type={content_type}")

        # 校验：如果响应是 HTML 而非二进制媒体，说明被重定向到了错误页面
        if content_type.startswith("text/") or content_type.startswith("application/html"):
            head_preview = file_data[:200].decode("utf-8", errors="replace")
            _log.warning(
                f"[QQ Attachment] 策略3: 响应为HTML而非媒体文件。"
                f"Content-Type={content_type}, 预览: {head_preview[:120]}"
            )
            return None

        # 从响应头获取准确的 MIME 类型
        mime_type = fallback_mime
        if content_type and "/" in content_type:
            mime_type = content_type.split(";")[0].strip()

        b64 = base64.b64encode(file_data).decode("utf-8")
        _log.info(f"[QQ Attachment] 策略3成功: type={mime_type}, data_size={len(b64)} chars")
        return f"data:{mime_type};base64,{b64}"

    except Exception as e:
        _log.warning(f"[QQ Attachment] 策略3失败: {e}")
        return None


def _resolve_web_attachment(attachment: Dict[str, Any]) -> Optional[str]:
    """Web 频道专用附件解析：将本地路径/相对 URL 转为视觉模型可访问的地址。"""
    data = attachment.get("data")
    if data:
        return data

    path = attachment.get("path")
    if path and os.path.isfile(path):
        try:
            mime_type, _ = mimetypes.guess_type(path)
            mime_type = mime_type or "application/octet-stream"
            with open(path, "rb") as f:
                b64 = base64.b64encode(f.read()).decode("utf-8")
            return f"data:{mime_type};base64,{b64}"
        except Exception as exc:
            _log.warning("Failed to read web attachment %r as data URL: %s", path, exc)

    url = attachment.get("url") or attachment.get("download_url")
    if url:
        if url.startswith("/"):
            try:
                from nbot.web.file_gateway import get_public_base_url
                from nbot.web.server import WebChatServer

                server = WebChatServer.get_instance()
                if server:
                    base_url = get_public_base_url(server)
                    if base_url:
                        return f"{base_url.rstrip('/')}{url}"
            except Exception as exc:
                _log.warning("Failed to resolve base URL for web attachment: %s", exc)
        return url

    return None


AttachmentResolver.register("web", _resolve_web_attachment)
AttachmentResolver.register("qq", _resolve_qq_attachment)
AttachmentResolver.register("qq_private", _resolve_qq_attachment)
AttachmentResolver.register("qq_group", _resolve_qq_attachment)
AttachmentResolver.register("telegram", _resolve_telegram_attachment)
AttachmentResolver.register("feishu", _resolve_feishu_attachment)
AttachmentResolver.register("feishu_ws", _resolve_feishu_attachment)

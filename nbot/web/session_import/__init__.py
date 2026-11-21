"""会话导入：nekobot 导出 JSON 与 DeepSeek 官方数据导出 ZIP。

模块划分::

    formats.py     导入格式注册表（界面按注册顺序渲染）
    deepseek.py    DeepSeek 官方导出 ZIP 的读取与消息还原
    url_policy.py  下载链接的公网 HTTPS 校验（防 SSRF）
    downloader.py  归档流式下载（逐跳校验重定向、限制体积）
    staging.py     归档临时落盘与一次性 token
    payload.py     归一化后落库（所有导入来源共用）
"""

from nbot.web.session_import.deepseek import DeepSeekArchiveReader, ImportableConversation
from nbot.web.session_import.errors import (
    ArchiveExpired,
    ArchiveTooLarge,
    DownloadFailed,
    InvalidUrl,
    SessionImportError,
    UnsupportedArchive,
)
from nbot.web.session_import.formats import (
    SESSION_IMPORT_FORMATS,
    SessionImportFormat,
    get_session_import_format,
    list_session_import_formats,
)
from nbot.web.session_import.payload import (
    extract_export_sessions,
    import_export_payload,
    normalize_imported_session,
)
from nbot.web.session_import.staging import ArchiveStaging, StagedArchive, get_staging

__all__ = [
    "ArchiveExpired",
    "ArchiveStaging",
    "ArchiveTooLarge",
    "DeepSeekArchiveReader",
    "DownloadFailed",
    "ImportableConversation",
    "InvalidUrl",
    "SESSION_IMPORT_FORMATS",
    "SessionImportError",
    "SessionImportFormat",
    "StagedArchive",
    "UnsupportedArchive",
    "extract_export_sessions",
    "get_session_import_format",
    "get_staging",
    "import_export_payload",
    "list_session_import_formats",
    "normalize_imported_session",
]

"""会话导入过程中可预期的失败。

异常只携带诊断信息，路由层按类型映射成 400 响应，避免把内部堆栈暴露到前端。
"""


class SessionImportError(ValueError):
    """会话导入失败的基类（路由层按 400 返回）。"""


class InvalidUrl(SessionImportError):
    """链接不是可用的 HTTPS 公网地址。"""

    def __init__(self, detail: str = "") -> None:
        message = "链接无效，只支持公网 HTTPS 地址"
        if detail:
            message = f"{message}（{detail}）"
        super().__init__(message)
        self.detail = detail


class DownloadFailed(SessionImportError):
    """下载失败（HTTP 错误、连接中断等）。"""

    def __init__(self, reason: str) -> None:
        super().__init__(f"下载失败：{reason}")
        self.reason = reason


class ArchiveTooLarge(SessionImportError):
    """归档或单个会话超出可安全读取的体积上限。"""

    def __init__(self, message: str = "文件过大，已停止导入") -> None:
        super().__init__(message)


class UnsupportedArchive(SessionImportError):
    """文件不是该格式的会话归档（或版本无法识别）。"""

    def __init__(self, format_id: str, detail: str) -> None:
        super().__init__(f"不是有效的 {format_id} 会话归档：{detail}")
        self.format_id = format_id
        self.detail = detail


class ArchiveExpired(SessionImportError):
    """暂存的归档已被清理或过期，需要重新上传。"""

    def __init__(self, message: str = "导入归档已过期，请重新上传") -> None:
        super().__init__(message)

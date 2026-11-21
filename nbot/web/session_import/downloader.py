"""从下载链接获取会话导出归档（例如 DeepSeek 官方数据导出的对象存储链接）。

链接由用户粘贴，属于不可信输入：

- 只接受 HTTPS + 公网主机（解析域名时校验，拦截 ``https://127.0.0.1`` 这类内网探测）；
- 重定向逐跳校验，避免公网地址跳转到内网；
- 下载体积有上限，超限即中止并删除半成品文件。

下载到指定路径后，解析与清理由调用方负责。
"""

from __future__ import annotations

import os
from collections.abc import Callable
from urllib.parse import urljoin

import requests

from nbot.web.session_import.errors import ArchiveTooLarge, DownloadFailed
from nbot.web.session_import.url_policy import require_public_https_url

REDIRECT_CODES = frozenset({301, 302, 303, 307, 308})
MAX_REDIRECTS = 5
_READ_CHUNK = 64 * 1024
_DEFAULT_TIMEOUT = (30.0, 180.0)


def download_archive(
    url: str,
    target_path: str,
    *,
    max_bytes: int,
    timeout: tuple[float, float] = _DEFAULT_TIMEOUT,
    validate: Callable[[str], str] = require_public_https_url,
) -> int:
    """把 ``url`` 指向的归档流式下载到 ``target_path``，返回写入的字节数。

    Args:
        url: 用户提供的下载链接（不可信）。
        target_path: 本地目标文件路径（不存在时创建，存在时覆盖）。
        max_bytes: 允许的最大体积，超出即中止。
        timeout: ``requests`` 的 (连接, 读取) 超时。
        validate: 地址校验函数，默认只允许公网 HTTPS；测试可替换。

    Raises:
        InvalidUrl: 链接非法或指向内网。
        DownloadFailed: HTTP 错误、重定向异常或响应为空。
        ArchiveTooLarge: 下载体积超出上限。
    """
    current = validate(url)
    redirects = 0
    written = 0
    try:
        with requests.Session() as session:
            while True:
                response = session.get(current, stream=True, allow_redirects=False, timeout=timeout)
                with response:
                    status = response.status_code
                    if status in REDIRECT_CODES:
                        redirects += 1
                        if redirects > MAX_REDIRECTS:
                            raise DownloadFailed("重定向次数过多")
                        location = response.headers.get("Location")
                        if not location:
                            raise DownloadFailed("重定向缺少 Location")
                        current = validate(urljoin(current, location))
                        continue
                    if status != 200:
                        raise DownloadFailed(f"HTTP {status}")
                    declared = response.headers.get("Content-Length")
                    if declared and declared.isdigit() and int(declared) > max_bytes:
                        raise ArchiveTooLarge()
                    with open(target_path, "wb") as handle:
                        for chunk in response.iter_content(_READ_CHUNK):
                            if not chunk:
                                continue
                            written += len(chunk)
                            if written > max_bytes:
                                raise ArchiveTooLarge()
                            handle.write(chunk)
                    break
    except Exception:
        _remove_quietly(target_path)
        raise

    if written == 0:
        _remove_quietly(target_path)
        raise DownloadFailed("响应内容为空")
    return written


def _remove_quietly(path: str) -> None:
    try:
        if os.path.isfile(path):
            os.remove(path)
    except OSError:  # pragma: no cover - 清理失败不影响主流程
        pass

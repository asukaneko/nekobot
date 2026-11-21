"""会话归档下载链接的公网校验（防 SSRF）。

下载链接由用户粘贴，属于不可信输入：只接受 HTTPS + 公网主机，拦截
``https://127.0.0.1`` / ``https://metadata.internal`` 这类内网探测入口。
重定向的逐跳校验在 :mod:`nbot.web.session_import.downloader` 里完成。
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlsplit

from nbot.web.session_import.errors import InvalidUrl

_ALLOWED_SCHEMES = frozenset({"https"})
_LOCAL_SUFFIXES = (".localhost", ".local", ".internal")


def is_public_ip(address: str) -> bool:
    """判断地址是否为公网可路由 IP（拒绝内网/回环/链路本地/保留/组播）。"""
    try:
        ip = ipaddress.ip_address(str(address).split("%", 1)[0])
    except ValueError:
        return False
    return not (
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )


def require_public_https_url(url: str) -> str:
    """校验下载链接，返回去掉首尾空白后的地址。

    Raises:
        InvalidUrl: 非 HTTPS、主机为本地/内网、端口非法或域名无法解析。
    """
    text = str(url or "").strip()
    if not text:
        raise InvalidUrl("缺少链接")

    parsed = urlsplit(text)
    if (parsed.scheme or "").lower() not in _ALLOWED_SCHEMES:
        raise InvalidUrl("只支持 HTTPS")
    host = (parsed.hostname or "").strip().lower()
    if not host:
        raise InvalidUrl("缺少主机名")
    if host == "localhost" or host.endswith(_LOCAL_SUFFIXES):
        raise InvalidUrl(f"不允许访问本机地址 {host}")
    try:
        port = parsed.port or 443
    except ValueError as exc:
        raise InvalidUrl("端口非法") from exc

    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, OSError, UnicodeError) as exc:
        raise InvalidUrl(f"无法解析主机 {host}") from exc
    addresses = {info[4][0] for info in infos if info[4]}
    if not addresses:
        raise InvalidUrl(f"无法解析主机 {host}")
    for address in addresses:
        if not is_public_ip(address):
            raise InvalidUrl(f"不允许访问内网地址 {host}")
    return text

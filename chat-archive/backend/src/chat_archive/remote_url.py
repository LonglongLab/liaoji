"""远程服务 base URL 的规范化与安全校验（FR-REMOTE-005/007、NFR-SEC-004）。

所有携带设备令牌的出站 transport（配对、同步、附件）共用同一规则：
- 必须是绝对 HTTP(S) URL；
- 不得包含凭据、query 或 fragment（避免令牌/密钥混入 URL 或被日志记录）；
- 非环回主机强制 HTTPS，只有本地环回地址允许明文 http（本地 wrangler dev / 自建单机）。
"""

from __future__ import annotations

import urllib.parse

_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1"}


def validate_remote_base_url(base_url: str) -> str:
    """校验并返回去掉尾部斜杠的规范化 base URL；非法输入抛 ValueError。"""
    parsed = urllib.parse.urlparse(base_url)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("remote base URL must be an absolute HTTP(S) URL")
    if parsed.query or parsed.fragment or parsed.username is not None or parsed.password is not None:
        raise ValueError("remote base URL must not contain credentials, query, or fragment")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("remote base URL must use HTTPS for non-loopback hosts")
    return base_url.rstrip("/")


def validate_storage_origin(origin: str) -> tuple[str, str]:
    """对象存储 origin 的同一套规则；返回 `(scheme, netloc)`。

    直传（PSU-2.49）是全仓第一处把字节发往**设备令牌那个主机之外**的地方，
    所以这里刻意与上面同规：非环回强制 HTTPS，不许带凭据、路径、query 或
    fragment。签名响应被篡改时，客户端据此拒收而不是照着发。
    """
    parsed = urllib.parse.urlparse(origin)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("storage origin must be an absolute HTTP(S) origin")
    if (
        parsed.path
        or parsed.query
        or parsed.fragment
        or parsed.username is not None
        or parsed.password is not None
    ):
        raise ValueError("storage origin must be scheme and host only")
    if parsed.scheme == "http" and parsed.hostname not in _LOOPBACK_HOSTS:
        raise ValueError("storage origin must use HTTPS for non-loopback hosts")
    return parsed.scheme, parsed.netloc

"""远程服务错误契约与安全头；语义镜像 Worker `http.ts`。"""

from __future__ import annotations

from collections.abc import Mapping

from fastapi import Response
from fastapi.responses import JSONResponse

REMOTE_ERROR_STATUS: dict[str, int] = {
    "UNAUTHORIZED": 401,
    "NOT_FOUND": 404,
    "QUERY_INVALID": 400,
    "METHOD_NOT_ALLOWED": 405,
    "SYNC_CONFLICT": 409,
    "BLOB_CONFLICT": 409,
    "BLOB_SOURCE_CHANGED": 409,
    "BLOB_INTEGRITY": 422,
    "SERVICE_UNAVAILABLE": 503,
    # 「远程忙，稍后再来」，与「远程不可用」分开（PSU-2.98）。自托管这一支
    # 独有：Worker 那支的写者是 D1，没有这把进程内的单写者锁，所以它的
    # `RemoteErrorCode` 里没有这一项——与四个 OBJECT_STORAGE_* 同一条先例。
    "REMOTE_BUSY": 503,
    "OBJECT_STORAGE_NOT_CONFIGURED": 409,
    "OBJECT_STORAGE_CONFIGURATION_INVALID": 400,
    "OBJECT_STORAGE_VERIFICATION_FAILED": 422,
    "OBJECT_STORAGE_GENERATION_MISMATCH": 409,
    # 定时备份（PSU-2.140/2.141）。与四个 OBJECT_STORAGE_* 同一条先例：
    # 只有自托管这一支有文件系统和长任务，Worker 那支答不出备份这件事。
    "BACKUP_NOT_CONFIGURED": 409,
    "BACKUP_CONFIGURATION_INVALID": 400,
    "BACKUP_RETENTION_NEEDS_DELETE": 400,
    "BACKUP_CREDENTIALS_UNREADABLE": 409,
    "BACKUP_NOT_AVAILABLE": 409,
    "BACKUP_VERIFICATION_FAILED": 422,
    "BACKUP_UNAVAILABLE": 409,
    # 在线分享（PSU-2.181）。只有自托管 SQLite 这一支注册分享路由。
    "SHARE_NOT_FOUND": 404,
    "SHARE_LIMIT_EXCEEDED": 413,
    "SHARE_INCOMPLETE": 409,
    "SHARE_STATE_INVALID": 409,
    "SHARE_UPLOAD_MISMATCH": 400,
    "SHARE_STORAGE_FAILED": 503,
    "SHARE_DELETE_FAILED": 503,
}

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": "default-src 'none'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cache-Control": "no-store",
}


class RemoteServerError(Exception):
    def __init__(self, code: str, message: str, *, retryable: bool = False) -> None:
        if code not in REMOTE_ERROR_STATUS:
            raise ValueError(f"unknown remote error code: {code}")
        super().__init__(code)
        self.code = code
        self.message = message
        self.retryable = retryable


def apply_security_headers(response: Response, *, overwrite: bool = True) -> None:
    """给响应加上这一组安全头。

    `overwrite=False` 让已经自己表过态的响应保留自己的值——静态托管的 SPA
    需要一份不同的 CSP 与缓存策略（`server/spa.py`），而只回 JSON 的 API
    仍然拿这里这份 `default-src 'none'`。
    """
    for name, value in SECURITY_HEADERS.items():
        if overwrite or name not in response.headers:
            response.headers[name] = value


def remote_error_response(
    error: RemoteServerError, *, headers: Mapping[str, str] | None = None
) -> JSONResponse:
    """``headers`` 是给 ``Retry-After`` 那一类「多久之后再来」用的（PSU-2.98）。

    安全头在它之后覆盖，所以调用方给不出一个能放宽 CSP 的头。
    """
    response = JSONResponse(
        status_code=REMOTE_ERROR_STATUS[error.code],
        content={"error": {"code": error.code, "message": error.message, "retryable": error.retryable}},
        headers=None if headers is None else dict(headers),
    )
    apply_security_headers(response)
    return response

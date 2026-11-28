"""Streaming ASGI request body size limit middleware for the self-hosted remote server."""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

from fastapi.responses import JSONResponse, Response

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.server.error_contract import (
    archive_error_response,
    speaks_local_error_contract,
)
from chat_archive.server.http import (
    RemoteServerError,
    apply_security_headers,
    remote_error_response,
)

if TYPE_CHECKING:
    from starlette.types import ASGIApp, Message, Receive, Scope, Send

_METHODS_WITH_BODY = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_LIMIT_TINY_BYTES = 64 * 1024
_LIMIT_AUTH_BYTES = 256 * 1024
_LIMIT_DEFAULT_BYTES = 1 * 1024 * 1024
_LIMIT_SHARE_DRAFT_BYTES = 4 * 1024 * 1024
_LIMIT_SYNC_BYTES = 16 * 1024 * 1024
_LIMIT_BLOB_BYTES = 32 * 1024 * 1024
_LIMIT_SHARE_OBJECT_BYTES = 205 * 1024 * 1024

_SHARE_OBJECT_RE = re.compile(
    r"^/api/v1/(?:archives/[^/]+|remote)/shares/[^/]+/objects/[^/]+$"
)
_SHARE_DRAFT_RE = re.compile(r"^/api/v1/(?:archives/[^/]+|remote)/shares(?:/.*)?$")
_ARCHIVE_BLOB_RE = re.compile(r"^/api/v1/archives/[^/]+/blobs(?:/.*)?$")
_AUTH_SMALL_PREFIXES = (
    "/api/v1/auth/",
    "/api/v1/agent-access",
    "/api/v1/remote/pairing-codes",
    "/api/v1/remote/object-storage/",
    "/api/v1/remote/backup/",
    "/api/v1/remote/sessions/",
    "/api/v1/remote/devices/",
)


def body_limit_for_path(path: str) -> int:
    """Return the maximum permitted request body byte count for a given request path."""
    if path in {"/api/v1/pair", "/oauth/token", "/oauth/register"}:
        return _LIMIT_TINY_BYTES
    if path == "/api/v1/agent-access" or path.startswith(_AUTH_SMALL_PREFIXES):
        return _LIMIT_AUTH_BYTES
    if _SHARE_OBJECT_RE.match(path):
        return _LIMIT_SHARE_OBJECT_BYTES
    if _SHARE_DRAFT_RE.match(path):
        return _LIMIT_SHARE_DRAFT_BYTES
    if _ARCHIVE_BLOB_RE.match(path):
        return _LIMIT_BLOB_BYTES
    if path.startswith(("/api/v1/archives/", "/api/v1/remote/", "/mcp")):
        return _LIMIT_SYNC_BYTES
    return _LIMIT_DEFAULT_BYTES


class _PayloadTooLargeError(Exception):
    pass


def _error_response(path: str, status: int) -> Response:
    if path.startswith("/oauth/"):
        response = JSONResponse(
            {"error": "invalid_request"},
            status_code=status,
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )
        apply_security_headers(response, overwrite=False)
        return response
    if speaks_local_error_contract(path):
        code = ErrorCode.PAYLOAD_TOO_LARGE if status == 413 else ErrorCode.QUERY_INVALID
        response = archive_error_response(ArchiveError(code))
        apply_security_headers(response)
        return response
    if status == 413:
        return remote_error_response(
            RemoteServerError("PAYLOAD_TOO_LARGE", "请求体超过允许大小。")
        )
    return remote_error_response(
        RemoteServerError("QUERY_INVALID", "Content-Length 必须是非负整数。")
    )


class RequestBodyLimitMiddleware:
    """Enforces route-tiered request body caps for both Content-Length and streamed bodies."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        method = str(scope.get("method", "GET")).upper()
        if method not in _METHODS_WITH_BODY:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path", "/"))
        limit = body_limit_for_path(path)
        for name, value in scope.get("headers", ()):
            if name.lower() == b"content-length":
                try:
                    declared = int(value.decode("ascii"))
                except (UnicodeDecodeError, ValueError):
                    await _error_response(path, 400)(scope, receive, send)
                    return
                if declared < 0:
                    await _error_response(path, 400)(scope, receive, send)
                    return
                if declared > limit:
                    await _error_response(path, 413)(scope, receive, send)
                    return

        received = 0
        exceeded = False
        response_started = False

        async def tracked_send(message: Message) -> None:
            nonlocal response_started
            if exceeded:
                return
            if message.get("type") == "http.response.start":
                response_started = True
            await send(message)

        async def bounded_receive() -> Message:
            nonlocal received, exceeded
            message = await receive()
            if message.get("type") == "http.request":
                body = message.get("body", b"")
                if isinstance(body, (bytes, bytearray)):
                    received += len(body)
                    if received > limit:
                        exceeded = True
                        raise _PayloadTooLargeError()
            return message

        try:
            await self.app(scope, bounded_receive, tracked_send)
        except Exception:
            if not exceeded:
                raise
        if exceeded and not response_started:
            await _error_response(path, 413)(scope, receive, send)


__all__ = ["RequestBodyLimitMiddleware", "body_limit_for_path"]

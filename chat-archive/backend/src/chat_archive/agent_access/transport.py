"""Stateless JSON responses over MCP Streamable HTTP."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Iterator
from urllib.parse import quote

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import TypeAdapter, ValidationError
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect

from chat_archive.agent_access.policy import Grant
from chat_archive.agent_access.tools import TOOLS, Document, QueryTools, ToolError

_OBJECT = TypeAdapter(dict[str, object])
_VERSIONS = ("2025-11-25", "2025-06-18")
_MAX_REQUEST = 32 * 1024
_PRIVATE = {
    "Cache-Control": "no-store", "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}
Authenticate = Callable[[str], tuple[str, Grant] | None]
IsCurrent = Callable[[str, tuple[str, Grant]], bool]


class AuthorizationUnavailable(Exception):
    """A valid shared connection is paused until its policy can be verified."""


def _bearer(request: Request) -> str | None:
    scheme, _, value = request.headers.get("authorization", "").partition(" ")
    if scheme.lower() != "bearer" or not 1 <= len(value) <= 512 or not value.isascii():
        return None
    return value


def _error(code: int, message: str, request_id: object = None, status: int = 200) -> JSONResponse:
    return JSONResponse(
        {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}},
        status_code=status, headers=_PRIVATE,
    )


def register_transport(
    app: FastAPI,
    tools: QueryTools,
    *,
    authorize: Authenticate,
    origin_for: Callable[[Request], str | None],
    stream_current: IsCurrent | None = None,
    challenge: str | Callable[[str], str] = "Bearer",
) -> None:
    @app.exception_handler(AuthorizationUnavailable)
    def policy_unavailable(_request: Request, _error: AuthorizationUnavailable) -> Response:
        return JSONResponse({"error": "authorization_temporarily_unavailable"}, status_code=503,
                            headers={**_PRIVATE, "Retry-After": "5"})

    def unauthenticated(origin: str) -> Response:
        header = challenge(origin) if callable(challenge) else challenge
        return Response(status_code=401, headers={**_PRIVATE, "WWW-Authenticate": header})

    def identity(request: Request) -> tuple[str, Grant] | None:
        token = _bearer(request)
        return authorize(token) if token is not None else None

    def current(request: Request, principal: tuple[str, Grant]) -> bool:
        try:
            return identity(request) == principal
        except AuthorizationUnavailable:
            return False

    @app.api_route("/mcp", methods=["GET", "POST", "DELETE"], include_in_schema=False)
    async def mcp(request: Request) -> Response:
        origin = await run_in_threadpool(origin_for, request)
        if origin is None:
            return Response(status_code=403, headers=_PRIVATE)
        principal = await run_in_threadpool(identity, request)
        if principal is None:
            return unauthenticated(origin)
        if request.method != "POST":
            return Response(status_code=405, headers={**_PRIVATE, "Allow": "POST"})
        accept = request.headers.get("Accept", "*/*")
        accepted = {item.partition(";")[0].strip().lower() for item in accept.split(",")}
        if not accepted & {"application/json", "application/*", "*/*"}:
            return Response(status_code=406, headers=_PRIVATE)
        version = request.headers.get("MCP-Protocol-Version")
        if version is not None and version not in _VERSIONS:
            return _error(-32600, "Unsupported protocol version", status=400)
        content_type = request.headers.get("Content-Type", "").split(";", 1)[0].lower()
        if content_type != "application/json":
            return Response(status_code=415, headers=_PRIVATE)
        body = bytearray()
        try:
            async for chunk in request.stream():
                if len(body) + len(chunk) > _MAX_REQUEST:
                    return Response(status_code=413, headers=_PRIVATE)
                body.extend(chunk)
        except ClientDisconnect:
            return Response(status_code=400, headers=_PRIVATE)
        try:
            rpc = _OBJECT.validate_json(body)
        except ValidationError as exc:
            code = -32700 if exc.errors()[0]["type"] == "json_invalid" else -32600
            return _error(code, "Invalid JSON-RPC request", status=400)
        request_id = rpc.get("id")
        method = rpc.get("method")
        if (
            rpc.get("jsonrpc") != "2.0" or not isinstance(method, str)
            or ("id" in rpc and not isinstance(request_id, (str, int)))
            or isinstance(request_id, bool)
        ):
            return _error(-32600, "Invalid JSON-RPC request", status=400)
        params = rpc.get("params", {})
        if "id" not in rpc:
            if method.startswith("notifications/"):
                return Response(status_code=202, headers=_PRIVATE)
            return _error(-32600, "Invalid JSON-RPC notification", status=400)
        if not isinstance(params, dict):
            return _error(-32602, "Invalid parameters", request_id)
        parameters = _OBJECT.validate_python(params)
        result: Document
        if method == "initialize":
            proposed = parameters.get("protocolVersion")
            if not isinstance(proposed, str):
                return _error(-32602, "Invalid protocol version", request_id)
            result = {
                "protocolVersion": proposed if proposed in _VERSIONS else _VERSIONS[0],
                "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "liaoji", "version": "1.0.0"},
            }
        elif method == "ping":
            result = {}
        elif method == "tools/list":
            result = {"tools": [
                {
                    "name": name, "description": tool.description,
                    "inputSchema": tool.arguments.model_json_schema(),
                    "outputSchema": {
                        "type": "object", "required": ["source"],
                        "properties": {"source": {"enum": ["local", "remote"]}},
                    },
                    "annotations": {
                        "readOnlyHint": True, "destructiveHint": False,
                        "idempotentHint": True, "openWorldHint": False,
                    },
                }
                for name, tool in TOOLS.items()
            ]}
        elif method == "tools/call":
            name = parameters.get("name")
            if not isinstance(name, str):
                return _error(-32602, "Invalid tool name", request_id)
            image = None
            try:
                value, image = await run_in_threadpool(
                    tools.call, name, parameters.get("arguments", {}), *principal, origin,
                )
            except ValidationError:
                return _error(-32602, "Invalid tool arguments", request_id)
            except ToolError as exc:
                value = {"error": exc.code}
            except (OSError, sqlite3.Error):
                value = {"error": "unavailable"}
            if await run_in_threadpool(identity, request) != principal:
                return unauthenticated(origin)
            value["source"] = tools.reader.source
            content = [{"type": "text", "text": json.dumps(value, ensure_ascii=False)}]
            result = {"structuredContent": value, "content": content, "isError": "error" in value}
            if image is not None:
                result["content"] = [*content, image]
        else:
            return _error(-32601, "Method not found", request_id)
        return JSONResponse({"jsonrpc": "2.0", "id": request_id, "result": result}, headers=_PRIVATE)

    @app.get("/mcp/files/{ticket}", include_in_schema=False)
    def download(request: Request, ticket: str) -> Response:
        origin = origin_for(request)
        if origin is None:
            return Response(status_code=403, headers=_PRIVATE)
        principal = identity(request)
        if principal is None:
            return unauthenticated(origin)
        try:
            artifact = tools.download(*principal, ticket)
        except (ToolError, OSError, sqlite3.Error):
            return Response(status_code=404, headers=_PRIVATE)
        stream_factory = artifact.open_bytes
        assert stream_factory is not None
        stream = stream_factory()
        try:
            first = next(stream, None)
        except OSError:
            return Response(status_code=404, headers=_PRIVATE)
        if identity(request) != principal:
            getattr(stream, "close", lambda: None)()
            return unauthenticated(origin)

        def chunks() -> Iterator[bytes]:
            try:
                if first is not None and current(request, principal):
                    yield first
                else:
                    return
                for chunk in stream:
                    token = _bearer(request)
                    if token is None or not (
                        stream_current(token, principal) if stream_current is not None
                        else current(request, principal)
                    ):
                        break
                    yield chunk
            finally:
                getattr(stream, "close", lambda: None)()

        headers = {
            **_PRIVATE,
            "Content-Disposition": f"attachment; filename*=UTF-8''{quote(artifact.name or 'attachment', safe='')}",
        }
        if artifact.size is not None and artifact.size >= 0:
            headers["Content-Length"] = str(artifact.size)
        return StreamingResponse(chunks(), media_type="application/octet-stream", headers=headers)

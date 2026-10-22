"""分享的公开读取（PSU-2.180）：远端上唯一一条**不要任何身份**的接口。

打不开的四种——过期、已删、不存在、还是草稿——回的是**同一个**响应，逐字节相同：
同一个状态码、同一份响应体、同一组头。拿着一个旧链接的人不该知道它是过期了还是被删了，
更不该知道原来是哪个会话；试探令牌的人也得不到「这个形状对了」之类的任何反馈。

成功的响应里只有渲染要用的东西：标题、条数、时间、快照，和每个附件一个 5 分钟的
预签名地址。没有归档 id、没有打开次数（那是分享的人在「我的分享」里看的）。
"""

# pyright: reportUnusedFunction=false
# FastAPI route functions are registered through decorators.

from __future__ import annotations

import json
import time
from collections.abc import Callable
from pathlib import Path

from fastapi import FastAPI, Request, Response
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.ports.objectstore import (
    ObjectStore,
    ObjectStoreError,
    ObjectStoreValidationError,
)
from chat_archive.server.auth import authenticate_request
from chat_archive.server.blob_routes import PRESIGNED_GET_TTL_SECONDS
from chat_archive.server.error_contract import archive_error_response
from chat_archive.server.http import RemoteServerError
from chat_archive.server.share_service import ShareService, share_payload
from chat_archive.server.shares import SHARE_OBJECT_SCOPE, Share, SqliteShareStore
from chat_archive.http_body import read_bounded_body, read_draft_json
from chat_archive.share_draft import MAX_OBJECT_BYTES, ShareDraftError

PUBLIC_SHARE_PATH = "/api/v1/public/shares/{token}"


# 两种响应都带。`no-store`：分享随时可能被删，任何一层缓存留着它都是在替人续命；
# `noindex`：链接被贴到公开网页上也不进搜索结果；`no-referrer`：点开附件时不把
# 这个带令牌的地址递给对象存储。
PUBLIC_SHARE_HEADERS: dict[str, str] = {
    "Cache-Control": "no-store",
    "X-Robots-Tag": "noindex, nofollow",
    "Referrer-Policy": "no-referrer",
    "X-Content-Type-Options": "nosniff",
}


def _with_headers(response: Response) -> Response:
    for name, value in PUBLIC_SHARE_HEADERS.items():
        response.headers[name] = value
    return response


public_share_response = _with_headers


def share_gone_response() -> Response:
    return _with_headers(archive_error_response(ArchiveError(ErrorCode.SHARE_GONE)))


def _attachments(share: Share, objects: ObjectStore) -> list[dict[str, object]]:
    """每个已落地的附件一个短时地址。签不出来就给 `null`：文字照样看得到。"""
    signed_until = max(1, int(time.time())) + PRESIGNED_GET_TTL_SECONDS
    items: list[dict[str, object]] = []
    for item in share.objects:
        if not item.uploaded:
            continue
        try:
            presigned = objects.presign_get(
                SHARE_OBJECT_SCOPE,
                item.object_key,
                expires_in_seconds=PRESIGNED_GET_TTL_SECONDS,
            )
        except (ObjectStoreError, ObjectStoreValidationError):
            # 后者包括「这个后端根本不会签」（本地文件系统）：同样只是没有地址。
            url: str | None = None
            expires_at: int | None = None
        else:
            url = presigned.url
            expires_at = signed_until
        items.append(
            {
                "attachment_id": item.attachment_id,
                "mime_type": item.mime_type,
                "size": item.size,
                "url": url,
                "expires_at": expires_at,
            }
        )
    return items


async def _bounded(request: Request, limit: int) -> bytes:
    try:
        return await read_bounded_body(request, limit)
    except ShareDraftError as error:
        raise RemoteServerError(error.code, "请求体不合格。") from None


async def _draft(request: Request) -> object:
    try:
        return share_payload(await read_draft_json(request))
    except ShareDraftError as error:
        raise RemoteServerError(error.code, "请求体不合格。") from None


def register_device_share_routes(
    app: FastAPI,
    auth_store: DeviceAuthStore,
    service: ShareService,
) -> None:
    """配对设备（本机）那一半：设备令牌、三字段错误信封（`remote-openapi.yaml`）。"""

    base = "/api/v1/archives/{archive_id}/shares"

    @app.post(base)
    async def create_share(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        payload = await _draft(request)
        return await run_in_threadpool(service.create, archive_id, payload)

    @app.get(base)
    async def list_shares(archive_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return {"items": await run_in_threadpool(service.list, archive_id)}

    @app.put(base + "/{token}/objects/{attachment_id}")
    async def upload_share_object(
        archive_id: str, token: str, attachment_id: str, request: Request
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        content = await _bounded(request, MAX_OBJECT_BYTES)
        await run_in_threadpool(service.upload, archive_id, token, attachment_id, content)
        return {"uploaded": True}

    @app.post(base + "/{token}/finalize")
    async def finalize_share(archive_id: str, token: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await run_in_threadpool(service.finalize, archive_id, token)

    @app.post(base + "/{token}/extend")
    async def extend_share(archive_id: str, token: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        return await run_in_threadpool(service.extend, archive_id, token)

    @app.delete(base + "/{token}")
    async def delete_share(archive_id: str, token: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        await run_in_threadpool(service.delete, archive_id, token)
        return {"deleted": True}


def register_share_routes(
    app: FastAPI,
    store: SqliteShareStore,
    objects: ObjectStore,
    *,
    frontend_dist: Path | None = None,
    storage_origin: Callable[[], str | None] = lambda: None,
) -> None:
    @app.get(PUBLIC_SHARE_PATH)
    def read_public_share(token: str, request: Request) -> Response:
        # The HTML bootstrap already counted the page. This flag only affects statistics;
        # it never skips the live/expiry check, and an expired bootstrap cannot restore access.
        share = store.open_public(token, count_view=False)
        if share is None:
            return share_gone_response()
        try:
            snapshot: object = json.loads(share.snapshot_json)
            from chat_archive.server.share_text import snapshot_messages

            snapshot_messages(share)
        except ValueError:
            # 快照读不出来，这份分享就给不出任何能看的东西——对读者来说与失效无异。
            return share_gone_response()
        if request.query_params.get("count_view") != "false" and store.open_public(token) is None:
            return share_gone_response()
        body = {
            "title": share.title,
            "message_count": share.message_count,
            "created_at": share.created_at,
            "expires_at": share.expires_at,
            "snapshot": snapshot,
            "attachments": _attachments(share, objects),
        }
        return _with_headers(JSONResponse(body))

    from chat_archive.server.share_agent_routes import register_share_agent_routes

    register_share_agent_routes(app, store, objects, frontend_dist=frontend_dist,
                                storage_origin=storage_origin)


__all__ = [
    "PUBLIC_SHARE_HEADERS",
    "PUBLIC_SHARE_PATH",
    "register_device_share_routes",
    "register_share_routes",
    "public_share_response",
    "share_gone_response",
]

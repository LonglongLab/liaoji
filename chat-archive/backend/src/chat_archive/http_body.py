"""按声明的大小在**读之前**拦住过大的请求体（PSU-2.181）。

三处要它——本机 app、远端的设备令牌路由、远端的 BFF——而三处的错误信封不一样，
所以这里只抛带稳定代号的 `ShareDraftError`，由各端翻成自己的那一份。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from starlette.requests import Request

from chat_archive.share_draft import ShareDraftError

# 建草稿那一份 JSON 的上限：2 MB 的快照加上附件清单与信封，留足余量。
MAX_DRAFT_BODY_BYTES = 3 * 1024 * 1024


def _declared_length(request: Request, limit: int) -> int:
    raw = request.headers.get("content-length")
    try:
        declared = int(raw) if raw is not None else None
    except ValueError:
        declared = None
    if declared is None or declared < 0:
        raise ShareDraftError("QUERY_INVALID")
    if declared > limit:
        raise ShareDraftError("SHARE_LIMIT_EXCEEDED")
    return declared


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """没声明大小的一律拒：判断必须在把整串字节收进内存之前。"""
    declared = _declared_length(request, limit)
    body = await request.body()
    if len(body) != declared:
        raise ShareDraftError("QUERY_INVALID")
    return body


async def stream_request_to_file(
    request: Request,
    directory: Path,
    limit: int,
) -> tuple[Path, int, str]:
    """Stream an upload body to a temporary ``0600`` file without buffering in RAM."""
    declared = _declared_length(request, limit)
    directory.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(directory, 0o700)
    target = directory / f"upload-{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    written = 0
    try:
        descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            async for chunk in request.stream():
                if not chunk:
                    continue
                written += len(chunk)
                if written > limit:
                    raise ShareDraftError("SHARE_LIMIT_EXCEEDED")
                if written > declared:
                    raise ShareDraftError("QUERY_INVALID")
                digest.update(chunk)
                stream.write(chunk)
        if written != declared:
            raise ShareDraftError("QUERY_INVALID")
    except BaseException:
        target.unlink(missing_ok=True)
        raise
    return target, written, digest.hexdigest()


async def read_draft_json(request: Request) -> object:
    body = await read_bounded_body(request, MAX_DRAFT_BODY_BYTES)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ShareDraftError("QUERY_INVALID") from None


__all__ = [
    "MAX_DRAFT_BODY_BYTES",
    "read_bounded_body",
    "read_draft_json",
    "stream_request_to_file",
]

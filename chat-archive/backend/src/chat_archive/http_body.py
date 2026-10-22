"""按声明的大小在**读之前**拦住过大的请求体（PSU-2.181）。

三处要它——本机 app、远端的设备令牌路由、远端的 BFF——而三处的错误信封不一样，
所以这里只抛带稳定代号的 `ShareDraftError`，由各端翻成自己的那一份。
"""

from __future__ import annotations

import json

from starlette.requests import Request

from chat_archive.share_draft import ShareDraftError

# 建草稿那一份 JSON 的上限：2 MB 的快照加上附件清单与信封，留足余量。
MAX_DRAFT_BODY_BYTES = 3 * 1024 * 1024


async def read_bounded_body(request: Request, limit: int) -> bytes:
    """没声明大小的一律拒：判断必须在把整串字节收进内存之前。"""
    raw = request.headers.get("content-length")
    try:
        declared = int(raw) if raw is not None else None
    except ValueError:
        declared = None
    if declared is None or declared < 0:
        raise ShareDraftError("QUERY_INVALID")
    if declared > limit:
        raise ShareDraftError("SHARE_LIMIT_EXCEEDED")
    body = await request.body()
    if len(body) != declared:
        raise ShareDraftError("QUERY_INVALID")
    return body


async def read_draft_json(request: Request) -> object:
    body = await read_bounded_body(request, MAX_DRAFT_BODY_BYTES)
    try:
        return json.loads(body)
    except (ValueError, RecursionError):
        raise ShareDraftError("QUERY_INVALID") from None


__all__ = ["MAX_DRAFT_BODY_BYTES", "read_bounded_body", "read_draft_json"]

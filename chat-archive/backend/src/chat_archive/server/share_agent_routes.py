"""Read-only representations of one public share, without JS or eager media signing."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import re
from collections.abc import Callable
from html import escape
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse

from chat_archive.ports.objectstore import ObjectStore, ObjectStoreError, ObjectStoreValidationError
from chat_archive.server.blob_routes import PRESIGNED_GET_TTL_SECONDS
from chat_archive.server.share_routes import public_share_response, share_gone_response
from chat_archive.server.share_text import (
    FALLBACK_STYLE,
    html_body,
    markdown_page,
    snapshot_messages,
    text_page,
)
from chat_archive.server.shares import SHARE_OBJECT_SCOPE, SqliteShareStore
from chat_archive.spa import content_security_policy

_MOUNT = re.compile(r'<div\s+id=["\']?app["\']?\s*>\s*</div>')
_ROBOTS = '<meta name="robots" content="noindex, nofollow">'
# One body for expired, deleted, draft, unknown and broken shares: the page must not reveal
# which one it is. Wording matches ErrorCode.SHARE_GONE and the Vue gone state.
GONE_TITLE = "这个分享已失效"
GONE_BODY = (
    f'<main class="share-fallback"><header><h1>{GONE_TITLE}</h1></header>'
    "<p>链接已过期，或者分享的人已经把它删了。</p>"
    "<footer>这是一份分享的聊天记录 · 由「聊记」生成</footer></main>"
)


def _compose(shell: str, body: str, title: str, head: str) -> str:
    if shell:
        document = _MOUNT.sub(lambda _: f'<div id="app">{body}</div>', shell, count=1)
        return document.replace("</head>", head + "</head>", 1)
    return (
        '<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        f"<title>{escape(title)}</title>{head}</head><body>{body}</body></html>"
    )


def _markdown_preferred(accept: str) -> bool:
    entries: dict[str, float] = {}
    for entry in accept.lower().split(","):
        media, *parameters = entry.strip().split(";")
        quality = 1.0
        for parameter in parameters:
            if parameter.strip().startswith("q="):
                try:
                    quality = float(parameter.strip()[2:])
                except ValueError:
                    quality = 0.0
        entries[media.strip()] = quality if 0 <= quality <= 1 else 0.0
    md = entries.get("text/markdown", 0)
    html = entries.get("text/html", entries.get("text/*", entries.get("*/*", 0)))
    return md > 0 and md >= html


def register_share_agent_routes(
    app: FastAPI,
    store: SqliteShareStore,
    objects: ObjectStore,
    *,
    frontend_dist: Path | None = None,
    storage_origin: Callable[[], str | None] = lambda: None,
) -> None:
    def _shell() -> str:
        if frontend_dist is None:
            return ""
        try:
            shell = (frontend_dist / "index.html").read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return ""
        return shell if _MOUNT.search(shell) and "</head>" in shell else ""

    def _csp() -> dict[str, str]:
        return {"Content-Security-Policy": content_security_policy(storage_origin())}

    def _gone(*, markdown: bool) -> Response:
        if markdown:
            return share_gone_response()
        # A browser opening a dead link gets a readable 404 page instead of the JSON error
        # envelope. With a frontend build the same shell boots Vue, whose JSON read answers
        # SHARE_GONE and renders the original gone state; without JS the static body stays.
        document = _compose(_shell(), GONE_BODY, GONE_TITLE, _ROBOTS + FALLBACK_STYLE)
        return public_share_response(HTMLResponse(document, status_code=404, headers=_csp()))

    def _document(token: str, request: Request, *, markdown: bool) -> Response:
        share = store.open_public(token, count_view=False)
        if share is None:
            return _gone(markdown=markdown)
        try:
            snapshot_messages(share)
        except (ValueError, TypeError):
            return _gone(markdown=markdown)
        cursor = request.query_params.get("cursor", "")
        try:
            page = text_page(share, cursor)
        except ValueError:
            return public_share_response(
                PlainTextResponse(
                    "读取范围不正确，请移除 cursor 从第一页重新读取。",
                    status_code=400,
                )
            )
        # Only the entry page (no cursor) counts as an open; continuation pages belong to the
        # same read. Every page still re-checks validity through the read above.
        if not cursor and store.open_public(token) is None:
            return _gone(markdown=markdown)
        if markdown:
            return public_share_response(
                PlainTextResponse(markdown_page(page, token), media_type="text/markdown")
            )
        alternate = f'<link rel="alternate" type="text/markdown" href="/s/{token}.md">'
        head = f"{_ROBOTS}{alternate}{FALLBACK_STYLE}"
        # A partial HTML page remains a static reader, otherwise Vue would replace it with
        # a full snapshot and hide the requested cursor/range from the browser user.
        shell = "" if cursor else _shell()
        document = _compose(shell, html_body(page, token), page.title, head)
        if shell:
            # The entry page has been counted; Vue's first JSON read must not count again.
            document = document.replace("<html", f'<html data-share-page-token="{token}"', 1)
        response = HTMLResponse(
            document,
            headers=_csp() | {"Link": f'</s/{token}.md>; rel="alternate"; type="text/markdown"'},
        )
        return public_share_response(response)

    @app.get("/s/{token}.md", include_in_schema=False)
    def share_markdown(token: str, request: Request) -> Response:
        return _document(token, request, markdown=True)

    @app.get("/s/{token}", include_in_schema=False)
    def share_page(token: str, request: Request) -> Response:
        response = _document(
            token, request, markdown=_markdown_preferred(request.headers.get("accept", ""))
        )
        response.headers["Vary"] = "Accept"
        return response

    @app.get("/s/{token}/media/{attachment_id}", include_in_schema=False)
    def share_media(token: str, attachment_id: str) -> Response:
        share = store.open_public(token, count_view=False)
        if share is None:
            return share_gone_response()
        try:
            snapshot_messages(share)
        except (ValueError, TypeError):
            return share_gone_response()
        item = next(
            (o for o in share.objects if o.uploaded and o.attachment_id == attachment_id), None
        )
        if item is None:
            return share_gone_response()
        try:
            presigned = objects.presign_get(
                SHARE_OBJECT_SCOPE, item.object_key, expires_in_seconds=PRESIGNED_GET_TTL_SECONDS
            )
            parsed = urlsplit(presigned.url)
            if parsed.scheme not in {"https", "http"} or not parsed.netloc or parsed.username:
                raise ObjectStoreValidationError("invalid media destination")
        except (ObjectStoreError, ObjectStoreValidationError, ValueError):
            return public_share_response(
                PlainTextResponse("这个附件暂时不可用，请稍后重试。", status_code=503)
            )
        return public_share_response(RedirectResponse(presigned.url, status_code=307))

"""Shared HTTP client helpers for urllib-based callers."""

from __future__ import annotations

import urllib.request
from email.message import Message
from typing import IO


class RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Refuse every HTTP redirect at the urllib boundary."""

    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: Message,
        newurl: str,
    ) -> urllib.request.Request | None:
        del req, fp, code, msg, headers, newurl
        return None


def build_no_redirect_opener(
    *handlers: urllib.request.BaseHandler,
) -> urllib.request.OpenerDirector:
    """Build a urllib opener that rejects every HTTP redirect."""
    return urllib.request.build_opener(RejectRedirectHandler(), *handlers)


__all__ = ["RejectRedirectHandler", "build_no_redirect_opener"]

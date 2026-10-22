"""Minimal, non-recursive preview of an already archived reply target."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass(frozen=True)
class ReplyPreviewRecord:
    record_key: str
    sender_display_name: str | None = field(repr=False)
    content_text: str = field(repr=False)
    message_type: str | None
    attachment_refs: tuple[str, ...]

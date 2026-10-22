"""Identify ordinary, already-decoded image headers; no source decoding."""

from __future__ import annotations


def plain_image_format(content: bytes) -> tuple[str, str] | None:
    for magic, extension, mime in (
        (b"\xff\xd8\xff", ".jpg", "image/jpeg"),
        (b"\x89PNG\r\n\x1a\n", ".png", "image/png"),
        (b"GIF87a", ".gif", "image/gif"),
        (b"GIF89a", ".gif", "image/gif"),
    ):
        if content.startswith(magic):
            return extension, mime
    if len(content) >= 12 and content.startswith(b"RIFF") and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None

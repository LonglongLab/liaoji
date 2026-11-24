"""Deterministic compact JSON encoding helpers."""

from __future__ import annotations

import json


def canonical_json_text(
    document: object,
    *,
    sort_keys: bool = True,
    ensure_ascii: bool = True,
) -> str:
    """Serialize ``document`` as compact canonical JSON text."""
    return json.dumps(
        document,
        ensure_ascii=ensure_ascii,
        sort_keys=sort_keys,
        separators=(",", ":"),
    )


def canonical_json_bytes(
    document: object,
    *,
    sort_keys: bool = True,
    ensure_ascii: bool = True,
    encoding: str = "utf-8",
    trailing_newline: bool = False,
) -> bytes:
    """Serialize ``document`` as compact canonical JSON bytes."""
    encoded = canonical_json_text(
        document,
        sort_keys=sort_keys,
        ensure_ascii=ensure_ascii,
    )
    if trailing_newline:
        encoded += "\n"
    return encoded.encode(encoding)


__all__ = ["canonical_json_bytes", "canonical_json_text"]

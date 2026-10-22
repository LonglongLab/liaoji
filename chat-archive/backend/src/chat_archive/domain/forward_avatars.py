"""Read/publish-time coordinates for exact-source forward avatars; no fetching."""
from __future__ import annotations

import json
import re
from collections.abc import Iterator, Mapping
from typing import cast, overload

from chat_archive.domain.app_card import MAX_CARD_BYTES, safe_forward_avatar

AvatarRefs = Mapping[str, tuple[str | None, str | None]]


def _card(raw: str | None) -> dict[str, object] | None:
    if raw is None or len(raw.encode("utf-8")) > MAX_CARD_BYTES:
        return None
    try:
        parsed: object = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return cast(dict[str, object], parsed) if isinstance(parsed, dict) else None


def _items(card: dict[str, object], depth: int = 0) -> Iterator[dict[str, object]]:
    if depth > 8:
        return
    items = card.get("items")
    if isinstance(items, list):
        for value in cast(list[object], items):
            if not isinstance(value, dict):
                continue
            item = cast(dict[str, object], value)
            yield item
            for key in ("nested", "reply"):
                child = item.get(key)
                if isinstance(child, dict):
                    yield from _items(cast(dict[str, object], child), depth + 1)
    quoted = card.get("quoted")
    if isinstance(quoted, dict):
        yield from _items(cast(dict[str, object], quoted), depth + 1)


def forward_avatar_urls(raw: str | None) -> set[str]:
    card = _card(raw)
    return set() if card is None else {
        url for item in _items(card) if (url := safe_forward_avatar(item.get("avatar_url")))
    }


def card_avatar_blob_ids(raw: str | None) -> set[str]:
    card = _card(raw)
    if card is None:
        return set()
    return {value for item in _items(card)
            if isinstance(value := item.get("avatar_blob_id"), str)
            and re.fullmatch(r"[0-9a-f]{64}", value)}


@overload
def bind_avatar_refs(raw: str, refs: AvatarRefs) -> str: ...


@overload
def bind_avatar_refs(raw: None, refs: AvatarRefs) -> None: ...


def bind_avatar_refs(raw: str | None, refs: AvatarRefs) -> str | None:
    card = _card(raw)
    if card is None:
        return raw
    changed = False
    for item in _items(card):
        old = (item.pop("avatar_ref", None), item.pop("avatar_blob_id", None))
        url = safe_forward_avatar(item.get("avatar_url"))
        local, blob = refs.get(url, (None, None)) if url else (None, None)
        if local is not None:
            item["avatar_ref"] = local
        if blob is not None:
            item["avatar_blob_id"] = blob
        changed |= old != (local, blob)
    if not changed:
        return raw
    result = json.dumps(card, ensure_ascii=False, separators=(",", ":"))
    # A cache optimization may never truncate the source card.
    return result if len(result.encode("utf-8")) <= MAX_CARD_BYTES else bind_avatar_refs(raw, {})

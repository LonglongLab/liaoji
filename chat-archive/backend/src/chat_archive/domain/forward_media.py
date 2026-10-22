"""Portable, verified media associations inside merged-forward cards (PSU-2.194)."""

from __future__ import annotations

import json
import hashlib
import re
from collections.abc import Callable, Mapping
from typing import Literal, TypedDict, cast


class RemoteMediaRef(TypedDict):
    blob_id: str
    variant: Literal["original", "thumbnail"]
    size: int
    sha256: str


UNPUBLISHED = "FORWARD_MEDIA_UNPUBLISHED"


def media_publication(raw: str | None) -> tuple[str, str] | None:
    """A content-free manifest for one capture, independent of canonical layout."""
    parsed = _card(raw)
    if parsed is None:
        return None
    entries: list[dict[str, object]] = []
    for path, item in _items(parsed).items():
        ref = media_ref(item.get("remote_media"))
        if ref is None:
            continue
        hashes = {key: value for key in ("full_md5", "thumb_md5")
                  if isinstance((value := item.get(key)), str)
                  and re.fullmatch(r"[0-9a-f]{32}", value)}
        # Every published item needs source evidence. Partial receipts must not
        # accidentally acknowledge an expected item without an identity.
        if not hashes:
            return None
        entries.append({"path": list(path), "kind": item["kind"], **hashes, "remote_media": ref})
    if not entries:
        return None
    manifest = json.dumps(entries, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(manifest.encode()).hexdigest(), manifest


def has_legacy_media_gap(incoming: str | None, canonical: str | None) -> bool:
    """Permit a separate capture, never assign its identity to a legacy item."""
    current, previous = _card(incoming), _card(canonical)
    if current is None or previous is None:
        return False
    old = _items(previous)
    for path, item in _items(current).items():
        legacy = old.get(path)
        if (media_ref(item.get("remote_media")) is not None and legacy is not None
                and legacy.get("kind") == item.get("kind")
                and not legacy.get("full_md5") and not legacy.get("thumb_md5")
                and legacy.get("remote_media") is None):
            return True
    return False


def publication_covers(expected: str, observed: str | None) -> bool:
    left, right = _card(expected), _card(observed)
    if left is None or right is None:
        return False
    other = _items(right)
    for path, item in _items(left).items():
        wanted = media_ref(item.get("remote_media"))
        if wanted is None:
            continue
        found = other.get(path)
        if found is None or not _same_item(item, found):
            found = next((cand for cand in other.values() if _same_item(item, cand)), None)
            if found is None:
                return False
        got = media_ref(found.get("remote_media"))
        if got is None or (wanted["variant"] == "original" and got["variant"] != "original"):
            return False
        if got["sha256"] != wanted["sha256"] and got["size"] < wanted["size"]:
            return False
    return True


def media_ref(value: object) -> RemoteMediaRef | None:
    if not isinstance(value, dict):
        return None
    value = cast(dict[str, object], value)
    if set(value) != {"blob_id", "variant", "size", "sha256"}:
        return None
    if (
        not isinstance(value["blob_id"], str)
        or re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", value["blob_id"]) is None
        or value["variant"] not in ("original", "thumbnail")
        or type(value["size"]) is not int
        or value["size"] < 0
        or not isinstance(value["sha256"], str)
        or re.fullmatch(r"[0-9a-f]{64}", value["sha256"]) is None
    ):
        return None
    return cast(RemoteMediaRef, value)


def _card(raw: str | None) -> dict[str, object] | None:
    if raw is None or len(raw.encode("utf-8")) > 524_288:
        return None
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    value = cast(dict[str, object], value)
    return value if value.get("kind") == "merged_forward" else None


def _items(
    card: dict[str, object], path: tuple[int, ...] = ()
) -> dict[tuple[int, ...], dict[str, object]]:
    found: dict[tuple[int, ...], dict[str, object]] = {}
    entries = card.get("items")
    if not isinstance(entries, list) or len(path) > 6:
        return found
    for index, raw in enumerate(cast(list[object], entries)[:2000]):
        if not isinstance(raw, dict):
            continue
        item = cast(dict[str, object], raw)
        position = (*path, index)
        if item.get("kind") in ("image", "video", "file", "sticker"):
            found[position] = item
        nested = item.get("nested")
        if isinstance(nested, dict):
            nested = cast(dict[str, object], nested)
            if nested.get("kind") == "merged_forward":
                found.update(_items(nested, position))
    return found


def card_media_refs(raw: str | None) -> tuple[RemoteMediaRef, ...]:
    parsed = _card(raw)
    if parsed is None:
        return ()
    return tuple(
        ref
        for item in _items(parsed).values()
        if (ref := media_ref(item.get("remote_media"))) is not None
    )


def card_attachment_media(raw: str | None) -> Mapping[str, RemoteMediaRef]:
    parsed = _card(raw)
    if parsed is None:
        return {}
    result: dict[str, RemoteMediaRef] = {}
    for item in _items(parsed).values():
        identifier = item.get("attachment_id")
        ref = media_ref(item.get("remote_media"))
        if isinstance(identifier, str) and ref is not None:
            result[identifier] = ref
    return result


def card_blob_metadata(raw: str | None) -> Mapping[str, tuple[str, str | None]]:
    """Kinds/names of validated domain references, for read-side adapters."""
    parsed = _card(raw)
    if parsed is None:
        return {}
    result: dict[str, tuple[str, str | None]] = {}
    for item in _items(parsed).values():
        ref = media_ref(item.get("remote_media"))
        if ref is None:
            continue
        name = item.get("file_name")
        kind = "emoji" if item["kind"] == "sticker" else str(item["kind"])
        result[ref["blob_id"]] = (kind, name if isinstance(name, str) else None)
    return result


def validate_media_card(raw: str | None) -> None:
    parsed = _card(raw)
    if parsed is None:
        return
    for item in _items(parsed).values():
        if item.get("remote_media") is not None:
            ref = media_ref(item["remote_media"])
            if ref is None or (ref["variant"] == "thumbnail" and item.get("kind") not in ("image", "sticker")):
                raise ValueError("invalid forwarded media descriptor")


def publish_media_card(raw: str | None, refs: Mapping[str, Mapping[str, object]]) -> str | None:
    parsed = _card(raw)
    if parsed is None:
        return raw
    for item in _items(parsed).values():
        item.pop("remote_media", None)
        identifier = item.get("attachment_id")
        ref = (
            media_ref(dict(refs[identifier]))
            if isinstance(identifier, str) and identifier in refs
            else None
        )
        if ref is not None:
            item["remote_media"] = ref
    return json.dumps(parsed, ensure_ascii=False, separators=(",", ":"))


def _same_item(left: dict[str, object], right: dict[str, object]) -> bool:
    if left.get("kind") != right.get("kind"):
        return False
    # A shared thumbnail or local coordinate cannot override different originals.
    a, b = left.get("full_md5"), right.get("full_md5")
    if isinstance(a, str) and isinstance(b, str) and a and b:
        return a == b
    left_hashes = {
        h for k in ("full_md5", "thumb_md5")
        if isinstance((h := left.get(k)), str) and h
    }
    right_hashes = {
        h for k in ("full_md5", "thumb_md5")
        if isinstance((h := right.get(k)), str) and h
    }
    # Source hashes authorize cross-capture association. Local ids, names, sizes
    # and remote descriptors alone do not prove which source item owns the bytes.
    return bool(left_hashes & right_hashes)


def merge_media_cards(
    incoming: str | None,
    canonical: str | None,
    verified: Callable[[RemoteMediaRef], bool],
) -> tuple[str | None, tuple[str, ...] | None]:
    """Merge by proven source identity, preferring nested position over an offset search.

    None refs means this is an ordinary legacy card; callers keep its old merge rules.
    Empty refs means a forward is known but no declared object is verified.
    """
    current, previous = _card(incoming), _card(canonical)
    if not card_media_refs(incoming) and not card_media_refs(canonical):
        return incoming, None
    if current is None:
        current = previous
    if current is None:
        return incoming, None
    previous_items = {} if previous is None else _items(previous)
    blob_ids: set[str] = set()
    for path, item in _items(current).items():
        old = previous_items.get(path)
        if old is None or not _same_item(item, old):
            old = next((cand for cand in previous_items.values() if _same_item(item, cand)), None)
        candidates: list[RemoteMediaRef] = []
        if old is not None and _same_item(item, old):
            ref = media_ref(old.get("remote_media"))
            if ref is not None and verified(ref):
                candidates.append(ref)
            # A capture-machine coordinate cannot cause endless republishing.
            item["attachment_id"] = old.get("attachment_id")
        ref = media_ref(item.get("remote_media"))
        if ref is not None and verified(ref):
            candidates.append(ref)
        item.pop("remote_media", None)
        if candidates:
            # Canonical wins ties, original beats thumbnail; no cross-item comparison.
            best = max(candidates, key=lambda r: (r["variant"] == "original", r["size"]))
            item["remote_media"] = best
            blob_ids.add(best["blob_id"])
    return json.dumps(current, ensure_ascii=False, separators=(",", ":")), tuple(sorted(blob_ids))


def enrich_canonical_media(
    incoming: str | None,
    canonical: str | None,
    verified: Callable[[RemoteMediaRef], bool],
) -> tuple[str | None, tuple[str, ...] | None]:
    """Enrich matching canonical items without replacing their captured fields.

    A parsed-text divergence does not authorize another capture to replace the
    canonical forward. Source media identity must match even if parser positions shift.
    """
    current, previous = _card(incoming), _card(canonical)
    if current is None or previous is None:
        return canonical, None
    incoming_items = _items(current)
    changed = False
    for path, item in _items(previous).items():
        candidate = incoming_items.get(path)
        if candidate is None or not _same_item(item, candidate):
            candidate = next((cand for cand in incoming_items.values() if _same_item(item, cand)), None)
        if candidate is None or not _same_item(item, candidate):
            continue
        new = media_ref(candidate.get("remote_media"))
        if new is None or not verified(new):
            continue
        old = media_ref(item.get("remote_media"))
        if old is not None:
            # Keep an association we cannot currently re-attest. Enrichment
            # never clears canonical history merely because a probe is unready.
            if not verified(old):
                continue
            best = max((old, new), key=lambda r: (r["variant"] == "original", r["size"]))
            if best == old:
                continue
        item["remote_media"] = new
        changed = True
    if not changed:
        return canonical, None
    enriched = json.dumps(previous, ensure_ascii=False, separators=(",", ":"))
    return enriched, tuple(sorted({ref["blob_id"] for ref in card_media_refs(enriched)}))

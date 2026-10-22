"""Self-hosted remote blob endpoints and verified object orchestration."""

# pyright: reportUnusedFunction=false
# FastAPI route functions are registered through decorators.

from __future__ import annotations

import hashlib
import json
import logging
import tempfile
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator, Mapping, Sequence
from dataclasses import dataclass
from typing import BinaryIO, cast

from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from starlette.concurrency import run_in_threadpool

from chat_archive.ports.blob_state import (
    BlobManifestExpectation,
    BlobManifestRecord,
    BlobPartExpectation,
    BlobStateConflictError,
    BlobStateStore,
    BlobUploadCreate,
    BlobUploadRecord,
    object_scope_for_archive,
    require_manifest_descriptor,
    require_remote_id,
)
from chat_archive.ports.blob_transfer import BLOB_CLAIM_HEADER
from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.ports.objectstore import (
    CapabilityCache,
    CompletedPart,
    ObjectAttributes,
    ObjectHead,
    ObjectHeadReader,
    ObjectIntegrityError,
    ObjectNotFoundError,
    ObjectMultipartUploadMissingError,
    ObjectStore,
    ObjectStoreError,
    ObjectStoreValidationError,
    StoreCapabilities,
    UploadPlan,
    require_md5,
    require_sha256,
)
from chat_archive.server.auth import authenticate_request
from chat_archive.server.http import RemoteServerError
from chat_archive.server.security import (
    generate_opaque_id,
    generate_secret_hex,
    sha256_hex,
)

_logger = logging.getLogger(__name__)

# (archive_id, record_keys) -> {record_key: attachment_refs} (PSU-2.143).
# Injected: records live in the metadata store, a different component and file.
RecordRefsLookup = Callable[[str, Sequence[str]], Mapping[str, tuple[str, ...]]]

MAX_BLOB_BYTES = 25 * 1024 * 1024
# The largest attachment any upload path may declare. MAX_BLOB_BYTES only
# bounds the single-request proxy path; a multipart plan is sized from the
# manifest, so without this the ceiling was whatever MAX_MULTIPART_PARTS ×
# MAX_MULTIPART_PART_BYTES happened to multiply out to — about 156 GiB of the
# owner's object storage, from two constants that only mirror S3's protocol
# limits. 2 GiB is twice the largest file WeChat itself will transfer, so no
# real attachment can reach it.
MAX_BLOB_TOTAL_BYTES = 2 * 1024 * 1024 * 1024
MAX_JSON_BODY_BYTES = 2 * 1024 * 1024
MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024
MAX_MULTIPART_PART_BYTES = 16 * 1024 * 1024
MAX_MULTIPART_PARTS = 10_000
MULTIPART_CAPABILITY_TTL_SECONDS = 15 * 60
# How long a read URL stays valid. Short, because it authenticates nothing: a
# copied URL is the attachment until it expires. Long enough that a page of
# images loads on a slow mobile link, and expiry only gates the *start* of a
# transfer — a download already running is not cut off.
PRESIGNED_GET_TTL_SECONDS = 5 * 60
MULTIPART_FINALIZATION_LEASE_SECONDS = 15 * 60

# --- direct upload (PSU-2.49) -------------------------------------------
# How long a write capability lives. Short, and **never renewed**: "the batch
# expired" and "the batch failed" are the same event to the client's retry, and
# because signing writes no session state (see `batch_presign_blobs`) they are
# the same event here too. Re-signing the same batch is what a retry is.
DIRECT_UPLOAD_TTL_SECONDS = 15 * 60
# The batch ceiling is two-dimensional on purpose. Count alone livelocks: five
# hundred large attachments cannot finish inside the TTL, so the batch expires,
# gets re-signed unchanged, and expires again. Bytes are what stop that.
MAX_DIRECT_BATCH_ITEMS = 500
MAX_DIRECT_BATCH_BYTES = 64 * 1024 * 1024
# Above this, an object keeps going through multipart. A presigned PUT is
# all-or-nothing; multipart resumes, and a cross-border transfer of tens of
# megabytes is exactly the case that needs to.
MAX_DIRECT_OBJECT_BYTES = 8 * 1024 * 1024
# PSU-2.132: an item that declares an MD5 may be this large, on a store that
# attests by MD5 (Tencent COS). There the resumable path costs more than it
# saves — multipart completion reads the whole object back to check it, across
# the border — and the uploader is a desktop talking to storage in its own
# country. 512 MiB covered every object the deployment held when it was set.
MAX_DIRECT_MD5_OBJECT_BYTES = 512 * 1024 * 1024

_STREAM_CHUNK_BYTES = 1024 * 1024
_MANIFEST_KEYS = frozenset({"size", "sha256", "state"})
_BATCH_PRESIGN_ITEM_KEYS = frozenset({"blob_id", "size", "sha256"})
# `md5` is optional, and only a client that saw `direct_md5` sends it (PSU-2.131).
# `record_key` likewise, only after `direct_record_claim` (PSU-2.143).
_BATCH_PRESIGN_OPTIONAL_KEYS = frozenset({"md5", "record_key"})
_EXPECTED_REQUIRED_KEYS = frozenset({"blob_id", "size", "sha256", "state"})
_EXPECTED_ALLOWED_KEYS = _EXPECTED_REQUIRED_KEYS | {"object_version", "verified_at"}
_MULTIPART_PLAN_KEYS = frozenset({"client_upload_id", "part_size", "parts"})
_MULTIPART_PART_KEYS = frozenset({"part_number", "size", "sha256"})


class _DiscardSink:
    def write(self, data: bytes) -> int:
        return len(data)


def _invalid(message: str = "请求体字段缺失或类型不正确。") -> RemoteServerError:
    return RemoteServerError("QUERY_INVALID", message)


async def _read_bounded(request: Request, *, limit: int) -> bytes:
    declared = request.headers.get("Content-Length")
    if declared is not None:
        try:
            declared_size = int(declared)
        except ValueError:
            raise _invalid("Content-Length 必须是非负整数。") from None
        if declared_size < 0 or declared_size > limit:
            raise _invalid("请求体超过允许大小。")
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > limit:
            raise _invalid("请求体超过允许大小。")
        chunks.append(chunk)
    return b"".join(chunks)


async def _json_object(request: Request) -> dict[str, object]:
    raw = await _read_bounded(request, limit=MAX_JSON_BODY_BYTES)
    try:
        parsed: object = json.loads(raw)
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise _invalid("请求体必须是大小受限的合法 JSON。") from None
    if not isinstance(parsed, dict):
        raise _invalid()
    return cast(dict[str, object], parsed)


def _manifest_response(manifest: BlobManifestRecord) -> dict[str, object]:
    payload: dict[str, object] = {
        "blob_id": manifest.blob_id,
        "size": manifest.size,
        "sha256": manifest.sha256,
        "state": manifest.state,
    }
    if manifest.state == "remote_ready":
        payload["object_version"] = manifest.object_version
        payload["verified_at"] = manifest.verified_at
    return payload


def _ready_response(manifest: BlobManifestRecord) -> dict[str, object]:
    return {
        "blob_id": manifest.blob_id,
        "state": "remote_ready",
        "object_version": manifest.object_version,
        "verified_at": manifest.verified_at,
    }


def _ready_item(manifest: BlobManifestRecord) -> dict[str, object]:
    return {
        "blob_id": manifest.blob_id,
        "state": "remote_ready",
        "object_version": manifest.object_version,
        "verified_at": manifest.verified_at,
    }


def _pending_item(blob_id: str, reason: str) -> dict[str, object]:
    """Still owed. The client re-signs and re-sends exactly these."""
    return {"blob_id": blob_id, "state": "pending_upload", "reason": reason}


def _forget_abandoned_object(
    concrete: ObjectStore, object_scope: str, stale_key: str
) -> None:
    """Drop the key a rewritten pending manifest just stopped naming (PSU-2.153).

    Either nothing was ever written there — the usual case, a plan that was
    signed and never used — or a direct upload landed bytes that no confirm
    ever vouched for. Both are unreachable the moment the manifest names a
    different key, so the only question is whether they sit in the bucket for
    good. They do not.

    A store that will not delete is **not** allowed to fail the signature. The
    manifest has already moved on; refusing here would put this blob back in
    exactly the permanent conflict this unit exists to end, and buy an orphan
    avoided for a picture never uploaded.
    """
    try:
        concrete.delete_object(object_scope, stale_key)
    except (ObjectStoreError, ObjectStoreValidationError):
        _logger.warning(
            "could not delete the object key %s abandoned by a re-planned blob",
            stale_key,
            exc_info=True,
        )


def _conflict_item(blob_id: str, reason: str) -> dict[str, object]:
    """Refused, and re-sending will not help until something changes.

    Per item rather than per request on purpose: one blob whose descriptor
    disagrees must not cost the other four hundred and ninety-nine their trip.
    """
    return {"blob_id": blob_id, "state": "conflict", "reason": reason}


def _claim_requested(request: Request) -> bool:
    """Whether this caller opted into content claiming (PSU-2.82).

    Absent header = no, and that default is the compatibility story: an older
    client never sends it and therefore never sees ``claimable``, which it
    would parse successfully and then turn into a permanent retry.

    The header is also the carrier of "only the attachment's main blob claims"
    — only that uploader sends it. Thumbnails are addressed by deriving from
    the main blob's id, so a thumbnail claimed onto another machine's derived
    address is one no published ref can reach.
    """
    return request.headers.get(BLOB_CLAIM_HEADER) == "1"


def _claimable_item(blob_id: str, manifest: BlobManifestRecord) -> dict[str, object]:
    """These exact bytes are already here under another id (PSU-2.82).

    ``blob_id`` is the one the **client asked about**, not the one it may now
    point at: the client matches this batch's answers back to its own requests
    by that field, so answering with a different id would silently misfile the
    whole batch. The existing id travels in ``ready_blob_id``, which is a new
    key an older client never reads.
    """
    return {
        "blob_id": blob_id,
        "state": "claimable",
        "ready_blob_id": manifest.blob_id,
        "object_version": manifest.object_version,
        "verified_at": manifest.verified_at,
    }


def _manifest_input(body: dict[str, object], *, blob_id: str) -> tuple[int, str, str]:
    if set(body) != set(_MANIFEST_KEYS):
        raise _invalid()
    size = body.get("size")
    sha256 = body.get("sha256")
    state = body.get("state")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or not isinstance(sha256, str)
        or not isinstance(state, str)
    ):
        raise _invalid()
    require_manifest_descriptor(
        blob_id,
        size=size,
        sha256=sha256,
        state=state,
    )
    # Checked here rather than on the multipart plan: this is the one place a
    # size enters, and the plan derives its part count from what lands here.
    if size > MAX_BLOB_TOTAL_BYTES:
        raise _invalid("附件超过单个附件的大小上限。")
    if state == "remote_ready":
        raise RemoteServerError(
            "BLOB_CONFLICT",
            "客户端不能直接把附件标记为 remote_ready。",
        )
    if state != "pending_upload":
        raise _invalid("state 只能是 pending_upload。")
    return size, sha256, state


def _expectation(value: object) -> BlobManifestExpectation:
    if not isinstance(value, dict):
        raise _invalid()
    item = cast(dict[str, object], value)
    if not _EXPECTED_REQUIRED_KEYS <= set(item) or not set(item) <= _EXPECTED_ALLOWED_KEYS:
        raise _invalid()
    blob_id = item.get("blob_id")
    size = item.get("size")
    sha256 = item.get("sha256")
    state = item.get("state")
    object_version = item.get("object_version")
    verified_at = item.get("verified_at")
    if (
        not isinstance(blob_id, str)
        or not isinstance(size, int)
        or isinstance(size, bool)
        or not isinstance(sha256, str)
        or not isinstance(state, str)
        or (object_version is not None and not isinstance(object_version, str))
        or (
            verified_at is not None
            and (not isinstance(verified_at, int) or isinstance(verified_at, bool))
        )
    ):
        raise _invalid()
    return BlobManifestExpectation(
        blob_id=blob_id,
        size=size,
        sha256=sha256,
        state=state,
        object_version=object_version,
        verified_at=verified_at,
    )


def _multipart_input(
    body: dict[str, object],
    manifest: BlobManifestRecord,
) -> tuple[str, int, tuple[BlobPartExpectation, ...], str]:
    if set(body) != set(_MULTIPART_PLAN_KEYS):
        raise _invalid()
    client_upload_id = body.get("client_upload_id")
    part_size = body.get("part_size")
    raw_parts = body.get("parts")
    if (
        not isinstance(client_upload_id, str)
        or not isinstance(part_size, int)
        or isinstance(part_size, bool)
        or not isinstance(raw_parts, list)
    ):
        raise _invalid()
    part_values = cast(list[object], raw_parts)
    require_remote_id(client_upload_id, field_name="client_upload_id")
    if not MIN_MULTIPART_PART_BYTES <= part_size <= MAX_MULTIPART_PART_BYTES:
        raise _invalid("part_size 超出允许范围。")
    if manifest.size <= 0:
        raise _invalid("空附件不能使用 multipart 上传。")
    expected_count = (manifest.size + part_size - 1) // part_size
    if (
        len(part_values) != expected_count
        or not 1 <= len(part_values) <= MAX_MULTIPART_PARTS
    ):
        raise _invalid("part 数量与附件清单不一致。")

    parts: list[BlobPartExpectation] = []
    for index, value in enumerate(part_values, start=1):
        if not isinstance(value, dict):
            raise _invalid()
        item = cast(dict[str, object], value)
        if set(item) != set(_MULTIPART_PART_KEYS):
            raise _invalid()
        part_number = item.get("part_number")
        size = item.get("size")
        sha256 = item.get("sha256")
        expected_size = min(part_size, manifest.size - part_size * (index - 1))
        if (
            not isinstance(part_number, int)
            or isinstance(part_number, bool)
            or part_number != index
            or not isinstance(size, int)
            or isinstance(size, bool)
            or size != expected_size
            or not isinstance(sha256, str)
        ):
            raise _invalid("part 编号、大小或摘要与固定规划不一致。")
        require_sha256(sha256)
        parts.append(
            BlobPartExpectation(
                part_number=part_number,
                size=size,
                sha256=sha256,
            )
        )

    canonical = {
        "client_upload_id": client_upload_id,
        "part_size": part_size,
        "parts": [
            {
                "part_number": part.part_number,
                "size": part.size,
                "sha256": part.sha256,
            }
            for part in parts
        ],
        "sha256": manifest.sha256,
        "size": manifest.size,
    }
    plan_sha256 = hashlib.sha256(
        json.dumps(
            canonical,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    ).hexdigest()
    return client_upload_id, part_size, tuple(parts), plan_sha256


@dataclass(frozen=True)
class _DirectUploadItem:
    blob_id: str
    size: int
    sha256: str
    md5: str | None = None
    # The message this attachment belongs to, when it is that message's only
    # attachment (PSU-2.143). Lets the remote say "this message already has an
    # original at least as large — keep that one".
    record_key: str | None = None
    # Larger than a single PUT may carry. Only a claiming caller may send one,
    # and only to ask "do you already have these bytes?" before it opens a
    # multipart session: the answer is `remote_ready`, `claimable` or a
    # conflict, never a signature (`direct_claim_oversized`).
    oversized: bool = False


def _batch_presign_input(
    body: dict[str, object], *, claim: bool = False
) -> tuple[_DirectUploadItem, ...]:
    if set(body) != {"items"} or not isinstance(body.get("items"), list):
        raise _invalid()
    raw_items = cast(list[object], body["items"])
    if not raw_items:
        # An empty batch is a client that computed nothing to do and asked
        # anyway. Refused rather than answered with an empty list, because the
        # two are different bugs and only one of them is worth a round trip.
        raise _invalid("批量签名至少要有一个条目。")
    if len(raw_items) > MAX_DIRECT_BATCH_ITEMS:
        raise _invalid("批量签名条目数超过上限。")
    items: list[_DirectUploadItem] = []
    total_bytes = 0
    for value in raw_items:
        if not isinstance(value, dict):
            raise _invalid()
        item = cast(dict[str, object], value)
        if not _BATCH_PRESIGN_ITEM_KEYS <= set(item) <= (
            _BATCH_PRESIGN_ITEM_KEYS | _BATCH_PRESIGN_OPTIONAL_KEYS
        ):
            raise _invalid()
        md5 = item.get("md5")
        if md5 is not None:
            if not isinstance(md5, str):
                raise _invalid()
            try:
                require_md5(md5)
            except ObjectStoreValidationError:
                raise _invalid("md5 必须是 32 位小写十六进制。") from None
        record_key = item.get("record_key")
        if record_key is not None:
            if not isinstance(record_key, str):
                raise _invalid()
            require_remote_id(record_key, field_name="record_key")
        blob_id = item.get("blob_id")
        size = item.get("size")
        sha256 = item.get("sha256")
        if (
            not isinstance(blob_id, str)
            or not isinstance(size, int)
            or isinstance(size, bool)
            or not isinstance(sha256, str)
        ):
            raise _invalid()
        require_manifest_descriptor(
            blob_id,
            size=size,
            sha256=sha256,
            state="pending_upload",
        )
        ceiling = MAX_DIRECT_OBJECT_BYTES if md5 is None else MAX_DIRECT_MD5_OBJECT_BYTES
        oversized = size > ceiling
        if oversized and not claim:
            raise _invalid("单个对象超过直传上限，请使用 multipart 上传。")
        # An oversized item is never signed, so it shares no signing TTL and
        # does not count against the batch's bytes.
        total_bytes += 0 if oversized else size
        if total_bytes > MAX_DIRECT_BATCH_BYTES and len(raw_items) > 1:
            # One object on its own is never cut short by the batch ceiling
            # (PSU-2.132): that ceiling bounds how many bytes share one signing
            # TTL, and a lone object shares it with nothing.
            raise _invalid("批量签名总字节数超过上限。")
        items.append(
            _DirectUploadItem(
                blob_id=blob_id,
                size=size,
                sha256=sha256,
                md5=md5,
                record_key=record_key,
                oversized=oversized,
            )
        )
    if len({item.blob_id for item in items}) != len(items):
        raise _invalid("同一批里不能出现重复的 blob_id。")
    return tuple(items)


def _batch_confirm_input(body: dict[str, object]) -> tuple[str, ...]:
    if set(body) != {"blob_ids"} or not isinstance(body.get("blob_ids"), list):
        raise _invalid()
    raw_ids = cast(list[object], body["blob_ids"])
    if not raw_ids:
        raise _invalid("批量确认至少要有一个条目。")
    if len(raw_ids) > MAX_DIRECT_BATCH_ITEMS:
        raise _invalid("批量确认条目数超过上限。")
    blob_ids: list[str] = []
    for value in raw_ids:
        if not isinstance(value, str):
            raise _invalid()
        require_remote_id(value, field_name="blob_id")
        blob_ids.append(value)
    if len(set(blob_ids)) != len(blob_ids):
        raise _invalid("同一批里不能出现重复的 blob_id。")
    return tuple(blob_ids)


def store_capabilities(object_store: ObjectStore) -> StoreCapabilities:
    """Prefer the cached probe; the fresh one writes objects to answer.

    Public since PSU-2.131: ``sync-health`` asked the fresh probe on every
    poll, which on Tencent COS is a handful of billed PUT/DELETE requests per
    poll and a trail of noncurrent versions in a versioned bucket.
    """
    if isinstance(object_store, CapabilityCache):
        return object_store.cached_capabilities()
    return object_store.probe_capabilities()


def blob_upload_modes(
    object_store: ObjectStore, *, record_claim: bool = False,
    capabilities: StoreCapabilities | None = None,
) -> tuple[str, ...]:
    """Which upload shapes this deployment can actually serve, right now.

    Published on ``/api/v1/health`` so a client picks its route from what the
    remote can do rather than from a version number. ``record_model_version``
    could not carry this: that one describes the *metadata* store's record
    model, and the blob HTTP surface is not in it — which is precisely why
    PSU-2.47 could add avatar upload against an unchanged remote.

    Runtime, not a compile-time constant: one binary in front of MinIO and the
    same binary in front of a filesystem must answer differently. Storage that
    is unreachable or unconfigured reports the two modes that do not need it to
    answer, because "we cannot ask right now" is not "you may not upload".
    """
    modes = ["proxy", "multipart"]
    try:
        capabilities = capabilities if capabilities is not None else store_capabilities(object_store)
        if capabilities.presigned_urls:
            modes.append("direct")
            if capabilities.content_md5:
                # A client that sees this sends an MD5 with each item, and that
                # MD5 is what confirmation checks instead of reading the object
                # back (PSU-2.131). A token, not a version: an older client
                # ignores it, and an older remote never says it.
                modes.append("direct_md5")
            if record_claim:
                # The remote can answer "this message already has an original
                # at least this large" (PSU-2.143). Only a client that sees this
                # sends `record_key`: an older remote refuses the unknown key.
                modes.append("direct_record_claim")
            # A claiming caller may ask about an object too large to sign, so an
            # attachment bound for multipart is claimed like any other instead
            # of being sent again. An older remote answers such an item with a
            # 400 for the whole batch, so the client asks only after this.
            modes.append("direct_claim_oversized")
    except (ObjectStoreError, ObjectStoreValidationError):
        pass
    return tuple(modes)


def _object_upload_plan(upload: BlobUploadRecord) -> UploadPlan:
    return UploadPlan(
        archive_id=upload.object_scope,
        object_key=upload.object_key,
        upload_id=upload.provider_upload_id,
        total_size=upload.total_size,
        part_size=upload.part_size,
        sha256=upload.sha256,
    )


def _upload_not_found() -> RemoteServerError:
    return RemoteServerError(
        "NOT_FOUND",
        "上传不存在或不属于当前设备。",
    )


def _upload_unauthorized() -> RemoteServerError:
    return RemoteServerError(
        "UNAUTHORIZED",
        "上传能力缺失、过期或无效。",
    )


async def _synchronize_uploaded_parts(
    upload: BlobUploadRecord,
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
) -> BlobUploadRecord:
    # Persisted ETags do not prove the provider session survived until complete.
    # A crashed finalizer also needs this probe; the store fences active leases.
    if upload.state not in ("planned", "uploading_parts", "completing"):
        return upload
    try:
        provider_parts = await run_in_threadpool(
            object_store.list_uploaded_parts, _object_upload_plan(upload),
        )
    except ObjectMultipartUploadMissingError:
        # Missing provider state is recoverable only while no completed object exists.
        if isinstance(object_store, ObjectHeadReader):
            heads = await run_in_threadpool(
                object_store.head_objects, upload.object_scope, [upload.object_key],
            )
        else:
            heads = await run_in_threadpool(
                object_store.describe_objects, upload.object_scope, [upload.object_key],
            )
        if upload.object_key in heads:
            if all(part.etag is not None for part in upload.parts):
                # Completion may have landed before its response was lost.
                # Keep its identity and let complete perform full verification.
                return upload
            raise
        replacement = await run_in_threadpool(
            object_store.plan_upload, upload.object_scope, total_size=upload.total_size,
            sha256=upload.sha256, part_size=upload.part_size, object_key=upload.object_key,
        )
        try:
            repaired = await run_in_threadpool(
                blob_state_store.replace_missing_provider_upload, upload, replacement.upload_id,
            )
        except BaseException:
            try:
                await run_in_threadpool(object_store.abort_upload, replacement)
            except ObjectStoreError:
                pass
            raise
        if repaired is not None:
            return repaired
        # Another request won or started completion. Never leak this loser's plan.
        try:
            await run_in_threadpool(object_store.abort_upload, replacement)
        except ObjectStoreError:
            pass
        current = await run_in_threadpool(
            blob_state_store.get_upload, upload.archive_id, upload.blob_id, upload.session_id, upload.device_id,
        )
        if current is None:
            raise BlobStateConflictError("multipart upload disappeared during recovery")
        return current
    expected = {part.part_number: part for part in upload.parts}
    for provider_part in provider_parts:
        planned = expected.get(provider_part.part_number)
        if (
            planned is None
            or (
                provider_part.sha256 is not None
                and provider_part.sha256 != planned.sha256
            )
        ):
            await run_in_threadpool(
                blob_state_store.mark_upload_integrity_failed,
                upload.archive_id,
                upload.blob_id,
                upload.session_id,
            )
            raise ObjectIntegrityError("provider returned an invalid multipart part")
    if not provider_parts:
        return upload
    return await run_in_threadpool(
        blob_state_store.sync_completed_parts,
        upload.archive_id,
        upload.blob_id,
        upload.session_id,
        tuple((part.part_number, part.entity_tag) for part in provider_parts),
        completed_at=max(1, int(time.time())),
        expected_provider_upload_id=upload.provider_upload_id,
    )


async def _upload_status_payload(
    upload: BlobUploadRecord,
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
) -> dict[str, object]:
    upload = await _synchronize_uploaded_parts(
        upload,
        blob_state_store,
        object_store,
    )
    active = upload.state in ("planned", "uploading_parts")
    expires_at = max(1, int(time.time())) + MULTIPART_CAPABILITY_TTL_SECONDS
    payload_parts: list[dict[str, object]] = []
    for part in upload.parts:
        if part.etag is not None:
            payload_parts.append(
                {
                    "part_number": part.part_number,
                    "size": part.size,
                    "sha256": part.sha256,
                    "state": "completed",
                }
            )
            continue
        payload: dict[str, object] = {
            "part_number": part.part_number,
            "size": part.size,
            "sha256": part.sha256,
            "state": "pending",
        }
        if active:
            secret = generate_secret_hex()
            await run_in_threadpool(
                blob_state_store.set_part_capability,
                upload.archive_id,
                upload.blob_id,
                upload.session_id,
                part.part_number,
                capability_hash=sha256_hex(secret),
                expires_at=expires_at,
            )
            payload["upload_request"] = {
                "url": (
                    f"/api/v1/archives/{upload.archive_id}/blobs/{upload.blob_id}"
                    f"/uploads/{upload.session_id}/parts/{part.part_number}"
                ),
                "method": "PUT",
                "headers": {
                    "Authorization": f"Upload {secret}",
                    "Content-Length": str(part.size),
                    "X-Content-SHA256": part.sha256,
                },
                "expires_at": expires_at,
            }
        payload_parts.append(payload)
    return {
        "blob_id": upload.blob_id,
        "client_upload_id": upload.client_upload_id,
        "session_id": upload.session_id,
        "state": "uploading_parts" if upload.state == "completing" else upload.state,
        "part_size": upload.part_size,
        "total_size": upload.total_size,
        "sha256": upload.sha256,
        "parts": payload_parts,
    }


def _verified_upload_response(upload: BlobUploadRecord) -> dict[str, object]:
    if (
        upload.state != "remote_ready"
        or upload.object_version is None
        or upload.verified_at is None
    ):
        raise BlobStateConflictError("verified multipart metadata is incomplete")
    return {
        "blob_id": upload.blob_id,
        "size": upload.total_size,
        "sha256": upload.sha256,
        "state": "remote_ready",
        "object_version": upload.object_version,
        "verified_at": upload.verified_at,
    }


async def _recover_completed_upload(
    upload: BlobUploadRecord,
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
    *,
    finalizer_id: str,
    storage_generation: int,
) -> BlobUploadRecord | None:
    try:
        existing = await run_in_threadpool(
            object_store.download_object,
            upload.object_scope,
            upload.object_key,
            cast(BinaryIO, _DiscardSink()),
        )
    except ObjectNotFoundError:
        return None
    if existing.size != upload.total_size or existing.sha256 != upload.sha256:
        await run_in_threadpool(
            blob_state_store.mark_upload_integrity_failed,
            upload.archive_id,
            upload.blob_id,
            upload.session_id,
            finalizer_id=finalizer_id,
        )
        raise ObjectIntegrityError(
            "existing completed object does not match multipart plan"
        )
    return await run_in_threadpool(
        blob_state_store.mark_upload_ready,
        upload.archive_id,
        upload.blob_id,
        upload.session_id,
        finalizer_id=finalizer_id,
        object_version=existing.version_id or uuid.uuid4().hex,
        verified_at=max(1, int(time.time())),
        storage_generation=storage_generation,
    )


def _verified_file_chunks(stream: BinaryIO) -> Iterator[bytes]:
    try:
        while chunk := stream.read(_STREAM_CHUNK_BYTES):
            yield chunk
    finally:
        stream.close()


def _require_verified_attributes(
    manifest: BlobManifestRecord,
    attributes: ObjectAttributes,
) -> None:
    if attributes.size != manifest.size or attributes.sha256 != manifest.sha256:
        raise RemoteServerError(
            "BLOB_INTEGRITY",
            "远程对象与附件清单不一致。",
        )


class AttachmentPresigner:
    """Mints one attachment's direct-read URL, for whoever is asking.

    Extracted so the device-token route and the browser BFF cannot answer
    differently. Both have to apply the same three rules — the manifest exists,
    it is ``remote_ready``, and it belongs to the *current* storage generation
    — and a second copy of those rules is how one end would start handing out
    URLs the other refuses.
    """

    def __init__(
        self,
        blob_state_store: BlobStateStore,
        object_store: ObjectStore,
        *,
        storage_generation: Callable[[], int] | None = None,
        object_store_snapshot: Callable[[], tuple[ObjectStore, int]] | None = None,
    ) -> None:
        self._blob_state_store = blob_state_store
        self._object_store = object_store
        self._storage_generation = storage_generation
        self._object_store_snapshot = object_store_snapshot

    def generation(self) -> int:
        # A deployment that never changes bucket stays on generation 1, which
        # is also what every pre-existing manifest row was backfilled to.
        return 1 if self._storage_generation is None else self._storage_generation()

    def require_current_generation(
        self,
        manifest: BlobManifestRecord,
        current: int | None = None,
    ) -> None:
        """Refuse to look for verified bytes in a bucket that no longer holds them.

        Without this the attachment would surface as a generic provider miss,
        which reads as "your file is gone" when the truth is "your file is in
        the previous bucket and switching back makes it readable again".
        """
        resolved_current = self.generation() if current is None else current
        if manifest.storage_generation == resolved_current:
            return
        raise RemoteServerError(
            "OBJECT_STORAGE_GENERATION_MISMATCH",
            f"附件存放在第 {manifest.storage_generation} 代对象存储中，"
            f"当前为第 {resolved_current} 代；切回该代配置后可打开。",
        )

    def store_for_manifest(self, manifest: BlobManifestRecord) -> tuple[ObjectStore, int]:
        if self._object_store_snapshot is None:
            concrete, generation = self._object_store, self.generation()
        else:
            concrete, generation = self._object_store_snapshot()
        self.require_current_generation(manifest, generation)
        return concrete, generation

    def __call__(self, archive_id: str, blob_id: str, /) -> dict[str, object]:
        manifest = self._blob_state_store.get_manifest(archive_id, blob_id)
        if manifest is None or manifest.state != "remote_ready":
            # A blob that is still uploading has an object key but not the
            # object, and a signed URL to it is a link that 404s — worse than
            # no link, because the reader cannot tell "not uploaded yet" from
            # "lost".
            raise RemoteServerError("NOT_FOUND", "附件尚未完成远程校验。")
        concrete, _generation = self.store_for_manifest(manifest)
        presigned = concrete.presign_get(
            manifest.object_scope,
            manifest.object_key,
            expires_in_seconds=PRESIGNED_GET_TTL_SECONDS,
        )
        origin = urllib.parse.urlsplit(presigned.url)
        return {
            "url": presigned.url,
            "method": presigned.method,
            "expires_at": max(1, int(time.time())) + PRESIGNED_GET_TTL_SECONDS,
            # Published separately so the page never has to take the signed URL
            # apart to learn which origin it is about to talk to; the signature
            # rides in that URL's query string.
            "storage_origin": f"{origin.scheme}://{origin.netloc}",
        }


def register_blob_routes(
    app: FastAPI,
    auth_store: DeviceAuthStore,
    blob_state_store: BlobStateStore,
    object_store: ObjectStore,
    storage_generation: Callable[[], int] | None = None,
    object_store_snapshot: Callable[[], tuple[ObjectStore, int]] | None = None,
    record_refs: RecordRefsLookup | None = None,
) -> None:
    presigner = AttachmentPresigner(
        blob_state_store,
        object_store,
        storage_generation=storage_generation,
        object_store_snapshot=object_store_snapshot,
    )

    def _generation() -> int:
        # A deployment that never changes bucket stays on generation 1, which is
        # also what every pre-existing manifest row was backfilled to.
        return 1 if storage_generation is None else storage_generation()

    def _require_current_generation(
        manifest: BlobManifestRecord,
        current: int | None = None,
    ) -> None:
        """Refuse to look for verified bytes in a bucket that no longer holds them.

        Without this the attachment would surface as a generic provider miss,
        which reads as "your file is gone" when the truth is "your file is in
        the previous bucket and switching back makes it readable again".
        """
        resolved_current = _generation() if current is None else current
        if manifest.storage_generation == resolved_current:
            return
        raise RemoteServerError(
            "OBJECT_STORAGE_GENERATION_MISMATCH",
            f"附件存放在第 {manifest.storage_generation} 代对象存储中，"
            f"当前为第 {resolved_current} 代；切回该代配置后可打开。",
        )

    def _store_for_manifest(manifest: BlobManifestRecord) -> tuple[ObjectStore, int]:
        if object_store_snapshot is None:
            concrete, generation = object_store, _generation()
        else:
            concrete, generation = object_store_snapshot()
        _require_current_generation(manifest, generation)
        return concrete, generation

    @app.get("/api/v1/archives/{archive_id}/blobs/{blob_id}/presigned-url")
    async def presign_blob_content(
        archive_id: str,
        blob_id: str,
        request: Request,
    ) -> dict[str, object]:
        """Hand out a short-lived direct URL instead of streaming the bytes.

        This is the whole reason attachments are reachable from a phone at all
        without pushing every megabyte through a 2-vCPU API process: the page
        fetches from object storage itself. The bytes never enter this process
        (`docs/local-remote-boundary.md` §4).
        """
        authenticate_request(request, archive_id, auth_store)
        return await run_in_threadpool(presigner, archive_id, blob_id)

    @app.get("/api/v1/archives/{archive_id}/blobs/{blob_id}/manifest")
    async def get_blob_manifest_descriptor(archive_id: str, blob_id: str, request: Request) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        manifest = await run_in_threadpool(blob_state_store.get_manifest, archive_id, blob_id)
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        return _manifest_response(manifest)

    @app.put("/api/v1/archives/{archive_id}/blobs/{blob_id}/manifest")
    async def put_blob_manifest(
        archive_id: str,
        blob_id: str,
        request: Request,
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        body = await _json_object(request)
        size, sha256, _state = _manifest_input(body, blob_id=blob_id)
        prior = await run_in_threadpool(blob_state_store.get_manifest, archive_id, blob_id)
        if prior is not None and prior.state == "uploading_parts" and (prior.size != size or prior.sha256 != sha256):
            raise RemoteServerError("BLOB_SOURCE_CHANGED", "当前字节与原上传计划不同。")
        manifest = await run_in_threadpool(
            blob_state_store.put_manifest,
            archive_id,
            blob_id,
            size=size,
            sha256=sha256,
            storage_generation=_generation(),
        )
        return _manifest_response(manifest)

    def _storage_origin(concrete: ObjectStore, archive_id: str) -> str:
        """Which origin the URLs in this response point at.

        Published beside them rather than left for the client to parse out,
        and published **here** rather than on ``/api/v1/health``: health is
        unauthenticated, and the storage host is one more fact about this
        deployment that a stranger does not need. This is also the moment the
        client actually wants it — it checks the batch it just received
        against this value before sending a byte anywhere.

        Read off a signature that is computed and thrown away. The origin is a
        property of the endpoint, not of any object, and every URL in this
        response comes from the same store instance.
        """
        scope = object_scope_for_archive(archive_id)
        probe = concrete.presign_get(
            scope,
            f"a/{scope}/{'0' * 32}",
            expires_in_seconds=60,
        )
        split = urllib.parse.urlsplit(probe.url)
        return f"{split.scheme}://{split.netloc}"

    def _presign_one(
        concrete: ObjectStore,
        generation: int,
        archive_id: str,
        item: _DirectUploadItem,
        expires_at: int,
        claim: bool,
        attests_md5: bool = False,
        message_refs: Mapping[str, tuple[str, ...]] | None = None,
    ) -> dict[str, object]:
        """One blob's answer: skip it, refuse it, claim it, or sign it."""
        if item.oversized or (item.size > MAX_DIRECT_OBJECT_BYTES and not attests_md5):
            # Only an MD5-attesting store may take a large object in one PUT
            # (PSU-2.132). Refused per item: the small ones beside it still go.
            if claim:
                return _claim_without_signing(generation, archive_id, item, message_refs)
            return _conflict_item(item.blob_id, "exceeds_direct_limit")
        manifest = blob_state_store.get_manifest(archive_id, item.blob_id)
        stale_key: str | None = None
        if manifest is not None:
            # An attachment's blob id is **not** derived from its bytes
            # (`domain.identity.remote_blob_id` says so in as many words); only
            # avatars are content-derived. So one id carrying two descriptors is
            # an ordinary event, not an impossibility — a re-decode changes the
            # bytes and keeps the id (PSU-2.146 turned 46,515 pictures from
            # lossless PNG back into WeChat's own wxgf that way).
            #
            # Which means the descriptor cannot be checked *before* the state:
            # doing that answers "you computed one of these wrong" to three
            # situations, and only one of them is that (PSU-2.153).
            same_bytes = manifest.size == item.size and manifest.sha256 == item.sha256
            md5_disagrees = (
                same_bytes
                and manifest.md5 is not None
                and item.md5 is not None
                and manifest.md5 != item.md5
            )
            if md5_disagrees:
                # Same length and SHA-256, different MD5: one of the two hashed
                # something other than what it holds (PSU-2.131). No size rule
                # can adjudicate that — the sizes are equal — and it is the one
                # reading of "two descriptors" that really is a caller bug.
                return _conflict_item(item.blob_id, "descriptor_mismatch")
            if manifest.state == "remote_ready":
                if same_bytes:
                    # This *is* the reconcile. Idempotent skip, not an error,
                    # and the reason the upload path costs two round trips per
                    # batch instead of three.
                    return _ready_item(manifest)
                if claim and manifest.size >= item.size:
                    # PSU-2.143's "only send it if this copy is larger", applied
                    # to the id the client actually asked about. The remote is
                    # holding a copy of this attachment that is no smaller than
                    # the one being offered, so nothing is owed — and
                    # `attachment_refs` carries the blob id alone, never a size
                    # or a hash, so the published ref and this manifest stay
                    # consistent with each other.
                    #
                    # Gated on the claim header for the same reason claiming is
                    # (PSU-2.82): it marks a caller that accepts bytes it did
                    # not send, and only the attachment's main-blob uploader
                    # sends it. Avatars derive their id *from* the content, so
                    # for them two descriptors under one id stays a real bug and
                    # keeps the strict answer below.
                    return _ready_item(manifest)
                # Replacing a verified original with different bytes is the
                # "swap the original" mechanism PSU-2.146 §7 deferred. It would
                # mint a new object key and strand the copy readers are looking
                # at, so it is refused rather than done quietly.
                return _conflict_item(item.blob_id, "descriptor_mismatch")
            if manifest.state == "uploading_parts":
                # Checked before the descriptor, not after: the caller's remedy
                # here is to finish or abandon that session, which is a
                # different thing to do than recomputing a hash.
                return _conflict_item(item.blob_id, "multipart_in_progress")
            if manifest.storage_generation != generation:
                return _conflict_item(item.blob_id, "storage_generation_mismatch")
            if not same_bytes:
                # A pending row with nothing confirmed under it: `object_version`
                # and `verified_at` are both NULL, so no reader can be pointed
                # at the key it is about to stop naming. `put_manifest` already
                # implements this migration; this branch is only about letting
                # it, and about not leaving an orphan behind.
                stale_key = manifest.object_key
        elif claim:
            # Nothing is registered under this id, but the same bytes may
            # already be here under the id another machine derived (PSU-2.82).
            # Deliberately *after* every branch above: a manifest that already
            # exists for this id has its own answer, and claiming would jump
            # the queue on three of them.
            #
            # Nothing is written down. No manifest is minted for the id the
            # client asked about, because a second manifest row for the same
            # bytes is exactly the duplicate this is here to stop — and
            # `server_blob_manifests.object_key` is UNIQUE, so it could not
            # point at the same object anyway.
            existing = blob_state_store.find_ready_manifest_by_content(
                archive_id,
                sha256=item.sha256,
                size=item.size,
                storage_generation=generation,
            )
            if existing is not None and not (
                # Same SHA-256 and length, different MD5: one of the two
                # machines hashed something other than what it holds. Uploading
                # again costs bytes; claiming would point this machine's record
                # at an object nobody can vouch is its picture (PSU-2.131).
                existing.md5 is not None
                and item.md5 is not None
                and existing.md5 != item.md5
            ):
                return _claimable_item(item.blob_id, existing)
            held = _held_for_message(generation, archive_id, item, message_refs)
            if held is not None:
                return _claimable_item(item.blob_id, held)
        try:
            stored = blob_state_store.put_manifest(
                archive_id,
                item.blob_id,
                size=item.size,
                sha256=item.sha256,
                storage_generation=generation,
                md5=item.md5,
            )
        except BlobStateConflictError:
            # Another writer registered a *different* descriptor for this id
            # between the read above and this write — the only case
            # `put_manifest` refuses. Reported per item, because one racing
            # blob must not cost the other 499 their trip.
            return _conflict_item(item.blob_id, "descriptor_mismatch")
        if stale_key is not None and stale_key != stored.object_key:
            _forget_abandoned_object(concrete, stored.object_scope, stale_key)
        signed = concrete.presign_put(
            stored.object_scope,
            stored.object_key,
            size=stored.size,
            sha256=stored.sha256,
            expires_in_seconds=DIRECT_UPLOAD_TTL_SECONDS,
            md5=stored.md5,
        )
        return {
            "blob_id": stored.blob_id,
            "state": "pending_upload",
            "upload_request": {
                "url": signed.url,
                "method": signed.method,
                "headers": dict(signed.headers),
                "expires_at": expires_at,
            },
        }

    def _claim_without_signing(
        generation: int,
        archive_id: str,
        item: _DirectUploadItem,
        message_refs: Mapping[str, tuple[str, ...]] | None,
    ) -> dict[str, object]:
        """For an object that will go up in parts: already here, or not.

        Writes nothing either way. `put_manifest` belongs to the multipart
        route that follows a "not here", and a manifest minted from this answer
        would carry a descriptor no signature was ever issued against.
        """
        manifest = blob_state_store.get_manifest(archive_id, item.blob_id)
        if manifest is not None:
            if (
                manifest.state == "remote_ready"
                and manifest.storage_generation == generation
                and (
                    manifest.size == item.size
                    and manifest.sha256 == item.sha256
                    or manifest.size >= item.size
                )
                and not (
                    manifest.md5 is not None
                    and item.md5 is not None
                    and manifest.sha256 == item.sha256
                    and manifest.md5 != item.md5
                )
            ):
                return _ready_item(manifest)
            # Every other state of a known id — a running session, a pending
            # descriptor, a smaller original — is the multipart route's to
            # answer, exactly as it did before this question could be asked.
            return _conflict_item(item.blob_id, "exceeds_direct_limit")
        existing = blob_state_store.find_ready_manifest_by_content(
            archive_id,
            sha256=item.sha256,
            size=item.size,
            storage_generation=generation,
        )
        if existing is not None and not (
            existing.md5 is not None and item.md5 is not None and existing.md5 != item.md5
        ):
            return _claimable_item(item.blob_id, existing)
        held = _held_for_message(generation, archive_id, item, message_refs)
        if held is not None:
            return _claimable_item(item.blob_id, held)
        return _conflict_item(item.blob_id, "exceeds_direct_limit")

    def _held_for_message(
        generation: int,
        archive_id: str,
        item: _DirectUploadItem,
        message_refs: Mapping[str, tuple[str, ...]] | None,
    ) -> BlobManifestRecord | None:
        """The original this message already has on the remote, if it will do.

        PSU-2.143: two machines capturing one account hold different bytes for
        one picture often enough (a thumbnail-sized copy on one, the full image
        on the other; a different decode), and content claiming cannot see that
        they are the same message. The record can: `record_key` is the same on
        both machines. The rule the user set is "only send it if this copy is
        larger" — so an original at least as large answers ``claimable``.

        Only single-ref records: with two or more there is no way to tell which
        of them this attachment is. The client sends `record_key` only for a
        message with exactly one attachment, for the same reason.
        """
        if item.record_key is None or not message_refs:
            return None
        refs = message_refs.get(item.record_key)
        if refs is None or len(refs) != 1 or refs[0] == item.blob_id:
            return None
        held = blob_state_store.get_manifest(archive_id, refs[0])
        if (
            held is None
            or held.state != "remote_ready"
            or held.storage_generation != generation
            or held.size < item.size
        ):
            return None
        return held

    def _batch_presign(
        archive_id: str,
        items: tuple[_DirectUploadItem, ...],
        claim: bool,
    ) -> dict[str, object]:
        # Resolved before the capability check, not after: an unconfigured
        # store must say so in its own words rather than as "this deployment
        # does not support direct upload", which would send the operator
        # looking for a feature flag instead of the bucket they never set up.
        if object_store_snapshot is None:
            concrete, generation = object_store, _generation()
        else:
            concrete, generation = object_store_snapshot()
        modes = blob_upload_modes(concrete)
        if "direct" not in modes:
            raise RemoteServerError(
                "BLOB_CONFLICT",
                "当前对象存储不支持直传，请使用代理或 multipart 上传。",
            )
        attests_md5 = "direct_md5" in modes
        # One lookup for the whole batch, and only when it can matter: the
        # caller asked to claim and named the messages (PSU-2.143).
        message_refs: Mapping[str, tuple[str, ...]] = {}
        if claim and record_refs is not None:
            wanted = [item.record_key for item in items if item.record_key is not None]
            if wanted:
                message_refs = record_refs(archive_id, wanted)
        expires_at = max(1, int(time.time())) + DIRECT_UPLOAD_TTL_SECONDS
        return {
            "storage_origin": _storage_origin(concrete, archive_id),
            "expires_at": expires_at,
            "items": [
                _presign_one(
                    concrete,
                    generation,
                    archive_id,
                    item,
                    expires_at,
                    claim,
                    attests_md5,
                    message_refs,
                )
                for item in items
            ],
        }

    @app.post("/api/v1/archives/{archive_id}/blobs/batch-presign")
    async def batch_presign_blobs(
        archive_id: str,
        request: Request,
    ) -> dict[str, object]:
        """Register N manifests and hand back N write capabilities, in one trip.

        Registering and signing live in one endpoint because they are born
        together: split, they cost two round trips to do one thing, and the
        whole point of this unit is that the round trips were the bill.

        **Signing writes no session state.** The only state transition on this
        surface is ``batch-confirm``. An expired capability, a failed transfer
        and a client that died mid-batch therefore look identical from here —
        nothing happened, do it again — and that is what makes the client's
        crash recovery a re-send rather than a reconciliation.
        """
        authenticate_request(request, archive_id, auth_store)
        body = await _json_object(request)
        claim = _claim_requested(request)
        items = _batch_presign_input(body, claim=claim)
        return await run_in_threadpool(_batch_presign, archive_id, items, claim)

    def _confirm_one(
        generation: int,
        archive_id: str,
        blob_id: str,
        described: Mapping[str, ObjectAttributes],
        headed: Mapping[str, ObjectHead],
        head_keys: frozenset[str],
    ) -> dict[str, object]:
        manifest = blob_state_store.get_manifest(archive_id, blob_id)
        if manifest is None:
            return _conflict_item(blob_id, "manifest_missing")
        if manifest.state == "remote_ready":
            return _ready_item(manifest)
        if manifest.storage_generation != generation:
            return _conflict_item(blob_id, "storage_generation_mismatch")
        version_id: str | None
        if manifest.object_key in head_keys:
            # PSU-2.131: the store refused any upload whose MD5 differed from
            # the one signed in, and for a single-part object its ETag is that
            # MD5. A composite ETag ("<hex>-<parts>") is never an MD5 of the
            # bytes, so it can never match a 32-hex declaration.
            head = headed.get(manifest.object_key)
            if head is None:
                return _pending_item(blob_id, "object_missing")
            if head.size != manifest.size or head.etag != manifest.md5:
                return _pending_item(blob_id, "checksum_mismatch")
            version_id, attestation = head.version_id, "md5"
        else:
            attributes = described.get(manifest.object_key)
            if attributes is None:
                # No object, or one whose bytes the store will not vouch for.
                # Owed, not failed: the client re-signs and re-sends this one.
                # Nothing is deleted — the key is unreadable while the manifest
                # says so, and the retry overwrites it.
                return _pending_item(blob_id, "object_missing")
            if attributes.size != manifest.size or attributes.sha256 != manifest.sha256:
                return _pending_item(blob_id, "checksum_mismatch")
            version_id, attestation = attributes.version_id, "sha256"
        try:
            ready = blob_state_store.mark_manifest_ready(
                archive_id,
                blob_id,
                size=manifest.size,
                sha256=manifest.sha256,
                object_key=manifest.object_key,
                object_version=version_id or uuid.uuid4().hex,
                verified_at=max(1, int(time.time())),
                storage_generation=generation,
                attestation=attestation,
            )
        except BlobStateConflictError:
            # Something else moved this manifest between the read above and
            # this compare-and-swap. Reported, not swallowed: the whole reason
            # `mark_manifest_ready` re-reads under BEGIN IMMEDIATE is so that
            # exactly one writer can believe it won.
            return _conflict_item(blob_id, "state_conflict")
        return _ready_item(ready)

    def _batch_confirm(archive_id: str, blob_ids: tuple[str, ...]) -> dict[str, object]:
        if object_store_snapshot is None:
            concrete, generation = object_store, _generation()
        else:
            concrete, generation = object_store_snapshot()
        scope = object_scope_for_archive(archive_id)
        # PSU-2.131: on a store that cannot vouch for a SHA-256 but enforces a
        # signed MD5, a manifest that declared one is confirmed by HEAD alone.
        # A store that *can* vouch for the SHA-256 keeps doing so — the
        # stronger proof — whether or not the client also sent an MD5.
        try:
            capabilities = store_capabilities(concrete)
            by_md5 = (
                capabilities.content_md5
                and not capabilities.checksum_sha256
                and isinstance(concrete, ObjectHeadReader)
            )
        except (ObjectStoreError, ObjectStoreValidationError):
            by_md5 = False
        describe_keys: list[str] = []
        head_keys: list[str] = []
        for blob_id in blob_ids:
            manifest = blob_state_store.get_manifest(archive_id, blob_id)
            if (
                manifest is not None
                and manifest.state != "remote_ready"
                and manifest.storage_generation == generation
            ):
                if by_md5 and manifest.md5 is not None:
                    head_keys.append(manifest.object_key)
                else:
                    describe_keys.append(manifest.object_key)
        # One batched interrogation of the store instead of N. This is the
        # whole trade the unit makes: the N that used to cross a border now
        # crosses a LAN.
        described: Mapping[str, ObjectAttributes] = (
            concrete.describe_objects(scope, tuple(describe_keys)) if describe_keys else {}
        )
        headed: Mapping[str, ObjectHead] = (
            cast(ObjectHeadReader, concrete).head_objects(scope, tuple(head_keys))
            if head_keys
            else {}
        )
        heading = frozenset(head_keys)
        return {
            "items": [
                _confirm_one(generation, archive_id, blob_id, described, headed, heading)
                for blob_id in blob_ids
            ]
        }

    @app.post("/api/v1/archives/{archive_id}/blobs/batch-confirm")
    async def batch_confirm_blobs(
        archive_id: str,
        request: Request,
    ) -> dict[str, object]:
        """Verify N directly-uploaded objects and mark the good ones ready.

        The digest compared here is one the **store** produced — the platform
        checksum it computed on write, or a hash this process recomputed by
        reading the object back (`ObjectStore.describe_objects`). Never the
        ``archive-sha256`` user metadata: under direct upload that is written
        by whoever holds the capability, so trusting it would make this
        endpoint agree with the claim instead of checking it.
        """
        authenticate_request(request, archive_id, auth_store)
        body = await _json_object(request)
        blob_ids = _batch_confirm_input(body)
        return await run_in_threadpool(_batch_confirm, archive_id, blob_ids)

    @app.post("/api/v1/archives/{archive_id}/blobs/reconcile")
    async def reconcile_blob_manifests(
        archive_id: str,
        request: Request,
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        body = await _json_object(request)
        if set(body) != {"expected"} or not isinstance(body.get("expected"), list):
            raise _invalid()
        raw_expected = cast(list[object], body["expected"])
        expected = tuple(_expectation(item) for item in raw_expected)
        result = await run_in_threadpool(
            blob_state_store.reconcile_manifests,
            archive_id,
            expected,
        )
        return {
            "missing_in_store": list(result.missing_in_store),
            "checksum_mismatches": list(result.checksum_mismatches),
            "orphaned_in_store": list(result.orphaned_in_store),
        }

    @app.put("/api/v1/archives/{archive_id}/blobs/{blob_id}/content")
    async def put_blob_content(
        archive_id: str,
        blob_id: str,
        request: Request,
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        if manifest.size > MAX_BLOB_BYTES:
            raise _invalid("附件超过代理上传上限，请使用 multipart 上传。")
        if manifest.state == "uploading_parts":
            raise RemoteServerError("BLOB_CONFLICT", "附件正在分段上传。")
        if manifest.state == "remote_ready":
            operation_store, _operation_generation = _store_for_manifest(manifest)
            attributes = await run_in_threadpool(
                operation_store.download_object,
                manifest.object_scope,
                manifest.object_key,
                cast(BinaryIO, _DiscardSink()),
            )
            _require_verified_attributes(manifest, attributes)
            return _ready_response(manifest)

        body = await _read_bounded(request, limit=MAX_BLOB_BYTES)
        if len(body) != manifest.size:
            raise _invalid("上传字节数与清单 size 不一致。")
        if hashlib.sha256(body).hexdigest() != manifest.sha256:
            raise _invalid("上传内容摘要与清单 sha256 不一致。")
        operation_store, operation_generation = _store_for_manifest(manifest)
        attributes = await run_in_threadpool(
            operation_store.put_object,
            manifest.object_scope,
            manifest.object_key,
            body,
            sha256=manifest.sha256,
        )
        _require_verified_attributes(manifest, attributes)
        object_version = attributes.version_id or uuid.uuid4().hex
        verified_at = max(1, int(time.time()))
        try:
            ready = await run_in_threadpool(
                blob_state_store.mark_manifest_ready,
                archive_id,
                blob_id,
                size=manifest.size,
                sha256=manifest.sha256,
                object_key=manifest.object_key,
                object_version=object_version,
                verified_at=verified_at,
                storage_generation=operation_generation,
            )
        except BlobStateConflictError:
            try:
                await run_in_threadpool(
                    operation_store.delete_object,
                    manifest.object_scope,
                    manifest.object_key,
                )
            except ObjectStoreError:
                pass
            raise
        return _ready_response(ready)

    @app.get("/api/v1/archives/{archive_id}/blobs/{blob_id}/content")
    async def get_blob_content(
        archive_id: str,
        blob_id: str,
        request: Request,
    ) -> StreamingResponse:
        authenticate_request(request, archive_id, auth_store)
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None or manifest.state != "remote_ready":
            raise RemoteServerError("NOT_FOUND", "附件尚未完成远程校验。")
        operation_store, _operation_generation = _store_for_manifest(manifest)
        stream = cast(BinaryIO, tempfile.TemporaryFile(mode="w+b"))
        try:
            attributes = await run_in_threadpool(
                operation_store.download_object,
                manifest.object_scope,
                manifest.object_key,
                stream,
            )
            _require_verified_attributes(manifest, attributes)
            stream.seek(0)
        except BaseException:
            stream.close()
            raise
        return StreamingResponse(
            _verified_file_chunks(stream),
            media_type="application/octet-stream",
        )

    @app.post("/api/v1/archives/{archive_id}/blobs/{blob_id}/uploads")
    async def create_blob_multipart_upload(
        archive_id: str,
        blob_id: str,
        request: Request,
    ) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, auth_store)
        body = await _json_object(request)
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        operation_store, _operation_generation = _store_for_manifest(manifest)
        client_upload_id, part_size, parts, plan_sha256 = _multipart_input(
            body,
            manifest,
        )
        prior = await run_in_threadpool(
            blob_state_store.find_upload_by_client,
            archive_id,
            auth.device_id,
            client_upload_id,
        )
        if prior is not None:
            if prior.blob_id != blob_id or prior.plan_sha256 != plan_sha256:
                raise RemoteServerError(
                    "BLOB_CONFLICT",
                    "同一 client_upload_id 携带了不同上传计划。",
                )
            return await _upload_status_payload(
                prior,
                blob_state_store,
                operation_store,
            )
        if manifest.state == "remote_ready":
            raise RemoteServerError("BLOB_CONFLICT", "已验证附件不可覆盖。")
        if manifest.state != "pending_upload":
            raise RemoteServerError(
                "BLOB_CONFLICT",
                "附件已有进行中的 multipart 上传。",
            )

        provider_plan = await run_in_threadpool(
            operation_store.plan_upload,
            manifest.object_scope,
            total_size=manifest.size,
            sha256=manifest.sha256,
            part_size=part_size,
            object_key=manifest.object_key,
        )
        session_id = generate_opaque_id()
        while session_id == provider_plan.upload_id:
            session_id = generate_opaque_id()
        create = BlobUploadCreate(
            archive_id=archive_id,
            blob_id=blob_id,
            device_id=auth.device_id,
            client_upload_id=client_upload_id,
            session_id=session_id,
            provider_upload_id=provider_plan.upload_id,
            object_scope=provider_plan.archive_id,
            object_key=provider_plan.object_key,
            total_size=provider_plan.total_size,
            part_size=provider_plan.part_size,
            sha256=provider_plan.sha256,
            plan_sha256=plan_sha256,
            parts=parts,
        )
        try:
            upload = await run_in_threadpool(
                blob_state_store.create_upload,
                create,
            )
        except BlobStateConflictError:
            try:
                await run_in_threadpool(
                    operation_store.abort_upload,
                    provider_plan,
                )
            except ObjectStoreError:
                pass
            winner = await run_in_threadpool(
                blob_state_store.find_upload_by_client,
                archive_id,
                auth.device_id,
                client_upload_id,
            )
            if (
                winner is not None
                and winner.blob_id == blob_id
                and winner.plan_sha256 == plan_sha256
            ):
                return await _upload_status_payload(
                    winner,
                    blob_state_store,
                    operation_store,
                )
            raise
        except BaseException:
            try:
                await run_in_threadpool(
                    operation_store.abort_upload,
                    provider_plan,
                )
            except ObjectStoreError:
                pass
            raise
        return await _upload_status_payload(
            upload,
            blob_state_store,
            operation_store,
        )

    @app.get(
        "/api/v1/archives/{archive_id}/blobs/{blob_id}/uploads/{session_id}"
    )
    async def get_blob_multipart_status(
        archive_id: str,
        blob_id: str,
        session_id: str,
        request: Request,
    ) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, auth_store)
        upload = await run_in_threadpool(
            blob_state_store.get_upload,
            archive_id,
            blob_id,
            session_id,
            auth.device_id,
        )
        if upload is None:
            raise _upload_not_found()
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        operation_store, _operation_generation = _store_for_manifest(manifest)
        return await _upload_status_payload(
            upload,
            blob_state_store,
            operation_store,
        )

    @app.put(
        "/api/v1/archives/{archive_id}/blobs/{blob_id}"
        "/uploads/{session_id}/parts/{part_number}"
    )
    async def put_blob_multipart_part(
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        request: Request,
    ) -> dict[str, object]:
        authorization = request.headers.get("Authorization")
        if authorization is None or not authorization.startswith("Upload "):
            raise _upload_unauthorized()
        secret = authorization.removeprefix("Upload ")
        if not secret:
            raise _upload_unauthorized()
        authorized = await run_in_threadpool(
            blob_state_store.authorize_part,
            archive_id,
            blob_id,
            session_id,
            part_number,
            capability_hash=sha256_hex(secret),
            now=max(1, int(time.time())),
        )
        if authorized is None:
            raise _upload_unauthorized()
        upload, part = authorized
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        operation_store, _operation_generation = _store_for_manifest(manifest)
        raw_length = request.headers.get("Content-Length")
        try:
            declared_length = int(raw_length) if raw_length is not None else -1
        except ValueError:
            raise _invalid("Content-Length 必须是整数。") from None
        if declared_length != part.size:
            raise _invalid("Content-Length 与计划 part 大小不一致。")
        if request.headers.get("X-Content-SHA256") != part.sha256:
            raise RemoteServerError(
                "BLOB_INTEGRITY",
                "part checksum header 与上传计划不一致。",
            )
        body = await _read_bounded(request, limit=part.size)
        if len(body) != part.size:
            raise _invalid("part 实际字节数与上传计划不一致。")
        if hashlib.sha256(body).hexdigest() != part.sha256:
            raise RemoteServerError(
                "BLOB_INTEGRITY",
                "part 内容 checksum 校验失败。",
            )
        completed = await run_in_threadpool(
            operation_store.upload_part,
            _object_upload_plan(upload),
            part.part_number,
            body,
        )
        if (
            completed.part_number != part.part_number
            or (
                completed.sha256 is not None
                and completed.sha256 != part.sha256
            )
        ):
            await run_in_threadpool(
                blob_state_store.mark_upload_integrity_failed,
                archive_id,
                blob_id,
                session_id,
            )
            raise ObjectIntegrityError("object store returned an invalid uploaded part")
        await run_in_threadpool(
            blob_state_store.mark_part_completed,
            archive_id,
            blob_id,
            session_id,
            part.part_number,
            etag=completed.entity_tag,
            completed_at=max(1, int(time.time())),
            expected_provider_upload_id=upload.provider_upload_id,
        )
        return {
            "part_number": completed.part_number,
            "etag": completed.entity_tag,
            "state": "completed",
        }

    @app.post(
        "/api/v1/archives/{archive_id}/blobs/{blob_id}"
        "/uploads/{session_id}/complete"
    )
    async def complete_blob_multipart_upload(
        archive_id: str,
        blob_id: str,
        session_id: str,
        request: Request,
    ) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, auth_store)
        upload = await run_in_threadpool(
            blob_state_store.get_upload,
            archive_id,
            blob_id,
            session_id,
            auth.device_id,
        )
        if upload is None:
            raise _upload_not_found()
        if upload.state == "remote_ready":
            return _verified_upload_response(upload)
        if upload.state in ("aborted", "integrity_failed"):
            raise RemoteServerError("BLOB_CONFLICT", "上传会话已结束。")
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        operation_store, operation_generation = _store_for_manifest(manifest)

        upload = await _synchronize_uploaded_parts(
            upload,
            blob_state_store,
            operation_store,
        )
        if any(part.etag is None for part in upload.parts):
            raise RemoteServerError(
                "BLOB_CONFLICT",
                "仍有 part 未上传，不能完成附件。",
                retryable=True,
            )
        finalizer_id = generate_opaque_id()
        try:
            upload = await run_in_threadpool(
                blob_state_store.claim_upload_completion,
                archive_id,
                blob_id,
                session_id,
                finalizer_id=finalizer_id,
                now=max(1, int(time.time())),
                lease_seconds=MULTIPART_FINALIZATION_LEASE_SECONDS,
            )
        except BlobStateConflictError:
            raise RemoteServerError(
                "BLOB_CONFLICT",
                "附件正在完成校验，请稍后重试。",
                retryable=True,
            ) from None
        recovered = await _recover_completed_upload(
            upload,
            blob_state_store,
            operation_store,
            finalizer_id=finalizer_id,
            storage_generation=operation_generation,
        )
        if recovered is not None:
            return _verified_upload_response(recovered)

        plan = _object_upload_plan(upload)
        completed_parts = tuple(
            CompletedPart(
                part_number=part.part_number,
                entity_tag=cast(str, part.etag),
                sha256=part.sha256,
            )
            for part in upload.parts
        )
        try:
            attributes = await run_in_threadpool(
                operation_store.complete_upload,
                plan,
                completed_parts,
            )
            if (
                attributes.size != upload.total_size
                or attributes.sha256 != upload.sha256
            ):
                raise ObjectIntegrityError(
                    "completed object does not match multipart plan"
                )
        except ObjectIntegrityError:
            await run_in_threadpool(
                blob_state_store.mark_upload_integrity_failed,
                archive_id,
                blob_id,
                session_id,
                finalizer_id=finalizer_id,
            )
            try:
                await run_in_threadpool(
                    operation_store.abort_upload,
                    plan,
                )
            except ObjectStoreError:
                pass
            raise
        except (ObjectStoreError, ObjectStoreValidationError):
            recovered_after_error = await _recover_completed_upload(
                upload,
                blob_state_store,
                operation_store,
                finalizer_id=finalizer_id,
                storage_generation=operation_generation,
            )
            if recovered_after_error is not None:
                return _verified_upload_response(recovered_after_error)
            raise
        ready = await run_in_threadpool(
            blob_state_store.mark_upload_ready,
            archive_id,
            blob_id,
            session_id,
            finalizer_id=finalizer_id,
            object_version=attributes.version_id or uuid.uuid4().hex,
            verified_at=max(1, int(time.time())),
            storage_generation=operation_generation,
        )
        return _verified_upload_response(ready)

    @app.delete(
        "/api/v1/archives/{archive_id}/blobs/{blob_id}/uploads/{session_id}"
    )
    async def abort_blob_multipart_upload(
        archive_id: str,
        blob_id: str,
        session_id: str,
        request: Request,
    ) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, auth_store)
        upload = await run_in_threadpool(
            blob_state_store.get_upload,
            archive_id,
            blob_id,
            session_id,
            auth.device_id,
        )
        if upload is None:
            raise _upload_not_found()
        if upload.state == "remote_ready":
            raise RemoteServerError(
                "BLOB_CONFLICT",
                "已验证附件不能中止或覆盖。",
            )
        manifest = await run_in_threadpool(
            blob_state_store.get_manifest,
            archive_id,
            blob_id,
        )
        if manifest is None:
            raise RemoteServerError("NOT_FOUND", "附件清单不存在。")
        operation_store, _operation_generation = _store_for_manifest(manifest)
        if upload.state != "aborted":
            try:
                upload = await run_in_threadpool(
                    blob_state_store.mark_upload_aborted,
                    archive_id,
                    blob_id,
                    session_id,
                    now=max(1, int(time.time())),
                )
            except BlobStateConflictError:
                if upload.state == "completing":
                    raise RemoteServerError(
                        "BLOB_CONFLICT",
                        "附件正在完成校验，不能中止。",
                        retryable=True,
                    ) from None
                raise
        try:
            await run_in_threadpool(
                operation_store.abort_upload,
                _object_upload_plan(upload),
            )
        except ObjectStoreError:
            pass
        return {
            "blob_id": blob_id,
            "session_id": upload.session_id,
            "state": "aborted",
        }

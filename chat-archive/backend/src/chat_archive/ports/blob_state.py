"""Persistent state required to orchestrate the remote blob HTTP contract."""

from __future__ import annotations

import hashlib
import re
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol


class BlobStateError(RuntimeError):
    """Base class for durable blob state failures."""


class BlobStateValidationError(ValueError):
    """A manifest or upload state input violates the remote contract."""


class BlobStateConflictError(BlobStateError):
    """A compare-and-swap or idempotency invariant was violated."""


_REMOTE_ID = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_SESSION_ID = re.compile(r"^[0-9a-f]{32}$")
_MANIFEST_STATES = frozenset({"pending_upload", "uploading_parts", "remote_ready"})
_UPLOAD_STATES = frozenset(
    {
        "planned",
        "uploading_parts",
        "completing",
        "remote_ready",
        "aborted",
        "integrity_failed",
    }
)


def require_remote_id(value: str, *, field_name: str) -> None:
    if _REMOTE_ID.fullmatch(value) is None:
        raise BlobStateValidationError(f"{field_name} is not a valid opaque identifier")


def require_content_digest(value: str) -> None:
    if _SHA256_HEX.fullmatch(value) is None:
        raise BlobStateValidationError("blob checksum must be lowercase SHA-256 hex")


def require_manifest_descriptor(
    blob_id: str,
    *,
    size: int,
    sha256: str,
    state: str,
) -> None:
    require_remote_id(blob_id, field_name="blob_id")
    if isinstance(size, bool) or size < 0:
        raise BlobStateValidationError("blob size must be a non-negative integer")
    require_content_digest(sha256)
    if state not in _MANIFEST_STATES:
        raise BlobStateValidationError("blob manifest state is invalid")


def object_scope_for_archive(archive_id: str) -> str:
    """Map the public archive ID to an ObjectStore-compatible opaque scope."""
    require_remote_id(archive_id, field_name="archive_id")
    material = b"chat-archive-object-scope\0" + archive_id.encode("utf-8")
    return hashlib.sha256(material).hexdigest()[:32]


@dataclass(frozen=True)
class BlobManifestRecord:
    archive_id: str
    blob_id: str
    size: int
    sha256: str
    state: str
    object_scope: str = field(repr=False)
    object_key: str = field(repr=False)
    object_version: str | None = None
    verified_at: int | None = None
    # Which object storage generation holds these bytes. Rows that predate the
    # column are 1, which is also the first generation any configuration gets,
    # so a single-bucket deployment never sees a mismatch.
    storage_generation: int = 1
    # The MD5 the uploader declared alongside the SHA-256, when it declared one
    # (PSU-2.131). On a store that ignores `x-amz-checksum-sha256` it is signed
    # into the upload and checked against the ETag at confirmation.
    md5: str | None = None
    # How the bytes were vouched for when the row became ready: "sha256" (a
    # digest the store computed or this server recomputed) or "md5" (the store
    # refused any other MD5 and its ETag agrees). None = became ready before
    # anyone wrote this down, which means sha256.
    attestation: str | None = None


@dataclass(frozen=True)
class BlobManifestExpectation:
    blob_id: str
    size: int
    sha256: str
    state: str
    object_version: str | None = None
    verified_at: int | None = None


@dataclass(frozen=True)
class BlobManifestReconciliation:
    missing_in_store: tuple[str, ...]
    checksum_mismatches: tuple[str, ...]
    orphaned_in_store: tuple[str, ...]


@dataclass(frozen=True)
class BlobPartExpectation:
    part_number: int
    size: int
    sha256: str


@dataclass(frozen=True)
class BlobPartRecord:
    part_number: int
    size: int
    sha256: str
    etag: str | None = None
    completed_at: int | None = None
    capability_hash: str | None = field(default=None, repr=False)
    capability_expires_at: int | None = None


@dataclass(frozen=True)
class BlobUploadCreate:
    archive_id: str
    blob_id: str
    device_id: str
    client_upload_id: str
    session_id: str
    provider_upload_id: str = field(repr=False)
    object_scope: str = field(repr=False)
    object_key: str = field(repr=False)
    total_size: int
    part_size: int
    sha256: str
    plan_sha256: str
    parts: tuple[BlobPartExpectation, ...]


@dataclass(frozen=True)
class BlobUploadRecord:
    archive_id: str
    blob_id: str
    device_id: str
    client_upload_id: str
    session_id: str
    provider_upload_id: str = field(repr=False)
    object_scope: str = field(repr=False)
    object_key: str = field(repr=False)
    total_size: int
    part_size: int
    sha256: str
    plan_sha256: str
    state: str
    object_version: str | None
    verified_at: int | None
    parts: tuple[BlobPartRecord, ...]


def require_upload_create(upload: BlobUploadCreate) -> None:
    require_remote_id(upload.archive_id, field_name="archive_id")
    require_remote_id(upload.blob_id, field_name="blob_id")
    require_remote_id(upload.device_id, field_name="device_id")
    require_remote_id(upload.client_upload_id, field_name="client_upload_id")
    if _SESSION_ID.fullmatch(upload.session_id) is None:
        raise BlobStateValidationError("session_id must be lowercase 32-hex")
    if (
        not upload.provider_upload_id
        or upload.total_size <= 0
        or upload.part_size <= 0
        or _SHA256_HEX.fullmatch(upload.sha256) is None
        or _SHA256_HEX.fullmatch(upload.plan_sha256) is None
        or not upload.parts
    ):
        raise BlobStateValidationError("multipart upload descriptor is invalid")
    total = 0
    for index, part in enumerate(upload.parts, start=1):
        if (
            part.part_number != index
            or part.size <= 0
            or _SHA256_HEX.fullmatch(part.sha256) is None
        ):
            raise BlobStateValidationError("multipart part descriptor is invalid")
        total += part.size
    if total != upload.total_size:
        raise BlobStateValidationError("multipart part sizes do not match total size")


def require_upload_state(state: str) -> None:
    if state not in _UPLOAD_STATES:
        raise BlobStateValidationError("multipart upload state is invalid")


@dataclass(frozen=True)
class BlobStorageHealth:
    """这个归档在对象存储上占了多少、还有多少没落地（PSU-2.57）。

    住在**这里**而不是 metadata store 上，是因为字节的账在这里记。健康页从前
    问的是 `metadata` 的 `blob_manifests`——那张表自 blob 状态搬家之后就没有任何
    写者了（`upsert_blob_manifest` 在 `src/` 里零调用），于是三个数字恒为 0：
    存量看着像空的，而 `pending_blob_count` 恒 0 意味着 `ATTACHMENTS_PENDING`
    永远不会亮，是漏报不是难看。
    """

    used_bytes: int
    blob_count: int
    pending_blob_count: int


class BlobStateStore(Protocol):
    def storage_health(self, archive_id: str) -> BlobStorageHealth: ...

    def list_manifests(self, archive_id: str) -> tuple[BlobManifestRecord, ...]:
        """这个归档的全部清单，**含未落地的那些**，供对账两个方向都用得上。

        对账要问两个不同的问题，各自需要的清单集合不一样（PSU-2.58）：

        - 「说好有、桶里却没有」只能问 ``remote_ready``。还在传的那些本来就
          不该被要求在桶里，算进缺失会让一次进行中的上传看起来像数据丢了。
        - 「桶里有、没人认领」必须问**全部状态**。字节是先 PUT 进桶、再确认成
          ``remote_ready`` 的，所以每一次正常上传都有一个窗口：对象已经在桶里
          而清单还是 ``pending_upload``。只拿 ``remote_ready`` 当认领名单，
          这些对象会被报成孤儿——同一个误报换了个方向而已。

        过滤留给调用方，因为只有它同时知道这两个问题。
        """
        ...

    def put_manifest(
        self,
        archive_id: str,
        blob_id: str,
        *,
        size: int,
        sha256: str,
        storage_generation: int = 1,
        md5: str | None = None,
    ) -> BlobManifestRecord:
        """``md5`` fills a row that has none; a *different* one is a conflict (PSU-2.131)."""
        ...

    def manifest_states(
        self,
        archive_id: str,
        blob_ids: Sequence[str],
    ) -> dict[str, str]:
        """这些 blob 各自的清单状态，查不到的直接不出现（PSU-2.82）。"""
        ...

    def find_ready_manifest_by_content(
        self,
        archive_id: str,
        *,
        sha256: str,
        size: int,
        storage_generation: int,
    ) -> BlobManifestRecord | None:
        """这个归档里、当前代次下、已核验过的同内容清单（PSU-2.82）。

        查找**锁死在 archive_id 之内**。归档归一个人所有，所以归档内的内容
        寻址回答不了它的主人不知道的事；跨归档探测仍然不可能，
        `remote_blob_id` 的非关联性质原样成立。
        """
        ...

    def get_manifest(self, archive_id: str, blob_id: str) -> BlobManifestRecord | None: ...

    def mark_manifest_ready(
        self,
        archive_id: str,
        blob_id: str,
        *,
        size: int,
        sha256: str,
        object_key: str,
        object_version: str,
        verified_at: int,
        storage_generation: int = 1,
        attestation: str | None = None,
    ) -> BlobManifestRecord:
        """``attestation`` is ``"sha256"`` or ``"md5"``: how this server vouched for the bytes."""
        ...

    def reconcile_manifests(
        self,
        archive_id: str,
        expected: tuple[BlobManifestExpectation, ...],
    ) -> BlobManifestReconciliation: ...

    def find_upload_by_client(
        self,
        archive_id: str,
        device_id: str,
        client_upload_id: str,
    ) -> BlobUploadRecord | None: ...

    def create_upload(self, upload: BlobUploadCreate) -> BlobUploadRecord: ...

    def get_upload(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        device_id: str,
    ) -> BlobUploadRecord | None: ...

    def set_part_capability(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        capability_hash: str,
        expires_at: int,
    ) -> None: ...

    def authorize_part(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        capability_hash: str,
        now: int,
    ) -> tuple[BlobUploadRecord, BlobPartRecord] | None: ...

    def mark_part_completed(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        part_number: int,
        *,
        etag: str,
        completed_at: int,
        expected_provider_upload_id: str | None = None,
    ) -> BlobUploadRecord: ...

    def sync_completed_parts(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        parts: tuple[tuple[int, str], ...],
        *,
        completed_at: int,
        expected_provider_upload_id: str | None = None,
    ) -> BlobUploadRecord: ...

    def replace_missing_provider_upload(
        self, upload: BlobUploadRecord, provider_upload_id: str,
    ) -> BlobUploadRecord | None: ...

    def claim_upload_completion(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str,
        now: int,
        lease_seconds: int,
    ) -> BlobUploadRecord: ...

    def mark_upload_ready(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str,
        object_version: str,
        verified_at: int,
        storage_generation: int = 1,
    ) -> BlobUploadRecord: ...

    def mark_upload_integrity_failed(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        finalizer_id: str | None = None,
    ) -> BlobUploadRecord: ...

    def mark_upload_aborted(
        self,
        archive_id: str,
        blob_id: str,
        session_id: str,
        *,
        now: int,
    ) -> BlobUploadRecord: ...

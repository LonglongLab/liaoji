"""Vendor-neutral object storage contract for archive attachments.

Object keys are opaque: an archive-scoped prefix plus a random ID, never a
file name, wxid, conversation name, date or content hash (FR-BLOB-002).
Presigned URLs and signed headers are sensitive values and are excluded
from every repr (FR-BLOB-005). Completion must verify size and an explicit
whole-object SHA-256 before attributes are returned — ETag is never used
as a content hash (FR-BLOB-006/007).
"""

from __future__ import annotations

import re
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, fields
from typing import BinaryIO, Protocol, runtime_checkable


class ObjectStoreError(RuntimeError):
    """Base class for object storage failures."""


class ObjectMultipartUploadMissingError(ObjectStoreError):
    """The provider no longer has a previously allocated multipart session."""


class ObjectStoreValidationError(ValueError):
    """A key, plan or argument violates the vendor-neutral contract."""


class ObjectNotFoundError(ObjectStoreError):
    """An archive-scoped object key has no verified object."""


class ObjectIntegrityError(ObjectStoreError):
    """Stored content does not match the declared checksum or size."""


class ObjectImmutabilityError(ObjectStoreError):
    """A verified object key was targeted for overwrite."""


class ObjectStoreCapabilityError(ObjectStoreValidationError):
    """The backend cannot do what this call needs, and will not pretend to.

    Raised instead of returning something unusable — a presigned URL from a
    backend that cannot sign is a link that fails at the far end, minutes
    later, with the caller no longer around to read the reason.

    A subclass of :class:`ObjectStoreValidationError` so the existing "invalid
    object request" mapping keeps holding for callers that never learned to
    ask; the distinct type is for the ones that route around a gap.
    """


_ARCHIVE_ID = re.compile(r"^[a-z0-9_-]{1,64}$")
_OBJECT_KEY = re.compile(r"^a/[a-z0-9_-]{1,64}/[0-9a-f]{32}$")
_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")


def new_object_key(archive_id: str) -> str:
    """Mint an opaque, archive-scoped object key."""
    if _ARCHIVE_ID.fullmatch(archive_id) is None:
        raise ObjectStoreValidationError("archive_id must be an opaque lowercase identifier")
    return f"a/{archive_id}/{uuid.uuid4().hex}"


def require_archive_key(archive_id: str, object_key: str) -> None:
    """Fail closed unless the key is well-formed and belongs to the archive."""
    if _ARCHIVE_ID.fullmatch(archive_id) is None:
        raise ObjectStoreValidationError("archive_id must be an opaque lowercase identifier")
    if _OBJECT_KEY.fullmatch(object_key) is None:
        raise ObjectStoreValidationError("object key is not a well-formed opaque key")
    if not object_key.startswith(f"a/{archive_id}/"):
        raise ObjectStoreValidationError("object key does not belong to this archive")


def require_sha256(value: str) -> None:
    if _SHA256_HEX.fullmatch(value) is None:
        raise ObjectStoreValidationError("checksum must be lowercase SHA-256 hex")


@dataclass(frozen=True)
class PartPlan:
    part_number: int
    offset: int
    size: int


def plan_parts(*, total_size: int, part_size: int) -> tuple[PartPlan, ...]:
    """Split an object into sequential parts; the final part takes the remainder."""
    if total_size <= 0 or part_size <= 0:
        raise ObjectStoreValidationError("total_size and part_size must be positive")
    parts: list[PartPlan] = []
    offset = 0
    part_number = 1
    while offset < total_size:
        size = min(part_size, total_size - offset)
        parts.append(PartPlan(part_number=part_number, offset=offset, size=size))
        offset += size
        part_number += 1
    return tuple(parts)


@dataclass(frozen=True)
class UploadPlan:
    archive_id: str
    object_key: str = field(repr=False)
    upload_id: str = field(repr=False)
    total_size: int
    part_size: int
    sha256: str

    def parts(self) -> tuple[PartPlan, ...]:
        return plan_parts(total_size=self.total_size, part_size=self.part_size)


@dataclass(frozen=True)
class CompletedPart:
    part_number: int
    entity_tag: str
    # None when the part was listed back from the provider, which cannot
    # return content hashes; whole-object verification still applies.
    sha256: str | None = None


@dataclass(frozen=True)
class PresignedRequest:
    url: str = field(repr=False)
    method: str = "GET"
    headers: Mapping[str, str] = field(repr=False, default_factory=dict[str, str])
    expires_in_seconds: int = 300


@dataclass(frozen=True)
class ObjectAttributes:
    size: int
    sha256: str
    version_id: str | None = None


_INFORMATIONAL_CAPABILITIES = frozenset({"conditional_writes", "content_md5"})

_MD5_PATTERN = re.compile(r"^[0-9a-f]{32}$")


def require_md5(value: str) -> str:
    """A lowercase hex MD5, or a validation error that does not echo the value."""
    if _MD5_PATTERN.fullmatch(value) is None:
        raise ObjectStoreValidationError("md5 must be 32 lowercase hex characters")
    return value


@dataclass(frozen=True)
class ObjectHead:
    """What a HEAD says about one object: its length and its bare ETag.

    Not :class:`ObjectAttributes` on purpose. An ETag is only a content digest
    on a store that proved it is (``StoreCapabilities.content_md5``), and only
    for a single-part object — so it travels under its own name, and deciding
    what it proves is left to the caller that knows which store it came from.
    """

    size: int
    etag: str
    version_id: str | None = None


@runtime_checkable
class ObjectHeadReader(Protocol):
    """A backend that can answer "how long, and what ETag" without the bytes."""

    def head_objects(
        self, archive_id: str, object_keys: Sequence[str]
    ) -> Mapping[str, ObjectHead]:
        """Keys with no object are absent, not raised — as in ``describe_objects``."""
        ...


@dataclass(frozen=True)
class StoreCapabilities:
    tls: bool
    multipart: bool
    checksum_sha256: bool
    versioning: bool
    presigned_urls: bool
    conditional_writes: bool
    # ``None`` = the provider would not answer — typically because the key is
    # not allowed to read the bucket's lifecycle configuration. **Not knowing
    # is not absence.** Answering ``False`` there reports a permissions fact as
    # a provider gap, and the verdict downstream turns one such gap into "the
    # object store is unhealthy", which sends someone hunting for a fault that
    # does not exist (PSU-2.57).
    lifecycle_incomplete_multipart: bool | None
    # The store refuses a signed Content-MD5 that does not match the bytes, and
    # a single-part object's ETag is its MD5 (PSU-2.131). Tencent COS is the
    # reason: it ignores `x-amz-checksum-sha256` entirely, so without this the
    # only way to vouch for a direct upload is to read it back — across a
    # border, on billed egress.
    content_md5: bool = False

    def missing(self) -> tuple[str, ...]:
        """Capabilities the provider **said** it does not have, and that matter.

        ``is False`` rather than falsiness: ``None`` means unanswered, and an
        unanswered probe is not evidence of a gap.

        Two are never gaps (PSU-2.131). ``conditional_writes`` gates nothing —
        writes already check for the key first and every key is random — so on
        a store that ignores ``If-None-Match`` it only ever put a permanent
        "degraded" on the health page. ``content_md5`` is an alternative, not a
        requirement. And a platform SHA-256 is not a gap on a store that attests
        by MD5: they are two ways of vouching for the same bytes.
        """
        gaps: list[str] = []
        for item in fields(self):
            if item.name in _INFORMATIONAL_CAPABILITIES:
                continue
            if getattr(self, item.name) is not False:
                continue
            if item.name == "checksum_sha256" and self.content_md5:
                continue
            gaps.append(item.name)
        return tuple(gaps)

    def unknown(self) -> tuple[str, ...]:
        """Capabilities nobody could find out about. Reported, not swallowed."""
        return tuple(
            item.name for item in fields(self) if getattr(self, item.name) is None
        )


@dataclass(frozen=True)
class ObjectReconciliation:
    missing_in_store: tuple[str, ...]
    checksum_mismatches: tuple[str, ...]
    orphaned_in_store: tuple[str, ...]


def reconcile_objects(
    *,
    expected: Mapping[str, str],
    stored: Mapping[str, str],
) -> ObjectReconciliation:
    """Bidirectional manifest-versus-store comparison shared by all backends."""
    return ObjectReconciliation(
        missing_in_store=tuple(sorted(set(expected) - set(stored))),
        checksum_mismatches=tuple(
            sorted(
                object_key
                for object_key in set(expected) & set(stored)
                if expected[object_key] != stored[object_key]
            )
        ),
        orphaned_in_store=tuple(sorted(set(stored) - set(expected))),
    )


@runtime_checkable
class CapabilityCache(Protocol):
    """A backend whose probe result may be asked for cheaply and repeatedly.

    Optional, in the same spirit as
    :class:`~chat_archive.ports.transport.ClosableTransport`: ``localfs``
    answers from a constant and has nothing to cache, while the S3 backend's
    probe writes and deletes real objects — far too expensive for a path like
    ``/api/v1/health``, which is unauthenticated and polled.

    Distinct from ``probe_capabilities`` rather than replacing it, because the
    callers differ in what they are asking. "What can this store do, as of a
    moment ago" is the right answer for route selection; the configuration
    test and the health *report* are asking about right now.
    """

    def cached_capabilities(self) -> StoreCapabilities: ...


class ObjectStore(Protocol):
    def probe_capabilities(self) -> StoreCapabilities: ...

    def put_object(
        self,
        archive_id: str,
        object_key: str,
        data: bytes,
        *,
        sha256: str,
    ) -> ObjectAttributes:
        """Create an immutable verified object, or replay the same content."""
        ...

    def copy_object(
        self,
        source_scope: str,
        source_key: str,
        target_scope: str,
        target_key: str,
        *,
        sha256: str,
        size: int,
    ) -> ObjectAttributes:
        """Duplicate a verified object under a new key without the bytes leaving the store.

        For share copies (PSU-2.184): the caller already knows, from a verified
        manifest, what ``source_key`` holds, and says so with ``sha256`` and
        ``size``. The target is a fresh key that must not exist yet — a copy
        never overwrites (:class:`ObjectImmutabilityError`). A missing source is
        :class:`ObjectNotFoundError`; a copy whose stored length (or, where the
        store can tell without re-reading, digest) is not the declared one is
        deleted again and raised as :class:`ObjectIntegrityError`, so a failed
        copy leaves nothing behind.
        """
        ...

    def plan_upload(
        self,
        archive_id: str,
        *,
        total_size: int,
        sha256: str,
        part_size: int,
        object_key: str | None = None,
    ) -> UploadPlan: ...

    def list_uploaded_parts(self, plan: UploadPlan) -> tuple[CompletedPart, ...]: ...

    def upload_part(self, plan: UploadPlan, part_number: int, data: bytes) -> CompletedPart: ...

    def complete_upload(
        self, plan: UploadPlan, parts: Sequence[CompletedPart]
    ) -> ObjectAttributes: ...

    def abort_upload(self, plan: UploadPlan) -> None: ...

    def download_object(
        self,
        archive_id: str,
        object_key: str,
        sink: BinaryIO,
    ) -> ObjectAttributes:
        """Stream a verified object into ``sink`` and return computed attributes."""
        ...

    def get_object(self, archive_id: str, object_key: str) -> bytes: ...

    def presign_get(
        self, archive_id: str, object_key: str, *, expires_in_seconds: int
    ) -> PresignedRequest: ...

    def presign_put(
        self,
        archive_id: str,
        object_key: str,
        *,
        size: int,
        sha256: str,
        expires_in_seconds: int,
        md5: str | None = None,
    ) -> PresignedRequest:
        """A capability to write **one** object, once, with declared bytes.

        ``md5`` is signed in as ``Content-MD5`` only on a store with
        ``StoreCapabilities.content_md5``; elsewhere it is ignored (PSU-2.131).

        The returned ``headers`` are not advice: they are signed into the URL,
        so a client that drops or alters one gets a signature mismatch from
        the store rather than a differently-shaped object. Whether a checksum
        header is among them depends on ``StoreCapabilities.checksum_sha256``
        — see the module docstring of the S3 adapter for what that buys.

        Raises :class:`ObjectStoreCapabilityError` when the backend cannot
        sign at all.
        """
        ...

    def describe_objects(
        self, archive_id: str, object_keys: Sequence[str]
    ) -> Mapping[str, ObjectAttributes]:
        """What the **store** says these objects are — never what a client said.

        The one rule this contract exists to state: an implementation may
        report a digest the platform computed, or one it recomputed by reading
        the bytes back, and nothing else. User metadata is not a source. Under
        proxied upload those coincided, because only the API could write; under
        direct upload the user metadata is written by whoever holds the
        presigned URL, so believing it would make verification a restatement of
        the claim being verified.

        Keys with no object, or whose stored bytes cannot be vouched for, are
        absent from the result rather than raising: one unreadable object in a
        batch of five hundred must not lose the other 499.
        """
        ...

    def list_object_checksums(self, archive_id: str) -> Mapping[str, str]:
        """Every key in the archive against its **declared** checksum.

        For inventory and orphan-hunting. Not for verification: the S3 backend
        reads this out of user metadata, which under direct upload the client
        wrote. :meth:`describe_objects` is the one that answers "are these
        bytes what the manifest says".
        """
        ...

    def list_object_sizes(self, archive_id: str) -> Mapping[str, int]:
        """Every key in the archive against its stored byte count.

        The cheap half of :meth:`list_object_checksums` (PSU-2.58): a LIST is
        one round trip per page, while the checksum variant HEADs **every
        object** — 79k serial requests on a real archive, far too much for a
        routine reconcile. Existence and length are what a routine reconcile
        needs; the bytes were hashed and verified when they landed.
        """
        ...

    def reconcile(
        self, archive_id: str, expected: Mapping[str, str]
    ) -> ObjectReconciliation: ...

    def delete_object(self, archive_id: str, object_key: str) -> None: ...

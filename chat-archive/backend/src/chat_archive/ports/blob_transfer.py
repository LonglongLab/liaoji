"""Durable ports and value objects for resumable attachment transfers."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


# Opt-in to content claiming on ``batch-presign`` (PSU-2.82). Spelled once,
# here in the port, because both ends of the wire have to agree on it and a
# literal written twice is a literal that eventually disagrees with itself.
#
# A header rather than a request field: the batch's items are
# ``additionalProperties: false``, so a new client asking an old server would
# put the request off-contract merely by asking. And it must stay opt-in —
# an old client parses ``claimable`` fine but then finds no upload request and
# answers ``REMOTE_INVALID_RESPONSE``, i.e. retries forever for an attachment
# that would never go up.
BLOB_CLAIM_HEADER = "x-chat-archive-blob-claim"


class BlobTransferError(RuntimeError):
    """A transfer operation failed with a stable, non-sensitive error code."""

    def __init__(self, code: str, message: str = "blob transfer failed") -> None:
        super().__init__(message)
        self.code = code


class BlobTransferPlanConflict(ValueError):
    """账本里这件附件已经有一份计划，而新的计划与它对不上（PSU-2.127）。

    单独一个类型，是为了让上传循环**认得出**它：字节变了（重解、读路径现解过），
    旧计划就没有价值，该丢掉重立——而不是把它当成随便什么参数错，整趟抛出去。
    仍然继承 `ValueError`，按 `ValueError` 接它的既有调用方不受影响。
    """


@dataclass(frozen=True)
class BlobPartPlan:
    part_number: int
    offset: int
    size: int
    sha256: str


@dataclass(frozen=True)
class BlobUploadRequest:
    url: str
    method: str
    headers: dict[str, str] = field(repr=False)


@dataclass(frozen=True)
class RemoteBlobPart:
    part_number: int
    size: int
    sha256: str
    state: str
    etag: str | None = None
    upload_request: BlobUploadRequest | None = field(default=None, repr=False)


@dataclass(frozen=True)
class RemoteUploadSession:
    session_id: str
    state: str
    parts: tuple[RemoteBlobPart, ...]


@dataclass(frozen=True)
class BlobTransferState:
    attachment_id: str
    archive_id: str
    device_id: str
    blob_id: str
    client_upload_id: str
    session_id: str | None
    total_size: int
    part_size: int
    sha256: str
    status: str
    attempt_count: int
    last_error_code: str | None
    cache_evictable: bool
    parts: tuple[BlobPartPlan, ...] = field(repr=False)


@dataclass(frozen=True)
class BlobTransferResult:
    status: str
    attachment_id: str
    session_id: str | None
    uploaded_parts: int
    cache_evictable: bool
    error_code: str | None = None


class BlobTransferStore(Protocol):
    def prepare_blob_transfer(
        self,
        *,
        attachment_id: str,
        archive_id: str,
        device_id: str,
        blob_id: str,
        client_upload_id: str,
        total_size: int,
        part_size: int,
        sha256: str,
        parts: tuple[BlobPartPlan, ...],
    ) -> BlobTransferState: ...

    def discard_blob_transfer(
        self,
        *,
        attachment_id: str,
        archive_id: str,
        device_id: str,
        allow_forward_thumbnail: bool = False,
    ) -> bool:
        """扔掉一份还没传完的计划，让下一次 `prepare` 重新立一份。

        存在的理由只有一个：**重解出来的字节不保证与第一次逐字节相同**（转码器版本、
        容器时间戳元数据都会变，语音和缩略图尤其）。而 `prepare_blob_transfer` 在
        计划不一致时会抛「conflicting plan」——不清掉旧行，一个重解过的附件就会
        每一轮都失败，永远传不上去。

        已经 `remote_ready` 的行不动：那份身份已经发布出去了，扔掉它等于把远端
        已有的对象变成孤儿。返回是否真的删了。
        """
        ...

    def set_blob_session(self, state: BlobTransferState, session_id: str) -> BlobTransferState:
        """CAS the unfinished plan's session; clear part receipts if it changes.

        A stale snapshot or a completed row raises LOCAL_SESSION_CHANGED.
        The client id and content plan remain unchanged.
        """
        ...

    def mark_blob_part_complete(
        self,
        state: BlobTransferState,
        *,
        part_number: int,
        etag: str,
    ) -> None: ...

    def mark_blob_transfer_failed(self, state: BlobTransferState, *, error_code: str) -> None: ...

    def mark_blob_transfer_ready(
        self,
        state: BlobTransferState,
        *,
        cache_evictable: bool,
    ) -> BlobTransferState: ...

    def mark_blob_transfers_ready(
        self,
        states: Sequence[BlobTransferState],
        *,
        cache_evictable: bool,
    ) -> None:
        """同上，一批一个事务（PSU-2.145）。"""
        ...

    def claim_blob_transfer(
        self,
        *,
        attachment_id: str,
        archive_id: str,
        device_id: str,
        claimed_blob_id: str,
        sha256: str,
    ) -> None:
        """改指到远端已有的那个 blob_id 并置 ready（PSU-2.82）。

        不能靠重跑 ``prepare_blob_transfer``：那条路对 blob_id 不一致的行是
        抛「conflicting plan」，而上传流程先 prepare 后 presign，认领答案到手时
        台账里已经写着本机自己算的那个 id 了。
        """
        ...


@dataclass(frozen=True)
class RemoteBlobDescriptor:
    blob_id: str
    size: int
    sha256: str
    state: str


@runtime_checkable
class BlobDescriptorReader(Protocol):
    def get_manifest_descriptor(self, *, archive_id: str, blob_id: str) -> RemoteBlobDescriptor: ...


@dataclass(frozen=True)
class BlobDirectItem:
    """One object a run wants to put up: what it is and what it hashes to."""

    blob_id: str
    size: int
    sha256: str
    # Only on a run whose remote said `direct_md5` (PSU-2.131), and only ever
    # computed from the same read of the bytes as a SHA-256 that matched `sha256`.
    md5: str | None = None
    # Only on a run whose remote said `direct_record_claim`, and only for a
    # message with exactly one attachment (PSU-2.143).
    record_key: str | None = None


@dataclass(frozen=True)
class BlobDirectTarget:
    """The remote's answer for one item of a signing batch.

    ``state`` is ``pending_upload`` (here is a capability, go),
    ``remote_ready`` (already there, skip — this is the reconcile),
    ``claimable`` (these bytes are already up there under ``ready_blob_id``;
    point at it and send nothing — PSU-2.82) or ``conflict`` (do not send;
    ``reason`` says why).

    ``blob_id`` is always the id **this client asked about**, including on a
    claim: it is what the caller matches answers back to requests by.
    """

    blob_id: str
    state: str
    upload_request: BlobUploadRequest | None = field(default=None, repr=False)
    reason: str | None = None
    ready_blob_id: str | None = None


@dataclass(frozen=True)
class BlobDirectBatch:
    # Which origin every URL in this batch must point at. Checked before a
    # byte is sent, so a tampered signing response cannot redirect the upload
    # to a third party.
    storage_origin: str
    expires_at: int
    targets: tuple[BlobDirectTarget, ...]


@dataclass(frozen=True)
class BlobConfirmation:
    blob_id: str
    state: str
    reason: str | None = None


@runtime_checkable
class DirectBlobTransport(Protocol):
    """A remote that can hand out write capabilities instead of taking bytes.

    Optional, like :class:`~chat_archive.ports.transport.ClosableTransport`:
    the in-memory transports the tests build have no storage behind them, and
    the Cloudflare Workers remote has no S3 credential to sign with. Callers
    check :meth:`upload_modes` and fall back to the proxy path, which is why
    an older remote needs no version negotiation at all.
    """

    def upload_modes(self) -> tuple[str, ...]: ...

    def batch_presign(
        self,
        *,
        archive_id: str,
        items: Sequence[BlobDirectItem],
        claim: bool = False,
    ) -> BlobDirectBatch:
        """``claim`` opts this batch into content claiming (PSU-2.82).

        Off by default, and only the attachment uploader turns it on. A remote
        that does not know the option ignores it and answers exactly as before.
        """
        ...

    def batch_confirm(
        self, *, archive_id: str, blob_ids: Sequence[str]
    ) -> tuple[BlobConfirmation, ...]: ...

    def direct_put(
        self,
        request: BlobUploadRequest,
        content: bytes,
        *,
        storage_origin: str,
    ) -> None:
        """Send one object straight to storage. **Never** with a device token.

        The presigned URL is the authorisation; a token on that origin is
        given away for nothing.
        """
        ...


class RemoteBlobTransport(Protocol):
    def put_manifest(self, *, archive_id: str, blob_id: str, size: int, sha256: str) -> None: ...

    def create_upload(
        self,
        *,
        archive_id: str,
        blob_id: str,
        client_upload_id: str,
        part_size: int,
        parts: tuple[BlobPartPlan, ...],
    ) -> RemoteUploadSession: ...

    def get_upload(
        self,
        *,
        archive_id: str,
        blob_id: str,
        session_id: str,
    ) -> RemoteUploadSession: ...

    def upload_part(self, request: BlobUploadRequest, content: bytes) -> str: ...

    def complete_upload(
        self,
        *,
        archive_id: str,
        blob_id: str,
        session_id: str,
    ) -> None: ...

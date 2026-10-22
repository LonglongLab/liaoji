"""Ports and durable value objects for the local-to-remote sync engine."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from chat_archive.ports.metadata import BatchReceipt


class SyncError(RuntimeError):
    """A sync attempt failed but its durable batch may be retried."""

    def __init__(self, code: str, message: str = "sync attempt failed") -> None:
        super().__init__(message)
        self.code = code


# The record model this build speaks. A remote that declares less than this in
# ``/api/v1/health`` gets a batch shaped for what it can keep — see
# :class:`RecordModelNegotiation`.
LOCAL_RECORD_MODEL_VERSION = 14
FORWARD_MEDIA_MIN_VERSION = 14
# Same wire fields as v14, but preserves canonical text while enriching media
# received from another capture whose parser rendered the summary differently.
FORWARD_MEDIA_ENRICHMENT_MIN_VERSION = 15
FORWARD_MEDIA_PUBLICATION_MIN_VERSION = 16

# The two versions that changed the *upload* bytes, named rather than compared
# against ``LOCAL_RECORD_MODEL_VERSION``. That shortcut held only while this
# build spoke exactly one richer model than v1; the moment a second section
# arrived, ``>= LOCAL_RECORD_MODEL_VERSION`` started meaning "v5 or nothing"
# and would have sent a v4 remote thin records.
RENDER_MODEL_MIN_VERSION = 2
ACCOUNT_IDENTITY_MIN_VERSION = 5
# 通讯录（记录模型 v8，PSU-2.47）。v6 与 v7 是**纯读侧**的，一个字节都没改上传
# 形状，所以那两版没有出现在这里；v8 多了一条发布流，于是必须有自己的下限。
ROSTER_MIN_VERSION = 8
# 语音转写（记录模型 v13，PSU-2.163）。v11/v12 没改上传形状，所以它们没有出现
# 在这里；v13 多了一条发布流（转写有自己的端点，理由和通讯录一样：最需要转写的
# 那个档案往往已经没有记录可同步，build 不出任何批次），于是必须有自己的下限。
TRANSCRIPT_MIN_VERSION = 13
# 一片转写最多多少行。和 roster 同一个量级：一次发完的发布等于永远发不完。
TRANSCRIPT_SLICE_ROWS = 500
# 账号自己的头像（记录模型 v10，PSU-2.55）。v9 是**纯读侧**的（锚点窗口），
# 一个字节都没改上传形状，所以它没有出现在这里；v10 给账号身份那条发布流多了
# 一个键，于是必须有自己的下限。
ACCOUNT_AVATAR_MIN_VERSION = 10
# 账号自己的微信身份（记录模型 v11，PSU-2.86）。同样给账号身份那条发布流多了一个
# 键，所以有自己的下限。这个键是**跨机器唯一稳定的那一个**：本机的 `acc_…`、
# 目录名与 `source_instance_id` 全都绑着"这台机器"，两台机器采集同一个微信时
# 算不出同一个值，远端于是把一个微信当成两个账号。
ACCOUNT_KEY_MIN_VERSION = 11

# Rows per published roster slice. Defined here rather than in the adapter
# because both ends of the publish read it: the outbox cuts to it and the
# service asks for it. The remote refuses a slice above 2,000
# (``server/sync_routes.py``'s ``_MAX_ROSTER_ROWS``), and half of a hard limit
# is the room a later column addition needs without a lock-step deploy.
ROSTER_SLICE_ROWS = 1_000


@dataclass(frozen=True)
class OutboxRecord:
    local_sequence: int
    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    content_sha256: str
    source_instance_id: str
    schema_version: str
    # PSU-2.30's render model. All optional, and ``None`` means the key does
    # not appear on the wire at all — which is how a batch built for a v1
    # remote stays byte-identical to what a v1 client would have sent.
    sender_uid: str | None = None
    sender_display_name: str | None = None
    is_self: bool | None = None
    message_type: str | None = None
    app_card_json: str | None = field(default=None, repr=False)
    reply_to_key: str | None = None
    source_sequence: int | None = None
    source_local_id: int | None = None
    attachment_refs: tuple[str, ...] | None = None


@dataclass(frozen=True)
class OutboxAccount:
    """What this upload stream's account is called (record model v5).

    Published on its own, not carried by a batch. The first design put it on
    every batch, and that cannot reach the case it exists for: an archive with
    nothing left to sync builds no batch, so the name had nothing to ride on —
    and that is the normal resting state of a finished archive.

    ``display_name`` is the *derived* name (``chat_archive.account_identity``),
    the same one the local top bar shows — not ``source_accounts.display_name``,
    which on a real machine is a generated placeholder.
    """

    display_name: str
    # The local row's ``updated_at``. Orders two writes to the same account on
    # the remote; opaque to it otherwise. Same role as the conversation
    # stream's, and the remote applies it the same way.
    source_updated_at: int
    # This account's own picture, as the avatar ledger has it (record model
    # v10). Chosen by the *same* walk that chose ``display_name`` — the local
    # top bar and the remote's switcher must not each derive "which of my
    # identities am I" and eventually disagree.
    #
    # ``None`` is a fact, not an omission: the ledger has not uploaded a
    # picture for this account, so the remote shows the initial rather than
    # waiting on bytes nobody sent. It is published as an explicit ``null``
    # for the same reason the roster's optional columns are — that is how a
    # picture that is no longer up gets cleared there too.
    avatar_blob_id: str | None = None
    # 这条流采集的是**哪一个微信**（记录模型 v11）。取自账号自己发出的消息里
    # 用得最多的 `sender_uid`（`chat_archive.account_identity`），也就是裸 wxid。
    #
    # 与上面两个字段同一次走查得出，理由也同一条：本机与远端不能各自推导"我是谁"。
    # 区别在于这一个不是拿来显示的——它是远端把两台机器的两条上传流认成同一个
    # 账号的**唯一依据**。
    #
    # ``None`` 是事实不是省略：这个账号一条自己发的消息都没有，本机据此不发这个键，
    # 远端据此让它自成一组，而不是拿目录名硬凑一个（凑出来的正是绑着这台机器的那个值）。
    account_key: str | None = None


@dataclass(frozen=True)
class OutboxIdentity:
    """One person out of this account's address book (record model v8).

    ``avatar_blob_id`` is filled from the avatar ledger, not from the contact
    row: the contact row records where a picture *can be fetched*, and this
    records where one **has already been put**. Publishing the first as if it
    were the second would leave the page waiting on bytes nobody sent.
    """

    uid: str
    display_name: str
    remark: str | None
    avatar_blob_id: str | None
    source_updated_at: int


@dataclass(frozen=True)
class OutboxMember:
    conversation_id: str
    member_uid: str
    display_name: str
    group_alias: str | None
    is_self: bool
    source_updated_at: int


@dataclass(frozen=True)
class OutboxConversationProfile:
    """The parts of a conversation only the address book knows.

    Separate from :class:`OutboxConversation`, which rides with every batch:
    an announcement changes rarely and a group avatar rarer still, so putting
    these on the name stream would make every batch carry bytes nobody read.
    """

    conversation_id: str
    contact_uid: str | None
    avatar_blob_id: str | None
    announcement: str | None
    my_group_alias: str | None
    source_updated_at: int


@dataclass(frozen=True)
class OutboxRosterSlice:
    """One publishable slice of an account's address book.

    Sliced because a real account has thousands of contacts and tens of
    thousands of memberships, and a publish that has to succeed all at once is
    a publish that never succeeds.
    """

    identities: tuple[OutboxIdentity, ...] = field(default=(), repr=False)
    members: tuple[OutboxMember, ...] = field(default=(), repr=False)
    conversations: tuple[OutboxConversationProfile, ...] = field(
        default=(), repr=False
    )

    def is_empty(self) -> bool:
        return not (self.identities or self.members or self.conversations)


@dataclass(frozen=True)
class OutboxConversation:
    conversation_id: str
    display_name: str
    is_group: bool
    source_updated_at: int
    # 这条会话在**微信眼里**的身份（记录模型 v11）。
    # `conversation_id` 是本机铸的，含本机账号 id，两台机器各铸各的；这个键由
    # `account_key` 与 `conversation_uid` 算出，两台机器算得出同一个值。
    #
    # ``None`` 是事实：这个账号的微信身份还没算出来（一条自己发的消息都没有），
    # 那就不并——身份未知时把两条会话认成一条，是在猜。
    conversation_key: str | None = None


@dataclass(frozen=True)
class SyncUploadBatch:
    archive_id: str
    device_id: str
    source_account_id: str
    client_batch_id: str
    local_sequence_start: int
    local_sequence_end: int
    source_instance_id: str
    schema_version: str
    payload_sha256: str
    records: tuple[OutboxRecord, ...] = field(repr=False)
    # Conversation display identities riding along. They consume no record
    # sequence and are cleared from the pending set on acknowledgement.
    conversations: tuple[OutboxConversation, ...] = field(default=(), repr=False)


@dataclass(frozen=True)
class SyncCursor:
    archive_id: str
    device_id: str
    source_account_id: str
    last_uploaded_local_sequence: int
    last_downloaded_server_sequence: int
    updated_at: int


@dataclass(frozen=True)
class SyncRunResult:
    status: str
    client_batch_id: str | None
    receipt: BatchReceipt | None
    error_code: str | None = None
    recovery_action: str | None = None


class SyncOutbox(Protocol):
    def prepare_upload_batch(
        self,
        *,
        archive_id: str,
        device_id: str,
        source_account_id: str,
        max_records: int,
        record_model_version: int = LOCAL_RECORD_MODEL_VERSION,
    ) -> SyncUploadBatch | None: ...

    def account_identity(self, source_account_id: str) -> OutboxAccount | None:
        """Derive this account's name, or ``None`` when the archive cannot.

        Asked **once per run**: it is a grouped scan of the account's own
        outgoing messages, and the answer cannot change mid-run.
        """
        ...

    def mark_upload_attempt(self, batch: SyncUploadBatch) -> None: ...

    def mark_upload_failed(self, batch: SyncUploadBatch, *, error_code: str) -> None: ...

    def acknowledge_upload(self, batch: SyncUploadBatch, receipt: BatchReceipt) -> SyncCursor: ...

    def sync_cursor(
        self,
        *,
        archive_id: str,
        device_id: str,
        source_account_id: str,
    ) -> SyncCursor: ...


class RemoteSyncTransport(Protocol):
    def commit_batch(self, batch: SyncUploadBatch) -> BatchReceipt: ...


@runtime_checkable
class OutboxBacklog(Protocol):
    """An outbox that can say how many records are still waiting to go up.

    Optional, like :class:`AccountIdentityPublisher` below and for the same
    reason: the in-memory outboxes the tests build have no such notion, and a
    caller that cannot get the number must say so rather than invent one.

    This is the denominator a long sync needs (PSU-2.107). Without it the
    interface can only say "sent 3,573 items", which tells the reader nothing
    about whether that is nearly done or barely started.
    """

    def pending_record_count(
        self,
        *,
        archive_id: str,
        device_id: str,
        source_account_id: str,
    ) -> int:
        """Records written past the upload cursor, i.e. still owed.

        The same arithmetic the sync health page reports
        (``max(local_sequence) - last_uploaded_local_sequence``), so the two
        never disagree in front of the user.
        """
        ...


@runtime_checkable
class AccountIdentityPublisher(Protocol):
    """A transport that can tell the remote what an account is called.

    Optional like :class:`RecordModelNegotiation`, and for the same reason:
    the in-memory transports the tests build have no remote to tell. A caller
    checks the negotiated record model *and* this protocol before publishing.
    """

    def publish_account_identity(
        self,
        archive_id: str,
        source_account_id: str,
        account: OutboxAccount,
    ) -> None: ...


@runtime_checkable
class RosterSource(Protocol):
    """An outbox that can hand over the account's address book, in slices.

    Separate from :class:`SyncOutbox` rather than folded into it, exactly as
    :class:`AccountIdentityPublisher` is separate from
    :class:`RemoteSyncTransport`: the in-memory outboxes the tests build have
    no address book to hand over, and making them grow one would be a cost
    paid by every test for a feature only the real archive has.
    """

    def roster_slices(
        self,
        source_account_id: str,
        *,
        slice_size: int,
    ) -> tuple[OutboxRosterSlice, ...]: ...


@runtime_checkable
class RosterPublisher(Protocol):
    """A transport that can hand the remote one slice of an address book."""

    def publish_roster(
        self,
        archive_id: str,
        source_account_id: str,
        roster: OutboxRosterSlice,
    ) -> None: ...


@runtime_checkable
class RecordModelNegotiation(Protocol):
    """A transport that can ask the remote which record model it speaks.

    Separate from :class:`RemoteSyncTransport` so the in-memory transports the
    tests build stay conformant without each having to answer a question they
    have no remote to ask. A transport that does not implement this is treated
    as speaking the local model — correct for a fake that accepts whatever the
    local build produces, and never applied to a real network peer, which the
    HTTP transport always answers for.
    """

    def record_model_version(self) -> int: ...


@runtime_checkable
class UploadCursorProbe(Protocol):
    """A transport that can ask where the remote thinks one account's upload stands (PSU-2.165).

    ``None`` means the remote cannot say — built before 2.165, or a backend
    without the dimension — and is never read as zero: realigning to a guess
    is exactly the kind of fix that loses messages.
    """

    def account_upload_cursor(self, archive_id: str, source_account_id: str) -> int | None: ...


@dataclass(frozen=True)
class CursorRealignment:
    """What one realignment did, for the log line that is its only witness."""

    previous: int
    current: int
    dropped_batches: int
    requeued_records: int


@runtime_checkable
class UploadCursorRealigner(Protocol):
    """An outbox that can move its upload cursor to where the remote is (PSU-2.165).

    Optional like :class:`OutboxBacklog`: the in-memory outboxes the tests
    build have no cursor table to rewrite. Returns ``None`` when it did
    nothing — the cursor was no longer ``expected_local_cursor``, or the move
    could not be made safely.
    """

    def realign_upload_cursor(
        self,
        *,
        archive_id: str,
        device_id: str,
        source_account_id: str,
        expected_local_cursor: int,
        remote_cursor: int,
    ) -> CursorRealignment | None: ...


class SyncClient:
    """Send one durable outbox batch at a time with at-least-once semantics."""

    def __init__(
        self,
        *,
        outbox: SyncOutbox,
        transport: RemoteSyncTransport,
        archive_id: str,
        device_id: str,
        source_account_id: str,
        max_records: int = 500,
        record_model_version: int = LOCAL_RECORD_MODEL_VERSION,
    ) -> None:
        if not archive_id or not device_id or not source_account_id:
            raise ValueError("sync identities must be non-empty")
        if max_records <= 0 or max_records > 500:
            raise ValueError("sync batch size must be between 1 and 500")
        if record_model_version < 1:
            raise ValueError("record model version must be at least 1")
        self._outbox = outbox
        self._transport = transport
        self._archive_id = archive_id
        self._device_id = device_id
        self._source_account_id = source_account_id
        self._max_records = max_records
        self._record_model_version = record_model_version

    def run_once(self) -> SyncRunResult:
        batch = self._outbox.prepare_upload_batch(
            archive_id=self._archive_id,
            device_id=self._device_id,
            source_account_id=self._source_account_id,
            max_records=self._max_records,
            record_model_version=self._record_model_version,
        )
        if batch is None:
            return SyncRunResult("idle", None, None)
        if self._record_model_version < RENDER_MODEL_MIN_VERSION and (
            batch.conversations
            or any(record.sender_uid is not None for record in batch.records)
        ):
            # A durable batch built for a richer remote, now facing a poorer
            # one — the remote was rolled back between runs. It cannot be
            # rebuilt: it may already have committed, and a replacement with a
            # new batch id would restart the same local window and be rejected
            # as non-contiguous. Refuse loudly instead of posting something the
            # peer will answer with a bare 400.
            self._outbox.mark_upload_failed(batch, error_code="REMOTE_RECORD_MODEL_TOO_OLD")
            return SyncRunResult(
                "retry",
                batch.client_batch_id,
                None,
                "REMOTE_RECORD_MODEL_TOO_OLD",
            )
        try:
            self._outbox.mark_upload_attempt(batch)
            receipt = self._transport.commit_batch(batch)
            self._outbox.acknowledge_upload(batch, receipt)
            if receipt.conflict_count > 0:
                # 批次已提交、游标已推进；服务端记录了跨设备分歧，向调用方
                # 显性化为可解释的冲突态与稳定恢复动作码（渲染留 PSU-2.7）。
                return SyncRunResult(
                    "conflict",
                    batch.client_batch_id,
                    receipt,
                    recovery_action="REVIEW_CONFLICTS",
                )
            return SyncRunResult("committed", batch.client_batch_id, receipt)
        except SyncError as error:
            self._outbox.mark_upload_failed(batch, error_code=error.code)
            return SyncRunResult("retry", batch.client_batch_id, None, error.code)
        except Exception:
            self._outbox.mark_upload_failed(batch, error_code="TRANSPORT_ERROR")
            return SyncRunResult("retry", batch.client_batch_id, None, "TRANSPORT_ERROR")

"""Vendor-neutral metadata storage contract."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Protocol, cast, runtime_checkable

from chat_archive.domain.reply_preview import ReplyPreviewRecord


@runtime_checkable
class ForwardMediaPublicationStore(Protocol):
    def forward_media_publication_confirmed(
        self, archive_id: str, record_key: str, device_id: str, publication_sha256: str,
    ) -> bool: ...


class MetadataConflictError(RuntimeError):
    """A stable identity was reused with conflicting content or sequence."""


class MetadataValidationError(ValueError):
    """A batch or query violates the vendor-neutral contract."""


class MetadataUnavailableError(RuntimeError):
    """The metadata backend cannot complete an otherwise valid operation."""


class MetadataBusyError(RuntimeError):
    """写入排不进去：有人正握着写锁，等不到（PSU-2.98）。

    与 :class:`MetadataUnavailableError` 分开是**故意的**。"远程不可用"和"远程忙，
    稍后再来"对操作者是两句话、两种处置，界面要能分开说；复用一个错误码等于
    在传输层就把它们混成一句。传输侧映射成 503 + ``Retry-After``，错误码
    ``REMOTE_BUSY``。

    名字说 metadata，但 blob 状态与浏览器登录那两个 store 抛的也是它：三处是
    同一把 :class:`~chat_archive.adapters.sqlite.guards.BoundedWriterLock`、同一个
    事实（这台部署此刻写不进去），给它三个名字只会让运维要认三个词。
    """


@dataclass(frozen=True)
class RecordInput:
    """One archived message on the wire.

    Everything past ``schema_version`` is the v2 render model (PSU-2.30) and is
    optional in the strict sense the checksum depends on: a key whose value is
    ``None`` does not appear in the canonical payload at all, so a batch of
    records that carries none of them hashes byte for byte the way it did
    before these fields existed. That is what makes the widening free for the
    frozen v1 backends, and it is why every one of them defaults to ``None``
    rather than to an empty string.

    The set is argued from "what does the remote need to draw a message",
    not from "what does the local row happen to have": ``sub_type`` is not
    here because not even the local ``MessageView`` exposes it, and the
    conversation's display name is not here because names get changed and
    729k frozen copies of the old one cannot be corrected (it travels on its
    own stream, :class:`ConversationInput`). The sender's display name *is*
    frozen per message, which is the chat-export convention: a rename does
    not rewrite history.
    """

    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    content_sha256: str
    source_instance_id: str | None = None
    schema_version: str | None = None
    sender_uid: str | None = None
    sender_display_name: str | None = None
    is_self: bool | None = None
    message_type: str | None = None
    app_card_json: str | None = field(default=None, repr=False)
    # The *record key* of the quoted message, not the local message id: the
    # remote has never seen a local id and could not resolve one.
    reply_to_key: str | None = None
    # Where the source itself put this message in its conversation. 0 is the
    # local table's own "the source did not say" sentinel, kept here so the
    # remote sort key degrades exactly the way the local one does.
    source_sequence: int | None = None
    source_local_id: int | None = None
    # Blob ids of this message's attachments (PSU-2.31). Empty tuple and
    # ``None`` are different statements: "this message has no attachments"
    # versus "this sender does not speak about attachments at all".
    attachment_refs: tuple[str, ...] | None = None


# The v2 render fields the *wire* carries, in the order the batch checksum
# hashes them. This tuple is a protocol constant: it defines the bytes
# ``SyncBatch.checksum`` digests, the TypeScript worker mirrors that digest,
# and recorded fixtures on both languages depend on it. Do not reorder it and
# do not remove from it.
V2_RECORD_FIELDS: tuple[str, ...] = (
    "sender_uid",
    "sender_display_name",
    "is_self",
    "message_type",
    "app_card_json",
    "reply_to_key",
    "source_sequence",
    "source_local_id",
    "attachment_refs",
)

# Of those, the ones a *change* is worth writing for — the render fingerprint's
# inputs. Narrower than the wire on purpose, and the two digests answer
# different questions: the checksum asks "did this payload arrive intact",
# which every field it carries must be inside; the fingerprint asks "does the
# sender know something the stored row does not", which is a question about the
# message, not about the transport.
#
# The test a field has to pass to be here is **"is this a property of the
# message, or of the machine that read it?"** A field of the second kind can
# never agree between two capture machines, and a field two machines can never
# agree on makes every message they share a change on every sync, in both
# directions, without end.
#
# ``source_sequence`` and ``source_local_id`` are of the second kind and were
# fingerprint inputs until PSU-2.97. They come from WeChat's own per-shard
# ``local_id 1..N`` (see the shard note in
# ``adapters/chatlog_keeper/installed.py``) — a coordinate inside one machine's
# database. On a real two-machine archive that classified 279,185 of 282,328
# shared messages (98.9%) as changes, each rewrite burning a server sequence
# and handing the row to the other machine, which the first machine then took
# back on its next run. They are still sent, still checksummed, still stored,
# and still the reading-order tie-break; they simply no longer *cause* a write.
# A record keeps whichever machine's positions arrived first, which is
# arbitrary — but stable, and stable is what an ordering needs to be.
#
# ``attachment_refs`` is machine-salted too, and stays: it carries information
# (what the remote can actually open) rather than a coordinate, and taking it
# out would mean a second machine's uploaded picture is never referenced by
# anything. That one write per machine is the design's price, not a defect.
FINGERPRINT_RECORD_FIELDS: tuple[str, ...] = tuple(
    name
    for name in V2_RECORD_FIELDS
    if name not in {"source_sequence", "source_local_id"}
)


def _v2_payload(
    record: RecordInput, fields: tuple[str, ...] = V2_RECORD_FIELDS
) -> dict[str, object]:
    """The named fields this record actually carries, absent keys omitted."""
    payload: dict[str, object] = {}
    for name in fields:
        value: object = getattr(record, name)
        if value is None:
            continue
        payload[name] = (
            list(cast(tuple[str, ...], value)) if isinstance(value, tuple) else value
        )
    return payload


def _canonical_json(payload: object) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def record_metadata_fingerprint(record: RecordInput) -> str | None:
    """Digest of a record's v2 render fields, or ``None`` when it has none.

    ``plan_commit`` compares this against what the store already holds, so
    that a re-upload whose *content* is unchanged but whose render fields are
    new is recognised as a change instead of being skipped. Without it the
    full retransmission this unit depends on would land nothing at all: the
    old rule short-circuits on ``content_sha256`` alone.

    ``None`` — not the digest of an empty object — is what makes a thin
    sender free of charge. A record carrying no such field is
    indistinguishable from a v1 record, so it keeps being a no-op against a v2
    store instead of rewriting every row once, and a v1 sender in a mixed
    fleet cannot wipe the render fields another device already delivered.

    Hashes :data:`FINGERPRINT_RECORD_FIELDS`, which is **not** the wire's
    field list: the machine-local positions travel and are checksummed, but
    they are not something a re-upload is worth rewriting a row for. See that
    constant for why.
    """
    payload = _v2_payload(record, FINGERPRINT_RECORD_FIELDS)
    if not payload:
        return None
    return hashlib.sha256(_canonical_json(payload)).hexdigest()


@dataclass(frozen=True)
class AccountInput:
    """What an upload stream calls itself (record model v5).

    Published on its own, not carried by a batch. It was a batch section
    first, and that could not reach the case it exists for: an archive that
    has already uploaded everything builds no batch at all, so the name had
    nothing to ride on — and "everything is already uploaded" is the normal
    resting state of a finished archive, not an edge case.

    Upserted last-write-wins by ``updated_at``, the same rule the conversation
    stream uses and for the same reason: a publish replayed out of order must
    not restore a name already moved past.
    """

    display_name: str
    updated_at: int
    # This account's own picture (record model v10). ``None`` means the sender
    # said it has none — an older sender omits the key entirely and lands on
    # the same default, which is correct: neither one has a picture to show.
    avatar_blob_id: str | None = None
    # *Which WeChat* this upload stream captured (record model v11), as the
    # bare wxid its own outgoing messages carry. The only thing about an
    # account that two machines can agree on: the stream id in the path, the
    # sender's data directory name and its source instance id are all minted
    # per installation.
    #
    # ``None`` — absent or explicitly null — means the sender has nothing to
    # say, and the stream then stands on its own. Never guessed at from the
    # stream id: guessing is what produced two accounts for one WeChat.
    account_key: str | None = None


@dataclass(frozen=True)
class IdentityInput:
    """One person as the account's own address book knows them (record model v8).

    Published on its own stream, not on the records: a person gets renamed and
    changes their avatar, and 729k frozen copies on individual messages cannot
    be corrected. Same reasoning as :class:`ConversationInput`, one dimension
    over — this is *who*, that is *where*.

    ``avatar_blob_id`` names bytes the sender has already uploaded through the
    blob surface. ``None`` means "this person has no avatar the sender could
    fetch", which is a different sentence from "the bytes are still on their
    way" — the latter simply has no identity row yet.
    """

    uid: str
    display_name: str
    remark: str | None = None
    avatar_blob_id: str | None = None
    # The sender's own row version, used only to order two writes to the same
    # person. A publish replayed out of order carries an older one.
    updated_at: int = 0


@dataclass(frozen=True)
class ConversationMemberInput:
    """One membership row: who is in which group, and what they are called there."""

    conversation_id: str
    member_uid: str
    display_name: str
    group_alias: str | None = None
    is_self: bool = False
    updated_at: int = 0


@dataclass(frozen=True)
class ConversationProfileInput:
    """The things about a conversation only the address book side knows.

    Separate from :class:`ConversationInput` because that one travels with
    every batch and this one does not: an announcement changes rarely and a
    group avatar rarer still, so putting them on the name stream would make
    every batch carry bytes nobody read.
    """

    conversation_id: str
    # 单聊对面那个人的标识（本机 `conversations.conversation_uid`）。名字过滤要
    # 靠它去匹配联系人名与备注——少了它这一端只能匹配到 5 列而本机匹配 7 列。
    contact_uid: str | None = None
    avatar_blob_id: str | None = None
    announcement: str | None = None
    my_group_alias: str | None = None
    updated_at: int = 0


@dataclass(frozen=True)
class TranscriptInput:
    """一条到达远端的语音转写（记录模型 v13）。

    键是 `record_key`——远端认消息就认它。本机那边的键是音频的 sha256，
    但远端没有附件指纹，那个键在这里对不上任何东西。
    """

    record_key: str
    text: str
    #: 哪个引擎写的。远端是多台机器的转写汇合处，覆盖按 `updated_at` 判——
    #: 哪一版赢了，事后只能从这一列读出来。
    engine: str
    low_confidence: bool
    updated_at: int


@dataclass(frozen=True)
class TranscriptsInput:
    """一片转写。切片的理由和通讯录一样：一次发完的发布等于永远发不完。"""

    transcripts: tuple[TranscriptInput, ...] = ()


@dataclass(frozen=True)
class RosterInput:
    """One slice of an account's address book on the wire (record model v8).

    Sliced rather than whole: a real account has thousands of contacts and tens
    of thousands of memberships, and a publish that has to succeed all at once
    is a publish that never succeeds. Every row is upserted last-write-wins on
    its own ``updated_at``, so slices may arrive in any order and a replay is
    a no-op.
    """

    identities: tuple[IdentityInput, ...] = ()
    members: tuple[ConversationMemberInput, ...] = ()
    conversations: tuple[ConversationProfileInput, ...] = ()


@dataclass(frozen=True)
class ConversationInput:
    """One conversation's display identity, on its own stream.

    Carried alongside the records rather than on them: a conversation gets
    renamed, and 729k frozen copies of the old name on individual messages
    cannot be corrected — whichever copy the list page happened to read would
    be wrong. Upserted last-write-wins on the remote, consuming no record
    sequence.
    """

    conversation_id: str
    display_name: str
    is_group: bool
    # The local row's ``updated_at``. Used only to order two writes to the
    # same conversation; opaque to the remote otherwise.
    updated_at: int
    # What *WeChat* calls this conversation (record model v11), as opposed to
    # ``conversation_id``, which the sending machine minted from its own
    # account id. Two machines capturing one chat send two ids and one key.
    #
    # ``None`` means the sender could not work out which WeChat it is at all,
    # so nothing may be merged on its behalf.
    conversation_key: str | None = None


@dataclass(frozen=True)
class SyncBatch:
    archive_id: str
    device_id: str
    # Which of the device's upload streams this window belongs to. A capture
    # machine keeps one outbox sequence per source account, so one WeChat
    # account is one stream; a device with two accounts sends two interleaved
    # 1..N sequences. Cursors and the contiguity check are scoped by this —
    # keyed by device alone, the second account's sequence 1 arrived after the
    # first account had moved the cursor, and every multi-account device was
    # rejected with "not contiguous" on its second stream. The value is the
    # sender's local account id: opaque here, meaningful only as a stream
    # discriminator within (archive_id, device_id).
    source_account_id: str
    client_batch_id: str
    local_sequence_start: int
    local_sequence_end: int
    payload_sha256: str
    records: tuple[RecordInput, ...] = field(repr=False)
    # Preserve the wire shape when HTTP applies batch-level metadata defaults
    # to ``records``; adapters still use payload_sha256 as the replay identity.
    payload_records: tuple[RecordInput, ...] | None = field(default=None, repr=False)
    # Conversation display identities riding along with this window. Empty on
    # every batch a v1 sender produces, and empty is the case the checksum is
    # built to leave untouched.
    conversations: tuple[ConversationInput, ...] = field(default=(), repr=False)

    @staticmethod
    def checksum(
        records: tuple[RecordInput, ...],
        conversations: tuple[ConversationInput, ...] = (),
    ) -> str:
        """Digest the batch payload, absent keys and absent sections omitted.

        Both halves are canonical JSON arrays, and the conversation half is
        simply appended to the record half. Two self-delimiting arrays can be
        split apart in exactly one place, so the concatenation is unambiguous;
        and a batch with no conversations hashes the *identical bytes* it
        hashed before conversations existed. Every checksum fixture recorded
        against the v1 wire therefore stays valid, on both languages, without
        anyone having to regenerate it.

        ``conversation_key`` (v11) is deliberately **outside** this digest, on
        the same reasoning: it is derived, not authored. Its value follows from
        the account's own identity and the conversation's uid, so it cannot
        vary between two batches that agree on everything else, and leaving it
        out keeps every recorded fixture valid for a third time. The cost is
        that a replayed batch is short-circuited on its receipt before the key
        is read — harmless, because the next batch carries the same key, and
        the account identity publish that drives merging runs every round.
        """
        payload = [
            {
                "record_key": record.record_key,
                "conversation_id": record.conversation_id,
                "created_at": record.created_at,
                "content_text": record.content_text,
                "content_sha256": record.content_sha256,
                **(
                    {"source_instance_id": record.source_instance_id}
                    if record.source_instance_id is not None
                    else {}
                ),
                **(
                    {"schema_version": record.schema_version}
                    if record.schema_version is not None
                    else {}
                ),
                **_v2_payload(record),
            }
            for record in records
        ]
        encoded = _canonical_json(payload)
        if conversations:
            encoded += _canonical_json(
                [
                    {
                        "conversation_id": item.conversation_id,
                        "display_name": item.display_name,
                        "is_group": item.is_group,
                        "updated_at": item.updated_at,
                    }
                    for item in conversations
                ]
            )
        return hashlib.sha256(encoded).hexdigest()

    @classmethod
    def create(
        cls,
        *,
        archive_id: str,
        device_id: str,
        source_account_id: str,
        client_batch_id: str,
        local_sequence_start: int,
        local_sequence_end: int,
        records: tuple[RecordInput, ...],
        conversations: tuple[ConversationInput, ...] = (),
    ) -> SyncBatch:
        return cls(
            archive_id=archive_id,
            device_id=device_id,
            source_account_id=source_account_id,
            client_batch_id=client_batch_id,
            local_sequence_start=local_sequence_start,
            local_sequence_end=local_sequence_end,
            payload_sha256=cls.checksum(records, conversations),
            records=records,
            conversations=conversations,
        )


@dataclass(frozen=True)
class BatchReceipt:
    client_batch_id: str
    server_sequence_start: int
    server_sequence_end: int
    inserted_count: int
    updated_count: int
    conflict_count: int = 0


@dataclass(frozen=True)
class RecordTranscript:
    """一条语音消息的机器转写，读侧看到的形状（记录模型 v13）。

    没有 `status` 也没有 `failure_code`：远端只收得下 `ok` 的转写
    （`_transcripts_input` 挡掉空文本，本机也只挑 `status == "ok"` 发），
    所以这里的存在本身就是那个状态。给它一个恒为 `"ok"` 的列，
    只是多一个可以和事实不一致的地方。
    """

    text: str = field(repr=False)
    #: 哪个引擎写的。界面不显示它，但两台机器的转写在远端按 `updated_at`
    #: 互相覆盖，"最后赢的是哪一版"只能从这里读出来。
    engine: str
    low_confidence: bool


@dataclass(frozen=True)
class StoredRecord:
    """A record as the remote holds it.

    The v2 render fields stay ``None`` for every row written before this unit
    and for every row a thin sender delivered. Readers must treat ``None`` as
    "the remote was never told", not as "empty" — the difference is the whole
    point of the honest-state rule.
    """

    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    content_sha256: str
    server_sequence: int
    sender_uid: str | None = None
    sender_display_name: str | None = None
    is_self: bool | None = None
    message_type: str | None = None
    app_card_json: str | None = field(default=None, repr=False)
    reply_to_key: str | None = None
    source_sequence: int = 0
    source_local_id: int = 0
    attachment_refs: tuple[str, ...] = ()
    # Which upload stream delivered this row (PSU-2.35a). ``None`` means the
    # row's owning batch predates account-scoped ledgers and its stream cannot
    # be proven — "unattributed", never silently assigned to anyone.
    source_account_id: str | None = None
    # The sender's avatar (PSU-2.47), resolved from the roster at read time
    # rather than frozen onto the record: people change their picture, and the
    # copy on a message from 2019 would keep the old one forever. Same
    # arrangement the local end uses (``query.py`` joins ``contacts``).
    sender_avatar_blob_id: str | None = None
    # 这条语音的机器转写（记录模型 v13，PSU-2.163）。和头像同样是读时接上的，
    # 不是记录自己的一部分：转写是后来才算出来的，重转会换一版，而记录不变。
    # ``None`` 三义合一——「这个远端到不了 v13」「这条不是语音」「还没发过来」
    # ——读侧对三者的动作是同一个：不画那一行。
    transcript: RecordTranscript | None = None
    # Read-only, scoped to this record's archive/account/conversation; never uploaded.
    reply_preview: ReplyPreviewRecord | None = None


@dataclass(frozen=True)
class RecordPage:
    records: tuple[StoredRecord, ...] = field(repr=False)
    next_server_sequence: int
    has_more: bool


@dataclass(frozen=True)
class ConversationSummary:
    """A conversation as the list page sees it.

    ``display_name`` is ``None`` until the conversation stream has delivered
    one. The list must then show the opaque id and say so, rather than invent
    a name out of the messages.
    """

    conversation_id: str
    message_count: int
    last_created_at: int
    last_server_sequence: int
    display_name: str | None = None
    is_group: bool | None = None
    # The one account whose records make up this row, or ``None`` when the
    # unscoped list merged several accounts (or none is attributed). A scoped
    # list always fills it with the account it was scoped to.
    source_account_id: str | None = None
    # 这条会话最后一条消息的类型，与列表上要显示的那一句（PSU-2.44）。
    # 两个都 ``None`` = 这个后端还没有这一版状态（冻结在 v1）或这条会话
    # 没有可预览的消息——两种在界面上都退回条数，本来就是同一句话。
    # 预览是**算好的一句**而不是原文：算它的口径两端共用一份
    # （`domain/preview.conversation_preview`），所以只能算一次、算在能同时
    # 看到类型与正文的那一层。
    last_message_type: str | None = None
    last_message_preview: str | None = None
    # 通讯录那一侧才知道的几样（记录模型 v8，PSU-2.47）。全是 ``None`` 时兼指
    # 「这个后端还没有这一版」与「这条会话没有这几样」——界面两种都不画，
    # 头像退回首字母，而不是印一个空抽屉。
    avatar_blob_id: str | None = None
    announcement: str | None = None
    my_group_alias: str | None = None
    member_count: int | None = None


@dataclass(frozen=True)
class ConversationPage:
    conversations: tuple[ConversationSummary, ...] = field(repr=False)
    next_conversation_id: str | None
    has_more: bool


@dataclass(frozen=True)
class ConversationPosition:
    """A place in the conversation list as it is *browsed*: most recent first.

    The same two columns the local list sorts by (``query.py`` orders on
    ``s.last_message_at DESC, c.id ASC``), so a reader paging the remote and a
    reader paging the local archive walk the same sequence. ``conversation_id``
    ascends while ``last_created_at`` descends — a mixed key, and the predicate
    has to spell that out rather than compare the pair as a tuple.
    """

    last_created_at: int
    conversation_id: str


@dataclass(frozen=True)
class RecentConversationPage:
    conversations: tuple[ConversationSummary, ...] = field(repr=False)
    next_position: ConversationPosition | None
    has_more: bool


@dataclass(frozen=True)
class MessagePage:
    records: tuple[StoredRecord, ...] = field(repr=False)
    next_server_sequence: int
    has_more: bool


@dataclass(frozen=True)
class MessagePosition:
    """A place in the conversation's reading order.

    The same four columns the local transcript sorts by, in the same order
    (``chat_archive/archive/schema.py`` ``ix_messages_account_conversation_order``),
    with ``record_key`` standing in for the local message id. The last of the
    four is a tie-break of last resort: it only decides between rows the
    source itself never positioned, and for those the local order is arbitrary
    too — just arbitrary in a different way.
    """

    created_at: int
    source_sequence: int
    source_local_id: int
    record_key: str


@dataclass(frozen=True)
class TimeOrderedMessagePage:
    records: tuple[StoredRecord, ...] = field(repr=False)
    next_position: MessagePosition | None
    has_more: bool


@dataclass(frozen=True)
class ConversationMemberSummary:
    """One row of a group's roster as the info drawer shows it."""

    member_uid: str
    display_name: str
    group_alias: str | None = None
    is_self: bool = False
    # Joined from the identity stream, not stored per membership: one person in
    # forty groups has one avatar, and forty copies of it would go stale forty
    # different ways.
    avatar_blob_id: str | None = None


@dataclass(frozen=True)
class ConversationMemberPage:
    members: tuple[ConversationMemberSummary, ...] = field(repr=False)
    next_member_uid: str | None
    has_more: bool


@dataclass(frozen=True)
class RecordSearchPage:
    """One page of search hits, PSU-2.45.

    The rows are whole :class:`StoredRecord`s, not a narrower "hit" shape, so a
    reader cannot tell whether a record was searched for or paged to — the same
    rule the two conversation list orders follow.

    ``next_after`` is ``(created_at, record_key)``: a point on the index's own
    sort line, which is global rather than per-account because the docid it
    resolves to leads with the timestamp.

    ``index_status`` is ``ready`` / ``building`` / ``unavailable``, matching the
    local end's vocabulary. ``building`` has to reach the page: a reader who is
    not told the index is still filling will read "no hits" as "no such
    message".
    """

    records: tuple[StoredRecord, ...] = field(repr=False)
    next_after: tuple[int, str] | None
    total_count: int
    conversation_counts: dict[str, int]
    index_status: str


@dataclass(frozen=True)
class ConflictPage:
    """A page of the cross-device conflict ledger, ordered by server_sequence.

    Entries reuse :class:`ConflictRecord` — checksums, opaque device/source
    diagnostics and detection time only, never chat bodies — so ledger reads
    do not leak archived content.
    """

    conflicts: tuple[ConflictRecord, ...] = field(repr=False)
    next_server_sequence: int
    has_more: bool


@dataclass(frozen=True)
class ArchiveStats:
    message_count: int
    conversation_count: int
    last_server_sequence: int


@dataclass(frozen=True)
class ArchiveStorageHealth:
    used_bytes: int
    blob_count: int
    pending_blob_count: int
    last_uploaded_local_sequence: int
    conflict_count: int


@dataclass(frozen=True)
class BlobManifest:
    blob_id: str
    size: int
    sha256: str
    state: str


@dataclass(frozen=True)
class ManifestReconciliation:
    missing_in_store: tuple[str, ...]
    checksum_mismatches: tuple[str, ...]
    orphaned_in_store: tuple[str, ...]


def validate_batch(batch: SyncBatch, *, max_records: int) -> None:
    """Shared fail-closed validation every MetadataStore adapter must apply."""
    if (
        not batch.archive_id
        or not batch.device_id
        or not batch.source_account_id
        or not batch.client_batch_id
    ):
        raise MetadataValidationError("batch identities must be non-empty")
    if not batch.records or len(batch.records) > max_records:
        raise MetadataValidationError("batch record count is outside configured limits")
    if batch.local_sequence_start <= 0 or batch.local_sequence_end < batch.local_sequence_start:
        raise MetadataValidationError("invalid local sequence window")
    payload_records = batch.records if batch.payload_records is None else batch.payload_records
    if batch.payload_sha256 != SyncBatch.checksum(payload_records, batch.conversations):
        raise MetadataValidationError("batch checksum does not match payload")
    if len(payload_records) != len(batch.records):
        raise MetadataValidationError("payload and effective record counts differ")
    for payload_record, effective_record in zip(payload_records, batch.records, strict=True):
        if (
            payload_record.record_key != effective_record.record_key
            or payload_record.conversation_id != effective_record.conversation_id
            or payload_record.created_at != effective_record.created_at
            or payload_record.content_text != effective_record.content_text
            or payload_record.content_sha256 != effective_record.content_sha256
            or (
                payload_record.source_instance_id is not None
                and payload_record.source_instance_id != effective_record.source_instance_id
            )
            or (
                payload_record.schema_version is not None
                and payload_record.schema_version != effective_record.schema_version
            )
            # No batch-level default fills any v2 field, so the effective
            # record must reproduce the wire exactly. Were that not checked,
            # the metadata fingerprint would be computed over something the
            # sender never signed.
            or _v2_payload(payload_record) != _v2_payload(effective_record)
        ):
            raise MetadataValidationError("payload and effective records differ")
    keys: set[str] = set()
    for record in batch.records:
        if not record.record_key or record.record_key in keys:
            raise MetadataValidationError("record keys must be non-empty and unique per batch")
        keys.add(record.record_key)
        digest = hashlib.sha256(record.content_text.encode("utf-8")).hexdigest()
        if digest != record.content_sha256:
            raise MetadataValidationError("record checksum does not match content")
        if record.source_sequence is not None and (
            isinstance(record.source_sequence, bool) or record.source_sequence < 0
        ):
            raise MetadataValidationError("record source positions must be non-negative")
        if record.source_local_id is not None and (
            isinstance(record.source_local_id, bool) or record.source_local_id < 0
        ):
            raise MetadataValidationError("record source positions must be non-negative")
        if record.attachment_refs is not None and (
            any(not item for item in record.attachment_refs)
            or len(set(record.attachment_refs)) != len(record.attachment_refs)
        ):
            raise MetadataValidationError("attachment refs must be non-empty and unique")
    conversation_ids: set[str] = set()
    for conversation in batch.conversations:
        if not conversation.conversation_id or conversation.conversation_id in conversation_ids:
            raise MetadataValidationError(
                "conversation ids must be non-empty and unique per batch"
            )
        conversation_ids.add(conversation.conversation_id)
        if isinstance(conversation.updated_at, bool) or conversation.updated_at < 0:
            raise MetadataValidationError("conversation update times must be non-negative")


@dataclass(frozen=True)
class SequencedRecord:
    record: RecordInput = field(repr=False)
    server_sequence: int
    is_insert: bool


@dataclass(frozen=True)
class StoredIdentity:
    """Identity of the record already stored under a record_key.

    Carries the current content checksum, diagnostic source instance and the
    paired device that last wrote it. ``plan_commit`` classifies solely by the
    server-authoritative device id; the client-supplied source instance is
    retained for diagnostics but is not authenticated device provenance.
    """

    content_sha256: str
    source_instance_id: str | None
    origin_device_id: str | None
    # The v2 render fingerprint of the stored row, or ``None`` when the row
    # carries no v2 field. Only stores that persist it fill this in, and only
    # those stores pass ``tracks_metadata_fingerprint=True`` to ``plan_commit``.
    metadata_fingerprint: str | None = None


@dataclass(frozen=True)
class ConflictRecord:
    """A cross-device divergence detected on an existing record_key.

    Holds only checksums, source instances, device ids, schema version and a
    detection timestamp — never chat bodies — so the ledger can be persisted
    and read without leaking content.
    """

    record_key: str
    server_sequence: int
    canonical_content_sha256: str
    incoming_content_sha256: str
    canonical_source_instance_id: str | None
    incoming_source_instance_id: str | None
    incoming_schema_version: str | None
    canonical_device_id: str | None
    incoming_device_id: str | None
    detected_at: int
    # What diverged (PSU-2.82). ``content`` is the original meaning and the
    # default every backend keeps. ``attachment_refs`` is only found on older
    # SQLite ledgers: it said the two machines each held an artifact the other
    # could not open. Those divergences are now resolved automatically by the
    # refs merge and no longer written, but the rows already written stay.
    reason: str = "content"


@dataclass(frozen=True)
class CommitPlan:
    changed: tuple[SequencedRecord, ...] = field(repr=False)
    conflicts: tuple[ConflictRecord, ...]
    sequence_start: int
    sequence_end: int
    inserted_count: int
    updated_count: int


def plan_commit(
    batch: SyncBatch,
    *,
    existing: Mapping[str, StoredIdentity],
    last_server_sequence: int,
    now: int,
    tracks_metadata_fingerprint: bool = False,
) -> CommitPlan:
    """Classify each record and assign server sequences; identical on every backend.

    Per record, given the stored identity under its key:

    - absent -> ``insert`` (writes a new canonical row);
    - same ``content_sha256`` and same render fingerprint -> ``noop``
      (idempotent merge, skipped);
    - same ``content_sha256``, **different** render fingerprint -> ``version``
      (the row is refreshed in place). Never a conflict, on any device:
      identical content is not a disagreement about what was said, so two
      devices sending the same message with different render fields are not
      in dispute and the ledger must stay quiet;
    - different content, **same** origin ``device_id`` -> ``version`` (a
      same-device correction overwrites the canonical row);
    - different content, **different** origin ``device_id``, and the incoming
      record declares a ``message_type`` other than ``"text"`` -> ``noop``
      (kept silently: canonical untouched, no ledger entry, no sequence);
    - different content, **different** origin ``device_id`` otherwise ->
      ``conflict`` (a cross-device divergence recorded in the ledger with
      ``now`` as its detection time; the canonical row is left untouched so
      the first-committed content wins deterministically).

    Why the non-text case is silent: the ``content_text`` of a quote, emoji,
    system notice, app card, link or file message is not what anybody typed;
    it is text the capture parser renders from the structured message, and
    two machines on different app versions render the same message
    differently. A real archive's ledger was dominated by exactly these —
    mostly old history replayed when a second machine joined — and none of
    them needed a person. The outcome was already decided (first-committed
    wins, the canonical row is not touched), so writing it down only made a
    resolved divergence look like an open one.

    It is classified as ``noop`` rather than as a conflict without a ledger
    row, because a conflict consumes a server sequence that no ledger row and
    no record would then carry: a hole every reader of the change feed would
    have to explain. As a ``noop`` it touches nothing and consumes nothing,
    and a batch that ends up entirely such records still consumes one
    sequence through the monotonic-receipt rule below, same as any unchanged
    batch.

    Only a *declared* non-text type qualifies. ``"text"`` is what somebody
    actually wrote, so a difference there stays a conflict; a record with no
    ``message_type`` (a v1 sender, and the Cloudflare Worker whose v1 wire has
    no such field) — or an empty one — cannot be told apart from text and
    keeps the conflict too.
    The v1 stores (PostgreSQL, D1) apply the same rule because it reads only
    the incoming record, never a stored column. The stored row's type is not
    consulted: the capture side decides a message's type, not its rendering,
    so it does not change between versions the way the rendering does.

    The discriminant is the server-authoritative committing device id
    (``batch.device_id``), not the client-supplied source instance, which is
    diagnostic metadata rather than authenticated device provenance.

    ``tracks_metadata_fingerprint`` is the store telling the plan what it is
    able to remember, and the layering is not optional. The v1 backends
    (PostgreSQL, D1) have no column to keep a fingerprint in, so their stored
    identity would read back as ``None`` forever: were the rule applied
    unconditionally, every rich record would be classified as a change on
    every single run and those archives would burn a server sequence per
    message per sync, without end. Left at ``False`` they keep the pure
    content rule they were frozen with.

    Inserts, versions and conflicts each consume one sequence, in batch order.
    A batch where nothing changed still consumes one sequence so its receipt
    stays monotonic relative to later batches. Conversations are upserted
    outside this plan and consume no sequence at all.
    """
    changed: list[SequencedRecord] = []
    conflicts: list[ConflictRecord] = []
    sequence = last_server_sequence
    inserted = 0
    updated = 0
    for record in batch.records:
        stored = existing.get(record.record_key)
        incoming_fingerprint = (
            record_metadata_fingerprint(record) if tracks_metadata_fingerprint else None
        )
        if stored is None:
            sequence += 1
            inserted += 1
            changed.append(SequencedRecord(record, sequence, is_insert=True))
        elif stored.content_sha256 == record.content_sha256:
            # A record that carries no v2 field at all is indistinguishable
            # from a v1 record and is judged on content alone. That keeps a
            # thin sender a no-op against a v2 store, and stops it from
            # stripping render fields another device already delivered.
            if incoming_fingerprint is None or stored.metadata_fingerprint == incoming_fingerprint:
                continue
            sequence += 1
            updated += 1
            changed.append(SequencedRecord(record, sequence, is_insert=False))
        elif stored.origin_device_id == batch.device_id:
            sequence += 1
            updated += 1
            changed.append(SequencedRecord(record, sequence, is_insert=False))
        elif record.message_type and record.message_type != "text":
            # Parser-rendered text differing across devices: first one wins,
            # silently. See the docstring for why this is a noop.
            continue
        else:
            sequence += 1
            conflicts.append(
                ConflictRecord(
                    record_key=record.record_key,
                    server_sequence=sequence,
                    canonical_content_sha256=stored.content_sha256,
                    incoming_content_sha256=record.content_sha256,
                    canonical_source_instance_id=stored.source_instance_id,
                    incoming_source_instance_id=record.source_instance_id,
                    incoming_schema_version=record.schema_version,
                    canonical_device_id=stored.origin_device_id,
                    incoming_device_id=batch.device_id,
                    detected_at=now,
                )
            )
    event_count = len(changed) + len(conflicts)
    sequence_start = sequence - event_count + 1
    if event_count == 0:
        sequence += 1
        sequence_start = sequence
    return CommitPlan(
        changed=tuple(changed),
        conflicts=tuple(conflicts),
        sequence_start=sequence_start,
        sequence_end=sequence,
        inserted_count=inserted,
        updated_count=updated,
    )


class MetadataStore(Protocol):
    def commit_batch(self, batch: SyncBatch) -> BatchReceipt: ...

    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int,
        limit: int,
    ) -> RecordPage: ...

    def search(self, archive_id: str, query: str, *, limit: int) -> tuple[StoredRecord, ...]: ...

    def list_conversations(
        self,
        archive_id: str,
        *,
        after_conversation_id: str = "",
        limit: int,
    ) -> ConversationPage: ...

    def list_conversation_messages(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after_server_sequence: int,
        limit: int,
    ) -> MessagePage: ...

    def list_conflicts(
        self,
        archive_id: str,
        *,
        after_server_sequence: int,
        limit: int,
    ) -> ConflictPage: ...

    def stats(self, archive_id: str) -> ArchiveStats: ...

    def storage_health(self, archive_id: str, device_id: str) -> ArchiveStorageHealth: ...

    def upsert_blob_manifest(self, archive_id: str, manifest: BlobManifest) -> None: ...

    def reconcile_blob_manifests(
        self,
        archive_id: str,
        expected: tuple[BlobManifest, ...],
    ) -> ManifestReconciliation: ...


@runtime_checkable
class RichMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v2 render model.

    Deliberately *not* folded into :class:`MetadataStore`. PostgreSQL and D1
    are frozen at the v1 record model (PSU-2.30 §7 方案 6); adding a method to
    the shared protocol would make them non-conformant and force either a
    dishonest implementation — one that answers a chronological read with
    upload order — or an unfreezing this unit explicitly does not do.

    A store is probed for this at runtime, and what it answers is what
    ``/api/v1/health`` publishes as ``record_model_version``. That is the
    machine-readable version declaration decision D was approved with: the
    number a client negotiates against comes from the same object that would
    have to serve the request, so it cannot drift away from the truth.

    ``runtime_checkable`` only proves the methods exist, which is exactly the
    claim being made — no adapter defines these names and means something
    else by them.
    """

    def list_conversation_messages_in_time_order(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after: MessagePosition | None,
        limit: int,
    ) -> TimeOrderedMessagePage: ...


@dataclass(frozen=True)
class ArchiveAccountSummary:
    """One upload stream's share of an archive, as the accounts list shows it."""

    source_account_id: str
    record_count: int
    # What the account calls itself (PSU-2.40). ``None`` until a batch from
    # that stream has delivered one — which for rows synced by a machine that
    # never ran v5 means never. The page shows the opaque id and says so,
    # rather than deriving a name from the messages.
    display_name: str | None = None
    # The account's own picture (PSU-2.55). ``None`` conflates "the sender has
    # no picture" with "the sender never ran v10", and deliberately so: both
    # end at the same honest reading, an initial rather than a face.
    avatar_blob_id: str | None = None
    # 同一个微信的其它上传流 id——这个账号「以前还叫过什么」（PSU-2.90）。
    # 客户端拿它把书签里的老 id 换成现在这个，否则读侧翻译虽然让老链接答出了
    # 数据，URL 上那个 id 仍然对不上切换器里的任何一条。
    #
    # 空元组不是「不知道」：一条流要么有同伴要么没有，没归并过就是空的。
    # 按字典序，与选举取 min 是同一个排序。
    merged_source_account_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ArchiveAccountsSnapshot:
    """The archive's account dimension: who is in it, and how much is nobody's.

    ``unattributed_count`` are rows whose owning batch predates account-scoped
    ledgers — their stream cannot be proven, so they belong to no account
    rather than being assigned to whichever one looks likely.
    """

    accounts: tuple[ArchiveAccountSummary, ...]
    unattributed_count: int


@dataclass(frozen=True)
class MergeBacklog:
    """归并这件事此刻走到哪了，按一个归档汇总（PSU-2.86 的收尾）。

    存在的理由是一次真机验收：账号并完之后会话还没并，而**从界面上完全看不出**
    卡在哪——要么是"还没有第二台机器报过 key"，要么是"链接建了但那一轮的搬家
    还没跑"。两者的界面表现相同（还是两条），而处置完全不同：前者要去另一台
    机器同步，后者只要再同步一次就收敛。当时是登到服务器上查表才分清的。

    `*_link_count` 回答第一个问题（有没有人报过 key），`*_pending` 回答第二个
    （链接建了，搬家还欠几趟）。会话那一半还顺带回答"出站回填跑过没有"——
    会话 outbox 在远端确认后删行，所以本机侧问不出来，只有远端记得。
    """

    account_link_count: int
    account_merge_pending: int
    conversation_link_count: int
    conversation_merge_pending: int
    # 欠搬的**行**数，不是链接数（PSU-2.98）。真机上这两个数差三个数量级：
    # 313 条链接、216,282 行。只报链接数会让「还差多少」看起来像个位数的活。
    #
    # `None` = 后台 worker 还没跑过一趟。**不许当成零**：对 216k 行现算一次
    # COUNT 就是又一个慢查询挂在健康轮询上，所以这个数只能来自 worker 写的
    # 内存缓存，而缓存是空的时候诚实的答复是「还不知道」。
    conversation_merge_pending_rows: int | None = None
    # 搬过行、索引还没跟上的记录数（PSU-2.101 的账本）。行已经在新家，搜索还
    # 按旧家答；worker 一趟一趟还。回滚到不认识账本的版本前它必须是 0。
    search_rehome_pending: int = 0


@dataclass(frozen=True)
class ConversationMergeStep:
    """会话归并往前推的**一小段**（PSU-2.98）。

    `settled_pairs` 与 `pending_pairs` 说的是「对」，`moved_rows` 说的是行——
    一对大会话要几十批才清得完，两个口径都要有，界面上「313」与「216,282」
    才不会互相冒充。

    `contended` 不是失败：搜索索引住在另一个 sqlite 文件里，请求路径、回填
    线程与这条 worker 三方都写它，抢不到就这一批回滚、下一趟重来。
    """

    moved_rows: int
    settled_pairs: int
    pending_pairs: int
    done: bool = False
    contended: bool = False
    # 这一趟动的是哪一条败者会话（没活干时是 `None`）。
    conversation_id: str | None = None
    # 搬完、合完之后**再问一次判据，它仍然说欠搬**。
    #
    # 判据比 settle 清得掉的范围宽（`archive_conversation_state` 加三张会话表，
    # 任意一张有残留就算欠搬），所以这件事是可能的。调用方靠它把这一条记进
    # 跳过集合——没有它，一对清不掉的链接会把同一归档里其余所有对饿死，而
    # 那些本来是能清完的。
    #
    # **不能靠形状去猜**：一对「行已搬空、表里还有残留」的链接，settle 返回的
    # 就是 `moved_rows=0, settled_pairs=1`，与「一对本来就没有记录的正常链接」
    # 一模一样。所以这里是重查出来的一个事实，不是推断。
    stuck: bool = False
    # 这一趟从账本里重放了多少条索引写（PSU-2.101）。搬行与写索引分成两步之后，
    # 崩在中间或搜索库被占，账本里就会留下键；一趟只重放不搬新行时
    # `conversation_id` 是 `None`，调用方靠这个数分辨「没活干」与「在还账」。
    replayed_rows: int = 0


@dataclass(frozen=True)
class WriterLockStatus:
    """那把全局写锁此刻的样子（PSU-2.98）。

    2026-09-07 的写瘫里健康探针全程答 200，因为读路径根本不走写锁——界面据此
    显示"远程可用"，而所有设备一个字节都写不进去。这两个数是补上那条盲区的：
    ``held_ms`` 是**此刻**已经被握了多久（没人握着是 0），
    ``rejections_recent`` 是最近一分钟里有多少次写被快速拒掉。
    """

    held_ms: int
    rejections_recent: int


@runtime_checkable
class WriterLockObservableStore(Protocol):
    """能说出自己那把写锁状态的存储（PSU-2.98）。

    单独一个协议而不是折进 :class:`MergeAwareMetadataStore`：归并进度是"这个
    后端有没有账号维度"的问题，写锁是"这个后端是不是单写者"的问题，两件事在
    postgres / D1 那两支上的答案并不一样。探针按名字判，答不上来就不报。
    """

    def writer_lock_status(self) -> WriterLockStatus: ...


@runtime_checkable
class UploadCursorObservableStore(Protocol):
    """能按账号说出一台设备传到了哪一条的存储（PSU-2.165）。

    `sync-health` 的 `last_uploaded_local_sequence` 是这台设备**所有账号**游标的
    MAX；而客户端撞上「序号不连续」之后要对齐的，是其中一个账号的那一个。
    理由同上面两个协议：答不上来的后端就整个不报，客户端据此不对齐。
    """

    def device_upload_cursors(self, archive_id: str, device_id: str) -> dict[str, int]: ...


@runtime_checkable
class RecordRefsReadableStore(Protocol):
    """能按 record_key 答出一条消息眼下发布的附件引用（PSU-2.143）。

    上传原图前问「这条消息远端是不是已经有原图了」要用。单独一个协议，理由同
    上面两个：只有 SQLite 后端存了 ``attachment_refs``，答不上来就不按消息认领。
    """

    def attachment_refs_for_records(
        self, archive_id: str, record_keys: Sequence[str]
    ) -> Mapping[str, tuple[str, ...]]:
        """每个**已存在且带 refs** 的 record_key 当前的 refs；其余缺席。"""
        ...


@runtime_checkable
class MergeAwareMetadataStore(Protocol):
    """能报出归并进度的存储。分层同 :class:`RichMetadataStore`。

    不折进共享协议的理由与那一条逐字相同：账号归并只有 SQLite 后端有
    （postgres / D1 连账号维度都没有），把它写进 `MetadataStore` 会让那两个
    后端不再符合协议。探针按名字判，答不上来就不报——**客户端把"没报"读成
    "这个远端不知道"，而不是读成零**，那两件事对操作者意味着完全相反的处置。
    """

    def merge_backlog(self, archive_id: str) -> MergeBacklog: ...

    def read_conversation_id(self, archive_id: str, conversation_id: str) -> str:
        """这条会话 id 读起来是哪一条：它自己，或它被并进的那一条（PSU-2.95）。

        读方法内部本来就走这道翻译，但答复里说不出来——一页记录能靠行自己
        表明主语，**一页空记录不能**。而读侧的两个面都得说出这一句，否则拿老
        id 深链进来的客户端只能拿「我问的那个」当基准，把一个正确的答复读成
        协议错（PSU-2.90 真机上就是这样卡住的）。

        答得出的 id 一个都不筛：没有链接的照旧是它自己，压根不存在的也是它
        自己。「被并掉的」与「打错的」在这里不分——分它们是读方法的事，那里
        答空；在这里编造一个 ``None`` 只会让调用方多一条兜底分支。
        """
        ...


@runtime_checkable
class AccountScopedMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v3 record model.

    Same layering as :class:`RichMetadataStore`, one version up: the account
    dimension (PSU-2.35a) is not folded into the shared protocol because the
    v1 backends (PostgreSQL, D1) are frozen and must stay conformant. What a
    store answers to this probe is what ``/api/v1/health`` publishes as
    ``record_model_version`` — 3 when it can scope reads by account.

    The scoped read methods repeat the base signatures with the optional
    ``source_account_id`` so callers can pass the filter through a correctly
    narrowed type instead of a cast; ``list_account_summaries`` is the name
    the runtime probe keys on.
    """

    def list_account_summaries(self, archive_id: str) -> ArchiveAccountsSnapshot: ...

    def list_conversations(
        self,
        archive_id: str,
        *,
        after_conversation_id: str = "",
        limit: int,
        source_account_id: str | None = None,
    ) -> ConversationPage: ...

    def list_conversation_messages(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after_server_sequence: int,
        limit: int,
        source_account_id: str | None = None,
    ) -> MessagePage: ...

    def list_conversation_messages_in_time_order(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after: MessagePosition | None,
        limit: int,
        source_account_id: str | None = None,
    ) -> TimeOrderedMessagePage: ...

    def search(
        self,
        archive_id: str,
        query: str,
        *,
        limit: int,
        source_account_id: str | None = None,
    ) -> tuple[StoredRecord, ...]: ...


@runtime_checkable
class AccountIdentityMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v5 account identity.

    Same layering as the three protocols above, one version up. Unlike them,
    v5 changes what a *sender* puts on the wire, so the probe governs an
    upload as well as a read: a client that sees less than 5 must not attach
    an account section, because a remote that cannot keep it would reject the
    whole batch on its ``additionalProperties: false``.

    The method is the one the read path actually calls, not a marker: a store
    that can answer it is exactly a store that kept the section.
    """

    def publish_account_identity(
        self,
        archive_id: str,
        source_account_id: str,
        account: AccountInput,
    ) -> None: ...

    def account_display_name(
        self,
        archive_id: str,
        source_account_id: str,
    ) -> str | None: ...


@runtime_checkable
class TranscriptMetadataStore(Protocol):
    """能收下语音转写的 store（记录模型 v13，PSU-2.163）。

    自己长一个协议，而不是像 v9~v12 那样共用最上面那一档：那几版加的是表、列
    和读答复里的键，**协议只看得见方法**，所以它们探测不出来也无所谓——缺了
    只是少一个键。v13 加的是一条**发布流**，有方法，也有真实后果：一个答不出
    它的远端会把整片转写连同 404 一起退回，而发送方会把那当成网络故障反复重试。
    """

    def publish_transcripts(
        self,
        archive_id: str,
        source_account_id: str,
        transcripts: TranscriptsInput,
    ) -> None: ...


@runtime_checkable
class RosterMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v8 roster (PSU-2.47).

    Same layering as the protocols above, one version up. Unlike the read-only
    ones, v8 changes what a *sender* puts on the wire, so the probe governs an
    upload as well as a read: a client that sees less than 8 must not publish a
    roster, because a remote that cannot keep it would reject the whole body.

    What v8 adds is the archive's address book — who these uids are, which of
    them are in which group, and where their pictures live. Three differences
    in the parity table are one dataset: the avatars are missing everywhere,
    the conversation info drawer has nothing to show, and the name filter
    matches one column where the local one matches seven.
    """

    def publish_roster(
        self,
        archive_id: str,
        source_account_id: str,
        roster: RosterInput,
    ) -> None: ...

    def list_conversation_members(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
        after_member_uid: str = "",
        limit: int,
    ) -> ConversationMemberPage: ...


@runtime_checkable
class SearchableMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v7 search model.

    Same layering as the protocols above, one version up, and for the same
    reason: PostgreSQL and D1 stay frozen at v1.

    What v7 adds is not new *data* — it is searching the archive the way the
    local app searches it. Up to v6 the remote answered a search with a full
    scan of ``instr(normalized_content, …)``: no index, a hard cap of 500 rows,
    no cursor, no total, no per-conversation counts, and none of the six
    filters the local search has always had. The index that serves this is the
    local one (``chat_archive.search.MessageSearchIndex``) run unchanged over
    the remote's own records, which is why the two ends can match on the same
    rule instead of approximating each other.

    A probe rather than a widening of :class:`MetadataStore` because the new
    filters must never reach a backend that would *ignore* them — a silently
    unfiltered answer is the failure this whole layering exists to prevent.
    """

    def search_page(
        self,
        archive_id: str,
        query: str | None,
        *,
        source_account_id: str | None = None,
        conversation_id: str | None = None,
        sender_uid: str | None = None,
        message_types: tuple[str, ...] | None = None,
        has_attachment: bool | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int,
    ) -> RecordSearchPage: ...


@runtime_checkable
class ConversationDetailMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v6 conversation identity.

    Same layering as the four protocols above, one version up, and for the same
    reason: PostgreSQL and D1 stay frozen at v1 and must stay conformant.

    What v6 adds is the ability to answer "who is this conversation" **without
    walking the list**. Up to v5 the only way to learn a conversation's name was
    to find it in a page of ``list_conversations*``, which made the name depend
    on which pages the reader happened to have loaded — change the filter and
    the title of the conversation you are already reading degrades to its opaque
    id. The local archive has had this endpoint all along
    (``/api/v1/conversations/{id}``); this is the remote growing the same one,
    not a new mechanism.

    ``None`` means the archive has no such conversation in the requested scope.
    That is a different answer from "it has no name yet", which is a summary
    whose ``display_name`` is ``None`` — the caller must be able to tell them
    apart, so the absence is not spelled as an empty summary.
    """

    def get_conversation(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> ConversationSummary | None: ...


@runtime_checkable
class BrowsableMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v4 browse model.

    Same layering as :class:`RichMetadataStore` and
    :class:`AccountScopedMetadataStore`, one version up, and for the same
    reason: PostgreSQL and D1 stay frozen at v1 and must stay conformant.

    What v4 adds is not new *data* — it is reading the archive the way the
    local app reads it. Up to v3 the remote answered "which conversations"
    ordered by an opaque id and "which messages" from the beginning of time,
    because those are the orders a keyset can walk cheaply off the columns the
    upload happens to carry. Neither is what a person browsing a chat archive
    means, and the difference is not cosmetic: the first screen of the list was
    an arbitrary hundred conversations, and opening a conversation landed on
    its oldest message.

    Both methods here mirror one in ``chat_archive.query`` field for field, so
    that "the remote is the same product over a different data source" is a
    property of the code rather than a claim in a document.
    """

    def list_conversations_by_recency(
        self,
        archive_id: str,
        *,
        after: ConversationPosition | None,
        limit: int,
        source_account_id: str | None = None,
        query: str | None = None,
    ) -> RecentConversationPage: ...

    def list_conversation_messages_before(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        before: MessagePosition | None,
        limit: int,
        source_account_id: str | None = None,
    ) -> TimeOrderedMessagePage: ...


@runtime_checkable
class AnchoredMetadataStore(Protocol):
    """The extra a store must be able to do to serve the v9 anchor model.

    Same layering as the protocols above, one version up, and for the same
    reason: PostgreSQL and D1 stay frozen at v1 and must stay conformant.

    v9 adds no data and no ordering. A window around a message is two reads
    this store could already do — ``list_conversation_messages_before`` for the
    older half and ``list_conversation_messages_in_time_order`` for the newer —
    and both of them take a :class:`MessagePosition`. What was missing is the
    step before: a caller holding a message id has no way to name the point on
    the reading line that id sits at, because that point is four columns.

    The anchor row comes back whole rather than as a bare position, because
    the window has to *contain* it: both paging reads are strict on their
    keyset, so a caller given only the point would have a hole where the
    message it asked about should be.

    Answering ``None`` is a real answer, not an error: the id may belong to
    another conversation, to another account, or to nothing this archive kept.
    All three mean the same thing to the caller — *there is no context here to
    show* — and none of them is worth telling apart at this layer, which is why
    resolving is separate from reading rather than a mode of it.
    """

    def get_conversation_message(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        record_key: str,
        source_account_id: str | None = None,
    ) -> StoredRecord | None: ...

"""Stable archive domain models aligned with the system design."""

from __future__ import annotations

from enum import Enum

from pydantic import BaseModel, ConfigDict, Field, field_validator

from chat_archive.domain.app_card import AppCard


class ArchiveModel(BaseModel):
    """Base model that fails closed on unexpected data."""

    model_config = ConfigDict(extra="forbid")


class ParseStatus(str, Enum):
    PARSED = "parsed"
    PARTIAL = "partial"
    UNSUPPORTED = "unsupported"


class AttachmentCacheStatus(str, Enum):
    DISCOVERED = "discovered"
    LOCATING = "locating"
    DECRYPTING = "decrypting"
    TRANSCODING = "transcoding"
    READY = "ready"
    SOURCE_MISSING = "source_missing"
    UNSUPPORTED = "unsupported"
    FAILED = "failed"
    EVICTED = "evicted"


class MessageType(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    LINK = "link"
    QUOTE = "quote"
    EMOJI = "emoji"
    APP = "app"
    SYSTEM = "system"
    UNKNOWN = "unknown"


class AttachmentKind(str, Enum):
    IMAGE = "image"
    EMOJI = "emoji"
    AUDIO = "audio"
    VIDEO = "video"
    FILE = "file"
    UNKNOWN = "unknown"


class JobStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    PAUSING = "pausing"
    PAUSED = "paused"
    INTERRUPTED = "interrupted"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class SourceAccount(ArchiveModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    provider: str = "wechat"
    account_uid: str
    display_name: str
    client_version: str | None = None
    status: str = "detected"
    source_instance_id: str
    canonical_account_id: str | None = None


class Contact(ArchiveModel):
    id: str
    source_account_id: str
    contact_uid: str
    display_name: str
    remark: str | None = None
    avatar_ref: str | None = None
    avatar_source_url: str | None = Field(default=None, exclude=True, repr=False)
    signature: str | None = None
    source_description: str | None = None
    added_at: int | None = None
    is_unknown: bool = False
    raw_identity_label: str | None = None


class Conversation(ArchiveModel):
    id: str
    source_account_id: str
    conversation_uid: str
    display_name: str
    is_group: bool = False
    last_message_at: int | None = None
    message_count: int = 0
    media_count: int = 0
    announcement: str | None = None
    my_group_alias: str | None = None


class ConversationMember(ArchiveModel):
    conversation_id: str
    member_uid: str
    display_name: str
    contact_id: str | None = None
    group_alias: str | None = None
    is_self: bool = False
    is_unknown: bool = False
    raw_identity_label: str | None = None
    avatar_ref: str | None = None


class Attachment(ArchiveModel):
    id: str
    message_id: str
    kind: AttachmentKind | str
    source_reference: str | None = Field(default=None, repr=False)
    file_name: str | None = None
    mime_type: str | None = None
    size: int | None = None
    duration_ms: int | None = None
    sha256: str | None = None
    cache_status: AttachmentCacheStatus = AttachmentCacheStatus.DISCOVERED
    failure_code: str | None = None
    cache_path: str | None = Field(default=None, repr=False)
    object_key: str | None = Field(default=None, repr=False)
    last_accessed_at: int | None = None
    decoder_version: str | None = None
    variant: str | None = None
    thumb_cache_path: str | None = Field(default=None, repr=False)
    thumb_size: int | None = None
    attempt_count: int = 0

    @field_validator("kind", mode="before")
    @classmethod
    def validate_kind(cls, value: object) -> AttachmentKind:
        return AttachmentKind(value)


class SourceCoordinate(ArchiveModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source_instance_id: str
    source_db_fingerprint: str
    source_partition: str
    source_table: str
    source_row_id: int
    # The source's own ordering of this message inside its conversation, kept
    # because `created_at` is only accurate to the second and a shared second is
    # otherwise decided by the archive's opaque id -- a coin flip. 0 means the
    # source did not say; those rows fall back to the old tie-break.
    source_sequence: int = 0
    source_local_id: int = 0
    schema_hash: str | None = None


class ParseProvenance(ArchiveModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    parser_name: str
    parser_version: str
    # Set only on messages the card derivation actually covers. None everywhere
    # else, so "this row has no card version" and "this row's card version is
    # old" stay different facts -- a text message is not behind on cards, it is
    # outside them, and a selector that confused the two would queue 59,601
    # rows nothing would change.
    app_card_version: int | None = None


class MessageRawPayload(ArchiveModel):
    """The source bytes a message was parsed from, kept for later replay.

    Local-only by construction. `docs/local-remote-boundary.md` rule 2 keeps
    the WeChat wire format on the machine that can already decrypt it: the
    remote runtimes store parsed domain models and must not acquire the ability
    to read the original format. So this never rides a `RemoteRecord`, never
    enters an export, and is excluded from `Message` serialization below.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    payload: bytes
    encoding: str
    sha256: str
    packed_info: bytes | None = None
    # The shape of the source table these bytes came from. Distinct from
    # `SourceCoordinate.schema_hash`, which the chatlog-keeper gateway has never
    # populated -- this one is measured at capture, off the table actually read.
    source_schema_hash: str | None = None


class Message(ArchiveModel):
    id: str
    conversation_id: str
    source_account_id: str
    source_message_id: str | None = None
    sender_uid: str
    sender_display_name: str | None = None
    created_at: int
    message_type: MessageType | str
    sub_type: str | None = None
    content_text: str
    raw_content_hash: str | None = None
    is_self: bool
    provider_message_key: str
    source: SourceCoordinate
    parser: ParseProvenance
    reply_to_source_key: str | None = None
    reply_to_id: str | None = None
    parse_status: ParseStatus = ParseStatus.PARSED
    raw_type_code: str | None = None
    # AppCard is a frozen dataclass; pydantic v2 handles dataclass fields
    # directly, so the card needs no parallel model.
    app_card: AppCard | None = None
    # exclude=True is load-bearing, not tidiness. Every model_dump of a message
    # feeds something that must not carry source bytes: ArchiveBatch.checksum
    # would base64 them into a transient JSON blob for every batch, and any
    # future serializer would ship them somewhere they are not allowed to go.
    # The payload's identity still travels, as raw_content_hash above.
    raw_payload: MessageRawPayload | None = Field(default=None, exclude=True, repr=False)

    @field_validator("message_type", mode="before")
    @classmethod
    def validate_message_type(cls, value: object) -> MessageType:
        return MessageType(value)


class Job(ArchiveModel):
    id: str
    source_account_id: str
    kind: str
    status: JobStatus = JobStatus.QUEUED
    stage: str | None = None
    progress_current: int = 0
    progress_total: int | None = None
    stage_progress_current: int = 0
    stage_progress_total: int | None = None
    stage_progress_unit: str | None = None
    stage_started_at: int | None = None
    recoverable: bool = True
    error_code: str | None = None
    error_summary: str | None = None

"""Read-only remote archive contract for newly paired devices."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Protocol, cast, runtime_checkable

from chat_archive.domain.reply_preview import ReplyPreviewRecord


class RemoteArchiveError(RuntimeError):
    """Stable transport/validation error that never carries remote payloads or secrets."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True)
class RemoteVoiceTranscript:
    """远端交过来的一条机器转写（记录模型 v13）。

    没有 `status`/`failure_code`：远端只存得下转成功的那些，所以「有这个对象」
    就是那个状态。形状与本机 `VoiceTranscript` 的可判定部分对齐，读侧不翻译。
    """

    text: str = field(repr=False)
    engine: str
    low_confidence: bool


@dataclass(frozen=True)
class RemoteMessage:
    """A message as a remote archive hands it over.

    Everything from ``sender_uid`` down is PSU-2.30's render model, and
    ``None`` there means *the remote was never told* — a v1 remote, or a
    record uploaded before the widening. It is not "empty", and the page must
    not draw it as if it were.
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
    # PSU-2.35a：这条记录归哪条上传流。``None`` 兼指「v2 及更早的远端根本
    # 不说这个」与「v3 远端说了：这行未归属」——两种情况读侧都不该派给谁。
    source_account_id: str | None = None
    # PSU-2.47：发送者头像的 blob 标识，**读时接上的**，不是冻在记录里的。
    # 人会换头像，冻在 2019 年那条消息上的副本会永远是旧的。``None`` 兼指
    # 「远端到不了 v8」「这个人没送过头像」与「这条记录还没归属」。
    sender_avatar_blob_id: str | None = None
    # PSU-2.163：这条语音的机器转写，同样是读时接上的。``None`` 兼指
    # 「远端到不了 v13」「这条不是语音」与「本机还没把它发过来」——三种
    # 情况读侧的动作相同：不画那一行。
    transcript: RemoteVoiceTranscript | None = None
    reply_preview: ReplyPreviewRecord | None = None


@dataclass(frozen=True)
class RemoteMessagePosition:
    """Keyset position in a conversation's reading order."""

    created_at: int
    source_sequence: int
    source_local_id: int
    record_key: str


@dataclass(frozen=True)
class RemoteRecord:
    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    content_sha256: str
    source_instance_id: str
    schema_version: str
    server_sequence: int


@dataclass(frozen=True)
class RemoteConversationSummary:
    conversation_id: str
    message_count: int
    last_created_at: int
    last_server_sequence: int
    # ``None`` until the conversation stream has delivered a name for this id.
    display_name: str | None = None
    is_group: bool | None = None
    # PSU-2.35a：独占这条会话的那一个账号；老远端、合并行与未归属都是 None。
    source_account_id: str | None = None
    # PSU-2.44：列表行上那一句「最后一条消息」。两个都是 None 兼指「远端到不了
    # v6」与「这条会话没有可预览的消息」——界面两种都退回条数，本来就是同一句话。
    last_message_type: str | None = None
    last_message_preview: str | None = None
    # PSU-2.47：通讯录那一侧才知道的几样。全 None 兼指「远端到不了 v8」与
    # 「这条会话没有这几样」——界面两种都退回首字母头像、不画抽屉。
    avatar_blob_id: str | None = None
    announcement: str | None = None
    my_group_alias: str | None = None
    # **没有 roster 时是 None，不是 0**：0 会声称「这个群是空的」，而没有群是空的。
    member_count: int | None = None


@dataclass(frozen=True)
class RemoteConversationMember:
    """一个群成员，如远端的通讯录所记（PSU-2.47）。"""

    member_uid: str
    display_name: str
    group_alias: str | None = None
    is_self: bool = False
    avatar_blob_id: str | None = None


@dataclass(frozen=True)
class RemoteConversationMemberPage:
    members: tuple[RemoteConversationMember, ...]
    next_member_uid: str | None
    has_more: bool


@dataclass(frozen=True)
class RemoteAccountSummary:
    """一条上传流在这个归档里的份额。"""

    source_account_id: str
    record_count: int
    # PSU-2.40：这个账号自己报的名字。``None`` 兼指「远端到不了 v5」与
    # 「v5 远端说了：这条流还没送过名字」——两种读侧都只能显示标识。
    display_name: str | None = None
    # PSU-2.55：这个账号自己的头像（记录模型 v10）。``None`` 同样兼指两种情形，
    # 而它们在界面上是同一件事：印首字母，不摆一个取不到字节的 <img>。
    avatar_blob_id: str | None = None
    # PSU-2.90：同一个微信的其它上传流 id——这个账号「以前还叫过什么」。
    # 客户端拿它把书签 URL 里的老 id 换成现在这个。空元组既是「没归并过」，
    # 也是「这个远端还没到 v11」——两种读侧行为相同：URL 上的 id 照旧不动。
    merged_source_account_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class RemoteAccountsPage:
    """归档的账号维度：谁在里面、有多少行谁的都不是。"""

    items: tuple[RemoteAccountSummary, ...]
    unattributed_count: int


@dataclass(frozen=True)
class RemoteSearchPosition:
    """搜索排序线上的一个点：时间倒序，记录标识破并列。"""

    created_at: int
    record_key: str


@dataclass(frozen=True)
class RemoteSearchResultPage:
    """一页命中，连同这一次搜索的全貌（PSU-2.45）。

    `index_status` 是 `ready` / `building` / `unavailable`。**必须到得了界面**：
    没被告知索引还在建的人，会把「搜不到」读成「没有这条消息」。
    """

    records: tuple[RemoteMessage, ...] = field(repr=False)
    next_position: RemoteSearchPosition | None
    has_more: bool
    total_count: int
    conversation_counts: dict[str, int]
    index_status: str


@dataclass(frozen=True)
class RemoteConversationPosition:
    """Keyset position in the browse order: most recently active first."""

    last_created_at: int
    conversation_id: str


@dataclass(frozen=True)
class RemoteConversationPage:
    conversations: tuple[RemoteConversationSummary, ...]
    next_conversation_id: str | None
    has_more: bool
    # Set instead of ``next_conversation_id`` when the page was read in browse
    # order. Exactly one of the two is meaningful per page, decided by which
    # order was asked for -- the same arrangement ``RemoteMessagePage`` uses.
    next_position: RemoteConversationPosition | None = None


@dataclass(frozen=True)
class RemoteMessagePage:
    records: tuple[RemoteMessage, ...] = field(repr=False)
    next_server_sequence: int
    has_more: bool
    # Set instead of advancing ``next_server_sequence`` when the page was read
    # in reading order. Exactly one of the two is meaningful per page, decided
    # by which order was asked for.
    next_position: RemoteMessagePosition | None = None
    # PSU-2.95：这一页是**关于哪条会话**的，按远端自己的说法。归并之后远端
    # 会把被并掉的 id 读成 primary 再作答，这一句是它说出那次翻译的地方。
    # ``None`` 是「这个远端没说」——v11 及更早，那时读侧只能拿调用方问的那个
    # 当主语。一页记录能靠行自己表明主语，**一页空记录不能**，所以这句话不能
    # 从行里反推。
    read_conversation_id: str | None = None


@dataclass(frozen=True)
class RemoteMessageWindow:
    """A slice of a transcript centred on one record (v9, PSU-2.48).

    No cursor of its own: reading further is asking again with the record at
    the edge as the new anchor, the same walk the local archive's window does.
    """

    records: tuple[RemoteMessage, ...] = field(repr=False)
    anchor_record_key: str
    # 与 :class:`RemoteMessagePage` 的同名字段同一件事、同一种 ``None``。
    read_conversation_id: str | None = None


@dataclass(frozen=True)
class RemoteRecordPage:
    records: tuple[RemoteRecord, ...] = field(repr=False)
    next_server_sequence: int
    has_more: bool


@dataclass(frozen=True)
class RemoteObjectStorageGeneration:
    """One stored bucket/region pairing, so old attachments can be named."""

    storage_generation: int
    endpoint: str
    bucket: str
    region: str
    access_key_hint: str
    updated_at: int
    active: bool


@dataclass(frozen=True)
class RemoteObjectStorageConfig:
    """Non-secret view of the remote object storage configuration.

    There is deliberately no secret key field: the shape itself makes an echo
    impossible, so no future edit can leak one by accident.
    """

    configured: bool
    provider: str
    endpoint: str | None
    bucket: str | None
    region: str | None
    access_key_hint: str | None
    storage_generation: int
    status: str
    updated_at: int | None
    generations: tuple[RemoteObjectStorageGeneration, ...] = ()


@dataclass(frozen=True)
class RemotePresignedAttachment:
    """A short-lived, direct-to-object-storage URL for one attachment.

    The bytes never pass through an API process — that is the whole point of
    handing out a URL instead of streaming — so this carries no content, only
    where to get it and until when.

    ``storage_origin`` is the scheme+host the URL points at, published
    separately so the page can be told which origin it is about to talk to
    without parsing the signed URL (which carries the signature in its query
    string and should not be picked apart).
    """

    url: str = field(repr=False)
    method: str
    expires_at: int
    storage_origin: str


@dataclass(frozen=True)
class RemoteObjectStorageVerification:
    verified: bool
    status: str
    capabilities: Mapping[str, object]


@dataclass(frozen=True)
class RemoteBackupConfig:
    """备份目标摘要。刻意没有放秘密的字段。"""

    configured: bool
    endpoint: str | None
    bucket: str | None
    region: str | None
    prefix: str | None
    access_key_hint: str | None
    updated_at: int | None
    interval_hours: int
    retention_mode: str
    daily_keep: int
    weekly_keep: int
    monthly_keep: int
    # 远端解不解得开存着的凭据。恢复之后是 False（主密钥不进备份）。
    credentials_readable: bool = True


@dataclass(frozen=True)
class RemoteBackupVerification:
    verified: bool
    can_delete: bool
    lifecycle_prefixes: tuple[str, ...] | None


@dataclass(frozen=True)
class RemoteBackupRun:
    id: int
    started_at: int
    finished_at: int | None
    status: str
    backup_id: str | None
    source_bytes: int | None
    stored_bytes: int | None
    failure_code: str | None
    verified: bool
    lifecycle_covered: bool | None = None


@dataclass(frozen=True)
class RemoteBackupStatus:
    last_backup_at: int | None
    last_backup_bytes: int | None
    last_backup_status: str
    backup_retention_mode: str | None
    backup_overdue: bool
    runs: tuple[RemoteBackupRun, ...]
    # 最近一次成功之后桶上的过期规则是否盖住备份前缀。`None` = 不适用或问不到；
    # 只有 `False` 是「规则被删了」。
    backup_lifecycle_covered: bool | None = None


@dataclass(frozen=True)
class RemoteBackupDownloadPart:
    name: str
    size: int
    sha256: str
    stored_size: int
    # 签名链接。与 `RemotePresignedUrl.url` 同一条理由排除在 repr 之外：
    # 它本身就是一次授权。
    url: str = field(repr=False)


@dataclass(frozen=True)
class RemoteBackupDownload:
    backup_id: str
    created_at: int
    app_version: str
    expires_in: int
    parts: tuple[RemoteBackupDownloadPart, ...]
    restore_notes: tuple[str, ...]


@dataclass(frozen=True)
class RemoteHealth:
    archive_id: str
    backend_type: str
    device_id: str
    last_uploaded_local_sequence: int
    last_server_sequence: int
    remote_used_bytes: int
    remote_blob_count: int
    pending_attachment_count: int
    conflict_count: int
    last_success_at: int | None
    last_reconciled_at: int | None
    last_recovery_verified_at: int | None
    remote_service_status: str
    object_storage_status: str
    # Free bytes on the remote's data volume, or ``None`` when it does not say
    # — a remote built before PSU-2.31, or one whose object storage lives on a
    # host its API cannot measure. ``None`` means "unknown", which the
    # attachment pre-flight treats as "proceed without the check": refusing an
    # upload on a number nobody has would be worse than not checking.
    remote_available_bytes: int | None = None
    # 归并进度（PSU-2.86 收尾）。`None` 是"这个远端不知道"——建于本单元之前的
    # 服务端，或后端根本没有账号维度（postgres / D1）。**不许当成零**：零的意思是
    # "没有待办"，而不知道的意思是"别拿这个数下结论"。
    account_link_count: int | None = None
    account_merge_pending: int | None = None
    conversation_link_count: int | None = None
    conversation_merge_pending: int | None = None
    # 远端写锁与后台欠账（PSU-2.98 / 2.101 加在服务端，PSU-2.102 接到这里）。
    # `None` 一律是「这个远端不报」：老远端、非 SQLite 后端、worker 还没数过——
    # **不许当成零**。写锁那两个说的是「现在别同步」，归并 / 账本说的是远端
    # 后台还在还的账。
    conversation_merge_pending_rows: int | None = None
    search_rehome_pending_rows: int | None = None
    writer_lock_held_ms: int | None = None
    writer_lock_rejections_recent: int | None = None
    # 定时备份（PSU-2.140/2.141）。`None` 一律是「这个远端答不出备份这件事」——
    # 老远端、postgres-s3（它的灾备是 pgBackRest 的 recovery point）、Worker
    # （没有文件系统也没有长任务）。**不许当成"没备过"**：那是
    # `last_backup_status == "never"` 说的话。
    last_backup_at: int | None = None
    last_backup_bytes: int | None = None
    last_backup_status: str | None = None
    backup_retention_mode: str | None = None
    backup_overdue: bool | None = None
    backup_lifecycle_covered: bool | None = None


class WritableBinaryStream(Protocol):
    def write(self, data: bytes, /) -> int: ...


class RemoteBlobTransport(Protocol):
    def download_blob(
        self,
        archive_id: str,
        blob_id: str,
        sink: WritableBinaryStream,
    ) -> None: ...


class RemoteRecordTransport(Protocol):
    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int = 0,
        limit: int = 100,
    ) -> RemoteRecordPage: ...


class RemoteArchiveTransport(Protocol):
    supports_concurrent_requests: bool
    """这个传输的读端点扛不扛得住被几个线程同时调。

    扇出搜索按它决定并发还是串行地发那几条流。读它的一侧用
    `getattr(..., False)` 取——没声明就是不扛：把「没核实过线程安全」默认成
    可并发，换来的是一类并发问题而不是提速，所以沉默必须落在串行那条安全路上。
    """

    def record_model_version(self) -> int:
        """Which record model this remote keeps; 1 when it does not declare one.

        Required of every transport rather than probed for, because the read
        path has to choose an ordering before it issues its first page and
        there is no safe guess: assuming v2 against a v1 remote asks for a
        keyset it will reject, and assuming v1 against a v2 remote silently
        pages the archive in upload order for the rest of the session.
        """
        ...

    def get_object_storage_config(
        self,
        archive_id: str,
    ) -> RemoteObjectStorageConfig: ...

    def test_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageVerification: ...

    def put_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageConfig: ...

    def get_backup_config(self, archive_id: str) -> RemoteBackupConfig: ...

    def test_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupVerification: ...

    def put_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupConfig: ...

    def list_backup_runs(self, archive_id: str) -> RemoteBackupStatus: ...

    def request_backup_run(self, archive_id: str) -> RemoteBackupStatus: ...

    def latest_backup_download(self, archive_id: str) -> RemoteBackupDownload: ...

    def restore_object_storage_generation(
        self,
        archive_id: str,
        storage_generation: int,
    ) -> RemoteObjectStorageConfig: ...

    def sync_health(self, archive_id: str) -> RemoteHealth: ...

    def list_conversations(
        self,
        archive_id: str,
        *,
        after_conversation_id: str | None = None,
        recent: bool = False,
        after_position: RemoteConversationPosition | None = None,
        query: str | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> RemoteConversationPage:
        """One page of the conversation list, in one of two orders.

        ``recent`` (PSU-2.39, remotes declaring v4) is the order a person
        browses in and the only one that accepts ``query``; it pages by
        ``after_position``. Without it the list keeps its pre-v4 walk by
        opaque id, which is what a frozen remote can answer.
        """
        ...

    def get_conversation(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> RemoteConversationSummary | None:
        """One conversation's identity, without walking the list (PSU-2.44).

        Only remotes declaring ``record_model_version >= 6`` have this; the
        caller (the facade) asks the version first, so a remote that cannot
        answer is never asked.

        ``None`` means the remote said it has no such conversation. Kept
        distinct from an error on purpose: "this archive is gone" and "this
        archive has no such conversation" are different sentences, and the
        second one is a normal answer under an account filter.
        """
        ...

    def list_conversation_members(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
        after_member_uid: str | None = None,
        limit: int = 200,
    ) -> RemoteConversationMemberPage:
        """One page of a conversation's member roster (PSU-2.47).

        Only remotes declaring ``record_model_version >= 8`` have this, and the
        facade asks the version first, so one that cannot answer is never
        asked. An empty page is a real answer — a conversation whose roster has
        not been published yet — and is not the same as the capability being
        absent, which is why the two are not both spelled as "no members".
        """
        ...

    def list_conversation_messages(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after_server_sequence: int = 0,
        chronological: bool = False,
        latest: bool = False,
        after: RemoteMessagePosition | None = None,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> RemoteMessagePage:
        """One page of a transcript, in one of three orders.

        ``latest`` (PSU-2.39, remotes declaring v4) walks the same reading
        order as ``chronological`` but from the newest end, so a transcript
        opens where the conversation is now. ``after`` then names the *oldest*
        record already held -- a keyset position is a point on the sort line,
        and which side of it you want is what the flag says.
        """
        ...

    def message_window(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        anchor_record_key: str,
        before: int = 20,
        after: int = 20,
        source_account_id: str | None = None,
    ) -> RemoteMessageWindow:
        """The records around one anchor, the anchor included.

        Only remotes declaring ``record_model_version >= 9`` have this, and the
        service asks the version first, so one that cannot answer is never
        asked. A remote that does not hold the anchor in this conversation
        raises ``NOT_FOUND`` rather than answering with some other screen:
        "here is the context" and "here is the newest page" must not look
        alike to the reader.
        """
        ...

    def search(
        self,
        archive_id: str,
        query: str,
        *,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> tuple[RemoteMessage, ...]: ...

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
        after: RemoteSearchPosition | None = None,
        limit: int = 100,
    ) -> RemoteSearchResultPage:
        """带索引与过滤的搜索（PSU-2.45，远端声明 `record_model_version >= 7`）。

        与 `search` 是**两条**路而不是一个可选参数：更早的远端会忽略认不得的
        过滤参数并把没筛过的结果答回来，而那正是这些参数存在的全部理由。
        调用方（facade）先按版本决定问哪一条。
        """
        ...

    def list_accounts(self, archive_id: str) -> RemoteAccountsPage:
        """归档的账号维度（PSU-2.35a）。

        只有声明 `record_model_version >= 3` 的远端有这个端点；调用方
        （facade）先按版本发问，不该问的远端不会被问到。
        """
        ...

    def presign_attachment(
        self,
        archive_id: str,
        blob_id: str,
    ) -> RemotePresignedAttachment: ...

    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int = 0,
        limit: int = 100,
    ) -> RemoteRecordPage: ...

    def download_blob(
        self,
        archive_id: str,
        blob_id: str,
        sink: WritableBinaryStream,
    ) -> None: ...


@dataclass(frozen=True)
class RemoteShare:
    """「我的分享」里的一行（PSU-2.181）。不含快照，也不含对象键。

    `path` 是远端上的页面地址（`/s/<令牌>`）；拼成完整链接是 facade 的事，
    因为只有它知道这台远端的对外地址。
    """

    token: str
    path: str
    title: str
    message_count: int
    attachment_count: int
    attachment_bytes: int
    created_at: int
    expires_at: int | None
    # draft / live / expired
    state: str
    views: int
    last_viewed_at: int | None
    # 什么时候会被清掉：过期的（过期后再留 7 天）与草稿有这个数，有效的是 None。
    purge_at: int | None
    # 只有建草稿的应答里有（PSU-2.184）：远端没能在对象存储里复制、还要逐个上传的
    # 分享内附件 id。None＝应答里没有这个键（旧远端，或不是建草稿）——当作「全部要传」。
    awaiting_upload: tuple[str, ...] | None = None


@runtime_checkable
class RemoteShareTransport(Protocol):
    """在线分享的建与管（PSU-2.181）。

    单列一个协议而不并进 `RemoteArchiveTransport`：只有自托管 SQLite 那一支答得出
    分享这件事，不支持的传输就是不实现它，facade 据此报「这台远端不支持」，而不是
    让每一个传输都长出六个只会抛异常的方法。
    """

    def create_share(self, archive_id: str, payload: Mapping[str, object]) -> RemoteShare: ...

    def upload_share_object(
        self, archive_id: str, token: str, attachment_id: str, content: bytes
    ) -> None: ...

    def finalize_share(self, archive_id: str, token: str) -> RemoteShare: ...

    def list_shares(self, archive_id: str) -> tuple[RemoteShare, ...]: ...

    def extend_share(self, archive_id: str, token: str) -> RemoteShare: ...

    def delete_share(self, archive_id: str, token: str) -> None: ...


def remote_share_from(value: Mapping[str, object]) -> RemoteShare:
    """把远端那一行读成 `RemoteShare`；形状不对抛 `RemoteArchiveError`。"""

    def text(key: str) -> str:
        item = value.get(key)
        if not isinstance(item, str):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return item

    def number(key: str) -> int:
        item = value.get(key)
        if isinstance(item, bool) or not isinstance(item, int):
            raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
        return item

    def optional(key: str) -> int | None:
        return None if value.get(key) is None else number(key)

    state = text("state")
    if state not in {"draft", "live", "expired"}:
        raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
    return RemoteShare(
        token=text("token"),
        path=text("path"),
        title=text("title"),
        message_count=number("message_count"),
        attachment_count=number("attachment_count"),
        attachment_bytes=number("attachment_bytes"),
        created_at=number("created_at"),
        expires_at=optional("expires_at"),
        state=state,
        views=number("views"),
        last_viewed_at=optional("last_viewed_at"),
        purge_at=optional("purge_at"),
        awaiting_upload=_awaiting_upload(value.get("awaiting_upload")),
    )


def _awaiting_upload(value: object) -> tuple[str, ...] | None:
    if value is None:
        return None
    if not isinstance(value, list):
        raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
    items = cast(list[object], value)
    if not all(isinstance(item, str) for item in items):
        raise RemoteArchiveError("REMOTE_INVALID_RESPONSE")
    return tuple(cast(list[str], items))

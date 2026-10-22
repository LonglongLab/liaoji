"""把「远程归档」这条读路径接到本进程自己的元数据库上。

浏览器 BFF 要回答的读接口，与本机应用上那三个 `/api/v1/remote/*` 代理端点
**逐字段相同**——那是「同一份前端零改动可用」的全部依据。要保证相同，最稳的
办法不是照抄一遍映射，而是让两边走同一段代码：`RemoteArchiveFacade` 已经
持有那份映射与游标签名，它要的只是一个 `RemoteArchiveTransport`。

于是这里给它一个不走网络的：请求打进来时，"远端"就是本进程。计划第 3 条允许
用 httpx 的 `ASGITransport` 自连，但 httpx 不在运行时依赖里（`httpx2` 只在 dev
组），为一次进程内自调用把它提成运行时依赖并不值得——直接调服务层同样等价，
还省掉一次序列化。

**设备令牌绝不下发到浏览器**，所以这条路径上压根没有令牌：facade 拿到的
"token" 只是会话自己的游标签名密钥（`BrowserSessionContext.cursor_key`），
既不能认证，也不出这台机器。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Protocol

from chat_archive.ports.metadata import (
    AccountScopedMetadataStore,
    AnchoredMetadataStore,
    BrowsableMetadataStore,
    ConversationDetailMetadataStore,
    RosterMetadataStore,
    ConversationPosition,
    SearchableMetadataStore,
    ConversationSummary,
    MergeAwareMetadataStore,
    MessagePosition,
    MetadataStore,
    RichMetadataStore,
    StoredRecord,
)
from chat_archive.ports.remote_archive import (
    RemoteAccountsPage,
    RemoteSearchPosition,
    RemoteSearchResultPage,
    RemoteAccountSummary,
    RemoteArchiveError,
    RemoteConversationMember,
    RemoteConversationMemberPage,
    RemoteConversationPage,
    RemoteConversationPosition,
    RemoteConversationSummary,
    RemoteBackupConfig,
    RemoteBackupDownload,
    RemoteBackupStatus,
    RemoteBackupVerification,
    RemoteHealth,
    RemoteMessage,
    RemoteMessagePage,
    RemoteMessagePosition,
    RemoteMessageWindow,
    RemoteObjectStorageConfig,
    RemoteObjectStorageVerification,
    RemotePresignedAttachment,
    RemoteRecordPage,
    RemoteShare,
    RemoteVoiceTranscript,
    WritableBinaryStream,
    remote_share_from,
)
from chat_archive.server.http import REMOTE_ERROR_STATUS, RemoteServerError
from chat_archive.server.share_service import ShareService
from chat_archive.server.routes import record_model_version_of
from chat_archive.ports.remote_credentials import (
    DeviceToken,
    RemoteDeviceProfile,
)

# facade 的游标里存了一个 profile 指纹（base_url/archive_id/device_id）。
# 这条路径没有 URL，给一个不可能与真实远端撞车的固定值，指纹的作用
# （换了归档/换了身份的旧游标一律作废）照样成立。
IN_PROCESS_BASE_URL = "in-process://self"

# 这几个码 facade 本来就原样认得（`_OBJECT_STORAGE_ERROR_CODES`），翻成
# HTTP 状态再翻回来只会把「附件在上一代桶里」压成一个泛泛的 409。
_OBJECT_STORAGE_PASSTHROUGH = frozenset(
    {
        "OBJECT_STORAGE_NOT_CONFIGURED",
        "OBJECT_STORAGE_CONFIGURATION_INVALID",
        "OBJECT_STORAGE_VERIFICATION_FAILED",
        "OBJECT_STORAGE_GENERATION_MISMATCH",
    }
)


class PresignAttachment(Protocol):
    """签一个附件直取 URL。由 app 注入，因为对象存储配置归它管。"""

    def __call__(self, archive_id: str, blob_id: str, /) -> RemotePresignedAttachment: ...


class InProcessRemoteArchiveTransport:
    """`RemoteArchiveTransport` 的进程内实现，只认领浏览器读得到的那三个操作。

    其余方法留 `NotImplementedError`：BFF 静态地只路由到那三个，真跑到这里
    说明是代码接错了线，那应该炸得清清楚楚，而不是被翻译成一个像是远端出问题的
    502 让人去查一台并不存在的服务器。
    """

    # 背后是 sqlite 的 MetadataStore，它的线程安全**没有**被核实过——扇出搜索
    # 对这条传输保持串行，等核实了再改这一行。
    supports_concurrent_requests = False

    def __init__(
        self,
        metadata_store: MetadataStore,
        presign: PresignAttachment | None = None,
        shares: ShareService | None = None,
    ) -> None:
        self._metadata = metadata_store
        self._presign = presign
        self._shares = shares

    def record_model_version(self) -> int:
        """由真正要服务这条读的 store 回答，与 `/api/v1/health` 同一个判定。

        不是常量：同一个二进制跑在冻结于 v1 的 store 上时，浏览器这条路径也
        必须只按上传顺序分页，否则会去要一个后端答不出来的键集。
        """
        return record_model_version_of(self._metadata)

    def _account_scoped(self) -> AccountScopedMetadataStore:
        """带账号的读只会到达答过 `record_model_version() >= 3` 的这里。

        facade 先问版本再决定要不要传过滤，那正是同一个 store 的同一个判定；
        真跑进这个分支而 store 不认账号，说明是代码接错了线，assert 炸出来
        比装成远端错误诚实。
        """
        assert isinstance(self._metadata, AccountScopedMetadataStore)
        return self._metadata

    def list_accounts(self, archive_id: str) -> RemoteAccountsPage:
        snapshot = self._account_scoped().list_account_summaries(archive_id)
        return RemoteAccountsPage(
            items=tuple(
                RemoteAccountSummary(
                    source_account_id=item.source_account_id,
                    record_count=item.record_count,
                    display_name=item.display_name,
                    avatar_blob_id=item.avatar_blob_id,
                    merged_source_account_ids=item.merged_source_account_ids,
                )
                for item in snapshot.accounts
            ),
            unattributed_count=snapshot.unattributed_count,
        )

    @staticmethod
    def _conversation(item: ConversationSummary) -> RemoteConversationSummary:
        """One list line, shared by both orderings — a reader must not be able
        to tell which cursor produced a row."""
        return RemoteConversationSummary(
            conversation_id=item.conversation_id,
            message_count=item.message_count,
            last_created_at=item.last_created_at,
            last_server_sequence=item.last_server_sequence,
            display_name=item.display_name,
            is_group=item.is_group,
            source_account_id=item.source_account_id,
            last_message_type=item.last_message_type,
            last_message_preview=item.last_message_preview,
            avatar_blob_id=item.avatar_blob_id,
            announcement=item.announcement,
            my_group_alias=item.my_group_alias,
            member_count=item.member_count,
        )

    @staticmethod
    def _message(record: StoredRecord) -> RemoteMessage:
        return RemoteMessage(
            record_key=record.record_key,
            conversation_id=record.conversation_id,
            created_at=record.created_at,
            content_text=record.content_text,
            content_sha256=record.content_sha256,
            server_sequence=record.server_sequence,
            sender_uid=record.sender_uid,
            sender_display_name=record.sender_display_name,
            is_self=record.is_self,
            message_type=record.message_type,
            app_card_json=record.app_card_json,
            reply_to_key=record.reply_to_key,
            source_sequence=record.source_sequence,
            source_local_id=record.source_local_id,
            attachment_refs=record.attachment_refs,
            source_account_id=record.source_account_id,
            sender_avatar_blob_id=record.sender_avatar_blob_id,
            reply_preview=record.reply_preview,
            transcript=(
                None
                if record.transcript is None
                else RemoteVoiceTranscript(
                    text=record.transcript.text,
                    engine=record.transcript.engine,
                    low_confidence=record.transcript.low_confidence,
                )
            ),
        )

    def _read_conversation(self, archive_id: str, conversation_id: str) -> str:
        """这一次答复是关于哪条会话的（记录模型 v12，PSU-2.95）。

        与 `query_routes._read_conversation` 是同一句话的另一个读面。两边都
        问同一个存储方法，所以两端**不会**在这个键上答得不一样——
        `test_remote_read_parity` 正是为这类"抄两遍然后各自漂移"存在的。
        """
        if not isinstance(self._metadata, MergeAwareMetadataStore):
            return conversation_id
        return self._metadata.read_conversation_id(archive_id, conversation_id)

    def _browsable(self) -> BrowsableMetadataStore:
        """浏览顺序的读只会到达答过 `record_model_version() >= 4` 的这里。

        与 `_account_scoped` 同一个理由：facade 先问版本再决定要不要按浏览
        顺序发问，真跑进这个分支而 store 不认，说明是代码接错了线。
        """
        assert isinstance(self._metadata, BrowsableMetadataStore)
        return self._metadata

    def _searchable(self) -> SearchableMetadataStore:
        """索引化的搜索只会到达答过 `record_model_version() >= 7` 的这里。"""
        assert isinstance(self._metadata, SearchableMetadataStore)
        return self._metadata

    def _anchored(self) -> AnchoredMetadataStore:
        """锚点定位只会到达答过 `record_model_version() >= 9` 的这里。"""
        assert isinstance(self._metadata, AnchoredMetadataStore)
        return self._metadata

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
        page = self._searchable().search_page(
            archive_id,
            query,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            sender_uid=sender_uid,
            message_types=message_types,
            has_attachment=has_attachment,
            date_from=date_from,
            date_to=date_to,
            after=None if after is None else (after.created_at, after.record_key),
            limit=limit,
        )
        spot = page.next_after
        return RemoteSearchResultPage(
            records=tuple(self._message(record) for record in page.records),
            next_position=(
                None
                if spot is None
                else RemoteSearchPosition(created_at=spot[0], record_key=spot[1])
            ),
            has_more=spot is not None,
            total_count=page.total_count,
            conversation_counts=page.conversation_counts,
            index_status=page.index_status,
        )

    def _roster(self) -> RosterMetadataStore:
        """群成员的读只会到达答过 `record_model_version() >= 8` 的这里。

        与 `_browsable` / `_conversation_detail` 同一个理由：facade 先问版本，
        再决定要不要这样发问。
        """
        assert isinstance(self._metadata, RosterMetadataStore)
        return self._metadata

    def list_conversation_members(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
        after_member_uid: str | None = None,
        limit: int = 200,
    ) -> RemoteConversationMemberPage:
        page = self._roster().list_conversation_members(
            archive_id,
            conversation_id,
            source_account_id=source_account_id,
            # 存储那一层用空串表示「从头开始」；传输这一层用 None。两边各自
            # 说得通，翻译只该有这一处。
            after_member_uid=after_member_uid or "",
            limit=limit,
        )
        return RemoteConversationMemberPage(
            members=tuple(
                RemoteConversationMember(
                    member_uid=item.member_uid,
                    display_name=item.display_name,
                    group_alias=item.group_alias,
                    is_self=item.is_self,
                    avatar_blob_id=item.avatar_blob_id,
                )
                for item in page.members
            ),
            next_member_uid=page.next_member_uid,
            has_more=page.has_more,
        )

    def _conversation_detail(self) -> ConversationDetailMetadataStore:
        """会话身份的读只会到达答过 `record_model_version() >= 6` 的这里。

        与 `_browsable` 同一个理由：facade 先问版本再决定要不要这样发问。
        """
        assert isinstance(self._metadata, ConversationDetailMetadataStore)
        return self._metadata

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
        if recent:
            recent_page = self._browsable().list_conversations_by_recency(
                archive_id,
                after=(
                    None
                    if after_position is None
                    else ConversationPosition(
                        last_created_at=after_position.last_created_at,
                        conversation_id=after_position.conversation_id,
                    )
                ),
                limit=limit,
                source_account_id=source_account_id,
                query=query,
            )
            spot = recent_page.next_position
            return RemoteConversationPage(
                conversations=tuple(
                    self._conversation(item) for item in recent_page.conversations
                ),
                next_conversation_id=None,
                has_more=recent_page.has_more,
                next_position=(
                    None
                    if spot is None
                    else RemoteConversationPosition(
                        last_created_at=spot.last_created_at,
                        conversation_id=spot.conversation_id,
                    )
                ),
            )
        if source_account_id is None:
            page = self._metadata.list_conversations(
                archive_id,
                after_conversation_id=after_conversation_id or "",
                limit=limit,
            )
        else:
            page = self._account_scoped().list_conversations(
                archive_id,
                after_conversation_id=after_conversation_id or "",
                limit=limit,
                source_account_id=source_account_id,
            )
        return RemoteConversationPage(
            conversations=tuple(self._conversation(item) for item in page.conversations),
            next_conversation_id=page.next_conversation_id,
            has_more=page.has_more,
        )

    def get_conversation(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> RemoteConversationSummary | None:
        item = self._conversation_detail().get_conversation(
            archive_id,
            conversation_id,
            source_account_id=source_account_id,
        )
        return None if item is None else self._conversation(item)

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
        if not chronological and not latest:
            if source_account_id is None:
                page = self._metadata.list_conversation_messages(
                    archive_id,
                    conversation_id,
                    after_server_sequence=after_server_sequence,
                    limit=limit,
                )
            else:
                page = self._account_scoped().list_conversation_messages(
                    archive_id,
                    conversation_id,
                    after_server_sequence=after_server_sequence,
                    limit=limit,
                    source_account_id=source_account_id,
                )
            return RemoteMessagePage(
                records=tuple(self._message(record) for record in page.records),
                next_server_sequence=page.next_server_sequence,
                has_more=page.has_more,
                read_conversation_id=self._read_conversation(archive_id, conversation_id),
            )
        position_after = (
            None
            if after is None
            else MessagePosition(
                created_at=after.created_at,
                source_sequence=after.source_sequence,
                source_local_id=after.source_local_id,
                record_key=after.record_key,
            )
        )
        if latest:
            time_page = self._browsable().list_conversation_messages_before(
                archive_id,
                conversation_id,
                before=position_after,
                limit=limit,
                source_account_id=source_account_id,
            )
        elif source_account_id is None:
            # 只有认得阅读顺序的 store 才会被问到这里——facade 先读过
            # `record_model_version()`，那正是同一个判定。
            assert isinstance(self._metadata, RichMetadataStore)
            time_page = self._metadata.list_conversation_messages_in_time_order(
                archive_id,
                conversation_id,
                after=position_after,
                limit=limit,
            )
        else:
            time_page = self._account_scoped().list_conversation_messages_in_time_order(
                archive_id,
                conversation_id,
                after=position_after,
                limit=limit,
                source_account_id=source_account_id,
            )
        position = time_page.next_position
        return RemoteMessagePage(
            records=tuple(self._message(record) for record in time_page.records),
            next_server_sequence=after_server_sequence,
            has_more=time_page.has_more,
            next_position=(
                None
                if position is None
                else RemoteMessagePosition(
                    created_at=position.created_at,
                    source_sequence=position.source_sequence,
                    source_local_id=position.source_local_id,
                    record_key=position.record_key,
                )
            ),
            read_conversation_id=self._read_conversation(archive_id, conversation_id),
        )

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
        """与远端那条路由是同一段装配，只是少了一次 HTTP。

        锚点不在这条会话里 → `NOT_FOUND`。**不退回一页最新的**：那会让读者
        以为自己看到的是命中的上下文。
        """
        anchor = self._anchored().get_conversation_message(
            archive_id,
            conversation_id,
            record_key=anchor_record_key,
            source_account_id=source_account_id,
        )
        if anchor is None:
            raise RemoteArchiveError("NOT_FOUND")
        position = MessagePosition(
            created_at=anchor.created_at,
            source_sequence=anchor.source_sequence,
            source_local_id=anchor.source_local_id,
            record_key=anchor.record_key,
        )
        older: tuple[StoredRecord, ...] = ()
        if before > 0:
            older = self._browsable().list_conversation_messages_before(
                archive_id,
                conversation_id,
                before=position,
                limit=before,
                source_account_id=source_account_id,
            ).records
        newer: tuple[StoredRecord, ...] = ()
        if after > 0:
            if source_account_id is None:
                assert isinstance(self._metadata, RichMetadataStore)
                newer = self._metadata.list_conversation_messages_in_time_order(
                    archive_id,
                    conversation_id,
                    after=position,
                    limit=after,
                ).records
            else:
                newer = self._account_scoped().list_conversation_messages_in_time_order(
                    archive_id,
                    conversation_id,
                    after=position,
                    limit=after,
                    source_account_id=source_account_id,
                ).records
        return RemoteMessageWindow(
            records=tuple(
                self._message(record) for record in (*older, anchor, *newer)
            ),
            anchor_record_key=anchor.record_key,
            read_conversation_id=self._read_conversation(archive_id, conversation_id),
        )

    def search(
        self,
        archive_id: str,
        query: str,
        *,
        limit: int = 100,
        source_account_id: str | None = None,
    ) -> tuple[RemoteMessage, ...]:
        if source_account_id is None:
            records = self._metadata.search(archive_id, query, limit=limit)
        else:
            records = self._account_scoped().search(
                archive_id,
                query,
                limit=limit,
                source_account_id=source_account_id,
            )
        return tuple(self._message(record) for record in records)

    # ------------------------------------------------------------------
    # 浏览器这条路径不经过下面这些。对象存储配置仍然只走设备令牌那套接口，
    # 附件字节属 PSU-2.31，同步记录属同步客户端。

    def get_object_storage_config(self, archive_id: str) -> RemoteObjectStorageConfig:
        raise NotImplementedError("browser BFF does not route object storage configuration")

    def test_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageVerification:
        raise NotImplementedError("browser BFF does not route object storage configuration")

    def put_object_storage_config(
        self,
        archive_id: str,
        payload: Mapping[str, str],
    ) -> RemoteObjectStorageConfig:
        raise NotImplementedError("browser BFF does not route object storage configuration")

    def restore_object_storage_generation(
        self,
        archive_id: str,
        storage_generation: int,
    ) -> RemoteObjectStorageConfig:
        raise NotImplementedError("browser BFF does not route object storage configuration")

    # 备份的配置与触发也不经这里（PSU-2.141）：填凭据是已配对设备的事，
    # 浏览器那条只读的近况由 `bff_routes` 直接读账本，不绕这个 transport。

    def get_backup_config(self, archive_id: str) -> RemoteBackupConfig:
        raise NotImplementedError("browser BFF does not route backup configuration")

    def test_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupVerification:
        raise NotImplementedError("browser BFF does not route backup configuration")

    def put_backup_config(
        self,
        archive_id: str,
        payload: Mapping[str, object],
    ) -> RemoteBackupConfig:
        raise NotImplementedError("browser BFF does not route backup configuration")

    def list_backup_runs(self, archive_id: str) -> RemoteBackupStatus:
        raise NotImplementedError("browser BFF reads the backup ledger directly")

    def request_backup_run(self, archive_id: str) -> RemoteBackupStatus:
        raise NotImplementedError("browser BFF does not trigger backups")

    def latest_backup_download(self, archive_id: str) -> RemoteBackupDownload:
        raise NotImplementedError("browser BFF does not hand out backup download links")

    def sync_health(self, archive_id: str) -> RemoteHealth:
        raise NotImplementedError("browser BFF does not route sync health")

    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int = 0,
        limit: int = 100,
    ) -> RemoteRecordPage:
        raise NotImplementedError("browser BFF does not route raw sync records")

    def presign_attachment(
        self,
        archive_id: str,
        blob_id: str,
    ) -> RemotePresignedAttachment:
        """本进程就是那个远端，所以这里直接签，不绕一趟 HTTP 回自己。

        走的仍是 `blob_routes.py` 那一段的同一个 store 与同一条 `remote_ready`
        判定——两端答的是同一份 URL，只是一边多跨了一次网络。
        """
        if self._presign is None:
            raise RemoteArchiveError("OBJECT_STORAGE_NOT_CONFIGURED")
        try:
            return self._presign(archive_id, blob_id)
        except RemoteServerError as error:
            # **翻译成 HTTP 客户端会抛的那一个**，不是原样抛出。本机代理这条路
            # 上，同样的失败先变成一个 HTTP 状态码、再被 facade 映射成公开错误码；
            # 这里少翻一次，两端就会对同一件事给出两个不同的错误码，而前端只写
            # 了一套处理。计划 §6 方案 3 要的「同形」包括失败的形状。
            if error.code in _OBJECT_STORAGE_PASSTHROUGH:
                raise RemoteArchiveError(error.code) from None
            raise RemoteArchiveError(
                f"REMOTE_HTTP_{REMOTE_ERROR_STATUS[error.code]}"
            ) from None

    def download_blob(
        self,
        archive_id: str,
        blob_id: str,
        sink: WritableBinaryStream,
    ) -> None:
        # 附件字节永远不经过 API 进程：浏览器拿 `presign_attachment` 的 URL
        # 直接去对象存储取（`docs/local-remote-boundary.md` 第 4 节）。
        raise NotImplementedError("attachment bytes go direct to object storage")


    # ------------------------------------------------------------------
    # 在线分享（PSU-2.181）。设备令牌那条路由调的是同一个 `ShareService`；
    # 失败照样翻译成 HTTP 客户端会抛的那一个，两端对同一件事给同一个代号。

    def _share_service(self) -> ShareService:
        if self._shares is None:
            raise RemoteArchiveError("REMOTE_HTTP_404")
        return self._shares

    @staticmethod
    def _share_error(error: RemoteServerError) -> RemoteArchiveError:
        if error.code.startswith("SHARE_") or error.code in _OBJECT_STORAGE_PASSTHROUGH:
            return RemoteArchiveError(error.code)
        return RemoteArchiveError(f"REMOTE_HTTP_{REMOTE_ERROR_STATUS[error.code]}")

    def create_share(self, archive_id: str, payload: Mapping[str, object]) -> RemoteShare:
        try:
            return remote_share_from(self._share_service().create(archive_id, dict(payload)))
        except RemoteServerError as error:
            raise self._share_error(error) from None

    def upload_share_object(
        self, archive_id: str, token: str, attachment_id: str, content: bytes
    ) -> None:
        try:
            self._share_service().upload(archive_id, token, attachment_id, content)
        except RemoteServerError as error:
            raise self._share_error(error) from None

    def finalize_share(self, archive_id: str, token: str) -> RemoteShare:
        try:
            return remote_share_from(self._share_service().finalize(archive_id, token))
        except RemoteServerError as error:
            raise self._share_error(error) from None

    def list_shares(self, archive_id: str) -> tuple[RemoteShare, ...]:
        try:
            return tuple(
                remote_share_from(item) for item in self._share_service().list(archive_id)
            )
        except RemoteServerError as error:
            raise self._share_error(error) from None

    def extend_share(self, archive_id: str, token: str) -> RemoteShare:
        try:
            return remote_share_from(self._share_service().extend(archive_id, token))
        except RemoteServerError as error:
            raise self._share_error(error) from None

    def delete_share(self, archive_id: str, token: str) -> None:
        try:
            self._share_service().delete(archive_id, token)
        except RemoteServerError as error:
            raise self._share_error(error) from None


class SessionCredentialStore:
    """一次请求内的假凭据：facade 要一个 `RemoteCredentialStore`，这就是它。

    `DeviceToken` 在这条路径上只被当成游标签名密钥用（`_cursor_key`），
    值来自会话而不是任何设备。会话一撤销，签发过的游标随之失效——这正是想要的。
    """

    def __init__(self, archive_id: str, session_id: str, cursor_key: str) -> None:
        self._profile = RemoteDeviceProfile(
            base_url=IN_PROCESS_BASE_URL,
            archive_id=archive_id,
            device_id=session_id,
        )
        self._token = DeviceToken(value=cursor_key)

    def get(self) -> tuple[RemoteDeviceProfile, DeviceToken] | None:
        return self._profile, self._token

    def put(self, profile: RemoteDeviceProfile, token: DeviceToken) -> None:
        raise NotImplementedError("the browser session is not a credential store")

    def delete(self) -> None:
        raise NotImplementedError("the browser session is not a credential store")


__all__ = [
    "IN_PROCESS_BASE_URL",
    "InProcessRemoteArchiveTransport",
    "SessionCredentialStore",
]

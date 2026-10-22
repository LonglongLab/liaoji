"""远程只读查询端点；逐字段镜像 Worker `queryRoutes.ts`。

PSU-2.30 起，这里比 Worker 多两样：记录的渲染字段，以及**按阅读顺序**分页的
键集游标。PSU-2.35a 再多一样：账号维度——记录与会话带 `source_account_id`，
三个查询端点认可选的账号过滤，另有 `query/accounts`。全部仍是**加法**——
`order` 缺省仍是 `server_sequence`，过滤参数缺省时行为与从前一致，新字段在
老记录上是 null，所以冻结在 v1 的 Worker 不必跟，而客户端靠 `/api/v1/health`
的 `record_model_version` 判断该问哪一种。
"""

# pyright: reportUnusedFunction=false
# FastAPI 路由函数经装饰器注册，pyright 不把装饰器调用视为访问。

from __future__ import annotations

from fastapi import FastAPI, Query, Request

from chat_archive.domain.reply_preview import ReplyPreviewRecord

from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.ports.metadata import (
    AccountScopedMetadataStore,
    AnchoredMetadataStore,
    BrowsableMetadataStore,
    ConversationDetailMetadataStore,
    ConversationPosition,
    ConversationSummary,
    MergeAwareMetadataStore,
    MessagePosition,
    MetadataStore,
    RichMetadataStore,
    RosterMetadataStore,
    SearchableMetadataStore,
    RecordTranscript,
    StoredRecord,
)
from chat_archive.search import normalize_search_text
from chat_archive.server.auth import authenticate_request
from chat_archive.server.http import RemoteServerError

_MAX_PAGE = 500
# 类型过滤在线上是一个逗号分隔的串。用重复的 `type=` 参数也可以，但那要客户端与
# 服务端两边都记住同一种重复语义；一个串只有一种读法。
_MAX_MESSAGE_TYPES = 16


def _message_types(raw: str | None) -> tuple[str, ...] | None:
    if raw is None:
        return None
    values = tuple(dict.fromkeys(item for item in raw.split(",") if item))
    if not values or len(values) > _MAX_MESSAGE_TYPES:
        raise RemoteServerError("QUERY_INVALID", "消息类型过滤无效。")
    return values


def _message(record: StoredRecord) -> dict[str, object]:
    """一条记录的线上形状。

    渲染字段一律显式带上，即使是 null：缺席与 null 在这条链路上是两句不同的话，
    而「远端根本没有这一版模型」由 health 的 `record_model_version` 回答，不由
    某个键在不在这里回答。
    """
    return {
        "record_key": record.record_key,
        "conversation_id": record.conversation_id,
        "created_at": record.created_at,
        "content_text": record.content_text,
        "content_sha256": record.content_sha256,
        "server_sequence": record.server_sequence,
        "sender_uid": record.sender_uid,
        "sender_display_name": record.sender_display_name,
        "is_self": record.is_self,
        "message_type": record.message_type,
        "app_card_json": record.app_card_json,
        "reply_to_key": record.reply_to_key,
        "source_sequence": record.source_sequence,
        "source_local_id": record.source_local_id,
        "attachment_refs": list(record.attachment_refs),
        # null = 这行的归属批次早于账号作用域化，流无法证明——「未归属」，
        # 不是「没有账号」。
        "source_account_id": record.source_account_id,
        # 发送者的头像（记录模型 v8，PSU-2.47）。读时从通讯录接上而不是冻在记录里：
        # 人会换头像，冻在 2019 年那条消息上的那一份会永远是旧的。
        "sender_avatar_blob_id": record.sender_avatar_blob_id,
        # 语音的机器转写（记录模型 v13，PSU-2.163）。**只说远端知道的三样**。
        # 本机那份 `VoiceTranscript` 还有 `status` 和 `failure_code`，这里不补：
        # 远端只收得下转成功的转写，那两个值会是恒定的 `ok`/null——恒定值放进
        # 契约，就是多一个可以和事实不一致的地方。补齐成本机形状是读侧适配层
        # （`remoteReadModel`）的事，那一层本来就为「换形状」而存在。
        "transcript": _transcript(record.transcript),
        "reply_preview": _reply_preview(record.reply_preview),
    }


def _reply_preview(value: ReplyPreviewRecord | None) -> dict[str, object] | None:
    if value is None:
        return None
    return {
        "record_key": value.record_key,
        "sender_display_name": value.sender_display_name,
        "content_text": value.content_text,
        "message_type": value.message_type,
        "attachment_refs": list(value.attachment_refs),
    }


def _transcript(value: RecordTranscript | None) -> dict[str, object] | None:
    """一条机器转写的线上形状，或者 null。

    null 三义合一：这条不是语音、本机还没发过来、或者这个归档里根本没有转写。
    读侧对三者的动作相同，所以不必分。
    """
    if value is None:
        return None
    return {
        "text": value.text,
        "engine": value.engine,
        "low_confidence": value.low_confidence,
    }


def _conversation(item: ConversationSummary) -> dict[str, object]:
    """One conversation's line-item shape, shared by both orderings.

    The two list endpoints differ only in how they are *walked*; a reader must
    not be able to tell which cursor produced a row.
    """
    return {
        "conversation_id": item.conversation_id,
        "message_count": item.message_count,
        "last_created_at": item.last_created_at,
        "last_server_sequence": item.last_server_sequence,
        # null 表示「会话流还没送到这条会话的名字」。界面据此显示
        # 不透明标识并说明原因，而不是拿消息内容凑一个名字出来。
        "display_name": item.display_name,
        "is_group": item.is_group,
        # 不带过滤时：恰好一个账号独占这条会话才填，混合与未归属
        # 都是 null——把合并行派给某一个账号是撒谎。
        "source_account_id": item.source_account_id,
        # 列表行上的那一句（记录模型 v6，PSU-2.44）。两个都是 null 时兼指
        # 「这个后端还没有这一版」与「这条会话没有可预览的消息」——界面两种
        # 都退回条数，本来就是同一句话。预览由归档侧算好，口径与本机档案共用
        # 一份（`domain/preview`）；远端没有附件文件名，所以 `[文件]` 不带名字。
        "last_message_type": item.last_message_type,
        "last_message_preview": item.last_message_preview,
        # 通讯录那一侧才知道的几样（记录模型 v8，PSU-2.47）。全 null 兼指
        # 「这个后端还没有这一版」与「这条会话没有这几样」——界面两种都退回
        # 首字母头像、不画抽屉。`member_count` 只有详情读会去数，列表上是 null。
        "avatar_blob_id": item.avatar_blob_id,
        "announcement": item.announcement,
        "my_group_alias": item.my_group_alias,
        "member_count": item.member_count,
    }


def _read_conversation(
    metadata_store: MetadataStore,
    archive_id: str,
    conversation_id: str,
) -> str:
    """这一次答复是**关于哪条会话**的（记录模型 v12，PSU-2.95）。

    读方法内部本来就走这道翻译（`_as_read_conversation`），但答复里说不出来：
    一页记录能靠行自己表明主语，**一页空记录不能**。而客户端拿老书签深链进来
    时，只有这一句能让它分清"远端把我的老编号读成了 primary"与"远端答错了一条
    会话"——PSU-2.90 真机上，它把前者当成了后者，转录变成一行红字。

    答不出这句话的后端（postgres / D1，连账号维度都没有）返回**问的那个**，
    与它们的实际行为一致：没有链接表就没有翻译。
    """
    if not isinstance(metadata_store, MergeAwareMetadataStore):
        return conversation_id
    return metadata_store.read_conversation_id(archive_id, conversation_id)


def _reading_position(
    created_at: int | None,
    source_sequence: int | None,
    source_local_id: int | None,
    record_key: str | None,
) -> MessagePosition | None:
    """The four-column keyset, or nothing — never a partial one.

    键集游标是一个整体。缺一列就换了一条排序线，页与页之间会重叠或跳空，
    而且两种都不会报错——只能在这里挡住。两个方向共用这一份校验：
    一个游标命名的是排序线上的一个点，与从哪一头走无关。
    """
    cursor = (created_at, source_sequence, source_local_id, record_key)
    if any(item is None for item in cursor) and any(item is not None for item in cursor):
        raise RemoteServerError("QUERY_INVALID", "阅读顺序游标必须四列齐备。")
    if record_key is None:
        return None
    return MessagePosition(
        created_at=created_at or 0,
        source_sequence=source_sequence or 0,
        source_local_id=source_local_id or 0,
        record_key=record_key,
    )


def _browsable(metadata_store: MetadataStore) -> BrowsableMetadataStore:
    """浏览顺序只有 v4 后端答得出来；不达标就当场拒绝，不悄悄换一种顺序。

    与 `order=chronological` 那条同一个理由：把「按最近活跃」降级成「按标识」
    是一个没人看得见的错误——列表照样出得来，只是第一屏是随便一百条会话。
    """
    if isinstance(metadata_store, BrowsableMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持按最近活跃排序。",
    )


def _rich(metadata_store: MetadataStore) -> RichMetadataStore:
    """阅读顺序只有 v2 后端答得出；不达标就当场拒绝。

    与 `query_messages` 里那一处 inline 判定同一句话、同一个理由——把阅读顺序
    悄悄换回上传顺序，是一个没人看得见的错误。
    """
    if isinstance(metadata_store, RichMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持按阅读顺序分页。",
    )


def _anchored(metadata_store: MetadataStore) -> AnchoredMetadataStore:
    """锚点定位只有 v9 后端答得出；不达标就当场拒绝。

    与 `_browsable` 同一个理由。这里没有可降级的东西可选：一个答不出「这条
    记录在哪」的后端，能给的只有「从最新那头开始翻」——那正是读者已经有的，
    也正是他点「定位到上下文」时说自己不要的。
    """
    if isinstance(metadata_store, AnchoredMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持定位到某条消息的上下文。",
    )


def _conversation_detail(metadata_store: MetadataStore) -> ConversationDetailMetadataStore:
    """一条会话的身份只有 v6 后端答得出；不达标就当场拒绝。

    与 `_browsable` 同一个理由。悄悄降级成「你自己去列表里找」正是这个端点
    存在的原因——那条路会让标题取决于读者恰好加载过哪几页。
    """
    if isinstance(metadata_store, ConversationDetailMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持单独读取一条会话。",
    )


def _searchable(metadata_store: MetadataStore) -> SearchableMetadataStore:
    """索引化的搜索只有 v7 后端答得出；不达标就当场拒绝。

    与 `_browsable` 同一个理由，只是更要紧：新增的六个过滤参数发给一个不认得
    它们的后端，会被**忽略**而不是拒绝，于是「按日期筛过的结果」其实一条都没筛
    ——一个没人看得见的错误。
    """
    if isinstance(metadata_store, SearchableMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持带索引与过滤的搜索。",
    )


def _search_position(
    created_at: int | None,
    record_key: str | None,
) -> tuple[int, str] | None:
    """两列一起给或都不给。半个键集会换一条排序线，页与页重叠或跳空。"""
    if (created_at is None) != (record_key is None):
        raise RemoteServerError("QUERY_INVALID", "搜索游标必须两列齐备。")
    if record_key is None:
        return None
    return (created_at or 0, record_key)


def _roster(metadata_store: MetadataStore) -> RosterMetadataStore:
    """通讯录只有 v8 后端存得下、答得出；不达标就当场拒绝。

    与 `_searchable` 同一个理由，而且更要紧：一个存不下 roster 的后端收到一份
    roster，最好的结果是整体拒绝——**不能悄悄收下再丢掉**，那样发送方会以为
    传上去了，而界面永远等不到头像。
    """
    if isinstance(metadata_store, RosterMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持通讯录。",
    )


def _account_scoped(
    metadata_store: MetadataStore,
    source_account_id: str | None,
) -> AccountScopedMetadataStore | None:
    """带账号过滤的请求只有 v3 后端答得出来；不达标就当场拒绝。

    **不能悄悄忽略过滤**：那会把「另一个账号的消息」混进答案里，而这正是
    这个参数存在的全部理由。判定与 health 报 `record_model_version` 的是
    同一个（`record_model_version_of`），所以按版本发问的客户端不会走到这条
    错误。
    """
    if source_account_id is None:
        return None
    if isinstance(metadata_store, AccountScopedMetadataStore):
        return metadata_store
    raise RemoteServerError(
        "QUERY_INVALID",
        "该远端的记录模型不支持按账号过滤。",
    )


def register_query_routes(
    app: FastAPI, auth_store: DeviceAuthStore, metadata_store: MetadataStore
) -> None:
    @app.get("/api/v1/archives/{archive_id}/query/accounts")
    def query_accounts(
        archive_id: str,
        request: Request,
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        if not isinstance(metadata_store, AccountScopedMetadataStore):
            raise RemoteServerError(
                "QUERY_INVALID",
                "该远端的记录模型不支持按账号读取。",
            )
        snapshot = metadata_store.list_account_summaries(archive_id)
        return {
            "items": [
                {
                    "source_account_id": item.source_account_id,
                    "record_count": item.record_count,
                    # null 表示「这个账号的上传流还没送到自己的名字」——
                    # 界面据此显示不透明标识并说明原因，不从记录里凑一个。
                    "display_name": item.display_name,
                    # 同理（v10）：null 表示这条流还没送过头像。界面印首字母。
                    "avatar_blob_id": item.avatar_blob_id,
                    # PSU-2.90：同一个微信的其它上传流 id。空数组而不是 null——
                    # 这里没有「不知道」这一档，一条流要么有同伴要么没有。
                    "merged_source_account_ids": list(item.merged_source_account_ids),
                }
                for item in snapshot.accounts
            ],
            "unattributed_count": snapshot.unattributed_count,
        }

    @app.get("/api/v1/archives/{archive_id}/query/conversations")
    def query_conversations(
        archive_id: str,
        request: Request,
        after_conversation_id: str = Query(default="", max_length=512),
        order: str = Query(default="conversation_id", pattern="^(conversation_id|recent)$"),
        after_last_created_at: int | None = Query(default=None, ge=0),
        q: str | None = Query(default=None, max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        scoped = _account_scoped(metadata_store, source_account_id)
        if order == "recent":
            browsable = _browsable(metadata_store)
            if (after_last_created_at is None) != (after_conversation_id == ""):
                # 键集是一个整体：只给一半就换了一条排序线，页与页之间会重叠
                # 或跳空，而且两种都不报错——只能在这里挡住。
                raise RemoteServerError("QUERY_INVALID", "最近活跃游标必须两列齐备。")
            recent_page = browsable.list_conversations_by_recency(
                archive_id,
                after=(
                    None
                    if after_last_created_at is None
                    else ConversationPosition(
                        last_created_at=after_last_created_at,
                        conversation_id=after_conversation_id,
                    )
                ),
                limit=limit,
                source_account_id=source_account_id,
                query=q,
            )
            position = recent_page.next_position
            return {
                "items": [_conversation(item) for item in recent_page.conversations],
                "next_position": (
                    None
                    if position is None
                    else {
                        "last_created_at": position.last_created_at,
                        "conversation_id": position.conversation_id,
                    }
                ),
                "has_more": recent_page.has_more,
            }
        if q is not None:
            # 过滤只在浏览顺序上定义。默认顺序按标识分页，配上过滤就成了
            # 「按标识排的搜索结果」——没有调用方要这个，也没人测过它。
            raise RemoteServerError("QUERY_INVALID", "会话名过滤要求 order=recent。")
        if scoped is None:
            page = metadata_store.list_conversations(
                archive_id,
                after_conversation_id=after_conversation_id,
                limit=limit,
            )
        else:
            page = scoped.list_conversations(
                archive_id,
                after_conversation_id=after_conversation_id,
                limit=limit,
                source_account_id=source_account_id,
            )
        return {
            "items": [_conversation(item) for item in page.conversations],
            "next_conversation_id": page.next_conversation_id,
            "has_more": page.has_more,
        }

    @app.get("/api/v1/archives/{archive_id}/query/conversations/{conversation_id}")
    def query_conversation(
        archive_id: str,
        conversation_id: str,
        request: Request,
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        """一条会话的身份，不必先在列表里找到它（记录模型 v6，PSU-2.44）。

        答的是与列表**逐字段相同**的那一行——读的人不该分得出标题来自哪一条
        路。这个归档（以及所指账号）下没有这条会话时答 404，与本机
        `/api/v1/conversations/{id}` 同一种答法：编一条空会话出来，会让「还没
        传上去」和「不存在」在界面上长得一模一样。
        """
        authenticate_request(request, archive_id, auth_store)
        _account_scoped(metadata_store, source_account_id)
        item = _conversation_detail(metadata_store).get_conversation(
            archive_id,
            conversation_id,
            source_account_id=source_account_id,
        )
        if item is None:
            raise RemoteServerError("NOT_FOUND", "该归档中没有这条会话。")
        return _conversation(item)

    @app.get("/api/v1/archives/{archive_id}/query/conversations/{conversation_id}/members")
    def query_conversation_members(
        archive_id: str,
        conversation_id: str,
        request: Request,
        after_member_uid: str = Query(default="", max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    ) -> dict[str, object]:
        """一页群成员（记录模型 v8，PSU-2.47）。

        按 `member_uid` 分页而不是按名字：名字会重、会改，一个两者都能走的键集
        会把一些成员走两遍、把另一些跳过去。抽屉自己排它显示的顺序，走查只要
        不重不漏。
        """
        authenticate_request(request, archive_id, auth_store)
        _account_scoped(metadata_store, source_account_id)
        page = _roster(metadata_store).list_conversation_members(
            archive_id,
            conversation_id,
            source_account_id=source_account_id,
            after_member_uid=after_member_uid,
            limit=limit,
        )
        return {
            "items": [
                {
                    "member_uid": item.member_uid,
                    "display_name": item.display_name,
                    "group_alias": item.group_alias,
                    "is_self": item.is_self,
                    "avatar_blob_id": item.avatar_blob_id,
                }
                for item in page.members
            ],
            "next_member_uid": page.next_member_uid,
            "has_more": page.has_more,
        }

    @app.get("/api/v1/archives/{archive_id}/query/conversations/{conversation_id}/messages")
    def query_messages(
        archive_id: str,
        conversation_id: str,
        request: Request,
        after_server_sequence: int = Query(default=0, ge=0),
        order: str = Query(
            default="server_sequence",
            pattern="^(server_sequence|chronological|latest)$",
        ),
        after_created_at: int | None = Query(default=None, ge=0),
        after_source_sequence: int | None = Query(default=None, ge=0),
        after_source_local_id: int | None = Query(default=None, ge=0),
        after_record_key: str | None = Query(default=None, max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        scoped = _account_scoped(metadata_store, source_account_id)
        if order == "latest":
            # 同一条阅读顺序，从另一头走。游标列复用 `after_*`：它命名的是
            # 「排序线上的一个点」，方向由 order 说了算——再开一组 `before_*`
            # 只会多出一组要各自校验、各自出错的参数。
            browsable = _browsable(metadata_store)
            before = _reading_position(
                after_created_at,
                after_source_sequence,
                after_source_local_id,
                after_record_key,
            )
            latest_page = browsable.list_conversation_messages_before(
                archive_id,
                conversation_id,
                before=before,
                limit=limit,
                source_account_id=source_account_id,
            )
            oldest = latest_page.next_position
            return {
                "items": [_message(record) for record in latest_page.records],
                "read_conversation_id": _read_conversation(
                    metadata_store, archive_id, conversation_id
                ),
                "next_position": (
                    None
                    if oldest is None
                    else {
                        "created_at": oldest.created_at,
                        "source_sequence": oldest.source_sequence,
                        "source_local_id": oldest.source_local_id,
                        "record_key": oldest.record_key,
                    }
                ),
                "has_more": latest_page.has_more,
            }
        if order == "server_sequence":
            if scoped is None:
                page = metadata_store.list_conversation_messages(
                    archive_id,
                    conversation_id,
                    after_server_sequence=after_server_sequence,
                    limit=limit,
                )
            else:
                page = scoped.list_conversation_messages(
                    archive_id,
                    conversation_id,
                    after_server_sequence=after_server_sequence,
                    limit=limit,
                    source_account_id=source_account_id,
                )
            return {
                "items": [_message(record) for record in page.records],
                "read_conversation_id": _read_conversation(
                    metadata_store, archive_id, conversation_id
                ),
                "next_server_sequence": page.next_server_sequence,
                "has_more": page.has_more,
            }
        if not isinstance(metadata_store, RichMetadataStore):
            # 一个不认阅读顺序的后端被问了阅读顺序。**不能悄悄改回上传顺序**：
            # 那会把「顺序错了」变成一个没人看得见的错误。
            raise RemoteServerError(
                "QUERY_INVALID",
                "该远端的记录模型不支持按阅读顺序分页。",
            )
        after = _reading_position(
            after_created_at,
            after_source_sequence,
            after_source_local_id,
            after_record_key,
        )
        if scoped is None:
            time_page = metadata_store.list_conversation_messages_in_time_order(
                archive_id,
                conversation_id,
                after=after,
                limit=limit,
            )
        else:
            time_page = scoped.list_conversation_messages_in_time_order(
                archive_id,
                conversation_id,
                after=after,
                limit=limit,
                source_account_id=source_account_id,
            )
        position = time_page.next_position
        return {
            "items": [_message(record) for record in time_page.records],
            "read_conversation_id": _read_conversation(
                metadata_store, archive_id, conversation_id
            ),
            "next_position": (
                None
                if position is None
                else {
                    "created_at": position.created_at,
                    "source_sequence": position.source_sequence,
                    "source_local_id": position.source_local_id,
                    "record_key": position.record_key,
                }
            ),
            "has_more": time_page.has_more,
        }

    @app.get(
        "/api/v1/archives/{archive_id}/query/conversations/{conversation_id}"
        "/messages/window"
    )
    def query_message_window(
        archive_id: str,
        conversation_id: str,
        request: Request,
        anchor_record_key: str = Query(min_length=1, max_length=512),
        before: int = Query(default=20, ge=0, le=_MAX_PAGE),
        after: int = Query(default=20, ge=0, le=_MAX_PAGE),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
    ) -> dict[str, object]:
        """锚点前后的一段，形状与本机 `/messages/window` 逐字相同。

        窗口不是第三种分页：两半各是上面那条路由已经在走的方向，锚点自己
        夹在中间。所以这里没有游标——续读是拿窗口边上那一条当新锚点再问一次，
        与本机那一份同一个走法。
        """
        authenticate_request(request, archive_id, auth_store)
        if before + after + 1 > _MAX_PAGE:
            raise RemoteServerError("QUERY_INVALID", "窗口一次取不了这么多。")
        scoped = _account_scoped(metadata_store, source_account_id)
        anchored = _anchored(metadata_store)
        anchor = anchored.get_conversation_message(
            archive_id,
            conversation_id,
            record_key=anchor_record_key,
            source_account_id=source_account_id,
        )
        if anchor is None:
            # 「这条会话里没有这条记录」。不退回「那就给你最新的一屏」——
            # 那会让读者以为自己看到的是命中的上下文。
            raise RemoteServerError("NOT_FOUND", "这条会话里没有这条记录。")
        position = MessagePosition(
            created_at=anchor.created_at,
            source_sequence=anchor.source_sequence,
            source_local_id=anchor.source_local_id,
            record_key=anchor.record_key,
        )
        browsable = _browsable(metadata_store)
        older = (
            browsable.list_conversation_messages_before(
                archive_id,
                conversation_id,
                before=position,
                limit=before,
                source_account_id=source_account_id,
            ).records
            if before > 0
            else ()
        )
        newer: tuple[StoredRecord, ...] = ()
        if after > 0:
            newer = (
                _rich(metadata_store).list_conversation_messages_in_time_order(
                    archive_id,
                    conversation_id,
                    after=position,
                    limit=after,
                )
                if scoped is None
                else scoped.list_conversation_messages_in_time_order(
                    archive_id,
                    conversation_id,
                    after=position,
                    limit=after,
                    source_account_id=source_account_id,
                )
            ).records
        return {
            "items": [_message(record) for record in (*older, anchor, *newer)],
            "anchor_record_key": anchor.record_key,
            # 独立问一次链接表，**不拿 `anchor.conversation_id` 顶替**：那是从
            # 内容反推声明，一旦读侧取错了会话，声明会跟着错到一处去，客户端
            # 那道对账就白设了。
            "read_conversation_id": _read_conversation(
                metadata_store, archive_id, conversation_id
            ),
        }

    @app.get("/api/v1/archives/{archive_id}/query/search")
    def query_search(
        archive_id: str,
        request: Request,
        q: str = Query(default="", max_length=512),
        source_account_id: str | None = Query(default=None, min_length=1, max_length=128),
        conversation_id: str | None = Query(default=None, max_length=512),
        sender_uid: str | None = Query(default=None, max_length=512),
        message_types: str | None = Query(default=None, max_length=256),
        has_attachment: bool | None = Query(default=None),
        date_from: int | None = Query(default=None, ge=0),
        date_to: int | None = Query(default=None, ge=0),
        after_created_at: int | None = Query(default=None, ge=0),
        after_record_key: str | None = Query(default=None, max_length=512),
        indexed: bool = Query(default=False),
        limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    ) -> dict[str, object]:
        """正文搜索。`indexed=true` 是 v7 的那一种，缺省仍是 v1 起就有的那一种。

        与两个列表端点同一条规矩：新能力是**加法**，缺省行为一个字没动，所以
        冻结在 v1 的后端不必跟，而客户端按 `record_model_version` 决定问哪一种。
        六个过滤参数只在 `indexed=true` 上定义——把它们接在旧那一支上，就成了
        「说了要筛、其实没筛」，正是这一层分层要防的事故。
        """
        authenticate_request(request, archive_id, auth_store)
        scoped = _account_scoped(metadata_store, source_account_id)
        filters = (
            conversation_id,
            sender_uid,
            message_types,
            has_attachment,
            date_from,
            date_to,
            after_record_key,
            after_created_at,
        )
        if not indexed:
            if any(item is not None for item in filters):
                raise RemoteServerError(
                    "QUERY_INVALID",
                    "搜索过滤与游标要求 indexed=true。",
                )
            if normalize_search_text(q).strip() == "":
                return {"items": []}
            if scoped is None:
                records = metadata_store.search(archive_id, q, limit=limit)
            else:
                records = scoped.search(
                    archive_id,
                    q,
                    limit=limit,
                    source_account_id=source_account_id,
                )
            return {"items": [_message(record) for record in records]}
        page = _searchable(metadata_store).search_page(
            archive_id,
            q,
            source_account_id=source_account_id,
            conversation_id=conversation_id,
            sender_uid=sender_uid,
            message_types=_message_types(message_types),
            has_attachment=has_attachment,
            date_from=date_from,
            date_to=date_to,
            after=_search_position(after_created_at, after_record_key),
            limit=limit,
        )
        position = page.next_after
        return {
            "items": [_message(record) for record in page.records],
            "next_position": (
                None
                if position is None
                else {"created_at": position[0], "record_key": position[1]}
            ),
            "has_more": position is not None,
            "total_count": page.total_count,
            "conversation_counts": page.conversation_counts,
            # ready / building / unavailable。**building 必须到得了界面**：
            # 没被告知索引还在建的人，会把「搜不到」读成「没有这条消息」。
            "index_status": page.index_status,
        }

"""同步端点；逐字段镜像 Worker `syncRoutes.ts`。"""

# pyright: reportUnusedFunction=false
# FastAPI 路由函数经装饰器注册，pyright 不把装饰器调用视为访问。

from __future__ import annotations

from typing import cast

from fastapi import FastAPI, Query, Request
from starlette.concurrency import run_in_threadpool

from chat_archive.ports.device_auth import DeviceAuthStore
from chat_archive.ports.metadata import (
    AccountIdentityMetadataStore,
    AccountInput,
    ConflictRecord,
    ConversationInput,
    ConversationMemberInput,
    ConversationProfileInput,
    IdentityInput,
    MetadataStore,
    RecordInput,
    RosterInput,
    RosterMetadataStore,
    TranscriptInput,
    TranscriptMetadataStore,
    TranscriptsInput,
    SyncBatch,
    ForwardMediaPublicationStore,
)
from chat_archive.server.auth import authenticate_request
from chat_archive.server.http import RemoteServerError

_MAX_PAGE = 500
_MAX_BATCH_RECORDS = 500
# An independent cap, not a consequence of the record cap. The client
# deliberately does **not** filter the conversation segment to the
# conversations this batch's records touch (`adapters/sqlite/archive.py`'s
# `_pending_conversations`): a conversation renamed with no new messages
# would then have nothing to travel on. The two limits are equal by choice,
# and the segment may name conversations no record in the batch mentions.
_MAX_BATCH_CONVERSATIONS = 500
# A display name, not a document. The local column is unbounded TEXT, so
# the bound has to be asserted here rather than assumed upstream.
_MAX_ACCOUNT_NAME_LENGTH = 256


def _conflict(entry: ConflictRecord) -> dict[str, object]:
    return {
        "server_sequence": entry.server_sequence,
        "record_key": entry.record_key,
        "canonical_content_sha256": entry.canonical_content_sha256,
        "incoming_content_sha256": entry.incoming_content_sha256,
        "canonical_device_id": entry.canonical_device_id,
        "incoming_device_id": entry.incoming_device_id,
        "canonical_source_instance_id": entry.canonical_source_instance_id,
        "incoming_source_instance_id": entry.incoming_source_instance_id,
        "incoming_schema_version": entry.incoming_schema_version,
        "detected_at": entry.detected_at,
        "reason": entry.reason,
    }


def _optional_string(record: dict[str, object], key: str) -> str | None:
    value = record.get(key)
    if value is None or isinstance(value, str):
        return value
    raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")


def _optional_bool(record: dict[str, object], key: str) -> bool | None:
    value = record.get(key)
    if value is None or isinstance(value, bool):
        return value
    raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")


def _optional_position(record: dict[str, object], key: str) -> int | None:
    value = record.get(key)
    if value is None:
        return None
    # ``bool`` is an ``int`` in Python and would sail straight through an
    # ``isinstance(value, int)`` check, landing True in a position column.
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")
    return value


def _optional_refs(record: dict[str, object], key: str) -> tuple[str, ...] | None:
    value = record.get(key)
    if value is None:
        return None
    if not isinstance(value, list):
        raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")
    items = cast(list[object], value)
    if any(not isinstance(item, str) or not item for item in items):
        raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")
    return tuple(cast(list[str], items))


def _record_input(
    item: object,
    *,
    batch_source_instance_id: str,
    batch_schema_version: str,
) -> RecordInput:
    if not isinstance(item, dict):
        raise RemoteServerError("QUERY_INVALID", "记录格式不正确。")
    record = cast(dict[str, object], item)
    record_key = record.get("record_key")
    conversation_id = record.get("conversation_id")
    created_at = record.get("created_at")
    content_text = record.get("content_text")
    content_sha256 = record.get("content_sha256")
    source_instance_id = record.get("source_instance_id")
    schema_version = record.get("schema_version")
    if (
        not isinstance(record_key, str)
        or not isinstance(conversation_id, str)
        or not isinstance(created_at, int)
        or isinstance(created_at, bool)
        or not isinstance(content_text, str)
        or not isinstance(content_sha256, str)
        or (source_instance_id is not None and not isinstance(source_instance_id, str))
        or (schema_version is not None and not isinstance(schema_version, str))
    ):
        raise RemoteServerError("QUERY_INVALID", "记录字段缺失或类型不正确。")
    if source_instance_id is not None and source_instance_id != batch_source_instance_id:
        raise RemoteServerError("QUERY_INVALID", "记录来源实例与批次不一致。")
    if schema_version is not None and schema_version != batch_schema_version:
        raise RemoteServerError("QUERY_INVALID", "记录 schema 版本与批次不一致。")
    return RecordInput(
        record_key=record_key,
        conversation_id=conversation_id,
        created_at=created_at,
        content_text=content_text,
        content_sha256=content_sha256,
        source_instance_id=source_instance_id,
        schema_version=schema_version,
        sender_uid=_optional_string(record, "sender_uid"),
        sender_display_name=_optional_string(record, "sender_display_name"),
        is_self=_optional_bool(record, "is_self"),
        message_type=_optional_string(record, "message_type"),
        app_card_json=_optional_string(record, "app_card_json"),
        reply_to_key=_optional_string(record, "reply_to_key"),
        source_sequence=_optional_position(record, "source_sequence"),
        source_local_id=_optional_position(record, "source_local_id"),
        attachment_refs=_optional_refs(record, "attachment_refs"),
    )


def _conversation_input(item: object) -> ConversationInput:
    if not isinstance(item, dict):
        raise RemoteServerError("QUERY_INVALID", "会话格式不正确。")
    conversation = cast(dict[str, object], item)
    conversation_id = conversation.get("conversation_id")
    display_name = conversation.get("display_name")
    is_group = conversation.get("is_group")
    updated_at = conversation.get("updated_at")
    if (
        not isinstance(conversation_id, str)
        or not conversation_id
        or not isinstance(display_name, str)
        or not isinstance(is_group, bool)
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
        or updated_at < 0
    ):
        raise RemoteServerError("QUERY_INVALID", "会话字段缺失或类型不正确。")
    # 记录模型 v11。与 `account_key` 同一条口径：缺省/null 都是"没有话说"，
    # 空串则被拒——它会让所有"说不出身份"的会话并成一条。
    conversation_key = _roster_text(
        conversation.get("conversation_key"), required=False, field="conversation_key"
    )
    if conversation_key == "":
        raise RemoteServerError("QUERY_INVALID", "conversation_key 类型或长度不正确。")
    return ConversationInput(
        conversation_id=conversation_id,
        display_name=display_name,
        is_group=is_group,
        updated_at=updated_at,
        conversation_key=conversation_key,
    )


async def _json_body(request: Request) -> dict[str, object]:
    """The request body as a JSON object, or a stable QUERY_INVALID."""
    try:
        parsed = await request.json()
    except ValueError:
        raise RemoteServerError("QUERY_INVALID", "请求体必须是合法 JSON。") from None
    if not isinstance(parsed, dict):
        raise RemoteServerError("QUERY_INVALID", "请求体格式不正确。")
    return cast(dict[str, object], parsed)


# 一个可空文本字段的上限。账号身份与 roster 共用：它们送的是同一批东西
# （名字、备注、blob 标识），两套上限迟早会各走各的。
_MAX_ROSTER_TEXT = 512

# 转写文本自己的上限，**不借 roster 那个**。借来会是一颗定时炸弹：微信语音
# 最长 60 秒，本机现有 3,838 条里最长 328 字，看着离 512 还远——直到哪天一条
# 语速快的录音越线，整片被 QUERY_INVALID 挡回，水位线停在它前面，而界面上
# 只是永远没有那一行字。给它一个与实测量级无关的宽上限。
_MAX_TRANSCRIPT_TEXT = 4096


def _transcripts_input(body: object) -> TranscriptsInput:
    """把请求体读成一片转写，读不通就当场拒绝。

    宁可挡在门口也不要存一行说不清的东西：这张表会直接喂给远端的阅读界面，
    而界面上每一句都会被标成「机器转写」——一行乱码从这里进去，
    出来时看着就像那个人真说过一样。
    """
    if not isinstance(body, dict):
        raise RemoteServerError("QUERY_INVALID", "请求体必须是对象。")
    raw = cast(dict[str, object], body).get("transcripts")
    if not isinstance(raw, list) or not raw:
        raise RemoteServerError("QUERY_INVALID", "transcripts 必须是非空数组。")
    items: list[TranscriptInput] = []
    for entry in cast(list[object], raw):
        if not isinstance(entry, dict):
            raise RemoteServerError("QUERY_INVALID", "转写条目必须是对象。")
        fields = cast(dict[str, object], entry)
        record_key = _roster_text(fields.get("record_key"), required=True, field="record_key")
        text = _bounded_text(
            fields.get("text"), field="text", limit=_MAX_TRANSCRIPT_TEXT
        )
        engine = _roster_text(fields.get("engine"), required=True, field="engine")
        updated_at = fields.get("updated_at")
        low_confidence = fields.get("low_confidence")
        if not isinstance(updated_at, int) or isinstance(updated_at, bool):
            raise RemoteServerError("QUERY_INVALID", "updated_at 必须是整数。")
        if not isinstance(low_confidence, bool):
            raise RemoteServerError("QUERY_INVALID", "low_confidence 必须是布尔。")
        items.append(
            TranscriptInput(
                record_key=str(record_key),
                text=text,
                engine=str(engine),
                low_confidence=low_confidence,
                updated_at=updated_at,
            )
        )
    return TranscriptsInput(transcripts=tuple(items))


def _bounded_text(value: object, *, field: str, limit: int) -> str:
    """一个必填文本字段，长度上限由调用方给。

    空串一并挡掉：远端只收 `ok` 的转写，而 `ok` 的定义就是有文本。放一个空串
    进去，读出来会是一行标着「机器转写」却什么都没有的字。
    """
    if not isinstance(value, str) or not value or len(value) > limit:
        raise RemoteServerError("QUERY_INVALID", f"{field} 类型或长度不正确。")
    return value


def _roster_text(value: object, *, required: bool, field: str) -> str | None:
    """One text field of a published row, or a stable QUERY_INVALID."""
    if value is None:
        if required:
            raise RemoteServerError("QUERY_INVALID", f"{field} 缺失。")
        return None
    if not isinstance(value, str) or len(value) > _MAX_ROSTER_TEXT:
        raise RemoteServerError("QUERY_INVALID", f"{field} 类型或长度不正确。")
    if required and not value:
        raise RemoteServerError("QUERY_INVALID", f"{field} 缺失。")
    return value


def _account_input(item: object) -> AccountInput:
    """The body of a published account identity (record model v5, v10, v11).

    Deliberately does *not* carry an account id: the path already names the
    stream, and a second copy in the body could only ever disagree with it.

    ``avatar_blob_id`` arrives from v10. Absent and null are **not** the same
    thing here even though they store the same value: absent is a sender that
    predates v10 and has nothing to say, null is a v10 sender saying it has no
    picture. Both leave the column empty, so the read side stays honest either
    way; keeping the distinction in the parser is what lets a v10 sender clear
    a picture that is no longer up.
    """
    if not isinstance(item, dict):
        raise RemoteServerError("QUERY_INVALID", "账号身份格式不正确。")
    account = cast(dict[str, object], item)
    display_name = account.get("display_name")
    updated_at = account.get("updated_at")
    if (
        not isinstance(display_name, str)
        or not display_name
        or len(display_name) > _MAX_ACCOUNT_NAME_LENGTH
        or not isinstance(updated_at, int)
        or isinstance(updated_at, bool)
        or updated_at < 0
    ):
        raise RemoteServerError("QUERY_INVALID", "账号字段缺失或类型不正确。")
    avatar_blob_id = _roster_text(
        account.get("avatar_blob_id"), required=False, field="avatar_blob_id"
    )
    # ``account_key`` arrives from v11, under the same absent-vs-null rule.
    # The empty string is neither, and is refused: null says "I cannot work
    # out which WeChat this is", while "" would be a *key* that every silent
    # stream shares — and this key's whole job is to decide which streams are
    # the same account. Merging on it would join strangers.
    account_key = _roster_text(
        account.get("account_key"), required=False, field="account_key"
    )
    if account_key == "":
        raise RemoteServerError("QUERY_INVALID", "account_key 类型或长度不正确。")
    return AccountInput(
        display_name=display_name,
        updated_at=updated_at,
        avatar_blob_id=avatar_blob_id,
        account_key=account_key,
    )


# roster 一片最多多少行。分片是必须的：一个真实账号有几千个联系人、几万条群成员
# 关系，而一次要么全成功要么全失败的发布，就是一次永远不会成功的发布。
_MAX_ROSTER_ROWS = 2_000
def _roster_required(value: object, field: str) -> str:
    text = _roster_text(value, required=True, field=field)
    assert text is not None
    return text


def _roster_updated_at(value: object) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or value < 0:
        raise RemoteServerError("QUERY_INVALID", "行版本缺失或类型不正确。")
    return value


def _roster_rows(body: dict[str, object], key: str) -> list[dict[str, object]]:
    raw = body.get(key)
    if raw is None:
        return []
    if not isinstance(raw, list) or len(cast(list[object], raw)) > _MAX_ROSTER_ROWS:
        raise RemoteServerError("QUERY_INVALID", f"{key} 不是一个合法的分片。")
    rows: list[dict[str, object]] = []
    for item in cast(list[object], raw):
        if not isinstance(item, dict):
            raise RemoteServerError("QUERY_INVALID", f"{key} 里有一行格式不正确。")
        rows.append(cast(dict[str, object], item))
    return rows


def _roster_input(body: dict[str, object]) -> RosterInput:
    """One slice of an account's address book (record model v8).

    Like the account identity above, it deliberately carries no account id: the
    path already names the stream, and a second copy in the body could only
    ever disagree with it.
    """
    identities = tuple(
        IdentityInput(
            uid=_roster_required(row.get("uid"), "uid"),
            display_name=_roster_required(row.get("display_name"), "display_name"),
            remark=_roster_text(row.get("remark"), required=False, field="remark"),
            avatar_blob_id=_roster_text(
                row.get("avatar_blob_id"), required=False, field="avatar_blob_id"
            ),
            updated_at=_roster_updated_at(row.get("updated_at")),
        )
        for row in _roster_rows(body, "identities")
    )
    members = tuple(
        ConversationMemberInput(
            conversation_id=_roster_required(
                row.get("conversation_id"), "conversation_id"
            ),
            member_uid=_roster_required(row.get("member_uid"), "member_uid"),
            display_name=_roster_required(row.get("display_name"), "display_name"),
            group_alias=_roster_text(
                row.get("group_alias"), required=False, field="group_alias"
            ),
            is_self=row.get("is_self") is True,
            updated_at=_roster_updated_at(row.get("updated_at")),
        )
        for row in _roster_rows(body, "members")
    )
    conversations = tuple(
        ConversationProfileInput(
            conversation_id=_roster_required(
                row.get("conversation_id"), "conversation_id"
            ),
            contact_uid=_roster_text(
                row.get("contact_uid"), required=False, field="contact_uid"
            ),
            avatar_blob_id=_roster_text(
                row.get("avatar_blob_id"), required=False, field="avatar_blob_id"
            ),
            announcement=_roster_text(
                row.get("announcement"), required=False, field="announcement"
            ),
            my_group_alias=_roster_text(
                row.get("my_group_alias"), required=False, field="my_group_alias"
            ),
            updated_at=_roster_updated_at(row.get("updated_at")),
        )
        for row in _roster_rows(body, "conversations")
    )
    if not identities and not members and not conversations:
        raise RemoteServerError("QUERY_INVALID", "通讯录分片是空的。")
    return RosterInput(
        identities=identities, members=members, conversations=conversations
    )


def _apply_batch_metadata(
    record: RecordInput,
    *,
    source_instance_id: str,
    schema_version: str,
) -> RecordInput:
    # Only the two batch-level defaults are filled in here. Every v2 field is
    # copied through untouched: the wire checksum was taken over them exactly
    # as sent, and ``validate_batch`` refuses a batch whose effective records
    # differ from the payload in any of them.
    return RecordInput(
        record_key=record.record_key,
        conversation_id=record.conversation_id,
        created_at=record.created_at,
        content_text=record.content_text,
        content_sha256=record.content_sha256,
        source_instance_id=record.source_instance_id or source_instance_id,
        schema_version=record.schema_version or schema_version,
        sender_uid=record.sender_uid,
        sender_display_name=record.sender_display_name,
        is_self=record.is_self,
        message_type=record.message_type,
        app_card_json=record.app_card_json,
        reply_to_key=record.reply_to_key,
        source_sequence=record.source_sequence,
        source_local_id=record.source_local_id,
        attachment_refs=record.attachment_refs,
    )


def register_sync_routes(
    app: FastAPI, auth_store: DeviceAuthStore, metadata_store: MetadataStore
) -> None:
    @app.get("/api/v1/archives/{archive_id}/sync/forward-media-publications/{record_key}")
    def forward_media_publication(
        archive_id: str, record_key: str, request: Request,
        publication_sha256: str = Query(pattern=r"^[0-9a-f]{64}$"),
    ) -> dict[str, object]:
        auth = authenticate_request(request, archive_id, auth_store)
        if not isinstance(metadata_store, ForwardMediaPublicationStore) or not getattr(
            metadata_store, "supports_forward_media_publications", False,
        ):
            raise RemoteServerError("NOT_FOUND", "资源不存在。")
        if len(record_key) > 256:
            raise RemoteServerError("QUERY_INVALID", "记录标识不正确。")
        return {"confirmed": metadata_store.forward_media_publication_confirmed(
            archive_id, record_key, auth.device_id, publication_sha256,
        )}

    @app.get("/api/v1/archives/{archive_id}/sync/conflicts")
    def sync_conflicts(
        archive_id: str,
        request: Request,
        after_server_sequence: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=_MAX_PAGE),
    ) -> dict[str, object]:
        authenticate_request(request, archive_id, auth_store)
        page = metadata_store.list_conflicts(
            archive_id,
            after_server_sequence=after_server_sequence,
            limit=limit,
        )
        return {
            "items": [_conflict(entry) for entry in page.conflicts],
            "next_server_sequence": page.next_server_sequence,
            "has_more": page.has_more,
        }

    @app.put("/api/v1/archives/{archive_id}/accounts/{source_account_id}/identity")
    async def publish_account_identity(
        archive_id: str,
        source_account_id: str,
        request: Request,
    ) -> dict[str, object]:
        """Tell the archive what an upload stream calls itself (v5).

        Its own endpoint rather than a batch section: the archives that most
        need a name are the ones that have finished uploading, and those build
        no batch at all. Idempotent, and ordered by the sender's own row
        version — replaying an older publish must not undo a newer name.
        """
        await run_in_threadpool(authenticate_request, request, archive_id, auth_store)
        if not isinstance(metadata_store, AccountIdentityMetadataStore):
            raise RemoteServerError(
                "QUERY_INVALID",
                "该远端的记录模型不支持记住账号名。",
            )
        if not source_account_id or len(source_account_id) > 128:
            raise RemoteServerError("QUERY_INVALID", "账号标识缺失或过长。")
        account = _account_input(await _json_body(request))
        await run_in_threadpool(
            metadata_store.publish_account_identity,
            archive_id,
            source_account_id,
            account,
        )
        return {"source_account_id": source_account_id, "display_name": account.display_name}

    @app.put("/api/v1/archives/{archive_id}/accounts/{source_account_id}/roster")
    async def publish_roster(
        archive_id: str,
        source_account_id: str,
        request: Request,
    ) -> dict[str, object]:
        """Publish one slice of an account's address book (v8, PSU-2.47).

        Its own endpoint rather than a batch section, for the reason 2.41
        learned the hard way: the archive that most needs a roster is the one
        with nothing left to sync, and that builds no batch at all.

        Sliced, idempotent, and ordered per row by the sender's own version, so
        slices may arrive in any order and a replay is a no-op.
        """
        await run_in_threadpool(authenticate_request, request, archive_id, auth_store)
        if not isinstance(metadata_store, RosterMetadataStore):
            raise RemoteServerError(
                "QUERY_INVALID",
                "该远端的记录模型不支持通讯录。",
            )
        if not source_account_id or len(source_account_id) > 128:
            raise RemoteServerError("QUERY_INVALID", "账号标识缺失或过长。")
        roster = _roster_input(await _json_body(request))
        await run_in_threadpool(
            metadata_store.publish_roster,
            archive_id,
            source_account_id,
            roster,
        )
        return {
            "source_account_id": source_account_id,
            "identity_count": len(roster.identities),
            "member_count": len(roster.members),
            "conversation_count": len(roster.conversations),
        }

    @app.put("/api/v1/archives/{archive_id}/accounts/{source_account_id}/transcripts")
    async def publish_transcripts(
        archive_id: str,
        source_account_id: str,
        request: Request,
    ) -> dict[str, object]:
        """收下一片语音转写（v13，PSU-2.163）。

        自己的端点，理由和通讯录一样：最需要转写的那个档案，往往正是已经没有
        记录可同步的那个，它 build 不出任何批次。

        按发送方自己的 `updated_at` 逐行 last-write-wins，所以片可以乱序到达、
        重放是无操作。
        """
        await run_in_threadpool(authenticate_request, request, archive_id, auth_store)
        if not isinstance(metadata_store, TranscriptMetadataStore):
            raise RemoteServerError(
                "QUERY_INVALID",
                "该远端的记录模型不支持语音转写。",
            )
        if not source_account_id or len(source_account_id) > 128:
            raise RemoteServerError("QUERY_INVALID", "账号标识缺失或过长。")
        transcripts = _transcripts_input(await _json_body(request))
        await run_in_threadpool(
            metadata_store.publish_transcripts,
            archive_id,
            source_account_id,
            transcripts,
        )
        return {
            "source_account_id": source_account_id,
            "transcript_count": len(transcripts.transcripts),
        }

    @app.post("/api/v1/archives/{archive_id}/sync/batches")
    async def sync_commit(archive_id: str, request: Request) -> dict[str, object]:
        # authenticate_request 和 commit_batch 都是同步阻塞调用(SQLite 写事务)；
        # 这是唯一一对没有走 run_in_threadpool 的路由处理器同伴，多设备并发同步
        # 时会卡住整个进程唯一的事件循环。其余 async handler(blob_routes.py、
        # app.py)都统一走 run_in_threadpool，这里补齐保持一致。
        auth = await run_in_threadpool(authenticate_request, request, archive_id, auth_store)
        body = await _json_body(request)
        client_batch_id = body.get("client_batch_id")
        # The stream this window belongs to: a device uploads one outbox
        # sequence per source account, and cursors are scoped by it. Required,
        # because a batch without it can only be checked against a per-device
        # cursor — the model that rejected every multi-account device.
        source_account_id = body.get("source_account_id")
        local_sequence_start = body.get("local_sequence_start")
        local_sequence_end = body.get("local_sequence_end")
        source_instance_id = body.get("source_instance_id")
        schema_version = body.get("schema_version")
        payload_sha256 = body.get("payload_sha256")
        records = body.get("records")
        conversations = body.get("conversations")
        if conversations is not None and not isinstance(conversations, list):
            raise RemoteServerError("QUERY_INVALID", "会话段格式不正确。")
        if (
            not isinstance(client_batch_id, str)
            or not client_batch_id
            or not isinstance(source_account_id, str)
            or not source_account_id
            or not isinstance(local_sequence_start, int)
            or isinstance(local_sequence_start, bool)
            or not isinstance(local_sequence_end, int)
            or isinstance(local_sequence_end, bool)
            or not isinstance(source_instance_id, str)
            or not source_instance_id
            or not isinstance(schema_version, str)
            or not schema_version
            or not isinstance(payload_sha256, str)
            or not isinstance(records, list)
        ):
            raise RemoteServerError(
                "QUERY_INVALID",
                "请求体缺少必要字段或类型不正确。",
            )
        record_items = cast(list[object], records)
        if not record_items or len(record_items) > _MAX_BATCH_RECORDS:
            raise RemoteServerError(
                "QUERY_INVALID",
                f"记录条数必须在 1 到 {_MAX_BATCH_RECORDS} 之间。",
            )
        conversation_items = cast(list[object], conversations or [])
        if len(conversation_items) > _MAX_BATCH_CONVERSATIONS:
            raise RemoteServerError(
                "QUERY_INVALID",
                f"会话条数不能超过 {_MAX_BATCH_CONVERSATIONS}。",
            )
        batch_conversations = tuple(_conversation_input(item) for item in conversation_items)

        raw_records = tuple(
            _record_input(
                item,
                batch_source_instance_id=source_instance_id,
                batch_schema_version=schema_version,
            )
            for item in record_items
        )
        # The wire checksum covers the records exactly as sent. Apply the
        # required batch-level defaults only after that checksum is validated.
        effective_records = tuple(
            _apply_batch_metadata(
                record,
                source_instance_id=source_instance_id,
                schema_version=schema_version,
            )
            for record in raw_records
        )
        batch = SyncBatch(
            archive_id=archive_id,
            device_id=auth.device_id,
            source_account_id=source_account_id,
            client_batch_id=client_batch_id,
            local_sequence_start=local_sequence_start,
            local_sequence_end=local_sequence_end,
            payload_sha256=payload_sha256,
            records=effective_records,
            payload_records=raw_records,
            conversations=batch_conversations,
        )
        receipt = await run_in_threadpool(metadata_store.commit_batch, batch)
        return {
            "client_batch_id": receipt.client_batch_id,
            "server_sequence_start": receipt.server_sequence_start,
            "server_sequence_end": receipt.server_sequence_end,
            "inserted_count": receipt.inserted_count,
            "updated_count": receipt.updated_count,
            "conflict_count": receipt.conflict_count,
        }

"""Read-only local archive query service with opaque cursor pagination."""

from __future__ import annotations

import base64
import binascii
import hashlib
import json
import sqlite3
from collections.abc import Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import cast

from chat_archive.ports.transcription import LOW_CONFIDENCE_BELOW
from chat_archive.domain.app_card import AppCard, card_from_json, card_to_json, safe_forward_avatar
from chat_archive.domain.forward_avatars import bind_avatar_refs, forward_avatar_urls
from chat_archive.domain.errors import ArchiveError, ErrorCode, RecommendedAction
from chat_archive.domain.models import Attachment, AttachmentCacheStatus, AttachmentKind
from chat_archive.domain.preview import conversation_preview as _conversation_preview
from chat_archive.account_identity import (
    SELF_IDENTITY_CANDIDATES_SQL,
    resolve_account_avatar_ref,
    resolve_account_display_name,
)
from chat_archive.search import escape_like
from chat_archive.analysis import AnalysisPage, read_analysis


_MAX_LIMIT = 500
_MAX_STATS_LIMIT = 100
#: 子序列匹配只对**短**输入开（PSU-2.74）。名录里搜的是名字，名字就是短的；
#: 而一个 512 字的输入会把 LIKE 变成几百个通配符的回溯，那不是名录该付的钱。
#: 超过这个长度仍按子串匹配——不是「搜不到」，是「按老规矩搜」。
MAX_FUZZY_QUERY_LENGTH = 16


@dataclass(frozen=True)
class NamePatterns:
    """一次名录检索的三个 LIKE 模式，按匹配得有多准排开。

    子序列是**超集**：`示例软件` 里挑得出「示」「软」，所以「示软」也算命中。
    三档的用处是排序——只有子序列命中的排在子串命中之后，子串又排在前缀之后，
    否则输入一个准确的名字，它会被一堆勉强算命中的名字埋掉。
    """

    prefix: str
    substring: str
    subsequence: str

    @property
    def fuzzy(self) -> bool:
        """子序列这一档开着没有。关着时它与 `substring` 是同一个模式。"""
        return self.subsequence != self.substring


def name_patterns(term: str) -> NamePatterns:
    escaped = escape_like(term)
    subsequence = (
        "%" + "%".join(escape_like(character) for character in term) + "%"
        if 0 < len(term) <= MAX_FUZZY_QUERY_LENGTH
        else f"%{escaped}%"
    )
    return NamePatterns(
        prefix=f"{escaped}%", substring=f"%{escaped}%", subsequence=subsequence
    )


def _match_rank_case(columns: Sequence[str]) -> str:
    """0 = 前缀命中，1 = 子串命中，2 = 只有子序列（或别处）命中。"""
    prefix = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns)
    substring = " OR ".join(f"{column} LIKE ? ESCAPE '\\'" for column in columns)
    return f"CASE WHEN {prefix} THEN 0 WHEN {substring} THEN 1 ELSE 2 END"


_CURSOR_KIND_CONVERSATIONS = "conv"
_CURSOR_KIND_MESSAGES = "msg"
_CURSOR_KIND_MEMBERS = "member"
_CURSOR_KIND_CONTACTS = "contact"

CursorValue = str | int | None


@dataclass(frozen=True)
class AccountView:
    id: str
    display_name: str
    source_label: str | None = None
    avatar_ref: str | None = None


@dataclass(frozen=True)
class BootstrapView:
    accounts: tuple[AccountView, ...]
    active_account_id: str | None
    health: str


@dataclass(frozen=True)
class ConversationView:
    id: str
    display_name: str
    is_group: bool
    last_message_at: int | None
    last_message_type: str | None
    last_message_preview: str | None
    message_count: int
    media_count: int
    avatar_ref: str | None = None


@dataclass(frozen=True)
class ConversationPage:
    items: tuple[ConversationView, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ContactView:
    id: str
    display_name: str
    remark: str | None
    avatar_ref: str | None
    signature: str | None
    source_description: str | None
    added_at: int | None
    is_unknown: bool


@dataclass(frozen=True)
class ConversationDetailView:
    id: str
    display_name: str
    is_group: bool
    last_message_at: int | None
    last_message_type: str | None
    last_message_preview: str | None
    message_count: int
    media_count: int
    announcement: str | None
    my_group_alias: str | None
    member_count: int
    contact: ContactView | None
    avatar_ref: str | None = None


@dataclass(frozen=True)
class ReplyPreviewView:
    message_id: str
    sender_display_name: str | None
    content_text: str
    message_type: str
    # 被引的那条消息自己的附件。微信在引用里只留一行占位文字（`[图片]`、
    # `[语音]`），所以引用条上此前永远只有字——而被引的那条就在同一份档案里，
    # 附件也在。真机 46,108 条引用里 46,080 条指得到原消息，其中 7,687 件带媒体。
    # 与消息自己的 `attachments` 同一种行，故意不另起一种形状：两处要显示的是
    # 同一件东西，界面那一端也就只有一条取字节的路。
    attachments: tuple[AttachmentView, ...] = ()


@dataclass(frozen=True)
class VoiceTranscriptView:
    """机器听出来的那句话。

    **不合并进 `content_text`。** 归档的原始记录不被机器写的东西污染，
    而且界面必须能把它标成「机器转写」——两句话混在一列里，这两件事都做不到。
    """

    text: str
    status: str
    engine: str
    low_confidence: bool
    failure_code: str | None = None


@dataclass(frozen=True)
class MessageView:
    id: str
    conversation_id: str
    sender_uid: str
    sender_display_name: str | None
    created_at: int
    message_type: str
    content_text: str
    is_self: bool
    reply_to_id: str | None
    parse_status: str
    attachments: tuple[AttachmentView, ...]
    reply_preview: ReplyPreviewView | None
    app_card: AppCard | None = None
    sender_avatar_ref: str | None = None
    transcript: VoiceTranscriptView | None = None


@dataclass(frozen=True)
class MessagePage:
    items: tuple[MessageView, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class MessageWindow:
    items: tuple[MessageView, ...]
    anchor_message_id: str


@dataclass(frozen=True)
class AttachmentView:
    id: str
    message_id: str
    kind: str
    file_name: str | None
    mime_type: str | None
    size: int | None
    duration_ms: int | None
    cache_status: str
    failure_code: str | None
    attempt_count: int
    content_available: bool
    # Which stored variant was served. "thumbnail" means the full-size source
    # could not be decoded and a lower-resolution sibling stood in for it; the
    # UI must say so rather than let it pass for the original.
    variant: str | None


@dataclass(frozen=True)
class ConversationMemberView:
    id: str
    display_name: str
    group_alias: str | None
    contact_id: str | None
    is_self: bool
    is_unknown: bool
    avatar_ref: str | None = None


@dataclass(frozen=True)
class ConversationMemberPage:
    items: tuple[ConversationMemberView, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class ContactDirectoryEntry:
    """发送人名录里的一行。

    只有四个字段，因为归档里也只有这些。刻意**不带 remark**：真实归档
    （2,605 行）的 `remark` 全是 NULL，送一个恒空的字段，读的人会把它读成
    「这些人都没设备注」。也刻意**不带命中数**：那要对 729,507 条消息做
    `GROUP BY sender_uid`，实测 21.8 秒。
    """

    contact_uid: str
    display_name: str
    avatar_ref: str | None
    #: False = 归档里没有这个人的名字，`display_name` 就是那串 uid 本身。
    #: 真实归档 2,605 行里有 813 行是这样。界面据此弱化显示，而不是编一个称呼。
    has_display_name: bool


@dataclass(frozen=True)
class ContactDirectoryPage:
    items: tuple[ContactDirectoryEntry, ...]
    next_cursor: str | None


@dataclass(frozen=True)
class AttachmentContentRecord:
    cache_path: str
    mime_type: str | None
    file_name: str | None


def encode_cursor(kind: str, payload: dict[str, CursorValue]) -> str:
    document = json.dumps(
        {"k": kind, **payload},
        ensure_ascii=True,
        sort_keys=True,
        separators=(",", ":"),
    )
    return base64.urlsafe_b64encode(document.encode("ascii")).decode("ascii").rstrip("=")


def decode_cursor(kind: str, value: str) -> dict[str, CursorValue]:
    try:
        padded = value + "=" * (-len(value) % 4)
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ArchiveError(ErrorCode.CURSOR_INVALID) from exc
    if not isinstance(decoded, dict):
        raise ArchiveError(ErrorCode.CURSOR_INVALID)
    document = cast(dict[str, object], decoded)
    if document.get("k") != kind:
        raise ArchiveError(ErrorCode.CURSOR_INVALID)
    payload: dict[str, CursorValue] = {}
    for key, entry in document.items():
        if key == "k":
            continue
        if entry is not None and not isinstance(entry, (str, int)):
            raise ArchiveError(ErrorCode.CURSOR_INVALID)
        payload[key] = entry
    return payload

# The transcript's sort key, in one place because four queries and the cursor
# have to agree on it exactly. `created_at` is only accurate to the second, so
# the last three columns are what decide a shared second:
#
#   source_sequence  WeChat's own `sort_seq`. What the client sorts by, but it
#                    carries sub-second digits on 0.4% of rows, so on its own it
#                    separates only 10% of same-second groups.
#   source_local_id  WeChat's per-conversation insert counter. Unique inside a
#                    conversation, so this is what actually breaks the rest.
#   id               Last resort, and it only ever decides between rows that
#                    tied on all three columns before it -- an opaque sha256
#                    prefix, i.e. a coin flip. A `reorder` pass replaces the
#                    zeros with the real values.
#
# The key is lexicographic, so a row still carrying 0 is separated from a
# positioned neighbour at `source_sequence` and never reaches `id` against it:
# inside a shared second the unpositioned rows come first. Where a whole second
# is unpositioned -- every row until `reorder` runs -- they tie all the way down
# to `id` and read exactly as they did before these columns existed.
MESSAGE_ORDER_COLUMNS = ("created_at", "source_sequence", "source_local_id", "id")
_ORDER_KEY = ", ".join(f"m.{column}" for column in MESSAGE_ORDER_COLUMNS)
_ORDER_BY_DESC = ", ".join(f"m.{column} DESC" for column in MESSAGE_ORDER_COLUMNS)
_ORDER_BY_ASC = ", ".join(f"m.{column} ASC" for column in MESSAGE_ORDER_COLUMNS)

_MessageAnchor = tuple[int, int, int, str]


def _encode_message_anchor(row: sqlite3.Row) -> str:
    return encode_cursor(
        _CURSOR_KIND_MESSAGES,
        {
            "t": int(row["created_at"]),
            "s": int(row["source_sequence"]),
            "l": int(row["source_local_id"]),
            "id": str(row["id"]),
        },
    )


def _decode_message_anchor(cursor: str) -> _MessageAnchor:
    """A cursor has to name a point in the sort key, all four parts of it.

    A cursor issued before the ordering columns existed names only two, and
    there is no honest way to place it in the new order: guessing a sequence
    would silently skip or repeat the rows sharing its second. Rejecting it
    costs one refetch of a page the caller is already looking at.
    """
    payload = decode_cursor(_CURSOR_KIND_MESSAGES, cursor)
    created_at = payload.get("t")
    sequence = payload.get("s")
    local_id = payload.get("l")
    identifier = payload.get("id")
    if (
        not isinstance(created_at, int)
        or not isinstance(sequence, int)
        or not isinstance(local_id, int)
        or not isinstance(identifier, str)
    ):
        raise ArchiveError(ErrorCode.CURSOR_INVALID)
    return created_at, sequence, local_id, identifier



def _require_limit(limit: int) -> None:
    if limit < 1 or limit > _MAX_LIMIT:
        raise ArchiveError(ErrorCode.QUERY_INVALID)


def _not_found() -> ArchiveError:
    return ArchiveError(
        ErrorCode.SOURCE_NOT_FOUND,
        recommended_action=RecommendedAction.RESELECT_SOURCE,
    )


@dataclass(frozen=True)
class DailyStatView:
    day: str
    message_count: int
    media_count: int


@dataclass(frozen=True)
class DailyStatsPage:
    items: tuple[DailyStatView, ...]
    updated_at: int | None


@dataclass(frozen=True)
class ConversationStatView:
    conversation_id: str
    display_name: str
    message_count: int
    media_count: int
    first_message_at: int
    last_message_at: int


@dataclass(frozen=True)
class ConversationStatsPage:
    items: tuple[ConversationStatView, ...]
    updated_at: int | None


def _utc_day(timestamp: int) -> str:
    return datetime.fromtimestamp(timestamp, timezone.utc).date().isoformat()


class ArchiveQueryService:
    """Serve archive reads from short-lived read-only SQLite connections."""

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms

    def _connect(self) -> sqlite3.Connection:
        uri = f"{self._database_path.resolve(strict=False).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: str) -> None:
        row = connection.execute(
            "SELECT 1 FROM source_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise _not_found()

    @staticmethod
    def _require_conversation(
        connection: sqlite3.Connection,
        account_id: str,
        conversation_id: str,
    ) -> sqlite3.Row:
        row = connection.execute(
            """
            SELECT c.id, c.display_name, c.is_group,
                   c.conversation_uid, c.announcement, c.my_group_alias,
                   s.last_message_at, s.message_count, s.media_count,
                   lm.message_type AS last_message_type,
                   lm.content_text AS last_message_content,
                   la.file_name AS last_attachment_file_name,
                   (SELECT COUNT(*) FROM conversation_members AS cm
                    WHERE cm.conversation_id = c.id) AS member_count,
                   ct.avatar_ref AS avatar_ref
            FROM conversations AS c
            LEFT JOIN conversation_stats AS s
              ON s.source_account_id = c.source_account_id AND s.conversation_id = c.id
            LEFT JOIN contacts AS ct
              ON ct.source_account_id = c.source_account_id
             AND ct.contact_uid = c.conversation_uid
            LEFT JOIN messages AS lm
              ON lm.id = (
                SELECT latest.id
                FROM messages AS latest
                WHERE latest.source_account_id = c.source_account_id
                  AND latest.conversation_id = c.id
                ORDER BY latest.created_at DESC, latest.source_sequence DESC,
                         latest.source_local_id DESC, latest.id DESC
                LIMIT 1
              )
            LEFT JOIN attachments AS la
              ON la.id = (
                SELECT first_attachment.id
                FROM attachments AS first_attachment
                WHERE first_attachment.message_id = lm.id
                ORDER BY first_attachment.id ASC
                LIMIT 1
              )
            WHERE c.source_account_id = ? AND c.id = ?
            """,
            (account_id, conversation_id),
        ).fetchone()
        if row is None:
            raise _not_found()
        return row

    @staticmethod
    def _conversation_view(row: sqlite3.Row) -> ConversationView:
        message_type = (
            str(row["last_message_type"])
            if row["last_message_type"] is not None
            else None
        )
        return ConversationView(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            is_group=bool(row["is_group"]),
            last_message_at=(
                int(row["last_message_at"]) if row["last_message_at"] is not None else None
            ),
            last_message_type=message_type,
            last_message_preview=ArchiveQueryService.conversation_preview(
                message_type,
                (
                    str(row["last_message_content"])
                    if row["last_message_content"] is not None
                    else None
                ),
                (
                    str(row["last_attachment_file_name"])
                    if row["last_attachment_file_name"] is not None
                    else None
                ),
            ),
            message_count=int(row["message_count"] or 0),
            media_count=int(row["media_count"] or 0),
            avatar_ref=str(row["avatar_ref"]) if row["avatar_ref"] is not None else None,
        )

    @staticmethod
    def _transcript_view(row: sqlite3.Row) -> VoiceTranscriptView | None:
        """把附件行上 join 回来的转写列拼成视图；没有转写就返回 None。

        `status` 为空即「这段录音没被转过」——和「转过但没听出东西」
        （`empty`）是两回事，界面要分别说，所以这里不把它们揉成一个。
        """
        status = row["transcript_status"] if "transcript_status" in row.keys() else None
        if status is None:
            return None
        confidence = row["transcript_confidence"]
        return VoiceTranscriptView(
            text=str(row["transcript_text"] or ""),
            status=str(status),
            engine=str(row["transcript_engine"] or ""),
            low_confidence=(
                confidence is not None
                and float(confidence) < LOW_CONFIDENCE_BELOW
            ),
            failure_code=(
                str(row["transcript_failure_code"])
                if row["transcript_failure_code"] is not None
                else None
            ),
        )

    @staticmethod
    def _message_view(
        row: sqlite3.Row,
        *,
        attachments: tuple[AttachmentView, ...] = (),
        reply_preview: ReplyPreviewView | None = None,
        transcript: VoiceTranscriptView | None = None,
    ) -> MessageView:
        return MessageView(
            id=str(row["id"]),
            conversation_id=str(row["conversation_id"]),
            sender_uid=str(row["sender_uid"]),
            sender_display_name=(
                str(row["sender_display_name"])
                if row["sender_display_name"] is not None
                else None
            ),
            created_at=int(row["created_at"]),
            message_type=str(row["message_type"]),
            content_text=str(row["content_text"]),
            is_self=bool(row["is_self"]),
            reply_to_id=str(row["reply_to_id"]) if row["reply_to_id"] is not None else None,
            parse_status=str(row["parse_status"]),
            attachments=attachments,
            reply_preview=reply_preview,
            # card_from_json returns None for anything it cannot read, so junk
            # in this column costs the card rather than the message.
            app_card=(
                card_from_json(str(row["app_card_json"]))
                if row["app_card_json"] is not None
                else None
            ),
            sender_avatar_ref=(
                str(row["sender_avatar_ref"])
                if row["sender_avatar_ref"] is not None
                else None
            ),
            transcript=transcript,
        )

    @staticmethod
    def conversation_preview(
        message_type: str | None,
        content_text: str | None,
        attachment_file_name: str | None,
    ) -> str | None:
        """列表行上的那一句。实现搬去了 `domain/preview`——远程端的元数据适配器
        要说的是**同一句**（PSU-2.44），而它不该 import 本机档案的查询服务。
        这里保留同名转调，既有调用方与测试不必知道搬过家。"""
        return _conversation_preview(message_type, content_text, attachment_file_name)

    @staticmethod
    def _contact_view(row: sqlite3.Row) -> ContactView:
        return ContactView(
            id=str(row["id"]),
            display_name=str(row["display_name"]),
            remark=str(row["remark"]) if row["remark"] is not None else None,
            avatar_ref=str(row["avatar_ref"]) if row["avatar_ref"] is not None else None,
            signature=str(row["signature"]) if row["signature"] is not None else None,
            source_description=(
                str(row["source_description"])
                if row["source_description"] is not None
                else None
            ),
            added_at=int(row["added_at"]) if row["added_at"] is not None else None,
            is_unknown=bool(row["is_unknown"]),
        )

    @staticmethod
    def _attachment_view(row: sqlite3.Row) -> AttachmentView:
        status = str(row["cache_status"])
        has_source = (
            row["source_reference"] is not None and str(row["source_reference"]) != ""
        )
        return AttachmentView(
            id=str(row["id"]),
            message_id=str(row["message_id"]),
            kind=AttachmentKind(str(row["kind"])),
            file_name=str(row["file_name"]) if row["file_name"] is not None else None,
            mime_type=str(row["mime_type"]) if row["mime_type"] is not None else None,
            size=int(row["size"]) if row["size"] is not None else None,
            duration_ms=(
                int(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            cache_status=status,
            failure_code=(
                str(row["failure_code"]) if row["failure_code"] is not None else None
            ),
            attempt_count=int(row["attempt_count"]),
            # 「能不能拿到字节」，不是「缓存里有没有」。缓存被配额淘汰是常态，
            # 只要源文件还在就现解一份——这个区别是实现细节，不该泄漏到界面上。
            # 这是乐观值：有 source_reference 不等于源文件还在。取内容那一步若
            # 发现源头也没了，会就地把行标成 source_missing，下一次列表就不再承诺。
            content_available=status == "ready"
            or (has_source and status in {"evicted", "discovered"}),
            variant=str(row["variant"]) if row["variant"] is not None else None,
        )

    def _message_views(
        self,
        connection: sqlite3.Connection,
        rows: list[sqlite3.Row] | tuple[sqlite3.Row, ...],
    ) -> tuple[MessageView, ...]:
        if not rows:
            return ()
        message_ids = tuple(str(row["id"]) for row in rows)
        reply_ids = tuple(
            str(row["reply_to_id"])
            for row in rows
            if row["reply_to_id"] is not None
        )
        # 被引的那条消息一起问：它的附件要挂到引用预览上。同一条查询而不是第二条，
        # 因为被引的那条往往就在这一页里，分两次问会把它的附件取两遍。
        wanted_ids = tuple(dict.fromkeys(message_ids + reply_ids))
        placeholders = ",".join("?" for _ in wanted_ids)
        attachment_rows = connection.execute(
            f"""
            SELECT a.id, a.message_id, a.kind, a.file_name, a.mime_type, a.size,
                   a.duration_ms, a.cache_status, a.failure_code, a.attempt_count,
                   a.variant, a.source_reference,
                   t.text AS transcript_text, t.status AS transcript_status,
                   t.engine AS transcript_engine,
                   t.confidence AS transcript_confidence,
                   t.failure_code AS transcript_failure_code
            FROM attachments AS a
            -- 同一问带回来，不是第二条查询：转写按录音内容作键，而键就在
            -- 这一行的 `sha256` 上，分开问等于把同一批附件再扫一遍。
            LEFT JOIN voice_transcripts AS t ON t.sha256 = a.sha256
            WHERE a.message_id IN ({placeholders})
            ORDER BY a.message_id, a.id
            """,
            wanted_ids,
        ).fetchall()
        attachments_by_message: dict[str, list[AttachmentView]] = {}
        transcripts_by_message: dict[str, VoiceTranscriptView] = {}
        for attachment_row in attachment_rows:
            message_id = str(attachment_row["message_id"])
            attachments_by_message.setdefault(message_id, []).append(
                self._attachment_view(attachment_row)
            )
            transcript = self._transcript_view(attachment_row)
            if transcript is not None:
                # 一条消息只有一段语音，所以第一条就是它的。真出现第二条时
                # 保留先到的那条而不是最后一条，免得顺序一变界面也跟着变。
                transcripts_by_message.setdefault(message_id, transcript)

        previews: dict[str, ReplyPreviewView] = {}
        if reply_ids:
            reply_placeholders = ",".join("?" for _ in reply_ids)
            reply_rows = connection.execute(
                f"""
                SELECT id, sender_display_name, content_text, message_type
                FROM messages WHERE id IN ({reply_placeholders})
                """,
                reply_ids,
            ).fetchall()
            previews = {
                str(reply["id"]): ReplyPreviewView(
                    message_id=str(reply["id"]),
                    sender_display_name=(
                        str(reply["sender_display_name"])
                        if reply["sender_display_name"] is not None
                        else None
                    ),
                    content_text=str(reply["content_text"]),
                    message_type=str(reply["message_type"]),
                    attachments=tuple(attachments_by_message.get(str(reply["id"]), ())),
                )
                for reply in reply_rows
            }
        views = tuple(
            self._message_view(
                row,
                attachments=tuple(attachments_by_message.get(str(row["id"]), ())),
                reply_preview=(
                    previews.get(str(row["reply_to_id"]))
                    if row["reply_to_id"] is not None
                    else None
                ),
                transcript=transcripts_by_message.get(str(row["id"])),
            )
            for row in rows
        )
        # Match only sources needed by this page, in batches; never by display name.
        wanted = {v.id: forward_avatar_urls(card_to_json(v.app_card))
                  for v in views if v.app_card is not None}
        wanted = {key: urls for key, urls in wanted.items() if urls}
        if not wanted:
            return views
        owners = {str(r[0]): str(r[1]) for r in connection.execute(
            "SELECT id,source_account_id FROM messages WHERE id IN ("
            + ",".join("?" for _ in wanted) + ")", tuple(wanted),
        )}
        refs: dict[str, dict[str, tuple[str | None, str | None]]] = {}
        for account_id in set(owners.values()):
            urls = {url for key in wanted if owners.get(key) == account_id for url in wanted[key]}
            sources = sorted(urls | {u.replace("https:", "http:", 1) for u in urls})
            found: dict[str, set[str]] = {}
            for start in range(0, len(sources), 400):
                chunk = sources[start:start + 400]
                for contact in connection.execute(
                    "SELECT avatar_source_url,avatar_ref,id FROM contacts WHERE source_account_id=? "
                    "AND avatar_source_url IN (" + ",".join("?" for _ in chunk) + ")",
                    (account_id, *chunk),
                ):
                    url = safe_forward_avatar(contact[0])
                    local = contact[1]
                    expected = f"/api/v1/avatars/{contact[2]}?account_id={account_id}"
                    if url and isinstance(local, str) and local == expected:
                        found.setdefault(url, set()).add(local)
            refs[account_id] = {u: (sorted(values)[0], None) for u, values in found.items()}
        return tuple(replace(v, app_card=card_from_json(bind_avatar_refs(
            card_to_json(v.app_card), refs.get(owners.get(v.id, ""), {}),
        ))) if v.id in wanted and v.app_card is not None else v for v in views)

    def require_account(self, account_id: str) -> None:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)

    def bootstrap(self) -> BootstrapView:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id, account_uid, display_name FROM source_accounts ORDER BY id"
            ).fetchall()
            accounts: list[AccountView] = []
            for row in rows:
                account_id = str(row["id"])
                account_uid = str(row["account_uid"])
                stored_name = str(row["display_name"])
                # 解析口径住在 `account_identity`，出站批次用的是**同一段**——
                # 远程站点的账号芯片要和这里显示同一个名字（PSU-2.40）。
                candidates = [
                    dict(candidate_row)
                    for candidate_row in connection.execute(
                        SELF_IDENTITY_CANDIDATES_SQL, (account_id,)
                    ).fetchall()
                ]
                resolved_name = resolve_account_display_name(
                    candidates, account_uid=account_uid
                )
                resolved_avatar = resolve_account_avatar_ref(candidates)
                display_name = resolved_name or stored_name
                accounts.append(
                    AccountView(
                        id=account_id,
                        display_name=display_name,
                        source_label=stored_name if display_name != stored_name else None,
                        avatar_ref=resolved_avatar,
                    )
                )
        account_views = tuple(accounts)
        return BootstrapView(
            accounts=account_views,
            active_account_id=account_views[0].id if account_views else None,
            health="ok",
        )

    def conversations_page(
        self,
        account_id: str,
        *,
        cursor: str | None = None,
        limit: int = 50,
        q: str | None = None,
    ) -> ConversationPage:
        _require_limit(limit)
        conditions = ["c.source_account_id = ?"]
        parameters: list[str | int] = [account_id]
        # 名字这几列上排得出「有多准」；群成员那一支排不出，它只回答「里面有这个
        # 人」，所以它命中的会话一律落在最后一档。
        ranked_columns = ("c.display_name", "ct.display_name", "ct.remark")
        ranked = q is not None and q != ""
        rank_expression = "0"
        if ranked:
            patterns = name_patterns(q or "")
            rank_expression = _match_rank_case(ranked_columns)
            # 排序用的两个模式在前，过滤用的子序列在后——参数的顺序就是 SQL 里
            # 出现的顺序，SELECT 列表排在 WHERE 之前。
            parameters = [
                *((patterns.prefix,) * len(ranked_columns)),
                *((patterns.substring,) * len(ranked_columns)),
                account_id,
            ]
            match = " OR ".join(
                f"{column} LIKE ? ESCAPE '\\'" for column in ranked_columns
            )
            conditions.append(
                f"""(
                    {match}
                    OR EXISTS (
                        SELECT 1
                        FROM conversation_members AS cm
                        LEFT JOIN contacts AS member_contact
                          ON member_contact.source_account_id = c.source_account_id
                         AND (
                           member_contact.id = cm.contact_id
                           OR member_contact.contact_uid = cm.member_uid
                         )
                        WHERE cm.conversation_id = c.id
                          AND (
                            cm.display_name LIKE ? ESCAPE '\\'
                            OR cm.group_alias LIKE ? ESCAPE '\\'
                            OR member_contact.display_name LIKE ? ESCAPE '\\'
                            OR member_contact.remark LIKE ? ESCAPE '\\'
                          )
                    )
                )"""
            )
            # 名字这几列按子序列筛（更宽），群成员那一支仍按子串——那是一条相关
            # 子查询，把它也放宽等于为每个会话多扫一遍成员表。
            parameters.extend((patterns.subsequence,) * len(ranked_columns))
            parameters.extend((patterns.substring,) * 4)
        outer_conditions: list[str] = []
        outer_parameters: list[str | int] = []
        if cursor is not None:
            payload = decode_cursor(_CURSOR_KIND_CONVERSATIONS, cursor)
            marker = payload.get("m")
            identifier = payload.get("id")
            if "r" in payload:
                rank = payload["r"]
            elif ranked:
                # 旧游标没有 r，在 rank-first 排序下无法续读——缺省成 0（最优档）
                # 会让第一页给过的 1/2 档行整页重放。宁可让前端重新翻页也不重复
                # 整页；「r 存在且为 0」是合法的前缀档位置，与「缺 r」两回事。
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            else:
                # 浏览（无 q）不分档，旧游标照旧续读。
                rank = 0
            if (
                not isinstance(identifier, str)
                or not (marker is None or isinstance(marker, int))
                or not isinstance(rank, int)
                or isinstance(rank, bool)
            ):
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            if marker is None:
                within = "last_message_at IS NULL AND id > ?"
                position: tuple[str | int, ...] = (identifier,)
            else:
                within = (
                    "(last_message_at < ? OR (last_message_at = ? AND id > ?)"
                    " OR last_message_at IS NULL)"
                )
                position = (marker, marker, identifier)
            # 档位是排序线的第一段，所以游标也从它开始比：同一档里再比时间与标识。
            outer_conditions.append(f"(match_rank > ? OR (match_rank = ? AND {within}))")
            outer_parameters.extend((rank, rank, *position))

        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(
                f"""
                WITH matched AS (
                SELECT c.id, c.display_name, c.is_group,
                       s.last_message_at, s.message_count, s.media_count,
                       lm.message_type AS last_message_type,
                       lm.content_text AS last_message_content,
                       la.file_name AS last_attachment_file_name,
                       ct.avatar_ref AS avatar_ref,
                       {rank_expression} AS match_rank
                FROM conversations AS c
                LEFT JOIN conversation_stats AS s
                  ON s.source_account_id = c.source_account_id AND s.conversation_id = c.id
                LEFT JOIN contacts AS ct
                  ON ct.source_account_id = c.source_account_id
                 AND ct.contact_uid = c.conversation_uid
                LEFT JOIN messages AS lm
                  ON lm.id = (
                    SELECT latest.id
                    FROM messages AS latest
                    WHERE latest.source_account_id = c.source_account_id
                      AND latest.conversation_id = c.id
                    ORDER BY latest.created_at DESC, latest.source_sequence DESC,
                             latest.source_local_id DESC, latest.id DESC
                    LIMIT 1
                  )
                LEFT JOIN attachments AS la
                  ON la.id = (
                    SELECT first_attachment.id
                    FROM attachments AS first_attachment
                    WHERE first_attachment.message_id = lm.id
                    ORDER BY first_attachment.id ASC
                    LIMIT 1
                  )
                WHERE {" AND ".join(conditions)}
                )
                SELECT * FROM matched
                {"WHERE " + " AND ".join(outer_conditions) if outer_conditions else ""}
                ORDER BY match_rank ASC, last_message_at DESC, id ASC
                LIMIT ?
                """,
                (*parameters, *outer_parameters, limit + 1),
            ).fetchall()

        items = tuple(self._conversation_view(row) for row in rows[:limit])
        next_cursor = None
        if len(rows) > limit and items:
            last = items[-1]
            next_cursor = encode_cursor(
                _CURSOR_KIND_CONVERSATIONS,
                {
                    "m": last.last_message_at,
                    "id": last.id,
                    "r": int(rows[limit - 1]["match_rank"]),
                },
            )
        return ConversationPage(items=items, next_cursor=next_cursor)

    def conversation_detail(
        self, account_id: str, conversation_id: str
    ) -> ConversationDetailView:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            row = self._require_conversation(connection, account_id, conversation_id)
            contact = None
            if not bool(row["is_group"]):
                contact_row = connection.execute(
                    """
                    SELECT id, display_name, remark, avatar_ref, signature,
                           source_description, added_at, is_unknown
                    FROM contacts
                    WHERE source_account_id = ? AND contact_uid = ?
                    """,
                    (account_id, str(row["conversation_uid"])),
                ).fetchone()
                if contact_row is not None:
                    contact = self._contact_view(contact_row)
        summary = self._conversation_view(row)
        return ConversationDetailView(
            **summary.__dict__,
            announcement=(
                str(row["announcement"]) if row["announcement"] is not None else None
            ),
            my_group_alias=(
                str(row["my_group_alias"]) if row["my_group_alias"] is not None else None
            ),
            member_count=int(row["member_count"]),
            contact=contact,
        )

    def messages_page(
        self,
        account_id: str,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> MessagePage:
        """Return a chronological slice, starting with the newest messages.

        The first request selects the newest ``limit`` records.  ``next_cursor``
        is anchored to the oldest record in that slice and therefore asks for
        an earlier slice on the next request.  Items remain ascending inside
        each response so the transcript can render them without reordering a
        page; the client reverses page order when it joins multiple pages.
        """
        _require_limit(limit)
        conditions = ["m.source_account_id = ?", "m.conversation_id = ?"]
        parameters: list[str | int] = [account_id, conversation_id]
        if cursor is not None:
            anchor = _decode_message_anchor(cursor)
            conditions.append(f"({_ORDER_KEY}) < (?, ?, ?, ?)")
            parameters.extend(anchor)

        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            self._require_conversation(connection, account_id, conversation_id)
            rows = connection.execute(
                f"""
                SELECT m.id, m.conversation_id, m.sender_uid, m.sender_display_name, m.created_at,
                       m.message_type, m.content_text, m.is_self, m.reply_to_id, m.parse_status,
                       m.app_card_json, m.source_sequence, m.source_local_id,
                       COALESCE(cm.avatar_ref, ct.avatar_ref) AS sender_avatar_ref
                FROM messages AS m
                LEFT JOIN conversation_members AS cm
                  ON cm.conversation_id = m.conversation_id AND cm.member_uid = m.sender_uid
                LEFT JOIN contacts AS ct
                  ON ct.source_account_id = m.source_account_id AND ct.contact_uid = m.sender_uid
                WHERE {" AND ".join(conditions)}
                ORDER BY {_ORDER_BY_DESC}
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()

            # The query is newest-first so the cursor can stop at a stable
            # older boundary.  Keep each response chronological for the
            # renderer and for callers that consume a single page directly.
            items = tuple(reversed(self._message_views(connection, rows[:limit])))
        next_cursor = None
        if len(rows) > limit and items:
            # Read off the row, not the view: the ordering columns are the
            # archive's own bookkeeping and have no business in the DTO the API
            # hands out. rows is newest-first and items is its reverse, so the
            # oldest item of this page is the last row of the slice.
            next_cursor = _encode_message_anchor(rows[limit - 1])
        return MessagePage(items=items, next_cursor=next_cursor)

    def message_window(
        self,
        account_id: str,
        conversation_id: str,
        *,
        anchor_message_id: str,
        before: int = 20,
        after: int = 20,
    ) -> MessageWindow:
        if before < 0 or after < 0 or before + after + 1 > _MAX_LIMIT:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            self._require_conversation(connection, account_id, conversation_id)
            anchor = connection.execute(
                """
                SELECT m.id, m.conversation_id, m.sender_uid, m.sender_display_name, m.created_at,
                       m.message_type, m.content_text, m.is_self, m.reply_to_id, m.parse_status,
                       m.app_card_json, m.source_sequence, m.source_local_id,
                       COALESCE(cm.avatar_ref, ct.avatar_ref) AS sender_avatar_ref
                FROM messages AS m
                LEFT JOIN conversation_members AS cm
                  ON cm.conversation_id = m.conversation_id AND cm.member_uid = m.sender_uid
                LEFT JOIN contacts AS ct
                  ON ct.source_account_id = m.source_account_id AND ct.contact_uid = m.sender_uid
                WHERE m.source_account_id = ? AND m.conversation_id = ? AND m.id = ?
                """,
                (account_id, conversation_id, anchor_message_id),
            ).fetchone()
            if anchor is None:
                raise _not_found()
            anchor_key = (
                int(anchor["created_at"]),
                int(anchor["source_sequence"]),
                int(anchor["source_local_id"]),
                str(anchor["id"]),
            )
            older = connection.execute(
                f"""
                SELECT m.id, m.conversation_id, m.sender_uid, m.sender_display_name, m.created_at,
                       m.message_type, m.content_text, m.is_self, m.reply_to_id, m.parse_status,
                       m.app_card_json, m.source_sequence, m.source_local_id,
                       COALESCE(cm.avatar_ref, ct.avatar_ref) AS sender_avatar_ref
                FROM messages AS m
                LEFT JOIN conversation_members AS cm
                  ON cm.conversation_id = m.conversation_id AND cm.member_uid = m.sender_uid
                LEFT JOIN contacts AS ct
                  ON ct.source_account_id = m.source_account_id AND ct.contact_uid = m.sender_uid
                WHERE m.source_account_id = ? AND m.conversation_id = ?
                  AND ({_ORDER_KEY}) < (?, ?, ?, ?)
                ORDER BY {_ORDER_BY_DESC}
                LIMIT ?
                """,
                (account_id, conversation_id, *anchor_key, before),
            ).fetchall()
            newer = connection.execute(
                f"""
                SELECT m.id, m.conversation_id, m.sender_uid, m.sender_display_name, m.created_at,
                       m.message_type, m.content_text, m.is_self, m.reply_to_id, m.parse_status,
                       m.app_card_json, m.source_sequence, m.source_local_id,
                       COALESCE(cm.avatar_ref, ct.avatar_ref) AS sender_avatar_ref
                FROM messages AS m
                LEFT JOIN conversation_members AS cm
                  ON cm.conversation_id = m.conversation_id AND cm.member_uid = m.sender_uid
                LEFT JOIN contacts AS ct
                  ON ct.source_account_id = m.source_account_id AND ct.contact_uid = m.sender_uid
                WHERE m.source_account_id = ? AND m.conversation_id = ?
                  AND ({_ORDER_KEY}) > (?, ?, ?, ?)
                ORDER BY {_ORDER_BY_ASC}
                LIMIT ?
                """,
                (account_id, conversation_id, *anchor_key, after),
            ).fetchall()

            combined = tuple((*reversed(older), anchor, *newer))
            items = self._message_views(connection, combined)
        return MessageWindow(items=items, anchor_message_id=anchor_message_id)

    def attachment_detail(self, account_id: str, attachment_id: str) -> AttachmentView:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            row = connection.execute(
                """
                SELECT a.id, a.message_id, a.kind, a.file_name, a.mime_type, a.size,
                       a.duration_ms,
                       a.cache_status, a.failure_code, a.attempt_count, a.variant,
                       a.source_reference
                FROM attachments AS a
                JOIN messages AS m ON m.id = a.message_id
                WHERE a.id = ? AND m.source_account_id = ?
                """,
                (attachment_id, account_id),
            ).fetchone()
        if row is None:
            raise _not_found()
        return self._attachment_view(row)

    def attachment_for_processing(self, account_id: str, attachment_id: str) -> Attachment:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            row = connection.execute(
                """
                SELECT a.id, a.message_id, a.kind, a.source_reference, a.file_name,
                       a.mime_type, a.size, a.duration_ms, a.sha256, a.cache_status, a.failure_code,
                       a.cache_path, a.object_key, a.last_accessed_at,
                       a.decoder_version, a.variant, a.thumb_cache_path, a.thumb_size,
                       a.attempt_count
                FROM attachments AS a
                JOIN messages AS m ON m.id = a.message_id
                WHERE a.id = ? AND m.source_account_id = ?
                """,
                (attachment_id, account_id),
            ).fetchone()
        if row is None:
            raise _not_found()
        return Attachment(
            id=str(row["id"]),
            message_id=str(row["message_id"]),
            kind=str(row["kind"]),
            source_reference=(
                str(row["source_reference"])
                if row["source_reference"] is not None
                else None
            ),
            file_name=str(row["file_name"]) if row["file_name"] is not None else None,
            mime_type=str(row["mime_type"]) if row["mime_type"] is not None else None,
            size=int(row["size"]) if row["size"] is not None else None,
            duration_ms=(
                int(row["duration_ms"]) if row["duration_ms"] is not None else None
            ),
            sha256=str(row["sha256"]) if row["sha256"] is not None else None,
            cache_status=AttachmentCacheStatus(str(row["cache_status"])),
            failure_code=(
                str(row["failure_code"]) if row["failure_code"] is not None else None
            ),
            cache_path=str(row["cache_path"]) if row["cache_path"] is not None else None,
            object_key=str(row["object_key"]) if row["object_key"] is not None else None,
            last_accessed_at=(
                int(row["last_accessed_at"])
                if row["last_accessed_at"] is not None
                else None
            ),
            decoder_version=(
                str(row["decoder_version"])
                if row["decoder_version"] is not None
                else None
            ),
            variant=str(row["variant"]) if row["variant"] is not None else None,
            thumb_cache_path=(
                str(row["thumb_cache_path"])
                if row["thumb_cache_path"] is not None
                else None
            ),
            thumb_size=int(row["thumb_size"]) if row["thumb_size"] is not None else None,
            attempt_count=int(row["attempt_count"]),
        )

    def attachment_content(
        self, account_id: str, attachment_id: str
    ) -> AttachmentContentRecord:
        attachment = self.attachment_for_processing(account_id, attachment_id)
        if attachment.cache_status.value != "ready" or attachment.cache_path is None:
            raise ArchiveError(ErrorCode.MEDIA_FAILED, retryable=True)
        return AttachmentContentRecord(
            cache_path=attachment.cache_path,
            mime_type=attachment.mime_type,
            file_name=attachment.file_name,
        )

    def contact_detail(self, account_id: str, contact_id: str) -> ContactView:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            row = connection.execute(
                """
                SELECT id, display_name, remark, avatar_ref, signature,
                       source_description, added_at, is_unknown
                FROM contacts WHERE source_account_id = ? AND id = ?
                """,
                (account_id, contact_id),
            ).fetchone()
        if row is None:
            raise _not_found()
        return self._contact_view(row)

    def contacts_page(
        self,
        account_id: str,
        *,
        q: str | None = None,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ContactDirectoryPage:
        """一页发送人名录。

        数据源是 `contacts` 而不是对 `messages` 做 DISTINCT：后者在真实归档上
        要 17.7 秒，而实测 2,430 个不同 `sender_uid` **100% 都在 contacts 里**
        （缺 0 个），所以这张表就是那份名录。

        按 `(display_name, contact_uid)` 键集分页。**不承诺拼音序**——SQLite
        默认 BINARY collation 下中文是码位序；名录只保证稳定、不重不漏。
        """
        _require_limit(limit)
        conditions = ["source_account_id = ?"]
        parameters: list[str | int] = [account_id]
        rank_expression = "0"
        ranked = q is not None and q != ""
        if ranked:
            patterns = name_patterns(q or "")
            rank_expression = _match_rank_case(("display_name",))
            parameters = [patterns.prefix, patterns.substring, account_id]
            conditions.append("display_name LIKE ? ESCAPE '\\'")
            parameters.append(patterns.subsequence)
        outer_conditions: list[str] = []
        outer_parameters: list[str | int] = []
        if cursor is not None:
            payload = decode_cursor(_CURSOR_KIND_CONTACTS, cursor)
            display_name = payload.get("n")
            contact_uid = payload.get("id")
            if "r" in payload:
                rank = payload["r"]
            elif ranked:
                # 旧游标没有 r，在 rank-first 排序下无法续读——宁可让前端重新
                # 翻页也不重复整页（同会话名录那条的理由）。
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            else:
                rank = 0
            if (
                not isinstance(display_name, str)
                or not isinstance(contact_uid, str)
                or not isinstance(rank, int)
                or isinstance(rank, bool)
            ):
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            # 档位是排序线的第一段，游标从它开始比；同一档里再按名字与标识。
            outer_conditions.append(
                "(match_rank > ? OR (match_rank = ? AND (display_name > ?"
                " OR (display_name = ? AND contact_uid > ?))))"
            )
            outer_parameters.extend(
                (rank, rank, display_name, display_name, contact_uid)
            )
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(
                f"""
                WITH matched AS (
                    SELECT contact_uid, display_name, avatar_ref,
                           {rank_expression} AS match_rank
                    FROM contacts
                    WHERE {" AND ".join(conditions)}
                )
                SELECT * FROM matched
                {"WHERE " + " AND ".join(outer_conditions) if outer_conditions else ""}
                ORDER BY match_rank ASC, display_name ASC, contact_uid ASC
                LIMIT ?
                """,
                (*parameters, *outer_parameters, limit + 1),
            ).fetchall()
        items = tuple(
            ContactDirectoryEntry(
                contact_uid=str(row["contact_uid"]),
                display_name=str(row["display_name"]),
                avatar_ref=(
                    str(row["avatar_ref"]) if row["avatar_ref"] is not None else None
                ),
                has_display_name=str(row["display_name"]) != str(row["contact_uid"]),
            )
            for row in rows[:limit]
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(
                _CURSOR_KIND_CONTACTS,
                {
                    "n": str(last["display_name"]),
                    "id": str(last["contact_uid"]),
                    "r": int(last["match_rank"]),
                },
            )
        return ContactDirectoryPage(items=items, next_cursor=next_cursor)

    def conversation_members_page(
        self,
        account_id: str,
        conversation_id: str,
        *,
        cursor: str | None = None,
        limit: int = 100,
    ) -> ConversationMemberPage:
        _require_limit(limit)
        conditions = ["cm.conversation_id = ?"]
        parameters: list[str | int] = [conversation_id]
        if cursor is not None:
            payload = decode_cursor(_CURSOR_KIND_MEMBERS, cursor)
            display_name = payload.get("n")
            member_uid = payload.get("id")
            if not isinstance(display_name, str) or not isinstance(member_uid, str):
                raise ArchiveError(ErrorCode.CURSOR_INVALID)
            conditions.append(
                "(cm.display_name > ? OR (cm.display_name = ? AND cm.member_uid > ?))"
            )
            parameters.extend((display_name, display_name, member_uid))
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            self._require_conversation(connection, account_id, conversation_id)
            rows = connection.execute(
                f"""
                SELECT cm.member_uid, cm.display_name, cm.group_alias, cm.contact_id,
                       cm.is_self, cm.is_unknown,
                       COALESCE(cm.avatar_ref, ct.avatar_ref) AS avatar_ref
                FROM conversation_members AS cm
                LEFT JOIN contacts AS ct ON ct.id = cm.contact_id
                WHERE {" AND ".join(conditions)}
                ORDER BY cm.display_name ASC, cm.member_uid ASC
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        items = tuple(
            ConversationMemberView(
                id=(
                    "member_"
                    + hashlib.sha256(
                        f"{conversation_id}\x1f{row['member_uid']}".encode("utf-8")
                    ).hexdigest()[:16]
                ),
                display_name=str(row["display_name"]),
                group_alias=(
                    str(row["group_alias"]) if row["group_alias"] is not None else None
                ),
                contact_id=(
                    str(row["contact_id"]) if row["contact_id"] is not None else None
                ),
                is_self=bool(row["is_self"]),
                is_unknown=bool(row["is_unknown"]),
                avatar_ref=(str(row["avatar_ref"]) if row["avatar_ref"] is not None else None),
            )
            for row in rows[:limit]
        )
        next_cursor = None
        if len(rows) > limit:
            last = rows[limit - 1]
            next_cursor = encode_cursor(
                _CURSOR_KIND_MEMBERS,
                {"n": str(last["display_name"]), "id": str(last["member_uid"])},
            )
        return ConversationMemberPage(items=items, next_cursor=next_cursor)

    def analysis_page(
        self, account_id: str, *, date_from: str, date_to: str,
        conversation_kind: str = "all", conversation_id: str | None = None,
    ) -> AnalysisPage:
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            if conversation_id is not None:
                self._require_conversation(connection, account_id, conversation_id)
            return read_analysis(connection, account_id, date_from=date_from, date_to=date_to,
                                 conversation_kind=conversation_kind, conversation_id=conversation_id)

    def daily_stats_page(
        self,
        account_id: str,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
    ) -> DailyStatsPage:
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            clauses = ["source_account_id = ?"]
            params: list[str] = [account_id]
            if date_from is not None:
                clauses.append("day >= ?")
                params.append(_utc_day(date_from))
            if date_to is not None:
                clauses.append("day <= ?")
                params.append(_utc_day(date_to))
            rows = connection.execute(
                f"""
                SELECT day, message_count, media_count, updated_at
                FROM daily_stats
                WHERE {" AND ".join(clauses)}
                ORDER BY day ASC
                """,
                params,
            ).fetchall()
        items = tuple(
            DailyStatView(
                day=str(row["day"]),
                message_count=int(row["message_count"]),
                media_count=int(row["media_count"]),
            )
            for row in rows
        )
        updated_at = max((int(row["updated_at"]) for row in rows), default=None)
        return DailyStatsPage(items=items, updated_at=updated_at)

    def conversation_message_days(
        self,
        account_id: str,
        conversation_id: str,
        *,
        date_from: int | None = None,
        date_to: int | None = None,
        utc_offset_minutes: int = 0,
    ) -> tuple[str, ...]:
        """会话里哪几天有消息，用于日历只点亮真正有记录的日子。

        日界按调用方给的 UTC 偏移切，浏览器传的是自己所在时区，这样日历上的
        「8 月 11 日」和转录里的日期分隔线是同一天。
        """
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        if not -14 * 60 <= utc_offset_minutes <= 14 * 60:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        offset_seconds = utc_offset_minutes * 60
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            self._require_conversation(connection, account_id, conversation_id)
            clauses = ["source_account_id = ?", "conversation_id = ?"]
            params: list[object] = [account_id, conversation_id]
            if date_from is not None:
                clauses.append("created_at >= ?")
                params.append(date_from)
            if date_to is not None:
                clauses.append("created_at <= ?")
                params.append(date_to)
            rows = connection.execute(
                f"""
                SELECT DISTINCT date(created_at + ?, 'unixepoch') AS day
                FROM messages
                WHERE {" AND ".join(clauses)}
                ORDER BY day ASC
                """,
                (offset_seconds, *params),
            ).fetchall()
        return tuple(str(row["day"]) for row in rows)

    def conversation_stats_page(
        self,
        account_id: str,
        *,
        limit: int = 20,
    ) -> ConversationStatsPage:
        if limit < 1 or limit > _MAX_STATS_LIMIT:
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        with closing(self._connect()) as connection:
            self._require_account(connection, account_id)
            rows = connection.execute(
                """
                SELECT s.conversation_id, c.display_name, s.message_count, s.media_count,
                       s.first_message_at, s.last_message_at, s.updated_at
                FROM conversation_stats AS s
                JOIN conversations AS c
                  ON c.source_account_id = s.source_account_id
                 AND c.id = s.conversation_id
                WHERE s.source_account_id = ?
                ORDER BY s.message_count DESC, s.conversation_id ASC
                LIMIT ?
                """,
                (account_id, limit),
            ).fetchall()
        items = tuple(
            ConversationStatView(
                conversation_id=str(row["conversation_id"]),
                display_name=str(row["display_name"]),
                message_count=int(row["message_count"]),
                media_count=int(row["media_count"]),
                first_message_at=int(row["first_message_at"]),
                last_message_at=int(row["last_message_at"]),
            )
            for row in rows
        )
        updated_at = max((int(row["updated_at"]) for row in rows), default=None)
        return ConversationStatsPage(items=items, updated_at=updated_at)

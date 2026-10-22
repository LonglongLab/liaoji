"""SQLite implementation of the vendor-neutral MetadataStore contract.

The same class additionally implements ``PortableArchiveStore`` so a single
archive can be exported to, and imported from, a vendor-neutral migration
bundle. The derived columns (``archive_records.normalized_content`` and the
whole ``archive_stats`` table) are deliberately outside that contract and are
recomputed here on import.

**The portable bundle stays at the v1 record model.** PSU-2.30's render
columns and the whole ``archive_conversations`` table are outside it, because
the bundle's shape is shared with PostgreSQL and D1, which this unit freezes
at v1 — widening it there would either break them or force one of them to
pretend. The consequence is real and bounded: a bundle round-trip carries the
message and drops the render fields, and the target then shows opaque ids and
no sender until a re-sync from the capturing machine refills them, which the
backfill command can do at any time.
"""

from __future__ import annotations

import json
import hashlib
import logging
import sqlite3
import time
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import cast

from chat_archive.domain.identity import thumbnail_blob_id
from chat_archive.domain.preview import conversation_preview
from chat_archive.domain.reply_preview import ReplyPreviewRecord
from chat_archive.remote_search import DEFAULT_BACKFILL_CHUNK, RemoteSearchIndex
from chat_archive.adapters.sqlite.guards import (
    DEFAULT_STATEMENT_ABORT_SECONDS,
    BoundedWriterLock,
    StatementGuardConnection,
    install_statement_guard,
)
from chat_archive.adapters.portable import (
    PORTABLE_BLOB_STATES,
    PORTABLE_COLUMNS,
    PORTABLE_INSERT_COLUMNS,
    PORTABLE_OCCUPIED_TABLES,
    PORTABLE_ROW_TYPES,
    PORTABLE_SORT_COLUMNS,
    PORTABLE_TABLES,
    PortableImportState,
    decode_portable_cursor,
    portable_chunks,
    portable_insert_parameters,
    resume_portable_import,
)
from chat_archive.ports.metadata import (
    ArchiveAccountSummary,
    ArchiveAccountsSnapshot,
    ArchiveStats,
    ArchiveStorageHealth,
    BatchReceipt,
    BlobManifest,
    ConflictPage,
    ConflictRecord,
    AccountInput,
    ConversationInput,
    ConversationPage,
    ConversationPosition,
    ConversationSummary,
    ManifestReconciliation,
    MergeBacklog,
    MessagePage,
    MessagePosition,
    MetadataConflictError,
    MetadataBusyError,
    MetadataUnavailableError,
    MetadataValidationError,
    ConversationMemberPage,
    ConversationMemberSummary,
    ConversationMergeStep,
    WriterLockStatus,
    RecentConversationPage,
    RecordInput,
    RecordPage,
    RecordSearchPage,
    RosterInput,
    TranscriptsInput,
    SequencedRecord,
    StoredIdentity,
    RecordTranscript,
    StoredRecord,
    SyncBatch,
    TimeOrderedMessagePage,
    plan_commit,
    record_metadata_fingerprint,
    validate_batch,
)
from chat_archive.ports.portable import (
    PORTABLE_SECTION_ORDER,
    ImportSummary,
    PortableArchiveRow,
    PortableBatchRow,
    PortableBlobRow,
    PortableConflictError,
    PortableConflictRow,
    PortableCursorRow,
    PortablePage,
    PortableRecordRow,
    PortableRow,
    PortableSection,
    PortableValidationError,
    portable_row_cursor,
)
from chat_archive.search import escape_like, normalize_search_text


# 状态表里那份正文副本存多长。比 `conversation_preview` 最长吐出的 160 长得多
# 是刻意的：预览会先把空白折叠再截断，所以「原文截到多少」必须留出折叠会吃掉
# 的那部分，否则一条前面全是空白的消息在列表上会被截短。存的是**原文**而不是
# 算好的那一句——文案口径将来还会改（比如补上附件文件名），改口径不该要一次迁移。
_STATE_CONTENT_LENGTH = 512

# 状态表的两列派生副本：这一组 (账号, 会话) 里按阅读顺序最后的那条消息的类型与
# 正文。三处维护点（增量、重算、整表物化）都靠这一条 ORDER BY 定义「最后一条」，
# 与转录分页、与 `_apply_conversation_state` 的那次 seek 是**同一条**排序线。
_STATE_PREVIEW_BACKFILL_SQL = f"""
    UPDATE archive_conversation_state
    SET (last_message_type, last_content_text) = (
        SELECT r.message_type, substr(r.content_text, 1, {_STATE_CONTENT_LENGTH})
        FROM archive_records AS r
        WHERE r.archive_id = archive_conversation_state.archive_id
          AND r.conversation_id = archive_conversation_state.conversation_id
          AND COALESCE(r.source_account_id, '')
              = archive_conversation_state.source_account_id
        ORDER BY r.created_at DESC, r.source_sequence DESC,
                 r.source_local_id DESC, r.record_key DESC
        LIMIT 1
    )
"""

# 同一条状态行的预览两列，但只重算一个账号的（PSU-2.86 的归并要用）。
# 由上面那条派生而不是抄一遍：两处「最后一条消息」的定义必须是同一条 ORDER BY，
# 抄写出来的第二份迟早会和转录分页的排序线分家。
_STATE_PREVIEW_BACKFILL_ONE_ACCOUNT_SQL = (
    _STATE_PREVIEW_BACKFILL_SQL
    + "    WHERE archive_id = ? AND source_account_id = ?" + chr(10)
)

# 账号归并要动的读侧表，以及每张表除账号之外的主键列（PSU-2.86）。
#
# 只有 `archive_records` 不在这里：它的主键是 `(archive_id, record_key)`、不含
# 账号，所以「改归属」就是一句 UPDATE。下面这五张的主键都含 `source_account_id`，
# 两台机器必然都有同一批联系人/会话的行，把败者直接 UPDATE 成 primary 会撞唯一
# 约束——所以对它们而言归并是**合并**，规则见 `_merge_account_tables`。
#
# 表名与列名全部来自这个常量、绝不来自调用方数据，与本文件 portable 语句同一条
# 理由：它们因此不需要 `sql.Identifier` 那一层引号。
_ACCOUNT_MERGE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("archive_accounts", ()),
    ("archive_conversations", ("conversation_id",)),
    ("archive_identities", ("uid",)),
    ("archive_conversation_members", ("conversation_id", "member_uid")),
    ("archive_conversation_profiles", ("conversation_id",)),
)

# 记录改写每批多少行，每批一个事务。
#
# 真机上要搬的通常只有几十行（第二台机器刚接进来），但机制是通用的：如果大流的
# id 字典序更大而小流后发布，要搬的就是 73 万行。发布方对这个请求是**吞错误、
# 每轮 sync 重发**的，所以一次几分钟的长事务会变成每轮 sync 都重试一次几分钟的
# 长事务。分批之后，崩溃最多丢一批，下一次发布接着搬——账号身份每轮必发，
# 这条自愈路径本来就在。
_ACCOUNT_MERGE_CHUNK_ROWS = 2_000

# 会话归并要动的三张表，以及每张表除会话之外的主键列（PSU-2.86 B）。
# 与 `_ACCOUNT_MERGE_TABLES` 是同一个形状的第二次应用：只并账号不并会话，
# 会把"两个联系人A"换成"一个联系人A下面两个文件传输助手"，换一个错法不算修好。
_CONVERSATION_MERGE_TABLES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("archive_conversations", ()),
    ("archive_conversation_profiles", ()),
    ("archive_conversation_members", ("member_uid",)),
)

# 一条会话链接「还欠一趟搬家」的判据（PSU-2.86 B，2026-09-02 起按行不按旗）。
#
# 判据不是链接表上的一个旗子，而是**败者 id 下面还有没有东西**：记录（经状态行）、
# 会话名、资料、成员，四处各一次主键前缀查找。旗子（`pending_merge`）在 2026-09-02
# 的真机上失了真：搬完清零之后，那台机器再来一批消息又按旧 id 落地，旗子仍是 0，
# 没有任何东西会再搬第二次，「两个联系人A」就这样在界面上复活。行在哪里是事实，
# 旗子只是对事实的一次记忆；判据要看事实。列留在表里不再读写。
#
# `{account}` 留给调用方：结算按账号进行（`_settle_conversation_merges`），
# 进度问的是整个归档（`merge_backlog`）。参数个数 = `_CONVERSATION_OWED_TABLES` 的长度。
_CONVERSATION_OWED_TABLES: tuple[str, ...] = (
    "archive_conversation_state",
    *(table for table, _ in _CONVERSATION_MERGE_TABLES),
)
_CONVERSATION_MOVE_OWED_SQL = (
    "l.conversation_id <> l.primary_conversation_id AND ("
    + " OR ".join(
        f"EXISTS (SELECT 1 FROM {table} AS t WHERE t.archive_id = l.archive_id "
        "AND t.conversation_id = l.conversation_id{account})"
        for table in _CONVERSATION_OWED_TABLES
    )
    + ")"
)

# 一次问链接表最多带多少个会话 id。一批最多几百条记录、几百个会话，远低于
# SQLite 的变量上限，分片只是为了不把那个上限当成隐含前提。
_LINK_LOOKUP_CHUNK = 500

# 一次问转写表最多带多少个 record_key。同上：一页最多 500 条，分片只是为了
# 不把 SQLite 的变量上限当成隐含前提。
_TRANSCRIPT_LOOKUP_CHUNK = 500

# Import writes land in short transactions of this many rows so no single
# transaction spans a whole section, however large the caller's page is.
_PORTABLE_IMPORT_CHUNK_ROWS = 500
_PORTABLE_SELECT_SQL: dict[PortableSection, str] = {
    section: (
        f"SELECT {', '.join(PORTABLE_COLUMNS[section])} "
        f"FROM {PORTABLE_TABLES[section]} WHERE archive_id = ?"
    )
    for section in PORTABLE_SECTION_ORDER
}
_PORTABLE_INSERT_SQL: dict[PortableSection, str] = {
    section: (
        f"INSERT INTO {PORTABLE_TABLES[section]}"
        f"(archive_id, {', '.join(PORTABLE_INSERT_COLUMNS[section])}) "
        f"VALUES ({', '.join('?' * (len(PORTABLE_INSERT_COLUMNS[section]) + 1))})"
    )
    for section in PORTABLE_SECTION_ORDER
}


def _state_content(value: object) -> str | None:
    """正文进状态表前只做一件事：截断。空白折叠留给读侧那一份口径去做。"""
    if value is None:
        return None
    return str(value)[:_STATE_CONTENT_LENGTH]


_logger = logging.getLogger(__name__)

# 会话名过滤匹配到哪几列（PSU-2.47）。本机匹配 7 列（`query.py:660-687`）：
# 会话名 / 联系人名 / 备注 / 群成员名 / 群昵称 / 成员的联系人名与备注。这一端
# 匹配同样的 7 列，外加**不透明标识**——名字还没送到时列表显示的就是它，
# 而「看得见的行必须找得到」。
# 单聊对面那个人的名字与备注怎么匹配：两种写法、同一对占位符、同一个次序。
# 带账号过滤时 `ci` 是主键直取的一行，直接联上即可；不带过滤时同一个 uid 在每个
# 账号下各有一行，**联进来会把状态行乘上账号数**——而外面正在 `SUM(message_count)`，
# 真机上就这样把 3 / 32 / 46 报成了 6 / 64 / 92。所以那一支改用 EXISTS 问
# 「有没有一行像」，行数一个都不多。
_CONTACT_MATCH_JOINED_SQL = """ci.display_name LIKE ? ESCAPE '\\'
    OR ci.remark LIKE ? ESCAPE '\\'"""
_CONTACT_MATCH_EXISTS_SQL = """EXISTS (
        SELECT 1
        FROM archive_identities AS ci
        WHERE ci.archive_id = s.archive_id
          AND ci.uid = pf.contact_uid
          AND (ci.display_name LIKE ? ESCAPE '\\' OR ci.remark LIKE ? ESCAPE '\\')
    )"""
_NAME_FILTER_SQL = """(
    c.display_name LIKE ? ESCAPE '\\'
    OR s.conversation_id LIKE ? ESCAPE '\\'
    OR {contact}
    OR EXISTS (
        SELECT 1
        FROM archive_conversation_members AS m
        LEFT JOIN archive_identities AS mi
          ON mi.archive_id = m.archive_id
         AND mi.source_account_id = m.source_account_id
         AND mi.uid = m.member_uid
        WHERE m.archive_id = s.archive_id
          AND m.conversation_id = s.conversation_id
          {scope}
          AND (
            m.display_name LIKE ? ESCAPE '\\'
            OR m.group_alias LIKE ? ESCAPE '\\'
            OR mi.display_name LIKE ? ESCAPE '\\'
            OR mi.remark LIKE ? ESCAPE '\\'
          )
    )
)"""
_NAME_FILTER_PATTERNS = 8



# 两个 `account_key` 要多像才值得报一声「这两条流本该并起来」。裸 wxid 一律以
# `wxid_` 开头（5 个字符），所以门槛定在 8：再多三个字符相同的巧合约 1/46656，
# 而两台机器为同一个微信选出的不同 outgoing identity 通常只差结尾。
_ACCOUNT_KEY_NEAR_MISS_PREFIX = 8


def _looks_like_the_same_identity(left: str, right: str) -> bool:
    """ "像但不等"：大小写不同、一个是另一个的前缀，或公共前缀够长。"""
    if left == right:
        return False
    if left.casefold() == right.casefold():
        return True
    if left.startswith(right) or right.startswith(left):
        return True
    shared = 0
    for a, b in zip(left, right):
        if a != b:
            break
        shared += 1
    return shared >= _ACCOUNT_KEY_NEAR_MISS_PREFIX


def _optional_text(value: object) -> str | None:
    return None if value is None else str(value)


def _encode_attachment_refs(refs: tuple[str, ...] | None) -> str | None:
    """Store the blob ids as a JSON array, or NULL when the sender said nothing.

    ``NULL`` and ``"[]"`` are different answers — "never told" against "told
    us there are none" — and the read model keeps them apart.
    """
    return None if refs is None else json.dumps(list(refs), separators=(",", ":"))


def _decode_attachment_refs(value: object) -> tuple[str, ...]:
    if value is None:
        return ()
    try:
        decoded = json.loads(str(value))
    except json.JSONDecodeError:
        return ()
    if not isinstance(decoded, list):
        return ()
    return tuple(item for item in cast(list[object], decoded) if isinstance(item, str))


# (archive_id, blob_ids) -> {blob_id: state}. Injected rather than looked up,
# because manifests live in the blob state store — a different component and a
# different SQLite file (PSU-2.82).
BlobManifestStates = Callable[[str, Sequence[str]], Mapping[str, str]]
ForwardMediaManifests = Callable[[str, Sequence[str]], Mapping[str, BlobManifest]]
# (archive_id, blob_ids) -> {blob_id: size} of the verified ones (PSU-2.143).
# Only asked when two single-ref records tie on reachability.
BlobManifestSizes = Callable[[str, Sequence[str]], Mapping[str, int]]

# What a ref list can actually open: how many originals and how many
# thumbnails are verified on the remote. Counted by *kind* rather than by id,
# and that is the crux of the merge.
#
# Two machines capturing one WeChat account derive different blob ids for the
# same picture — the id is salted by the account, the account by the capture
# machine — so comparing id sets says "incomparable" for every single message
# and would turn the normal case into a ledger full of conflicts. What the two
# lists can be compared on is how much of the picture each one reaches.
ReachableArtifacts = tuple[int, int]

# ``attachment_refs`` is no longer written: refs divergences are resolved by
# ``merge_attachment_refs``. The name stays because older ledgers still hold
# rows with this reason, and they are history, not something to rewrite.
REFS_CONFLICT_REASON = "attachment_refs"
CONTENT_CONFLICT_REASON = "content"

_RECORD_CONFLICT_INSERT = """
    INSERT INTO record_conflicts(
        archive_id, server_sequence, record_key,
        canonical_content_sha256, incoming_content_sha256,
        canonical_source_instance_id, incoming_source_instance_id,
        incoming_schema_version, canonical_device_id,
        incoming_device_id, detected_at, reason
    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def reachable_artifacts(
    refs: Sequence[str],
    ready: Callable[[str], bool],
) -> ReachableArtifacts:
    """``(originals, thumbnails)`` this ref list can open on the remote.

    A ref ``R`` reaches the original when ``manifest(R)`` is verified, and the
    thumbnail when ``manifest(thumbnail_blob_id(R))`` is — the derived address
    is how a reader asks for a thumbnail at all, so it is also how one is
    counted here.
    """
    originals = sum(1 for ref in refs if ready(ref))
    thumbnails = sum(1 for ref in refs if ready(thumbnail_blob_id(ref)))
    return originals, thumbnails


@dataclass(frozen=True)
class AttachmentRefsMerge:
    """What to store for one record's refs.

    ``conflict`` is always ``False`` and stays only so the shape callers and
    tests read did not change: since the refs merge started resolving every
    case by itself there is nothing left to write down for a person.
    """

    refs: str | None
    conflict: bool = False


def merge_attachment_refs(
    *,
    incoming: str | None,
    canonical: str | None,
    ready: Callable[[str], bool],
    size: Callable[[str], int | None] | None = None,
) -> AttachmentRefsMerge:
    """Take the incoming refs only when they do not lose anything (PSU-2.82).

    Until PSU-2.82 the column was overwritten unconditionally, and with two
    machines uploading one account that quietly *destroyed* reachability: each
    machine publishes refs naming its own blob ids, so whichever synced last
    replaced the other's — and the picture the first machine had actually
    uploaded stopped being referenced by anything.

    The outcomes, all decided here and **none of them recorded**:

    - **incoming is NULL** — the sender carries no opinion about attachments
      (a v1 sender, or one that never uploaded). Keep what is there. NULL and
      ``[]`` are different answers everywhere else in this file and they are
      different here too.
    - **identical** — nothing to decide.
    - otherwise compare the ``(originals, thumbnails)`` each list reaches
      **lexicographically**: more originals wins; with equal originals, more
      thumbnails wins. When incoming wins, take it — the work laptop supplies
      the original the home machine never had. When canonical wins, keep it
      quietly; this includes every new machine's first sync, whose refs point
      at blobs not uploaded yet.
    - **exact tie** — keep canonical, except for the PSU-2.143 tie-break
      below.

    The lexicographic order replaces what used to be the one recorded case,
    "neither reaches what the other does" (e.g. one side opens the original,
    the other only a thumbnail of a different copy). A real archive's ledger
    held tens of thousands of those and not one needed a person: the message
    text was identical on both sides, the two lists merely reached different
    copies of the same picture, and a large share of them were the *same*
    device disagreeing with an earlier upload of its own — this merge never
    looks at the device. The system can pick the better copy by itself, so it
    does, and a choice it made itself is not presented as needing a person.
    The original outranks the thumbnail because it is the artifact that
    cannot be regenerated from the other.

    One tie-break on top (PSU-2.143): when both lists are a single ref, both
    reach the same artifacts and both reach an original, the **strictly
    larger** original wins. The uploader sends its copy only when it is larger
    than what the remote already holds for the message; without this the
    larger copy would land and nothing would point at it. Unknown sizes, or
    more than one ref on either side, keep the canonical row.
    """
    if incoming is None:
        return AttachmentRefsMerge(canonical)
    if canonical is None or incoming == canonical:
        return AttachmentRefsMerge(incoming)
    incoming_refs = _decode_attachment_refs(incoming)
    canonical_refs = _decode_attachment_refs(canonical)
    if incoming_refs == canonical_refs:
        return AttachmentRefsMerge(incoming)
    mine = reachable_artifacts(incoming_refs, ready)
    theirs = reachable_artifacts(canonical_refs, ready)
    if mine > theirs:
        return AttachmentRefsMerge(incoming)
    if (
        mine == theirs
        and size is not None
        and len(incoming_refs) == 1
        and len(canonical_refs) == 1
        and ready(incoming_refs[0])
        and ready(canonical_refs[0])
    ):
        incoming_size = size(incoming_refs[0])
        canonical_size = size(canonical_refs[0])
        if (
            incoming_size is not None
            and canonical_size is not None
            and incoming_size > canonical_size
        ):
            return AttachmentRefsMerge(incoming)
    return AttachmentRefsMerge(canonical)


@dataclass(frozen=True)
class _MergedRefs:
    """Per record_key: what to store."""

    refs: dict[str, str | None]


def attachment_refs_blob_ids(encoded: str | None) -> tuple[str, ...]:
    """Every manifest id a merge decision about ``encoded`` would have to ask about."""
    refs = _decode_attachment_refs(encoded) if encoded is not None else ()
    return tuple(refs) + tuple(thumbnail_blob_id(ref) for ref in refs)


@dataclass(frozen=True)
class RecordPlacement:
    """Where one record sits, and whose row numbers it carries.

    The four values that describe a record's *situation* rather than its
    content. ``source_account_id`` is ``""`` for the legacy rows whose column
    is still NULL — the same "no account" the state table spells with an empty
    string, translated at this boundary and nowhere else.
    """

    conversation_id: str
    source_account_id: str
    source_sequence: int
    source_local_id: int


def resolve_record_placement(
    *,
    incoming: RecordPlacement,
    stored: RecordPlacement | None,
    owned_by_committer: bool,
) -> RecordPlacement:
    """Only a record's owner may move it or renumber it (PSU-2.97).

    ``record_key`` is derived from the message identity and knows nothing about
    which machine read it, so two machines capturing one WeChat account land on
    the *same row* — that is the merge working. Until this unit the row's
    conversation, account and row numbers were then overwritten by whoever
    committed last, and the premise written next to that SQL ("a record_key
    belongs to exactly one upload stream, so old and new are normally the
    same") is false in exactly the case this system exists for. On a real
    two-machine archive it moved 279,185 records into the second machine's
    account and split 346 chats down the middle: same name, two entries, half
    the messages in each.

    The owner is ``archive_records.origin_device_id`` — the same
    server-authoritative discriminant ``plan_commit`` uses to tell a
    same-device correction from a cross-device divergence. A row whose owner
    cannot be proven (NULL, written before that column existed) fails safe to
    "not the committer", the same direction ``plan_commit`` fails.

    What a non-owner may still deliver is everything that describes the
    *message*: sender, type, card, quote target, attachment refs. Those are the
    reason the render fingerprint exists. What it may not do is claim the row.

    The one exception is an account of ``""``: a NULL column has nothing worth
    preserving, and the read model filters by ``COALESCE(source_account_id,
    '')``, so leaving it NULL keeps the row invisible to every account. Any
    re-upload backfills it. The conversation has no such case — that column has
    always been NOT NULL.
    """
    if stored is None or owned_by_committer:
        return incoming
    return RecordPlacement(
        conversation_id=stored.conversation_id,
        source_account_id=stored.source_account_id or incoming.source_account_id,
        source_sequence=stored.source_sequence,
        source_local_id=stored.source_local_id,
    )


# 「索引与归档库并排」这个缺省要与「显式不要索引」分得开，而 `None` 已经是后者。
# 一个私有哨兵比 `Path | None | Literal["beside"]` 那种三态串老实。
_SEARCH_INDEX_BESIDE = Path("<beside the archive>")


class SQLiteMetadataStore:
    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        max_batch_records: int = 5_000,
        search_index_path: Path = _SEARCH_INDEX_BESIDE,
        blob_manifest_states: BlobManifestStates | None = None,
        blob_manifest_sizes: BlobManifestSizes | None = None,
        forward_media_manifests: ForwardMediaManifests | None = None,
        # 单条语句的墙钟硬上限。`None` = 不中止，给运维命令用：`settle-merges`
        # 自己就是那条长语句，守卫防的是**请求路径**上的跑飞（PSU-2.98）。
        statement_abort_seconds: float | None = DEFAULT_STATEMENT_ABORT_SECONDS,
    ) -> None:
        if busy_timeout_ms <= 0 or max_batch_records <= 0:
            raise ValueError("storage limits must be positive")
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._max_batch_records = max_batch_records
        # 合并 `attachment_refs` 时要问「这个 blob 落地了没有」，而清单住在
        # **另一个** store、另一个 sqlite 文件里（PSU-2.82）。所以是注入一个
        # 回调而不是自己去连：记录存储不该知道 blob 状态库在哪。
        #
        # 缺省 `None` = 问不到，此时合并退回到「incoming 说了算」，也就是这个
        # 单元之前的行为。装配根接上之后才有 refs 只升不降。
        self._blob_manifest_states = blob_manifest_states
        # 两条单 ref 记录可达工件打平时比原图大小（PSU-2.143）。缺省 `None` = 不比，
        # 留先到的那份——与这一条之前逐字相同。
        self._blob_manifest_sizes = blob_manifest_sizes
        self._forward_media_manifests = forward_media_manifests
        self.supports_forward_media = forward_media_manifests is not None
        self.supports_forward_media_enrichment = forward_media_manifests is not None
        self.supports_forward_media_publications = forward_media_manifests is not None
        self._statement_abort_seconds = statement_abort_seconds
        # 欠搬行数的内存缓存，由 `refresh_conversation_merge_pending_rows` 写、
        # 健康探针读。进程内、按归档，重启就忘——忘掉之后答的是 `None`
        # （"还不知道"），而不是零（PSU-2.98）。
        self._merge_pending_rows: dict[str, int] = {}
        # 有界的写锁，不是裸的 `threading.Lock`（PSU-2.98）：一个慢写不能把六个
        # 写入口连同所有设备一起拖死，而被弃的请求线程更不能无限堆积。
        self._writer_lock = BoundedWriterLock(name="metadata")
        # 账号归并把索引写留给了 worker（PSU-2.101）；运行时把 worker 的 `poke`
        # 挂在这里，归并一结束就叫它起来，而不是等它睡完一个空闲周期。
        self.on_search_rehome_pending: Callable[[], None] | None = None
        # Per-archive marker for an import in progress: it both suppresses the
        # empty-target check between chunks and carries the row counts the
        # finalize self-check compares against what actually landed.
        self._portable_imports: dict[str, PortableImportState] = {}
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()
        # 检索索引住在自己的库里（PSU-2.45），缺省与归档库并排。这个参数说的是
        # **放在哪**，不是**要不要**：一个能答 `search_page` 却其实没有索引的
        # store，会让 `record_model_version` 报 7 而请求打进来就炸——能力探测问的
        # 是类型，它看不见一个构造参数。
        self._search = RemoteSearchIndex(
            database_path.with_name("search.sqlite")
            if search_index_path is _SEARCH_INDEX_BESIDE
            else search_index_path,
            busy_timeout_ms=busy_timeout_ms,
        )

    @property
    def search_index(self) -> RemoteSearchIndex:
        """检索索引本体。它住在自己的库里，是这个 store 的**第二条写入路径**。

        公开成属性而不是让调用方去摸 `_search`：归并要显式重排索引，用例也要能
        在这个接缝上做文章，而"第二条写入路径"这件事本身就该在类型上看得见——
        它正是 2.86 差点漏掉的那一条。
        """
        return self._search

    @staticmethod
    def _publication_blob_ids(raw: str) -> set[str]:
        from chat_archive.domain.forward_media import media_ref

        entries = cast(list[object], json.loads(raw))
        blobs: set[str] = set()
        for entry in entries:
            if isinstance(entry, dict):
                ref = media_ref(cast(dict[str, object], entry).get("remote_media"))
                if ref is not None:
                    blobs.add(ref["blob_id"])
        return blobs

    def forward_media_publication_confirmed(
        self, archive_id: str, record_key: str, device_id: str, publication_sha256: str,
    ) -> bool:
        """An exact durable capture whose blobs remain associated to its parent."""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT p.media_manifest_json,r.attachment_refs FROM forward_media_publications p "
                "JOIN archive_records r ON r.archive_id=p.archive_id AND r.record_key=p.record_key "
                "WHERE p.archive_id=? AND p.record_key=? AND p.device_id=? "
                "AND p.publication_sha256=? LIMIT 1",
                (archive_id, record_key, device_id, publication_sha256),
            ).fetchone()
            if row is None or hashlib.sha256(str(row[0]).encode()).hexdigest() != publication_sha256:
                return False
            blobs = self._publication_blob_ids(str(row[0]))
            return bool(blobs) and blobs.issubset(set(_decode_attachment_refs(row[1])))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1_000,
            # 守卫要说得出「是哪条语句」，而 `set_trace_callback` 交给回调的是
            # 展开过绑定参数的 SQL——一个 200 KB 的 blob 会被整个转成字面量再
            # 丢掉（实测 6.5 倍）。工厂只做一次切片（PSU-2.98）。
            factory=StatementGuardConnection,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        # 装在这里而不是在某一条路径上：连接是每次新建的，而「哪条语句跑飞了」
        # 这个问题在 2026-09-07 的事故里一整个小时都没人答得上来（PSU-2.98）。
        install_statement_guard(
            connection,
            name="metadata",
            abort_after=self._statement_abort_seconds,
        )
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS forward_media_publications (
                    archive_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    publication_sha256 TEXT NOT NULL,
                    media_manifest_json TEXT NOT NULL,
                    source_instance_id TEXT,
                    source_schema_version TEXT,
                    PRIMARY KEY (archive_id, record_key, device_id, publication_sha256),
                    FOREIGN KEY (archive_id, record_key) REFERENCES archive_records(archive_id, record_key) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS archive_sequences (
                    archive_id TEXT PRIMARY KEY,
                    last_server_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS device_cursors (
                    archive_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    last_local_sequence INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, device_id, source_account_id)
                );
                CREATE TABLE IF NOT EXISTS sync_batches (
                    archive_id TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    client_batch_id TEXT NOT NULL,
                    payload_sha256 TEXT NOT NULL,
                    server_sequence_start INTEGER NOT NULL,
                    server_sequence_end INTEGER NOT NULL,
                    inserted_count INTEGER NOT NULL,
                    updated_count INTEGER NOT NULL,
                    conflict_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (archive_id, device_id, client_batch_id)
                );
                CREATE TABLE IF NOT EXISTS archive_records (
                    archive_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    content_text TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    content_sha256 TEXT NOT NULL,
                    source_instance_id TEXT,
                    origin_device_id TEXT,
                    server_sequence INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, record_key)
                );
                CREATE INDEX IF NOT EXISTS ix_archive_records_cursor
                    ON archive_records(archive_id, server_sequence, record_key);
                CREATE INDEX IF NOT EXISTS ix_archive_records_created_at
                    ON archive_records(archive_id, created_at);
                CREATE TABLE IF NOT EXISTS archive_transcripts (
                    archive_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    text TEXT NOT NULL,
                    -- 哪个引擎写的。多台机器的转写在这里汇合，而覆盖只看
                    -- `updated_at`；哪一版赢了，事后只能从这一列读出来。
                    engine TEXT NOT NULL DEFAULT '',
                    low_confidence INTEGER NOT NULL DEFAULT 0,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, record_key)
                );
                CREATE TABLE IF NOT EXISTS archive_accounts (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    -- 这个账号自己的头像（记录模型 v10）。与通讯录那两张表里的
                    -- 同一个口径：台账算出来的 blob id，不是本机资源路径。
                    avatar_blob_id TEXT,
                    -- The sender's own row version, used only to order two
                    -- writes to the same account. A batch replayed out of
                    -- order carries an older one and must not restore a name
                    -- the archive has already moved past.
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, source_account_id)
                );
                CREATE TABLE IF NOT EXISTS archive_account_links (
                    archive_id TEXT NOT NULL,
                    -- 上传流身份：请求路径上的那个 id，一台机器一个，**永不改写**。
                    -- `device_cursors` 与 `sync_batches` 也按它记账，那两张表的
                    -- 1..N 连续性判据就挂在这上面。
                    source_account_id TEXT NOT NULL,
                    -- 微信自己的身份（裸 wxid，记录模型 v11）。两台机器采集同一个
                    -- 微信时，只有这一个值相同。
                    account_key TEXT NOT NULL,
                    -- 阅读身份：同 `account_key` 那一组里字典序最小的上传流 id。
                    -- 取最小而不是「最早」——远端没有可靠的「最早」，而最小值在
                    -- 任何到达顺序下都收敛到同一个答案。
                    primary_account_id TEXT NOT NULL,
                    -- 这条流的行还没搬完。**没有它就得每轮 sync 全表扫一遍**：
                    -- `archive_records` 上没有按账号的索引，问一句「这条流还剩
                    -- 几行」在 73 万行的归档上就是一次全表扫描，而账号身份每轮
                    -- 必发。只在 primary 真的变了的时候置位，搬完清零，于是稳态
                    -- 下一次都不扫。
                    pending_merge INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (archive_id, source_account_id)
                );
                CREATE INDEX IF NOT EXISTS ix_archive_account_links_key
                    ON archive_account_links(archive_id, account_key);
                -- 搬过行、索引还没跟上的记录（PSU-2.101）。归并把索引写搬出了
                -- 写锁：归档库事务里只搬行 + 记这里，提交、放锁之后再写索引，
                -- 写完才删。崩在中间或搜索库被占，键留在这里，下一趟按记录表
                -- 现状整条重写。回填救不了搬过的行（水位不后退），这张表是
                -- 唯一的记性。
                CREATE TABLE IF NOT EXISTS search_rehome_pending (
                    archive_id TEXT NOT NULL,
                    record_key TEXT NOT NULL,
                    PRIMARY KEY (archive_id, record_key)
                );
                CREATE TABLE IF NOT EXISTS archive_conversation_links (
                    archive_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    -- 这条会话在微信眼里的身份（记录模型 v11）。已经按
                    -- `account_key` 加过盐，所以不带账号也不会跨微信撞上——
                    -- 而不带账号，账号归并改写 `source_account_id` 时这张表
                    -- 就一行都不用跟着动。
                    conversation_key TEXT NOT NULL,
                    primary_conversation_id TEXT NOT NULL,
                    pending_merge INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (archive_id, conversation_id)
                );
                CREATE INDEX IF NOT EXISTS ix_archive_conversation_links_key
                    ON archive_conversation_links(archive_id, conversation_key);
                CREATE TABLE IF NOT EXISTS archive_conversations (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    is_group INTEGER NOT NULL,
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, source_account_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS record_conflicts (
                    archive_id TEXT NOT NULL,
                    server_sequence INTEGER NOT NULL,
                    record_key TEXT NOT NULL,
                    canonical_content_sha256 TEXT NOT NULL,
                    incoming_content_sha256 TEXT NOT NULL,
                    canonical_source_instance_id TEXT,
                    incoming_source_instance_id TEXT,
                    incoming_schema_version TEXT,
                    canonical_device_id TEXT,
                    incoming_device_id TEXT,
                    detected_at INTEGER NOT NULL DEFAULT 0,
                    reason TEXT NOT NULL DEFAULT 'content',
                    PRIMARY KEY (archive_id, server_sequence)
                );
                CREATE INDEX IF NOT EXISTS ix_record_conflicts_key
                    ON record_conflicts(archive_id, record_key);
                CREATE TABLE IF NOT EXISTS archive_identities (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    -- 通讯录里那个人的标识。与记录上的 `sender_uid` 同一个口径,
                    -- 所以气泡上的头像是一次 JOIN 就能接上的。
                    uid TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    remark TEXT,
                    avatar_blob_id TEXT,
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, source_account_id, uid)
                );
                CREATE TABLE IF NOT EXISTS archive_conversation_members (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    member_uid TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    group_alias TEXT,
                    is_self INTEGER NOT NULL DEFAULT 0,
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (
                        archive_id, source_account_id, conversation_id, member_uid
                    )
                );
                CREATE TABLE IF NOT EXISTS archive_conversation_profiles (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    -- 单聊对面那个人的标识。本机 `conversations.conversation_uid`
                    -- 就是它,名字过滤要靠它去匹配联系人名与备注——本机匹配 7 列,
                    -- 少了这一列这一端只能匹配到 5 列。
                    contact_uid TEXT,
                    avatar_blob_id TEXT,
                    announcement TEXT,
                    my_group_alias TEXT,
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, source_account_id, conversation_id)
                );
                CREATE INDEX IF NOT EXISTS ix_archive_identities_name
                    ON archive_identities(archive_id, source_account_id, display_name);
                CREATE INDEX IF NOT EXISTS ix_archive_members_conversation
                    ON archive_conversation_members(
                        archive_id, source_account_id, conversation_id, member_uid
                    );
                CREATE TABLE IF NOT EXISTS archive_stats (
                    archive_id TEXT PRIMARY KEY,
                    message_count INTEGER NOT NULL,
                    conversation_count INTEGER NOT NULL,
                    last_server_sequence INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS blob_manifests (
                    archive_id TEXT NOT NULL,
                    blob_id TEXT NOT NULL,
                    size INTEGER NOT NULL,
                    sha256 TEXT NOT NULL,
                    state TEXT NOT NULL,
                    PRIMARY KEY (archive_id, blob_id)
                );
                """
            )
            self._ensure_column(
                connection,
                table="sync_batches",
                column="conflict_count",
                declaration="INTEGER NOT NULL DEFAULT 0",
            )
            # Which upload stream a batch belonged to. NULL on rows committed
            # before cursors were account-scoped: their stream cannot be proven
            # after the fact, and inventing one would corrupt the sequence
            # continuity the recovery drill checks.
            self._ensure_column(
                connection,
                table="sync_batches",
                column="source_account_id",
                declaration="TEXT",
            )
            self._rebuild_legacy_device_cursors(connection)
            self._ensure_column(
                connection,
                table="archive_records",
                column="source_instance_id",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="archive_records",
                column="origin_device_id",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="record_conflicts",
                column="canonical_source_instance_id",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="record_conflicts",
                column="canonical_device_id",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="record_conflicts",
                column="incoming_device_id",
                declaration="TEXT",
            )
            self._ensure_column(
                connection,
                table="record_conflicts",
                column="detected_at",
                declaration="INTEGER NOT NULL DEFAULT 0",
            )
            # 分歧的**种类**（PSU-2.82）。既有行全部是内容分歧，所以常量
            # DEFAULT 就是回填，没有第二趟可以被打断。
            self._ensure_column(
                connection,
                table="record_conflicts",
                column="reason",
                declaration="TEXT NOT NULL DEFAULT 'content'",
            )
            # PSU-2.30's render model. Every one of these is nullable and stays
            # NULL on rows written before this build: NULL means "the sender
            # never told us", which the read model must keep distinct from an
            # empty value — that difference is what the page shows honestly
            # instead of inventing a sender or a type.
            #
            # The two position columns are the exception, and deliberately so:
            # they are NOT NULL DEFAULT 0, the same sentinel the local
            # ``messages`` table uses for "the source did not say". Left
            # nullable, the reading-order index could not be used without a
            # COALESCE in ORDER BY, and every pre-existing row would have had
            # to wait for the backfill before it could be ordered at all.
            for column, declaration in (
                ("sender_uid", "TEXT"),
                ("sender_display_name", "TEXT"),
                ("is_self", "INTEGER"),
                ("message_type", "TEXT"),
                ("app_card_json", "TEXT"),
                ("reply_to_key", "TEXT"),
                ("source_sequence", "INTEGER NOT NULL DEFAULT 0"),
                ("source_local_id", "INTEGER NOT NULL DEFAULT 0"),
                ("attachment_refs", "TEXT"),
                ("metadata_fingerprint", "TEXT"),
            ):
                self._ensure_column(
                    connection,
                    table="archive_records",
                    column=column,
                    declaration=declaration,
                )
            # Created after the ALTERs, not in the script above: the columns it
            # covers do not exist yet when that script runs on a v1 database.
            # Serves both the reading-order page and the conversation list's
            # GROUP BY, which until now scanned the whole table per request.
            connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_archive_records_reading_order
                    ON archive_records(
                        archive_id, conversation_id, created_at,
                        source_sequence, source_local_id, record_key
                    )
                """
            )
            # 账号头像那一列（记录模型 v10）。`CREATE TABLE IF NOT EXISTS` 对一张
            # 已经存在的表什么也不做，所以早于 v10 建起来的归档只能靠这一句补列。
            self._ensure_column(
                connection,
                table="archive_accounts",
                column="avatar_blob_id",
                declaration="TEXT",
            )
            # 转写的引擎列（记录模型 v13）。这张表还没上过任何真远端，所以
            # 上面那句 CREATE 就够了——补这一句是因为开发树里已经有按旧形状
            # 建好的库，而 `IF NOT EXISTS` 对它一个字也不会改。
            self._ensure_column(
                connection,
                table="archive_transcripts",
                column="engine",
                declaration="TEXT NOT NULL DEFAULT ''",
            )
            if self._migrate_account_dimension(connection):
                # That migration rewrites `archive_records.source_account_id`,
                # and that column is the key the conversation state is grouped
                # by — a state table built before it is now attributed to the
                # wrong accounts. Drop it so the next step rebuilds it exactly.
                connection.execute("DROP TABLE IF EXISTS archive_conversation_state")
            self._migrate_conversation_state(connection)
            connection.commit()

    @staticmethod
    def _migrate_conversation_state(connection: sqlite3.Connection) -> None:
        """PSU-2.39: materialise what the browse list orders by.

        The list a person reads is ordered by "most recently active", which on
        the records table means ``GROUP BY conversation_id`` with
        ``ORDER BY MAX(created_at) DESC`` — an ordering no keyset can walk,
        because every page has to aggregate the whole archive before it knows
        which hundred rows come first. Measured on a 729k-record synthetic of
        this machine's own archive: 1,204 ms per page, against 0.16 ms off the
        table below.

        This is not a mechanism invented for the remote. It is the same thing
        the local archive has always kept in ``conversation_stats``, which is
        why the two ends can sort by the same rule instead of approximating
        each other.

        Backfilled exactly, once, when the table is created — one grouped scan
        (1,130 ms on that same synthetic), inside whatever transaction the
        caller opened, so a crash cannot leave the table present and empty.
        """
        exists = (
            connection.execute(
                "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
                ("archive_conversation_state",),
            ).fetchone()
            is not None
        )
        if exists:
            columns = {
                str(row["name"])
                for row in connection.execute(
                    "PRAGMA table_info(archive_conversation_state)"
                ).fetchall()
            }
            if "last_content_text" in columns:
                return
            # PSU-2.44 widened the shape. Dropped and rebuilt rather than
            # ALTERed: the two new columns are a *derived* copy of the last
            # record in each group, so an ALTER would leave them NULL and every
            # list row would claim "this conversation has no preview" until
            # something happened to touch it. The rebuild below is exact and
            # is the same one a fresh archive gets — one grouped scan plus one
            # seek per group. Same reasoning as the account-dimension migration
            # dropping this table when it rewrote the column it groups by.
            connection.execute("DROP TABLE archive_conversation_state")
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            """
            CREATE TABLE archive_conversation_state (
                archive_id TEXT NOT NULL,
                -- '' rather than NULL: this is a primary key column, and in
                -- SQLite two NULLs do not conflict, so the unattributed rows
                -- would grow one state row per batch instead of one per
                -- conversation. The empty string is the same "no account"
                -- the records table spells NULL, translated at this boundary
                -- and nowhere else.
                source_account_id TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                message_count INTEGER NOT NULL,
                last_created_at INTEGER NOT NULL,
                last_server_sequence INTEGER NOT NULL,
                -- 这一组里最后一条消息的类型与正文（截断存储，PSU-2.44）。
                -- 纯派生：`archive_records` 从 v2 起就带这两个字段,上传字节
                -- 一字不动。两个都可以是 NULL——那是「这一组一条消息都没有」,
                -- 而不是「有一条空消息」。
                last_message_type TEXT,
                last_content_text TEXT,
                PRIMARY KEY (archive_id, source_account_id, conversation_id)
            )
            """
        )
        connection.execute(
            """
            INSERT INTO archive_conversation_state(
                archive_id, source_account_id, conversation_id,
                message_count, last_created_at, last_server_sequence
            )
            SELECT archive_id, COALESCE(source_account_id, ''), conversation_id,
                   COUNT(*), MAX(created_at), MAX(server_sequence)
            FROM archive_records
            GROUP BY archive_id, COALESCE(source_account_id, ''), conversation_id
            """
        )
        # 预览两列单独一趟：一次分组扫描给聚合,一次按组的索引 seek 给「最后
        # 一条」。挤进上面那句 GROUP BY 要靠 SQLite 的裸列取值规则,而那条规则
        # 只在**恰好一个** MIN/MAX 聚合时才有定义,这里有两个。
        connection.execute(_STATE_PREVIEW_BACKFILL_SQL)
        connection.execute(
            """
            CREATE INDEX ix_archive_conversation_state_recent
                ON archive_conversation_state(
                    archive_id, source_account_id, last_created_at DESC, conversation_id
                )
            """
        )

    @staticmethod
    def _migrate_account_dimension(connection: sqlite3.Connection) -> bool:
        """PSU-2.35a: give records and conversations the account dimension.

        Returns whether it changed anything, because what it changes — every
        row's owning account — is what the conversation state is keyed by.

        Everything lands in one transaction on purpose. The column ALTER and
        the ledger backfill must be indivisible: a crash between them would
        leave the column present, this method skipped forever, and every
        pre-existing row silently unattributed — the one outcome the honest
        NULL is not allowed to mean.

        Records are backfilled from the batch ledger, one UPDATE per batch:
        a row's ``server_sequence`` is the sequence of the batch that last
        wrote it, and ``sync_batches`` keeps that batch's sequence window next
        to its account. Rows covered only by pre-scoping batches (account
        NULL in the ledger) stay NULL — their stream cannot be proven, and
        the ledger column's own rule is that it is not guessed at.

        Conversations need a rebuilt primary key (SQLite cannot alter one in
        place). Attribution, per row: the archive's sole ledger account when
        the ledger only ever saw one; otherwise the sole account among the
        conversation's attributed records. A row neither rule can attribute
        is dropped, not sentinel-keyed: the conversation itself stays visible
        through the records-driven list, with the ``display_name: null`` the
        read model already treats as "the name stream has not delivered".
        """

        def has_account_column(table: str) -> bool:
            return "source_account_id" in {
                str(row["name"])
                for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
            }

        records_need_column = not has_account_column("archive_records")
        conversations_need_rebuild = not has_account_column("archive_conversations")
        if not records_need_column and not conversations_need_rebuild:
            return False
        if not connection.in_transaction:
            connection.execute("BEGIN IMMEDIATE")
        if records_need_column:
            connection.execute(
                'ALTER TABLE "archive_records" ADD COLUMN "source_account_id" TEXT'
            )
            batches = connection.execute(
                """
                SELECT archive_id, server_sequence_start, server_sequence_end,
                       source_account_id
                FROM sync_batches WHERE source_account_id IS NOT NULL
                """
            ).fetchall()
            for batch in batches:
                connection.execute(
                    """
                    UPDATE archive_records SET source_account_id = ?
                    WHERE archive_id = ? AND source_account_id IS NULL
                      AND server_sequence BETWEEN ? AND ?
                    """,
                    (
                        str(batch["source_account_id"]),
                        str(batch["archive_id"]),
                        int(batch["server_sequence_start"]),
                        int(batch["server_sequence_end"]),
                    ),
                )
        if conversations_need_rebuild:
            # Statement by statement, never ``executescript``: that call
            # commits the open transaction first, which would land the ALTER
            # without its backfill — exactly the torn state ruled out above.
            connection.execute(
                """
                CREATE TABLE archive_conversations_account_scoped (
                    archive_id TEXT NOT NULL,
                    source_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    is_group INTEGER NOT NULL,
                    source_updated_at INTEGER NOT NULL,
                    updated_at INTEGER NOT NULL,
                    PRIMARY KEY (archive_id, source_account_id, conversation_id)
                )
                """
            )
            connection.execute(
                """
                WITH sole_ledger_account AS (
                    SELECT archive_id,
                           CASE WHEN COUNT(DISTINCT source_account_id) = 1
                                THEN MIN(source_account_id) END AS account
                    FROM sync_batches WHERE source_account_id IS NOT NULL
                    GROUP BY archive_id
                ),
                sole_record_account AS (
                    SELECT archive_id, conversation_id,
                           CASE WHEN COUNT(DISTINCT source_account_id) = 1
                                THEN MIN(source_account_id) END AS account
                    FROM archive_records WHERE source_account_id IS NOT NULL
                    GROUP BY archive_id, conversation_id
                )
                INSERT INTO archive_conversations_account_scoped
                SELECT c.archive_id,
                       COALESCE(ledger.account, records.account),
                       c.conversation_id, c.display_name, c.is_group,
                       c.source_updated_at, c.updated_at
                FROM archive_conversations AS c
                LEFT JOIN sole_ledger_account AS ledger
                    ON ledger.archive_id = c.archive_id
                LEFT JOIN sole_record_account AS records
                    ON records.archive_id = c.archive_id
                   AND records.conversation_id = c.conversation_id
                WHERE COALESCE(ledger.account, records.account) IS NOT NULL
                """
            )
            connection.execute("DROP TABLE archive_conversations")
            connection.execute(
                "ALTER TABLE archive_conversations_account_scoped RENAME TO archive_conversations"
            )
        return True

    @staticmethod
    def _rebuild_legacy_device_cursors(connection: sqlite3.Connection) -> None:
        """Rebuild a pre-account-scoped device_cursors table.

        SQLite cannot alter a primary key in place, and the old per-device
        rows cannot be attributed to an account after the fact, so they are
        dropped rather than guessed at. The consequence is bounded and safe:
        an affected device re-uploads its outbox from sequence 1, the batch
        replay identity and the record upsert absorb every repeat, and
        at-least-once delivery is exactly what the sync contract promises.
        """
        columns = {
            str(row["name"])
            for row in connection.execute('PRAGMA table_info("device_cursors")').fetchall()
        }
        if "source_account_id" in columns:
            return
        connection.executescript(
            """
            DROP TABLE device_cursors;
            CREATE TABLE device_cursors (
                archive_id TEXT NOT NULL,
                device_id TEXT NOT NULL,
                source_account_id TEXT NOT NULL,
                last_local_sequence INTEGER NOT NULL,
                PRIMARY KEY (archive_id, device_id, source_account_id)
            );
            """
        )

    @staticmethod
    def _ensure_column(
        connection: sqlite3.Connection,
        *,
        table: str,
        column: str,
        declaration: str,
    ) -> None:
        """Add one legacy-schema column exactly once.

        Existing canonical rows deliberately keep ``origin_device_id=NULL``:
        their committing device cannot be proven from the canonical row alone,
        so the first divergent write must fail safe as a conflict.
        """
        columns = {
            str(row["name"])
            for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
        }
        if column not in columns:
            connection.execute(
                f'ALTER TABLE "{table}" ADD COLUMN "{column}" {declaration}'
            )

    def _merge_batch_attachment_refs(
        self,
        *,
        archive_id: str,
        changed: Sequence[SequencedRecord],
        canonical_refs: Mapping[str, str | None],
    ) -> _MergedRefs:
        """Decide each changed record's ``attachment_refs`` (PSU-2.82).

        The manifest lookup happens **once per batch and only for the records
        that need it** — those whose refs differ from what is stored *and*
        where both sides actually name something. A record the sender says
        nothing about, or one that has no stored refs yet, is decided without
        asking the blob store anything. After content claiming lands, two
        machines converge on the same blob ids for the same picture, so
        "differ" becomes rare and this path costs nothing at all.
        """
        incoming: dict[str, str | None] = {
            item.record.record_key: _encode_attachment_refs(item.record.attachment_refs)
            for item in changed
        }
        undecided = {
            key: value
            for key, value in incoming.items()
            if key in canonical_refs and value != canonical_refs[key]
        }
        if not undecided:
            return _MergedRefs(dict(incoming))
        comparable = {
            key
            for key, value in undecided.items()
            if value is not None and canonical_refs[key] is not None
        }
        states: Mapping[str, str] = {}
        if comparable and self._blob_manifest_states is not None:
            wanted: list[str] = []
            for key in comparable:
                wanted.extend(attachment_refs_blob_ids(undecided[key]))
                wanted.extend(attachment_refs_blob_ids(canonical_refs[key]))
            states = self._blob_manifest_states(archive_id, tuple(dict.fromkeys(wanted)))

        def ready(blob_id: str) -> bool:
            return states.get(blob_id) == "remote_ready"

        # 只为两边都恰好一个 ref 的那些问大小（PSU-2.143），而且一批只问一次。
        sizes: Mapping[str, int] = {}
        if comparable and self._blob_manifest_sizes is not None:
            single: list[str] = []
            for key in comparable:
                mine = _decode_attachment_refs(undecided[key])
                theirs = _decode_attachment_refs(canonical_refs[key])
                if len(mine) == 1 and len(theirs) == 1:
                    single.extend((mine[0], theirs[0]))
            if single:
                sizes = self._blob_manifest_sizes(
                    archive_id, tuple(dict.fromkeys(single))
                )

        resolved = dict(incoming)
        for key, value in undecided.items():
            if key in comparable and self._blob_manifest_states is None:
                # No lookup wired means no honest way to compare reachability,
                # so this store keeps what it did before the unit: incoming
                # wins. Refusing the batch over a missing wiring would be
                # worse, and the NULL rule below still applies either way.
                continue
            decision = merge_attachment_refs(
                incoming=value,
                canonical=canonical_refs[key],
                ready=ready,
                size=sizes.get if self._blob_manifest_sizes is not None else None,
            )
            resolved[key] = decision.refs
        return _MergedRefs(resolved)

    @staticmethod
    def _receipt(row: sqlite3.Row) -> BatchReceipt:
        return BatchReceipt(
            client_batch_id=str(row["client_batch_id"]),
            server_sequence_start=int(row["server_sequence_start"]),
            server_sequence_end=int(row["server_sequence_end"]),
            inserted_count=int(row["inserted_count"]),
            updated_count=int(row["updated_count"]),
            conflict_count=int(row["conflict_count"]),
        )

    def commit_batch(self, batch: SyncBatch) -> BatchReceipt:
        validate_batch(batch, max_records=self._max_batch_records)
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                # 上传流身份 → 阅读身份，一进门翻译一次（记录模型 v11）。
                #
                # 此后这一批的**每一处读模型写入**都用它：记录、会话、状态表，
                # 以及索引——索引是第二条独立的写入路径，只改上面那句 SQL 会让它
                # 继续按流 id 长歪。
                #
                # `device_cursors` 与 `sync_batches` 例外，仍用 `batch.source_account_id`：
                # 那两张是上传流的账本，每台设备的 1..N 连续性判据挂在原始 id 上，
                # 并掉就是两台设备的序号互相打架。
                read_account_id = self._read_account_id(
                    connection, batch.archive_id, batch.source_account_id
                )
                # 会话是同一道翻译的第二次应用，而且链接要在翻译**之前**建：这一批
                # 自己带来的键若已经有 primary，它的记录就直接落在 primary 名下，
                # 不必等下一轮搬家。只建链、只选举，**不搬家**——搬多少行由归档
                # 决定而不是由这一批决定，塞进批次事务里就是让一次普通同步偶尔变成
                # 几分钟的写锁；搬家交给每轮必发的账号身份发布去驱动
                # （`_settle_conversation_merges`）。
                #
                # 2026-09-02 真机上漏掉这道翻译的后果：败者会话归并过一次之后，
                # 那台机器再来一批消息，记录、会话名、状态行、索引全部按它自报的
                # 旧 id 落地，「两个联系人A」在界面上复活——而链接表里它早已归位，
                # 没有任何东西会再搬第二次。翻译发生在入口，此后**每一处**读模型
                # 写入都用翻译后的会话 id；`_link_conversations` 例外，它记的正是
                # 发送方自报的那个 id。
                self._link_conversations(
                    connection, batch.archive_id, batch.conversations
                )
                read_conversation_ids = self._read_conversation_ids(
                    connection,
                    batch.archive_id,
                    {record.conversation_id for record in batch.records}
                    | {item.conversation_id for item in batch.conversations},
                )
                prior = connection.execute(
                    """
                    SELECT client_batch_id, payload_sha256, server_sequence_start,
                           server_sequence_end, inserted_count, updated_count, conflict_count
                    FROM sync_batches
                    WHERE archive_id = ? AND device_id = ? AND client_batch_id = ?
                    """,
                    (batch.archive_id, batch.device_id, batch.client_batch_id),
                ).fetchone()
                if prior is not None:
                    if str(prior["payload_sha256"]) != batch.payload_sha256:
                        raise MetadataConflictError("client batch identity has conflicting payload")
                    connection.rollback()
                    return self._receipt(prior)

                cursor = connection.execute(
                    """
                    SELECT last_local_sequence FROM device_cursors
                    WHERE archive_id = ? AND device_id = ? AND source_account_id = ?
                    """,
                    (batch.archive_id, batch.device_id, batch.source_account_id),
                ).fetchone()
                expected_start = 1 if cursor is None else int(cursor["last_local_sequence"]) + 1
                if batch.local_sequence_start != expected_start:
                    raise MetadataConflictError("local sequence window is not contiguous")

                sequence_row = connection.execute(
                    "SELECT last_server_sequence FROM archive_sequences WHERE archive_id = ?",
                    (batch.archive_id,),
                ).fetchone()
                last_sequence = (
                    0 if sequence_row is None else int(sequence_row["last_server_sequence"])
                )
                existing: dict[str, StoredIdentity] = {}
                # Where each already-stored record sat before this batch, so
                # the conversation counters below can be moved exactly rather
                # than recounted. A re-upload may change a record's
                # conversation or its account — ``record_key`` is derived from
                # the message identity, not from either — and an incremental
                # counter that assumes otherwise drifts silently.
                placed: dict[str, RecordPlacement] = {}
                # 每条 record_key 上现存的 refs 原文（可能是 NULL）。存原文而不是
                # 解码后的元组：NULL（「没说过」）与 `[]`（「说了没有」）在这个
                # 文件里处处是两个答案，合并这一步也不例外。
                canonical_refs: dict[str, str | None] = {}
                canonical_cards: dict[str, str | None] = {}
                canonical_rows: dict[str, sqlite3.Row] = {}
                for record in batch.records:
                    stored = connection.execute(
                        """
                        SELECT content_sha256, source_instance_id, origin_device_id,
                               metadata_fingerprint, conversation_id, source_account_id,
                               source_sequence, source_local_id, attachment_refs, app_card_json,
                               message_type
                        FROM archive_records
                        WHERE archive_id = ? AND record_key = ?
                        """,
                        (batch.archive_id, record.record_key),
                    ).fetchone()
                    if stored is not None:
                        canonical_rows[record.record_key] = stored
                        if stored["content_sha256"] == record.content_sha256 or (
                            self._forward_media_manifests is not None
                            and
                            stored["message_type"] == "app" and record.message_type == "app"
                        ):
                            canonical_cards[record.record_key] = stored["app_card_json"]
                        raw_refs = stored["attachment_refs"]
                        canonical_refs[record.record_key] = (
                            None if raw_refs is None else str(raw_refs)
                        )
                        source_instance = stored["source_instance_id"]
                        origin_device = stored["origin_device_id"]
                        fingerprint = stored["metadata_fingerprint"]
                        placed[record.record_key] = RecordPlacement(
                            conversation_id=str(stored["conversation_id"]),
                            source_account_id=(
                                "" if stored["source_account_id"] is None
                                else str(stored["source_account_id"])
                            ),
                            source_sequence=int(stored["source_sequence"]),
                            source_local_id=int(stored["source_local_id"]),
                        )
                        existing[record.record_key] = StoredIdentity(
                            content_sha256=str(stored["content_sha256"]),
                            source_instance_id=(
                                None if source_instance is None else str(source_instance)
                            ),
                            origin_device_id=(
                                None if origin_device is None else str(origin_device)
                            ),
                            metadata_fingerprint=(
                                None if fingerprint is None else str(fingerprint)
                            ),
                        )
                from chat_archive.domain.forward_media import (
                    card_media_refs,
                    enrich_canonical_media,
                    merge_media_cards,
                    validate_media_card,
                )

                wanted = {
                    ref["blob_id"]
                    for record in batch.records
                    for raw in (record.app_card_json, canonical_cards.get(record.record_key))
                    for ref in card_media_refs(raw)
                }
                manifests: Mapping[str, BlobManifest] = (
                    self._forward_media_manifests(batch.archive_id, tuple(wanted))
                    if wanted and self._forward_media_manifests is not None
                    else {}
                )

                def verified(ref: Mapping[str, object]) -> bool:
                    manifest = manifests.get(str(ref["blob_id"]))
                    return (
                        manifest is not None
                        and manifest.state == "remote_ready"
                        and manifest.size == ref["size"]
                        and manifest.sha256 == ref["sha256"]
                    )

                from chat_archive.domain.forward_media import has_legacy_media_gap, media_publication

                publications: dict[str, tuple[str, str, str | None, str | None]] = {}
                effective_records: list[RecordInput] = []
                forward_refs: dict[str, tuple[str, ...]] = {}
                for record in batch.records:
                    # Do not enrich a thin sender: its legacy no-op must remain a no-op.
                    if record_metadata_fingerprint(record) is None:
                        effective_records.append(record)
                        continue
                    try:
                        validate_media_card(record.app_card_json)
                    except ValueError as exc:
                        raise MetadataValidationError(str(exc)) from None
                    if any(not verified(ref) for ref in card_media_refs(record.app_card_json)):
                        raise MetadataValidationError(
                            "forward media must match a verified manifest"
                        )
                    canonical = canonical_cards.get(record.record_key)
                    stored_row = canonical_rows.get(record.record_key)
                    if (
                        stored_row is not None
                        and stored_row["content_sha256"] != record.content_sha256
                        and stored_row["origin_device_id"] != batch.device_id
                        and stored_row["message_type"] == "app"
                        and record.message_type == "app"
                    ):
                        enriched, enriched_refs = enrich_canonical_media(record.app_card_json, canonical, verified)
                        publication = media_publication(record.app_card_json)
                        if publication is not None and has_legacy_media_gap(record.app_card_json, canonical):
                            publications[record.record_key] = (*publication, record.source_instance_id, record.schema_version)
                            # A receipt belongs to this capture, not to the old hashless
                            # placeholder. Preserve canonical card bytes verbatim when
                            # no independently matched canonical item was enriched.
                            if enriched_refs is None:
                                enriched = canonical
                                enriched_refs = ()
                            enriched_refs = tuple(sorted(set(enriched_refs) | {
                                ref["blob_id"] for ref in card_media_refs(record.app_card_json)
                            }))
                        if enriched_refs is not None:
                            fields = connection.execute(
                                """SELECT content_text,created_at,sender_uid,sender_display_name,is_self,reply_to_key
                                   FROM archive_records WHERE archive_id=? AND record_key=?""",
                                (batch.archive_id, record.record_key),
                            ).fetchone()
                            if fields is None:
                                raise MetadataValidationError("canonical record is unavailable")
                            preserved_refs = tuple(sorted(set(enriched_refs) | set(
                                _decode_attachment_refs(canonical_refs.get(record.record_key))
                            )))
                            # The original wire was already validated. This
                            # internal projection preserves canonical body and
                            # provenance while adding only verified media.
                            record = replace(
                                record, content_text=str(fields["content_text"]),
                                content_sha256=str(stored_row["content_sha256"]),
                                created_at=int(fields["created_at"]),
                                source_instance_id=stored_row["source_instance_id"],
                                sender_uid=fields["sender_uid"],
                                sender_display_name=fields["sender_display_name"],
                                is_self=None if fields["is_self"] is None else bool(fields["is_self"]),
                                message_type=stored_row["message_type"], reply_to_key=fields["reply_to_key"],
                                source_sequence=stored_row["source_sequence"], source_local_id=stored_row["source_local_id"],
                                app_card_json=enriched, attachment_refs=preserved_refs,
                            )
                            forward_refs[record.record_key] = preserved_refs
                        effective_records.append(record)
                        continue
                    if self._forward_media_manifests is None and card_media_refs(canonical):
                        # A restored/offline store cannot re-check objects, but it must
                        # not erase already verified associations on an old-client sync.
                        preserved = tuple(sorted({ref["blob_id"] for ref in card_media_refs(canonical)}))
                        record = replace(record, app_card_json=canonical, attachment_refs=preserved)
                        effective_records.append(record)
                        forward_refs[record.record_key] = preserved
                        continue
                    card, refs = merge_media_cards(
                        record.app_card_json,
                        canonical_cards.get(record.record_key),
                        verified,
                    )
                    if refs is not None:
                        record = replace(record, app_card_json=card, attachment_refs=refs)
                        forward_refs[record.record_key] = refs
                    effective_records.append(record)
                plan = plan_commit(
                    replace(batch, records=tuple(effective_records)),
                    existing=existing,
                    last_server_sequence=last_sequence,
                    now=int(time.time()),
                    # This store keeps the fingerprint column, so it opts into
                    # the render-model rule. Without it a full retransmission
                    # of unchanged content would be classified as 729k no-ops
                    # and land not one of the new fields.
                    tracks_metadata_fingerprint=True,
                )
                accepted_keys = {item.record.record_key for item in plan.changed}
                for key, (digest, manifest, capture_source, capture_schema) in publications.items():
                    if key in accepted_keys or key in canonical_rows:
                        connection.execute(
                            "INSERT OR IGNORE INTO forward_media_publications VALUES (?,?,?,?,?,?,?)",
                            (batch.archive_id, key, batch.device_id, digest, manifest, capture_source, capture_schema),
                        )
                last_sequence = plan.sequence_end
                sequence_start = plan.sequence_start
                inserted = plan.inserted_count
                updated = plan.updated_count
                conflicts = len(plan.conflicts)
                merged = self._merge_batch_attachment_refs(
                    archive_id=batch.archive_id,
                    changed=plan.changed,
                    canonical_refs=canonical_refs,
                )
                for key, refs in forward_refs.items():
                    merged.refs[key] = _encode_attachment_refs(refs)
                # Independent capture history must remain addressable even when
                # a later legacy sender knows nothing about supplemental items.
                for item in plan.changed:
                    key = item.record.record_key
                    stored_publications = connection.execute(
                        "SELECT media_manifest_json FROM forward_media_publications "
                        "WHERE archive_id=? AND record_key=?", (batch.archive_id, key),
                    ).fetchall()
                    supplemental: set[str] = set()
                    for publication_row in stored_publications:
                        supplemental.update(self._publication_blob_ids(str(publication_row[0])))
                    if supplemental:
                        merged.refs[key] = _encode_attachment_refs(tuple(sorted(supplemental | set(
                            _decode_attachment_refs(merged.refs.get(key)),
                        ))))
                # 落点算一次，此后**三处共用**：记录 upsert、会话计数器、搜索索引。
                # 索引与计数器是两条独立的写入路径，只改 upsert 会让它们继续按
                # 提交方自报的那一组长歪——而计数器一旦与记录表判得不一样，
                # 状态表就会凭空多出一组，界面上是「多了一个空会话」。
                placement = {
                    item.record.record_key: resolve_record_placement(
                        incoming=RecordPlacement(
                            conversation_id=read_conversation_ids[
                                item.record.conversation_id
                            ],
                            source_account_id=read_account_id,
                            source_sequence=item.record.source_sequence or 0,
                            source_local_id=item.record.source_local_id or 0,
                        ),
                        stored=placed.get(item.record.record_key),
                        owned_by_committer=(
                            existing[item.record.record_key].origin_device_id
                            == batch.device_id
                        )
                        if item.record.record_key in existing
                        else True,
                    )
                    for item in plan.changed
                }
                changed: list[tuple[object, ...]] = [
                    (
                        batch.archive_id,
                        item.record.record_key,
                        placement[item.record.record_key].conversation_id,
                        item.record.created_at,
                        item.record.content_text,
                        normalize_search_text(item.record.content_text),
                        item.record.content_sha256,
                        item.record.source_instance_id,
                        batch.device_id,
                        item.server_sequence,
                        item.record.sender_uid,
                        item.record.sender_display_name,
                        None if item.record.is_self is None else int(item.record.is_self),
                        item.record.message_type,
                        item.record.app_card_json,
                        item.record.reply_to_key,
                        placement[item.record.record_key].source_sequence,
                        placement[item.record.record_key].source_local_id,
                        merged.refs[item.record.record_key],
                        record_metadata_fingerprint(item.record),
                        placement[item.record.record_key].source_account_id,
                    )
                    for item in plan.changed
                ]
                connection.executemany(
                    """
                    INSERT INTO archive_records(
                        archive_id, record_key, conversation_id, created_at,
                        content_text, normalized_content, content_sha256,
                        source_instance_id, origin_device_id, server_sequence,
                        sender_uid, sender_display_name, is_self, message_type,
                        app_card_json, reply_to_key, source_sequence,
                        source_local_id, attachment_refs, metadata_fingerprint,
                        source_account_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(archive_id, record_key) DO UPDATE SET
                        -- 会话、账号与两个本机行号都已经由
                        -- `resolve_record_placement` 判过（PSU-2.97）：非属主
                        -- 设备的那四个值在绑参数之前就换成了库里的旧值，所以
                        -- 这里仍然写 `excluded` 的那一份。判定放在 Python 里而
                        -- 不写成四个 CASE，是因为**会话计数器与搜索索引要用同一
                        -- 个结果**，而那两条是独立的写入路径，各判各的就会漂。
                        conversation_id = excluded.conversation_id,
                        created_at = excluded.created_at,
                        content_text = excluded.content_text,
                        normalized_content = excluded.normalized_content,
                        content_sha256 = excluded.content_sha256,
                        source_instance_id = excluded.source_instance_id,
                        -- 内容没变就不改归属。`origin_device_id` 是
                        -- `plan_commit` 用来分辨「同设备更正」与「跨设备分歧」
                        -- 的那一个判据；一次只补渲染字段的刷新（content 同、
                        -- 指纹不同）把它翻给提交方，此后原采集设备的一次正文
                        -- 更正就会被判成 conflict 而不是 version——账本噪音，
                        -- 且首提交内容仍胜，所以看不出是记账错了。
                        --
                        -- 只在这一个 store 上修：content 同的 version 分支
                        -- **只有开了 `tracks_metadata_fingerprint` 的后端走得到**
                        -- （其余三个实现里 content 同一律是 noop，压根不进这条
                        -- upsert）。改另外三处等于动一份跨语言冻结实现却不改变
                        -- 任何行为。
                        --
                        -- 上面那行 `source_instance_id` 刻意仍跟着提交方：它是
                        -- 客户端自报的诊断字段（哪一台微信装机），不是服务端
                        -- 认证过的归属，冲突仲裁一个字都不看它。
                        origin_device_id = CASE
                            WHEN archive_records.content_sha256 = excluded.content_sha256
                            THEN archive_records.origin_device_id
                            ELSE excluded.origin_device_id
                        END,
                        server_sequence = excluded.server_sequence,
                        sender_uid = excluded.sender_uid,
                        sender_display_name = excluded.sender_display_name,
                        is_self = excluded.is_self,
                        message_type = excluded.message_type,
                        app_card_json = excluded.app_card_json,
                        reply_to_key = excluded.reply_to_key,
                        source_sequence = excluded.source_sequence,
                        source_local_id = excluded.source_local_id,
                        attachment_refs = excluded.attachment_refs,
                        metadata_fingerprint = excluded.metadata_fingerprint,
                        source_account_id = excluded.source_account_id
                    """,
                    changed,
                )
                # Conversations ride outside the record plan and consume no
                # server sequence. Last write wins, but only forward: a batch
                # replayed out of order carries an older ``updated_at`` and
                # must not restore a name the archive has already moved past.
                connection.executemany(
                    """
                    INSERT INTO archive_conversations(
                        archive_id, source_account_id, conversation_id,
                        display_name, is_group, source_updated_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(archive_id, source_account_id, conversation_id)
                    DO UPDATE SET
                        display_name = excluded.display_name,
                        is_group = excluded.is_group,
                        source_updated_at = excluded.source_updated_at,
                        updated_at = excluded.updated_at
                    WHERE excluded.source_updated_at >= archive_conversations.source_updated_at
                    """,
                    [
                        (
                            batch.archive_id,
                            # 会话名随批次的**阅读身份**入键（PSU-2.35a 起按
                            # 上传流，2.86 起按翻译后的账号）。两个真正不同的
                            # 账号各叫各的 filehelper：同名 conversation_id 各自
                            # 成行，改名不再互相覆盖；而同一个微信的两条流会落到
                            # 同一个账号下。
                            read_account_id,
                            read_conversation_ids[conversation.conversation_id],
                            conversation.display_name,
                            int(conversation.is_group),
                            conversation.updated_at,
                            int(time.time()),
                        )
                        for conversation in batch.conversations
                    ],
                )
                connection.executemany(
                    _RECORD_CONFLICT_INSERT,
                    [
                        (
                            batch.archive_id,
                            conflict.server_sequence,
                            conflict.record_key,
                            conflict.canonical_content_sha256,
                            conflict.incoming_content_sha256,
                            conflict.canonical_source_instance_id,
                            conflict.incoming_source_instance_id,
                            conflict.incoming_schema_version,
                            conflict.canonical_device_id,
                            conflict.incoming_device_id,
                            conflict.detected_at,
                            CONTENT_CONFLICT_REASON,
                        )
                        for conflict in plan.conflicts
                    ],
                )
                # attachment_refs 的分歧由 `merge_attachment_refs` 自动裁决，
                # 不再写账本；库里已有的 reason=attachment_refs 旧行原样保留。
                connection.execute(
                    """
                    INSERT INTO archive_sequences(archive_id, last_server_sequence)
                    VALUES (?, ?)
                    ON CONFLICT(archive_id) DO UPDATE SET
                        last_server_sequence = excluded.last_server_sequence
                    """,
                    (batch.archive_id, last_sequence),
                )
                connection.execute(
                    """
                    INSERT INTO device_cursors(
                        archive_id, device_id, source_account_id, last_local_sequence
                    )
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(archive_id, device_id, source_account_id) DO UPDATE SET
                        last_local_sequence = excluded.last_local_sequence
                    """,
                    (
                        batch.archive_id,
                        batch.device_id,
                        batch.source_account_id,
                        batch.local_sequence_end,
                    ),
                )
                connection.execute(
                    """
                    INSERT INTO sync_batches(
                        archive_id, device_id, source_account_id, client_batch_id,
                        payload_sha256, server_sequence_start, server_sequence_end,
                        inserted_count, updated_count, conflict_count
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        batch.archive_id,
                        batch.device_id,
                        batch.source_account_id,
                        batch.client_batch_id,
                        batch.payload_sha256,
                        sequence_start,
                        last_sequence,
                        inserted,
                        updated,
                        conflicts,
                    ),
                )
                self._apply_conversation_state(
                    connection,
                    archive_id=batch.archive_id,
                    changed=plan.changed,
                    placement=placement,
                    placed=placed,
                )
                # Both numbers now come off the conversation state, which is
                # ~one row per conversation. They used to be two full scans of
                # ``archive_records`` **per batch** — on a 729k-record archive
                # that is the single largest thing a commit did, and it was
                # only ever bookkeeping.
                totals = connection.execute(
                    """
                    SELECT COALESCE(SUM(message_count), 0) AS message_count,
                           COUNT(DISTINCT conversation_id) AS conversation_count
                    FROM archive_conversation_state WHERE archive_id = ?
                    """,
                    (batch.archive_id,),
                ).fetchone()
                message_count = int(totals["message_count"])
                conversation_count = int(totals["conversation_count"])
                connection.execute(
                    """
                    INSERT INTO archive_stats(
                        archive_id, message_count, conversation_count, last_server_sequence
                    ) VALUES (?, ?, ?, ?)
                    ON CONFLICT(archive_id) DO UPDATE SET
                        message_count = excluded.message_count,
                        conversation_count = excluded.conversation_count,
                        last_server_sequence = excluded.last_server_sequence
                    """,
                    (batch.archive_id, message_count, conversation_count, last_sequence),
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise
        # 索引在**提交之后**才喂，而且喂失败一个字都不影响这一批：记录是同步的
        # 目的，索引是从记录派生出来的东西，掉了的窗口由回填补得回来（水位就停在
        # 这里不前进）。**异常也要吞掉**——抛出去会让一批已经落库的记录在发送方
        # 看来是失败的，于是它下一轮重传一遍已经在这里的东西，而根因只是一条
        # 派生物没写进去。
        try:
            self._index_committed(batch, plan.changed, placement=placement)
        except (sqlite3.Error, OSError):
            _logger.exception(
                "search index write failed after commit; the backfill will catch up"
            )
        return BatchReceipt(
            client_batch_id=batch.client_batch_id,
            server_sequence_start=sequence_start,
            server_sequence_end=last_sequence,
            inserted_count=inserted,
            updated_count=updated,
            conflict_count=conflicts,
        )

    def _index_committed(
        self,
        batch: SyncBatch,
        changed: tuple[SequencedRecord, ...],
        *,
        placement: Mapping[str, RecordPlacement],
    ) -> None:
        """Feed this batch's changed rows to the search index (PSU-2.45).

        Only the changed ones: a record whose content and render fields are
        unchanged is already in the index under the same id, and re-writing it
        would be the batch's cost paid twice for nothing.

        ``placement`` is where each record **actually landed** — the batch's
        read identities (record model v11) for a record this device owns, the
        stored row's for one it does not (PSU-2.97). This is a second,
        independent write path: the records table and the index are written by
        different statements against different databases, so deciding an id in
        one of them and not the other is exactly how the index would keep
        growing under an id the reader no longer filters by.
        """
        if not changed:
            return
        records = self._with_transcripts(
            batch.archive_id,
            tuple(
                self._search_record(item, placement[item.record.record_key])
                for item in changed
            ),
        )
        self._search.index_records(batch.archive_id, records)
        self._search.advance_indexed_sequence(
            batch.archive_id, max(item.server_sequence for item in changed)
        )

    @staticmethod
    def _search_record(item: SequencedRecord, placement: RecordPlacement) -> StoredRecord:
        """The batch's record as the index will see it.

        Goes through :class:`StoredRecord` rather than mapping ``RecordInput``
        straight to an indexed message, so the sync path and the backfill path
        share **one** field mapping. Two mappings drift, and the drift shows up
        as "this message is findable only if it was uploaded after Tuesday".

        Every situational field comes off ``placement`` rather than off the
        record: neither the conversation nor the account is necessarily the one
        the sender wrote on it (record model v11 translates them, PSU-2.97 may
        keep the stored row's), and the backfill reads these same columns back
        out of ``archive_records``. Taking them from the sender here is how the
        two paths would disagree about the same row.
        """
        record = item.record
        return StoredRecord(
            record_key=record.record_key,
            conversation_id=placement.conversation_id,
            created_at=record.created_at,
            content_text=record.content_text,
            content_sha256=record.content_sha256,
            server_sequence=item.server_sequence,
            sender_uid=record.sender_uid,
            sender_display_name=record.sender_display_name,
            is_self=record.is_self,
            message_type=record.message_type,
            app_card_json=record.app_card_json,
            reply_to_key=record.reply_to_key,
            source_sequence=placement.source_sequence,
            source_local_id=placement.source_local_id,
            attachment_refs=record.attachment_refs or (),
            source_account_id=placement.source_account_id,
        )

    def backfill_search_chunk(
        self,
        archive_id: str,
        *,
        chunk_size: int = DEFAULT_BACKFILL_CHUNK,
    ) -> tuple[int, bool]:
        """Index one slice of the archive's existing records; returns (rows, finished).

        Shaped like the local ``build_chunk``: a short read, a resumable
        watermark, and a crash costs at most one chunk. Deliberately **not** a
        background thread started at boot — a job that takes minutes on 729k
        rows has no business on the startup path, and operations has to be able
        to pick the window (the attachment upload is often running).
        """
        if chunk_size <= 0:
            raise MetadataValidationError("chunk size must be positive")
        after = self._search.indexed_sequence(archive_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND server_sequence > ?
                ORDER BY server_sequence
                LIMIT ?
                """,
                (archive_id, after, chunk_size),
            ).fetchall()
        if not rows:
            return self._sweep_transcripts_chunk(archive_id, chunk_size=chunk_size)
        records = self._with_transcripts(
            archive_id, tuple(self._stored_record(row) for row in rows)
        )
        self._search.index_records(archive_id, records)
        self._search.advance_indexed_sequence(archive_id, records[-1].server_sequence)
        return (len(records), False)

    def _sweep_transcripts_chunk(
        self, archive_id: str, *, chunk_size: int
    ) -> tuple[int, bool]:
        """把已经收下、却还没接进索引的转写补上一片；返回 (接上几条, 扫完没有)。

        记录都索引完了才轮到它（排在 `backfill_search_chunk` 的尾巴上），所以扫到的
        每一条记录都已经在索引里。收下转写那一刻接不上时（搜索库被占），
        `publish_transcripts` 会把进度拨回开头，由这里再扫一遍——几千行，幂等。
        """
        after = self._search.transcript_sweep_position(archive_id)
        if after is None:
            return (0, True)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT record_key, text FROM archive_transcripts
                WHERE archive_id = ? AND record_key > ?
                ORDER BY record_key
                LIMIT ?
                """,
                (archive_id, after, chunk_size),
            ).fetchall()
        if not rows:
            self._search.set_transcript_sweep_position(archive_id, None)
            return (0, True)
        refreshed = self._search.refresh_transcripts(
            archive_id, ((str(row["record_key"]), str(row["text"])) for row in rows)
        )
        self._search.set_transcript_sweep_position(archive_id, str(rows[-1]["record_key"]))
        return (refreshed, False)

    def search_index_status(self, archive_id: str) -> str:
        return self._search.index_status(
            archive_id, last_server_sequence=self.stats(archive_id).last_server_sequence
        )

    @staticmethod
    def _apply_conversation_state(
        connection: sqlite3.Connection,
        *,
        archive_id: str,
        changed: tuple[SequencedRecord, ...],
        placement: Mapping[str, RecordPlacement],
        placed: Mapping[str, RecordPlacement],
    ) -> None:
        """Move the browse counters for exactly the groups this batch touched.

        ``placement`` is where each record landed and ``placed`` where it sat
        before — both decided once in ``commit_batch`` and handed down, never
        re-derived here. A group is where the record was *actually written*,
        which is neither what the sender wrote on it (record model v11
        translates upload ids to reading ids) nor necessarily the committing
        device's account at all (PSU-2.97 leaves a record it does not own where
        it is). Deciding that twice is how the counters would invent a group
        the records table never gained: an empty conversation on the page and a
        wrong total in ``archive_stats``.

        Exact, not approximate, and cheap in the case that actually happens:

        * ``message_count`` moves by a counted delta. Sync never deletes
          (``docs/local-remote-boundary.md`` rule three), so the only way a
          group can lose a record is a re-upload that moves it to another
          conversation or another account — which ``placed`` reports.
        * ``last_created_at`` is re-read with one seek per touched group. The
          reading-order index leads with ``(archive_id, conversation_id)`` and
          descends by ``created_at``, so this stops on the first row: 0.005 ms
          measured. It degrades only where another account owns newer rows of
          the same conversation, and 52k such rows measured 27 ms — still far
          cheaper than the alternative of taking a running maximum, which a
          corrected timestamp would leave permanently stale.
        * ``last_server_sequence`` takes the running maximum, and that *is*
          exact for a group gaining rows: the sequences written by this batch
          are the highest the archive has ever issued.

        A group that lost rows is recomputed outright — it is the rare path,
        and there its maxima can genuinely fall.
        """
        arriving: dict[tuple[str, str], tuple[int, int]] = {}
        departing: set[tuple[str, str]] = set()
        for item in changed:
            landed = placement[item.record.record_key]
            destination = (landed.source_account_id, landed.conversation_id)
            delta, sequence = arriving.get(destination, (0, 0))
            was = placed.get(item.record.record_key)
            origin = None if was is None else (was.source_account_id, was.conversation_id)
            if origin is None:
                delta += 1
            elif origin != destination:
                delta += 1
                departing.add(origin)
            arriving[destination] = (delta, max(sequence, item.server_sequence))
        for group in departing:
            arriving.setdefault(group, (0, 0))
        for (account, conversation), (delta, sequence) in arriving.items():
            if (account, conversation) in departing:
                SQLiteMetadataStore._recount_conversation_state(
                    connection,
                    archive_id=archive_id,
                    account=account,
                    conversation=conversation,
                )
                continue
            SQLiteMetadataStore._bump_conversation_state(
                connection,
                archive_id=archive_id,
                account=account,
                conversation=conversation,
                delta=delta,
                sequence=sequence,
            )

    @staticmethod
    def _bump_conversation_state(
        connection: sqlite3.Connection,
        *,
        archive_id: str,
        account: str,
        conversation: str,
        delta: int,
        sequence: int,
    ) -> None:
        """一组**多了** `delta` 行：计数加上去，其余按现在的最后一条重取。

        批次提交与归并搬行共用这一条（PSU-2.100）——两件事对状态行说的是同一
        句话「这一组多了 n 行」，原来只有提交走这里，归并却对整条会话 GROUP BY。

        * `message_count += delta`：同步永不删行，得组只增，精确。
        * `last_created_at` 与预览两列：一次索引 seek 取**现在**这一组的最后一条
          （调用方保证行已经写到位），精确；ORDER BY 与转录分页同一条排序线。
        * `last_server_sequence` 取 running max：提交写的序号是最新的，搬来的行
          序号是旧的，MAX 两边都对。
        """
        # 同一次 seek 顺带取回预览要的两列（PSU-2.44）：SELECT 多两列并不
        # 多走一次索引，所以列表行的预览是**零额外查询**换来的。
        newest = connection.execute(
            """
            SELECT created_at, message_type, content_text FROM archive_records
            WHERE archive_id = ? AND conversation_id = ?
              AND COALESCE(source_account_id, '') = ?
            ORDER BY created_at DESC, source_sequence DESC,
                     source_local_id DESC, record_key DESC
            LIMIT 1
            """,
            (archive_id, conversation, account),
        ).fetchone()
        if newest is None:
            return
        connection.execute(
            """
            INSERT INTO archive_conversation_state(
                archive_id, source_account_id, conversation_id,
                message_count, last_created_at, last_server_sequence,
                last_message_type, last_content_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_id, source_account_id, conversation_id)
            DO UPDATE SET
                message_count = archive_conversation_state.message_count + ?,
                last_created_at = excluded.last_created_at,
                last_server_sequence = MAX(
                    archive_conversation_state.last_server_sequence,
                    excluded.last_server_sequence
                ),
                last_message_type = excluded.last_message_type,
                last_content_text = excluded.last_content_text
            """,
            (
                archive_id,
                account,
                conversation,
                delta,
                int(newest["created_at"]),
                sequence,
                _optional_text(newest["message_type"]),
                _state_content(newest["content_text"]),
                delta,
            ),
        )

    @staticmethod
    def _shift_conversation_state(
        connection: sqlite3.Connection,
        *,
        archive_id: str,
        rows: Sequence[sqlite3.Row],
        set_column: str,
        old_value: str,
    ) -> None:
        """一批行刚从 `old_value` 改到了新值：状态行按这一批的搬动量挪一次。

        `rows` 是改写**之后**再读出来的这一批（`_drain_records_chunk` 本来就要
        读它们去重排索引），所以每一行现在的组直接在手上；原来的组只差一维——
        `set_column` 那一列换回 `old_value`。一批可以横跨多个账号（含 `''`）或
        多个会话，按组各算各的 `(delta, max_sequence)`。

        失组只减计数（下限 0），**不在批内删、不重算它的 `last_*`**：两趟归并都把
        失组搬空，删行留给两条尾段——它们只在败者名下已经没有记录时才跑。批内
        不删是为了保住「状态行存在 ⇔ 记录存在」：欠搬判据（`_CONVERSATION_MOVE_OWED_SQL`）
        看的就是状态表，若计数曾被老 ingest 少加过，批内按计数删会**提前**删掉
        一条名下还有记录的会话的状态行。代价是搬到一半时列表里败者短暂显示 0 条、
        预览可能指着一条已经搬走的行，那是几秒钟的窗口——此前整趟搬完之前两边的
        状态行都是旧的，只会更准不会更糟。失组本来就没有状态行时 UPDATE 影响 0 行，
        不算错。

        这就是 PSU-2.100 的全部：搬完之后不再对整条会话 / 整个账号 GROUP BY。
        真机上那一步一个大会话 5.4 s、整账号 7.0 s，都跑在写锁里、都与批大小无关。
        """
        gains: dict[tuple[str, str], tuple[int, int]] = {}
        losses: dict[tuple[str, str], int] = {}
        for row in rows:
            account = row["source_account_id"]
            account = "" if account is None else str(account)
            conversation = str(row["conversation_id"])
            after = (account, conversation)
            if set_column == "conversation_id":
                before = (account, old_value)
            else:
                before = (old_value, conversation)
            delta, sequence = gains.get(after, (0, 0))
            gains[after] = (delta + 1, max(sequence, int(row["server_sequence"])))
            losses[before] = losses.get(before, 0) + 1
        for (account, conversation), delta in losses.items():
            connection.execute(
                "UPDATE archive_conversation_state "
                "SET message_count = MAX(message_count - ?, 0) "
                "WHERE archive_id = ? AND source_account_id = ? AND conversation_id = ?",
                (delta, archive_id, account, conversation),
            )
        for (account, conversation), (delta, sequence) in gains.items():
            SQLiteMetadataStore._bump_conversation_state(
                connection,
                archive_id=archive_id,
                account=account,
                conversation=conversation,
                delta=delta,
                sequence=sequence,
            )

    @staticmethod
    def _recount_conversation_state(
        connection: sqlite3.Connection,
        *,
        archive_id: str,
        account: str,
        conversation: str,
    ) -> None:
        """Rebuild one group's counters from the records themselves."""
        row = connection.execute(
            """
            SELECT COUNT(*) AS message_count,
                   COALESCE(MAX(created_at), 0) AS last_created_at,
                   COALESCE(MAX(server_sequence), 0) AS last_server_sequence
            FROM archive_records
            WHERE archive_id = ? AND conversation_id = ?
              AND COALESCE(source_account_id, '') = ?
            """,
            (archive_id, conversation, account),
        ).fetchone()
        if int(row["message_count"]) == 0:
            connection.execute(
                """
                DELETE FROM archive_conversation_state
                WHERE archive_id = ? AND source_account_id = ? AND conversation_id = ?
                """,
                (archive_id, account, conversation),
            )
            return
        # 这条路径原先只有聚合，而「最后一条消息是哪一条」不是聚合答得出来的
        # （MAX(created_at) 说的是时间，不是那一行）。补一次与增量路径**同一条**
        # ORDER BY 的 seek——重传改归属正是会把这一组的最后一条整个换掉的路径。
        newest = connection.execute(
            """
            SELECT message_type, content_text FROM archive_records
            WHERE archive_id = ? AND conversation_id = ?
              AND COALESCE(source_account_id, '') = ?
            ORDER BY created_at DESC, source_sequence DESC,
                     source_local_id DESC, record_key DESC
            LIMIT 1
            """,
            (archive_id, conversation, account),
        ).fetchone()
        connection.execute(
            """
            INSERT INTO archive_conversation_state(
                archive_id, source_account_id, conversation_id,
                message_count, last_created_at, last_server_sequence,
                last_message_type, last_content_text
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(archive_id, source_account_id, conversation_id)
            DO UPDATE SET
                message_count = excluded.message_count,
                last_created_at = excluded.last_created_at,
                last_server_sequence = excluded.last_server_sequence,
                last_message_type = excluded.last_message_type,
                last_content_text = excluded.last_content_text
            """,
            (
                archive_id,
                account,
                conversation,
                int(row["message_count"]),
                int(row["last_created_at"]),
                int(row["last_server_sequence"]),
                None if newest is None else _optional_text(newest["message_type"]),
                None if newest is None else _state_content(newest["content_text"]),
            ),
        )

    def _read_shaped(
        self, archive_id: str, records: tuple[StoredRecord, ...]
    ) -> tuple[StoredRecord, ...]:
        """一页记录读出来之后要接上的所有东西，**只有这一处**。

        六个读都产出记录，而每多一样读时接上的字段，就多六个可以漏掉它的地方。
        漏掉的样子还特别难看：不是报错，是「某一种翻页看得见转写、另一种看不见」。

        每一样各自一趟查询，各自的键，互不知道对方存在——它们只在这里排成一列。
        """
        return self._with_reply_previews(
            archive_id,
            self._with_transcripts(archive_id, self._with_sender_avatars(archive_id, records)),
        )

    def _with_reply_previews(
        self, archive_id: str, records: tuple[StoredRecord, ...]
    ) -> tuple[StoredRecord, ...]:
        """Resolve deduplicated reply targets in bounded batches, never recursively."""
        wanted = {
            (record.reply_to_key, record.source_account_id, record.conversation_id)
            for record in records
            if record.source_account_id
            and record.reply_to_key
            and record.reply_to_key != record.record_key
        }
        if not wanted:
            return records
        found: dict[tuple[str, str | None, str], ReplyPreviewRecord] = {}
        ordered = sorted(wanted, key=lambda item: (item[0], item[1] or "", item[2]))
        with closing(self._connect()) as connection:
            for start in range(0, len(ordered), 200):
                batch = ordered[start : start + 200]
                placeholders = ", ".join("(?, ?, ?)" for _ in batch)
                parameters: list[object] = [value for target in batch for value in target]
                parameters.append(archive_id)
                rows = connection.execute(
                    f"""
                    WITH wanted(record_key, account, conversation) AS (VALUES {placeholders})
                    SELECT r.record_key, r.source_account_id, r.conversation_id,
                           r.sender_display_name, r.content_text, r.message_type, r.attachment_refs
                    FROM wanted JOIN archive_records AS r
                      ON r.record_key = wanted.record_key
                     AND r.source_account_id IS wanted.account
                     AND r.conversation_id = wanted.conversation
                    WHERE r.archive_id = ?
                    """,
                    parameters,
                ).fetchall()
                for row in rows:
                    key = (
                        str(row["record_key"]),
                        _optional_text(row["source_account_id"]),
                        str(row["conversation_id"]),
                    )
                    found[key] = ReplyPreviewRecord(
                        record_key=key[0],
                        sender_display_name=_optional_text(row["sender_display_name"]),
                        content_text=str(row["content_text"]).strip()[:40],
                        message_type=_optional_text(row["message_type"]),
                        attachment_refs=_decode_attachment_refs(row["attachment_refs"]),
                    )
        return tuple(
            replace(
                record,
                reply_preview=found.get(
                    (record.reply_to_key, record.source_account_id, record.conversation_id)
                ),
            )
            if record.reply_to_key
            else record
            for record in records
        )

    def _with_transcripts(
        self, archive_id: str, records: tuple[StoredRecord, ...]
    ) -> tuple[StoredRecord, ...]:
        """接上这一页里语音消息的机器转写（记录模型 v13，PSU-2.163）。

        **每页一次查询**，理由与头像那一趟一字不差。键是 `record_key`,
        也就是这张表的主键——远端没有附件指纹，本机那把 sha256 的键
        在这一端对不上任何东西。

        只跳过**确定不可能是语音**的那些行——类型已知且不是 `audio`。
        类型为 `None` 的照查：那是「这个远端没被告知过类型」，不是「不是语音」，
        按它筛等于让一整批早期记录从此不再有转写，而且不会有人知道。
        整页都不可能是语音时（绝大多数会话如此）连查询都不发。
        """
        if not records:
            return records
        by_key: dict[str, RecordTranscript] = {}
        keys = sorted(
            {
                record.record_key
                for record in records
                if record.message_type in (None, "audio")
            }
        )
        if not keys:
            return records
        with closing(self._connect()) as connection:
            for start in range(0, len(keys), _TRANSCRIPT_LOOKUP_CHUNK):
                batch = keys[start : start + _TRANSCRIPT_LOOKUP_CHUNK]
                placeholders = ", ".join("?" for _ in batch)
                rows = connection.execute(
                    f"""
                    SELECT record_key, text, engine, low_confidence
                    FROM archive_transcripts
                    WHERE archive_id = ? AND record_key IN ({placeholders})
                    """,
                    (archive_id, *batch),
                ).fetchall()
                for row in rows:
                    by_key[str(row["record_key"])] = RecordTranscript(
                        text=str(row["text"]),
                        engine=str(row["engine"]),
                        low_confidence=bool(row["low_confidence"]),
                    )
        if not by_key:
            return records
        return tuple(
            record
            if record.record_key not in by_key
            else replace(record, transcript=by_key[record.record_key])
            for record in records
        )

    def _with_sender_avatars(
        self, archive_id: str, records: tuple[StoredRecord, ...]
    ) -> tuple[StoredRecord, ...]:
        """Fill in each sender's avatar from the roster (PSU-2.47).

        **One query per page**, not a join in every read: five different reads
        produce records, and a join bolted onto each of them is five places for
        the same rule to drift. The lookup is keyed by (account, uid), which is
        what the identity table's primary key already leads with.

        Resolved at read time rather than frozen onto the record, for the same
        reason the conversation's name is not on its messages: people change
        their picture, and the copy on a message from 2019 would keep the old
        one forever.
        """
        wanted = {
            (record.source_account_id or "", record.sender_uid)
            for record in records
            if record.sender_uid
        }
        if not wanted:
            return records
        found: dict[tuple[str, str], str] = {}
        ordered = sorted(wanted)
        placeholders = ", ".join("(?, ?)" for _ in ordered)
        parameters: list[object] = [
            value for account_uid in ordered for value in account_uid
        ]
        parameters.append(archive_id)
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""
                WITH wanted(source_account_id, uid) AS (VALUES {placeholders})
                SELECT identity.source_account_id, identity.uid,
                       identity.avatar_blob_id
                FROM wanted
                JOIN archive_identities AS identity
                  ON identity.source_account_id = wanted.source_account_id
                 AND identity.uid = wanted.uid
                WHERE identity.archive_id = ?
                """,
                parameters,
            ).fetchall()
        for row in rows:
            if row["avatar_blob_id"] is not None:
                found[(str(row["source_account_id"]), str(row["uid"]))] = str(
                    row["avatar_blob_id"]
                )
        if not found:
            return records
        return tuple(
            record
            if record.sender_uid is None
            or (record.source_account_id or "", record.sender_uid) not in found
            else replace(
                record,
                sender_avatar_blob_id=found[
                    (record.source_account_id or "", record.sender_uid)
                ],
            )
            for record in records
        )

    @staticmethod
    def _stored_record(row: sqlite3.Row) -> StoredRecord:
        return StoredRecord(
            record_key=str(row["record_key"]),
            conversation_id=str(row["conversation_id"]),
            created_at=int(row["created_at"]),
            content_text=str(row["content_text"]),
            content_sha256=str(row["content_sha256"]),
            server_sequence=int(row["server_sequence"]),
            sender_uid=_optional_text(row["sender_uid"]),
            sender_display_name=_optional_text(row["sender_display_name"]),
            is_self=None if row["is_self"] is None else bool(row["is_self"]),
            message_type=_optional_text(row["message_type"]),
            app_card_json=_optional_text(row["app_card_json"]),
            reply_to_key=_optional_text(row["reply_to_key"]),
            source_sequence=int(row["source_sequence"]),
            source_local_id=int(row["source_local_id"]),
            attachment_refs=_decode_attachment_refs(row["attachment_refs"]),
            source_account_id=_optional_text(row["source_account_id"]),
        )

    def list_records(
        self,
        archive_id: str,
        *,
        after_server_sequence: int,
        limit: int,
    ) -> RecordPage:
        if after_server_sequence < 0 or limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid record cursor or limit")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND server_sequence > ?
                ORDER BY server_sequence, record_key
                LIMIT ?
                """,
                (archive_id, after_server_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        records = tuple(self._stored_record(row) for row in selected)
        next_sequence = after_server_sequence if not records else records[-1].server_sequence
        return RecordPage(records, next_sequence, has_more)

    def list_conversations(
        self,
        archive_id: str,
        *,
        after_conversation_id: str = "",
        limit: int,
        source_account_id: str | None = None,
    ) -> ConversationPage:
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid conversation limit")
        if source_account_id == "":
            raise MetadataValidationError("account filter must be non-empty")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            # Still driven by the records, joined to the names — not the other
            # way round. A conversation the name stream has not reached yet
            # (a v1 sender, or a backfill still running) has records and no
            # row in ``archive_conversations``; driving from the name table
            # would make it disappear from the list entirely, which is a
            # worse lie than showing its opaque id.
            if source_account_id is None:
                # The unscoped list keeps its pre-v3 shape: one row per
                # conversation_id, however many accounts share the id. The
                # summary's account is filled only when exactly one attributed
                # account owns the rows — anything else would assign a merged
                # row to somebody. Names are per-account now, so the join
                # picks the most recently renamed row; on a single-account
                # archive that is the one row there is.
                rows = connection.execute(
                    """
                    SELECT r.conversation_id AS conversation_id,
                           COUNT(*) AS message_count,
                           MAX(r.created_at) AS last_created_at,
                           MAX(r.server_sequence) AS last_server_sequence,
                           CASE WHEN COUNT(DISTINCT r.source_account_id) = 1
                                THEN MIN(r.source_account_id) END
                               AS source_account_id,
                           p.last_message_type AS last_message_type,
                           p.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_records AS r
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, display_name,
                               is_group, MAX(source_updated_at)
                        FROM archive_conversations
                        GROUP BY archive_id, conversation_id
                    ) AS c
                        ON c.archive_id = r.archive_id
                       AND c.conversation_id = r.conversation_id
                    -- 合并行取「最近改过的那一行」,与上面取会话名的子查询同一种
                    -- 写法：恰好一个 MAX 聚合,裸列才有定义。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, contact_uid,
                               avatar_blob_id, announcement, my_group_alias,
                               MAX(source_updated_at)
                        FROM archive_conversation_profiles
                        GROUP BY archive_id, conversation_id
                    ) AS pf
                        ON pf.archive_id = r.archive_id
                       AND pf.conversation_id = r.conversation_id
                    -- 预览与会话名一样,取「最近活跃的那个账号的最后一条」。
                    -- 恰好一个 MAX 聚合的子查询是 SQLite 保证裸列取自极值行的
                    -- 唯一形状,与上面那个取会话名的子查询同一条规则。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, last_message_type,
                               last_content_text, MAX(last_created_at)
                        FROM archive_conversation_state
                        GROUP BY archive_id, conversation_id
                    ) AS p
                        ON p.archive_id = r.archive_id
                       AND p.conversation_id = r.conversation_id
                    WHERE r.archive_id = ? AND r.conversation_id > ?
                    GROUP BY r.conversation_id
                    ORDER BY r.conversation_id
                    LIMIT ?
                    """,
                    (archive_id, after_conversation_id, limit + 1),
                ).fetchall()
            else:
                rows = connection.execute(
                    """
                    SELECT r.conversation_id AS conversation_id,
                           COUNT(*) AS message_count,
                           MAX(r.created_at) AS last_created_at,
                           MAX(r.server_sequence) AS last_server_sequence,
                           r.source_account_id AS source_account_id,
                           p.last_message_type AS last_message_type,
                           p.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_records AS r
                    LEFT JOIN archive_conversations AS c
                        ON c.archive_id = r.archive_id
                       AND c.source_account_id = r.source_account_id
                       AND c.conversation_id = r.conversation_id
                    -- 通讯录那一侧的几样（PSU-2.47）。主键直取。
                    LEFT JOIN archive_conversation_profiles AS pf
                        ON pf.archive_id = r.archive_id
                       AND pf.source_account_id = r.source_account_id
                       AND pf.conversation_id = r.conversation_id
                    -- 主键直取:状态表这一行就是这一组的物化,预览随它走。
                    LEFT JOIN archive_conversation_state AS p
                        ON p.archive_id = r.archive_id
                       AND pf.source_account_id = r.source_account_id
                       AND pf.conversation_id = r.conversation_id
                    WHERE r.archive_id = ? AND r.source_account_id = ?
                      AND r.conversation_id > ?
                    GROUP BY r.conversation_id
                    ORDER BY r.conversation_id
                    LIMIT ?
                    """,
                    (archive_id, source_account_id, after_conversation_id, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        # 同一个映射,两种走法。行的形状不能因为走的是哪条游标而不同——
        # 「读的人分不出这一行是哪个游标取来的」是这两个端点的约定。
        conversations = tuple(self._state_conversation(row) for row in rows[:limit])
        next_id = None if not conversations else conversations[-1].conversation_id
        return ConversationPage(conversations, next_id, has_more)

    @staticmethod
    def _state_conversation(
        row: sqlite3.Row, *, member_count: int | None = None
    ) -> ConversationSummary:
        """One browse-order list line, off the materialised state.

        The preview is computed here, not stored: the state table keeps the
        last record's *type and raw text*, and the sentence a list row shows is
        derived from them by the one function both ends share
        (`domain/preview.conversation_preview`). Storing the sentence instead
        would freeze the wording into 614 rows and make changing it a
        migration. The remote has no attachment file names (they are not part
        of the uploaded bytes), so `[文件]` arrives without one — the same
        honest gap the local end fills and this one cannot.
        """
        message_type = _optional_text(row["last_message_type"])
        return ConversationSummary(
            conversation_id=str(row["conversation_id"]),
            message_count=int(row["message_count"]),
            last_created_at=int(row["last_created_at"]),
            last_server_sequence=int(row["last_server_sequence"]),
            display_name=_optional_text(row["display_name"]),
            is_group=None if row["is_group"] is None else bool(row["is_group"]),
            source_account_id=_optional_text(row["source_account_id"]),
            last_message_type=message_type,
            last_message_preview=conversation_preview(
                message_type,
                _optional_text(row["last_content_text"]),
                None,
            ),
            # 通讯录那一侧才知道的几样（PSU-2.47）。没有 roster 时全是 None——
            # 界面据此退回首字母头像、不画抽屉，而不是印一个空抽屉。
            avatar_blob_id=_optional_text(row["avatar_blob_id"]),
            announcement=_optional_text(row["announcement"]),
            my_group_alias=_optional_text(row["my_group_alias"]),
            # 成员数只有详情读会去数：列表一页 100 行,每行一次 COUNT 是在为一个
            # 列表上根本不显示的数字付钱。
            member_count=member_count,
        )

    def list_conversations_by_recency(
        self,
        archive_id: str,
        *,
        after: ConversationPosition | None,
        limit: int,
        source_account_id: str | None = None,
        query: str | None = None,
    ) -> RecentConversationPage:
        """The conversation list as a person reads it: most recently active first.

        Ordered by ``(last_created_at DESC, conversation_id ASC)`` — the same
        rule as the local list (``query.py`` ``ORDER BY s.last_message_at DESC,
        c.id ASC``), down to the ascending tie-break, so the two ends walk one
        sequence. Because the two columns run in opposite directions the keyset
        cannot be a row-value comparison; it is spelled out instead.

        ``query`` matches the conversation's name, and its opaque id — because
        the id is what the list *displays* when the name stream has not reached
        that conversation, and a row you can see has to be a row you can find.
        The local list additionally matches contact remarks and group members;
        this end has neither, so it matches on less rather than pretending the
        two searches are the same one.
        """
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid conversation limit")
        if source_account_id == "":
            raise MetadataValidationError("account filter must be non-empty")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conditions: list[str] = ["s.archive_id = ?"]
            parameters: list[object] = [archive_id]
            # 带账号过滤时成员那一趟也要限在同一个账号里；不带过滤时不能限——
            # 那会把另一个账号的群成员从匹配里摘掉，而这一行本来就是合并行。
            account_scope = (
                "AND m.source_account_id = s.source_account_id"
                if source_account_id is not None
                else ""
            )
            if source_account_id is not None:
                conditions.append("s.source_account_id = ?")
                parameters.append(source_account_id)
            if query:
                pattern = f"%{escape_like(query)}%"
                conditions.append(
                    _NAME_FILTER_SQL.format(
                        scope=account_scope,
                        contact=(
                            _CONTACT_MATCH_JOINED_SQL
                            if source_account_id is not None
                            else _CONTACT_MATCH_EXISTS_SQL
                        ),
                    )
                )
                # 与本机 `conversations_page` 同一套 OR 条件、同一个 `(pattern,) * n`
                # 的写法（`query.py:660-687`）：一处改了另一处会立刻显出来。
                parameters.extend((pattern,) * _NAME_FILTER_PATTERNS)
            # The rolled-up list compares the *aggregate* activity time, so the
            # keyset has to name it by its select alias rather than by the column.
            # Same predicate, same three parameters, one column expression apart.
            def keyset_on(column: str) -> str:
                return f"({column} < ? OR ({column} = ? AND s.conversation_id > ?))"

            keyset_parameters: list[object] = []
            if after is not None:
                keyset_parameters = [
                    after.last_created_at,
                    after.last_created_at,
                    after.conversation_id,
                ]
            keyset = "" if after is None else keyset_on("s.last_created_at")
            rolled_up_keyset = "" if after is None else keyset_on("last_created_at")
            if source_account_id is not None:
                where = " AND ".join([*conditions, *([keyset] if keyset else [])])
                rows = connection.execute(
                    f"""
                    SELECT s.conversation_id AS conversation_id,
                           s.message_count AS message_count,
                           s.last_created_at AS last_created_at,
                           s.last_server_sequence AS last_server_sequence,
                           s.source_account_id AS source_account_id,
                           s.last_message_type AS last_message_type,
                           s.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_conversation_state AS s
                    LEFT JOIN archive_conversations AS c
                        ON c.archive_id = s.archive_id
                       AND c.source_account_id = s.source_account_id
                       AND c.conversation_id = s.conversation_id
                    -- 通讯录那一侧的几样（PSU-2.47）。主键直取。
                    LEFT JOIN archive_conversation_profiles AS pf
                        ON pf.archive_id = s.archive_id
                       AND pf.source_account_id = s.source_account_id
                       AND pf.conversation_id = s.conversation_id
                    -- 单聊对面那个人的身份，名字过滤要匹配它的名字与备注。
                    LEFT JOIN archive_identities AS ci
                        ON ci.archive_id = s.archive_id
                       AND ci.uid = pf.contact_uid
                       AND ci.source_account_id = s.source_account_id
                    WHERE {where}
                    ORDER BY s.last_created_at DESC, s.conversation_id ASC
                    LIMIT ?
                    """,
                    (*parameters, *keyset_parameters, limit + 1),
                ).fetchall()
            else:
                # One row per conversation however many accounts share the id,
                # exactly as the unscoped keyset list has always answered. The
                # account is filled only when a single attributed account owns
                # the row: ``COUNT(DISTINCT …)`` skips the '' that stands for
                # unattributed, so a conversation whose rows are one account
                # plus some pre-scoping ones still names that account.
                rows = connection.execute(
                    f"""
                    SELECT s.conversation_id AS conversation_id,
                           SUM(s.message_count) AS message_count,
                           MAX(s.last_created_at) AS last_created_at,
                           MAX(s.last_server_sequence) AS last_server_sequence,
                           CASE WHEN COUNT(DISTINCT NULLIF(s.source_account_id, '')) = 1
                                THEN MIN(NULLIF(s.source_account_id, '')) END
                               AS source_account_id,
                           p.last_message_type AS last_message_type,
                           p.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_conversation_state AS s
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, display_name,
                               is_group, MAX(source_updated_at)
                        FROM archive_conversations
                        GROUP BY archive_id, conversation_id
                    ) AS c
                        ON c.archive_id = s.archive_id
                       AND c.conversation_id = s.conversation_id
                    -- 合并行取「最近改过的那一行」,与上面取会话名的子查询同一种
                    -- 写法：恰好一个 MAX 聚合,裸列才有定义。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, contact_uid,
                               avatar_blob_id, announcement, my_group_alias,
                               MAX(source_updated_at)
                        FROM archive_conversation_profiles
                        GROUP BY archive_id, conversation_id
                    ) AS pf
                        ON pf.archive_id = s.archive_id
                       AND pf.conversation_id = s.conversation_id
                    -- 单聊对面那个人的身份**不联进来**：不带账号过滤时同一个 uid
                    -- 在每个账号下各有一行，联上就把状态行乘了账号数，下面的 SUM
                    -- 跟着翻倍。名字过滤改用 EXISTS 问（`_CONTACT_MATCH_EXISTS_SQL`）。
                    -- 合并行的预览取「最近活跃的那个账号的最后一条」。写成
                    -- **恰好一个** MAX 聚合的子查询,是因为 SQLite 只在这种
                    -- 形状下保证裸列取自达到极值的那一行;上面那个取会话名的
                    -- 子查询用的是同一条规则、同一种写法。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, last_message_type,
                               last_content_text, MAX(last_created_at)
                        FROM archive_conversation_state
                        GROUP BY archive_id, conversation_id
                    ) AS p
                        ON p.archive_id = s.archive_id
                       AND p.conversation_id = s.conversation_id
                    WHERE {" AND ".join(conditions)}
                    GROUP BY s.conversation_id
                    {f"HAVING {rolled_up_keyset}" if rolled_up_keyset else ""}
                    ORDER BY last_created_at DESC, s.conversation_id ASC
                    LIMIT ?
                    """,
                    (*parameters, *keyset_parameters, limit + 1),
                ).fetchall()
        has_more = len(rows) > limit
        conversations = tuple(self._state_conversation(row) for row in rows[:limit])
        next_position = None
        if conversations:
            last = conversations[-1]
            next_position = ConversationPosition(
                last_created_at=last.last_created_at,
                conversation_id=last.conversation_id,
            )
        return RecentConversationPage(conversations, next_position, has_more)

    def get_conversation(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
    ) -> ConversationSummary | None:
        """One conversation by id — the identity read, PSU-2.44 (record model v6).

        Until this existed the only way to learn a conversation's name was to
        find it in a page of the list, which made the name depend on which
        pages the reader had loaded: change the filter while a conversation is
        open and its title degraded to the opaque id, because the query key had
        changed and the row was in another cache. The local archive has always
        had ``/api/v1/conversations/{id}`` for exactly this; this is the same
        endpoint, not a new mechanism.

        The row is the *same shape* the list answers with, off the same
        materialised state and through the same mapping — a reader must not be
        able to tell whether a title came from a page or from here.

        ``None`` means this archive has no such conversation in the requested
        scope. That is a different statement from "it has no name yet", which
        is a summary whose ``display_name`` is ``None``.
        """
        if source_account_id == "":
            raise MetadataValidationError("account filter must be non-empty")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            if source_account_id is not None:
                row = connection.execute(
                    """
                    SELECT s.conversation_id AS conversation_id,
                           s.message_count AS message_count,
                           s.last_created_at AS last_created_at,
                           s.last_server_sequence AS last_server_sequence,
                           s.source_account_id AS source_account_id,
                           s.last_message_type AS last_message_type,
                           s.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_conversation_state AS s
                    LEFT JOIN archive_conversations AS c
                        ON c.archive_id = s.archive_id
                       AND c.source_account_id = s.source_account_id
                       AND c.conversation_id = s.conversation_id
                    -- 通讯录那一侧的几样（PSU-2.47）。主键直取。
                    LEFT JOIN archive_conversation_profiles AS pf
                        ON pf.archive_id = s.archive_id
                       AND pf.source_account_id = s.source_account_id
                       AND pf.conversation_id = s.conversation_id
                    -- 单聊对面那个人的身份，名字过滤要匹配它的名字与备注。
                    LEFT JOIN archive_identities AS ci
                        ON ci.archive_id = s.archive_id
                       AND ci.uid = pf.contact_uid
                       AND ci.source_account_id = s.source_account_id
                    WHERE s.archive_id = ? AND s.source_account_id = ?
                      AND s.conversation_id = ?
                    """,
                    (archive_id, source_account_id, conversation_id),
                ).fetchone()
            else:
                # 与不带过滤的列表同一种合并：一行,不论几个账号共用这个标识。
                row = connection.execute(
                    """
                    SELECT s.conversation_id AS conversation_id,
                           SUM(s.message_count) AS message_count,
                           MAX(s.last_created_at) AS last_created_at,
                           MAX(s.last_server_sequence) AS last_server_sequence,
                           CASE WHEN COUNT(DISTINCT NULLIF(s.source_account_id, '')) = 1
                                THEN MIN(NULLIF(s.source_account_id, '')) END
                               AS source_account_id,
                           p.last_message_type AS last_message_type,
                           p.last_content_text AS last_content_text,
                           pf.avatar_blob_id AS avatar_blob_id,
                           pf.announcement AS announcement,
                           pf.my_group_alias AS my_group_alias,
                           c.display_name AS display_name,
                           c.is_group AS is_group
                    FROM archive_conversation_state AS s
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, display_name,
                               is_group, MAX(source_updated_at)
                        FROM archive_conversations
                        GROUP BY archive_id, conversation_id
                    ) AS c
                        ON c.archive_id = s.archive_id
                       AND c.conversation_id = s.conversation_id
                    -- 合并行取「最近改过的那一行」,与上面取会话名的子查询同一种
                    -- 写法：恰好一个 MAX 聚合,裸列才有定义。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, contact_uid,
                               avatar_blob_id, announcement, my_group_alias,
                               MAX(source_updated_at)
                        FROM archive_conversation_profiles
                        GROUP BY archive_id, conversation_id
                    ) AS pf
                        ON pf.archive_id = s.archive_id
                       AND pf.conversation_id = s.conversation_id
                    -- 这里没有名字过滤，联系人那一行不联：不带账号时它会把
                    -- 状态行乘上账号数，下面的 SUM 跟着翻倍。
                    LEFT JOIN (
                        SELECT archive_id, conversation_id, last_message_type,
                               last_content_text, MAX(last_created_at)
                        FROM archive_conversation_state
                        GROUP BY archive_id, conversation_id
                    ) AS p
                        ON p.archive_id = s.archive_id
                       AND p.conversation_id = s.conversation_id
                    WHERE s.archive_id = ? AND s.conversation_id = ?
                    GROUP BY s.conversation_id
                    """,
                    (archive_id, conversation_id),
                ).fetchone()
        if row is None:
            return None
        return self._state_conversation(
            row, member_count=self._member_count(archive_id, conversation_id, source_account_id)
        )

    def _member_count(
        self, archive_id: str, conversation_id: str, source_account_id: str | None
    ) -> int | None:
        """How many members this conversation's roster names, or ``None`` for no roster.

        ``0`` and ``None`` are different sentences: zero would claim "this group
        is empty", which no group is — it means the roster has not arrived.
        """
        conditions = ["archive_id = ?", "conversation_id = ?"]
        parameters: list[object] = [archive_id, conversation_id]
        if source_account_id is not None:
            conditions.append("source_account_id = ?")
            parameters.append(source_account_id)
        with closing(self._connect()) as connection:
            total = connection.execute(
                f"SELECT COUNT(*) FROM archive_conversation_members "
                f"WHERE {' AND '.join(conditions)}",
                parameters,
            ).fetchone()[0]
        return None if total == 0 else int(total)

    def list_conversation_messages(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after_server_sequence: int,
        limit: int,
        source_account_id: str | None = None,
    ) -> MessagePage:
        if after_server_sequence < 0 or limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid message cursor or limit")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            account_filter, account_parameters = self._account_filter(source_account_id)
            rows = connection.execute(
                f"""
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND conversation_id = ?
                  AND server_sequence > ?{account_filter}
                ORDER BY server_sequence, record_key
                LIMIT ?
                """,
                (
                    archive_id,
                    conversation_id,
                    after_server_sequence,
                    *account_parameters,
                    limit + 1,
                ),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        records = self._read_shaped(
            archive_id, tuple(self._stored_record(row) for row in selected)
        )
        next_sequence = after_server_sequence if not records else records[-1].server_sequence
        return MessagePage(records, next_sequence, has_more)

    @staticmethod
    def _account_filter(source_account_id: str | None) -> tuple[str, tuple[str, ...]]:
        """The optional v3 account predicate, shared by every scoped read."""
        if source_account_id is None:
            return "", ()
        if not source_account_id:
            raise MetadataValidationError("account filter must be non-empty")
        return " AND source_account_id = ?", (source_account_id,)

    def list_conversation_messages_in_time_order(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        after: MessagePosition | None,
        limit: int,
        source_account_id: str | None = None,
    ) -> TimeOrderedMessagePage:
        """Page a conversation the way it is read, not the way it was uploaded.

        The four-column keyset is compared as a tuple in one predicate. Written
        as the equivalent chain of ORs it is the same rows, but SQLite will not
        drive it off ``ix_archive_records_reading_order`` — and at 729k records
        that is the difference between a seek and a scan.
        """
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid message cursor or limit")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            account_filter, account_parameters = self._account_filter(source_account_id)
            parameters: list[object] = [archive_id, conversation_id, *account_parameters]
            keyset = ""
            if after is not None:
                if (
                    after.created_at < 0
                    or after.source_sequence < 0
                    or after.source_local_id < 0
                    or not after.record_key
                ):
                    raise MetadataValidationError("invalid message cursor or limit")
                keyset = (
                    " AND (created_at, source_sequence, source_local_id, record_key) > (?, ?, ?, ?)"
                )
                parameters.extend(
                    (
                        after.created_at,
                        after.source_sequence,
                        after.source_local_id,
                        after.record_key,
                    )
                )
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND conversation_id = ?{account_filter}{keyset}
                ORDER BY created_at, source_sequence, source_local_id, record_key
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        records = self._read_shaped(
            archive_id, tuple(self._stored_record(row) for row in rows[:limit])
        )
        next_position = None
        if records:
            last = records[-1]
            next_position = MessagePosition(
                created_at=last.created_at,
                source_sequence=last.source_sequence,
                source_local_id=last.source_local_id,
                record_key=last.record_key,
            )
        return TimeOrderedMessagePage(records, next_position, has_more)

    def list_conversation_messages_before(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        before: MessagePosition | None,
        limit: int,
        source_account_id: str | None = None,
    ) -> TimeOrderedMessagePage:
        """A slice ending at the newest message, mirroring ``query.messages_page``.

        The first request (``before=None``) selects the newest ``limit``
        records, so a transcript opens where the conversation currently is
        rather than where it started. ``next_position`` is anchored to the
        **oldest** record of the slice and therefore asks for an earlier one
        next; items stay ascending inside the page so the renderer never
        reorders a page, and the client only reverses page order when it joins
        several. Every one of those sentences is also true of the local
        archive's own method — that is the point.
        """
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid message cursor or limit")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            account_filter, account_parameters = self._account_filter(source_account_id)
            parameters: list[object] = [archive_id, conversation_id, *account_parameters]
            keyset = ""
            if before is not None:
                if (
                    before.created_at < 0
                    or before.source_sequence < 0
                    or before.source_local_id < 0
                    or not before.record_key
                ):
                    raise MetadataValidationError("invalid message cursor or limit")
                keyset = (
                    " AND (created_at, source_sequence, source_local_id, record_key) < (?, ?, ?, ?)"
                )
                parameters.extend(
                    (
                        before.created_at,
                        before.source_sequence,
                        before.source_local_id,
                        before.record_key,
                    )
                )
            parameters.append(limit + 1)
            rows = connection.execute(
                f"""
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND conversation_id = ?{account_filter}{keyset}
                ORDER BY created_at DESC, source_sequence DESC,
                         source_local_id DESC, record_key DESC
                LIMIT ?
                """,
                parameters,
            ).fetchall()
        has_more = len(rows) > limit
        records = self._read_shaped(
            archive_id,
            tuple(reversed([self._stored_record(row) for row in rows[:limit]])),
        )
        next_position = None
        if has_more and records:
            oldest = records[0]
            next_position = MessagePosition(
                created_at=oldest.created_at,
                source_sequence=oldest.source_sequence,
                source_local_id=oldest.source_local_id,
                record_key=oldest.record_key,
            )
        return TimeOrderedMessagePage(records, next_position, has_more)

    def get_conversation_message(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        record_key: str,
        source_account_id: str | None = None,
    ) -> StoredRecord | None:
        """One record by key, or None if this conversation does not hold it.

        A primary-key seek. The row comes back whole because the caller wants
        two things from it that must not disagree: the message itself, and the
        four columns naming where it sits on the reading line.

        The conversation and the account are predicates, not lookups. A key
        that exists in a *different* conversation answers None — the caller
        asked for the context in this one, and the honest answer there is that
        there is none.
        """
        if not record_key:
            raise MetadataValidationError("anchor record key must be non-empty")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            account_filter, account_parameters = self._account_filter(source_account_id)
            row = connection.execute(
                f"""
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND record_key = ?
                  AND conversation_id = ?{account_filter}
                """,
                (archive_id, record_key, conversation_id, *account_parameters),
            ).fetchone()
        if row is None:
            return None
        return self._read_shaped(archive_id, (self._stored_record(row),))[0]

    def search(
        self,
        archive_id: str,
        query: str,
        *,
        limit: int,
        source_account_id: str | None = None,
    ) -> tuple[StoredRecord, ...]:
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid search limit")
        normalized = normalize_search_text(query).strip()
        if not normalized:
            return ()
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            account_filter, account_parameters = self._account_filter(source_account_id)
            rows = connection.execute(
                f"""
                SELECT record_key, conversation_id, created_at, content_text,
                       content_sha256, server_sequence, sender_uid,
                       sender_display_name, is_self, message_type, app_card_json,
                       reply_to_key, source_sequence, source_local_id,
                       attachment_refs, source_account_id
                FROM archive_records
                WHERE archive_id = ? AND instr(normalized_content, ?) > 0{account_filter}
                ORDER BY created_at DESC, record_key
                LIMIT ?
                """,
                (archive_id, normalized, *account_parameters, limit),
            ).fetchall()
        return self._read_shaped(
            archive_id, tuple(self._stored_record(row) for row in rows)
        )

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
    ) -> RecordSearchPage:
        """Search the way the local end searches (PSU-2.45, record model v7).

        The matching is the local index's — `MessageSearchIndex` run unchanged
        over this archive's records. What comes back from it is a set of
        **identities**; the rows themselves are re-read from `archive_records`,
        so a search result and a transcript row are the same shape down to the
        last field.
        """
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid search limit")
        if source_account_id == "":
            raise MetadataValidationError("account filter must be non-empty")
        with closing(self._connect()) as connection:
            # 这个方法本来就要开好几条连接（账号快照、统计、回读命中行各一条），
            # 翻译单开一条与它现有的形状一致；不像下面那几个有现成的连接可搭。
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            # 这里的会话是**可选过滤**，不是必填路径段——不带就不翻。
            if conversation_id is not None:
                conversation_id = self._as_read_conversation(
                    connection, archive_id, conversation_id
                )
        # 没有关键词时走浏览分页，与本机 `search_page` 一字不差：只按日期筛、
        # 只按类型筛都是本机搜索页的正常用法。什么都不给就是「整个归档，最近的
        # 在前」——那也正是本机答的东西。两端同形要一路同到这种边角上，
        # 否则「同一份前端」在这里就得多一段判断。
        if source_account_id is not None:
            accounts: list[str | None] = [source_account_id]
        else:
            # 不带过滤时问遍每一条上传流，外加未归属的那一个空账号——那些行属于
            # 没有人，但它们**在这个归档里**，搜不到它们等于让它们消失。
            snapshot = self.list_account_summaries(archive_id)
            accounts = [item.source_account_id for item in snapshot.accounts]
            if snapshot.unattributed_count > 0:
                accounts.append(None)
        result = self._search.search(
            archive_id,
            query,
            accounts=accounts,
            last_server_sequence=self.stats(archive_id).last_server_sequence,
            conversation_id=conversation_id,
            sender_uid=sender_uid,
            message_types=message_types,
            has_attachment=has_attachment,
            date_from=date_from,
            date_to=date_to,
            after=after,
            limit=limit,
        )
        return RecordSearchPage(
            records=self._records_by_key(archive_id, result.record_keys),
            next_after=result.next_after,
            total_count=result.total_count,
            conversation_counts=result.conversation_counts,
            index_status=result.index_status,
        )

    def _records_by_key(
        self, archive_id: str, record_keys: tuple[str, ...]
    ) -> tuple[StoredRecord, ...]:
        """Re-read the hit rows from the archive, in the order the index gave them.

        The index keeps its own copy of the content so it can match on it; it is
        not the record. Reading the row back means a hit carries exactly what a
        transcript row carries — including the fields the index has no column
        for (the app card, the reply target, the attachment ids).

        A key the index knows and the archive does not is **dropped, silently
        and on purpose**: the two are separate databases and the pair is only
        eventually consistent, so a hit whose row is gone is a stale index
        entry, not a record to invent.
        """
        if not record_keys:
            return ()
        found: dict[str, StoredRecord] = {}
        with closing(self._connect()) as connection:
            for offset in range(0, len(record_keys), 400):
                batch = record_keys[offset : offset + 400]
                placeholders = ", ".join("?" for _ in batch)
                for row in connection.execute(
                    f"""
                    SELECT record_key, conversation_id, created_at, content_text,
                           content_sha256, server_sequence, sender_uid,
                           sender_display_name, is_self, message_type, app_card_json,
                           reply_to_key, source_sequence, source_local_id,
                           attachment_refs, source_account_id
                    FROM archive_records
                    WHERE archive_id = ? AND record_key IN ({placeholders})
                    """,
                    (archive_id, *batch),
                ).fetchall():
                    record = self._stored_record(row)
                    found[record.record_key] = record
        return self._read_shaped(
            archive_id, tuple(found[key] for key in record_keys if key in found)
        )

    # ------------------------------------------------- 账号归并（PSU-2.86）

    @staticmethod
    def _read_account_id(
        connection: sqlite3.Connection, archive_id: str, source_account_id: str
    ) -> str:
        """这条上传流的**阅读身份**：它自己，或它所在那一组的 primary。

        上传身份与阅读身份必须分开，这是整个归并的中心约束。请求路径上那个 id
        说的是"哪台机器的哪条流"，`device_cursors` 与 `sync_batches` 的 1..N
        连续性判据挂在它上面，一个字都不能改；读侧要回答的却是"这是哪个微信"，
        而两台机器采集同一个微信时那必须是同一个答案。这个函数是两者之间唯一的
        那道翻译，`commit_batch` 一进门就过它，此后该批次的所有读模型写入都用
        翻译后的那个。
        """
        row = connection.execute(
            "SELECT primary_account_id FROM archive_account_links "
            "WHERE archive_id = ? AND source_account_id = ?",
            (archive_id, source_account_id),
        ).fetchone()
        return source_account_id if row is None else str(row["primary_account_id"])

    def _as_read_account(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        source_account_id: str | None,
    ) -> str | None:
        """把请求里的 id 换成阅读身份——**读侧**的那一道翻译（PSU-2.90）。

        写侧早就有这道翻译（`commit_batch` 一进门就过 `_read_account_id` 与
        `_read_conversation_ids`），读侧没有。于是 PSU-2.86 归并之后，指向败者 id
        的书签与深链一律答空：远端深链把账号钉在 URL 上（远端会话主键是
        「归档 + 账号 + 会话」，PSU-2.35a 为 `filehelper` 跨账号撞号改的），
        归并之前存下的每一个都带着一个现在谁也不拥有的 id。**空列表在界面上与
        「这个账号真的没有消息」长得一模一样**，这是 PSU-2.89 那类错的另一件外衣。

        翻译写在存储层而不是路由上：服务端有两个读面（设备认证的 `query/…` 与
        浏览器 BFF 的 `remote/…`），两者落到的是同一批方法。写在路由上就是同一段
        逻辑抄两遍，而那正是这个仓库一直在还的债。

        用**调用方自己的连接**，不另开一条：实测开一条连接 323 µs，而这两次点查
        加起来 80 µs——为省一次重构多付四倍，说不过去。

        `None` 原样返回，一次查询都不发：不带账号过滤是列表页的默认形态。

        会话那一维是 `_as_read_conversation`。**分成两个而不是返回一对**：会话 id
        在读方法上是必填位置参数（声明就是 `str`），把它和可空的账号挤进一个返回
        元组，类型就只能松成 `str | None`，然后在六个调用点上各撒一次谎。
        """
        if source_account_id is None:
            return None
        return self._read_account_id(connection, archive_id, source_account_id)

    def _as_read_conversation(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        conversation_id: str,
    ) -> str:
        """会话 id 的阅读身份。见 `_as_read_account`——同一道翻译的另一维。"""
        return self._read_conversation_ids(connection, archive_id, (conversation_id,))[
            conversation_id
        ]

    @staticmethod
    def _read_conversation_ids(
        connection: sqlite3.Connection,
        archive_id: str,
        conversation_ids: Iterable[str],
    ) -> dict[str, str]:
        """每个会话 id 的**阅读身份**：它自己，或它所在那一组的 primary。

        `_read_account_id` 在会话这一维的同一道翻译。`commit_batch` 与
        `publish_roster` 一进门就过它，此后记录、会话名、状态行、成员、资料与索引
        全部按翻译后的 id 落地——败者会话归并过一次之后，那条流再发来的东西不会
        再在旧 id 下面重新长出一条会话（2026-09-02 真机上就是这样复活的）。

        答案对每一个问到的 id 都有：没有链接的照旧是它自己，调用方直接下标、不必
        再兜底。链接表在真机上是个位数行，问一批就是一次 `IN` 查找。
        """
        wanted = tuple(dict.fromkeys(conversation_ids))
        read_ids = {conversation_id: conversation_id for conversation_id in wanted}
        for start in range(0, len(wanted), _LINK_LOOKUP_CHUNK):
            chunk = wanted[start : start + _LINK_LOOKUP_CHUNK]
            placeholders = ",".join("?" * len(chunk))
            for row in connection.execute(
                "SELECT conversation_id, primary_conversation_id "
                "FROM archive_conversation_links "
                f"WHERE archive_id = ? AND conversation_id IN ({placeholders})",
                (archive_id, *chunk),
            ):
                read_ids[str(row["conversation_id"])] = str(
                    row["primary_conversation_id"]
                )
        return read_ids

    @staticmethod
    def _elect_account_primary(
        connection: sqlite3.Connection,
        archive_id: str,
        source_account_id: str,
        account_key: str,
    ) -> tuple[str, tuple[str, ...]]:
        """记下这条流采集的是哪个微信，重新选举这一组的 primary。

        **选举取组内 `source_account_id` 的最小值，不取"最早的那条流"。**
        远端没有可靠的"最早"：发布可以乱序、可以重放，一条流的第一次发布也可能
        发生在另一条流已经传了半年之后。最小值在任何到达顺序下都收敛到同一个
        答案——两台机器谁先同步，最终态一样。代价是一条更小的 id 后到时老 primary
        也要跟着搬，那是一次有界的代价，换来"最终态与顺序无关"这条能写进用例的性质。

        `pending_merge` 只在 primary **真的变了**的那一行上置位。稳态下这一句一行
        都不动，于是后面那趟搬家整个跳过——没有它，一个已经并好的组会在每一轮
        sync 上重新扫一遍归档去确认"没有东西要搬"。
        """
        connection.execute(
            "INSERT INTO archive_account_links("
            "archive_id, source_account_id, account_key, primary_account_id"
            ") VALUES (?, ?, ?, ?) "
            "ON CONFLICT(archive_id, source_account_id) DO UPDATE SET "
            "account_key = excluded.account_key",
            (archive_id, source_account_id, account_key, source_account_id),
        )
        members = tuple(
            str(row["source_account_id"])
            for row in connection.execute(
                "SELECT source_account_id FROM archive_account_links "
                "WHERE archive_id = ? AND account_key = ? ORDER BY source_account_id",
                (archive_id, account_key),
            )
        )
        primary = members[0]
        connection.execute(
            "UPDATE archive_account_links SET "
            "pending_merge = CASE "
            "WHEN source_account_id <> ? AND primary_account_id <> ? THEN 1 "
            "ELSE pending_merge END, "
            "primary_account_id = ? "
            "WHERE archive_id = ? AND account_key = ?",
            (primary, primary, primary, archive_id, account_key),
        )
        pending = tuple(
            str(row["source_account_id"])
            for row in connection.execute(
                "SELECT source_account_id FROM archive_account_links "
                "WHERE archive_id = ? AND account_key = ? AND pending_merge = 1 "
                "AND source_account_id <> ? ORDER BY source_account_id",
                (archive_id, account_key, primary),
            )
        )
        SQLiteMetadataStore._warn_on_near_miss_account_key(
            connection, archive_id, source_account_id, account_key
        )
        return primary, pending

    @staticmethod
    def _warn_on_near_miss_account_key(
        connection: sqlite3.Connection,
        archive_id: str,
        source_account_id: str,
        account_key: str,
    ) -> None:
        """两个几乎一样的 key 落在同一个归档里，留一条痕（PSU-2.86 评审第 4 条）。

        `account_key` 的跨机确定性靠数据分布，不靠构造：本机取的是自己发出的消息里
        `usage_count` 最高的那个 `sender_uid`，而一个档案可以有多个 outgoing identity
        （改过名的账号、某个群里的别名）。两台机器的候选分布不同时，理论上会各选各的、
        **静默不并**。退化方向是安全的——不并等于今天的行为，不会把两个不同的微信错并
        到一起——但它没有任何外部表现：界面上就是"还是两个账号"，与"对方还没升级到 v11"
        长得一模一样。

        判据是"像但不等"，不是"都是单成员组"：一台机器上装着两个不同的微信本来就该给出
        两个不相干的 key，那是正确行为，为它报警就是把噪音当信号。裸 wxid 都以 `wxid_`
        开头（5 个字符），所以公共前缀要求到 8 才算"像"——再多三个字符的巧合约 1/46656。
        """
        others = [
            str(row["account_key"])
            for row in connection.execute(
                "SELECT DISTINCT account_key FROM archive_account_links "
                "WHERE archive_id = ? AND account_key <> ?",
                (archive_id, account_key),
            )
        ]
        for other in others:
            if not _looks_like_the_same_identity(account_key, other):
                continue
            _logger.warning(
                "archive %s: account key %r (stream %s) looks like %r but is not "
                "equal, so these two upload streams will not merge. One machine's "
                "outgoing identity walk picked a different sender_uid; the accounts "
                "stay separate, which is the safe direction but a silent one.",
                archive_id,
                account_key,
                source_account_id,
                other,
            )

    @staticmethod
    def _merge_account_tables(
        connection: sqlite3.Connection, archive_id: str, *, loser: str, primary: str
    ) -> None:
        """把五张读侧表里败者那一份并进 primary。

        对这五张表来说"改归属"其实是**合并**：它们的主键都含 `source_account_id`，
        而两台机器必然都有同一批联系人、同一个会话的行，直接 UPDATE 会撞唯一约束。
        （`archive_records` 不在其中：它的主键 `(archive_id, record_key)` 不含账号。）

        规则与 ingest 的 upsert 同一条口径——last-write-wins by `source_updated_at`。
        先删掉两边各自输掉的行，剩下的才整体搬过去，UPDATE 到那一步已经不可能撞。

        **平局留 primary**（第一句用 `>=`，第二句用 `>`）。这不只是要个确定性：
        它让重放变成真正的 no-op——同一次发布连发两遍，第二遍每一行都平局，
        于是一行都不动，行数与内容逐列不变。
        """
        for table, key_columns in _ACCOUNT_MERGE_TABLES:
            match = "".join(
                f" AND p.{column} = {table}.{column}" for column in key_columns
            )
            connection.execute(
                f"DELETE FROM {table} WHERE archive_id = ? AND source_account_id = ? "
                f"AND EXISTS (SELECT 1 FROM {table} AS p "
                f"WHERE p.archive_id = {table}.archive_id "
                f"AND p.source_account_id = ?{match} "
                f"AND p.source_updated_at >= {table}.source_updated_at)",
                (archive_id, loser, primary),
            )
            connection.execute(
                f"DELETE FROM {table} WHERE archive_id = ? AND source_account_id = ? "
                f"AND EXISTS (SELECT 1 FROM {table} AS p "
                f"WHERE p.archive_id = {table}.archive_id "
                f"AND p.source_account_id = ?{match} "
                f"AND p.source_updated_at > {table}.source_updated_at)",
                (archive_id, primary, loser),
            )
            connection.execute(
                f"UPDATE {table} SET source_account_id = ? "
                "WHERE archive_id = ? AND source_account_id = ?",
                (primary, archive_id, loser),
            )

    _RECORD_COLUMNS = (
        "record_key, conversation_id, created_at, content_text, "
        "content_sha256, server_sequence, sender_uid, sender_display_name, "
        "is_self, message_type, app_card_json, reply_to_key, "
        "source_sequence, source_local_id, attachment_refs, source_account_id"
    )

    def _move_records_chunk(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        keys: tuple[str, ...],
        *,
        set_column: str,
        old_value: str,
        new_value: str,
    ) -> tuple[sqlite3.Row, ...]:
        """搬一批记录、挪状态行、记账本——账号与会话两趟归并共用的这一块只写一份。

        `set_column` 只接受调用方写死的列名字面量，不接外部输入。返回搬完之后
        读出来的这一批行，调用方**放锁之后**拿它去写索引（`_index_moved_rows`）。

        状态行在**同一个事务**里按这一批挪（PSU-2.100，`_shift_conversation_state`）：
        这批行本来就要读出来，搬动量零额外读；崩在中间整批回滚。

        索引**不在这里写**（PSU-2.101）。它写的是另一个 sqlite 文件，不需要归档库的
        写锁，却曾占了一批持锁的 87%。原来把它夹在事务里是为了「崩在两步之间那一批
        还能重来」——现在这件事由账本 `search_rehome_pending` 负责：键与搬行同一个
        事务落地，索引写完才删；留在账本里的键下一趟按记录表现状整条重写。
        """
        placeholders = ",".join("?" * len(keys))
        connection.execute("BEGIN IMMEDIATE")
        try:
            connection.execute(
                f"UPDATE archive_records SET {set_column} = ? "
                f"WHERE archive_id = ? AND record_key IN ({placeholders})",
                (new_value, archive_id, *keys),
            )
            rows = tuple(
                connection.execute(
                    f"SELECT {self._RECORD_COLUMNS} FROM archive_records "
                    f"WHERE archive_id = ? AND record_key IN ({placeholders})",
                    (archive_id, *keys),
                ).fetchall()
            )
            self._shift_conversation_state(
                connection,
                archive_id=archive_id,
                rows=rows,
                set_column=set_column,
                old_value=old_value,
            )
            connection.executemany(
                "INSERT OR IGNORE INTO search_rehome_pending(archive_id, record_key) VALUES (?, ?)",
                ((archive_id, str(row["record_key"])) for row in rows),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return rows

    def _index_moved_rows(self, archive_id: str, rows: Sequence[sqlite3.Row]) -> bool:
        """搬完的这批行在索引里改归属，改成了的划掉账本；返回搜索库是不是让写了。

        **在写锁外面调，也只做锁外做得对的事**：`rehome_records` 只改会话列，
        那个值不会被并发提交改掉。改不了的键（索引里没 docid、索引行的账号与记录
        现状不一致）**留在账本上**，交给写锁里的 `_replay_rehome_ledger` 整条重写
        ——锁外整条重写读的是快照，会把并发提交刚写的索引盖回去（评审订正）。
        搜索库被别人握着（`locked/busy`）就返回 False，账本整批留着，不抛：
        一次抢不到不该带塌整轮。
        """
        if not rows:
            return True
        moved = tuple(self._stored_record(row) for row in rows)
        try:
            leftovers = set(self._search.rehome_records(archive_id, moved))
        except sqlite3.OperationalError as error:
            text = str(error)
            if "locked" not in text and "busy" not in text:
                raise
            return False
        done = tuple(
            str(row["record_key"]) for row in rows if str(row["record_key"]) not in leftovers
        )
        if done:
            self._forget_rehome_keys_locked(archive_id, done)
        return True

    def _forget_rehome_keys_locked(self, archive_id: str, keys: Sequence[str]) -> None:
        """短锁划账本。索引已经写成了，锁一时拿不到就再试两次——放弃的代价是
        下一趟白付一次整条重写，不是错。cli 里服务已停，锁不会忙。"""
        for attempt in range(3):
            try:
                with self._writer_lock, closing(self._connect()) as connection:
                    self._forget_rehome_keys(connection, archive_id, keys)
                return
            except MetadataBusyError:
                if attempt == 2:
                    raise

    @staticmethod
    def _forget_rehome_keys(
        connection: sqlite3.Connection, archive_id: str, keys: Sequence[str]
    ) -> None:
        """账本上划掉这些键。调用方持锁。"""
        connection.execute("BEGIN IMMEDIATE")
        try:
            for offset in range(0, len(keys), 500):
                chunk = tuple(keys[offset : offset + 500])
                placeholders = ",".join("?" * len(chunk))
                connection.execute(
                    "DELETE FROM search_rehome_pending "
                    f"WHERE archive_id = ? AND record_key IN ({placeholders})",
                    (archive_id, *chunk),
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise

    def _replay_rehome_ledger(self, archive_id: str, *, max_rows: int) -> tuple[int, bool]:
        """把账本里欠着的索引写还上一批；返回 `(还了多少条, 搜索库是不是被占)`。

        一律**整条重写**、按记录表现状，**在写锁里做**（评审订正）：记录不是不可变
        的——`commit_batch` 会 `ON CONFLICT DO UPDATE` 改渲染字段并顺手写索引，锁外
        用一份快照重写会把它刚写的盖回去。这条路只在崩溃 / 搜索库被占 / rehome 交还
        之后发生，持锁 1～2 s 可接受。账本不记这条键是哪种归并搬的，也不必：重写幂等。
        记录表里已经没有的键直接划掉，别让它把 `done` 永远钉在假上。
        """
        with self._writer_lock, closing(self._connect()) as connection:
            keys = tuple(
                str(row["record_key"])
                for row in connection.execute(
                    "SELECT record_key FROM search_rehome_pending "
                    "WHERE archive_id = ? ORDER BY record_key LIMIT ?",
                    (archive_id, max_rows),
                )
            )
            if not keys:
                return (0, False)
            placeholders = ",".join("?" * len(keys))
            rows = connection.execute(
                f"SELECT {self._RECORD_COLUMNS} FROM archive_records "
                f"WHERE archive_id = ? AND record_key IN ({placeholders})",
                (archive_id, *keys),
            ).fetchall()
            if rows:
                try:
                    self._search.index_records(
                        archive_id,
                        self._with_transcripts(
                            archive_id, tuple(self._stored_record(row) for row in rows)
                        ),
                    )
                except sqlite3.OperationalError as error:
                    text = str(error)
                    if "locked" not in text and "busy" not in text:
                        raise
                    return (0, True)
            self._forget_rehome_keys(connection, archive_id, keys)
        return (len(keys), False)

    def _drain_account_records(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        *,
        loser: str,
        primary: str,
    ) -> None:
        """把败者那条流的记录逐批搬到 primary 名下，并重排索引。

        **分批走主键的键集，而不是每批重新按账号过滤取前 N 条。**
        `archive_records` 上没有 `(archive_id, source_account_id)` 索引，只有主键
        `(archive_id, record_key)`；按账号过滤取前 N 条，每一批都要从头扫一遍，
        73 万行就是几百次全表扫。带上 `record_key > 上一批末尾` 之后，整趟合起来
        只是**一次**顺序扫描。

        每批一个事务。真机上要搬的通常只有几十行，但机制是通用的：大流的 id 字典序
        更大而小流后发布时，要搬的就是 73 万行。发布方对这个请求是吞错误、每轮 sync
        重发的，一次几分钟的长事务会变成每轮都重试一次几分钟的长事务。分批之后
        崩溃最多丢一批，下一次发布接着搬。

        索引必须在这里显式重排，**指望回填自愈是错的**：`backfill_search_chunk`
        按 `server_sequence > 水位` 前进，而搬家不推进任何序号，被搬过的行永远落在
        水位后面，回填再跑一万遍也不会重访。索引行的 id 是 `archive_id ∥ record_key`，
        record_key 本单元不动，所以重排是一次幂等覆盖，不产生孤儿行、不必先删。
        """
        after = ""
        while True:
            keys = tuple(
                str(row["record_key"])
                for row in connection.execute(
                    "SELECT record_key FROM archive_records "
                    "WHERE archive_id = ? AND record_key > ? AND source_account_id = ? "
                    "ORDER BY record_key LIMIT ?",
                    (archive_id, after, loser, _ACCOUNT_MERGE_CHUNK_ROWS),
                )
            )
            if not keys:
                return
            after = keys[-1]
            # 只搬行、记账本；索引由 worker 下一趟重放（PSU-2.101）。这段跑在
            # `publish_account_identity` 的请求路径里、握着写锁，此前几十万行的
            # 索引重写就在这里做。
            self._move_records_chunk(
                connection,
                archive_id,
                keys,
                set_column="source_account_id",
                old_value=loser,
                new_value=primary,
            )

    def _rebuild_account_state(
        self, connection: sqlite3.Connection, archive_id: str, account_id: str
    ) -> None:
        """按记录表重算这一个账号的会话状态，与建表回填逐字同一条口径。

        改写 `archive_records.source_account_id` 之后**必须**重算：状态表就是按
        这一列分组的。PSU-2.35a 的账号维度迁移在改写同一列之后直接把整张状态表
        丢掉重建，记的是同一条硬约束。这里只重算受影响的那一个账号——归并动的
        是两条流，别的账号一行都没变，丢整张表要多付一次全归档的分组扫描。
        """
        connection.execute(
            "DELETE FROM archive_conversation_state WHERE archive_id = ? AND source_account_id = ?",
            (archive_id, account_id),
        )
        connection.execute(
            "INSERT INTO archive_conversation_state("
            "archive_id, source_account_id, conversation_id, "
            "message_count, last_created_at, last_server_sequence) "
            "SELECT archive_id, COALESCE(source_account_id, ''), conversation_id, "
            "COUNT(*), MAX(created_at), MAX(server_sequence) "
            "FROM archive_records "
            "WHERE archive_id = ? AND COALESCE(source_account_id, '') = ? "
            "GROUP BY archive_id, COALESCE(source_account_id, ''), conversation_id",
            (archive_id, account_id),
        )
        connection.execute(
            _STATE_PREVIEW_BACKFILL_ONE_ACCOUNT_SQL, (archive_id, account_id)
        )

    @staticmethod
    def _refresh_archive_stats(
        connection: sqlite3.Connection, archive_id: str
    ) -> None:
        """两个总数按状态表重算。`last_server_sequence` 不动——归并不产生序号。"""
        totals = connection.execute(
            "SELECT COALESCE(SUM(message_count), 0) AS message_count, "
            "COUNT(DISTINCT conversation_id) AS conversation_count "
            "FROM archive_conversation_state WHERE archive_id = ?",
            (archive_id,),
        ).fetchone()
        connection.execute(
            "UPDATE archive_stats SET message_count = ?, conversation_count = ? "
            "WHERE archive_id = ?",
            (
                int(totals["message_count"]),
                int(totals["conversation_count"]),
                archive_id,
            ),
        )

    def _merge_account_group(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        *,
        source_account_id: str,
        account_key: str,
    ) -> str:
        """把这一组归并到收敛为止，返回它的阅读身份。

        可重入、可续跑：链接表先落地并提交，此后每一次发布都会把还没搬完的接着
        搬。这不是补偿路径而是常态路径——账号身份每轮 sync 无条件发布
        （`manual_sync.py` 在第一批之前就发，正因为最需要名字的恰恰是已经传完的
        归档），所以"没搬完"最多活到下一次同步。
        """
        connection.execute("BEGIN IMMEDIATE")
        try:
            primary, pending = self._elect_account_primary(
                connection, archive_id, source_account_id, account_key
            )
            for loser in pending:
                self._merge_account_tables(
                    connection, archive_id, loser=loser, primary=primary
                )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        if not pending:
            return primary
        for loser in pending:
            self._drain_account_records(
                connection, archive_id, loser=loser, primary=primary
            )
        connection.execute("BEGIN IMMEDIATE")
        try:
            for loser in pending:
                connection.execute(
                    "DELETE FROM archive_conversation_state "
                    "WHERE archive_id = ? AND source_account_id = ?",
                    (archive_id, loser),
                )
                connection.execute(
                    "UPDATE archive_account_links SET pending_merge = 0 "
                    "WHERE archive_id = ? AND source_account_id = ?",
                    (archive_id, loser),
                )
            # primary 的状态行已经被每一批挪到位了（PSU-2.100）。原来这里整账号
            # `DELETE` + `GROUP BY` 重建，真机 803,335 行要 7.0 s，而这段跑在
            # `publish_account_identity` 的请求路径里、握着写锁。
            self._refresh_archive_stats(connection, archive_id)
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        # 索引欠着账本（PSU-2.101）：叫醒 worker，别让它睡满一个空闲周期。
        listener = self.on_search_rehome_pending
        if listener is not None:
            listener()
        return primary

    # ------------------------------------------------- 会话归并（PSU-2.86 B）

    @staticmethod
    def _link_conversations(
        connection: sqlite3.Connection,
        archive_id: str,
        conversations: tuple[ConversationInput, ...],
    ) -> None:
        """记下这些会话在微信眼里的身份；搬家由 `_settle_conversation_merges` 驱动。

        表按 `(archive_id, conversation_id)` 建键而**不带账号**：`conversation_key`
        本身已经按 `account_key` 加过盐，同一个归档里两个不同微信的会话不可能撞上。
        不带账号还有一个好处——账号归并改写 `source_account_id` 时，这张表一行都
        不用跟着动。

        选举与账号那边同一条规矩：取组内 `conversation_id` 最小值，与到达顺序无关。

        这里**不再置任何「欠搬」的旗子**：欠不欠搬看败者 id 下面还有没有行
        （`_CONVERSATION_MOVE_OWED_SQL`），而不是看这一次选举有没有改变 primary。
        旗子只记得选举那一刻，记不住之后又落到旧 id 下面的东西。
        """
        for conversation in conversations:
            key = conversation.conversation_key
            if key is None:
                continue
            connection.execute(
                "INSERT INTO archive_conversation_links("
                "archive_id, conversation_id, conversation_key, primary_conversation_id"
                ") VALUES (?, ?, ?, ?) "
                "ON CONFLICT(archive_id, conversation_id) DO UPDATE SET "
                "conversation_key = excluded.conversation_key",
                (
                    archive_id,
                    conversation.conversation_id,
                    key,
                    conversation.conversation_id,
                ),
            )
            primary = str(
                connection.execute(
                    "SELECT MIN(conversation_id) AS primary_id "
                    "FROM archive_conversation_links "
                    "WHERE archive_id = ? AND conversation_key = ?",
                    (archive_id, key),
                ).fetchone()["primary_id"]
            )
            connection.execute(
                "UPDATE archive_conversation_links SET primary_conversation_id = ? "
                "WHERE archive_id = ? AND conversation_key = ?",
                (primary, archive_id, key),
            )

    @staticmethod
    def _merge_conversation_tables(
        connection: sqlite3.Connection,
        archive_id: str,
        *,
        account_id: str,
        loser: str,
        primary: str,
    ) -> None:
        """三张会话侧表按 last-write-wins 合并，规则与账号那边逐字相同。"""
        for table, key_columns in _CONVERSATION_MERGE_TABLES:
            match = "".join(
                f" AND p.{column} = {table}.{column}" for column in key_columns
            )
            connection.execute(
                f"DELETE FROM {table} WHERE archive_id = ? AND source_account_id = ? "
                f"AND conversation_id = ? AND EXISTS (SELECT 1 FROM {table} AS p "
                f"WHERE p.archive_id = {table}.archive_id "
                f"AND p.source_account_id = {table}.source_account_id "
                f"AND p.conversation_id = ?{match} "
                f"AND p.source_updated_at >= {table}.source_updated_at)",
                (archive_id, account_id, loser, primary),
            )
            connection.execute(
                f"DELETE FROM {table} WHERE archive_id = ? AND source_account_id = ? "
                f"AND conversation_id = ? AND EXISTS (SELECT 1 FROM {table} AS p "
                f"WHERE p.archive_id = {table}.archive_id "
                f"AND p.source_account_id = {table}.source_account_id "
                f"AND p.conversation_id = ?{match} "
                f"AND p.source_updated_at > {table}.source_updated_at)",
                (archive_id, account_id, primary, loser),
            )
            connection.execute(
                f"UPDATE {table} SET conversation_id = ? WHERE archive_id = ? "
                "AND source_account_id = ? AND conversation_id = ?",
                (primary, archive_id, account_id, loser),
            )

    def _owed_conversation_pairs(
        self, connection: sqlite3.Connection, archive_id: str
    ) -> tuple[tuple[str, str], ...]:
        """还欠搬家的 `(败者, primary)` 对，按会话 id 排序。

        **欠不欠搬按行判，不按旗判**（`_CONVERSATION_MOVE_OWED_SQL`）：败者 id
        下面只要还有状态行、会话名、资料或成员，就再搬一次。这样任何一条把行
        漏到旧 id 下面的路——2026-09-02 之前的 ingest 就是一条——都会在下一趟被
        自愈，而不是靠一个搬完就清零、此后再也不会置位的旗子。

        问的是整个归档而不是某一个账号（PSU-2.98）：驱动它的从「每轮同步都发的
        账号身份」换成了后台 worker，而 worker 没有「当前是哪个账号」这回事。
        """
        return tuple(
            (str(row["conversation_id"]), str(row["primary_conversation_id"]))
            for row in connection.execute(
                "SELECT l.conversation_id, l.primary_conversation_id "
                "FROM archive_conversation_links AS l "
                f"WHERE l.archive_id = ? AND {_CONVERSATION_MOVE_OWED_SQL.format(account='')} "
                "ORDER BY l.conversation_id",
                (archive_id,),
            )
        )

    def _conversation_still_owes_a_move(
        self, connection: sqlite3.Connection, archive_id: str, loser: str
    ) -> bool:
        """搬完、合完之后，判据是不是**还**说这一对欠搬。

        四次主键前缀 EXISTS，很便宜。要重查而不是从返回值推：一对「行已搬空、
        表里还有残留」的链接，settle 返回的形状与「一对本来就没有记录的正常
        链接」一模一样（都是搬了 0 行、这一对处理完了）。
        """
        row = connection.execute(
            "SELECT 1 FROM archive_conversation_links AS l "
            "WHERE l.archive_id = ? AND l.conversation_id = ? "
            f"AND {_CONVERSATION_MOVE_OWED_SQL.format(account='')}",
            (archive_id, loser),
        ).fetchone()
        return row is not None

    def _conversation_accounts(
        self, connection: sqlite3.Connection, archive_id: str, conversation_id: str
    ) -> tuple[str, ...]:
        """这条会话在三张会话侧表里出现在哪些账号名下。

        `_merge_conversation_tables` 是按账号合的（三张表的主键都含
        `source_account_id`），而 worker 手上没有账号——所以在这里问出来。
        """
        selects = " UNION ".join(
            f"SELECT source_account_id FROM {table} WHERE archive_id = ? AND conversation_id = ?"
            for table, _ in _CONVERSATION_MERGE_TABLES
        )
        return tuple(
            str(row["source_account_id"])
            for row in connection.execute(
                selects, (archive_id, conversation_id) * len(_CONVERSATION_MERGE_TABLES)
            )
        )

    def _settle_conversation_pair(
        self,
        connection: sqlite3.Connection,
        archive_id: str,
        *,
        loser: str,
        primary: str,
        max_rows: int,
    ) -> tuple[tuple[sqlite3.Row, ...], bool]:
        """搬**一批**，返回 `(搬完的行, 这一对是不是清完了)`。

        行交给调用方在**放锁之后**写索引（PSU-2.101）。

        一批之后就返回，让调用方放锁——跨批握着那把 Python 写锁正是
        2026-09-07 写瘫的一半（另一半是尾段那次整账号重建，见
        `_shift_conversation_state` 按批挪，PSU-2.100）。批内的事务边界本来就在
        `_drain_records_chunk` 里，这里一个字没动。

        进度天然可续：搬完的行不再属于败者，下一批自然接着走，不需要水位。
        """
        keys = tuple(
            str(row["record_key"])
            for row in connection.execute(
                "SELECT record_key FROM archive_records "
                "WHERE archive_id = ? AND conversation_id = ? LIMIT ?",
                (archive_id, loser, max_rows),
            )
        )
        rows: tuple[sqlite3.Row, ...] = ()
        if keys:
            rows = self._move_records_chunk(
                connection,
                archive_id,
                keys,
                set_column="conversation_id",
                old_value=loser,
                new_value=primary,
            )
            if len(keys) >= max_rows:
                return rows, False
        accounts = self._conversation_accounts(connection, archive_id, loser)
        connection.execute("BEGIN IMMEDIATE")
        try:
            for account_id in accounts:
                self._merge_conversation_tables(
                    connection,
                    archive_id,
                    account_id=account_id,
                    loser=loser,
                    primary=primary,
                )
            # primary 的状态行已经被每一批挪到位了（`_shift_conversation_state`）。
            # 败者的状态行在这里删：它已经减到 0，而批内刻意不删（见那边的说明），
            # 留着会让这条链接永远判成「还欠搬」。
            connection.execute(
                "DELETE FROM archive_conversation_state "
                "WHERE archive_id = ? AND conversation_id = ?",
                (archive_id, loser),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        return rows, True

    def settle_conversation_merge_step(
        self,
        archive_id: str,
        *,
        max_rows: int = _ACCOUNT_MERGE_CHUNK_ROWS,
        skip: Collection[str] = (),
    ) -> ConversationMergeStep:
        """把会话归并往前推**一小段**，然后放锁。

        **执行位置从请求路径搬到了后台**（PSU-2.98）。它原来挂在
        `publish_account_identity` 上——那是每一轮同步的第一个写请求——理由是
        批次那条路上提交后的异常被整段吞掉，没有任何东西会重试一次被吞掉的
        搬家，而账号身份每轮必发，是当初唯一可靠的自愈钩子。理由今天仍然成立，
        换掉的只是**谁来驱动**（后台 worker，见 `ConversationMergeWorker`）与
        **边界**（无界 → 一批一放锁），不是取消这件事。

        搜索索引与归档库是两个 sqlite 文件，同时写它的还有请求路径与
        `SearchBackfillWorker`，靠 5 秒 `busy_timeout` 裁决。抢不到就**这一批
        回滚、下一趟重来**（`contended=True`），不能让整轮塌掉。

        `skip` 是调用方本趟已经放弃的败者 id。判据按行、四张表任意一张有残留
        就算欠搬，所以**存在一对搬完仍被判欠搬的可能**；没有这个参数的话，
        每一趟都取 `pairs[0]`，那一对会把同一个归档里其余所有对饿死。
        原来那版（请求路径上的整轮遍历）没有这个问题，换成一次一小段之后
        有了——所以由调用方记住 `stuck` 的那几条，跳过它们继续。
        """
        if max_rows <= 0:
            raise ValueError("merge chunk must be positive")
        # 先还账本（PSU-2.101）：上一趟搬了行、索引没写成的键。有账要还的这一趟
        # 不搬新行——索引欠着的时候再往前搬，只是把账本越堆越高。
        replayed, replay_contended = self._replay_rehome_ledger(archive_id, max_rows=max_rows)
        if replayed or replay_contended:
            with closing(self._connect()) as connection:
                pending = len(self._owed_conversation_pairs(connection, archive_id))
            return ConversationMergeStep(
                moved_rows=0,
                settled_pairs=0,
                pending_pairs=pending,
                contended=replay_contended,
                replayed_rows=replayed,
            )
        with self._writer_lock, closing(self._connect()) as connection:
            owed = self._owed_conversation_pairs(connection, archive_id)
            pairs = tuple(pair for pair in owed if pair[0] not in skip)
            if not pairs:
                # 跳过之后没活干了，**不等于**欠账清零：`done` 与那个缓存说的都是
                # 整个归档还欠多少，不是这一趟还剩多少。把它们混起来，界面会在
                # 还有几条卡住的链接时报「已清零」。
                if not owed:
                    self._merge_pending_rows[archive_id] = 0
                return ConversationMergeStep(
                    moved_rows=0,
                    settled_pairs=0,
                    pending_pairs=len(owed),
                    done=not owed,
                )
            loser, primary = pairs[0]
            try:
                rows, settled = self._settle_conversation_pair(
                    connection,
                    archive_id,
                    loser=loser,
                    primary=primary,
                    max_rows=max_rows,
                )
            except sqlite3.OperationalError as error:
                text = str(error)
                if "locked" not in text and "busy" not in text:
                    raise
                return ConversationMergeStep(
                    moved_rows=0,
                    settled_pairs=0,
                    pending_pairs=len(owed),
                    contended=True,
                    conversation_id=loser,
                )
            # 清完了没有，**重查**而不是从 `len(owed) - 1` 推：那一步搬走的行
            # 可能让别的链接也不再欠搬（同一条会话可能被两条链接指着），而一对
            # 「合完仍有残留」的链接根本没离开过欠账。
            stuck = settled and self._conversation_still_owes_a_move(
                connection, archive_id, loser
            )
            remaining = len(self._owed_conversation_pairs(connection, archive_id))
            if remaining == 0:
                # 两个总数按状态表重算，**只在欠账清零时跑一次**：它扫的是状态
                # 表（会话数量级）而不是记录表，但也没有理由每一批都付一次。
                connection.execute("BEGIN IMMEDIATE")
                try:
                    self._refresh_archive_stats(connection, archive_id)
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
                self._merge_pending_rows[archive_id] = 0
        # 写锁已经放了。索引写的是另一个 sqlite 文件（PSU-2.101）；写不成就留着
        # 账本、报 contended，下一趟 `_replay_rehome_ledger` 按记录表现状重写。
        indexed = self._index_moved_rows(archive_id, rows)
        return ConversationMergeStep(
            moved_rows=len(rows),
            settled_pairs=0 if stuck else (1 if settled else 0),
            pending_pairs=remaining,
            done=remaining == 0 and indexed,
            contended=not indexed,
            conversation_id=loser,
            stuck=stuck,
        )

    def refresh_conversation_merge_pending_rows(self, archive_id: str) -> int:
        """数一次「还欠搬多少**行**」，记进内存缓存，返回它。

        **不许放进健康探针里现算**：真机上这是 216,282 行，对它做一次 COUNT 就是
        又一个慢查询挂在每轮健康轮询上。由 worker 每趟调一次，探针只读缓存；
        worker 没跑过就是 `None`——那是「还不知道」，不是零。

        度量口径警告：判据必须带 `l.conversation_id <> l.primary_conversation_id`
        （它在 `_CONVERSATION_MOVE_OWED_SQL` 里），否则链接表里的自链会把这个数
        算成三倍多——真机上 216,282 会变成 754,667。
        """
        with closing(self._connect()) as connection:
            pairs = self._owed_conversation_pairs(connection, archive_id)
            total = 0
            for loser, _primary in pairs:
                row = connection.execute(
                    "SELECT COUNT(*) FROM archive_records "
                    "WHERE archive_id = ? AND conversation_id = ?",
                    (archive_id, loser),
                ).fetchone()
                total += 0 if row is None else int(row[0])
        self._merge_pending_rows[archive_id] = total
        return total

    def rebuild_account_state(self, archive_id: str, account_id: str) -> None:
        """整账号重建一次状态行。**只有清账那条运维命令调它**（PSU-2.98）。

        归并不再整账号重建（状态行按批增量维护，见 `_shift_conversation_state`），于是
        「每次归并顺手校正全部状态行」这层保险没了。`settle-merges` 跑完做一次，
        把这层保险补在停机窗口里——那里没有请求在等这把锁。
        """
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                self._rebuild_account_state(connection, archive_id, account_id)
                self._refresh_archive_stats(connection, archive_id)
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def writer_lock_status(self) -> WriterLockStatus:
        """这把全局写锁此刻的样子（PSU-2.98）。

        读路径不走写锁（`sync_health` 用裸 `_connect()`），所以在写瘫的那一个
        小时里健康探针一直答 200，界面据此说「远程可用」。**没有这两个数，
        「如实报远程忙」就是一句空话。**
        """
        return WriterLockStatus(
            held_ms=self._writer_lock.held_ms,
            rejections_recent=self._writer_lock.rejections_recent(),
        )

    def publish_account_identity(
        self,
        archive_id: str,
        source_account_id: str,
        account: AccountInput,
    ) -> None:
        """Record what an upload stream calls itself (record model v5).

        Its own operation rather than a batch section, because the case it
        exists for is an archive with nothing left to sync: that builds no
        batch, so a section would never travel. Last-write-wins on the
        sender's own row version — a publish replayed out of order carries an
        older one and must not restore a name already moved past.
        """
        if not archive_id or not source_account_id:
            raise MetadataValidationError("account identity requires both identities")
        if not account.display_name or account.updated_at < 0:
            raise MetadataValidationError("account identity is not well formed")
        with self._writer_lock, closing(self._connect()) as connection:
            # 名字写在**阅读身份**名下，不是请求路径上那条流的 id（记录模型 v11）。
            # 带了 `account_key` 就先把这一组并掉——两台机器采集同一个微信时，
            # 这里是它们第一次有机会认出对方。
            read_account_id = (
                self._read_account_id(connection, archive_id, source_account_id)
                if account.account_key is None
                else self._merge_account_group(
                    connection,
                    archive_id,
                    source_account_id=source_account_id,
                    account_key=account.account_key,
                )
            )
            connection.execute(
                """
                INSERT INTO archive_accounts(
                    archive_id, source_account_id, display_name, avatar_blob_id,
                    source_updated_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id, source_account_id) DO UPDATE SET
                    display_name = excluded.display_name,
                    avatar_blob_id = excluded.avatar_blob_id,
                    source_updated_at = excluded.source_updated_at,
                    updated_at = excluded.updated_at
                WHERE excluded.source_updated_at >= archive_accounts.source_updated_at
                """,
                (
                    archive_id,
                    read_account_id,
                    account.display_name,
                    account.avatar_blob_id,
                    account.updated_at,
                    int(time.time()),
                ),
            )
            connection.commit()
            # 会话归并**不再挂在这里**（PSU-2.98）。它曾经挂在每轮同步的第一个
            # 写请求上，于是一次 216,282 行的搬家把整个归档、所有设备、所有写
            # 入口一起瘫掉一小时。现在由 `ConversationMergeWorker` 在后台一批
            # 一批地推，自愈语义一个字没变——欠不欠搬仍然按行判，见
            # `_owed_conversation_pairs`。

    def publish_transcripts(
        self,
        archive_id: str,
        source_account_id: str,
        transcripts: TranscriptsInput,
    ) -> None:
        """收下一片语音转写（记录模型 v13，PSU-2.163）。

        和 roster 同一条规矩：**按发送方自己的 `updated_at` 做 last-write-wins**，
        所以片可以乱序到达、重放是无操作。换引擎重转会推进那个时间，于是新的
        那一版盖得住旧的；而一片迟到的旧数据盖不住新的。

        一片一个事务：片就是「要么整片落地、要么没落地」的那个单位。

        不存 `source_account_id`：远端按 `record_key` 认消息，而那个键里已经
        含了账号（`remote_record_key(provider, canonical_account_id, ...)`）。
        再存一列只会多出一个可以和键不一致的地方。
        """
        if not archive_id or not source_account_id:
            raise MetadataValidationError("transcripts require both identities")
        for item in transcripts.transcripts:
            if not item.record_key or item.updated_at < 0:
                raise MetadataValidationError("transcript is not well formed")
        if not transcripts.transcripts:
            raise MetadataValidationError("a transcript slice cannot be empty")
        with self._writer_lock, closing(self._connect()) as connection, connection:
            connection.executemany(
                """
                INSERT INTO archive_transcripts
                    (archive_id, record_key, text, engine, low_confidence,
                     updated_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(archive_id, record_key) DO UPDATE SET
                    text = excluded.text,
                    engine = excluded.engine,
                    low_confidence = excluded.low_confidence,
                    updated_at = excluded.updated_at
                WHERE excluded.updated_at >= archive_transcripts.updated_at
                """,
                [
                    (
                        archive_id,
                        item.record_key,
                        item.text,
                        item.engine,
                        int(item.low_confidence),
                        item.updated_at,
                    )
                    for item in transcripts.transcripts
                ],
            )
        self._index_published_transcripts(
            archive_id, tuple(item.record_key for item in transcripts.transcripts)
        )

    def _index_published_transcripts(
        self, archive_id: str, record_keys: tuple[str, ...]
    ) -> None:
        """刚收下的转写接进检索，远端才搜得到语音里说的话。

        取的是**落库之后**的那份文本：last-write-wins 可能让这一片里的某条没盖住
        更新的旧值，按片里的原文接会把索引写回旧版本。

        接不上（搜索库被占）不让这一片失败——转写已经落库，重发也只是再落一遍。
        把存量扫描拨回开头，后台回填会把它补上。
        """
        try:
            with closing(self._connect()) as connection:
                current: list[tuple[str, str]] = []
                keys = sorted(set(record_keys))
                for start in range(0, len(keys), _TRANSCRIPT_LOOKUP_CHUNK):
                    batch = keys[start : start + _TRANSCRIPT_LOOKUP_CHUNK]
                    placeholders = ", ".join("?" for _ in batch)
                    current.extend(
                        (str(row["record_key"]), str(row["text"]))
                        for row in connection.execute(
                            f"""
                            SELECT record_key, text FROM archive_transcripts
                            WHERE archive_id = ? AND record_key IN ({placeholders})
                            """,
                            (archive_id, *batch),
                        )
                    )
            self._search.refresh_transcripts(archive_id, current)
        except sqlite3.OperationalError as error:
            text = str(error)
            if "locked" not in text and "busy" not in text:
                raise
            try:
                self._search.set_transcript_sweep_position(archive_id, "")
            except sqlite3.OperationalError:
                # 进度表与索引同在搜索库里，库被占时它也写不进去。留下痕迹：
                # 这几条要等下一次收下转写或手动拨回进度才会接上。
                _logger.warning(
                    "transcripts for %s landed but could not reach the search index",
                    archive_id,
                )

    def publish_roster(
        self,
        archive_id: str,
        source_account_id: str,
        roster: RosterInput,
    ) -> None:
        """Record one slice of an account's address book (record model v8, PSU-2.47).

        Its own operation rather than a batch section, for the reason 2.41
        learned the hard way: the archive that most needs a roster is the one
        with nothing left to sync, and that builds no batch at all.

        Every row is last-write-wins on the **sender's own** ``updated_at``, so
        slices may arrive in any order and a replay is a no-op. One transaction
        per slice: a slice is the unit that either lands or does not.
        """
        if not archive_id or not source_account_id:
            raise MetadataValidationError("roster requires both identities")
        for identity in roster.identities:
            if not identity.uid or not identity.display_name or identity.updated_at < 0:
                raise MetadataValidationError("identity is not well formed")
        for member in roster.members:
            if (
                not member.conversation_id
                or not member.member_uid
                or not member.display_name
                or member.updated_at < 0
            ):
                raise MetadataValidationError("conversation member is not well formed")
        for profile in roster.conversations:
            if not profile.conversation_id or profile.updated_at < 0:
                raise MetadataValidationError("conversation profile is not well formed")
        now = int(time.time())
        with self._writer_lock, closing(self._connect()) as connection:
            try:
                connection.execute("BEGIN IMMEDIATE")
                # 通讯录也写在**阅读身份**名下（记录模型 v11）。不翻译的话，
                # 败者那条流发来的联系人会落在它自己的 id 下，要等下一轮身份发布
                # 才被并过去——中间那一段时间，气泡上的头像与群成员名单对不上。
                read_account_id = self._read_account_id(
                    connection, archive_id, source_account_id
                )
                # 成员与资料按会话 id 入键，同样要翻译成阅读身份：败者会话归并
                # 之后，那条流再发来的名单若按旧 id 落地，就是一条没有消息、
                # 只有成员的空壳会话，而且没有任何东西会再搬它。
                read_conversation_ids = self._read_conversation_ids(
                    connection,
                    archive_id,
                    {item.conversation_id for item in roster.members}
                    | {item.conversation_id for item in roster.conversations},
                )
                connection.executemany(
                    """
                    INSERT INTO archive_identities(
                        archive_id, source_account_id, uid, display_name, remark,
                        avatar_blob_id, source_updated_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(archive_id, source_account_id, uid) DO UPDATE SET
                        display_name = excluded.display_name,
                        remark = excluded.remark,
                        avatar_blob_id = excluded.avatar_blob_id,
                        source_updated_at = excluded.source_updated_at,
                        updated_at = excluded.updated_at
                    WHERE excluded.source_updated_at >= archive_identities.source_updated_at
                    """,
                    [
                        (
                            archive_id,
                            read_account_id,
                            item.uid,
                            item.display_name,
                            item.remark,
                            item.avatar_blob_id,
                            item.updated_at,
                            now,
                        )
                        for item in roster.identities
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO archive_conversation_members(
                        archive_id, source_account_id, conversation_id, member_uid,
                        display_name, group_alias, is_self, source_updated_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        archive_id, source_account_id, conversation_id, member_uid
                    ) DO UPDATE SET
                        display_name = excluded.display_name,
                        group_alias = excluded.group_alias,
                        is_self = excluded.is_self,
                        source_updated_at = excluded.source_updated_at,
                        updated_at = excluded.updated_at
                    WHERE excluded.source_updated_at
                          >= archive_conversation_members.source_updated_at
                    """,
                    [
                        (
                            archive_id,
                            read_account_id,
                            read_conversation_ids[item.conversation_id],
                            item.member_uid,
                            item.display_name,
                            item.group_alias,
                            1 if item.is_self else 0,
                            item.updated_at,
                            now,
                        )
                        for item in roster.members
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO archive_conversation_profiles(
                        archive_id, source_account_id, conversation_id, contact_uid,
                        avatar_blob_id, announcement, my_group_alias,
                        source_updated_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(
                        archive_id, source_account_id, conversation_id
                    ) DO UPDATE SET
                        contact_uid = excluded.contact_uid,
                        avatar_blob_id = excluded.avatar_blob_id,
                        announcement = excluded.announcement,
                        my_group_alias = excluded.my_group_alias,
                        source_updated_at = excluded.source_updated_at,
                        updated_at = excluded.updated_at
                    WHERE excluded.source_updated_at
                          >= archive_conversation_profiles.source_updated_at
                    """,
                    [
                        (
                            archive_id,
                            read_account_id,
                            read_conversation_ids[item.conversation_id],
                            item.contact_uid,
                            item.avatar_blob_id,
                            item.announcement,
                            item.my_group_alias,
                            item.updated_at,
                            now,
                        )
                        for item in roster.conversations
                    ],
                )
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def list_conversation_members(
        self,
        archive_id: str,
        conversation_id: str,
        *,
        source_account_id: str | None = None,
        after_member_uid: str = "",
        limit: int,
    ) -> ConversationMemberPage:
        """One page of a group's roster (record model v8, PSU-2.47).

        Paged by ``member_uid`` rather than by name: names repeat and change,
        and a keyset that can do either would walk some members twice and skip
        others. The info drawer sorts what it shows; the walk only has to be
        complete.

        The avatar is joined from the identity stream rather than stored per
        membership: one person in forty groups has one picture, and forty
        copies of it would go stale forty different ways.
        """
        if limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid member limit")
        if source_account_id == "":
            raise MetadataValidationError("account filter must be non-empty")
        with closing(self._connect()) as connection:
            source_account_id = self._as_read_account(
                connection, archive_id, source_account_id
            )
            conversation_id = self._as_read_conversation(
                connection, archive_id, conversation_id
            )
            conditions = ["m.archive_id = ?", "m.conversation_id = ?", "m.member_uid > ?"]
            parameters: list[object] = [archive_id, conversation_id, after_member_uid]
            if source_account_id is not None:
                conditions.append("m.source_account_id = ?")
                parameters.append(source_account_id)
            rows = connection.execute(
                f"""
                SELECT m.member_uid AS member_uid,
                       m.display_name AS display_name,
                       m.group_alias AS group_alias,
                       m.is_self AS is_self,
                       i.avatar_blob_id AS avatar_blob_id
                FROM archive_conversation_members AS m
                LEFT JOIN archive_identities AS i
                    ON i.archive_id = m.archive_id
                   AND i.source_account_id = m.source_account_id
                   AND i.uid = m.member_uid
                WHERE {" AND ".join(conditions)}
                ORDER BY m.member_uid
                LIMIT ?
                """,
                (*parameters, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        members = tuple(
            ConversationMemberSummary(
                member_uid=str(row["member_uid"]),
                display_name=str(row["display_name"]),
                group_alias=_optional_text(row["group_alias"]),
                is_self=bool(row["is_self"]),
                avatar_blob_id=_optional_text(row["avatar_blob_id"]),
            )
            for row in rows[:limit]
        )
        next_uid = None if not members else members[-1].member_uid
        return ConversationMemberPage(members, next_uid, has_more)

    def account_display_name(
        self,
        archive_id: str,
        source_account_id: str,
    ) -> str | None:
        """What this account calls itself, or ``None`` when nobody has said.

        Also the v5 capability probe (``AccountIdentityMetadataStore``): a
        store that answers this is exactly a store that kept the section.
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT display_name FROM archive_accounts
                WHERE archive_id = ? AND source_account_id = ?
                """,
                (archive_id, source_account_id),
            ).fetchone()
        return None if row is None else _optional_text(row["display_name"])

    def list_account_summaries(self, archive_id: str) -> ArchiveAccountsSnapshot:
        """The archive's account dimension, with each account's own name.

        Counted from the conversation state rather than the ledger: the ledger
        says who *uploaded*, the state says what is *readable now*, and the
        switcher this feeds is about the latter. It also used to be a
        ``GROUP BY`` over every record — a full scan of the archive on every
        page load — and the state table (PSU-2.39) already holds exactly these
        counts, one row per conversation, summing to the same number by
        construction.

        The empty-string account groups into ``unattributed_count``: rows whose
        stream cannot be proven stay countable without being assigned to
        anyone. ``display_name`` is ``None`` until a batch from that stream has
        delivered one, which for rows synced before PSU-2.40 means until that
        machine syncs again.
        """
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT s.source_account_id AS source_account_id,
                       SUM(s.message_count) AS record_count,
                       a.display_name AS display_name,
                       a.avatar_blob_id AS avatar_blob_id
                FROM archive_conversation_state AS s
                LEFT JOIN archive_accounts AS a
                    ON a.archive_id = s.archive_id
                   AND a.source_account_id = s.source_account_id
                WHERE s.archive_id = ?
                GROUP BY s.source_account_id
                ORDER BY s.source_account_id
                """,
                (archive_id,),
            ).fetchall()
            merged = self._merged_away_ids(connection, archive_id)
        accounts: list[ArchiveAccountSummary] = []
        unattributed = 0
        for row in rows:
            account = str(row["source_account_id"])
            if account == "":
                unattributed = int(row["record_count"])
            else:
                accounts.append(
                    ArchiveAccountSummary(
                        source_account_id=account,
                        record_count=int(row["record_count"]),
                        display_name=_optional_text(row["display_name"]),
                        avatar_blob_id=_optional_text(row["avatar_blob_id"]),
                        merged_source_account_ids=merged.get(account, ()),
                    )
                )
        return ArchiveAccountsSnapshot(tuple(accounts), unattributed)

    @staticmethod
    def _merged_away_ids(
        connection: sqlite3.Connection, archive_id: str
    ) -> dict[str, tuple[str, ...]]:
        """每条上传流的**同组伙伴**：同一个微信的其它 id，不含它自己（PSU-2.90）。

        整张链接表一次读完再在内存里分组：它的行数是「机器数 × 账号数」，
        真机个位数，不是记录量级。按组问一次要发 N 条查询，为一个开关面板不值得。

        按 `account_key` 分组而不是按 `primary_account_id`：搬家还没跑完时，
        败者流自己也会出现在账号列表里（`pending_merge = 1`），那时它同样该报出
        伙伴——它就是那个用户会看见"多出来一条"的时刻。
        """
        by_key: dict[str, list[str]] = {}
        of_account: dict[str, str] = {}
        for row in connection.execute(
            "SELECT account_key, source_account_id FROM archive_account_links "
            "WHERE archive_id = ? ORDER BY source_account_id",
            (archive_id,),
        ):
            key = str(row["account_key"])
            account = str(row["source_account_id"])
            by_key.setdefault(key, []).append(account)
            of_account[account] = key
        return {
            account: tuple(peer for peer in by_key[key] if peer != account)
            for account, key in of_account.items()
        }

    @staticmethod
    def _conflict(row: sqlite3.Row) -> ConflictRecord:
        canonical_source = row["canonical_source_instance_id"]
        incoming_source = row["incoming_source_instance_id"]
        incoming_schema = row["incoming_schema_version"]
        canonical_device = row["canonical_device_id"]
        incoming_device = row["incoming_device_id"]
        return ConflictRecord(
            record_key=str(row["record_key"]),
            server_sequence=int(row["server_sequence"]),
            canonical_content_sha256=str(row["canonical_content_sha256"]),
            incoming_content_sha256=str(row["incoming_content_sha256"]),
            canonical_source_instance_id=(
                None if canonical_source is None else str(canonical_source)
            ),
            incoming_source_instance_id=(
                None if incoming_source is None else str(incoming_source)
            ),
            incoming_schema_version=None if incoming_schema is None else str(incoming_schema),
            canonical_device_id=(
                None if canonical_device is None else str(canonical_device)
            ),
            incoming_device_id=(
                None if incoming_device is None else str(incoming_device)
            ),
            detected_at=int(row["detected_at"]),
            reason=str(row["reason"]),
        )

    def list_conflicts(
        self,
        archive_id: str,
        *,
        after_server_sequence: int,
        limit: int,
    ) -> ConflictPage:
        if after_server_sequence < 0 or limit <= 0 or limit > 500:
            raise MetadataValidationError("invalid conflict cursor or limit")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """
                SELECT server_sequence, record_key, canonical_content_sha256,
                       incoming_content_sha256, canonical_source_instance_id,
                       incoming_source_instance_id, incoming_schema_version,
                       canonical_device_id, incoming_device_id, detected_at,
                       reason
                FROM record_conflicts
                WHERE archive_id = ? AND server_sequence > ?
                ORDER BY server_sequence
                LIMIT ?
                """,
                (archive_id, after_server_sequence, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        selected = rows[:limit]
        conflicts = tuple(self._conflict(row) for row in selected)
        next_sequence = after_server_sequence if not conflicts else conflicts[-1].server_sequence
        return ConflictPage(conflicts, next_sequence, has_more)

    def stats(self, archive_id: str) -> ArchiveStats:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT message_count, conversation_count, last_server_sequence
                FROM archive_stats WHERE archive_id = ?
                """,
                (archive_id,),
            ).fetchone()
        if row is None:
            return ArchiveStats(0, 0, 0)
        return ArchiveStats(
            message_count=int(row["message_count"]),
            conversation_count=int(row["conversation_count"]),
            last_server_sequence=int(row["last_server_sequence"]),
        )

    def attachment_refs_for_records(
        self, archive_id: str, record_keys: Sequence[str]
    ) -> dict[str, tuple[str, ...]]:
        """这些消息眼下发布的附件引用（PSU-2.143），没有记录或 refs 为 NULL 的缺席。

        按主键 (archive_id, record_key) 逐块查；一次批量签名最多几百条，
        每条一次主键查找。
        """
        wanted = tuple(dict.fromkeys(record_keys))
        found: dict[str, tuple[str, ...]] = {}
        if not wanted:
            return found
        with closing(self._connect()) as connection:
            for start in range(0, len(wanted), 500):
                chunk = wanted[start : start + 500]
                placeholders = ",".join("?" for _ in chunk)
                rows = connection.execute(
                    f"""
                    SELECT record_key, attachment_refs FROM archive_records
                    WHERE archive_id = ? AND record_key IN ({placeholders})
                      AND attachment_refs IS NOT NULL
                    """,  # noqa: S608 - placeholders are generated, values are bound
                    (archive_id, *chunk),
                ).fetchall()
                for row in rows:
                    found[str(row[0])] = _decode_attachment_refs(row[1])
        return found

    def merge_backlog(self, archive_id: str) -> MergeBacklog:
        """归并走到哪了：两张链接表各有多少行、其中多少趟搬家还欠着。

        链接表在真机上是个位数行，所以这一问随 `sync-health` 每轮跑一次也不值
        一提。会话那个「欠着」与 `_settle_conversation_merges` 用**同一条判据**
        （按行不按旗）：报出来的数就是下一次发布会搬的数，不多不少。
        """
        with closing(self._connect()) as connection:

            def count(table: str, where: str = "") -> int:
                row = connection.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE archive_id = ?{where}",
                    (archive_id,),
                ).fetchone()
                return 0 if row is None else int(row[0])

            owed = connection.execute(
                "SELECT COUNT(*) FROM archive_conversation_links AS l "
                f"WHERE l.archive_id = ? AND {_CONVERSATION_MOVE_OWED_SQL.format(account='')}",
                (archive_id,),
            ).fetchone()
            return MergeBacklog(
                account_link_count=count("archive_account_links"),
                account_merge_pending=count(
                    "archive_account_links", " AND pending_merge = 1"
                ),
                conversation_link_count=count("archive_conversation_links"),
                conversation_merge_pending=0 if owed is None else int(owed[0]),
                # 缓存读出来的，**不在这里现算**：对 216k 行做 COUNT 会让每一次
                # 健康轮询都付一次慢查询（PSU-2.98）。
                conversation_merge_pending_rows=self._merge_pending_rows.get(archive_id),
                # 账本是只有主键的小表，直接数（PSU-2.101）。回滚到不认识账本的
                # 版本之前，运维要看得见它是不是空的。
                search_rehome_pending=count("search_rehome_pending"),
            )

    def read_conversation_id(self, archive_id: str, conversation_id: str) -> str:
        """`_as_read_conversation` 的公开面（PSU-2.95）。

        自己开一条连接，因为调用它的是路由的装配段，手上没有读那一页时用的
        那条。多付的是一次开连接（实测 323 µs）加一次主键点查（40 µs）——
        与它买到的东西相比不值一提：**一页空转录也能说出自己是关于哪条会话
        的**，而空转录正是老书签最常见的落点。

        与那一页的读用的是同一张链接表，但**是两次问**。中间恰好落进一次
        归并的话，两次会不一致——那不是一个新的失败模式：客户端拿声明与行
        逐条对，不一致当场是协议错，重读一次就收敛。悄悄用行去反推声明才是
        真的危险，那会让"远端答错了一条会话"永远说不出口。
        """
        with closing(self._connect()) as connection:
            return self._as_read_conversation(connection, archive_id, conversation_id)

    def device_upload_cursors(self, archive_id: str, device_id: str) -> dict[str, int]:
        """这台设备每个账号传到了哪一条（PSU-2.165）。读路径，不过写锁。"""
        with closing(self._connect()) as connection:
            return {
                str(row["source_account_id"]): int(row["last_local_sequence"])
                for row in connection.execute(
                    """
                    SELECT source_account_id, last_local_sequence FROM device_cursors
                    WHERE archive_id = ? AND device_id = ?
                    """,
                    (archive_id, device_id),
                )
            }

    def storage_health(self, archive_id: str, device_id: str) -> ArchiveStorageHealth:
        """游标与冲突数是活的；三个 blob 数字**在这一后端上恒为 0**。

        `blob_manifests` 只有 `upsert_blob_manifest` 会写，而那个方法在整个
        `src/` 里没有任何调用者——blob 状态早就搬到 `SqliteBlobStateStore`
        （`blob-state.sqlite` 的 `server_blob_manifests`）去了。方法与表都留着，
        因为 Worker 那一支的同名 D1 表**是活的**（`remote/src/blobRoutes.ts` 在写），
        删 Python 侧救不了那边，反而会动到冻结的 v1 后端。

        所以自托管 API 的健康页自 PSU-2.57 起不再从这里取字节的账：它问
        `BlobStateStore.storage_health()`。在这里加这段话，是因为上一次没加，
        于是那三个 0 在线上待了很久没人发现。
        """
        with closing(self._connect()) as connection:
            row = connection.execute(
                """
                SELECT COALESCE(SUM(size), 0) AS used_bytes,
                       COUNT(*) AS blob_count,
                       COALESCE(SUM(CASE WHEN state = 'remote_ready' THEN 0 ELSE 1 END), 0)
                           AS pending_blob_count,
                       COALESCE((
                           -- A device carries one cursor per source account;
                           -- health reports the furthest stream.
                           SELECT MAX(last_local_sequence) FROM device_cursors
                           WHERE archive_id = ? AND device_id = ?
                       ), 0) AS last_uploaded_local_sequence,
                       (SELECT COUNT(*) FROM record_conflicts WHERE archive_id = ?)
                           AS conflict_count
                FROM blob_manifests WHERE archive_id = ?
                """,
                (archive_id, device_id, archive_id, archive_id),
            ).fetchone()
        assert row is not None
        return ArchiveStorageHealth(
            used_bytes=int(row["used_bytes"]),
            blob_count=int(row["blob_count"]),
            pending_blob_count=int(row["pending_blob_count"]),
            last_uploaded_local_sequence=int(row["last_uploaded_local_sequence"]),
            conflict_count=int(row["conflict_count"]),
        )

    def upsert_blob_manifest(self, archive_id: str, manifest: BlobManifest) -> None:
        if not archive_id or not manifest.blob_id or manifest.size < 0:
            raise MetadataValidationError("invalid blob manifest")
        with self._writer_lock, closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO blob_manifests(archive_id, blob_id, size, sha256, state)
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(archive_id, blob_id) DO UPDATE SET
                    size = excluded.size,
                    sha256 = excluded.sha256,
                    state = excluded.state
                """,
                (archive_id, manifest.blob_id, manifest.size, manifest.sha256, manifest.state),
            )
            connection.commit()

    def reconcile_blob_manifests(
        self,
        archive_id: str,
        expected: tuple[BlobManifest, ...],
    ) -> ManifestReconciliation:
        expected_by_id = {manifest.blob_id: manifest for manifest in expected}
        if len(expected_by_id) != len(expected):
            raise MetadataValidationError("expected manifest contains duplicate blob IDs")
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT blob_id, sha256 FROM blob_manifests WHERE archive_id = ?",
                (archive_id,),
            ).fetchall()
        stored = {str(row["blob_id"]): str(row["sha256"]) for row in rows}
        return ManifestReconciliation(
            missing_in_store=tuple(sorted(set(expected_by_id) - set(stored))),
            checksum_mismatches=tuple(
                sorted(
                    blob_id
                    for blob_id in set(expected_by_id) & set(stored)
                    if expected_by_id[blob_id].sha256 != stored[blob_id]
                )
            ),
            orphaned_in_store=tuple(sorted(set(stored) - set(expected_by_id))),
        )

    # -- PortableArchiveStore -------------------------------------------------

    @staticmethod
    def _portable_row(section: PortableSection, row: sqlite3.Row) -> PortableRow:
        if section is PortableSection.RECORDS:
            source_instance = row["source_instance_id"]
            origin_device = row["origin_device_id"]
            return PortableRecordRow(
                record_key=str(row["record_key"]),
                conversation_id=str(row["conversation_id"]),
                created_at=int(row["created_at"]),
                content_text=str(row["content_text"]),
                content_sha256=str(row["content_sha256"]),
                source_instance_id=None if source_instance is None else str(source_instance),
                origin_device_id=None if origin_device is None else str(origin_device),
                server_sequence=int(row["server_sequence"]),
            )
        if section is PortableSection.CONFLICTS:
            canonical_source = row["canonical_source_instance_id"]
            incoming_source = row["incoming_source_instance_id"]
            incoming_schema = row["incoming_schema_version"]
            canonical_device = row["canonical_device_id"]
            incoming_device = row["incoming_device_id"]
            return PortableConflictRow(
                server_sequence=int(row["server_sequence"]),
                record_key=str(row["record_key"]),
                canonical_content_sha256=str(row["canonical_content_sha256"]),
                incoming_content_sha256=str(row["incoming_content_sha256"]),
                canonical_source_instance_id=(
                    None if canonical_source is None else str(canonical_source)
                ),
                incoming_source_instance_id=(
                    None if incoming_source is None else str(incoming_source)
                ),
                incoming_schema_version=(
                    None if incoming_schema is None else str(incoming_schema)
                ),
                canonical_device_id=(
                    None if canonical_device is None else str(canonical_device)
                ),
                incoming_device_id=None if incoming_device is None else str(incoming_device),
                detected_at=int(row["detected_at"]),
            )
        if section is PortableSection.BATCHES:
            return PortableBatchRow(
                device_id=str(row["device_id"]),
                source_account_id=(
                    None
                    if row["source_account_id"] is None
                    else str(row["source_account_id"])
                ),
                client_batch_id=str(row["client_batch_id"]),
                payload_sha256=str(row["payload_sha256"]),
                server_sequence_start=int(row["server_sequence_start"]),
                server_sequence_end=int(row["server_sequence_end"]),
                inserted_count=int(row["inserted_count"]),
                updated_count=int(row["updated_count"]),
                conflict_count=int(row["conflict_count"]),
            )
        if section is PortableSection.CURSORS:
            return PortableCursorRow(
                device_id=str(row["device_id"]),
                source_account_id=str(row["source_account_id"]),
                last_local_sequence=int(row["last_local_sequence"]),
            )
        if section is PortableSection.BLOB_MANIFESTS:
            return PortableBlobRow(
                blob_id=str(row["blob_id"]),
                size=int(row["size"]),
                sha256=str(row["sha256"]),
                state=str(row["state"]),
            )
        return PortableArchiveRow(last_server_sequence=int(row["last_server_sequence"]))

    def export_portable_section(
        self,
        archive_id: str,
        section: PortableSection,
        *,
        cursor: str | None,
        limit: int,
    ) -> PortablePage:
        if not archive_id:
            raise PortableValidationError("archive_id must not be empty")
        if limit <= 0:
            raise PortableValidationError("portable export limit must be positive")
        statement = _PORTABLE_SELECT_SQL[section]
        parameters: list[object] = [archive_id]
        if cursor is not None:
            key = decode_portable_cursor(section, cursor)
            if section is PortableSection.ARCHIVE:
                # The archive section is a single unordered row, so its only
                # cursor means "past that row".
                return PortablePage(section=section, rows=(), next_cursor=None)
            columns = PORTABLE_SORT_COLUMNS[section]
            if len(columns) == 2:
                statement += f" AND ({columns[0]} > ? OR ({columns[0]} = ? AND {columns[1]} > ?))"
                parameters.extend((key[0], key[0], key[1]))
            else:
                statement += f" AND {columns[0]} > ?"
                parameters.append(key[0])
        order = ", ".join(PORTABLE_SORT_COLUMNS[section])
        if order:
            statement += f" ORDER BY {order}"
        statement += " LIMIT ?"
        parameters.append(limit + 1)
        try:
            with closing(self._connect()) as connection:
                found = connection.execute(statement, parameters).fetchall()
        except sqlite3.Error:
            raise MetadataUnavailableError(
                "portable export could not read the archive backend"
            ) from None
        has_more = len(found) > limit
        rows = tuple(self._portable_row(section, row) for row in found[:limit])
        next_cursor = portable_row_cursor(rows[-1]) if has_more else None
        return PortablePage(section=section, rows=rows, next_cursor=next_cursor)

    def portable_archive_is_empty(self, archive_id: str) -> bool:
        if not archive_id:
            raise PortableValidationError("archive_id must not be empty")
        try:
            with closing(self._connect()) as connection:
                # ``archive_conversations`` is checked here rather than in the
                # shared ``PORTABLE_OCCUPIED_TABLES``: it is a v2 table this
                # backend alone has, and the frozen v1 backends run the same
                # shared list against databases that have no such table. It is
                # not a portable *section* either, so a bundle carries no
                # conversation names — see the note on the export in the
                # module docstring.
                for table in (*PORTABLE_OCCUPIED_TABLES, "archive_conversations"):
                    occupied = connection.execute(
                        f"SELECT 1 FROM {table} WHERE archive_id = ? LIMIT 1",
                        (archive_id,),
                    ).fetchone()
                    if occupied is not None:
                        return False
        except sqlite3.Error:
            raise MetadataUnavailableError(
                "portable import could not inspect the archive backend"
            ) from None
        return True

    def import_portable_section(
        self,
        archive_id: str,
        section: PortableSection,
        rows: tuple[PortableRow, ...],
    ) -> None:
        if not archive_id:
            raise PortableValidationError("archive_id must not be empty")
        expected = PORTABLE_ROW_TYPES[section]
        if any(type(row) is not expected for row in rows):
            raise PortableValidationError("portable rows do not match their section")
        statement = _PORTABLE_INSERT_SQL[section]
        with self._writer_lock:
            state = self._portable_imports.get(archive_id)
            if state is None:
                if not self.portable_archive_is_empty(archive_id):
                    raise PortableConflictError("portable import target archive is not empty")
                state = PortableImportState.started()
                self._portable_imports[archive_id] = state
            elif state.failed_section is not None:
                resume_portable_import(
                    section,
                    state,
                    archive_is_empty=self.portable_archive_is_empty(archive_id),
                )
            # Defence in depth only. While the unresolved-section refusal in
            # finalize stands, a failed import never reaches the row-count
            # check, so nothing can pin this placement from the outside; it is
            # counted here so the check still works if that refusal is removed.
            state.counts[section] += len(rows)
            # Marked pessimistically, before anything at all can fail —
            # including _connect, whose pragmas raise on an unopenable file or
            # an exhausted descriptor table — and cleared only once every chunk
            # has committed. Any escape in between therefore leaves the section
            # recorded as unresolved, with no statement of this method relying
            # on being reached.
            state.failed_section = section
            try:
                with closing(self._connect()) as connection:
                    for chunk in portable_chunks(rows, _PORTABLE_IMPORT_CHUNK_ROWS):
                        try:
                            values = [
                                portable_insert_parameters(archive_id, row) for row in chunk
                            ]
                            connection.execute("BEGIN IMMEDIATE")
                            connection.executemany(statement, values)
                            connection.commit()
                        except BaseException:
                            connection.rollback()
                            raise
            except sqlite3.IntegrityError:
                # ``from None`` keeps the driver's message — which quotes the
                # offending values — out of the raised error, so the cause can
                # only be named as a class.
                raise PortableValidationError(
                    "portable import row violates a storage constraint; a duplicate "
                    "row identity is the most likely cause, but NOT NULL, CHECK and "
                    "foreign-key violations surface the same way"
                ) from None
            except sqlite3.Error:
                # Deliberately broad, and broader than "transient": a
                # ProgrammingError, InterfaceError or missing table is
                # deterministic yet still surfaces as retryable. Narrowing it
                # would mean enumerating every driver's fault taxonomy, and the
                # cost of retrying a deterministic fault is one more failure,
                # while the cost of leaking a driver exception is an operator
                # cleanup. Keep this shape in the other backends.
                raise MetadataUnavailableError(
                    "portable import could not write to the archive backend"
                ) from None
            state.failed_section = None

    def _finalize_portable_import_locked(
        self,
        archive_id: str,
        imported: dict[PortableSection, int],
    ) -> ImportSummary:
        """Self-check the imported archive, materialize its stats, summarize it.

        Four checks, all fail-closed (spec §4.1):

        1. no ``server_sequence`` in ``archive_records`` or ``record_conflicts``
           exceeds the imported ``archive_sequences.last_server_sequence``;
        2. every section holds exactly the rows that were handed to
           :meth:`import_portable_section` — this is what catches a concurrent
           writer or a silently dropped row;
        3. no conflict shares a ``server_sequence`` with a canonical record;
        4. every blob manifest state is one of the three known states.
        """
        try:
            with closing(self._connect()) as connection:
                connection.execute("BEGIN IMMEDIATE")
                try:
                    persisted = {
                        section: int(
                            connection.execute(
                                f"SELECT COUNT(*) FROM {PORTABLE_TABLES[section]} "
                                "WHERE archive_id = ?",
                                (archive_id,),
                            ).fetchone()[0]
                        )
                        for section in PORTABLE_SECTION_ORDER
                    }
                    conversation_count = int(
                        connection.execute(
                            """
                            SELECT COUNT(DISTINCT conversation_id)
                            FROM archive_records WHERE archive_id = ?
                            """,
                            (archive_id,),
                        ).fetchone()[0]
                    )
                    sequence_row = connection.execute(
                        "SELECT last_server_sequence FROM archive_sequences WHERE archive_id = ?",
                        (archive_id,),
                    ).fetchone()
                    last_sequence = (
                        0 if sequence_row is None else int(sequence_row["last_server_sequence"])
                    )
                    max_sequence = max(
                        int(
                            connection.execute(
                                f"SELECT COALESCE(MAX(server_sequence), 0) FROM {table} "
                                "WHERE archive_id = ?",
                                (archive_id,),
                            ).fetchone()[0]
                        )
                        for table in ("archive_records", "record_conflicts")
                    )
                    collisions = int(
                        connection.execute(
                            """
                            SELECT COUNT(*) FROM record_conflicts AS c
                            JOIN archive_records AS r
                              ON r.archive_id = c.archive_id
                             AND r.server_sequence = c.server_sequence
                            WHERE c.archive_id = ?
                            """,
                            (archive_id,),
                        ).fetchone()[0]
                    )
                    placeholders = ", ".join("?" * len(PORTABLE_BLOB_STATES))
                    unknown_states = int(
                        connection.execute(
                            f"SELECT COUNT(*) FROM blob_manifests WHERE archive_id = ? "
                            f"AND state NOT IN ({placeholders})",
                            (archive_id, *PORTABLE_BLOB_STATES),
                        ).fetchone()[0]
                    )

                    if max_sequence > last_sequence:
                        raise PortableValidationError(
                            "portable import server sequence exceeds the archive sequence"
                        )
                    for section in PORTABLE_SECTION_ORDER:
                        if persisted[section] != imported[section]:
                            raise PortableValidationError(
                                "portable import row count does not match the imported rows "
                                f"for section {section.value}"
                            )
                    if collisions:
                        raise PortableValidationError(
                            "portable import conflict sequence collides with a record sequence"
                        )
                    if unknown_states:
                        raise PortableValidationError(
                            "portable import blob state is not recognized"
                        )

                    # The bundle's record section is frozen pre-v3 and carries
                    # no account column, but its batch section does — so the
                    # import re-derives attribution by the same ledger rule the
                    # schema migration uses, instead of dropping it. Rows whose
                    # covering batch has no account stay NULL: unattributed,
                    # not guessed at.
                    for batch in connection.execute(
                        """
                        SELECT server_sequence_start, server_sequence_end,
                               source_account_id
                        FROM sync_batches
                        WHERE archive_id = ? AND source_account_id IS NOT NULL
                        """,
                        (archive_id,),
                    ).fetchall():
                        connection.execute(
                            """
                            UPDATE archive_records SET source_account_id = ?
                            WHERE archive_id = ? AND source_account_id IS NULL
                              AND server_sequence BETWEEN ? AND ?
                            """,
                            (
                                str(batch["source_account_id"]),
                                archive_id,
                                int(batch["server_sequence_start"]),
                                int(batch["server_sequence_end"]),
                            ),
                        )

                    # The browse counters are derived, like ``archive_stats``
                    # and ``normalized_content``, so the bundle does not carry
                    # them and this is where they come back. Rebuilt for this
                    # archive only — the same file may already hold others.
                    connection.execute(
                        "DELETE FROM archive_conversation_state WHERE archive_id = ?",
                        (archive_id,),
                    )
                    connection.execute(
                        """
                        INSERT INTO archive_conversation_state(
                            archive_id, source_account_id, conversation_id,
                            message_count, last_created_at, last_server_sequence
                        )
                        SELECT archive_id, COALESCE(source_account_id, ''),
                               conversation_id, COUNT(*), MAX(created_at),
                               MAX(server_sequence)
                        FROM archive_records
                        WHERE archive_id = ?
                        GROUP BY archive_id, COALESCE(source_account_id, ''),
                                 conversation_id
                        """,
                        (archive_id,),
                    )
                    connection.execute(
                        f"{_STATE_PREVIEW_BACKFILL_SQL} WHERE archive_id = ?",
                        (archive_id,),
                    )

                    message_count = persisted[PortableSection.RECORDS]
                    connection.execute(
                        """
                        INSERT INTO archive_stats(
                            archive_id, message_count, conversation_count, last_server_sequence
                        ) VALUES (?, ?, ?, ?)
                        ON CONFLICT(archive_id) DO UPDATE SET
                            message_count = excluded.message_count,
                            conversation_count = excluded.conversation_count,
                            last_server_sequence = excluded.last_server_sequence
                        """,
                        (archive_id, message_count, conversation_count, last_sequence),
                    )
                    connection.commit()
                except Exception:
                    connection.rollback()
                    raise
        except sqlite3.Error:
            raise MetadataUnavailableError(
                "portable import could not reach the archive backend"
            ) from None
        return ImportSummary(
            message_count=message_count,
            conversation_count=conversation_count,
            last_server_sequence=last_sequence,
            section_counts=MappingProxyType(dict(imported)),
        )

    def finalize_portable_import(self, archive_id: str) -> ImportSummary:
        if not archive_id:
            raise PortableValidationError("archive_id must not be empty")
        with self._writer_lock:
            state = self._portable_imports.get(archive_id)
            if state is None:
                raise PortableValidationError(
                    "no portable import is in progress for this archive"
                )
            if state.failed_section is not None:
                # Refused on the structural fact that a write did not complete,
                # not on the row-count check — which would in fact also fire,
                # since handed-in is counted before the write. Resting the
                # refusal on that arithmetic would tie it to the counting
                # placement staying correct. The state is deliberately kept so
                # the section-skip guard stays armed.
                raise PortableValidationError(
                    "portable import cannot be finalized while a section write is unresolved"
                )
            try:
                summary = self._finalize_portable_import_locked(archive_id, state.counts)
            except MetadataUnavailableError:
                # Nothing was written and the caller is being told the backend
                # is temporarily unable to serve, which the server surfaces as
                # retryable. Discarding the bookkeeping here would make that
                # advice a lie: the retry would find no import in progress and
                # the whole archive would need an operator cleanup.
                raise
            except BaseException:
                # Any other failure ends the import. The self-check rejected
                # the archive, so the next attempt must meet the empty-target
                # guard and force an operator to clear it first.
                del self._portable_imports[archive_id]
                raise
            else:
                del self._portable_imports[archive_id]
                return summary

"""SQLite full-text search with a covering, time-ordered index (v2).

Two generations coexist:

* **v2** (`search_grams_v2` + `search_fts_v2`): the sort key is encoded into
  the row identity itself (``docid``), so a page is the first rows of an index
  range and a count is one aggregation over index-resident columns. Nothing
  touches the message rows until the final, page-sized hydration.
* **v1** (`search_grams` + `search_messages_fts`): kept verbatim to serve
  archives whose v2 structures are still being backfilled. Its cost grows with
  the hit count -- one random probe per candidate -- which is the measured
  10-second first search this rewrite removes.

`search_index_state.engine` says which generation answers queries:
``building`` (v1 answers, writes go to both) or ``v2``. Archives never opened
by this version have no state row and read as ``v1-legacy``.
"""

from __future__ import annotations

import dataclasses
import sqlite3
import secrets
import unicodedata
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from enum import Enum
from typing import TypedDict, cast


# --- v1 constants (legacy read path, kept until every archive is rebuilt) ---
_IDENTITY_INDEX = "ix_search_messages_identity"
_ACCOUNT_ORDER_INDEX = "ix_search_messages_account_order"
_FILE_CLASS_INDEX = "ix_search_messages_file_class"
_FILE_ORDER_INDEX = "ix_search_messages_file_order"

#: 读路径的查询计划依赖这些索引，而它们**都是在既有档案之后才加的**。
#: 读连接是只读的，所以网关据此判断「该开一次写连接把缺的补上」。
#: 少列一条，那条索引就永远到不了已经存在的档案——PSU-2.66 一次加了五条，
#: 网关那边却只按名字盯着 `conversation_filters` 一条，于是真机上一条都没建，
#: 所有实测出来的提速一毫秒也没有落地。
READ_PATH_INDEXES: tuple[str, ...] = (
    "ix_search_messages_conversation_filters",
    "ix_search_messages_sender_order",
    "ix_search_messages_sender_filters",
    "ix_search_messages_type_order",
    "ix_search_messages_type_filters",
    _FILE_CLASS_INDEX,
    _FILE_ORDER_INDEX,
)
_TIME_ORDERED_HIT_FLOOR = 2_000

# --- v2 docid encoding ---
# created_at occupies the high bits so integer order == time order; the low
# bits only disambiguate messages that share a second. A fixed hash keeps the
# first candidate deterministic, and collisions linear-probe upward inside the
# second's 2^20 slot space.
_DOCID_SHIFT = 20
_DOCID_DISAMBIG_MASK = (1 << _DOCID_SHIFT) - 1
_DOCID_MAX_SECONDS = (1 << 42) - 1

ENGINE_V1_LEGACY = "v1-legacy"
ENGINE_BUILDING = "building"
ENGINE_REBUILDING = "rebuilding"
ENGINE_V2 = "v2"

# Which generations a given engine state writes. `building` keeps v1 loaded
# because v1 still answers its queries; `rebuilding` has no v1 left to keep.
_WRITES_V1 = (ENGINE_V1_LEGACY, ENGINE_BUILDING)

# 检索索引不收的消息类型。撤回不是单独一类:微信的 10000 整类(撤回、拍一拍、
# 入群退群、群公告…)都落成 `system`,它自己也把这些渲染成居中的一行灰字,
# 不是谁说过的话。留在索引里的代价是搜一个人名先刷出几十条「X 撤回了一条消息」。
# 转录照常显示、导出照常带上——不可检索不等于不存在。
#
# 排除发生在写入侧而不是查询侧,是因为 v2 的取页与计数全程不离开索引(见模块
# 开头);查询时再按 message_type 过滤,就要为每个候选回查一次消息行,正是这次
# 重写去掉的那笔开销。改动这个集合会让下一次打开归档重跑一遍清扫。
NON_SEARCHABLE_MESSAGE_TYPES = frozenset({"system"})

# 清扫状态。存的是排除集合本身而不是版本号:改了上面那个 frozenset,
# 存量与当前集合自然对不上,下一次打开归档就会重扫,不靠谁记得改版本。
#
# 同样的道理也适用于「按什么判定一行属于排除集合」。索引里那一列 message_type
# 是写入当时的副本,会过期(见 `purge_chunk`),所以判定依据换成了归档自己的
# 那一列——依据也是行为的一部分,一并记进这个串,存量归档下次打开自动重扫。
_PURGE_BASIS = "archive-type"
_EXCLUDED_TYPES_KEY = "excluded_types"
_EXCLUDED_TYPES_WATERMARK_KEY = "excluded_types_watermark"
# 一个不可能等于任何排除集合的值:集合成员是类型名,拼不出问号。
_PURGE_PENDING = "?"

# 用户开没开检索(PSU-2.88)。与 `engine` 是两件事:`engine` 说的是索引结构处在
# 哪一代,这一条说的是**这个归档要不要有索引**。合成一个键会让「用户没开」和
# 「还没迁完」混作一谈,而它们要给用户看的话截然不同。
#
# 键缺失时不敢直接当「没开」:老归档从来没写过这个键,而它们的索引是好的。
# 判据因此是「索引里有没有行」,与 `engine` 的播种同一个依据。
_INITIALIZED_KEY = "initialized"
# 初始化回填读到 `messages.rowid` 的哪里了。与 `backfill_watermark` 分开:那一条
# 走的是 `search_messages`(v1→v2 再推导),这一条走的是归档的消息表。
_INIT_WATERMARK_KEY = "init_watermark"

# 三个取值而不是布尔。回填要跑几分钟,期间索引是**部分**的:报 ready 会让搜不到
# 的那一半看起来像不存在,而报「没开」又不对——它正在开。中间这个状态本来就在
# 契约里(`building`),前端也已经会画。
INIT_NONE = "0"
INIT_BUILDING = "building"
INIT_READY = "1"

# 初始化回填要从归档消息表读的列。缺一列就说明这不是一个本机归档库(远端那份
# 索引库连 `messages` 都没有),此时安全空转。
_INIT_REQUIRED_COLUMNS = frozenset(
    {
        "id",
        "source_account_id",
        "conversation_id",
        "created_at",
        "content_text",
        "sender_uid",
        "sender_display_name",
        "message_type",
        "is_self",
    }
)


def searchable_message_type(message_type: str) -> bool:
    return message_type not in NON_SEARCHABLE_MESSAGE_TYPES


def _excluded_types_state() -> str:
    return f"{','.join(sorted(NON_SEARCHABLE_MESSAGE_TYPES))}@{_PURGE_BASIS}"


class SearchBackend(str, Enum):
    FTS5_TRIGRAM = "fts5_trigram"
    NORMALIZED_GRAMS = "normalized_grams"


class SearchIndexStatus(str, Enum):
    READY = "ready"
    BUILDING = "building"
    UNAVAILABLE = "unavailable"
    # 用户还没开检索。与 `UNAVAILABLE`(索引缺表/缺列,即坏了)分开:一个是「你还
    # 没开」,一个是「它坏了」,而两者今天都会落成「0 条结果」——那句话在归档产品
    # 里的意思是「你没说过这句话」,不能拿它顶替「我没建索引」。
    NOT_INITIALIZED = "not_initialized"


@dataclass(frozen=True)
class IndexedMessage:
    message_id: str
    source_account_id: str
    conversation_id: str
    created_at: int
    content: str
    sender_uid: str = ""
    sender_display_name: str | None = None
    message_type: str = "unknown"
    has_attachment: bool = False
    attachment_file_name: str | None = None
    is_self: bool = False
    #: 机器转写出来的文本。和 `content` 一起进归一化列，所以语音能被搜到；
    #: 分开存是为了命中之后还能说清「这句是机器写的」。
    #:
    #: `None` 是「这次没带」，**保留索引里已有的那份**；`""` 才是「确实没有」。
    #: 导入、回填、远端同步这些重写整行的路都拿不到转写——它们手里只有消息表，
    #: 转写在另一张表、另一个时间到达。缺省若是 `""`，每重写一次就把已经接进来的
    #: 转写抹掉一次，而且不报错：语音只是从此搜不到了。
    transcript: str | None = None


@dataclass(frozen=True)
class SearchHit:
    message_id: str
    conversation_id: str
    created_at: int
    content: str
    sender_uid: str
    sender_display_name: str | None
    message_type: str
    has_attachment: bool
    attachment_file_name: str | None = None
    is_self: bool = False
    # These fields are hydrated from the normalized archive tables at read time
    # rather than duplicated in the FTS index. This keeps legacy indexes
    # readable while allowing the desktop search UI to render the same sender
    # and media affordances as the transcript.
    sender_avatar_ref: str | None = None
    attachment_id: str | None = None
    attachment_kind: str | None = None
    attachment_mime_type: str | None = None
    attachment_size: int | None = None
    attachment_cache_status: str | None = None
    attachment_variant: str | None = None
    attachment_content_available: bool = False


@dataclass(frozen=True)
class SearchPage:
    hits: list[SearchHit]
    next_after: tuple[int, str] | None
    index_status: SearchIndexStatus = SearchIndexStatus.READY
    total_count: int = 0
    conversation_counts: dict[str, int] = field(default_factory=dict[str, int])


class _SearchFilters(TypedDict):
    """`search_page` 交给 v1/v2 查询实现的同一组筛选参数。"""

    conversation_ids: Sequence[str] | None
    sender_uids: Sequence[str] | None
    message_types: Sequence[str] | None
    has_attachment: bool | None
    date_from: int | None
    date_to: int | None
    after: tuple[int, str] | None
    limit: int
    sort: str
    file_classes: Sequence[str] | None


def _searchable_text(content: str, transcript: str) -> str:
    """进归一化列的东西：本人说的 + 机器听的。

    拼起来只是为了让 gram 同时覆盖两边；**两者在库里仍然分开存**，
    所以命中之后还能说清哪一句是机器写的。
    """
    if not transcript:
        return content
    if not content:
        return transcript
    return content + "\n" + transcript


def normalize_search_text(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def escape_match_phrase(value: str) -> str:
    """Return one FTS5 phrase; embedded quotes use FTS5's doubled quote form."""
    return f'"{value.replace(chr(34), chr(34) * 2)}"'


def escape_like(term: str) -> str:
    """Neutralise LIKE wildcards for a literal substring match.

    Lives here rather than beside either caller: the local conversation list
    and the remote one filter on the same user-typed text, and a name
    containing ``%`` has to mean the same thing on both ends. Every caller
    pairs it with ``ESCAPE '\\'``.
    """
    return term.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _grams(value: str) -> tuple[str, ...]:
    if not value:
        return ()
    width = min(3, len(value))
    return tuple(dict.fromkeys(value[index : index + width] for index in range(len(value) - width + 1)))


def _index_grams(value: str) -> tuple[str, ...]:
    grams: dict[str, None] = {}
    for width in range(1, min(3, len(value)) + 1):
        for index in range(len(value) - width + 1):
            grams[value[index : index + width]] = None
    return tuple(grams)


def _index_grams_v2(value: str) -> tuple[str, ...]:
    """Widths 1..2 only. Three characters and above are FTS5 trigram territory,
    and the 3-wide substrings were the bulk of the 35M-row v1 gram table."""
    grams: dict[str, None] = {}
    for width in range(1, min(2, len(value)) + 1):
        for index in range(len(value) - width + 1):
            grams[value[index : index + width]] = None
    return tuple(grams)


def _fnv1a32(value: str) -> int:
    digest = 0x811C9DC5
    for byte in value.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return digest


def docid_floor(created_at: int) -> int:
    """Smallest docid any message of this second can carry."""
    clamped = min(max(created_at, 0), _DOCID_MAX_SECONDS)
    return clamped << _DOCID_SHIFT


def file_class_conditions(file_class: str) -> tuple[str, list[str]]:
    """把一个文件类别翻成 SQL 条件与参数。

    比对的是 `search_messages.attachment_file_name`——索引自己存的那份文件名副本，
    与消息同时写入、同时回填，因此不会比归档旧。用 `GLOB` 而不是 `LIKE`：
    `LIKE` 在 SQLite 里对 ASCII 大小写不敏感，可扩展名本来就按小写归类，
    `lower()` 一次比让两套大小写规则打架清楚。

    `other` 是取反：不属于任何一类，**包括没有扩展名的那些**（真实归档里有 25 个）。
    """
    from chat_archive.file_classes import file_class_extensions, known_extensions

    extensions = file_class_extensions(file_class)
    if extensions:
        clause = " OR ".join(
            ["lower(m.attachment_file_name) GLOB ?"] * len(extensions)
        )
        return f"({clause})", [f"*.{extension}" for extension in extensions]
    known = known_extensions()
    clause = " AND ".join(
        ["lower(COALESCE(m.attachment_file_name, '')) NOT GLOB ?"] * len(known)
    )
    return f"({clause})", [f"*.{extension}" for extension in known]


def column_filter(
    column: str, values: Sequence[str] | Sequence[int]
) -> tuple[str, list[str | int]]:
    """一个值写成 `= ?`，多个值写成 `IN (...)`（PSU-2.74）。

    单值**必须**仍然是 `= ?`：那是今天的形状，多值这条路不该顺手改掉它的计划。
    多值只出现在扇出之后剩下的那些「行内过滤」维度上——被扇出的那个维度每条流
    只有一个值，seek 照旧。
    """
    parameters: list[str | int] = [value for value in values]
    if len(values) == 1:
        return f"{column} = ?", parameters
    placeholders = ", ".join("?" for _ in values)
    return f"{column} IN ({placeholders})", parameters


def file_classes_conditions(file_classes: Sequence[str]) -> tuple[str, list[str]]:
    """多个类别是它们的**并集**——一条 OR，不是多次查询（PSU-2.74）。

    会话与发送人那两个维度多选时必须扇出（`IN` 会让规划器丢掉 seek，真机实测
    7.2 秒），文件类别不用：它本来就是拿文件名做 GLOB 判的一串条件，多一个类别
    只是多几个 GLOB，仍然待在同一条按文件分区的索引里。
    """
    clauses: list[str] = []
    parameters: list[str] = []
    for file_class in file_classes:
        clause, clause_parameters = file_class_conditions(file_class)
        clauses.append(clause)
        parameters.extend(clause_parameters)
    return f"({' OR '.join(clauses)})", parameters


#: 搜索结果的排序线。**只有时间的两个方向**——按大小排在真实归档上实测每翻一页
#: 都要把整个候选集物化再排（最小的「文件」范围 6,287 行也要 315–1,302 ms，
#: 且与翻多深无关），正是 v2 索引当初为了避开而重新设计的那种代价。契约里
#: 因此也只有这两个值，不给一个运行时才会被拒的选项。
SORT_NEWEST = "newest"
SORT_OLDEST = "oldest"
SEARCH_SORTS = (SORT_NEWEST, SORT_OLDEST)


def search_sort_key(hit: SearchHit, *, sort: str) -> tuple[int, str]:
    """一条命中在排序线上的位置。与 SQL 的 `created_at <dir>, message_id ASC` 同一条。

    时间倒序时把时间取负，于是两个方向都是「元组升序」，归并的比较只有一处。
    """
    return (-hit.created_at if sort == SORT_NEWEST else hit.created_at, hit.message_id)


@dataclass(frozen=True)
class BrowseSlice:
    """扇出之后的一条流：被扇出的那个维度只剩一个值，其余维度原样带着。"""

    conversation_ids: tuple[str, ...]
    sender_uids: tuple[str, ...]
    message_types: tuple[str, ...] | None


def browse_fan_out(
    conversation_ids: Sequence[str] | None,
    sender_uids: Sequence[str] | None,
    message_types: Sequence[str] | None,
) -> list[BrowseSlice]:
    """把一次多值浏览拆成几条各自能 seek 的流（PSU-2.74）。

    **只挑一个维度扇出**，其余维度留作行内 `IN` 过滤。没有哪条索引同时带着
    (会话, 类型, 时间)，所以交叉扇出买不到东西——真机上 271,435 行的大会话叠一个
    稀疏类型仍只要 3.1 ms，而流的条数会乘起来。

    顺序是会话 > 发送人 > 类型，按选择度排：一个会话通常比一个类型窄得多。
    一个维度都没多选时只返回一条流，于是走的是与单值**逐字相同**的那条 SQL。

    只用于**浏览**（没有关键词）。带关键词那条路的顺序由 `docid` 给，不是
    (时间, 标识)，在那里归并会换掉比较器、翻页就会跳行；而在那条路上，会话、
    发送人与类型本来就都是 gram/fts 索引行上的过滤列，多值直接落成 `IN` 即可，
    形状不变。
    """
    conversations = tuple(conversation_ids or ())
    senders = tuple(sender_uids or ())
    types = tuple(message_types) if message_types else None
    if len(conversations) > 1:
        return [BrowseSlice((value,), senders, types) for value in conversations]
    if len(senders) > 1:
        return [BrowseSlice(conversations, (value,), types) for value in senders]
    if types is not None and len(types) > 1:
        return [BrowseSlice(conversations, senders, (value,)) for value in types]
    return [BrowseSlice(conversations, senders, types)]


def merge_search_pages(
    pages: Sequence[SearchPage], *, limit: int, sort: str = SORT_NEWEST
) -> SearchPage:
    """把按值扇出的几条流合成一页（PSU-2.74）。

    多选**不能**写成 `IN`：真实归档上两个旧的小会话一起筛，`conversation_id
    IN (?,?)` 让规划器丢掉会话序索引改走账号级全扫，7,187 ms；按值各查一页再
    在这里归并，0.9 ms。所以扇出是实现方式，不是权宜。

    **调用方必须保证各流互不相交**（按会话、发送人或类型扇出天然如此：一条消息
    只有一个会话、一个发送人、一个类型）。计数是直接相加的——相交的流会被算两次，
    而那种错误在界面上表现为「总数比列得出的多」，很难被看出来。

    「还有没有下一页」有两个来源，缺一不可：并集本身超了一页，或者任何一条流
    自己还有下一页（它这一页的命中可能全都排在别人后面而没被取走）。
    """
    if len(pages) == 1:
        return pages[0]
    seen: set[str] = set()
    merged: list[SearchHit] = []
    for page in pages:
        for hit in page.hits:
            if hit.message_id in seen:
                continue
            seen.add(hit.message_id)
            merged.append(hit)
    merged.sort(key=lambda hit: search_sort_key(hit, sort=sort))
    hits = merged[:limit]
    has_more = len(merged) > limit or any(page.next_after is not None for page in pages)
    next_after = (hits[-1].created_at, hits[-1].message_id) if has_more and hits else None
    conversation_counts: dict[str, int] = {}
    for page in pages:
        for conversation_id, count in page.conversation_counts.items():
            conversation_counts[conversation_id] = (
                conversation_counts.get(conversation_id, 0) + count
            )
    status = (
        SearchIndexStatus.BUILDING
        if any(page.index_status is SearchIndexStatus.BUILDING for page in pages)
        else pages[0].index_status
        if pages
        else SearchIndexStatus.READY
    )
    return SearchPage(
        hits=hits,
        next_after=next_after,
        index_status=status,
        total_count=sum(page.total_count for page in pages),
        conversation_counts=conversation_counts,
    )



def docid_first_candidate(created_at: int, message_id: str) -> int:
    return docid_floor(created_at) | (_fnv1a32(message_id) & _DOCID_DISAMBIG_MASK)


def probe_fts5_trigram(connection: sqlite3.Connection) -> bool:
    token = secrets.token_hex(8)
    table = f"_trigram_probe_{token}"
    try:
        connection.execute(
            f"CREATE VIRTUAL TABLE temp.{table} "
            "USING fts5(content, tokenize='trigram')"
        )
    except sqlite3.OperationalError:
        return False
    finally:
        connection.execute(f"DROP TABLE IF EXISTS temp.{table}")
    return True


class MessageSearchIndex:
    def __init__(
        self,
        connection: sqlite3.Connection,
        *,
        force_fallback: bool = False,
        ensure_schema: bool = True,
    ) -> None:
        self._connection = connection
        self._connection.row_factory = sqlite3.Row
        self._account_refs: dict[str, int] = {}
        self._conversation_refs: dict[tuple[int, str], int] = {}
        if ensure_schema:
            native = not force_fallback and probe_fts5_trigram(connection)
        else:
            # Read-only connections must not run DDL or the write-based probe;
            # the backend is whatever the writer materialized.
            native = not force_fallback and self._has_fts_table(connection)
        self.backend = SearchBackend.FTS5_TRIGRAM if native else SearchBackend.NORMALIZED_GRAMS
        if ensure_schema:
            self._create_schema()
        self._identity_index = self._has_index(connection, _IDENTITY_INDEX)
        # 只在它真的存在时才钉：`INDEXED BY` 找不到索引会让整条查询报错，
        # 而这里想要的是「快一点」，不是「没有就别答了」。
        self._file_class_index = self._has_index(connection, _FILE_CLASS_INDEX)
        self._file_order_index = self._has_index(connection, _FILE_ORDER_INDEX)

    @staticmethod
    def _has_fts_table(connection: sqlite3.Connection) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' "
            "AND name IN ('search_fts_v2', 'search_messages_fts')"
        ).fetchone()
        return row is not None

    @staticmethod
    def _has_index(connection: sqlite3.Connection, name: str) -> bool:
        row = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'index' AND name = ?", (name,)
        ).fetchone()
        return row is not None

    @staticmethod
    def _has_table(connection: sqlite3.Connection, name: str) -> bool:
        return _table_exists(connection, name)

    def engine(self) -> str:
        """Durable state read, never cached across statements.

        A concurrent backfill can flip the archive to v2 and drop the v1
        structures at any moment; whoever asked one statement ago must not act
        on the answer it got then. Writers re-read inside their transaction,
        where SQLite's single-writer lock makes the answer stable.
        """
        if not self._has_table(self._connection, "search_index_state"):
            return ENGINE_V1_LEGACY
        row = self._connection.execute(
            "SELECT value FROM search_index_state WHERE key = 'engine'"
        ).fetchone()
        return str(row[0]) if row is not None else ENGINE_V1_LEGACY

    def initialization_state(self) -> str:
        """这个归档的检索索引处在哪一步:没开 / 正在建 / 建好了。

        缺键时回落到「索引里有没有行」而不是「没开」:只读连接不跑 DDL,所以一个
        还没被写入方打开过的老归档根本没有这个键,而它的索引是好的。反过来,一个
        真的没建过索引的归档这里也没有行,答案同样正确。
        """
        if not self._has_table(self._connection, "search_index_state"):
            return INIT_READY if self._index_has_rows() else INIT_NONE
        row = self._connection.execute(
            f"SELECT value FROM search_index_state WHERE key = '{_INITIALIZED_KEY}'"
        ).fetchone()
        if row is None:
            return INIT_READY if self._index_has_rows() else INIT_NONE
        value = str(row[0])
        if value in (INIT_BUILDING, INIT_READY):
            return value
        # 「没开」只在索引确实是空的时候才作数。任何往索引里写过行的路径——导入、
        # 远端那份镜像、初始化回填——都使这个键过时了,而它们不该各自记得回来改它。
        # 于是行是权威,键只是一个否定信号,自愈。
        return INIT_READY if self._index_has_rows() else INIT_NONE

    def initialized(self) -> bool:
        """写入侧的门:正在建也算开着,新消息必须照常进索引。

        这是 PSU-2.88 的一条要害。索引写入原本由**每次导入的选项**门控,默认改关
        之后就会出现「用户点了初始化 → 下次导入的新消息不进索引 → 状态仍报 ready」,
        搜不到的消息被说成没有过。门控因此挪到归档级状态上。
        """
        return self.initialization_state() != INIT_NONE

    def _index_has_rows(self) -> bool:
        if not self._has_table(self._connection, "search_messages"):
            return False
        return (
            self._connection.execute("SELECT 1 FROM search_messages LIMIT 1").fetchone()
            is not None
        )

    def _create_schema(self) -> None:
        migration_started = False
        try:
            # Schema upgrades can be initiated by import and query connections.
            # Acquire SQLite's writer lock before inspecting legacy columns so a
            # second connection rechecks the durable schema after this commit.
            #
            # Backfills that must stay atomic with their ALTER run inside this
            # lock (measured 44 ms steady-state on the old 82,952-row archive;
            # the one-off column backfill was 10.1 s there). The v2 message
            # backfill is NOT run here: on the current 729k-row archive it takes
            # minutes, so it streams in short transactions via `build_chunk`
            # while v1 keeps answering queries.
            self._connection.execute("BEGIN IMMEDIATE")
            migration_started = True
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_messages (
                    message_id TEXT PRIMARY KEY,
                    source_account_id TEXT NOT NULL,
                    conversation_id TEXT NOT NULL,
                    created_at INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    normalized_content TEXT NOT NULL,
                    sender_uid TEXT NOT NULL DEFAULT '',
                    sender_display_name TEXT,
                    message_type TEXT NOT NULL DEFAULT 'unknown',
                    has_attachment INTEGER NOT NULL DEFAULT 0,
                    attachment_file_name TEXT,
                    transcript TEXT NOT NULL DEFAULT '',
                    is_self INTEGER NOT NULL DEFAULT 0
                        CHECK(is_self IN (0, 1)),
                    metadata_complete INTEGER NOT NULL DEFAULT 0
                        CHECK(metadata_complete IN (0, 1)),
                    docid INTEGER
                )
                """
            )
            columns = {
                str(row[1])
                for row in self._connection.execute(
                    "PRAGMA table_info(search_messages)"
                ).fetchall()
            }
            additions = {
                "sender_uid": "TEXT NOT NULL DEFAULT ''",
                "sender_display_name": "TEXT",
                "message_type": "TEXT NOT NULL DEFAULT 'unknown'",
                "has_attachment": "INTEGER NOT NULL DEFAULT 0",
                "attachment_file_name": "TEXT",
                "transcript": "TEXT NOT NULL DEFAULT ''",
                "is_self": "INTEGER NOT NULL DEFAULT 0 CHECK(is_self IN (0, 1))",
                "metadata_complete": (
                    "INTEGER NOT NULL DEFAULT 0 CHECK(metadata_complete IN (0, 1))"
                ),
                "docid": "INTEGER",
            }
            for name, declaration in additions.items():
                if name not in columns:
                    self._connection.execute(
                        f"ALTER TABLE search_messages ADD COLUMN {name} {declaration}"
                    )
                    if name == "attachment_file_name":
                        self._backfill_attachment_file_names()
                    if name == "transcript":
                        self._backfill_transcripts()
                    if name == "is_self":
                        self._backfill_self_flags()
            retyped = self._backfill_legacy_metadata()
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_account_order
                ON search_messages(source_account_id, created_at DESC, message_id)
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_account_filters
                ON search_messages(
                    source_account_id, sender_uid, message_type, has_attachment,
                    created_at DESC, message_id
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_conversation_order
                ON search_messages(
                    source_account_id, conversation_id, created_at DESC, message_id
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_account_completeness
                ON search_messages(source_account_id, metadata_complete)
                """
            )
            # Counting a filtered browse -- a panel opened with no keyword yet.
            # `metadata_complete` lived only in the account-wide index above and
            # `conversation_id` only in the order index, so the planner took one
            # and filtered the rest by hand: 729k rows read to count one
            # conversation's 271k, 1,079 ms cold. Holding all of them makes the
            # count covering, and leading with the conversation lets it seek
            # rather than scan. Ordered by selectivity, not by the WHERE clause.
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_conversation_filters
                ON search_messages(
                    source_account_id, conversation_id, metadata_complete,
                    message_type, has_attachment
                )
                """
            )
            # PSU-2.66 makes "sender" one of the global search scopes: pick a
            # name from the directory and the whole list becomes that person's.
            # Nothing indexed that before, because the only way to ask was to
            # type a uid. `ix_search_messages_account_filters` leads with the
            # sender but puts `message_type`/`has_attachment` before the sort
            # columns, so with those unconstrained the planner cannot seek past
            # them -- it fell back to the account-wide scans. Measured on the
            # real 729,507-row archive, for a prolific sender (18,996 messages)
            # and a rare one (5), cold then warm:
            #
            #   per-conversation count   8,250 / 1,501 ms cold, ~800 ms warm
            #   first page                   1 / 2,427 ms cold,   387 ms warm
            #
            # The rare sender is the bad case for the page: with only the
            # account-wide order index the planner walks the whole account to
            # find five rows.
            #
            # Two indexes rather than one, the same split the conversation scope
            # already uses above: one holds the sort line, the other the group
            # key, and neither can serve the other's plan -- putting
            # `conversation_id` before `created_at` costs the page its ordering,
            # putting it after costs the count its grouping, and both were
            # measured going back to seconds. With both: count 5.1 / 0.1 ms
            # covering, page 0.3 / 0.1 ms (the page reads content columns, so it
            # seeks and looks up ≤LIMIT rows rather than covering).
            # 3.9 s to build, +40 MB on a 4.1 GB archive.
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_sender_order
                ON search_messages(
                    source_account_id, sender_uid, metadata_complete,
                    created_at DESC, message_id
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_sender_filters
                ON search_messages(
                    source_account_id, sender_uid, metadata_complete, conversation_id
                )
                """
            )
            # PSU-2.66 also makes the message type a scope of its own (文本 /
            # 图片与视频 / 文件 / 链接 …). `ix_search_messages_account_filters`
            # carries the type but behind `sender_uid`, so with the sender
            # unconstrained the planner cannot seek to it and falls back to the
            # account-wide order index -- it then walks the whole account until
            # it has collected a page, which for a sparse type never happens
            # early. Measured on the real archive, a page of the 应用程序 file
            # class cost 2,644 ms cold / 263 ms warm; per-conversation counts
            # for a type cost 103-176 ms. With these two: page 0.0-14.4 ms,
            # counts 0.3-10.5 ms. 3.7 s to build.
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_type_order
                ON search_messages(
                    source_account_id, message_type, metadata_complete,
                    created_at DESC, message_id
                )
                """
            )
            self._connection.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_search_messages_type_filters
                ON search_messages(
                    source_account_id, message_type, metadata_complete, conversation_id
                )
                """
            )
            # 文件类别的分会话计数。`type_filters` 停在 conversation_id，而类别
            # 是拿 `attachment_file_name` 做 GLOB 判的，于是那一条要么逐行回表、
            # 要么被规划器整个放弃——真实归档上实测端到端 2 秒（温），首次 10 秒，
            # 而取页只要 9 ms：慢的全在计数这一边，我最初只量了取页，漏了它。
            # 把文件名折进来之后这条计数变成覆盖扫描：doc 578→11 ms、
            # app 41→10 ms、other 84→49 ms。2.8 s 建成，+36 MB。
            self._connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {_FILE_CLASS_INDEX}
                ON search_messages(
                    source_account_id, message_type, metadata_complete,
                    attachment_file_name, conversation_id
                )
                """
            )
            # 文件类别的**取页**。上面那条只救了计数：取页 seek 到
            # `message_type='file'` 之后按时间倒序扫 `type_order`，而类别是拿
            # `attachment_file_name` 做 GLOB 判的，那一列不在 `type_order` 里
            # ——于是每个候选都要回一次主表才知道它算不算这一类。凑够一页要走
            # 多深，就要回多少次表。真实归档（6,287 条文件）实测：
            # doc 2,666 条走 127 行 0.3 ms；slide 90 条走 1,089 行 5.1 ms
            # （冷 610 ms）；audio 45 条把 6,287 行全扫一遍 31.4 ms。
            # 把文件名排在排序线之后当载荷，GLOB 就在索引行上判掉了，主表只对
            # 真要返回的那 ≤51 行回表。分区到 file 行上，所以它只有几千行大。
            self._connection.execute(
                f"""
                CREATE INDEX IF NOT EXISTS {_FILE_ORDER_INDEX}
                ON search_messages(
                    source_account_id, metadata_complete, created_at DESC,
                    message_id, attachment_file_name
                ) WHERE message_type = 'file'
                """
            )
            self._connection.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS ix_search_messages_docid "
                "ON search_messages(docid)"
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_index_state (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            engine_row = self._connection.execute(
                "SELECT value FROM search_index_state WHERE key = 'engine'"
            ).fetchone()
            had_grams_v2 = self._has_table(self._connection, "search_grams_v2")
            had_fts_v2 = self._has_table(self._connection, "search_fts_v2")
            if engine_row is None:
                has_rows = (
                    self._connection.execute(
                        "SELECT 1 FROM search_messages LIMIT 1"
                    ).fetchone()
                    is not None
                )
                engine = ENGINE_BUILDING if has_rows else ENGINE_V2
                self._connection.execute(
                    "INSERT INTO search_index_state(key, value) VALUES ('engine', ?)",
                    (engine,),
                )
                self._connection.execute(
                    "INSERT OR REPLACE INTO search_index_state(key, value) "
                    "VALUES ('backfill_watermark', '0')"
                )
            else:
                engine = str(engine_row[0])
                v2_lost = not had_grams_v2 or (
                    self.backend is SearchBackend.FTS5_TRIGRAM and not had_fts_v2
                )
                has_rows = (
                    self._connection.execute(
                        "SELECT 1 FROM search_messages LIMIT 1"
                    ).fetchone()
                    is not None
                )
                if v2_lost and has_rows:
                    # The state row promises an index the file no longer holds
                    # (a partial restore, a pruned table, or a fallback-built
                    # archive opened by an FTS5-capable build). Recreating the
                    # shape below and calling it done would answer those
                    # queries with silence forever; re-derive instead. While
                    # v1 still exists it keeps serving complete results; once
                    # it is gone the archive honestly reports itself building.
                    engine = (
                        ENGINE_BUILDING
                        if engine == ENGINE_BUILDING
                        else ENGINE_REBUILDING
                    )
                    self._connection.execute(
                        "UPDATE search_index_state SET value = ? WHERE key = 'engine'",
                        (engine,),
                    )
                    self._connection.execute(
                        "INSERT OR REPLACE INTO search_index_state(key, value) "
                        "VALUES ('backfill_watermark', '0')"
                    )
            # 播种「开没开」。判据与上面的 `engine` 一致——索引里有行就是开着的,
            # 因为在这一版之前,有行只可能是导入路径写进去的。空归档(含刚建好、
            # 一条消息都没有的)落成「没开」,与新归档的默认值一致:它没有东西可搜,
            # 而真要搜的时候初始化是瞬间的。
            if (
                self._connection.execute(
                    f"SELECT 1 FROM search_index_state WHERE key = '{_INITIALIZED_KEY}'"
                ).fetchone()
                is None
            ):
                self._connection.execute(
                    "INSERT INTO search_index_state(key, value) VALUES (?, ?)",
                    (
                        _INITIALIZED_KEY,
                        INIT_READY if self._index_has_rows() else INIT_NONE,
                    ),
                )
            if engine in _WRITES_V1:
                # v1 must keep answering (and, while building, keep absorbing
                # writes) until the backfill flips the archive.
                self._connection.execute(
                    """
                    CREATE TABLE IF NOT EXISTS search_grams (
                        gram TEXT NOT NULL,
                        message_id TEXT NOT NULL,
                        PRIMARY KEY (gram, message_id),
                        FOREIGN KEY (message_id) REFERENCES search_messages(message_id)
                            ON DELETE CASCADE
                    ) WITHOUT ROWID
                    """
                )
                self._connection.execute(
                    "CREATE INDEX IF NOT EXISTS ix_search_grams_message "
                    "ON search_grams(message_id)"
                )
                self._connection.execute(
                    f"""
                    CREATE INDEX IF NOT EXISTS {_IDENTITY_INDEX}
                    ON search_messages(
                        message_id, source_account_id, conversation_id, metadata_complete
                    )
                    """
                )
                if self.backend is SearchBackend.FTS5_TRIGRAM:
                    self._connection.execute(
                        """
                        CREATE VIRTUAL TABLE IF NOT EXISTS search_messages_fts
                        USING fts5(
                            message_id UNINDEXED,
                            normalized_content,
                            tokenize='trigram'
                        )
                        """
                    )
            else:
                self._drop_v1_structures_if_empty()
            self._initialize_purge_state(retyped=retyped)
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_accounts (
                    account_ref INTEGER PRIMARY KEY,
                    account_id TEXT NOT NULL UNIQUE
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_conversations (
                    conversation_ref INTEGER PRIMARY KEY,
                    account_ref INTEGER NOT NULL
                        REFERENCES search_accounts(account_ref),
                    conversation_id TEXT NOT NULL,
                    UNIQUE(account_ref, conversation_id)
                )
                """
            )
            self._connection.execute(
                """
                CREATE TABLE IF NOT EXISTS search_grams_v2 (
                    gram TEXT NOT NULL,
                    account_ref INTEGER NOT NULL,
                    neg_docid INTEGER NOT NULL,
                    conversation_ref INTEGER NOT NULL,
                    PRIMARY KEY (gram, account_ref, neg_docid)
                ) WITHOUT ROWID
                """
            )
            if self.backend is SearchBackend.FTS5_TRIGRAM:
                self._connection.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS search_fts_v2
                    USING fts5(
                        normalized_content,
                        account_ref UNINDEXED,
                        conversation_ref UNINDEXED,
                        tokenize='trigram'
                    )
                    """
                )
            self._connection.commit()
        except BaseException:
            if migration_started and self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _initialize_purge_state(self, *, retyped: bool) -> None:
        """Record whether this archive still holds rows this build would not index.

        Two ways it can: it was written before the type was excluded, or
        `_backfill_legacy_metadata` just learned the real type of rows that
        carried none (the 10000 rows that predate the mapping are exactly that
        case). Both are recorded the same way -- the stored set stops matching
        the current one -- and both are answered by `purge_chunk`.
        """
        current = _excluded_types_state()
        row = self._connection.execute(
            "SELECT value FROM search_index_state WHERE key = ?",
            (_EXCLUDED_TYPES_KEY,),
        ).fetchone()
        stored = None if row is None else str(row[0])
        if stored == current and not retyped:
            return
        has_rows = (
            self._connection.execute("SELECT 1 FROM search_messages LIMIT 1").fetchone()
            is not None
        )
        # An empty index has no history to sweep, whatever the set says.
        value = _PURGE_PENDING if has_rows else current
        if stored == value and not retyped:
            # Already pending. Leave the watermark alone: restarting a sweep
            # from zero on every open would be correct but would re-read the
            # whole table each time the server starts.
            return
        self._connection.execute(
            "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, ?)",
            (_EXCLUDED_TYPES_KEY, value),
        )
        self._connection.execute(
            "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, '0')",
            (_EXCLUDED_TYPES_WATERMARK_KEY,),
        )

    def purge_pending(self) -> bool:
        """Does this archive owe the index a sweep of excluded rows?

        Read durably rather than cached, for `engine`'s reason: a concurrent
        sweep can finish at any moment, and archives that predate this schema
        have no state row at all -- those report false here and are migrated by
        the v2 backfill, which runs `_create_schema` and settles the question.
        """
        if not self._has_table(self._connection, "search_index_state"):
            return False
        row = self._connection.execute(
            "SELECT value FROM search_index_state WHERE key = ?",
            (_EXCLUDED_TYPES_KEY,),
        ).fetchone()
        return row is None or str(row[0]) != _excluded_types_state()

    def _drop_v1_structures_if_empty(self) -> None:
        """A v2 archive pays for v1 structures on every write; shed empty ones.

        Non-empty ones are left alone -- they mean someone re-tagged the state
        by hand, and destroying data is not this method's call. The normal
        teardown of a populated v1 lives in `finalize_legacy_teardown`.
        """
        for table in ("search_grams", "search_messages_fts"):
            if not self._has_table(self._connection, table):
                continue
            if self._connection.execute(f"SELECT 1 FROM {table} LIMIT 1").fetchone() is None:
                self._connection.execute(f"DROP TABLE {table}")
        self._connection.execute(f"DROP INDEX IF EXISTS {_IDENTITY_INDEX}")

    def _backfill_legacy_metadata(self) -> bool:
        """Fill in metadata legacy rows lack; returns whether any row changed.

        The answer matters to `_initialize_purge_state`: a row that just learned
        it is a system notice has to be swept out of the index, and it is this
        statement that teaches it.
        """
        message_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        attachment_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(attachments)").fetchall()
        }
        if not {
            "id",
            "sender_uid",
            "sender_display_name",
            "message_type",
            "is_self",
        } <= message_columns or "message_id" not in attachment_columns:
            return False
        updated = self._connection.execute(
            """
            UPDATE search_messages
            SET sender_uid = (
                    SELECT messages.sender_uid FROM messages
                    WHERE messages.id = search_messages.message_id
                      AND messages.source_account_id = search_messages.source_account_id
                ),
                sender_display_name = (
                    SELECT messages.sender_display_name FROM messages
                    WHERE messages.id = search_messages.message_id
                      AND messages.source_account_id = search_messages.source_account_id
                ),
                message_type = (
                    SELECT messages.message_type FROM messages
                    WHERE messages.id = search_messages.message_id
                      AND messages.source_account_id = search_messages.source_account_id
                ),
                is_self = COALESCE((
                    SELECT messages.is_self FROM messages
                    WHERE messages.id = search_messages.message_id
                      AND messages.source_account_id = search_messages.source_account_id
                ), 0),
                has_attachment = CASE WHEN EXISTS (
                    SELECT 1 FROM attachments
                    WHERE attachments.message_id = search_messages.message_id
                ) THEN 1 ELSE 0 END,
                metadata_complete = 1
            WHERE metadata_complete = 0
              AND EXISTS (
                  SELECT 1 FROM messages
                  WHERE messages.id = search_messages.message_id
                    AND messages.source_account_id = search_messages.source_account_id
              )
            """
        )
        return updated.rowcount > 0

    def _backfill_attachment_file_names(self) -> None:
        """Fill the new column once, at the moment it is added.

        `_backfill_legacy_metadata` is gated on `metadata_complete = 0`, which is
        false for every row an existing archive already indexed. Reusing that gate
        here would leave old archives without file names forever.
        """
        attachment_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(attachments)").fetchall()
        }
        if not {"id", "message_id", "file_name"} <= attachment_columns:
            return
        self._connection.execute(
            """
            UPDATE search_messages
            SET attachment_file_name = (
                SELECT file_name FROM attachments
                WHERE attachments.message_id = search_messages.message_id
                  AND attachments.file_name IS NOT NULL
                ORDER BY attachments.id
                LIMIT 1
            )
            """
        )

    def _backfill_self_flags(self) -> None:
        """Fill the new column once, at the moment it is added.

        Same reason as `_backfill_attachment_file_names`: rows an existing archive
        already indexed carry `metadata_complete = 1`, so the gated backfill would
        never reach them and every old hit would claim it came from someone else.
        """
        message_columns = {
            str(row[1])
            for row in self._connection.execute("PRAGMA table_info(messages)").fetchall()
        }
        if not {"id", "source_account_id", "is_self"} <= message_columns:
            return
        self._connection.execute(
            """
            UPDATE search_messages
            SET is_self = COALESCE((
                SELECT messages.is_self FROM messages
                WHERE messages.id = search_messages.message_id
                  AND messages.source_account_id = search_messages.source_account_id
            ), 0)
            """
        )

    # --- v2 write plumbing -------------------------------------------------

    def _account_ref(self, account_id: str) -> int:
        cached = self._account_refs.get(account_id)
        if cached is not None:
            return cached
        self._connection.execute(
            "INSERT OR IGNORE INTO search_accounts(account_id) VALUES (?)", (account_id,)
        )
        ref = int(
            self._connection.execute(
                "SELECT account_ref FROM search_accounts WHERE account_id = ?",
                (account_id,),
            ).fetchone()[0]
        )
        self._account_refs[account_id] = ref
        return ref

    def _conversation_ref(self, account_ref: int, conversation_id: str) -> int:
        key = (account_ref, conversation_id)
        cached = self._conversation_refs.get(key)
        if cached is not None:
            return cached
        self._connection.execute(
            "INSERT OR IGNORE INTO search_conversations(account_ref, conversation_id) "
            "VALUES (?, ?)",
            key,
        )
        ref = int(
            self._connection.execute(
                "SELECT conversation_ref FROM search_conversations "
                "WHERE account_ref = ? AND conversation_id = ?",
                key,
            ).fetchone()[0]
        )
        self._conversation_refs[key] = ref
        return ref

    def _lookup_account_ref(self, account_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT account_ref FROM search_accounts WHERE account_id = ?", (account_id,)
        ).fetchone()
        return None if row is None else int(row[0])

    def _lookup_conversation_ref(self, account_ref: int, conversation_id: str) -> int | None:
        row = self._connection.execute(
            "SELECT conversation_ref FROM search_conversations "
            "WHERE account_ref = ? AND conversation_id = ?",
            (account_ref, conversation_id),
        ).fetchone()
        return None if row is None else int(row[0])

    def _allocate_docid(self, created_at: int, message_id: str) -> int:
        candidate = docid_first_candidate(created_at, message_id)
        while True:
            taken = self._connection.execute(
                "SELECT message_id FROM search_messages WHERE docid = ?", (candidate,)
            ).fetchone()
            if taken is None or str(taken[0]) == message_id:
                return candidate
            candidate += 1

    def _fetch_indexed_rows(
        self, message_ids: Sequence[str]
    ) -> dict[str, sqlite3.Row]:
        rows: dict[str, sqlite3.Row] = {}
        for offset in range(0, len(message_ids), 500):
            batch = tuple(message_ids[offset : offset + 500])
            placeholders = ", ".join("?" for _ in batch)
            for row in self._connection.execute(
                f"""
                SELECT message_id, source_account_id, conversation_id, created_at,
                       normalized_content, docid
                FROM search_messages WHERE message_id IN ({placeholders})
                """,
                batch,
            ).fetchall():
                rows[str(row["message_id"])] = row
        return rows

    def _delete_v2_entries(self, existing: Sequence[sqlite3.Row]) -> None:
        """Point-delete the v2 rows a message previously produced.

        The grams are recomputed from the stored normalized text instead of
        being looked up through a message->gram index; determinism makes that
        index unnecessary, which is most of the v1 table's weight.
        """
        gram_keys: list[tuple[str, int, int]] = []
        docids: list[int] = []
        for row in existing:
            docid = row["docid"]
            if docid is None:
                continue
            docid = int(docid)
            docids.append(docid)
            account_ref = self._lookup_account_ref(str(row["source_account_id"]))
            if account_ref is None:
                continue
            for gram in _index_grams_v2(str(row["normalized_content"])):
                gram_keys.append((gram, account_ref, -docid))
        if gram_keys:
            self._connection.executemany(
                "DELETE FROM search_grams_v2 "
                "WHERE gram = ? AND account_ref = ? AND neg_docid = ?",
                gram_keys,
            )
        if docids and self.backend is SearchBackend.FTS5_TRIGRAM:
            for offset in range(0, len(docids), 500):
                batch = docids[offset : offset + 500]
                placeholders = ", ".join("?" for _ in batch)
                self._connection.execute(
                    f"DELETE FROM search_fts_v2 WHERE rowid IN ({placeholders})",
                    batch,
                )

    def _insert_v2_entries(
        self,
        *,
        docid: int,
        account_ref: int,
        conversation_ref: int,
        normalized: str,
    ) -> None:
        self._connection.executemany(
            "INSERT OR IGNORE INTO search_grams_v2"
            "(gram, account_ref, neg_docid, conversation_ref) VALUES (?, ?, ?, ?)",
            (
                (gram, account_ref, -docid, conversation_ref)
                for gram in _index_grams_v2(normalized)
            ),
        )
        if self.backend is SearchBackend.FTS5_TRIGRAM:
            self._connection.execute(
                "INSERT INTO search_fts_v2"
                "(rowid, normalized_content, account_ref, conversation_ref) "
                "VALUES (?, ?, ?, ?)",
                (docid, normalized, account_ref, conversation_ref),
            )

    # --- write paths -------------------------------------------------------

    def upsert(self, message: IndexedMessage) -> None:
        self.upsert_many((message,))

    def _retry_after_flip(self, operation: Callable[[], object]) -> object:
        """Run a write; if the backfill flipped the archive mid-flight, rerun once.

        The engine is read at the top of each write transaction, but SQLite
        only takes the write lock at the first DML statement. In that gap a
        finishing backfill can flip to v2 and drop the v1 tables. The flip is
        one-way and the failed transaction rolled back whole, so a single
        rerun -- which now reads v2 -- is exact, not a heuristic.
        """
        try:
            return operation()
        except sqlite3.OperationalError as error:
            if "no such table" in str(error) and self.engine() == ENGINE_V2:
                return operation()
            raise

    def upsert_many(self, messages: Sequence[IndexedMessage]) -> None:
        if not messages:
            return
        deduplicated = {message.message_id: message for message in messages}
        self._retry_after_flip(lambda: self._upsert_many_once(deduplicated))

    def rehome_conversations(
        self, moves: Sequence[tuple[str, str, str]]
    ) -> tuple[str, ...]:
        """把已索引的消息**原地**改到另一条会话下；返回原地改不了、要整条重写的 id。

        `moves` 是 `(message_id, 记录现在所属的账号键, 新会话)`。索引行的账号与记录
        现状不一致就**交还**——账号归并与会话归并撞上同一条记录时（账号归并搬完记了
        账本、还没重写，会话归并又把它搬了一次），按索引里的旧账号改 gram / FTS 的
        `conversation_ref` 会把这条钉死在旧账号下，而回填不会再访它。交还之后由
        账本那条路按记录表现状整条重写（评审订正）。

        归并不改正文，只改归属（PSU-2.101）。索引里带会话的只有三处：
        `search_messages.conversation_id`、`search_grams_v2.conversation_ref`
        （主键 `(gram, account_ref, neg_docid)` 不含它，原地 UPDATE）、
        `search_fts_v2.conversation_ref`（UNINDEXED 列，FTS5 原地改不重建 posting）。
        整条重写要把正文再切一遍 gram、再喂一遍 FTS——真机拷贝上 2,000 条 6.0 s，
        只改三处 3.35 s，而其中 gram 表那 2.1 s 是必须付的：1–2 字查询靠它答。

        * 不在索引里的 id **跳过**而不是交还：整条重写它也没用（没有正文可写），
          回填 / 提交路径会按记录表现状把它写进来。
        * 没有 docid 的行（v1 遗留）与非 v2 引擎交还给调用方整条重写。
        * 已经在目标会话下的行是空操作，所以重放是幂等的。
        """
        if not moves:
            return ()
        wanted = {message_id: (account, target) for message_id, account, target in moves}

        def run() -> tuple[str, ...]:
            with self._connection:
                if self.engine() != ENGINE_V2:
                    return tuple(wanted)
                existing = self._fetch_indexed_rows(tuple(wanted))
                leftovers: list[str] = []
                for message_id, row in existing.items():
                    account, target = wanted[message_id]
                    if row["docid"] is None or str(row["source_account_id"]) != account:
                        leftovers.append(message_id)
                        continue
                    if str(row["conversation_id"]) == target:
                        continue
                    account_ref = self._lookup_account_ref(str(row["source_account_id"]))
                    if account_ref is None:
                        leftovers.append(message_id)
                        continue
                    conversation_ref = self._conversation_ref(account_ref, target)
                    docid = int(row["docid"])
                    self._connection.execute(
                        "UPDATE search_messages SET conversation_id = ? WHERE message_id = ?",
                        (target, message_id),
                    )
                    self._connection.executemany(
                        "UPDATE search_grams_v2 SET conversation_ref = ? "
                        "WHERE gram = ? AND account_ref = ? AND neg_docid = ?",
                        (
                            (conversation_ref, gram, account_ref, -docid)
                            for gram in _index_grams_v2(str(row["normalized_content"]))
                        ),
                    )
                    if self.backend is SearchBackend.FTS5_TRIGRAM:
                        self._connection.execute(
                            "UPDATE search_fts_v2 SET conversation_ref = ? WHERE rowid = ?",
                            (conversation_ref, docid),
                        )
                return tuple(leftovers)

        result = self._retry_after_flip(run)
        assert isinstance(result, tuple)
        return cast("tuple[str, ...]", result)

    def _upsert_many_once(self, deduplicated: dict[str, IndexedMessage]) -> None:
        with self._connection:
            engine = self.engine()
            existing: dict[str, sqlite3.Row] = {}
            if engine != ENGINE_V1_LEGACY:
                # Legacy archives predate the docid column this reads.
                existing = self._fetch_indexed_rows(tuple(deduplicated))
                self._delete_v2_entries(tuple(existing.values()))
            if engine in _WRITES_V1:
                self._delete_existing_in_transaction(tuple(deduplicated))
            kept = self._stored_transcripts(
                tuple(
                    message_id
                    for message_id, message in deduplicated.items()
                    if message.transcript is None
                )
            )
            if kept:
                deduplicated = {
                    message_id: (
                        replace(message, transcript=kept[message_id])
                        if message_id in kept
                        else message
                    )
                    for message_id, message in deduplicated.items()
                }
            excluded = [
                message_id
                for message_id, message in deduplicated.items()
                if not searchable_message_type(message.message_type)
            ]
            if excluded:
                # Declining to write is not enough. An earlier build indexed
                # this type, and a replay can retype a row into it -- both
                # leave a row here that only a delete removes. The entries it
                # produced are already gone above.
                self._delete_message_rows(excluded)
            for message in deduplicated.values():
                if not searchable_message_type(message.message_type):
                    continue
                self._insert_in_transaction(
                    message, engine=engine, existing=existing.get(message.message_id)
                )

    def _stored_transcripts(self, message_ids: Sequence[str]) -> dict[str, str]:
        """这几条在索引里已有的转写（只返回非空的）。见 `IndexedMessage.transcript`。"""
        found: dict[str, str] = {}
        for offset in range(0, len(message_ids), 500):
            batch = tuple(message_ids[offset : offset + 500])
            placeholders = ", ".join("?" for _ in batch)
            for row in self._connection.execute(
                f"SELECT message_id, transcript FROM search_messages "
                f"WHERE message_id IN ({placeholders}) AND transcript != ''",
                batch,
            ).fetchall():
                found[str(row[0])] = str(row[1])
        return found

    def _delete_message_rows(self, message_ids: Sequence[str]) -> None:
        """Drop index rows by id. Callers clear the derived entries first."""
        for offset in range(0, len(message_ids), 500):
            batch = tuple(message_ids[offset : offset + 500])
            placeholders = ", ".join("?" for _ in batch)
            self._connection.execute(
                f"DELETE FROM search_messages WHERE message_id IN ({placeholders})",
                batch,
            )

    def _delete_existing_in_transaction(self, message_ids: Sequence[str]) -> None:
        # Keep each statement under SQLite's conservative 999-variable limit.
        # v1 FTS5 cannot use an UNINDEXED message_id for deletion, so one IN
        # scan per import batch replaces hundreds of full virtual-table scans.
        # (v2 deletes by rowid and pays no scan at all.)
        for offset in range(0, len(message_ids), 500):
            batch = tuple(message_ids[offset : offset + 500])
            placeholders = ", ".join("?" for _ in batch)
            self._connection.execute(
                f"DELETE FROM search_grams WHERE message_id IN ({placeholders})",
                batch,
            )
            if self.backend is SearchBackend.FTS5_TRIGRAM:
                self._connection.execute(
                    f"DELETE FROM search_messages_fts "
                    f"WHERE message_id IN ({placeholders})",
                    batch,
                )

    def _insert_in_transaction(
        self,
        message: IndexedMessage,
        *,
        engine: str,
        existing: sqlite3.Row | None,
    ) -> None:
        transcript = message.transcript or ""
        normalized = normalize_search_text(_searchable_text(message.content, transcript))
        docid: int | None = None
        if engine != ENGINE_V1_LEGACY:
            if (
                existing is not None
                and existing["docid"] is not None
                and int(existing["created_at"]) == message.created_at
            ):
                docid = int(existing["docid"])
            else:
                docid = self._allocate_docid(message.created_at, message.message_id)
        self._connection.execute(
            """
            INSERT INTO search_messages(
                message_id, source_account_id, conversation_id,
                created_at, content, normalized_content, sender_uid,
                sender_display_name, message_type, has_attachment,
                attachment_file_name, transcript, is_self, metadata_complete, docid
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?)
            ON CONFLICT(message_id) DO UPDATE SET
                source_account_id = excluded.source_account_id,
                conversation_id = excluded.conversation_id,
                created_at = excluded.created_at,
                content = excluded.content,
                normalized_content = excluded.normalized_content,
                sender_uid = excluded.sender_uid,
                sender_display_name = excluded.sender_display_name,
                message_type = excluded.message_type,
                has_attachment = excluded.has_attachment,
                attachment_file_name = excluded.attachment_file_name,
                transcript = excluded.transcript,
                is_self = excluded.is_self,
                metadata_complete = 1,
                docid = excluded.docid
            """,
            (
                message.message_id,
                message.source_account_id,
                message.conversation_id,
                message.created_at,
                message.content,
                normalized,
                message.sender_uid,
                message.sender_display_name,
                message.message_type,
                int(message.has_attachment),
                message.attachment_file_name,
                transcript,
                int(message.is_self),
                docid,
            ),
        )
        if engine in _WRITES_V1:
            self._connection.executemany(
                "INSERT INTO search_grams(gram, message_id) VALUES (?, ?)",
                ((gram, message.message_id) for gram in _index_grams(normalized)),
            )
            if self.backend is SearchBackend.FTS5_TRIGRAM:
                self._connection.execute(
                    "INSERT INTO search_messages_fts(message_id, normalized_content) "
                    "VALUES (?, ?)",
                    (message.message_id, normalized),
                )
        if docid is not None:
            account_ref = self._account_ref(message.source_account_id)
            conversation_ref = self._conversation_ref(account_ref, message.conversation_id)
            self._insert_v2_entries(
                docid=docid,
                account_ref=account_ref,
                conversation_ref=conversation_ref,
                normalized=normalized,
            )

    def _backfill_transcripts(self) -> None:
        """加列那一刻填一次，理由同 `_backfill_attachment_file_names`。

        只填 `transcript` 这一列，**不重算 `normalized_content`**：重算要连
        gram 与 FTS 一起改写，那是几万行的写放大，不能挂在打开索引这条路上。
        已有转写的那些行由 `refresh_transcript` 分批推进。
        """
        tables = {
            str(row[0])
            for row in self._connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            ).fetchall()
        }
        if not {"voice_transcripts", "attachments"} <= tables:
            return
        self._connection.execute(
            """
            UPDATE search_messages
            SET transcript = COALESCE((
                SELECT t.text FROM attachments AS a
                JOIN voice_transcripts AS t ON t.sha256 = a.sha256
                WHERE a.message_id = search_messages.message_id
                  AND t.status = 'ok'
                ORDER BY a.id
                LIMIT 1
            ), '')
            """
        )

    def refresh_transcript(self, message_id: str, transcript: str) -> bool:
        """一条语音转写好之后，把它接进检索。

        走和 `update_content` 同一条路——删旧 gram、改归一化列、写新 gram——
        因为转写对索引的影响和「重新解析改了正文」一模一样。区别只在于
        `content` 不动：原始记录不被机器写的东西污染，这条在索引里也成立。
        """
        row = self._connection.execute(
            "SELECT content FROM search_messages WHERE message_id = ?",
            (message_id,),
        ).fetchone()
        if row is None:
            return False
        content = str(row[0])
        normalized = normalize_search_text(_searchable_text(content, transcript))
        result = self._retry_after_flip(
            lambda: self._update_transcript_once(
                message_id, content, transcript, normalized
            )
        )
        return bool(result)

    def _update_transcript_once(
        self, message_id: str, content: str, transcript: str, normalized: str
    ) -> bool:
        with self._connection:
            self._connection.execute(
                "UPDATE search_messages SET transcript = ? WHERE message_id = ?",
                (transcript, message_id),
            )
        return self._update_content_once(message_id, content, normalized)

    def update_content(self, message_id: str, content: str) -> bool:
        """Reindex one message's text, leaving the rest of its row alone.

        Reprocessing changes what a message says, not who sent it or when. A
        full upsert would need every one of those fields read back just to
        rewrite them unchanged, and the caller repairing a parse does not have
        them. Returns whether the message was in the index at all.

        The stored transcript goes back into the normalized text: reparsing a
        voice message's body must not make what was said in it unfindable.
        """
        kept = self._stored_transcripts((message_id,)).get(message_id, "")
        normalized = normalize_search_text(_searchable_text(content, kept))
        result = self._retry_after_flip(
            lambda: self._update_content_once(message_id, content, normalized)
        )
        return bool(result)

    def _update_content_once(self, message_id: str, content: str, normalized: str) -> bool:
        with self._connection:
            engine = self.engine()
            existing: sqlite3.Row | None = None
            if engine != ENGINE_V1_LEGACY:
                # Legacy archives predate the docid column this reads.
                existing = self._fetch_indexed_rows((message_id,)).get(message_id)
                if existing is None:
                    return False
                self._delete_v2_entries((existing,))
            updated = self._connection.execute(
                """
                UPDATE search_messages
                SET content = ?, normalized_content = ?
                WHERE message_id = ?
                """,
                (content, normalized, message_id),
            )
            if updated.rowcount == 0:
                return False
            if engine in _WRITES_V1:
                self._delete_existing_in_transaction((message_id,))
                self._connection.executemany(
                    "INSERT INTO search_grams(gram, message_id) VALUES (?, ?)",
                    ((gram, message_id) for gram in _index_grams(normalized)),
                )
                if self.backend is SearchBackend.FTS5_TRIGRAM:
                    self._connection.execute(
                        "INSERT INTO search_messages_fts(message_id, normalized_content) "
                        "VALUES (?, ?)",
                        (message_id, normalized),
                    )
            if engine != ENGINE_V1_LEGACY and existing is not None:
                docid = existing["docid"]
                if docid is None:
                    docid = self._allocate_docid(
                        int(existing["created_at"]), message_id
                    )
                    self._connection.execute(
                        "UPDATE search_messages SET docid = ? WHERE message_id = ?",
                        (int(docid), message_id),
                    )
                account_ref = self._account_ref(str(existing["source_account_id"]))
                conversation_ref = self._conversation_ref(
                    account_ref, str(existing["conversation_id"])
                )
                self._insert_v2_entries(
                    docid=int(docid),
                    account_ref=account_ref,
                    conversation_ref=conversation_ref,
                    normalized=normalized,
                )
        return True

    def orphan_message_ids(self, *, limit: int | None = None) -> tuple[str, ...]:
        """索引里还留着、而 `messages` 里已经没有的那些行。

        真机 2026-09-02 有 3 行，来源是迁移 `20260831_0021`——它
        `DELETE FROM messages WHERE provider_message_key LIKE '%:coord:%'`，
        没有连带删索引。**搜索结果不受影响**（`_hydrate_docids` 要 join `messages`，
        孤儿自然落空），坏的是对账：任何「索引条数 vs 应索引条数」的等值判定都会
        长期对不上，`_compute_index_status` 就曾因此把整个账号永久钉在 `building`。

        全表反连接实测 0.96 秒 / 724,288 行，够便宜到按需跑，不值得为它加索引。
        """
        if not self._has_table(self._connection, "search_messages"):
            return ()
        sql = (
            "SELECT s.message_id FROM search_messages AS s "
            "WHERE NOT EXISTS (SELECT 1 FROM messages AS m WHERE m.id = s.message_id)"
        )
        if limit is not None:
            sql += f" LIMIT {int(limit)}"
        return tuple(str(row[0]) for row in self._connection.execute(sql))

    def prune_orphans(self) -> int:
        """把上面那些行删掉，返回删了几行。

        逐行走 `delete()` 而不是一条 `DELETE ... WHERE NOT EXISTS`：索引不是一张表，
        v2 的 gram 行与 FTS 影子表都要跟着删，而那份连带关系只写在 `delete()` 里。
        孤儿是个位数，多几次往返换「只有一处知道怎么删一行索引」。
        """
        orphans = self.orphan_message_ids()
        for message_id in orphans:
            self.delete(message_id)
        return len(orphans)

    def delete(self, message_id: str) -> None:
        self._retry_after_flip(lambda: self._delete_once(message_id))

    def _delete_once(self, message_id: str) -> None:
        with self._connection:
            engine = self.engine()
            if engine != ENGINE_V1_LEGACY:
                existing = self._fetch_indexed_rows((message_id,)).get(message_id)
                if existing is not None:
                    self._delete_v2_entries((existing,))
            if engine in _WRITES_V1:
                self._connection.execute(
                    "DELETE FROM search_grams WHERE message_id = ?", (message_id,)
                )
                if self.backend is SearchBackend.FTS5_TRIGRAM:
                    self._connection.execute(
                        "DELETE FROM search_messages_fts WHERE message_id = ?",
                        (message_id,),
                    )
            self._connection.execute(
                "DELETE FROM search_messages WHERE message_id = ?",
                (message_id,),
            )

    # --- shared read helpers ----------------------------------------------

    @staticmethod
    def _to_hits(rows: list[sqlite3.Row]) -> list[SearchHit]:
        return [
            SearchHit(
                message_id=str(row["message_id"]),
                conversation_id=str(row["conversation_id"]),
                created_at=int(row["created_at"]),
                content=str(row["content"]),
                sender_uid=str(row["sender_uid"]),
                sender_display_name=(
                    str(row["sender_display_name"])
                    if row["sender_display_name"] is not None
                    else None
                ),
                message_type=str(row["message_type"]),
                has_attachment=bool(row["has_attachment"]),
                attachment_file_name=(
                    str(row["attachment_file_name"])
                    if row["attachment_file_name"] is not None
                    else None
                ),
                is_self=bool(row["is_self"]),
            )
            for row in rows
        ]

    def search(self, account_id: str, query: str, *, limit: int = 50) -> list[SearchHit]:
        return self.search_page(account_id, query, limit=limit).hits

    def search_page(
        self,
        account_id: str,
        query: str | None,
        *,
        conversation_ids: Sequence[str] | None = None,
        sender_uids: Sequence[str] | None = None,
        message_types: Sequence[str] | None = None,
        has_attachment: bool | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int = 50,
        sort: str = SORT_NEWEST,
        file_classes: Sequence[str] | None = None,
    ) -> SearchPage:
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        if sort not in SEARCH_SORTS:
            raise ValueError(f"sort must be one of {SEARCH_SORTS}")
        # str 自己就是 Sequence[str]，pyright strict 也拦不住：裸字符串会被
        # `column_filter` 逐字拆成 IN ('c','o','n',…)，浏览扇出还会一字一条流。
        # 所以四个序列参数都在入口当场拒绝，而不是等它静默查空。
        if isinstance(message_types, str):
            raise TypeError("message_types must be a sequence, not a bare string")
        if isinstance(conversation_ids, str):
            raise TypeError("conversation_ids must be a sequence, not a bare string")
        if isinstance(sender_uids, str):
            raise TypeError("sender_uids must be a sequence, not a bare string")
        if isinstance(file_classes, str):
            raise TypeError("file_classes must be a sequence, not a bare string")
        # 参数校验之后、真正查之前。连空查询的浏览分支一起罩住:`_browse_page` 读的
        # 同样是 `search_messages`,没建索引时它也只会答空,而空在这里的意思是「没有
        # 这条记录」。两条路必须说同一句实话。放在校验之后,是因为一个写错类型的调用
        # 必须照样报错——「没建索引」不该把调用方的 bug 藏起来。
        init_state = self.initialization_state()
        if init_state == INIT_NONE:
            return SearchPage(
                hits=[],
                next_after=None,
                index_status=SearchIndexStatus.NOT_INITIALIZED,
            )
        normalized = normalize_search_text(query).strip() if query is not None else ""
        if not normalized:
            slices = browse_fan_out(conversation_ids, sender_uids, message_types)
            return self._while_initializing(
                init_state,
                merge_search_pages(
                    [
                        self._browse_page(
                            account_id,
                            conversation_ids=sliced.conversation_ids,
                            sender_uids=sliced.sender_uids,
                            message_types=sliced.message_types,
                            has_attachment=has_attachment,
                            date_from=date_from,
                            date_to=date_to,
                            after=after,
                            limit=limit,
                            sort=sort,
                            file_classes=file_classes,
                        )
                        for sliced in slices
                    ],
                    limit=limit,
                    sort=sort,
                ),
            )
        kwargs: _SearchFilters = {
            "conversation_ids": conversation_ids,
            "sender_uids": sender_uids,
            "message_types": message_types,
            "has_attachment": has_attachment,
            "date_from": date_from,
            "date_to": date_to,
            "after": after,
            "limit": limit,
            "sort": sort,
            "file_classes": file_classes,
        }
        engine = self.engine()
        if engine in (ENGINE_V2, ENGINE_REBUILDING):
            return self._while_initializing(
                init_state, self._dispatch_v2(engine, account_id, normalized, kwargs)
            )
        try:
            return self._while_initializing(
                init_state, self._search_page_v1(account_id, normalized, **kwargs)
            )
        except sqlite3.OperationalError as error:
            # A backfill can flip the archive to v2 and drop the v1 tables
            # between our engine read and the query. The flip is one-way, so
            # one re-dispatch settles it; anything else is a real error.
            engine = self.engine()
            if "no such table" in str(error) and engine in (ENGINE_V2, ENGINE_REBUILDING):
                return self._while_initializing(
                    init_state, self._dispatch_v2(engine, account_id, normalized, kwargs)
                )
            raise

    @staticmethod
    def _while_initializing(init_state: str, page: SearchPage) -> SearchPage:
        """初始化回填还没跑完时,一页完整的答案是不存在的。

        `ready` 在这一刻是假话:索引里只有水位以下那一段,搜不到的部分看起来就像
        没发生过。契约里的 `building` 正是为这种「答得出但不全」准备的,前端也已经
        会画,所以这里只需把它盖上去。
        """
        if init_state == INIT_BUILDING and page.index_status is SearchIndexStatus.READY:
            return dataclasses.replace(page, index_status=SearchIndexStatus.BUILDING)
        return page

    def _dispatch_v2(
        self,
        engine: str,
        account_id: str,
        normalized: str,
        kwargs: _SearchFilters,
    ) -> SearchPage:
        page = self._search_page_v2(account_id, normalized, **kwargs)
        if engine == ENGINE_REBUILDING:
            # v1 is gone and v2 is still being re-derived: results can be
            # incomplete, and the page must say so instead of claiming ready.
            return dataclasses.replace(page, index_status=SearchIndexStatus.BUILDING)
        return page

    def _browse_page(
        self,
        account_id: str,
        *,
        conversation_ids: Sequence[str] | None,
        sender_uids: Sequence[str] | None,
        message_types: Sequence[str] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
        after: tuple[int, str] | None,
        limit: int,
        sort: str = SORT_NEWEST,
        file_classes: Sequence[str] | None = None,
    ) -> SearchPage:
        """Pure-filter listing; identical for both engines."""
        newest_first = sort == SORT_NEWEST
        conditions = ["m.source_account_id = ?", "m.metadata_complete = 1"]
        parameters: list[str | int] = [account_id]
        if conversation_ids:
            clause, clause_parameters = column_filter("m.conversation_id", conversation_ids)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if sender_uids:
            clause, clause_parameters = column_filter("m.sender_uid", sender_uids)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        # 只筛「文件」这一种时把类型写成字面量，别的一律照旧走占位符。
        # 取页那条索引分区在 `message_type = 'file'` 上，而**分区索引的可用性
        # 判定不看绑定参数**：`m.message_type = ?` 配上 `INDEXED BY` 会当场
        # `no query solution`。字面量只在这一种情形下出现，也只有这一种情形
        # 用得上那条索引。
        files_only = message_types is not None and tuple(message_types) == ("file",)
        if files_only:
            conditions.append("m.message_type = 'file'")
        elif message_types is not None and len(message_types) > 0:
            clause, clause_parameters = column_filter("m.message_type", message_types)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if has_attachment is not None:
            conditions.append("m.has_attachment = ?")
            parameters.append(int(has_attachment))
        if file_classes:
            clause, clause_parameters = file_classes_conditions(file_classes)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if date_from is not None:
            conditions.append("m.created_at >= ?")
            parameters.append(date_from)
        if date_to is not None:
            conditions.append("m.created_at <= ?")
            parameters.append(date_to)
        where_base = " AND ".join(conditions)
        # 带文件类别时把计数钉在那条含文件名的索引上。类别是拿
        # `attachment_file_name` 做 GLOB 判的，规划器估不准这种谓词的选择度：
        # 真实归档上它挑了账号级的会话序索引去扫，895 ms；同一台机器上换成
        # 副本又挑了 `type_filters` 逐行回表，578 ms。钉住之后是覆盖扫描 11 ms。
        # 与本文件另外两处 `INDEXED BY` 同一个理由，不是过早优化。
        #
        # 钉子只在没有发送人过滤时上：这条索引末列带着 `conversation_id`，会话
        # 过滤仍是覆盖扫描；但它没有 `sender_uid`，带发送人时钉住就要逐行回表
        # 去否掉别人的行——那时规划器自己挑的 seek 更快（实测见取页那条的注）。
        count_index = (
            f" INDEXED BY {_FILE_CLASS_INDEX}"
            if file_classes and self._file_class_index and not sender_uids
            else ""
        )
        count_rows = self._connection.execute(
            f"""
            SELECT m.conversation_id, COUNT(*) AS hit_count
            FROM search_messages AS m{count_index}
            WHERE {where_base}
            GROUP BY m.conversation_id
            """,
            parameters,
        ).fetchall()
        if after is not None:
            after_created_at, after_message_id = after
            # 方向翻转时这一条也必须跟着翻：同一个游标在另一个方向下照样解得出
            # 一个合法位置，然后静默给出互补的那一半。
            conditions.append(
                "(m.created_at < ? OR (m.created_at = ? AND m.message_id > ?))"
                if newest_first
                else "(m.created_at > ? OR (m.created_at = ? AND m.message_id > ?))"
            )
            parameters.extend((after_created_at, after_created_at, after_message_id))
        order = "DESC" if newest_first else "ASC"
        # 取页同样要钉。规划器**不会**自己挑这条分区索引：它与 `type_order`
        # 给出同样的顺序，而 GLOB 的选择度它估不出来，于是两种情况下都挑
        # `type_order`——那条里没有文件名，只能逐个候选回表去判。
        #
        # 钉子只在没有身份过滤时上——有会话/发送人时规划器的 seek 更快：这条
        # 分区索引里既没有 `conversation_id` 也没有 `sender_uid`，钉住它等于扫
        # 全账号的文件分区、每个过 GLOB 的候选再回一次主表，只为否掉不属于这个
        # 会话的行（真实归档 2,666 条 doc 全数付一遍）。不钉时规划器挑
        # `ix_search_messages_conversation_order` 的会话 seek——实测温查询快
        # 4 倍、冷查询差出秒级，浏览扇出还要按流数再乘一遍。
        page_index = (
            f" INDEXED BY {_FILE_ORDER_INDEX}"
            if file_classes
            and files_only
            and self._file_order_index
            and not conversation_ids
            and not sender_uids
            else ""
        )
        rows = self._connection.execute(
            f"""
            SELECT m.message_id, m.conversation_id, m.created_at, m.content,
                   m.sender_uid, m.sender_display_name, m.message_type,
                   m.has_attachment, m.attachment_file_name, m.is_self
            FROM search_messages AS m{page_index}
            WHERE {" AND ".join(conditions)}
            ORDER BY m.created_at {order}, m.message_id ASC
            LIMIT ?
            """,
            (*parameters, limit + 1),
        ).fetchall()
        return self._assemble_page(rows, limit, count_rows)

    @staticmethod
    def _assemble_page(
        rows: list[sqlite3.Row], limit: int, count_rows: list[sqlite3.Row]
    ) -> SearchPage:
        hits = MessageSearchIndex._to_hits(list(rows[:limit]))
        next_after = None
        if len(rows) > limit and hits:
            last = hits[-1]
            next_after = (last.created_at, last.message_id)
        conversation_counts = {
            str(row["conversation_id"]): int(row["hit_count"]) for row in count_rows
        }
        return SearchPage(
            hits=hits,
            next_after=next_after,
            total_count=sum(conversation_counts.values()),
            conversation_counts=conversation_counts,
        )

    # --- v2 read path -------------------------------------------------------

    def _after_docid(self, after: tuple[int, str]) -> int:
        after_created_at, after_message_id = after
        row = self._connection.execute(
            "SELECT docid FROM search_messages WHERE message_id = ?",
            (after_message_id,),
        ).fetchone()
        if row is not None and row[0] is not None:
            return int(row[0])
        # The cursor row was deleted between pages. The deterministic first
        # candidate is exact unless that message's hash slot had collided; the
        # worst miss is a handful of same-second messages.
        return docid_first_candidate(after_created_at, after_message_id)

    def _hydrate_docids(
        self, docids: Sequence[int], *, sort: str = SORT_NEWEST
    ) -> list[sqlite3.Row]:
        if not docids:
            return []
        placeholders = ", ".join("?" for _ in docids)
        return self._connection.execute(
            f"""
            SELECT m.message_id, m.conversation_id, m.created_at, m.content,
                   m.sender_uid, m.sender_display_name, m.message_type,
                   m.has_attachment, m.attachment_file_name, m.is_self
            FROM search_messages AS m
            WHERE m.docid IN ({placeholders})
            ORDER BY m.docid {"DESC" if sort == SORT_NEWEST else "ASC"}
            """,
            tuple(docids),
        ).fetchall()

    def _search_page_v2(
        self,
        account_id: str,
        normalized: str,
        *,
        conversation_ids: Sequence[str] | None,
        sender_uids: Sequence[str] | None,
        message_types: Sequence[str] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
        after: tuple[int, str] | None,
        limit: int,
        sort: str = SORT_NEWEST,
        file_classes: Sequence[str] | None = None,
    ) -> SearchPage:
        account_ref = self._lookup_account_ref(account_id)
        if account_ref is None:
            return SearchPage(hits=[], next_after=None)
        conversation_refs: list[int] | None = None
        if conversation_ids:
            # 认不出的会话标识**不是**「不筛」：一个都认不出就是空结果，与单值
            # 时同一条规矩。认得出的那几个照筛——归档里没有的那个本来也没有记录。
            resolved = [
                ref
                for ref in (
                    self._lookup_conversation_ref(account_ref, conversation_id)
                    for conversation_id in conversation_ids
                )
                if ref is not None
            ]
            if not resolved:
                return SearchPage(hits=[], next_after=None)
            conversation_refs = resolved

        docid_min = docid_floor(date_from) if date_from is not None else None
        docid_max = docid_floor(date_to + 1) - 1 if date_to is not None else None
        after_docid = self._after_docid(after) if after is not None else None

        # Filters that live on the message row force a per-candidate probe.
        # They stay out of the count/page SQL entirely when absent so the
        # common case never leaves the index.
        row_conditions: list[str] = []
        row_parameters: list[str | int] = []
        if sender_uids:
            clause, clause_parameters = column_filter("m.sender_uid", sender_uids)
            row_conditions.append(clause)
            row_parameters.extend(clause_parameters)
        if message_types is not None and len(message_types) > 0:
            clause, clause_parameters = column_filter("m.message_type", message_types)
            row_conditions.append(clause)
            row_parameters.extend(clause_parameters)
        if has_attachment is not None:
            row_conditions.append("m.has_attachment = ?")
            row_parameters.append(int(has_attachment))
        if file_classes:
            clause, clause_parameters = file_classes_conditions(file_classes)
            row_conditions.append(clause)
            row_parameters.extend(clause_parameters)

        if len(normalized) <= 2:
            page = self._v2_gram_page(
                normalized,
                account_ref=account_ref,
                conversation_refs=conversation_refs,
                docid_min=docid_min,
                docid_max=docid_max,
                after_docid=after_docid,
                row_conditions=row_conditions,
                row_parameters=row_parameters,
                limit=limit,
                sort=sort,
            )
        elif self.backend is SearchBackend.FTS5_TRIGRAM:
            page = self._v2_fts_page(
                normalized,
                account_ref=account_ref,
                conversation_refs=conversation_refs,
                docid_min=docid_min,
                docid_max=docid_max,
                after_docid=after_docid,
                row_conditions=row_conditions,
                row_parameters=row_parameters,
                limit=limit,
                sort=sort,
            )
        else:
            page = self._v2_fallback_page(
                normalized,
                account_ref=account_ref,
                conversation_refs=conversation_refs,
                docid_min=docid_min,
                docid_max=docid_max,
                after_docid=after_docid,
                row_conditions=row_conditions,
                row_parameters=row_parameters,
                limit=limit,
                sort=sort,
            )
        return page

    def _conversation_ids_for_refs(self, refs: Sequence[int]) -> dict[int, str]:
        if not refs:
            return {}
        placeholders = ", ".join("?" for _ in refs)
        return {
            int(row["conversation_ref"]): str(row["conversation_id"])
            for row in self._connection.execute(
                f"SELECT conversation_ref, conversation_id FROM search_conversations "
                f"WHERE conversation_ref IN ({placeholders})",
                tuple(refs),
            ).fetchall()
        }

    def _v2_assemble(
        self,
        docids: list[int],
        had_extra: bool,
        limit: int,
        count_rows: list[sqlite3.Row],
        *,
        sort: str = SORT_NEWEST,
    ) -> SearchPage:
        rows = self._hydrate_docids(docids[:limit], sort=sort)
        hits = self._to_hits(rows)
        next_after = None
        if had_extra and hits:
            last = hits[-1]
            next_after = (last.created_at, last.message_id)
        ref_counts = {int(row[0]): int(row[1]) for row in count_rows}
        names = self._conversation_ids_for_refs(tuple(ref_counts))
        conversation_counts = {
            names[ref]: count for ref, count in ref_counts.items() if ref in names
        }
        return SearchPage(
            hits=hits,
            next_after=next_after,
            total_count=sum(conversation_counts.values()),
            conversation_counts=conversation_counts,
        )

    def _v2_gram_page(
        self,
        normalized: str,
        *,
        account_ref: int,
        conversation_refs: Sequence[int] | None,
        docid_min: int | None,
        docid_max: int | None,
        after_docid: int | None,
        row_conditions: list[str],
        row_parameters: list[str | int],
        limit: int,
        sort: str = SORT_NEWEST,
    ) -> SearchPage:
        """One index range holds the whole answer for a 1-2 character query.

        `search_grams_v2` stores every 1..2 wide substring, so a gram hit *is*
        a body match -- no verification read. The primary key leads with
        (gram, account_ref, neg_docid): the range arrives newest-first, a page
        is its first `limit` entries, and the per-conversation count is one
        sequential aggregation of the same range.
        """
        conditions = ["g.gram = ?", "g.account_ref = ?"]
        parameters: list[str | int] = [normalized, account_ref]
        if conversation_refs:
            clause, clause_parameters = column_filter("g.conversation_ref", conversation_refs)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if docid_max is not None:
            conditions.append("g.neg_docid >= ?")
            parameters.append(-docid_max)
        if docid_min is not None:
            conditions.append("g.neg_docid <= ?")
            parameters.append(-docid_min)
        joined = ""
        if row_conditions:
            joined = " JOIN search_messages AS m ON m.docid = -g.neg_docid"
            conditions.extend(row_conditions)
            parameters.extend(row_parameters)
        where = " AND ".join(conditions)
        count_rows = self._connection.execute(
            f"""
            SELECT g.conversation_ref, COUNT(*) AS hit_count
            FROM search_grams_v2 AS g{joined}
            WHERE {where}
            GROUP BY g.conversation_ref
            """,
            parameters,
        ).fetchall()
        page_conditions = list(conditions)
        page_parameters = list(parameters)
        newest_first = sort == SORT_NEWEST
        if after_docid is not None:
            # `neg_docid` 升序就是 docid 降序，所以比较符与排序方向必须一起翻。
            # 只翻一个，旧游标照样验签通过、解出一个合法位置，然后静默给出
            # 互补的那一半——不报错，看起来只是「翻页翻丢了」。
            page_conditions.append("g.neg_docid > ?" if newest_first else "g.neg_docid < ?")
            page_parameters.append(-after_docid)
        rows = self._connection.execute(
            f"""
            SELECT -g.neg_docid AS docid
            FROM search_grams_v2 AS g{joined}
            WHERE {" AND ".join(page_conditions)}
            ORDER BY g.neg_docid {"ASC" if newest_first else "DESC"}
            LIMIT ?
            """,
            (*page_parameters, limit + 1),
        ).fetchall()
        docids = [int(row["docid"]) for row in rows]
        return self._v2_assemble(docids, len(docids) > limit, limit, count_rows, sort=sort)

    def _v2_fts_page(
        self,
        normalized: str,
        *,
        account_ref: int,
        conversation_refs: Sequence[int] | None,
        docid_min: int | None,
        docid_max: int | None,
        after_docid: int | None,
        row_conditions: list[str],
        row_parameters: list[str | int],
        limit: int,
        sort: str = SORT_NEWEST,
    ) -> SearchPage:
        """FTS5 doclists are stored in rowid order and rowid *is* the sort key.

        `ORDER BY rowid DESC LIMIT n` therefore streams matches newest-first
        and stops at a page -- no materialized hit set, no sort. Every column
        the count needs rides in the index row itself.
        """
        conditions = ["search_fts_v2 MATCH ?", "f.account_ref = ?"]
        parameters: list[str | int] = [escape_match_phrase(normalized), account_ref]
        if conversation_refs:
            clause, clause_parameters = column_filter("f.conversation_ref", conversation_refs)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if docid_min is not None:
            conditions.append("f.rowid >= ?")
            parameters.append(docid_min)
        if docid_max is not None:
            conditions.append("f.rowid <= ?")
            parameters.append(docid_max)
        joined = ""
        if row_conditions:
            joined = " JOIN search_messages AS m ON m.docid = f.rowid"
            conditions.extend(row_conditions)
            parameters.extend(row_parameters)
        where = " AND ".join(conditions)
        count_rows = self._connection.execute(
            f"""
            SELECT f.conversation_ref, COUNT(*) AS hit_count
            FROM search_fts_v2 AS f{joined}
            WHERE {where}
            GROUP BY f.conversation_ref
            """,
            parameters,
        ).fetchall()
        page_conditions = list(conditions)
        page_parameters = list(parameters)
        newest_first = sort == SORT_NEWEST
        if after_docid is not None:
            page_conditions.append("f.rowid < ?" if newest_first else "f.rowid > ?")
            page_parameters.append(after_docid)
        rows = self._connection.execute(
            f"""
            SELECT f.rowid AS docid
            FROM search_fts_v2 AS f{joined}
            WHERE {" AND ".join(page_conditions)}
            ORDER BY f.rowid {"DESC" if newest_first else "ASC"}
            LIMIT ?
            """,
            (*page_parameters, limit + 1),
        ).fetchall()
        docids = [int(row["docid"]) for row in rows]
        return self._v2_assemble(docids, len(docids) > limit, limit, count_rows, sort=sort)

    def _v2_fallback_page(
        self,
        normalized: str,
        *,
        account_ref: int,
        conversation_refs: Sequence[int] | None,
        docid_min: int | None,
        docid_max: int | None,
        after_docid: int | None,
        row_conditions: list[str],
        row_parameters: list[str | int],
        limit: int,
        sort: str = SORT_NEWEST,
    ) -> SearchPage:
        """No FTS5: drive from the first 2-gram, point-check the last, verify.

        Both grams present still does not mean the query occurs contiguously,
        so the body check stays. The scan remains newest-first and bounded by
        the driving gram's range instead of the account's row count.
        """
        head = normalized[:2]
        tail = normalized[-2:]
        conditions = ["g.gram = ?", "g.account_ref = ?"]
        parameters: list[str | int] = [head, account_ref]
        if conversation_refs:
            clause, clause_parameters = column_filter("g.conversation_ref", conversation_refs)
            conditions.append(clause)
            parameters.extend(clause_parameters)
        if docid_max is not None:
            conditions.append("g.neg_docid >= ?")
            parameters.append(-docid_max)
        if docid_min is not None:
            conditions.append("g.neg_docid <= ?")
            parameters.append(-docid_min)
        conditions.append(
            "EXISTS (SELECT 1 FROM search_grams_v2 AS t "
            "WHERE t.gram = ? AND t.account_ref = g.account_ref "
            "AND t.neg_docid = g.neg_docid)"
        )
        parameters.append(tail)
        conditions.append("instr(m.normalized_content, ?) > 0")
        parameters.append(normalized)
        conditions.extend(row_conditions)
        parameters.extend(row_parameters)
        joined = " JOIN search_messages AS m ON m.docid = -g.neg_docid"
        where = " AND ".join(conditions)
        count_rows = self._connection.execute(
            f"""
            SELECT g.conversation_ref, COUNT(*) AS hit_count
            FROM search_grams_v2 AS g{joined}
            WHERE {where}
            GROUP BY g.conversation_ref
            """,
            parameters,
        ).fetchall()
        page_conditions = list(conditions)
        page_parameters = list(parameters)
        newest_first = sort == SORT_NEWEST
        if after_docid is not None:
            # `neg_docid` 升序就是 docid 降序，所以比较符与排序方向必须一起翻。
            # 只翻一个，旧游标照样验签通过、解出一个合法位置，然后静默给出
            # 互补的那一半——不报错，看起来只是「翻页翻丢了」。
            page_conditions.append("g.neg_docid > ?" if newest_first else "g.neg_docid < ?")
            page_parameters.append(-after_docid)
        rows = self._connection.execute(
            f"""
            SELECT -g.neg_docid AS docid
            FROM search_grams_v2 AS g{joined}
            WHERE {" AND ".join(page_conditions)}
            ORDER BY g.neg_docid {"ASC" if newest_first else "DESC"}
            LIMIT ?
            """,
            (*page_parameters, limit + 1),
        ).fetchall()
        docids = [int(row["docid"]) for row in rows]
        return self._v2_assemble(docids, len(docids) > limit, limit, count_rows, sort=sort)

    # --- v1 read path (serves archives whose v2 backfill has not finished) --

    def _prefers_time_ordered_scan(
        self,
        grams: tuple[str, ...],
        count_rows: list[sqlite3.Row],
        *,
        conversation_ids: Sequence[str] | None,
        sender_uids: Sequence[str] | None,
        message_types: Sequence[str] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
    ) -> bool:
        """Walk newest-first and ask the gram table, instead of sorting all hits?

        Only worth it when the term is so common that ordering the whole hit set
        dwarfs reading a page, and only safe when nothing else narrows the result:
        with a further filter the scan could walk the entire account before it
        finds fifty rows that qualify.
        """
        if len(grams) != 1 or not self._identity_index:
            return False
        if (
            conversation_ids
            or sender_uids
            or (message_types is not None and len(message_types) > 0)
            or has_attachment is not None
            or date_from is not None
            or date_to is not None
        ):
            return False
        return sum(int(row["hit_count"]) for row in count_rows) > _TIME_ORDERED_HIT_FLOOR

    def _search_page_v1(
        self,
        account_id: str,
        normalized: str,
        *,
        conversation_ids: Sequence[str] | None,
        sender_uids: Sequence[str] | None,
        message_types: Sequence[str] | None,
        has_attachment: bool | None,
        date_from: int | None,
        date_to: int | None,
        after: tuple[int, str] | None,
        limit: int,
        sort: str = SORT_NEWEST,
        file_classes: Sequence[str] | None = None,
    ) -> SearchPage:
        newest_first = sort == SORT_NEWEST
        base_conditions = ["m.source_account_id = ?", "m.metadata_complete = 1"]
        base_parameters: list[str | int] = [account_id]
        if conversation_ids:
            clause, clause_parameters = column_filter("m.conversation_id", conversation_ids)
            base_conditions.append(clause)
            base_parameters.extend(clause_parameters)
        if sender_uids:
            clause, clause_parameters = column_filter("m.sender_uid", sender_uids)
            base_conditions.append(clause)
            base_parameters.extend(clause_parameters)
        if message_types is not None and len(message_types) > 0:
            clause, clause_parameters = column_filter("m.message_type", message_types)
            base_conditions.append(clause)
            base_parameters.extend(clause_parameters)
        if has_attachment is not None:
            base_conditions.append("m.has_attachment = ?")
            base_parameters.append(int(has_attachment))
        if file_classes:
            clause, clause_parameters = file_classes_conditions(file_classes)
            base_conditions.append(clause)
            base_parameters.extend(clause_parameters)
        if date_from is not None:
            base_conditions.append("m.created_at >= ?")
            base_parameters.append(date_from)
        if date_to is not None:
            base_conditions.append("m.created_at <= ?")
            base_parameters.append(date_to)
        conditions = list(base_conditions)
        parameters = list(base_parameters)
        if after is not None:
            after_created_at, after_message_id = after
            conditions.append(
                "(m.created_at < ? OR (m.created_at = ? AND m.message_id > ?))"
                if newest_first
                else "(m.created_at > ? OR (m.created_at = ? AND m.message_id > ?))"
            )
            parameters.extend((after_created_at, after_created_at, after_message_id))
        where_base = " AND ".join(base_conditions)
        where_extra = " AND ".join(conditions)

        if self.backend is SearchBackend.FTS5_TRIGRAM and len(normalized) >= 3:
            count_rows = self._connection.execute(
                f"""
                SELECT m.conversation_id, COUNT(*) AS hit_count
                FROM search_messages_fts AS f
                JOIN search_messages AS m ON m.message_id = f.message_id
                WHERE search_messages_fts MATCH ? AND {where_base}
                GROUP BY m.conversation_id
                """,
                (escape_match_phrase(normalized), *base_parameters),
            ).fetchall()
            rows = self._connection.execute(
                f"""
                SELECT m.message_id, m.conversation_id, m.created_at, m.content,
                       m.sender_uid, m.sender_display_name, m.message_type,
                       m.has_attachment, m.attachment_file_name, m.is_self
                FROM search_messages_fts AS f
                JOIN search_messages AS m ON m.message_id = f.message_id
                WHERE search_messages_fts MATCH ? AND {where_extra}
                ORDER BY m.created_at {"DESC" if newest_first else "ASC"},
                         m.message_id ASC
                LIMIT ?
                """,
                (escape_match_phrase(normalized), *parameters, limit + 1),
            ).fetchall()
        else:
            grams = _grams(normalized)
            if not grams:
                return SearchPage(hits=[], next_after=None)
            if len(grams) == 1 and self._identity_index:
                # 至多三个字符的查询整个就是一个 gram，而 `_index_grams` 存了正文
                # 1..3 宽的全部子串——命中 gram 与正文包含该串完全等价。再做一次
                # `instr` 复核不会改变结果，却逼着每个候选把整条正文读出来；
                # 中文里两字词恰恰是最常见的查询，这条复核最贵也最多余。
                #
                # CROSS JOIN 与 INDEXED BY 都不改变结果，只是不许规划器自作主张：
                # 放手让它选，真实归档上它会从账号的 8 万条消息扫起、再逐条回查
                # gram，一次取页要 4 秒以上（旧写法约 0.2 秒）。gram 是选择性最强的
                # 条件，必须由它先缩小候选集，再按 message_id 定位。
                candidate_source = (
                    "search_grams AS g "
                    f"CROSS JOIN search_messages AS m INDEXED BY {_IDENTITY_INDEX} "
                    "ON m.message_id = g.message_id"
                )
                candidate_condition = "g.gram = ?"
                candidate_parameters: tuple[str | int, ...] = (grams[0],)
                verify_condition = ""
                verify_parameters: tuple[str, ...] = ()
            else:
                placeholders = ", ".join("?" for _ in grams)
                candidate_source = (
                    f"(SELECT message_id FROM search_grams WHERE gram IN ({placeholders}) "
                    "GROUP BY message_id HAVING COUNT(*) = ?) AS c "
                    "JOIN search_messages AS m ON m.message_id = c.message_id"
                )
                # 四字以上只按 3 宽窗口取 gram，全中不代表连续，仍须逐条复核。
                candidate_condition = ""
                candidate_parameters = (*grams, len(grams))
                verify_condition = " AND instr(m.normalized_content, ?) > 0"
                verify_parameters = (normalized,)
            gram_prefix = f"{candidate_condition} AND " if candidate_condition else ""
            count_rows = self._connection.execute(
                f"""
                SELECT m.conversation_id, COUNT(*) AS hit_count
                FROM {candidate_source}
                WHERE {gram_prefix}{where_base}{verify_condition}
                GROUP BY m.conversation_id
                """,
                (*candidate_parameters, *base_parameters, *verify_parameters),
            ).fetchall()
            if self._prefers_time_ordered_scan(
                grams,
                count_rows,
                conversation_ids=conversation_ids,
                sender_uids=sender_uids,
                message_types=message_types,
                has_attachment=has_attachment,
                date_from=date_from,
                date_to=date_to,
            ):
                # 计数已经量出候选集有多大，取页策略照着它选就行，不必猜。
                # 常见到占满四分之一正文的单字（本机「的」命中 21,612 条），
                # 从 gram 出发要把两万条排一遍序才取 50 条；反过来按时间序扫消息、
                # 逐条问 gram 表，扫几十条就凑够一页。实测同一份真实归档：
                # 「吃饭」gram 优先 0.3ms / 时间序 421ms；「的」gram 优先 164ms /
                # 时间序 0.3ms。两条路给出的结果完全一致，只是代价相反。
                rows = self._connection.execute(
                    f"""
                    SELECT m.message_id, m.conversation_id, m.created_at, m.content,
                           m.sender_uid, m.sender_display_name, m.message_type,
                           m.has_attachment, m.attachment_file_name, m.is_self
                    FROM search_messages AS m INDEXED BY {_ACCOUNT_ORDER_INDEX}
                    WHERE {where_extra}
                      AND EXISTS (
                        SELECT 1 FROM search_grams AS g
                        WHERE g.gram = ? AND g.message_id = m.message_id
                      )
                    ORDER BY m.created_at {"DESC" if newest_first else "ASC"},
                             m.message_id ASC
                    LIMIT ?
                    """,
                    (*parameters, grams[0], limit + 1),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    f"""
                    SELECT m.message_id, m.conversation_id, m.created_at, m.content,
                           m.sender_uid, m.sender_display_name, m.message_type,
                           m.has_attachment, m.attachment_file_name, m.is_self
                    FROM {candidate_source}
                    WHERE {gram_prefix}{where_extra}{verify_condition}
                    ORDER BY m.created_at {"DESC" if newest_first else "ASC"},
                             m.message_id ASC
                    LIMIT ?
                    """,
                    (*candidate_parameters, *parameters, *verify_parameters, limit + 1),
                ).fetchall()

        return self._assemble_page(rows, limit, count_rows)

    def explain(self, account_id: str, query: str) -> tuple[str, ...]:
        normalized = normalize_search_text(query).strip()
        if not normalized:
            return ()
        if self.engine() in (ENGINE_V2, ENGINE_REBUILDING):
            account_ref = self._lookup_account_ref(account_id) or 0
            if len(normalized) <= 2:
                rows = self._connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT conversation_ref, COUNT(*)
                    FROM search_grams_v2
                    WHERE gram = ? AND account_ref = ?
                    GROUP BY conversation_ref
                    """,
                    (normalized, account_ref),
                ).fetchall()
            elif self.backend is SearchBackend.FTS5_TRIGRAM:
                rows = self._connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT rowid FROM search_fts_v2
                    WHERE search_fts_v2 MATCH ? AND account_ref = ?
                    ORDER BY rowid DESC LIMIT 50
                    """,
                    (escape_match_phrase(normalized), account_ref),
                ).fetchall()
            else:
                rows = self._connection.execute(
                    """
                    EXPLAIN QUERY PLAN
                    SELECT neg_docid FROM search_grams_v2
                    JOIN search_messages ON search_messages.docid = -search_grams_v2.neg_docid
                    WHERE search_grams_v2.gram = ? AND search_grams_v2.account_ref = ?
                      AND instr(search_messages.normalized_content, ?) > 0
                    ORDER BY search_grams_v2.neg_docid ASC LIMIT 50
                    """,
                    (normalized[:2], account_ref, normalized),
                ).fetchall()
            return tuple(str(row[3]) for row in rows)
        if self.backend is SearchBackend.FTS5_TRIGRAM and len(normalized) >= 3:
            rows = self._connection.execute(
                """
                EXPLAIN QUERY PLAN
                SELECT m.message_id
                FROM search_messages_fts AS f
                JOIN search_messages AS m ON m.message_id = f.message_id
                WHERE search_messages_fts MATCH ? AND m.source_account_id = ?
                """,
                (escape_match_phrase(normalized), account_id),
            ).fetchall()
        else:
            grams = _grams(normalized)
            placeholders = ", ".join("?" for _ in grams)
            rows = self._connection.execute(
                f"""
                EXPLAIN QUERY PLAN
                WITH candidates AS (
                    SELECT message_id
                    FROM search_grams
                    WHERE gram IN ({placeholders})
                    GROUP BY message_id
                    HAVING COUNT(*) = ?
                )
                SELECT m.message_id
                FROM candidates AS c
                JOIN search_messages AS m ON m.message_id = c.message_id
                WHERE m.source_account_id = ?
                  AND instr(m.normalized_content, ?) > 0
                """,
                (*grams, len(grams), account_id, normalized),
            ).fetchall()
        return tuple(str(row[3]) for row in rows)

    # --- excluded-type sweep -----------------------------------------------

    def purge_chunk(self, *, chunk_size: int = 2_000) -> tuple[int, bool]:
        """Drop one slice of rows this build does not index; (removed, finished).

        Same shape as `build_chunk` and for the same reasons: one short
        IMMEDIATE transaction per chunk, a durable watermark, resumable from
        anywhere. The watermark is a rowid the scan has already walked past
        rather than the last row removed, so the whole sweep reads
        `search_messages` once instead of once per chunk -- there is no index
        on `message_type` alone, and adding one to be dropped again after a
        single sweep would cost more than the sweep.

        What decides membership is the archive's `message_type`, not the copy
        the index holds. That copy is written once and can go stale: a replay
        that retypes a row updates `messages`, and until this sweep learned to
        look there, it filtered on the stale copy -- on a real archive it found
        54 of 12,403 notices, deleted them, and declared itself finished. An
        index kept in its own database has no `messages` to consult and falls
        back to its own column, which is all it has ever had.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self.engine() == ENGINE_V1_LEGACY:
                raise RuntimeError(
                    "archive has no v2 schema; open it with ensure_schema=True first"
                )
            current = _excluded_types_state()
            stored = self._connection.execute(
                "SELECT value FROM search_index_state WHERE key = ?",
                (_EXCLUDED_TYPES_KEY,),
            ).fetchone()
            if stored is not None and str(stored[0]) == current:
                self._connection.commit()
                return (0, True)
            excluded = sorted(NON_SEARCHABLE_MESSAGE_TYPES)
            rows: list[sqlite3.Row] = []
            if excluded:
                watermark_row = self._connection.execute(
                    "SELECT value FROM search_index_state WHERE key = ?",
                    (_EXCLUDED_TYPES_WATERMARK_KEY,),
                ).fetchone()
                watermark = int(watermark_row[0]) if watermark_row is not None else 0
                placeholders = ", ".join("?" for _ in excluded)
                if self._has_table(self._connection, "messages"):
                    source = (
                        "search_messages AS s LEFT JOIN messages AS m ON m.id = s.message_id"
                    )
                    membership = f"COALESCE(m.message_type, s.message_type) IN ({placeholders})"
                else:
                    source = "search_messages AS s"
                    membership = f"s.message_type IN ({placeholders})"
                rows = self._connection.execute(
                    f"""
                    SELECT s.rowid AS rowid, s.message_id AS message_id,
                           s.source_account_id AS source_account_id,
                           s.conversation_id AS conversation_id,
                           s.created_at AS created_at,
                           s.normalized_content AS normalized_content,
                           s.docid AS docid
                    FROM {source}
                    WHERE s.rowid > ? AND {membership}
                    ORDER BY s.rowid
                    LIMIT ?
                    """,
                    (watermark, *excluded, chunk_size),
                ).fetchall()
            if rows:
                self._purge_rows_in_transaction(rows)
                self._connection.execute(
                    "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, ?)",
                    (_EXCLUDED_TYPES_WATERMARK_KEY, str(int(rows[-1]["rowid"]))),
                )
            # A short chunk means the scan reached the end of the table: there
            # is no further row of an excluded type above the watermark.
            finished = len(rows) < chunk_size
            if finished:
                self._connection.execute(
                    "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, ?)",
                    (_EXCLUDED_TYPES_KEY, current),
                )
            self._connection.commit()
            return (len(rows), finished)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def _purge_rows_in_transaction(self, rows: Sequence[sqlite3.Row]) -> None:
        message_ids = [str(row["message_id"]) for row in rows]
        engine = self.engine()
        if engine != ENGINE_V1_LEGACY:
            self._delete_v2_entries(rows)
        if engine in _WRITES_V1:
            self._delete_existing_in_transaction(message_ids)
        self._delete_message_rows(message_ids)

    # --- v2 backfill --------------------------------------------------------

    def set_initialization_state(self, state: str) -> None:
        """开、关、或标记正在建。写入方专用。"""
        if state not in (INIT_NONE, INIT_BUILDING, INIT_READY):
            raise ValueError(f"unknown initialization state: {state!r}")
        with self._connection:
            self._set_state(_INITIALIZED_KEY, state)
            if state != INIT_BUILDING:
                # 重新开一次要从头扫,而不是接着上次那条半截的水位;关掉时留着这个
                # 水位更没有意义——下次开索引表都是新建的。
                self._set_state(_INIT_WATERMARK_KEY, "0")

    def enable(self) -> str:
        """把「没开」推进到开着,返回落定的状态。

        NONE→BUILDING/READY 这条转移规则只写这一份——导入门控与 HTTP 开关都调
        它;写在两处的话,规则的下一个细节（第四种状态、开启时点数）只会落进其中
        一处,两条路开出来的归档就不一样了。

        有旧消息只能答 building（此刻报 ready 是撒谎,让回填去补）,空归档直接
        ready。幂等:已经开着（含正在建）原样返回,重开不归零水位。
        """
        state = self.initialization_state()
        if state != INIT_NONE:
            return state
        state = INIT_BUILDING if self.archive_has_messages() else INIT_READY
        self.set_initialization_state(state)
        return state

    def archive_has_messages(self) -> bool:
        """归档里已经有消息了吗——决定「现在打开索引」算不算完整。

        一次导入把索引从关变开时,这一次只索引它自己带来的那些消息。归档要是本来
        就有旧消息,那些还在索引外面,此刻报 ready 就是撒谎;答 building,让回填去补。
        """
        if not self._has_table(self._connection, "messages"):
            return False
        return (
            self._connection.execute("SELECT 1 FROM messages LIMIT 1").fetchone()
            is not None
        )

    def _archive_columns(self, table: str) -> set[str]:
        if not self._has_table(self._connection, table):
            return set()
        return {
            str(row[1]) for row in self._connection.execute(f"PRAGMA table_info({table})")
        }

    def _set_state(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, ?)",
            (key, value),
        )

    def _initialization_cancelled(self, started_from: str) -> bool:
        """建索引期间用户按了「关掉检索」——在途分片不得再写状态。

        `drop_search_index` 与回填线程之间没有锁：删表把状态写回 `none`、水位归零，
        而正在跑的分片如果无条件把 `building` 和自己的水位盖回去，界面会永远说
        「正在建」而没有任何人在建；更糟的是下一次开启会沿着这个过期水位续跑，
        把水位以下的消息全部跳过，最后以 `ready` 掩盖一个不完整的索引。所以每次
        写状态前都在同一个事务里重读一遍：起跑时不是 `none`、现在读到 `none`，
        就是中途被关掉了，直接收工。起跑本来就是 `none` 的（直接调用这条 API
        从零建）不算——那是老语义，不是取消。
        """
        if started_from == INIT_NONE:
            return False
        row = self._connection.execute(
            f"SELECT value FROM search_index_state WHERE key = '{_INITIALIZED_KEY}'"
        ).fetchone()
        return row is not None and str(row[0]) == INIT_NONE

    def initialize_chunk(self, *, chunk_size: int = 2_000) -> tuple[int, bool]:
        """从归档自己的消息表建一片索引;返回 (已索引行数, 是否建完)。

        与 `build_chunk` 是两件事,别混:那一条读 `search_messages`,做的是 v1→v2 的
        再推导,对一个**从来没建过索引**的归档跑它只会得到一个空索引并报告完成。
        这一条读的是 `messages`,是「从零建起来」唯一的那条路。

        形状照抄 `remote_search` 那条已经在生产里跑的回填:短事务、持久水位、幂等
        upsert,所以「至少一次」就够——崩在写水位之前,最多把这一片重做一遍。

        读归档表这件事在本模块有先例(几处元数据回填),同样用列存在性守卫:远端
        那份索引库连 `messages` 都没有,调用它必须安全空转而不是炸掉。
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        if not _INIT_REQUIRED_COLUMNS <= self._archive_columns("messages"):
            return (0, True)
        attachment_columns = self._archive_columns("attachments")
        has_attachments_table = {"message_id", "file_name"} <= attachment_columns
        attachment_select = (
            """,
                   EXISTS (
                       SELECT 1 FROM attachments AS a
                       WHERE a.message_id = m.id
                   ) AS has_attachment,
                   (
                       SELECT a2.file_name FROM attachments AS a2
                       WHERE a2.message_id = m.id AND a2.file_name IS NOT NULL
                       ORDER BY a2.id LIMIT 1
                   ) AS attachment_file_name"""
            if has_attachments_table
            else """,
                   0 AS has_attachment,
                   NULL AS attachment_file_name"""
        )
        # 从零建起来时转写也得一起进：索引里还没有行，「保留已有的」保留不到任何东西，
        # 而 `_backfill_transcripts` 只在加列那一刻跑一次。与它同一条取法。
        has_transcripts = (
            has_attachments_table
            and "sha256" in attachment_columns
            and {"sha256", "text", "status"} <= self._archive_columns("voice_transcripts")
        )
        transcript_select = (
            """,
                   COALESCE((
                       SELECT t.text FROM attachments AS a3
                       JOIN voice_transcripts AS t ON t.sha256 = a3.sha256
                       WHERE a3.message_id = m.id AND t.status = 'ok'
                       ORDER BY a3.id LIMIT 1
                   ), '') AS transcript"""
            if has_transcripts
            else """,
                   '' AS transcript"""
        )
        started_from = self.initialization_state()
        watermark_row = self._connection.execute(
            f"SELECT value FROM search_index_state WHERE key = '{_INIT_WATERMARK_KEY}'"
        ).fetchone()
        watermark = int(watermark_row[0]) if watermark_row is not None else 0
        rows = self._connection.execute(
            f"""
            SELECT m.rowid AS source_rowid, m.id, m.source_account_id,
                   m.conversation_id, m.created_at, m.content_text, m.sender_uid,
                   m.sender_display_name, m.message_type, m.is_self{attachment_select}{transcript_select}
            FROM messages AS m
            WHERE m.rowid > ?
            ORDER BY m.rowid
            LIMIT ?
            """,
            (watermark, chunk_size),
        ).fetchall()
        if not rows:
            with self._connection:
                if self._initialization_cancelled(started_from):
                    return (0, True)
                self._set_state(_INITIALIZED_KEY, INIT_READY)
            return (0, True)
        messages = [
            IndexedMessage(
                message_id=str(row["id"]),
                source_account_id=str(row["source_account_id"]),
                conversation_id=str(row["conversation_id"]),
                created_at=int(row["created_at"]),
                content=str(row["content_text"] or ""),
                sender_uid=str(row["sender_uid"] or ""),
                sender_display_name=row["sender_display_name"],
                message_type=str(row["message_type"] or "unknown"),
                has_attachment=bool(row["has_attachment"]),
                attachment_file_name=row["attachment_file_name"],
                is_self=bool(row["is_self"]),
                transcript=str(row["transcript"] or ""),
            )
            for row in rows
        ]
        # `upsert_many` 自己管事务,并且已经会拒收不可检索的类型(还会顺手删掉早年
        # 建进去的同类行),所以这里整片交给它,不在外面重复那条规则。
        self.upsert_many(messages)
        finished = len(rows) < chunk_size
        with self._connection:
            if self._initialization_cancelled(started_from):
                return (0, True)
            self._set_state(_INIT_WATERMARK_KEY, str(int(rows[-1]["source_rowid"])))
            self._set_state(
                _INITIALIZED_KEY, INIT_READY if finished else INIT_BUILDING
            )
        indexed = sum(
            1 for message in messages if searchable_message_type(message.message_type)
        )
        return (indexed, finished)

    def build_chunk(self, *, chunk_size: int = 2_000) -> tuple[int, bool]:
        """Index one slice of pre-v2 rows; returns (rows_indexed, finished).

        Runs in a single short IMMEDIATE transaction so a concurrent import is
        held out for milliseconds, not the whole rebuild, and a crash loses at
        most one chunk. The watermark makes it resumable from anywhere.
        """
        if chunk_size <= 0:
            raise ValueError("chunk_size must be positive")
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            engine = self.engine()
            if engine == ENGINE_V2:
                self._connection.commit()
                return (0, True)
            if engine == ENGINE_V1_LEGACY:
                raise RuntimeError(
                    "archive has no v2 schema; open it with ensure_schema=True first"
                )
            watermark_row = self._connection.execute(
                "SELECT value FROM search_index_state WHERE key = 'backfill_watermark'"
            ).fetchone()
            watermark = int(watermark_row[0]) if watermark_row is not None else 0
            rows = self._connection.execute(
                """
                SELECT rowid, message_id, source_account_id, conversation_id,
                       created_at, normalized_content, docid
                FROM search_messages
                WHERE rowid > ?
                ORDER BY rowid
                LIMIT ?
                """,
                (watermark, chunk_size),
            ).fetchall()
            if not rows:
                self._connection.execute(
                    "UPDATE search_index_state SET value = ? WHERE key = 'engine'",
                    (ENGINE_V2,),
                )
                self._connection.commit()
                return (0, True)
            for row in rows:
                docid = row["docid"]
                if docid is None:
                    docid = self._allocate_docid(
                        int(row["created_at"]), str(row["message_id"])
                    )
                    self._connection.execute(
                        "UPDATE search_messages SET docid = ? WHERE message_id = ?",
                        (docid, str(row["message_id"])),
                    )
                else:
                    docid = int(docid)
                account_ref = self._account_ref(str(row["source_account_id"]))
                conversation_ref = self._conversation_ref(
                    account_ref, str(row["conversation_id"])
                )
                normalized = str(row["normalized_content"])
                if self.backend is SearchBackend.FTS5_TRIGRAM:
                    # Dual-written rows already carry their FTS row; an FTS5
                    # table happily stores duplicate rowids, so check first.
                    present = self._connection.execute(
                        "SELECT 1 FROM search_fts_v2 WHERE rowid = ?", (docid,)
                    ).fetchone()
                    if present is not None:
                        self._connection.executemany(
                            "INSERT OR IGNORE INTO search_grams_v2"
                            "(gram, account_ref, neg_docid, conversation_ref) "
                            "VALUES (?, ?, ?, ?)",
                            (
                                (gram, account_ref, -docid, conversation_ref)
                                for gram in _index_grams_v2(normalized)
                            ),
                        )
                        continue
                self._insert_v2_entries(
                    docid=docid,
                    account_ref=account_ref,
                    conversation_ref=conversation_ref,
                    normalized=normalized,
                )
            self._connection.execute(
                "UPDATE search_index_state SET value = ? "
                "WHERE key = 'backfill_watermark'",
                (str(int(rows[-1]["rowid"])),),
            )
            self._connection.commit()
            return (len(rows), False)
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise

    def finalize_legacy_teardown(self) -> bool:
        """Drop the v1 structures once v2 answers queries. Returns whether it ran.

        Separate from the flip on purpose: dropping a 35M-row table takes real
        time, and holding the flip transaction open that long would stall a
        concurrent import for nothing. Between flip and teardown every reader
        already ignores v1.
        """
        if self.engine() != ENGINE_V2:
            return False
        self._connection.execute("BEGIN IMMEDIATE")
        try:
            if self.engine() != ENGINE_V2:
                self._connection.rollback()
                return False
            self._connection.execute("DROP TABLE IF EXISTS search_grams")
            self._connection.execute("DROP TABLE IF EXISTS search_messages_fts")
            self._connection.execute(f"DROP INDEX IF EXISTS {_IDENTITY_INDEX}")
            self._connection.commit()
            return True
        except BaseException:
            if self._connection.in_transaction:
                self._connection.rollback()
            raise


def _table_exists(connection: sqlite3.Connection, name: str) -> bool:
    return (
        connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (name,)
        ).fetchone()
        is not None
    )


def drop_search_index(connection: sqlite3.Connection) -> None:
    """删掉索引结构,把归档退回「没开」。

    删表而不是删行:22,783,788 行的 gram 表逐行删要几分钟并且把同样多的页面丢进
    空闲链表,而 DROP 是常数级的。表在下次写入时由 `_create_schema` 重新建出来,
    只读端看不到表就答 `not_initialized`——与这里写下的状态是同一句话。

    不数删了多少行:契约的响应模型装不下这个数,为了一个没人收到的数字先全表
    COUNT 一遍,会把常数级的「关掉」变成几秒。

    **不碰任何消息**。这是「关掉检索」,不是「删归档」。
    """
    with connection:
        for table in (
            "search_fts_v2",
            "search_messages_fts",
            "search_grams_v2",
            "search_grams",
            "search_messages",
            "search_conversations",
            "search_accounts",
        ):
            connection.execute(f"DROP TABLE IF EXISTS {table}")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS search_index_state (key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        for key, value in (
            (_INITIALIZED_KEY, INIT_NONE),
            (_INIT_WATERMARK_KEY, "0"),
            ("backfill_watermark", "0"),
        ):
            connection.execute(
                "INSERT OR REPLACE INTO search_index_state(key, value) VALUES (?, ?)",
                (key, value),
            )


# 每条可检索消息在索引里大约占多少字节。真机实测外推:724,288 条消息对应
# `search_fts_v2` 数据块 234 MiB 加 `search_grams_v2` 2278 万行(约 450 MiB),
# 合计约 684 MiB。**这是估算不是测量**,给出去的地方必须写「约」。
ESTIMATED_INDEX_BYTES_PER_MESSAGE = 990


def count_searchable_messages(
    connection: sqlite3.Connection,
    *,
    source_account_id: str | None = None,
) -> int:
    """归档里**可检索**的消息数——预估与完备性判断共用的这一份口径。

    「哪些类型该进索引」只能有一个答案:状态端点用它算 pending,搜索页的
    ready/building 判定用它比对已索引数。两处各写一遍谓词,`NON_SEARCHABLE_MESSAGE_TYPES`
    一变就会出现一边说 ready、另一边永远说 building。
    """
    if not _table_exists(connection, "messages"):
        return 0
    excluded = sorted(NON_SEARCHABLE_MESSAGE_TYPES)
    clauses: list[str] = []
    params: list[str] = []
    if source_account_id is not None:
        clauses.append("source_account_id = ?")
        params.append(source_account_id)
    if excluded:
        clauses.append(
            f"message_type NOT IN ({', '.join('?' for _ in excluded)})"
        )
        params.extend(excluded)
    where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
    row = connection.execute(
        f"SELECT COUNT(*) FROM messages{where}", tuple(params)
    ).fetchone()
    return int(row[0]) if row is not None else 0


def search_index_overview(
    connection: sqlite3.Connection,
    *,
    searchable_total: int | None = None,
) -> tuple[str, int, int]:
    """(状态, 已索引条数, 还没进索引的条数)。

    第三个数是开启前要如实告诉用户「这一步要做多少事」的依据,所以它数的是归档里
    **可检索**的消息,不是全部——把撤回通知那一类也算进去会让预估虚高。

    ``searchable_total`` 让调用方把那次全表计数（真机上是几百万行的扫描）缓存起来
    传进来:总数只随导入变化,而这个函数在索引端点的每一次 GET/POST/DELETE 上都
    要跑——每次请求都重扫一遍就是 PSU-2.36 那类「状态行计数」回归的形状。
    """
    index = MessageSearchIndex(connection, ensure_schema=False)
    state = index.initialization_state()
    indexed = 0
    if _table_exists(connection, "search_messages"):
        row = connection.execute("SELECT COUNT(*) FROM search_messages").fetchone()
        indexed = int(row[0]) if row is not None else 0
    total = (
        searchable_total
        if searchable_total is not None
        else count_searchable_messages(connection)
    )
    status = {
        INIT_NONE: SearchIndexStatus.NOT_INITIALIZED,
        INIT_BUILDING: SearchIndexStatus.BUILDING,
        INIT_READY: SearchIndexStatus.READY,
    }[state]
    return (status.value, indexed, max(0, total - indexed))


def complete_search_initialization(
    connection: sqlite3.Connection,
    *,
    chunk_size: int = 2_000,
    progress: Callable[[int], None] | None = None,
) -> int:
    """把一个处在 `building` 的归档建完;返回索引了多少条。

    可中断、可重跑:每一片是一个短事务加一次水位推进,崩在哪里下次就从哪里接着。
    对已经 ready 或压根没开的归档是空转——所以后台线程可以无脑调用它。
    """
    index = MessageSearchIndex(connection)
    if index.initialization_state() != INIT_BUILDING:
        return 0
    total = 0
    while True:
        indexed, finished = index.initialize_chunk(chunk_size=chunk_size)
        total += indexed
        if progress is not None and indexed:
            progress(total)
        if finished:
            return total


def rebuild_search_index(
    connection: sqlite3.Connection,
    *,
    chunk_size: int = 2_000,
    progress: Callable[[int], None] | None = None,
    purged: Callable[[int], None] | None = None,
) -> int:
    """Drive the excluded-type sweep and the v2 backfill on a read-write connection.

    The sweep runs first so the backfill never indexes rows it is about to
    lose; both are chunked, resumable and safe to re-run.
    """
    index = MessageSearchIndex(connection)
    removed = 0
    while True:
        deleted, swept = index.purge_chunk(chunk_size=chunk_size)
        removed += deleted
        if purged is not None and deleted:
            purged(removed)
        if swept:
            break
    total = 0
    while True:
        indexed, finished = index.build_chunk(chunk_size=chunk_size)
        total += indexed
        if progress is not None and indexed:
            progress(total)
        if finished:
            break
    index.finalize_legacy_teardown()
    return total

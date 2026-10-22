"""远端归档的检索索引：**本机那一套，原样跑在远端上**（PSU-2.45）。

远端的搜索到这一版为止是 `normalized_content` 上的全表 `instr()`，没有任何索引，
没有游标、没有总数、没有任何过滤。而本机早就有一套中文 bigram + FTS5 trigram 的
索引（`chat_archive.search.MessageSearchIndex`）。重写第二套是「重复开发本地端
逻辑」——这个模块做的事只有三件：**把远端的记录喂给它、把它的答案翻回远端的形状、
以及一条可续跑的存量回填**。

`MessageSearchIndex` 能被这样用，是因为它是自包含的（2026-08-27 逐条核实）：
模块级零 `chat_archive.*` 依赖，构造只吃一个 `sqlite3.Connection`，自己维护它那
六张表；它碰本机 `messages` / `attachments` 的四处全部有守卫——三处回填由
`PRAGMA table_info` 守（表不存在就是空集合，子集判定不成立，直接返回），
`purge_chunk` 由 `_has_table("messages")` 守。所以 `search.py` 一个字没改。

## 索引住在自己的库里

`search.sqlite`，与归档库并排。建进归档库虽然可行，但会让归档库混入索引表、
729k 回填期间归档库陪着抖，还与 `commit_batch` 的事务纠缠。代价是双写不原子——
用水位自愈：提交后顺带写索引，掉了的窗口由回填补齐。`upsert_many` 是幂等的
（先删同 id 行再插），所以「至少一次」就够，不需要跨两个库的两阶段提交。

## 归属折进键里

一个 `search.sqlite` 可能服务同一进程里的多个归档，而索引没有 archive 维度。
索引眼里 `message_id` 与账号串都是**不透明的**，所以把归档折进这两个键即可，
零 schema 改动。NUL 到不了任何一段（归档标识与 `record_key` 都经线上校验），
拼接可逆，出站时剥掉。
"""

from __future__ import annotations

import logging
import sqlite3
import threading
from collections.abc import Callable, Iterable, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path

from chat_archive.ports.metadata import StoredRecord
from chat_archive.search import IndexedMessage, MessageSearchIndex, SearchIndexStatus

# 归档标识与账号标识、记录标识之间的分隔符。两段都经线上校验（`^[A-Za-z0-9_-]+$`
# 一类的模式），NUL 到不了其中任何一段，所以拼接可逆、两个不同的对拼不出同一个键。
_SEPARATOR = "\x00"

# 回填一片多少行。与本机 `build_chunk` 同一个量级：短事务，一次崩溃最多丢一片，
# 而一片的重做是幂等的。
DEFAULT_BACKFILL_CHUNK = 2_000

_logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class RemoteSearchResult:
    """索引答出来的一页，用远端自己的标识说话。

    只有**标识**：正文那一份由归档库按 `record_key` 回取，于是线上那一条记录的
    形状与列表、转录、以及这个端点从前答的完全相同——读的人不该分得出一条记录
    是搜出来的还是翻出来的。
    """

    record_keys: tuple[str, ...]
    next_after: tuple[int, str] | None
    total_count: int
    conversation_counts: dict[str, int]
    index_status: str


class RemoteSearchIndex:
    """远端归档的检索索引：一个 `search.sqlite`，一份 `MessageSearchIndex`。

    每次操作新开一条连接（与 `SQLiteMetadataStore` 同一种做法），因为 FastAPI 的
    同步路由跑在线程池里，而 `sqlite3.Connection` 不是线程安全的。开销是
    `ensure_schema=False` 下的两次极小查询——schema 在构造时一次性建好。
    """

    def __init__(self, database_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy timeout must be positive")
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        database_path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    # ------------------------------------------------------------------ 连接 --

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._database_path,
            timeout=self._busy_timeout_ms / 1_000,
        )
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            # 索引自己那六张表由 `MessageSearchIndex` 建；一个空库开出来就是 v2
            # （`_create_schema` 在没有存量行时直接落 engine='v2'），所以远端这条
            # 路上根本不存在 v1 遗留那一支。
            MessageSearchIndex(connection, ensure_schema=True)
            # 我自己的那张：回填水位。刻意**不**去写 `search_index_state`——
            # 那几个键是 `search.py` 自己的状态机，塞一个它不认得的键进去，
            # 将来它多一个键就撞上了。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS remote_search_state (
                    archive_id TEXT PRIMARY KEY,
                    -- 已经喂进索引的最大 server_sequence。回填从这里往后走，
                    -- 进程被杀了也能从这里续。
                    indexed_server_sequence INTEGER NOT NULL
                )
                """
            )
            # 存量转写补索引的进度。转写与记录分开到达：先于记录到的，记录进索引时
            # 顺带接上；晚于记录到的，收下那一刻接上。这张表管的是这两条路上线以前
            # 就已经收下的那些——它们两条路都没赶上。按 `record_key` 走，扫完记 done。
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS remote_search_transcript_sweep (
                    archive_id TEXT PRIMARY KEY,
                    after_record_key TEXT NOT NULL,
                    done INTEGER NOT NULL
                )
                """
            )
            connection.commit()

    def _index(self, connection: sqlite3.Connection) -> MessageSearchIndex:
        # `ensure_schema=False`：只读连接与工作线程都不该跑 DDL，后端用哪一种
        # 由写入方已经物化下来的东西说了算。
        return MessageSearchIndex(connection, ensure_schema=False)

    # ------------------------------------------------------------------ 键 ----

    @staticmethod
    def account_key(archive_id: str, source_account_id: str | None) -> str:
        """索引眼里的「账号」。未归属的行归到一个空账号下，而不是散落成 NULL。"""
        return f"{archive_id}{_SEPARATOR}{source_account_id or ''}"

    @staticmethod
    def message_key(archive_id: str, record_key: str) -> str:
        return f"{archive_id}{_SEPARATOR}{record_key}"

    @staticmethod
    def _record_key_of(message_id: str) -> str:
        _, separator, record_key = message_id.partition(_SEPARATOR)
        return record_key if separator else message_id

    @staticmethod
    def to_indexed(archive_id: str, record: StoredRecord) -> IndexedMessage:
        """一条远端记录在索引里的样子。

        逐字段直映射，唯一取不到的是 `attachment_file_name`——附件文件名不在上传
        字节里（总方案第 2 类差异 12），保持 null。它**只影响展示不影响匹配**：
        本机也从不把它放进 WHERE/MATCH。

        `sender_uid` 与 `is_self` 的缺省不是猜：索引的字段是非空的，而「远端没被
        告知」在这一层只能落成空串 / False。搜索卡上那两处仍由记录本身的 null
        说话（正文由归档库回取），所以界面不会因为这里的缺省而替远端断言什么。
        """
        return IndexedMessage(
            message_id=RemoteSearchIndex.message_key(archive_id, record.record_key),
            source_account_id=RemoteSearchIndex.account_key(
                archive_id, record.source_account_id
            ),
            conversation_id=record.conversation_id,
            created_at=record.created_at,
            content=record.content_text,
            sender_uid=record.sender_uid or "",
            sender_display_name=record.sender_display_name,
            message_type=record.message_type or "unknown",
            has_attachment=bool(record.attachment_refs),
            attachment_file_name=None,
            is_self=record.is_self is True,
            # 记录上没挂转写 = 这条路没去取，不是「没有」：保留索引里已有的那份。
            transcript=None if record.transcript is None else record.transcript.text,
        )

    # ------------------------------------------------------------------ 写 ----

    def index_records(self, archive_id: str, records: Iterable[StoredRecord]) -> None:
        """把这一批记录喂进索引。幂等。"""
        messages = [self.to_indexed(archive_id, record) for record in records]
        if not messages:
            return
        with closing(self._connect()) as connection:
            self._index(connection).upsert_many(messages)

    def refresh_transcripts(
        self, archive_id: str, transcripts: Iterable[tuple[str, str]]
    ) -> int:
        """把 `(record_key, 转写)` 接进已经索引过的行；返回接上了几条。

        还没进索引的记录跳过：它将来被索引时，调用方会把转写一起挂在记录上带进来。
        """
        items = tuple(transcripts)
        if not items:
            return 0
        refreshed = 0
        with closing(self._connect()) as connection:
            index = self._index(connection)
            for record_key, text in items:
                if index.refresh_transcript(self.message_key(archive_id, record_key), text):
                    refreshed += 1
        return refreshed

    def transcript_sweep_position(self, archive_id: str) -> str | None:
        """存量转写补索引走到哪了；`None` 是已经扫完。"""
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT after_record_key, done FROM remote_search_transcript_sweep "
                "WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        if row is None:
            return ""
        return None if row["done"] else str(row["after_record_key"])

    def set_transcript_sweep_position(self, archive_id: str, after: str | None) -> None:
        """`after=None` 记为扫完；`after=""` 是从头再扫一遍（接不上时的退路）。"""
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO remote_search_transcript_sweep(archive_id, after_record_key, done)
                VALUES (?, ?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    after_record_key = excluded.after_record_key,
                    done = excluded.done
                """,
                (archive_id, after or "", int(after is None)),
            )
            connection.commit()

    def rehome_records(
        self, archive_id: str, records: Iterable[StoredRecord]
    ) -> tuple[str, ...]:
        """这批记录换了会话：索引里只改归属；返回改不了、要整条重写的 `record_key`。

        `records` 是搬完之后再读出来的行，`conversation_id` 已经是新家。**不在这里
        整条重写**（PSU-2.101 评审订正）：这条路跑在归档库写锁外面，而整条重写读的
        是一份快照——并发的批次提交刚改过的行会被它盖回去。交还的键留在账本上，由
        写锁里的重放路径按记录表现状重写。
        """
        items = tuple(records)
        if not items:
            return ()
        with closing(self._connect()) as connection:
            leftovers = set(
                self._index(connection).rehome_conversations(
                    tuple(
                        (
                            self.message_key(archive_id, record.record_key),
                            self.account_key(archive_id, record.source_account_id),
                            record.conversation_id,
                        )
                        for record in items
                    )
                )
            )
        return tuple(
            record.record_key
            for record in items
            if self.message_key(archive_id, record.record_key) in leftovers
        )

    def indexed_sequence(self, archive_id: str) -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT indexed_server_sequence FROM remote_search_state WHERE archive_id = ?",
                (archive_id,),
            ).fetchone()
        return 0 if row is None else int(row[0])

    def advance_indexed_sequence(self, archive_id: str, sequence: int) -> None:
        """水位只前进，不后退。

        重传会让一条已经索引过的记录拿到更高的 server_sequence，而回填是按
        sequence 走的——水位后退会让回填把已经索引过的一大段重做一遍；水位前进
        过头则会漏。所以取最大值，漏掉的那些由「提交后顺带写索引」这条路覆盖。
        """
        with closing(self._connect()) as connection:
            connection.execute(
                """
                INSERT INTO remote_search_state(archive_id, indexed_server_sequence)
                VALUES (?, ?)
                ON CONFLICT(archive_id) DO UPDATE SET
                    indexed_server_sequence = MAX(
                        remote_search_state.indexed_server_sequence, excluded.indexed_server_sequence
                    )
                """,
                (archive_id, sequence),
            )
            connection.commit()

    # ------------------------------------------------------------------ 读 ----

    def index_status(self, archive_id: str, *, last_server_sequence: int) -> str:
        """索引跟上归档了没有。

        `building` 时界面必须如实说「索引仍在构建，当前结果可能不完整」——
        本机早有这句文案。假装完整比慢更糟：读的人会把「搜不到」读成「没有」。
        """
        if last_server_sequence <= 0:
            return SearchIndexStatus.READY.value
        if self.indexed_sequence(archive_id) >= last_server_sequence:
            return SearchIndexStatus.READY.value
        return SearchIndexStatus.BUILDING.value

    def search(
        self,
        archive_id: str,
        query: str | None,
        *,
        accounts: Sequence[str | None],
        last_server_sequence: int,
        conversation_id: str | None = None,
        sender_uid: str | None = None,
        message_types: Sequence[str] | None = None,
        has_attachment: bool | None = None,
        date_from: int | None = None,
        date_to: int | None = None,
        after: tuple[int, str] | None = None,
        limit: int,
    ) -> RemoteSearchResult:
        """一页命中。

        `accounts` 是要问的上传流。带账号过滤时它只有一个；不带时是归档里的全部
        （外加未归属的那一个空账号）——索引是按账号问的，而「不带过滤」是这个端点
        从前就有的能力，不能因为换了实现就悄悄不支持了。

        扇出之所以可行，是因为 `docid` **全局按时间有序**（`created_at << 20 | 哈希`）：
        同一个 `after` 拿给每条流各走一页，归并后截断，下一页游标取归并后的最后
        一个——每条流自己的键集都从同一个全局位置往后走，所以不重不漏。
        """
        if limit <= 0 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        status = self.index_status(archive_id, last_server_sequence=last_server_sequence)
        keys = [self.account_key(archive_id, account) for account in accounts]
        if not keys:
            return RemoteSearchResult((), None, 0, {}, status)
        # 游标在线上说的是**远端自己的** `record_key`，索引里那个键是折了归档的
        # 合成串。不还原就查不到那一行，`_after_docid` 会退回按哈希算的候选位——
        # 那是另一个 docid，页与页之间会跳空，而且不报错。
        position = (
            None
            if after is None
            else (after[0], self.message_key(archive_id, after[1]))
        )
        with closing(self._connect()) as connection:
            index = self._index(connection)
            pages = [
                index.search_page(
                    key,
                    query,
                    conversation_ids=() if conversation_id is None else (conversation_id,),
                    sender_uids=() if sender_uid is None else (sender_uid,),
                    message_types=message_types,
                    has_attachment=has_attachment,
                    date_from=date_from,
                    date_to=date_to,
                    after=position,
                    limit=limit,
                )
                for key in keys
            ]
            counts: dict[str, int] = {}
            for page in pages:
                for conversation, count in page.conversation_counts.items():
                    counts[conversation] = counts.get(conversation, 0) + count
            hits = [hit for page in pages for hit in page.hits]
            if len(pages) == 1:
                ordered = hits
            else:
                # 归并要用**真的那个 docid**，不能拿 (created_at, message_id) 凑：
                # 同一秒内的次序由 docid 的消歧位决定，换一个比较器就不再是索引
                # 自己的顺序，游标一取就可能跳过行。
                docids = self._docids(connection, [hit.message_id for hit in hits])
                ordered = sorted(
                    hits,
                    key=lambda hit: -docids.get(hit.message_id, 0),
                )
        kept = ordered[:limit]
        more = len(ordered) > limit or any(page.next_after is not None for page in pages)
        next_after = (
            (kept[-1].created_at, self._record_key_of(kept[-1].message_id))
            if more and kept
            else None
        )
        # 状态以「索引落后了没有」为准，但索引自己也会说话（重建中）——两边任一
        # 说没跟上，就是没跟上。
        if any(page.index_status is not SearchIndexStatus.READY for page in pages):
            status = SearchIndexStatus.BUILDING.value
        return RemoteSearchResult(
            record_keys=tuple(self._record_key_of(hit.message_id) for hit in kept),
            next_after=next_after,
            total_count=sum(counts.values()),
            conversation_counts=counts,
            index_status=status,
        )

    @staticmethod
    def _docids(
        connection: sqlite3.Connection, message_ids: Sequence[str]
    ) -> dict[str, int]:
        found: dict[str, int] = {}
        for offset in range(0, len(message_ids), 500):
            batch = tuple(message_ids[offset : offset + 500])
            placeholders = ", ".join("?" for _ in batch)
            for row in connection.execute(
                f"SELECT message_id, docid FROM search_messages "
                f"WHERE message_id IN ({placeholders})",
                batch,
            ).fetchall():
                if row["docid"] is not None:
                    found[str(row["message_id"])] = int(row["docid"])
        return found


class SearchBackfillWorker:
    """把存量记录喂进索引的后台线程。

    **为什么是线程而不是一条运维命令**（这一点在计划里改过主意，记在这里）：
    运维命令要先停服务——`SingleWriterLease` 就是为「一个数据目录一个写者」立的，
    而 729k 行要跑几分钟。让搜索能用的代价是几分钟停机，不值当。

    而线程不占启动路径：应用照常起来、照常服务，只是搜索先答
    `index_status = "building"`，界面如实说「索引仍在构建，当前结果可能不完整」。
    这条线程只写 `search.sqlite`（归档库它只读），所以既不与同步抢锁，也不与
    正在跑的附件长传抢任何东西。

    跑完不退出，隔一阵再看一眼：后来新开的归档、以及「提交后顺带写索引」那条路
    掉过的窗口，都在这里补上。
    """

    def __init__(
        self,
        *,
        archives: Callable[[], Sequence[str]],
        backfill: Callable[[str, int], tuple[int, bool]],
        chunk_size: int = DEFAULT_BACKFILL_CHUNK,
        idle_seconds: float = 60.0,
    ) -> None:
        if chunk_size <= 0 or idle_seconds <= 0:
            raise ValueError("backfill pacing must be positive")
        self._archives = archives
        self._backfill = backfill
        self._chunk_size = chunk_size
        self._idle_seconds = idle_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="chat-archive-search-backfill", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def run_once(self) -> int:
        """把每个归档都追平，返回这一趟索引了多少行。测试驱动的就是这个。"""
        indexed = 0
        for archive_id in self._archives():
            while not self._stop.is_set():
                rows, finished = self._backfill(archive_id, self._chunk_size)
                indexed += rows
                if finished:
                    break
        return indexed

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                # 回填是派生物的重建，塌了不该把服务带走——下一轮再来。
                # 但必须留下痕迹：一个安静地永远追不平的索引，界面会一直说
                # 「仍在构建」而没人知道为什么。
                _logger.exception("search index backfill round failed")
            self._stop.wait(self._idle_seconds)


__all__ = [
    "DEFAULT_BACKFILL_CHUNK",
    "RemoteSearchIndex",
    "RemoteSearchResult",
    "SearchBackfillWorker",
]

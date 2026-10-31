"""Persistent jobs.sqlite state machine and resumable event feed."""

from __future__ import annotations

import json
import re
import sqlite3
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import closing, contextmanager
from pathlib import Path
from typing import cast

from chat_archive.domain.models import JobStatus
from chat_archive.ports.jobs import (
    Checkpoint,
    CheckpointValue,
    ControlPointResult,
    ExportJobsRemoved,
    JobEvent,
    JobNotFoundError,
    JobRecord,
    JobRepositoryClosedError,
    JobStateError,
    JobStorageSettings,
    JobTaskSpec,
    JobTaskSpecValue,
    JobValidationError,
    ProgressUnit,
)


JOBS_SCHEMA_VERSION = 4
_JOB_ID = re.compile(r"^job_[A-Za-z0-9_-]{8,64}$")
_ERROR_CODE = re.compile(r"^[A-Z][A-Z0-9_]{2,63}$")
_DETAILS_REF = re.compile(r"^diag_[A-Za-z0-9_-]{8,64}$")
_ALLOWED_KINDS = frozenset(
    {
        "import",
        "export",
        "media_rebuild",
        "index_rebuild",
        "sync",
        "reconcile",
        "restore",
        "key_rotation",
        # 回收磁盘空间（PSU-2.88）。实测分钟级（prune 116s + VACUUM 194s），
        # 所以它必须是一个可轮询的作业，而不是一个必然超时的同步请求。
        "reclaim",
    }
)
_CHECKPOINT_KEYS = frozenset(
    {
        "source_instance_id",
        "source_db_fingerprint",
        "partition",
        "table",
        "row_id",
        "sequence",
        "create_time",
        "overlap_from_sequence",
        "parser_version",
        "snapshot_fingerprint",
        "message_cursor",
        "batch_index",
        "messages_complete",
        "oldest_imported_at",
        # 空间不足停下时那两个数（PSU-2.104）：作业行自己说得出「远端还剩多少、
        # 这台机器估计还欠多少」，界面据此才问得出「仍然继续吗」。
        "remote_available_bytes",
        "pending_bytes",
        # 这一轮为什么停在这一段（PSU-2.98 C3）。白名单是刻意的——checkpoint 是
        # 一条要被反序列化的持久化结构，不是一个随手塞东西的口袋。
        "stopped_error_code",
        "verification_phase",
        "verification_last_progress_at",
        "verification_prepared_count",
        "deferred_avatar_count",
        "sync_activity",
        "sync_last_progress_at",
    }
)
_TERMINAL = frozenset(
    {JobStatus.COMPLETED, JobStatus.FAILED, JobStatus.CANCELLED}
)
# 正占着一条工作线程的三档（PSU-2.109 收成一份）。四个判据里三个用的就是这一套，
# 第四个（缓存维护那个）在它之上再加两档；差别写在各自的方法上，值只留一份——
# 从前是四处各写一遍，改判据要记得改四个地方，而漏掉哪一个都不会有用例说话。
_HOLDS_A_WORKER: tuple[str, ...] = (
    JobStatus.QUEUED.value,
    JobStatus.RUNNING.value,
    JobStatus.PAUSING.value,
)
# 可恢复的持久状态：不占线程，但缓存维护一动库它们就回不来了。
_RESUMABLE: tuple[str, ...] = (JobStatus.PAUSED.value, JobStatus.INTERRUPTED.value)


def _status_in(statuses: tuple[str, ...]) -> str:
    """`status IN (?, ?, …)` 的那一段。拼的只有占位符，值仍旧走绑定。"""
    return "status IN (" + ", ".join("?" for _ in statuses) + ")"


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class SQLiteJobRepository:
    """Store job commands and states separately from the archive database."""

    def __init__(
        self,
        database_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._database_path = database_path
        self._busy_timeout_ms = busy_timeout_ms
        self._write_lock = threading.Lock()
        self._event_condition = threading.Condition()
        self._state_lock = threading.Lock()
        self._closed = False
        database_path.parent.mkdir(parents=True, exist_ok=True)
        from chat_archive.migration_tracking import migration_guard
        with migration_guard(database_path, {"jobs": JOBS_SCHEMA_VERSION}):
            self._initialize()

    def __enter__(self) -> SQLiteJobRepository:
        return self

    def __exit__(
        self,
        _exception_type: object,
        _exception: object,
        _traceback: object,
    ) -> None:
        self.close()

    def close(self) -> None:
        with self._state_lock:
            self._closed = True
        with self._event_condition:
            self._event_condition.notify_all()

    def _require_open(self) -> None:
        with self._state_lock:
            if self._closed:
                raise JobRepositoryClosedError("job repository is closed")

    def _connect(self, *, read_only: bool = False) -> sqlite3.Connection:
        if read_only:
            uri = f"{self._database_path.resolve(strict=False).as_uri()}?mode=ro"
            connection = sqlite3.connect(
                uri,
                uri=True,
                timeout=self._busy_timeout_ms / 1_000,
            )
        else:
            connection = sqlite3.connect(
                self._database_path,
                timeout=self._busy_timeout_ms / 1_000,
            )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @contextmanager
    def _write_transaction(self) -> Generator[sqlite3.Connection]:
        self._require_open()
        with self._write_lock, closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                yield connection
                connection.commit()
            except Exception:
                connection.rollback()
                raise

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS job_schema (
                    singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                    version INTEGER NOT NULL
                );
                INSERT INTO job_schema(singleton, version) VALUES (1, 4)
                ON CONFLICT(singleton) DO NOTHING;

                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY,
                    source_account_id TEXT NOT NULL,
                    kind TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT,
                    progress_current INTEGER NOT NULL,
                    progress_total INTEGER,
                    stage_progress_current INTEGER NOT NULL,
                    stage_progress_total INTEGER,
                    stage_progress_unit TEXT,
                    stage_started_at INTEGER,
                    checkpoint_json TEXT,
                    task_spec_json TEXT,
                    recoverable INTEGER NOT NULL,
                    pause_requested INTEGER NOT NULL,
                    cancel_requested INTEGER NOT NULL,
                    error_code TEXT,
                    details_ref TEXT,
                    created_at INTEGER NOT NULL,
                    started_at INTEGER,
                    finished_at INTEGER,
                    updated_at INTEGER NOT NULL,
                    -- `kind` 故意**没有** CHECK 约束，`_ALLOWED_KINDS` 是唯一的
                    -- 那一份清单。这里曾经有一份重复的，而 PSU-2.88 加 `reclaim`
                    -- 时只改了 Python 那份：CHECK 约束在 SQLite 里改不动（没有
                    -- ALTER CONSTRAINT），既有 `jobs.sqlite` 也从不重建，于是
                    -- **回收作业在任何一台已经用过的机器上都建不出来**——而报出
                    -- 来的是「查询参数无效」，因为 IntegrityError 被当成了主键冲突。
                    -- 一份写在 Python 里、一份写在 SQL 里，只有下一次加动词时才会
                    -- 发现它们已经漂开了。所以只留一份。
                    CHECK(status IN (
                        'queued', 'running', 'pausing', 'paused', 'interrupted',
                        'completed', 'failed', 'cancelled'
                    )),
                    CHECK(recoverable IN (0, 1)),
                    CHECK(pause_requested IN (0, 1)),
                    CHECK(cancel_requested IN (0, 1)),
                    CHECK(progress_current >= 0),
                    CHECK(progress_total IS NULL OR progress_total >= 0),
                    CHECK(stage_progress_current >= 0),
                    CHECK(stage_progress_total IS NULL OR stage_progress_total >= 0),
                    CHECK(stage_progress_unit IS NULL OR stage_progress_unit IN ('bytes', 'items'))
                );
                CREATE INDEX IF NOT EXISTS ix_jobs_kind_status
                    ON jobs(kind, status, created_at, id);
                CREATE INDEX IF NOT EXISTS ix_jobs_account_status
                    ON jobs(source_account_id, status, updated_at, id);

                CREATE TABLE IF NOT EXISTS job_events (
                    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
                    source_account_id TEXT NOT NULL,
                    event_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    stage TEXT,
                    progress_current INTEGER NOT NULL,
                    progress_total INTEGER,
                    stage_progress_current INTEGER NOT NULL,
                    stage_progress_total INTEGER,
                    stage_progress_unit TEXT,
                    stage_started_at INTEGER,
                    error_code TEXT,
                    created_at INTEGER NOT NULL
                );
                CREATE INDEX IF NOT EXISTS ix_job_events_account_sequence
                    ON job_events(source_account_id, sequence);
                CREATE INDEX IF NOT EXISTS ix_job_events_job_sequence
                    ON job_events(job_id, sequence);
                """
            )
            version = connection.execute(
                "SELECT version FROM job_schema WHERE singleton = 1"
            ).fetchone()
            if version is None:
                raise JobValidationError("jobs database schema version is unsupported")
            schema_version = int(version[0])
            if schema_version == 1:
                connection.executescript(
                    """
                    ALTER TABLE jobs ADD COLUMN stage_progress_current INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE jobs ADD COLUMN stage_progress_total INTEGER;
                    ALTER TABLE jobs ADD COLUMN stage_progress_unit TEXT;
                    ALTER TABLE jobs ADD COLUMN stage_started_at INTEGER;
                    ALTER TABLE job_events ADD COLUMN stage_progress_current INTEGER NOT NULL DEFAULT 0;
                    ALTER TABLE job_events ADD COLUMN stage_progress_total INTEGER;
                    ALTER TABLE job_events ADD COLUMN stage_progress_unit TEXT;
                    ALTER TABLE job_events ADD COLUMN stage_started_at INTEGER;
                    UPDATE job_schema SET version = 2 WHERE singleton = 1;
                    """
                )
                schema_version = 2
            if schema_version == 2:
                connection.executescript(
                    """
                    ALTER TABLE jobs ADD COLUMN task_spec_json TEXT;
                    UPDATE job_schema SET version = 3 WHERE singleton = 1;
                    """
                )
                schema_version = 3
            if schema_version == 3:
                # 把 `kind` 上那条过时的 CHECK 拆掉。SQLite 没有 ALTER CONSTRAINT，
                # 只能重建表。不重建的话，v4 之前建过的每一个 `jobs.sqlite` 都拒绝
                # `reclaim` 作业（PSU-2.88 只加了 Python 侧的白名单），而报出来的是
                # 「查询参数无效」——真机 2026-09-02 就是这么卡住的。
                #
                # **两个 PRAGMA 一个都不能少**：`job_events.job_id` 带
                # `REFERENCES jobs(id) ON DELETE CASCADE`，而自 SQLite 3.25 起
                # `ALTER TABLE ... RENAME` 会把别处的 REFERENCES 一起改写过去。
                # 不关 `legacy_alter_table`，`job_events` 就会指向 `jobs_pre_v4`，
                # 那张表随后被 DROP——外键指向一张不存在的表。不关 `foreign_keys`，
                # 那次 DROP 还会顺着 CASCADE 把事件表清空
                # （alembic 那边踩过同一个坑，见 batch drop_column 那次）。
                connection.execute("PRAGMA foreign_keys = OFF")
                connection.execute("PRAGMA legacy_alter_table = ON")
                try:
                    connection.executescript(
                        """
                        ALTER TABLE jobs RENAME TO jobs_pre_v4;
                        CREATE TABLE jobs (
                            id TEXT PRIMARY KEY,
                            source_account_id TEXT NOT NULL,
                            kind TEXT NOT NULL,
                            status TEXT NOT NULL,
                            stage TEXT,
                            progress_current INTEGER NOT NULL,
                            progress_total INTEGER,
                            stage_progress_current INTEGER NOT NULL DEFAULT 0,
                            stage_progress_total INTEGER,
                            stage_progress_unit TEXT,
                            stage_started_at INTEGER,
                            checkpoint_json TEXT,
                            task_spec_json TEXT,
                            recoverable INTEGER NOT NULL,
                            pause_requested INTEGER NOT NULL,
                            cancel_requested INTEGER NOT NULL,
                            error_code TEXT,
                            details_ref TEXT,
                            created_at INTEGER NOT NULL,
                            started_at INTEGER,
                            finished_at INTEGER,
                            updated_at INTEGER NOT NULL,
                            CHECK(status IN (
                                'queued', 'running', 'pausing', 'paused', 'interrupted',
                                'completed', 'failed', 'cancelled'
                            )),
                            CHECK(recoverable IN (0, 1)),
                            CHECK(pause_requested IN (0, 1)),
                            CHECK(cancel_requested IN (0, 1)),
                            CHECK(progress_current >= 0),
                            CHECK(progress_total IS NULL OR progress_total >= 0),
                            CHECK(stage_progress_current >= 0),
                            CHECK(stage_progress_total IS NULL
                                  OR stage_progress_total >= 0),
                            CHECK(stage_progress_unit IS NULL
                                  OR stage_progress_unit IN ('bytes', 'items'))
                        );
                        INSERT INTO jobs SELECT
                            id, source_account_id, kind, status, stage,
                            progress_current, progress_total,
                            stage_progress_current, stage_progress_total,
                            stage_progress_unit, stage_started_at,
                            checkpoint_json, task_spec_json,
                            recoverable, pause_requested, cancel_requested,
                            error_code, details_ref,
                            created_at, started_at, finished_at, updated_at
                        FROM jobs_pre_v4;
                        DROP TABLE jobs_pre_v4;
                        CREATE INDEX IF NOT EXISTS ix_jobs_account_status
                            ON jobs(source_account_id, status, updated_at, id);
                        UPDATE job_schema SET version = 4 WHERE singleton = 1;
                        """
                    )
                finally:
                    connection.execute("PRAGMA legacy_alter_table = OFF")
                    connection.execute("PRAGMA foreign_keys = ON")
                violations = connection.execute("PRAGMA foreign_key_check").fetchall()
                if violations:
                    # 重建之后自检一次。这条永远不该触发，而它真触发时，
                    # 沉默地继续等于把一个引用悬空的作业库交给下一次启动。
                    raise JobValidationError(
                        f"jobs schema upgrade left {len(violations)} dangling reference(s)"
                    )
                schema_version = 4
            if schema_version != JOBS_SCHEMA_VERSION:
                raise JobValidationError("jobs database schema version is unsupported")
            connection.commit()

    def storage_settings(self) -> JobStorageSettings:
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            journal_mode = str(connection.execute("PRAGMA journal_mode").fetchone()[0])
            busy_timeout_ms = int(connection.execute("PRAGMA busy_timeout").fetchone()[0])
        return JobStorageSettings(journal_mode, busy_timeout_ms)

    @staticmethod
    def _deserialize_checkpoint(value: object) -> dict[str, CheckpointValue] | None:
        if value is None:
            return None
        parsed = json.loads(str(value))
        if not isinstance(parsed, dict):
            raise JobValidationError("stored checkpoint is invalid")
        raw_checkpoint = cast(dict[object, object], parsed)
        checkpoint: dict[str, CheckpointValue] = {}
        for key, item in raw_checkpoint.items():
            if not isinstance(key, str) or not (
                item is None or isinstance(item, (str, int))
            ):
                raise JobValidationError("stored checkpoint is invalid")
            checkpoint[key] = item
        return checkpoint

    @staticmethod
    def _deserialize_task_spec(
        value: object,
    ) -> dict[str, JobTaskSpecValue] | None:
        if value is None:
            return None
        parsed = json.loads(str(value))
        if not isinstance(parsed, dict):
            raise JobValidationError("stored task specification is invalid")
        raw_spec = cast(dict[object, object], parsed)
        task_spec: dict[str, JobTaskSpecValue] = {}
        for key, item in raw_spec.items():
            if not isinstance(key, str) or not key:
                raise JobValidationError("stored task specification is invalid")
            if isinstance(item, list):
                raw_items = cast(list[object], item)
                if not all(isinstance(entry, str) for entry in raw_items):
                    raise JobValidationError("stored task specification is invalid")
                task_spec[key] = [str(entry) for entry in raw_items]
                continue
            if item is None or isinstance(item, (str, int, bool)):
                task_spec[key] = item
                continue
            raise JobValidationError("stored task specification is invalid")
        return task_spec

    @classmethod
    def _job(cls, row: sqlite3.Row) -> JobRecord:
        return JobRecord(
            id=str(row["id"]),
            source_account_id=str(row["source_account_id"]),
            kind=str(row["kind"]),
            status=JobStatus(str(row["status"])),
            stage=None if row["stage"] is None else str(row["stage"]),
            progress_current=int(row["progress_current"]),
            progress_total=(
                None if row["progress_total"] is None else int(row["progress_total"])
            ),
            stage_progress_current=int(row["stage_progress_current"]),
            stage_progress_total=(
                None
                if row["stage_progress_total"] is None
                else int(row["stage_progress_total"])
            ),
            stage_progress_unit=(
                None
                if row["stage_progress_unit"] is None
                else cast(ProgressUnit, str(row["stage_progress_unit"]))
            ),
            stage_started_at=(
                None if row["stage_started_at"] is None else int(row["stage_started_at"])
            ),
            checkpoint=cls._deserialize_checkpoint(row["checkpoint_json"]),
            task_spec=cls._deserialize_task_spec(row["task_spec_json"]),
            recoverable=bool(row["recoverable"]),
            pause_requested=bool(row["pause_requested"]),
            cancel_requested=bool(row["cancel_requested"]),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            details_ref=None if row["details_ref"] is None else str(row["details_ref"]),
            created_at=int(row["created_at"]),
            started_at=None if row["started_at"] is None else int(row["started_at"]),
            finished_at=None if row["finished_at"] is None else int(row["finished_at"]),
            updated_at=int(row["updated_at"]),
        )

    @staticmethod
    def _event(row: sqlite3.Row) -> JobEvent:
        return JobEvent(
            sequence=int(row["sequence"]),
            job_id=str(row["job_id"]),
            source_account_id=str(row["source_account_id"]),
            event_type=str(row["event_type"]),
            status=JobStatus(str(row["status"])),
            stage=None if row["stage"] is None else str(row["stage"]),
            progress_current=int(row["progress_current"]),
            progress_total=(
                None if row["progress_total"] is None else int(row["progress_total"])
            ),
            stage_progress_current=int(row["stage_progress_current"]),
            stage_progress_total=(
                None
                if row["stage_progress_total"] is None
                else int(row["stage_progress_total"])
            ),
            stage_progress_unit=(
                None
                if row["stage_progress_unit"] is None
                else cast(ProgressUnit, str(row["stage_progress_unit"]))
            ),
            stage_started_at=(
                None if row["stage_started_at"] is None else int(row["stage_started_at"])
            ),
            error_code=None if row["error_code"] is None else str(row["error_code"]),
            created_at=int(row["created_at"]),
        )

    @staticmethod
    def _load_for_update(connection: sqlite3.Connection, job_id: str) -> sqlite3.Row:
        row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError("job was not found")
        return row

    @staticmethod
    def _append_event(
        connection: sqlite3.Connection,
        job: JobRecord,
        event_type: str,
    ) -> None:
        connection.execute(
            """
            INSERT INTO job_events(
                job_id, source_account_id, event_type, status, stage,
                progress_current, progress_total, stage_progress_current,
                stage_progress_total, stage_progress_unit, stage_started_at,
                error_code, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                job.id,
                job.source_account_id,
                event_type,
                job.status.value,
                job.stage,
                job.progress_current,
                job.progress_total,
                job.stage_progress_current,
                job.stage_progress_total,
                job.stage_progress_unit,
                job.stage_started_at,
                job.error_code,
                job.updated_at,
            ),
        )

    def _notify_events(self) -> None:
        with self._event_condition:
            self._event_condition.notify_all()

    @staticmethod
    def _validate_create(
        job_id: str,
        source_account_id: str,
        kind: str,
        progress_total: int | None,
    ) -> None:
        if _JOB_ID.fullmatch(job_id) is None or not source_account_id:
            raise JobValidationError("job identities are invalid")
        if kind not in _ALLOWED_KINDS:
            raise JobValidationError("job kind is unsupported")
        if progress_total is not None and progress_total < 0:
            raise JobValidationError("job progress total is invalid")

    def create_job(
        self,
        *,
        job_id: str,
        source_account_id: str,
        kind: str,
        progress_total: int | None = None,
        recoverable: bool = True,
        task_spec: JobTaskSpec | None = None,
    ) -> JobRecord:
        self._validate_create(job_id, source_account_id, kind, progress_total)
        serialized_task_spec = self._serialize_task_spec(task_spec)
        now = _now_ms()
        try:
            with self._write_transaction() as connection:
                connection.execute(
                    """
                    INSERT INTO jobs(
                        id, source_account_id, kind, status, stage,
                        progress_current, progress_total, stage_progress_current,
                        stage_progress_total, stage_progress_unit, stage_started_at,
                        checkpoint_json, task_spec_json,
                        recoverable, pause_requested, cancel_requested,
                        error_code, details_ref, created_at, started_at,
                        finished_at, updated_at
                    ) VALUES (?, ?, ?, ?, NULL, 0, ?, 0, NULL, NULL, NULL,
                              NULL, ?, ?, 0, 0,
                              NULL, NULL, ?, NULL, NULL, ?)
                    """,
                    (
                        job_id,
                        source_account_id,
                        kind,
                        JobStatus.QUEUED.value,
                        progress_total,
                        serialized_task_spec,
                        int(recoverable),
                        now,
                        now,
                    ),
                )
                job = self._job(self._load_for_update(connection, job_id))
                self._append_event(connection, job, "created")
        except sqlite3.IntegrityError as exc:
            # 只有主键冲突才是「这个 id 已经有了」。别的约束——过时的 CHECK、
            # 后加的 NOT NULL——一律报成主键冲突，会把排查引到请求体上：真机
            # 2026-09-02 就是这样，一个全新的随机 job_id 被告知「已存在」，
            # 而真因是 `kind` 上一条谁也改不动的旧 CHECK 不认识 `reclaim`。
            if "jobs.id" not in str(exc):
                raise JobValidationError(f"job row was refused by the database: {exc}") from exc
            raise JobStateError("job identity already exists") from exc
        self._notify_events()
        return job

    @staticmethod
    def _serialize_task_spec(task_spec: JobTaskSpec | None) -> str | None:
        if task_spec is None:
            return None
        normalized: dict[str, JobTaskSpecValue] = {}
        for key, value in task_spec.items():
            if not key:
                raise JobValidationError("task specification is invalid")
            if isinstance(value, list):
                normalized[key] = list(value)
                continue
            normalized[key] = value
        return json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def purge_export_jobs(
        self, account_id: str, export_id: str, cleanup: Callable[[], None]
    ) -> ExportJobsRemoved:
        """Lock exact export identities; no time-based inference for legacy tasks."""
        with self._write_transaction() as connection:
            rows = connection.execute(
                """SELECT id,status FROM jobs WHERE source_account_id=? AND kind='export'
                AND CASE WHEN json_valid(task_spec_json)
                    THEN json_extract(task_spec_json,'$.export_id') END=?""",
                (account_id, export_id),
            ).fetchall()
            if any(JobStatus(str(row["status"])) not in _TERMINAL for row in rows):
                raise JobStateError("export task is not terminal")
            legacy = bool(connection.execute(
                """SELECT EXISTS(SELECT 1 FROM jobs WHERE source_account_id=? AND kind='export'
                AND CASE WHEN json_valid(task_spec_json)
                    THEN json_extract(task_spec_json,'$.export_id') END IS NULL)""",
                (account_id,),
            ).fetchone()[0])
            cleanup()
            connection.execute("PRAGMA secure_delete=ON")
            ids = tuple(str(row["id"]) for row in rows)
            connection.executemany("DELETE FROM jobs WHERE id=?", ((identifier,) for identifier in ids))
        self._notify_events()
        return ExportJobsRemoved(ids, legacy)

    def get_job(self, job_id: str) -> JobRecord:
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            raise JobNotFoundError("job was not found")
        return self._job(row)

    def list_jobs(self, source_account_id: str) -> tuple[JobRecord, ...]:
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs WHERE source_account_id = ?
                ORDER BY created_at DESC, id
                """,
                (source_account_id,),
            ).fetchall()
        return tuple(self._job(row) for row in rows)

    def active_job_of_kind(self, source_account_id: str, kind: str) -> JobRecord | None:
        """这个账号这种作业里正占着线程的那一个（PSU-2.106）。

        `queued` / `running` / `pausing`——与 `has_running_jobs` 同一套判据，
        理由见端口注释。`ORDER BY created_at, id` 而不是随便取一行：这个答案会被
        原样交给界面，同一份库问两次必须答同一个作业。
        """
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE source_account_id = ? AND kind = ? AND {_status_in(_HOLDS_A_WORKER)}
                ORDER BY created_at, id
                LIMIT 1
                """,  # noqa: S608 - 拼进去的只有占位符，见 `_status_in`
                (source_account_id, kind, *_HOLDS_A_WORKER),
            ).fetchone()
        return None if row is None else self._job(row)

    def recent_import_for_scope(self, account_id: str, conversation_id: str | None, finished_after: int) -> JobRecord | None:
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            rows = connection.execute(
                "SELECT * FROM jobs WHERE source_account_id = ? AND status = 'completed' AND kind = 'import' AND finished_at >= ? ORDER BY updated_at DESC, id DESC LIMIT 16",
                (account_id, finished_after),
            ).fetchall()
        for row in rows:
            job = self._job(row)
            spec = job.task_spec
            if spec is None or spec.get("date_from") is not None or spec.get("date_to") is not None:
                continue
            selected = spec.get("conversation_ids")
            if not isinstance(selected, list):
                continue
            if not selected or (conversation_id is not None and conversation_id in selected):
                return job
        return None

    def active_import_job(self) -> JobRecord | None:
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute(
                f"SELECT * FROM jobs WHERE kind = 'import' AND {_status_in(_HOLDS_A_WORKER)} ORDER BY created_at, id LIMIT 1",  # noqa: S608
                _HOLDS_A_WORKER,
            ).fetchone()
        return None if row is None else self._job(row)

    def cache_conflicting_import_job(self) -> JobRecord | None:
        """挡着缓存维护的那个导入作业。`paused` / `interrupted` 也算——见端口注释。

        `ORDER BY created_at, id` 而不是 `LIMIT 1` 取任意一行：这个答案会被原样
        显示在界面上，同一份库问两次必须答同一个作业。`id` 只是同毫秒时的定序，
        判据是时间。
        """
        self._require_open()
        # 比另外三个多的正是那两档——写成加法，差别就看得见。
        statuses = _HOLDS_A_WORKER + _RESUMABLE
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute(
                f"""
                SELECT * FROM jobs
                WHERE kind = 'import' AND {_status_in(statuses)}
                ORDER BY created_at, id
                LIMIT 1
                """,  # noqa: S608 - 见 `_status_in`
                statuses,
            ).fetchone()
        return None if row is None else self._job(row)

    def has_active_reclaim_jobs(self) -> bool:
        """排队或在跑的回收作业。挡的是「点两次回收按钮排两个 VACUUM」；
        paused/interrupted 不算——回收不可暂停，落进那两档就是死作业。"""
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute(
                f"""
                SELECT 1 FROM jobs
                WHERE kind = 'reclaim' AND {_status_in(_HOLDS_A_WORKER)}
                LIMIT 1
                """,  # noqa: S608 - 见 `_status_in`
                _HOLDS_A_WORKER,
            ).fetchone()
        return row is not None

    def has_running_jobs(self) -> bool:
        """现在关掉进程会打断的任务。桌面端关窗确认用的就是这个判据。

        与 `cache_conflicting_import_job` 只差两个状态，但差的正是重点：
        `paused` 与 `interrupted` 是可恢复的持久状态，关掉再开什么都不损失。
        把它们也算成「在跑」，确认框就会天天弹——而一个天天弹的确认框等于
        没有确认框。种类不筛：同步、导出与导入一样会被关窗打断。
        """
        self._require_open()
        with closing(self._connect(read_only=True)) as connection:
            row = connection.execute(
                f"SELECT 1 FROM jobs WHERE {_status_in(_HOLDS_A_WORKER)} LIMIT 1",  # noqa: S608
                _HOLDS_A_WORKER,
            ).fetchone()
        return row is not None

    def _transition(
        self,
        job_id: str,
        *,
        allowed: frozenset[JobStatus],
        target: JobStatus,
        event_type: str,
        extra_values: Mapping[str, object] | None = None,
    ) -> JobRecord:
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status not in allowed:
                raise JobStateError("job transition is not allowed")
            values: dict[str, object] = {"status": target.value, "updated_at": now}
            if extra_values:
                values.update(extra_values)
            assignments = ", ".join(f"{column} = ?" for column in values)
            connection.execute(
                f"UPDATE jobs SET {assignments} WHERE id = ?",
                (*values.values(), job_id),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, event_type)
        self._notify_events()
        return job

    def start_job(self, job_id: str) -> JobRecord:
        return self._transition(
            job_id,
            allowed=frozenset({JobStatus.QUEUED}),
            target=JobStatus.RUNNING,
            event_type="started",
            extra_values={"started_at": _now_ms(), "finished_at": None},
        )

    def request_pause(self, job_id: str) -> JobRecord:
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status is JobStatus.PAUSING and current.pause_requested:
                return current
            if current.status is not JobStatus.RUNNING:
                raise JobStateError("job cannot pause from its current state")
            connection.execute(
                """
                UPDATE jobs SET status = ?, pause_requested = 1, updated_at = ?
                WHERE id = ?
                """,
                (JobStatus.PAUSING.value, now, job_id),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, "pause_requested")
        self._notify_events()
        return job

    def request_cancel(self, job_id: str) -> JobRecord:
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status is JobStatus.CANCELLED or current.cancel_requested:
                return current
            if current.status in {JobStatus.QUEUED, JobStatus.PAUSED}:
                target = JobStatus.CANCELLED
                event_type = "cancelled"
                cancel_requested = 0
                finished_at: int | None = now
            elif current.status in {JobStatus.RUNNING, JobStatus.PAUSING}:
                target = current.status
                event_type = "cancel_requested"
                cancel_requested = 1
                finished_at = None
            else:
                raise JobStateError("job cannot be cancelled from its current state")
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, cancel_requested = ?, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (target.value, cancel_requested, finished_at, now, job_id),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, event_type)
        self._notify_events()
        return job

    @staticmethod
    def _validate_stage_progress(
        *,
        stage: str,
        progress_current: int,
        progress_total: int | None,
        progress_unit: ProgressUnit | None,
    ) -> None:
        if not stage or progress_current < 0:
            raise JobValidationError("stage progress is invalid")
        if progress_total is not None and (
            progress_total < 0 or progress_current > progress_total
        ):
            raise JobValidationError("stage progress is outside the valid range")
        if progress_unit not in {None, "bytes", "items"}:
            raise JobValidationError("stage progress unit is invalid")
        if progress_total is not None and progress_unit is None:
            raise JobValidationError("stage progress unit is required for a known total")

    def report_stage_progress(
        self,
        job_id: str,
        *,
        stage: str,
        progress_current: int,
        progress_total: int | None,
        progress_unit: ProgressUnit | None,
    ) -> JobRecord:
        self._validate_stage_progress(
            stage=stage,
            progress_current=progress_current,
            progress_total=progress_total,
            progress_unit=progress_unit,
        )
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status not in {JobStatus.RUNNING, JobStatus.PAUSING}:
                raise JobStateError("job is not running while reporting progress")
            same_phase = (
                current.stage == stage
                and current.stage_progress_unit == progress_unit
            )
            if same_phase and progress_current < current.stage_progress_current:
                raise JobValidationError("stage progress cannot move backwards")
            if (
                same_phase
                and current.stage_progress_total is not None
                and progress_total != current.stage_progress_total
            ):
                raise JobValidationError("stage progress total cannot change")
            stage_started_at = (
                current.stage_started_at
                if same_phase and current.stage_started_at is not None
                else now
            )
            connection.execute(
                """
                UPDATE jobs
                SET stage = ?, stage_progress_current = ?,
                    stage_progress_total = ?, stage_progress_unit = ?,
                    stage_started_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    stage,
                    progress_current,
                    progress_total,
                    progress_unit,
                    stage_started_at,
                    now,
                    job_id,
                ),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, "progress")
        self._notify_events()
        return job

    def heartbeat(self, job_id: str, *, stage: str) -> JobRecord:
        if not stage:
            raise JobValidationError("heartbeat stage is invalid")
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status not in {JobStatus.RUNNING, JobStatus.PAUSING}:
                raise JobStateError("job is not running while reporting a heartbeat")
            if current.stage != stage:
                raise JobStateError("heartbeat stage does not match the current stage")
            connection.execute(
                "UPDATE jobs SET updated_at = ? WHERE id = ?",
                (now, job_id),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, "heartbeat")
        self._notify_events()
        return job

    @staticmethod
    def _serialize_checkpoint(checkpoint: Checkpoint) -> str:
        if not checkpoint or not set(checkpoint).issubset(_CHECKPOINT_KEYS):
            raise JobValidationError("checkpoint shape is invalid")
        normalized: dict[str, CheckpointValue] = {}
        for key, value in checkpoint.items():
            normalized[key] = value
        return json.dumps(
            normalized,
            ensure_ascii=True,
            sort_keys=True,
            separators=(",", ":"),
        )

    def checkpoint(
        self,
        job_id: str,
        *,
        stage: str,
        progress_current: int,
        checkpoint: Checkpoint,
        stage_progress_current: int | None = None,
        stage_progress_total: int | None = None,
        stage_progress_unit: ProgressUnit | None = None,
    ) -> ControlPointResult:
        if not stage or progress_current < 0:
            raise JobValidationError("checkpoint progress is invalid")
        if stage_progress_current is not None:
            self._validate_stage_progress(
                stage=stage,
                progress_current=stage_progress_current,
                progress_total=stage_progress_total,
                progress_unit=stage_progress_unit,
            )
        serialized = self._serialize_checkpoint(checkpoint)
        now = _now_ms()
        with self._write_transaction() as connection:
            current = self._job(self._load_for_update(connection, job_id))
            if current.status not in {JobStatus.RUNNING, JobStatus.PAUSING}:
                raise JobStateError("job is not running at a control point")
            if progress_current < current.progress_current or (
                current.progress_total is not None
                and progress_current > current.progress_total
            ):
                raise JobValidationError("checkpoint progress is outside the valid range")
            same_stage = current.stage == stage
            if stage_progress_current is None:
                next_stage_progress_current = (
                    current.stage_progress_current if same_stage else 0
                )
                next_stage_progress_total = current.stage_progress_total if same_stage else None
                next_stage_progress_unit = current.stage_progress_unit if same_stage else None
            else:
                next_stage_progress_current = stage_progress_current
                next_stage_progress_total = stage_progress_total
                next_stage_progress_unit = stage_progress_unit
                if same_stage and next_stage_progress_current < current.stage_progress_current:
                    raise JobValidationError("stage progress cannot move backwards")
            next_stage_started_at = (
                current.stage_started_at
                if same_stage and current.stage_started_at is not None
                else now
            )
            if current.cancel_requested:
                target = JobStatus.CANCELLED
                action = "cancel"
                event_type = "cancelled"
                finished_at: int | None = now
            elif current.pause_requested:
                target = JobStatus.PAUSED
                action = "pause"
                event_type = "paused"
                finished_at = None
            else:
                target = JobStatus.RUNNING
                action = "continue"
                event_type = "progress"
                finished_at = None
            connection.execute(
                """
                UPDATE jobs
                SET status = ?, stage = ?, progress_current = ?,
                    stage_progress_current = ?, stage_progress_total = ?,
                    stage_progress_unit = ?, stage_started_at = ?,
                    checkpoint_json = ?, pause_requested = 0,
                    cancel_requested = 0, finished_at = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    target.value,
                    stage,
                    progress_current,
                    next_stage_progress_current,
                    next_stage_progress_total,
                    next_stage_progress_unit,
                    next_stage_started_at,
                    serialized,
                    finished_at,
                    now,
                    job_id,
                ),
            )
            job = self._job(self._load_for_update(connection, job_id))
            self._append_event(connection, job, event_type)
        self._notify_events()
        return ControlPointResult(job, action)

    def resume_job(self, job_id: str) -> JobRecord:
        return self._transition(
            job_id,
            allowed=frozenset({JobStatus.PAUSED}),
            target=JobStatus.QUEUED,
            event_type="resumed",
            extra_values={
                "pause_requested": 0,
                "cancel_requested": 0,
                "finished_at": None,
            },
        )

    def complete_job(self, job_id: str) -> JobRecord:
        current = self.get_job(job_id)
        if current.pause_requested or current.cancel_requested:
            raise JobStateError("pending job command must be handled at a checkpoint")
        return self._transition(
            job_id,
            allowed=frozenset({JobStatus.RUNNING}),
            target=JobStatus.COMPLETED,
            event_type="completed",
            extra_values={"finished_at": _now_ms(), "recoverable": 0},
        )

    def interrupt_job(self, job_id: str, *, error_code: str | None = None) -> JobRecord:
        """跑到一半停下，状态可续（PSU-2.98）。

        与 `complete_job` 的区别只在目标状态与事件名；与 `fail_job` 的区别是
        `interrupted` **不是终态**，所以既不写 `finished_at` 也不清 `recoverable`。
        允许的来源状态与 `recover_after_restart` 那条路一致地只认 `running`：
        暂停/取消有它们自己的出口（`_Stopped`），从那里再转成 interrupted 会把
        用户按下的那个按钮说成一次远端故障。

        带上 `error_code` 也**不改这个状态的性质**（PSU-2.113）：仍然不是终态，
        仍然不写 `finished_at`、不清 `recoverable`。它只是让作业行自己说得出
        「为什么停」——界面读的就是这一列。
        """
        # 代号的形状与 `fail_job` 同一条规矩——它最终落在同一列上。
        if error_code is not None and _ERROR_CODE.fullmatch(error_code) is None:
            raise JobValidationError("job failure references are invalid")
        return self._transition(
            job_id,
            allowed=frozenset({JobStatus.RUNNING}),
            target=JobStatus.INTERRUPTED,
            event_type="interrupted",
            # 没有代号就一列都不写：不是写 None 把上一次的抹掉，也不是
            # 编一个出来。重启恢复那条路走的正是这一支。
            extra_values=None if error_code is None else {"error_code": error_code},
        )

    def fail_job(
        self,
        job_id: str,
        *,
        error_code: str,
        details_ref: str | None = None,
    ) -> JobRecord:
        if _ERROR_CODE.fullmatch(error_code) is None or (
            details_ref is not None and _DETAILS_REF.fullmatch(details_ref) is None
        ):
            raise JobValidationError("job failure references are invalid")
        current = self.get_job(job_id)
        if current.status in _TERMINAL:
            raise JobStateError("terminal job state is irreversible")
        return self._transition(
            job_id,
            allowed=frozenset(
                {
                    JobStatus.QUEUED,
                    JobStatus.RUNNING,
                    JobStatus.PAUSING,
                    JobStatus.PAUSED,
                    JobStatus.INTERRUPTED,
                }
            ),
            target=JobStatus.FAILED,
            event_type="failed",
            extra_values={
                "error_code": error_code,
                "details_ref": details_ref,
                "finished_at": _now_ms(),
            },
        )

    def recover_after_restart(
        self,
        checkpoint_validator: Callable[[JobRecord], bool],
    ) -> tuple[JobRecord, ...]:
        self._require_open()
        interrupted: list[JobRecord] = []
        now = _now_ms()
        with self._write_transaction() as connection:
            rows = connection.execute(
                """
                SELECT * FROM jobs
                WHERE status IN (?, ?, ?)
                ORDER BY id
                """,
                (
                    JobStatus.RUNNING.value,
                    JobStatus.PAUSING.value,
                    JobStatus.INTERRUPTED.value,
                ),
            ).fetchall()
            for row in rows:
                current = self._job(row)
                if current.status is not JobStatus.INTERRUPTED:
                    connection.execute(
                        """
                        UPDATE jobs SET status = ?, pause_requested = 0,
                                        cancel_requested = 0, updated_at = ?
                        WHERE id = ?
                        """,
                        (JobStatus.INTERRUPTED.value, now, current.id),
                    )
                    current = self._job(self._load_for_update(connection, current.id))
                    self._append_event(connection, current, "interrupted")
                interrupted.append(current)
        if interrupted:
            self._notify_events()

        recovered: list[JobRecord] = []
        for job in interrupted:
            valid = job.recoverable and checkpoint_validator(job)
            if valid:
                recovered.append(
                    self._transition(
                        job.id,
                        allowed=frozenset({JobStatus.INTERRUPTED}),
                        target=JobStatus.PAUSED,
                        event_type="recovered",
                    )
                )
            else:
                recovered.append(
                    self.fail_job(job.id, error_code="CHECKPOINT_INVALID")
                )
        return tuple(recovered)

    def list_events(
        self,
        source_account_id: str,
        *,
        after_sequence: int,
        limit: int,
        job_id: str | None = None,
    ) -> tuple[JobEvent, ...]:
        """Events for one account, narrowed to a single job when asked.

        A reader following one job must narrow here rather than filter the
        result: the limit is applied by the query, so an account-wide window
        can be filled entirely by other jobs' events and hand back a page that
        looks empty while never advancing the caller's cursor past them.
        """
        self._require_open()
        if not source_account_id or after_sequence < 0 or limit <= 0 or limit > 500:
            raise JobValidationError("event cursor or account scope is invalid")
        scope = "" if job_id is None else " AND job_id = ?"
        parameters: tuple[object, ...] = (
            (source_account_id, after_sequence, limit)
            if job_id is None
            else (source_account_id, job_id, after_sequence, limit)
        )
        with closing(self._connect(read_only=True)) as connection:
            rows = connection.execute(
                f"""
                SELECT * FROM job_events
                WHERE source_account_id = ?{scope} AND sequence > ?
                ORDER BY sequence LIMIT ?
                """,
                parameters,
            ).fetchall()
        return tuple(self._event(row) for row in rows)

    def wait_events(
        self,
        source_account_id: str,
        *,
        after_sequence: int,
        timeout_seconds: float,
        limit: int,
        job_id: str | None = None,
    ) -> tuple[JobEvent, ...]:
        if timeout_seconds < 0 or timeout_seconds > 60:
            raise JobValidationError("event wait timeout is invalid")
        deadline = time.monotonic() + timeout_seconds
        with self._event_condition:
            while True:
                events = self.list_events(
                    source_account_id,
                    after_sequence=after_sequence,
                    limit=limit,
                    job_id=job_id,
                )
                if events or timeout_seconds == 0:
                    return events
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return ()
                # One condition serves every waiter, so another account's (or
                # another job's) event wakes this one too. Keep waiting out the
                # caller's timeout instead of answering empty, which would turn
                # a long poll into an immediate re-request.
                self._event_condition.wait(remaining)
                self._require_open()

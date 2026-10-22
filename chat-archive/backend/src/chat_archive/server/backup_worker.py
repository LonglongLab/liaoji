"""到点就备一次的后台线程（PSU-2.140）。

起停的壳照抄 `SearchBackfillWorker` / `ConversationMergeWorker`——那一套在
lifespan 里跑了几个版本，没有理由换一个形状。

**为什么在进程内而不是宿主上的 systemd timer。** 隔离性确实是后者更好，但那等于
多一个你要记得维护的东西，而"忘了"正是这个功能要防的那件事。跑在进程内，失败直接
进健康页，设置页也能点"立即备份"。

它握不到、也不需要那把全局写锁：快照是**读**（`Connection.backup()` 在只读连接上
拷页），不与同步的写入口争锁。

到点判据读的是账本（`backup_runs` 的最近一次成功），**重启不丢**。错过的窗口——
比如服务器当时关着——在启动后 `STARTUP_GRACE_SECONDS` 补跑一次，而不是傻等到
下一个整周期。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path

from chat_archive.server.backup_archive import (
    BackupArchiveError,
    BackupRunResult,
    lifecycle_covers,
    run_backup,
)
from chat_archive.server.backup_config import (
    BackupAlreadyRunningError,
    RUN_FAILED,
    RUN_SUCCESS,
    BackupConfigurationError,
    BackupCredentials,
    SqliteBackupConfigStore,
)
from chat_archive.server.backup_snapshot import (
    BackupInsufficientDiskError,
    BackupSnapshotError,
)
from chat_archive.server.backup_store import BackupStore, BackupStoreError

_logger = logging.getLogger(__name__)

# 进程起来之后先缓一会儿再考虑补跑：让 API 先把启动路径走完。
STARTUP_GRACE_SECONDS = 300.0
# 没事干的时候多久醒一次看看到点没有。
DEFAULT_IDLE_SECONDS = 300.0

FAILURE_INSUFFICIENT_DISK = "BACKUP_INSUFFICIENT_DISK"
FAILURE_SNAPSHOT = "BACKUP_SNAPSHOT_FAILED"
FAILURE_UPLOAD = "BACKUP_UPLOAD_FAILED"
FAILURE_ARCHIVE = "BACKUP_ARCHIVE_FAILED"
FAILURE_CONFIGURATION = "BACKUP_CONFIGURATION_INVALID"
# 五个自家异常之外的任何东西——磁盘满时 `_compress` 的 OSError、权限、总线错。
# 没有这一条，账本上那一行会永远停在 `running`，直到下次进程重启。
FAILURE_UNEXPECTED = "BACKUP_UNEXPECTED_FAILURE"

# 失败后的退避：首次 15 分钟，每连败一次翻倍，封顶到一个周期。
#
# 没有它，`due()` 只看上次成功时间，失败不改变判据，每个 idle tick（5 分钟）都会
# 再来一轮——凭据配错时，那台 2 核机每 5 分钟拷 1.1 GB、跑一遍 integrity_check、
# 压 400 MB，然后再失败一次。「立即备份」是显式请求，不受退避约束。
FAILURE_BACKOFF_BASE_SECONDS = 15 * 60


def failure_code_for(error: BaseException) -> str:
    """一次失败记进账本的稳定代号。CLI 与 worker 用的是**同一张**表。"""
    if isinstance(error, BackupInsufficientDiskError):
        return FAILURE_INSUFFICIENT_DISK
    if isinstance(error, BackupSnapshotError):
        return FAILURE_SNAPSHOT
    if isinstance(error, BackupStoreError):
        return FAILURE_UPLOAD
    if isinstance(error, BackupArchiveError):
        return FAILURE_ARCHIVE
    if isinstance(error, BackupConfigurationError):
        return FAILURE_CONFIGURATION
    return FAILURE_UNEXPECTED


class BackupWorker:
    """一条到点备份、失败不死、随时可叫醒的后台线程。"""

    def __init__(
        self,
        *,
        data_dir: Path,
        config_store: SqliteBackupConfigStore,
        store_factory: Callable[[BackupCredentials], BackupStore],
        idle_seconds: float = DEFAULT_IDLE_SECONDS,
        startup_grace_seconds: float = STARTUP_GRACE_SECONDS,
        clock: Callable[[], datetime] | None = None,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if idle_seconds <= 0:
            raise ValueError("idle seconds must be positive")
        self._data_dir = data_dir
        self._config_store = config_store
        self._store_factory = store_factory
        self._idle_seconds = idle_seconds
        self._startup_grace = startup_grace_seconds
        self._clock: Callable[[], datetime] = (
            (lambda: datetime.now(timezone.utc)) if clock is None else clock
        )
        self._monotonic = monotonic
        self._started_at = monotonic()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread: threading.Thread | None = None

    def poke(self) -> None:
        """有人要求现在就备一次。线程安全，可从请求路径调。"""
        self._wake.set()

    def start(self) -> None:
        if self._thread is not None:
            return
        self._started_at = self._monotonic()
        self._thread = threading.Thread(
            target=self._run, name="chat-archive-backup", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def due(self) -> bool:
        """到点了没有。没配置就永远不到点——那不是失败，是还没开始。

        到点之后还要过退避这一关：上一轮失败了就等一会儿再试，连败越多等越久。
        """
        policy = self._config_store.policy()
        if policy is None:
            return False
        interval = policy.interval_hours * 3600
        now = int(self._clock().timestamp())
        last = self._config_store.last_success()
        if last is None or last.finished_at is None:
            # 从没成功过：等启动宽限期过去就跑第一次。
            if self._monotonic() - self._started_at < self._startup_grace:
                return False
        elif now - last.finished_at < interval:
            return False
        streak = self._config_store.failure_streak()
        if streak.count == 0 or streak.last_finished_at is None:
            return True
        backoff = min(interval, FAILURE_BACKOFF_BASE_SECONDS * 2 ** (streak.count - 1))
        return now - streak.last_finished_at >= backoff

    def run_once(self, *, force: bool = False) -> BackupRunResult | None:
        """跑一轮。没配置返回 `None`；失败**不抛**，记进账本。"""
        try:
            credentials = self._config_store.load_credentials()
        except BackupConfigurationError:
            _logger.warning("backup configuration cannot be read; skipping this round")
            return None
        if credentials is None:
            return None
        if not force and not self.due():
            return None
        policy = self._config_store.policy()
        if policy is None:
            return None
        try:
            run_id = self._config_store.begin_run()
        except BackupAlreadyRunningError:
            # 运维正在手跑一趟（`backup now` 在另一个进程里）。这不是失败，
            # 也不该记账——下一轮再看（PSU-2.142）。
            _logger.info("another backup is already running; skipping this round")
            return None
        try:
            store = self._store_factory(credentials)
            result = run_backup(
                data_dir=self._data_dir,
                staging_root=self._data_dir,
                store=store,
                prefix=credentials.prefix,
                policy=policy,
                moment=self._clock(),
            )
        except (
            BackupSnapshotError,
            BackupStoreError,
            BackupArchiveError,
            BackupConfigurationError,
        ) as error:
            self._fail(run_id, failure_code_for(error))
            return None
        except Exception as error:  # noqa: BLE001 - 账本那一行必须收口
            # 与自家异常同一条处置：记稳定代号、不抛。区别只在这里留一份堆栈，
            # 因为它是没料到的那种失败。
            _logger.exception("backup run failed unexpectedly")
            self._fail(run_id, failure_code_for(error))
            return None
        self._config_store.finish_run(
            run_id,
            status=RUN_SUCCESS,
            backup_id=result.backup_id,
            source_bytes=result.source_bytes,
            stored_bytes=result.stored_bytes,
            verified=True,
            lifecycle_covered=lifecycle_covers(
                result.retention.lifecycle_prefixes, credentials.prefix
            ),
        )
        return result

    def _fail(self, run_id: int, code: str) -> None:
        # 账本上只留稳定代号。provider 原话与路径不进日志也不进库。
        _logger.warning("backup run failed with %s", code)
        self._config_store.finish_run(run_id, status=RUN_FAILED, failure_code=code)

    def _run(self) -> None:
        while not self._stop.is_set():
            forced = self._wake.is_set()
            self._wake.clear()
            try:
                self.run_once(force=forced)
            except Exception:  # noqa: BLE001 - 一条后台线程不许被任何东西杀掉
                _logger.exception("backup worker round failed unexpectedly")
            if self._stop.is_set():
                return
            self._wake.wait(self._idle_seconds)


__all__ = [
    "DEFAULT_IDLE_SECONDS",
    "FAILURE_ARCHIVE",
    "FAILURE_BACKOFF_BASE_SECONDS",
    "FAILURE_CONFIGURATION",
    "FAILURE_INSUFFICIENT_DISK",
    "FAILURE_SNAPSHOT",
    "FAILURE_UNEXPECTED",
    "FAILURE_UPLOAD",
    "STARTUP_GRACE_SECONDS",
    "BackupWorker",
    "failure_code_for",
]

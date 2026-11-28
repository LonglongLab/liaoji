"""到点清掉该删的分享（PSU-2.180）。起停的壳照抄 `BackupWorker`。

删的顺序是**先对象、后行**：行一删，这些对象就再没有人记得它们了——对象存储里
会留下一批没人管、也永远不会被删的副本，而分享功能对用户的承诺正是「删了就没了」。
所以任何一个对象删不掉，这一行就原样留着，下一轮再来。

日志只有条数与耗时：令牌、标题、对象键一个都不写。
"""

from __future__ import annotations

import logging
import os
import stat
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from chat_archive.ports.objectstore import ObjectNotFoundError, ObjectStore
from chat_archive.server.shares import SHARE_OBJECT_SCOPE, Share, SqliteShareStore

_logger = logging.getLogger(__name__)

DEFAULT_INTERVAL_SECONDS = 3600.0
SHARE_UPLOAD_STAGING_DIRNAME = ".share-uploads"
STALE_UPLOAD_STAGING_SECONDS = 7200


def cleanup_share_upload_staging(
    staging_dir: Path,
    *,
    max_age_seconds: int | None = None,
    now: Callable[[], float] | None = None,
) -> int:
    """Remove leftover ``upload-*.tmp`` staging files without following symlinks."""
    if not staging_dir.exists() or staging_dir.is_symlink() or not staging_dir.is_dir():
        return 0
    current = (time.time() if now is None else now()) if max_age_seconds is not None else 0.0
    removed = 0
    try:
        entries = list(staging_dir.iterdir())
    except OSError:
        return 0
    for entry in entries:
        name = entry.name
        if not (name.startswith("upload-") and name.endswith(".tmp")):
            continue
        try:
            if entry.is_symlink():
                continue
            stat_result = os.lstat(entry)
            if not stat.S_ISREG(stat_result.st_mode):
                continue
            if max_age_seconds is not None and current - stat_result.st_mtime < max_age_seconds:
                continue
            entry.unlink(missing_ok=True)
            removed += 1
        except OSError:
            continue
    if removed:
        _logger.info("cleaned %d orphaned share staging file(s)", removed)
    return removed


@dataclass(frozen=True)
class SweepResult:
    removed: int
    kept: int


def remove_share(store: SqliteShareStore, objects: ObjectStore, share: Share) -> bool:
    """删一份分享：先删它的每个对象，全删掉了才删行。删成了返回 True。

    已经不在的对象算删掉了——上传中断的草稿，对象可能根本没落地。
    """
    for item in share.objects:
        try:
            objects.delete_object(SHARE_OBJECT_SCOPE, item.object_key)
        except ObjectNotFoundError:
            continue
        except Exception:
            return False
    store.delete(share.token)
    return True


class ShareSweeper:
    """一条每小时醒一次、失败不死的后台线程。"""

    def __init__(
        self,
        *,
        store: SqliteShareStore,
        objects: ObjectStore,
        interval_seconds: float = DEFAULT_INTERVAL_SECONDS,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("interval seconds must be positive")
        self._store = store
        self._objects = objects
        self._interval = interval_seconds
        self._monotonic = monotonic
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="chat-archive-share-sweeper", daemon=True
        )
        self._thread.start()

    def stop(self, *, timeout: float = 5.0) -> None:
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None:
            thread.join(timeout=timeout)

    def run_once(self) -> SweepResult:
        started = self._monotonic()
        removed = kept = 0
        for share in self._store.sweep_candidates():
            if remove_share(self._store, self._objects, share):
                removed += 1
            else:
                kept += 1
        staging_dir = self._store.database_path.parent / SHARE_UPLOAD_STAGING_DIRNAME
        cleanup_share_upload_staging(
            staging_dir, max_age_seconds=STALE_UPLOAD_STAGING_SECONDS
        )
        if removed or kept:
            _logger.info(
                "share sweep removed %d, kept %d for retry, took %.2f s",
                removed,
                kept,
                self._monotonic() - started,
            )
        return SweepResult(removed=removed, kept=kept)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                self.run_once()
            except Exception:
                _logger.exception("share sweep round failed unexpectedly")
            if self._stop.wait(self._interval):
                return


__all__ = [
    "DEFAULT_INTERVAL_SECONDS",
    "SHARE_UPLOAD_STAGING_DIRNAME",
    "STALE_UPLOAD_STAGING_SECONDS",
    "ShareSweeper",
    "SweepResult",
    "cleanup_share_upload_staging",
    "remove_share",
]

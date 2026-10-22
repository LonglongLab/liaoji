"""Delete only the server-owned derivatives of one authenticated export.

No object-store dependency: the export pipeline reads original objects and
publishes its ZIP to disk. Original object keys are never cleanup targets.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import stat
import threading
from collections.abc import Callable
from pathlib import Path
from typing import cast

from sqlalchemy.exc import SQLAlchemyError

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.export import ExportService
from chat_archive.ports.export import ExportHistoryRepository
from chat_archive.ports.jobs import JobRepository, JobStateError

_EXPORT_ID = re.compile(r"export_[A-Za-z0-9_-]{8,64}\Z")
_DELETE_LOCK = threading.Lock()


def _unsafe() -> ArchiveError:
    return ArchiveError(ErrorCode.EXPORT_DELETE_UNSAFE)


def _linked(path: Path) -> bool:
    info = path.lstat()
    return stat.S_ISLNK(info.st_mode) or bool(
        getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT
    )


def _check(path: Path, root: Path) -> None:
    """Check lexical containment, all parents, and junctions without following them."""
    absolute = path.absolute()
    anchor = root.absolute()
    if absolute == anchor or not absolute.is_relative_to(anchor):
        raise _unsafe()
    current = absolute
    while True:
        try:
            if _linked(current):
                raise _unsafe()
        except FileNotFoundError:
            pass
        if current == anchor:
            break
        current = current.parent
    if not absolute.resolve().is_relative_to(anchor.resolve()):
        raise _unsafe()


def _check_tree(path: Path, root: Path) -> None:
    _check(path, root)
    if not path.exists():
        return
    if path.is_dir():
        def fail(error: OSError) -> None:
            raise error

        for directory, dirs, files in os.walk(path, followlinks=False, onerror=fail):
            for name in (*dirs, *files):
                _check(Path(directory) / name, root)


def delete_export(
    service: ExportService,
    history: ExportHistoryRepository,
    jobs: JobRepository,
    export_id: str,
    account_id: str,
    *,
    transient_media_root: Path | None = None,
    forget_job: Callable[[str], None] | None = None,
) -> dict[str, bool]:
    # Both supported runtimes have one workspace process. Serialize the
    # complete cleanup, including history/marker finalization after the jobs
    # transaction; a second request must not recreate a just-removed marker.
    with _DELETE_LOCK:
        return _delete_export(service, history, jobs, export_id, account_id,
                              transient_media_root=transient_media_root, forget_job=forget_job)


def _delete_export(
    service: ExportService,
    history: ExportHistoryRepository,
    jobs: JobRepository,
    export_id: str,
    account_id: str,
    *,
    transient_media_root: Path | None,
    forget_job: Callable[[str], None] | None,
) -> dict[str, bool]:
    if _EXPORT_ID.fullmatch(export_id) is None:
        raise ArchiveError(ErrorCode.QUERY_INVALID)
    root = service.exports_root
    marker = root / ".delete" / f"{export_id}.json"
    # Ownership is checked before listing or deleting any artifact tree.
    try:
        record = history.get_export(account_id, export_id)
        _check(marker, root)
        pending = marker.exists()
        if pending:
            raw: object = json.loads(marker.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise _unsafe()
            value = cast(dict[str, object], raw)
            if set(value) != {"account_id"}:
                raise _unsafe()
            if value["account_id"] != account_id:
                raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)
        if record is None and not pending:
            raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)
        if record is not None:
            if record.status not in {"ready", "failed", "cancelled", "missing"}:
                raise ArchiveError(ErrorCode.EXPORT_DELETE_BUSY)
            if record.artifact_name not in {None, export_id}:
                raise _unsafe()

        snapshot = service.scope_snapshot_path(export_id)
        targets = [(service.final_dir(export_id), root), (root / ".tmp" / export_id, root)]
        targets.extend((Path(str(snapshot) + suffix), root) for suffix in ("", "-wal", "-shm", "-journal"))
        if transient_media_root is not None:
            # The server passes its dedicated sibling, never the original media_root.
            expected = root.parent / ".media"
            if transient_media_root.absolute() != expected.absolute():
                raise _unsafe()
            targets.append((expected / export_id, expected))
        for target, anchor in targets:
            _check_tree(target, anchor)

        def cleanup() -> None:
            # Terminal state and job identity are locked by the jobs repository
            # before revoking access or removing files. Any failure keeps this
            # minimal marker and the history so a retry has an authoritative ID.
            _check(marker, root)
            marker.parent.mkdir(parents=True, exist_ok=True)
            if not marker.exists():
                staging = marker.with_suffix(".pending")
                _check(staging, root)
                staging.unlink(missing_ok=True)
                with staging.open("x", encoding="utf-8") as stream:
                    json.dump({"account_id": account_id}, stream)
                    stream.flush()
                    os.fsync(stream.fileno())
                _check(marker, root)
                staging.replace(marker)
            for target, anchor in targets:
                _check_tree(target, anchor)
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink(missing_ok=True)
            for target, anchor in targets:
                _check(target, anchor)
                if target.exists():
                    raise OSError("export cleanup incomplete")

        removed = jobs.purge_export_jobs(account_id, export_id, cleanup)
        if forget_job is not None:
            for job_id in removed.job_ids:
                forget_job(job_id)
        _check(marker, root)
        marker.unlink()
        # Keep the owning history until even the retry marker is removed. If
        # either final step fails, reloading the page still exposes the record
        # that can retry; all artifacts are already absent and cannot download.
        history.delete_export_record(account_id, export_id)
        if history.get_export(account_id, export_id) is not None:
            raise OSError("export history cleanup incomplete")
        return {"deleted": True, "legacy_job_history_retained": removed.legacy_job_history_retained}
    except JobStateError:
        raise ArchiveError(ErrorCode.EXPORT_DELETE_BUSY) from None
    except (OSError, sqlite3.Error, SQLAlchemyError, ValueError):
        raise ArchiveError(ErrorCode.EXPORT_DELETE_FAILED, retryable=True) from None

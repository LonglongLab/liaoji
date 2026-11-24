"""Small durable history adapter for the shared export worker."""

from __future__ import annotations

import sqlite3
import time
from contextlib import closing
from dataclasses import asdict
from pathlib import Path

from chat_archive.adapters.sqlite.connection import open_sqlite
from chat_archive.ports.export import ExportArtifactRecord


class SQLiteExportHistory:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect(wal=True)) as connection, connection:
            connection.execute("""CREATE TABLE IF NOT EXISTS exports(
                id TEXT PRIMARY KEY,source_account_id TEXT NOT NULL,format TEXT NOT NULL,
                scope_json TEXT NOT NULL,status TEXT NOT NULL,artifact_name TEXT,
                record_count INTEGER,size_bytes INTEGER,error_code TEXT,
                created_at INTEGER NOT NULL,finished_at INTEGER)""")

    def _connect(self, *, wal: bool = False) -> sqlite3.Connection:
        return open_sqlite(self.path, wal=wal, guard_name="export-history")

    def record_export_started(self, record: ExportArtifactRecord) -> None:
        with closing(self._connect()) as connection, connection:
            values = asdict(record)
            connection.execute(
                f"INSERT INTO exports({','.join(values)}) VALUES({','.join('?' for _ in values)})",
                tuple(values.values()),
            )

    def finish_export(
        self,
        export_id: str,
        *,
        status: str,
        artifact_name: str | None = None,
        record_count: int | None = None,
        size_bytes: int | None = None,
        error_code: str | None = None,
    ) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE exports SET status=?,artifact_name=?,record_count=?,
              size_bytes=?,error_code=?,finished_at=? WHERE id=?""",
                (
                    status,
                    artifact_name,
                    record_count,
                    size_bytes,
                    error_code,
                    int(time.time()),
                    export_id,
                ),
            )

    def mark_export_missing(self, export_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("UPDATE exports SET status='missing' WHERE id=?", (export_id,))

    def export_history(self, source_account_id: str) -> tuple[ExportArtifactRecord, ...]:
        with closing(self._connect()) as connection:
            return tuple(
                ExportArtifactRecord(**dict(row))
                for row in connection.execute(
                    "SELECT * FROM exports WHERE source_account_id=? ORDER BY created_at DESC,id",
                    (source_account_id,),
                )
            )

    def get_export(self, source_account_id: str, export_id: str) -> ExportArtifactRecord | None:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM exports WHERE id=? AND source_account_id=?", (export_id, source_account_id)).fetchone()
            return ExportArtifactRecord(**dict(row)) if row is not None else None

    def delete_export_record(self, source_account_id: str, export_id: str) -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute("PRAGMA secure_delete=ON")
            connection.execute("DELETE FROM exports WHERE id=? AND source_account_id=? AND status!='running'", (export_id, source_account_id))

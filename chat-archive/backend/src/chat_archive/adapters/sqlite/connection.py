"""Shared SQLite connection factory for local and server stores."""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Literal

from chat_archive.adapters.sqlite.guards import (
    DEFAULT_STATEMENT_ABORT_SECONDS,
    StatementGuardConnection,
    install_statement_guard,
)


def sqlite_uri(database_path: Path, *, mode: Literal["ro", "rw", "rwc"]) -> str:
    """Return a resolved SQLite file URI with the requested open mode."""
    return f"{database_path.resolve(strict=False).as_uri()}?mode={mode}"


def open_sqlite(
    database_path: Path,
    *,
    read_only: bool = False,
    mode: Literal["ro", "rw", "rwc"] | None = None,
    busy_timeout_ms: int = 5_000,
    wal: bool = False,
    foreign_keys: bool = True,
    row_factory: type[sqlite3.Row] | None = sqlite3.Row,
    isolation_level: Literal["DEFERRED", "EXCLUSIVE", "IMMEDIATE"] | None = "DEFERRED",
    guard_name: str | None = None,
    statement_abort_seconds: float | None = DEFAULT_STATEMENT_ABORT_SECONDS,
) -> sqlite3.Connection:
    """Open a SQLite connection with unified URI resolution, PRAGMAs, and optional statement guard."""
    if busy_timeout_ms <= 0:
        raise ValueError("busy_timeout_ms must be positive")

    effective_mode = mode
    if read_only:
        if mode is not None and mode != "ro":
            raise ValueError("read_only=True conflicts with non-ro mode")
        effective_mode = "ro"

    if wal and effective_mode == "ro":
        raise ValueError("cannot enable WAL mode on a read-only SQLite connection")

    timeout = busy_timeout_ms / 1_000
    if effective_mode == "ro" or effective_mode == "rw":
        connect_target: str | Path = sqlite_uri(database_path, mode=effective_mode)
        use_uri = True
    else:
        connect_target = database_path
        use_uri = False

    if guard_name is not None:
        connection: sqlite3.Connection = sqlite3.connect(
            connect_target,
            timeout=timeout,
            uri=use_uri,
            isolation_level=isolation_level,
            factory=StatementGuardConnection,
        )
    else:
        connection = sqlite3.connect(
            connect_target,
            timeout=timeout,
            uri=use_uri,
            isolation_level=isolation_level,
        )

    try:
        if row_factory is not None:
            connection.row_factory = row_factory
        if foreign_keys:
            connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {busy_timeout_ms}")
        if wal:
            connection.execute("PRAGMA journal_mode = WAL")
        if guard_name is not None:
            install_statement_guard(
                connection,
                name=guard_name,
                abort_after=statement_abort_seconds,
            )
    except Exception:
        connection.close()
        raise
    return connection


__all__ = ["open_sqlite", "sqlite_uri"]

"""Exact, transactionally maintained search counts, derived from existing rows.

Creation/backfill belongs to writer or application initialization. Reads never
create structures and return None for older snapshots without a ready summary.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Sequence

_STATE = "search_summary_state"
_INDEX = "search_browse_counts"
_SOURCE = "search_source_counts"


def _table(connection: sqlite3.Connection, name: str) -> bool:
    return connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
    ).fetchone() is not None


def _ready(connection: sqlite3.Connection, kind: str, version: str) -> bool:
    owner, table = ("search_messages", _INDEX) if kind == "index" else ("messages", _SOURCE)
    if not all(_table(connection, name) for name in (owner, table, _STATE)):
        return False
    row = connection.execute(f"SELECT value FROM {_STATE} WHERE key=?", (kind,)).fetchone()
    if row is None or str(row[0]) != version:
        return False
    triggers = tuple(f"search_summary_{kind}_{event}" for event in ("insert", "delete", "update"))
    return len(connection.execute(
        "SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=? "
        "AND name IN (?, ?, ?)", (owner, *triggers),
    ).fetchall()) == 3


def _reset(connection: sqlite3.Connection, kind: str) -> None:
    for event in ("insert", "delete", "update"):
        connection.execute(f"DROP TRIGGER IF EXISTS search_summary_{kind}_{event}")
    connection.execute(f"DELETE FROM {_INDEX if kind == 'index' else _SOURCE}")


def _mark(connection: sqlite3.Connection, kind: str, version: str) -> None:
    connection.execute(
        f"INSERT INTO {_STATE}(key,value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (kind, version),
    )


def _ensure_index(connection: sqlite3.Connection) -> None:
    if not _table(connection, "search_messages") or _ready(connection, "index", "1"):
        return
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {_INDEX} (
            source_account_id TEXT NOT NULL, conversation_id TEXT NOT NULL,
            indexed_count INTEGER NOT NULL CHECK(indexed_count>=0),
            complete_count INTEGER NOT NULL CHECK(complete_count>=0 AND complete_count<=indexed_count),
            PRIMARY KEY(source_account_id, conversation_id)
        ) WITHOUT ROWID
    """)
    _reset(connection, "index")
    connection.execute(f"""
        INSERT INTO {_INDEX}
        SELECT source_account_id, conversation_id, COUNT(*), SUM(metadata_complete)
        FROM search_messages GROUP BY source_account_id, conversation_id
    """)
    add = f"""
        INSERT INTO {_INDEX} VALUES (NEW.source_account_id, NEW.conversation_id, 1, NEW.metadata_complete)
        ON CONFLICT(source_account_id, conversation_id) DO UPDATE SET
            indexed_count=indexed_count+1, complete_count=complete_count+NEW.metadata_complete;
    """
    remove = f"""
        UPDATE {_INDEX} SET indexed_count=indexed_count-1,
            complete_count=complete_count-OLD.metadata_complete
        WHERE source_account_id=OLD.source_account_id AND conversation_id=OLD.conversation_id;
        DELETE FROM {_INDEX} WHERE source_account_id=OLD.source_account_id
            AND conversation_id=OLD.conversation_id AND indexed_count=0;
    """
    connection.execute(f"CREATE TRIGGER search_summary_index_insert AFTER INSERT ON search_messages BEGIN {add} END")
    connection.execute(f"CREATE TRIGGER search_summary_index_delete AFTER DELETE ON search_messages BEGIN {remove} END")
    connection.execute(f"""
        CREATE TRIGGER search_summary_index_update
        AFTER UPDATE OF source_account_id, conversation_id, metadata_complete ON search_messages
        WHEN OLD.source_account_id IS NOT NEW.source_account_id
          OR OLD.conversation_id IS NOT NEW.conversation_id
          OR OLD.metadata_complete IS NOT NEW.metadata_complete
        BEGIN {remove} {add} END
    """)
    _mark(connection, "index", "1")


def _source_version(excluded: Collection[str]) -> str:
    return "1:" + json.dumps(sorted(set(excluded)), separators=(",", ":"))


def _ensure_source(connection: sqlite3.Connection, excluded: Collection[str]) -> None:
    if not _table(connection, "messages"):
        return
    version = _source_version(excluded)
    if _ready(connection, "source", version):
        return
    columns = {str(row[1]) for row in connection.execute("PRAGMA table_info(messages)")}
    if not {"source_account_id", "message_type"} <= columns:
        return
    connection.execute(f"""
        CREATE TABLE IF NOT EXISTS {_SOURCE} (
            source_account_id TEXT PRIMARY KEY NOT NULL,
            searchable_count INTEGER NOT NULL CHECK(searchable_count>=0)
        ) WITHOUT ROWID
    """)
    _reset(connection, "source")
    literals = ",".join("'" + value.replace("'", "''") + "'" for value in sorted(set(excluded)))

    def predicate(prefix: str = "") -> str:
        return f"COALESCE({prefix}message_type NOT IN ({literals}), 0)" if literals else "1"

    connection.execute(f"""
        INSERT INTO {_SOURCE} SELECT source_account_id, COUNT(*) FROM messages
        WHERE {predicate()} GROUP BY source_account_id
    """)
    add = f"""
        INSERT INTO {_SOURCE} SELECT NEW.source_account_id, 1 WHERE {predicate('NEW.')}
        ON CONFLICT(source_account_id) DO UPDATE SET searchable_count=searchable_count+1;
    """
    remove = f"""
        UPDATE {_SOURCE} SET searchable_count=searchable_count-1
        WHERE source_account_id=OLD.source_account_id AND {predicate('OLD.')};
        DELETE FROM {_SOURCE} WHERE source_account_id=OLD.source_account_id AND searchable_count=0;
    """
    connection.execute(f"CREATE TRIGGER search_summary_source_insert AFTER INSERT ON messages BEGIN {add} END")
    connection.execute(f"CREATE TRIGGER search_summary_source_delete AFTER DELETE ON messages BEGIN {remove} END")
    connection.execute(f"""
        CREATE TRIGGER search_summary_source_update AFTER UPDATE OF source_account_id, message_type ON messages
        WHEN OLD.source_account_id IS NOT NEW.source_account_id
          OR ({predicate('OLD.')}) != ({predicate('NEW.')})
        BEGIN {remove} {add} END
    """)
    _mark(connection, "source", version)


def ensure_search_summaries(connection: sqlite3.Connection, *, excluded_types: Collection[str]) -> None:
    """Initialize once, within the caller's existing writer transaction."""
    if not connection.in_transaction:
        raise ValueError("search summary initialization requires a writer transaction")
    if not (_table(connection, "search_messages") or _table(connection, "messages")):
        return
    connection.execute(f"CREATE TABLE IF NOT EXISTS {_STATE}(key TEXT PRIMARY KEY, value TEXT NOT NULL)")
    _ensure_index(connection)
    _ensure_source(connection, excluded_types)


def browse_counts(
    connection: sqlite3.Connection, account_id: str, *, conversation_ids: Sequence[str] | None = None,
) -> dict[str, int] | None:
    if not _ready(connection, "index", "1"):
        return None
    where = "source_account_id=? AND complete_count>0"
    params = [account_id]
    if conversation_ids:
        where += f" AND conversation_id IN ({','.join('?' for _ in conversation_ids)})"
        params.extend(conversation_ids)
    return {str(row[0]): int(row[1]) for row in connection.execute(
        f"SELECT conversation_id, complete_count FROM {_INDEX} WHERE {where}", params,
    )}


def index_totals(connection: sqlite3.Connection, *, account_id: str | None = None) -> tuple[int, int] | None:
    if not _ready(connection, "index", "1"):
        return None
    where, params = ("", ()) if account_id is None else (" WHERE source_account_id=?", (account_id,))
    row = connection.execute(
        f"SELECT COALESCE(SUM(indexed_count),0), COALESCE(SUM(complete_count),0) FROM {_INDEX}{where}", params,
    ).fetchone()
    return (int(row[0]), int(row[1])) if row is not None else (0, 0)


def source_total(
    connection: sqlite3.Connection, *, excluded_types: Collection[str], account_id: str | None = None,
) -> int | None:
    if not _ready(connection, "source", _source_version(excluded_types)):
        return None
    if account_id is None:
        row = connection.execute(f"SELECT COALESCE(SUM(searchable_count),0) FROM {_SOURCE}").fetchone()
    else:
        row = connection.execute(f"SELECT searchable_count FROM {_SOURCE} WHERE source_account_id=?", (account_id,)).fetchone()
    return int(row[0]) if row is not None else 0

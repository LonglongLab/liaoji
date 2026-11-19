"""Versioned authorization, independent of archive data and device upload credentials."""

from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
from contextlib import closing, contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Generator, Literal

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, model_validator
from typing_extensions import Annotated

Identifier = Annotated[str, StringConstraints(min_length=1, max_length=512)]


class ChatScope(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: Literal["selected", "all"]
    conversation_ids: tuple[Identifier, ...] = Field(default=(), max_length=500)

    @model_validator(mode="after")
    def check_mode(self) -> ChatScope:
        ids = self.conversation_ids
        if (self.mode == "selected") != bool(ids) or len(set(ids)) != len(ids):
            raise ValueError("Invalid conversation selection")
        return self

    def allows(self, conversation_id: str) -> bool:
        return self.mode == "all" or conversation_id in self.conversation_ids


@dataclass(frozen=True)
class Grant:
    id: str
    archive_id: str
    account_id: str
    version: int
    scope: ChatScope | None


class PolicyConflict(Exception):
    """The caller must re-read the effective policy before trying a new edit."""


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class PolicyStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    @contextmanager
    def transaction(self, *, write: bool = False) -> Generator[sqlite3.Connection]:
        with closing(sqlite3.connect(self.path)) as connection:
            connection.row_factory = sqlite3.Row
            connection.execute("PRAGMA foreign_keys = ON")
            if write:
                connection.execute("BEGIN IMMEDIATE")
            with connection:
                yield connection

    def initialize(self) -> None:
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS grants (
                    id TEXT PRIMARY KEY, archive_id TEXT NOT NULL, account_id TEXT NOT NULL,
                    version INTEGER NOT NULL, scope TEXT
                );
                CREATE TABLE IF NOT EXISTS policy_operations (
                    grant_id TEXT NOT NULL REFERENCES grants(id),
                    operation_id TEXT NOT NULL, request_digest TEXT NOT NULL,
                    PRIMARY KEY (grant_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS connections (
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id),
                    token_digest TEXT UNIQUE NOT NULL
                );
                CREATE TABLE IF NOT EXISTS grant_creation (
                    archive_id TEXT NOT NULL, operation_id TEXT NOT NULL,
                    grant_id TEXT NOT NULL REFERENCES grants(id), request_digest TEXT NOT NULL,
                    PRIMARY KEY (archive_id, operation_id)
                );
                CREATE TABLE IF NOT EXISTS declined_chats (
                    grant_id TEXT NOT NULL REFERENCES grants(id), conversation_id TEXT NOT NULL,
                    PRIMARY KEY (grant_id, conversation_id)
                );
                CREATE TABLE IF NOT EXISTS agent_service (
                    id INTEGER PRIMARY KEY CHECK (id = 1), enabled INTEGER NOT NULL
                );
                INSERT OR IGNORE INTO agent_service VALUES (1, 0);
                """)

    @staticmethod
    def _grant(row: sqlite3.Row) -> Grant:
        return Grant(
            row["id"], row["archive_id"], row["account_id"], row["version"],
            ChatScope.model_validate_json(row["scope"]) if row["scope"] is not None else None,
        )

    def enabled(self) -> bool:
        with self.transaction() as db:
            return bool(db.execute("SELECT enabled FROM agent_service WHERE id=1").fetchone()[0])

    def set_enabled(self, enabled: bool) -> None:
        with self.transaction(write=True) as db:
            db.execute("UPDATE agent_service SET enabled=? WHERE id=1", (enabled,))

    def list_grants(self, archive_id: str) -> tuple[Grant, ...]:
        with self.transaction() as db:
            return tuple(self._grant(row) for row in db.execute(
                "SELECT * FROM grants WHERE archive_id=? AND scope IS NOT NULL ORDER BY rowid",
                (archive_id,),
            ))

    def get_grant(self, grant_id: str, archive_id: str) -> Grant | None:
        with self.transaction() as db:
            row = db.execute("SELECT * FROM grants WHERE id=? AND archive_id=?",
                             (grant_id, archive_id)).fetchone()
        return self._grant(row) if row is not None else None

    def additions(self, grant: Grant, requested: tuple[str, ...]) -> tuple[str, ...]:
        if grant.scope is None or grant.scope.mode == "all":
            return ()
        return tuple(sorted(set(requested) - set(self.declined(grant)) - set(grant.scope.conversation_ids)))

    def declined(self, grant: Grant) -> tuple[str, ...]:
        with self.transaction() as db:
            return tuple(
                row[0] for row in db.execute(
                    "SELECT conversation_id FROM declined_chats WHERE grant_id=? ORDER BY conversation_id",
                    (grant.id,),
                )
            )

    def decline(self, grant: Grant, ids: tuple[str, ...]) -> None:
        with self.transaction(write=True) as db:
            current = db.execute("SELECT version FROM grants WHERE id=? AND scope IS NOT NULL",
                                 (grant.id,)).fetchone()
            if current is None or current[0] != grant.version:
                raise PolicyConflict
            db.executemany("INSERT OR IGNORE INTO declined_chats VALUES (?, ?)",
                           [(grant.id, cid) for cid in ids])


    def create(
        self, archive_id: str, account_id: str, scope: ChatScope, operation_id: str | None = None
    ) -> Grant:
        grant = Grant(secrets.token_urlsafe(18), archive_id, account_id, 1, scope)
        digest = token_digest(json.dumps([account_id, scope.model_dump_json()]))
        with self.transaction(write=True) as db:
            if operation_id is not None:
                row = db.execute("""
                    SELECT g.*, c.request_digest FROM grant_creation c JOIN grants g ON g.id=c.grant_id
                    WHERE c.archive_id=? AND c.operation_id=?
                    """, (archive_id, operation_id)).fetchone()
                if row is not None:
                    if row["request_digest"] != digest:
                        raise PolicyConflict
                    return self._grant(row)
            db.execute(
                "INSERT INTO grants VALUES (?, ?, ?, ?, ?)",
                (grant.id, archive_id, account_id, grant.version, scope.model_dump_json()),
            )
            if operation_id is not None:
                db.execute("INSERT INTO grant_creation VALUES (?,?,?,?)",
                           (archive_id, operation_id, grant.id, digest))
        return grant

    def change(
        self,
        grant_id: str,
        archive_id: str,
        *,
        expected_version: int,
        operation_id: str,
        scope: ChatScope | None,
    ) -> Grant:
        scope_json = scope.model_dump_json() if scope is not None else None
        fingerprint = token_digest(json.dumps([expected_version, scope_json]))
        with self.transaction(write=True) as db:
            row = db.execute(
                "SELECT * FROM grants WHERE id=? AND archive_id=?", (grant_id, archive_id)
            ).fetchone()
            if row is None:
                raise PolicyConflict
            current = self._grant(row)
            applied = db.execute(
                "SELECT request_digest FROM policy_operations WHERE grant_id=? AND operation_id=?",
                (grant_id, operation_id),
            ).fetchone()
            if applied is not None:
                if applied[0] != fingerprint:
                    raise PolicyConflict
                return current
            if current.scope is None or current.version != expected_version:
                raise PolicyConflict
            db.execute("UPDATE grants SET version=version+1, scope=? WHERE id=?",
                       (scope_json, grant_id))
            db.execute("INSERT INTO policy_operations VALUES (?, ?, ?)",
                       (grant_id, operation_id, fingerprint))
            if scope is None:
                db.execute("DELETE FROM connections WHERE grant_id=?", (grant_id,))
            return Grant(grant_id, archive_id, current.account_id, current.version + 1, scope)

    def authenticate(self, token: str) -> tuple[str, Grant] | None:
        with self.transaction() as db:
            row = db.execute(
                """
                SELECT c.id AS connection_id, g.* FROM connections AS c
                JOIN grants AS g ON g.id=c.grant_id
                JOIN agent_service AS s ON s.id=1 AND s.enabled=1
                WHERE c.token_digest=? AND g.scope IS NOT NULL
                """,
                (token_digest(token),),
            ).fetchone()
        return (row["connection_id"], self._grant(row)) if row is not None else None

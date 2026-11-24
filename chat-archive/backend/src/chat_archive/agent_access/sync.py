"""Independent, versioned policy synchronization for a confirmed desktop link."""

from __future__ import annotations

import json
import secrets
from dataclasses import dataclass, replace
from typing import Literal
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from chat_archive.agent_access.oauth import OAuthStore, valid_origin
from chat_archive.agent_access.policy import (
    ChatScope,
    Grant,
    Identifier,
    PolicyConflict,
    PolicyStore,
    token_digest,
)
from chat_archive.http_utils import RejectRedirectHandler


class LinkRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str


class BindRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    account_id: str
    code: str


class LocalBindRequest(BindRequest):
    local_grant_id: Identifier | None = None
    version: int | None = Field(default=None, ge=1, strict=True)

    @model_validator(mode="after")
    def target_version(self) -> LocalBindRequest:
        if (self.local_grant_id is None) != (self.version is None):
            raise ValueError("An existing local authorization requires its version")
        return self


class RemoteGrant(BaseModel):
    id: str
    archive_id: str
    account_id: str
    version: int
    scope: ChatScope | None
    linked_account_id: str
    declined_conversation_ids: tuple[str, ...] = ()


class RejectSharedAdditions(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(ge=1, strict=True)
    conversation_ids: tuple[Identifier, ...] = Field(min_length=1, max_length=10)


class LinkReceipt(RemoteGrant):
    token: str


class SyncUnavailable(Exception):
    """The effective upstream range cannot be verified."""


class SyncRevoked(Exception):
    """The remote synchronization link has been revoked."""


NoRedirect = RejectRedirectHandler


class RemotePolicy:
    def __init__(self, store: OAuthStore) -> None:
        self.store = store
        with store.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS policy_links (
                    code_digest TEXT UNIQUE, expires_at INTEGER NOT NULL,
                    token_digest TEXT UNIQUE, grant_id TEXT NOT NULL REFERENCES grants(id),
                    source_account_id TEXT NOT NULL
                );
                """)

    def offer(self, grant: Grant, source_account_id: str) -> str:
        code = secrets.token_urlsafe(32)
        now = int(self.store.now())
        with self.store.transaction(write=True) as db:
            db.execute("DELETE FROM policy_links WHERE expires_at<=? AND token_digest IS NULL", (now,))
            db.execute("DELETE FROM policy_links WHERE grant_id=? AND source_account_id=? AND token_digest IS NULL",
                       (grant.id, source_account_id))
            db.execute("INSERT INTO policy_links VALUES (?,?,NULL,?,?)",
                       (token_digest(code), now + 300, grant.id, source_account_id))
        return code

    def claim(self, code: str, source_account_id: str) -> LinkReceipt | None:
        token = secrets.token_urlsafe(32)
        with self.store.transaction(write=True) as db:
            row = db.execute("""
                UPDATE policy_links SET code_digest=NULL,token_digest=?
                WHERE code_digest=? AND expires_at>? AND source_account_id=?
                 AND EXISTS (SELECT 1 FROM grants g WHERE g.id=grant_id AND g.scope IS NOT NULL)
                RETURNING grant_id
                """, (token_digest(token), token_digest(code), int(self.store.now()), source_account_id)
            ).fetchone()
            if row is None:
                return None
            grant = self.store._grant(db.execute(  # pyright: ignore[reportPrivateUsage]
                "SELECT * FROM grants WHERE id=?", (row[0],)
            ).fetchone())
        return LinkReceipt(id=grant.id, archive_id=grant.archive_id,
                           account_id=grant.account_id, version=grant.version,
                           scope=grant.scope, linked_account_id=source_account_id, token=token,
                           declined_conversation_ids=self.store.declined(grant))

    def read(self, token: str) -> tuple[Grant, str] | None:
        with self.store.transaction() as db:
            row = db.execute("""
                SELECT g.*,l.source_account_id FROM policy_links l JOIN grants g ON g.id=l.grant_id
                WHERE l.token_digest=?
                """, (token_digest(token),)).fetchone()
        if row is None:
            return None
        return self.store._grant(row), row["source_account_id"]  # pyright: ignore[reportPrivateUsage]


@dataclass(frozen=True)
class LocalLink:
    grant_id: str
    upstream_id: str
    archive_id: str
    origin: str
    token: bytes


class LocalLinks:
    def __init__(self, store: PolicyStore) -> None:
        self.store = store
        with store.transaction() as db:
            db.execute("""
                CREATE TABLE IF NOT EXISTS shared_policy (
                    grant_id TEXT PRIMARY KEY REFERENCES grants(id), upstream_id TEXT NOT NULL,
                    archive_id TEXT NOT NULL, origin TEXT NOT NULL, token BLOB NOT NULL,
                    UNIQUE (origin, archive_id, upstream_id)
                )
                """)
            db.execute("""
                CREATE TABLE IF NOT EXISTS pending_policy (
                    grant_id TEXT PRIMARY KEY REFERENCES grants(id), version INTEGER NOT NULL,
                    operation_id TEXT NOT NULL, scope TEXT
                )
                """)

    def pending(self, grant_id: str) -> tuple[int, str, ChatScope | None] | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT version,operation_id,scope FROM pending_policy WHERE grant_id=?",
                             (grant_id,)).fetchone()
        return (row[0], row[1], ChatScope.model_validate_json(row[2]) if row[2] is not None else None
                ) if row is not None else None

    def pending_ids(self) -> tuple[str, ...]:
        with self.store.transaction() as db:
            return tuple(row[0] for row in db.execute(
                "SELECT grant_id FROM pending_policy ORDER BY rowid LIMIT 100"
            ))

    def save_pending(self, grant_id: str, version: int, operation: str, scope: ChatScope | None) -> None:
        with self.store.transaction(write=True) as db:
            current = db.execute(
                "SELECT version,scope FROM grants WHERE id=?", (grant_id,)
            ).fetchone()
            if current is None or current[0] != version or current[1] is None:
                raise PolicyConflict
            previous = db.execute(
                "SELECT version,operation_id FROM pending_policy WHERE grant_id=?", (grant_id,)
            ).fetchone()
            if previous is not None and previous[0] == version and previous[1] != operation:
                raise PolicyConflict
            db.execute("INSERT OR REPLACE INTO pending_policy VALUES (?,?,?,?)",
                       (grant_id, version, operation, scope.model_dump_json() if scope else None))

    def clear_pending(self, grant_id: str, operation: str) -> None:
        with self.store.transaction(write=True) as db:
            db.execute("DELETE FROM pending_policy WHERE grant_id=? AND operation_id=?",
                       (grant_id, operation))

    def get(self, grant_id: str) -> LocalLink | None:
        with self.store.transaction() as db:
            row = db.execute("SELECT * FROM shared_policy WHERE grant_id=?", (grant_id,)).fetchone()
        return LocalLink(*row) if row is not None else None

    def attach(
        self, archive_id: str, local_account: str, upstream_archive: str, origin: str,
        upstream: RemoteGrant, protected_token: bytes,
        *, local_grant_id: str | None = None, expected_version: int | None = None,
    ) -> Grant:
        if upstream.scope is None:
            raise PolicyConflict
        with self.store.transaction(write=True) as db:
            original = None
            if local_grant_id is not None:
                original = db.execute("""
                    SELECT g.* FROM grants g WHERE g.id=? AND g.archive_id=? AND g.account_id=?
                    AND g.scope IS NOT NULL AND g.version=?
                    AND NOT EXISTS (SELECT 1 FROM shared_policy s WHERE s.grant_id=g.id)
                    """, (local_grant_id, archive_id, local_account, expected_version)).fetchone()
                if original is None:
                    raise PolicyConflict
            elif db.execute("""
                SELECT 1 FROM grants g WHERE g.archive_id=? AND g.account_id=? AND g.scope IS NOT NULL
                AND NOT EXISTS (SELECT 1 FROM shared_policy s WHERE s.grant_id=g.id)
                """, (archive_id, local_account)).fetchone() is not None:
                # Never silently leave a previously configured Agent on an independent scope.
                raise PolicyConflict
            prior = db.execute("""
                SELECT g.* FROM shared_policy l JOIN grants g ON g.id=l.grant_id
                WHERE l.origin=? AND l.archive_id=? AND l.upstream_id=?
                """, (origin, upstream_archive, upstream.id)).fetchone()
            if prior is not None:
                previous = self.store._grant(prior)  # pyright: ignore[reportPrivateUsage]
                if previous.account_id != local_account or previous.scope is None or (
                    previous.version > upstream.version
                    or (previous.version == upstream.version and previous.scope != upstream.scope)
                ):
                    raise PolicyConflict
                db.execute("UPDATE grants SET version=?,scope=? WHERE id=?",
                           (upstream.version, upstream.scope.model_dump_json(), previous.id))
                db.execute("UPDATE shared_policy SET token=? WHERE grant_id=?",
                           (protected_token, previous.id))
                new_id = previous.id
            else:
                new_id = secrets.token_urlsafe(18)
                db.execute("INSERT INTO grants VALUES (?,?,?,?,?)",
                           (new_id, archive_id, local_account, upstream.version,
                            upstream.scope.model_dump_json()))
                db.execute("INSERT INTO shared_policy VALUES (?,?,?,?,?)",
                           (new_id, upstream.id, upstream_archive, origin, protected_token))
            if original is not None:
                # The token/connection is retained, but now resolves to the shared grant.
                # Old tickets bind the old grant ID and therefore cannot be resurrected
                # even when the independent and upstream version numbers happen to match.
                db.execute("UPDATE connections SET grant_id=? WHERE grant_id=?", (new_id, local_grant_id))
                db.execute("UPDATE grants SET scope=NULL,version=version+1 WHERE id=?", (local_grant_id,))
        return Grant(new_id, archive_id, local_account, upstream.version, upstream.scope)

    def reconcile(self, grant: Grant, remote: RemoteGrant) -> Grant:
        with self.store.transaction(write=True) as db:
            row = db.execute("SELECT * FROM grants WHERE id=?", (grant.id,)).fetchone()
            saved = self.store._grant(row)  # pyright: ignore[reportPrivateUsage]
            if remote.version < saved.version or (
                saved.scope is None and remote.scope is not None
            ):
                raise SyncUnavailable
            if remote.version == saved.version and remote.scope != saved.scope:
                raise SyncUnavailable
            if remote.version > saved.version:
                scope = remote.scope.model_dump_json() if remote.scope is not None else None
                db.execute("UPDATE grants SET version=?,scope=? WHERE id=?",
                           (remote.version, scope, grant.id))
                if scope is None:
                    db.execute("DELETE FROM connections WHERE grant_id=?", (grant.id,))
            if remote.declined_conversation_ids:
                db.executemany("INSERT OR IGNORE INTO declined_chats VALUES (?, ?)",
                               [(grant.id, chat) for chat in remote.declined_conversation_ids])
        return replace(grant, version=remote.version, scope=remote.scope)


def request_policy(
    origin: str,
    path: Literal["claim", "current", "decline"],
    *,
    method: Literal["GET", "POST", "PUT"],
    token: str | None = None,
    body: dict[str, object] | None = None,
) -> RemoteGrant:
    if not valid_origin(origin):
        raise SyncUnavailable
    headers = {"Accept": "application/json"}
    if token is not None:
        headers["Authorization"] = f"Bearer {token}"
    payload = json.dumps(body).encode() if body is not None else None
    if payload is not None:
        headers["Content-Type"] = "application/json"
    request = Request(f"{origin}/api/v1/agent-access/sync/{path}", data=payload,
                      method=method, headers=headers)
    try:
        with build_opener(NoRedirect()).open(request, timeout=3) as response:
            data = response.read(512 * 1024)
        return TypeAdapter(LinkReceipt if path == "claim" else RemoteGrant).validate_json(data)
    except HTTPError as exc:
        exc.close()
        if exc.code == 409:
            raise PolicyConflict from None
        if exc.code in {401, 404, 410}:
            raise SyncRevoked from None
        raise SyncUnavailable from None
    except (ValueError, OSError):
        raise SyncUnavailable from None

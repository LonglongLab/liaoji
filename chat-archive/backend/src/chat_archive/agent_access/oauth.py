"""OAuth code and refresh exchanges for the self-hosted MCP resource."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time
from collections.abc import Callable
from pathlib import Path
from urllib.parse import urlsplit
from pydantic import TypeAdapter

from chat_archive.agent_access.policy import Grant, PolicyStore, token_digest
from chat_archive.agent_access.tools import Document

_PKCE = re.compile(r"[A-Za-z0-9._~-]{43,128}\Z")
_CHALLENGE = re.compile(r"[A-Za-z0-9_-]{43}\Z")
ACCESS_SECONDS = 900
REFRESH_SECONDS = 30 * 24 * 3600
_REQUEST = TypeAdapter(dict[str, str])


def valid_origin(origin: str) -> bool:
    try:
        url = urlsplit(origin)
        return bool(url.scheme == "https" and url.hostname and url.username is None
                    and not url.path and not url.query and not url.fragment
                    and origin == f"https://{url.netloc}" and url.port != 0
                    and not re.search(r"[\s\\]", origin))
    except ValueError:
        return False


def valid_redirect(uri: str) -> bool:
    try:
        url = urlsplit(uri)
        return bool(
            url.hostname and url.username is None and not url.fragment and url.port != 0
            and not re.search(r"[\s\\]", uri)
            and (url.scheme == "https" or
                 (url.scheme == "http" and url.hostname in {"127.0.0.1", "::1", "localhost"}))
            and uri.startswith(f"{url.scheme}://{url.netloc}")
        )
    except ValueError:
        return False


class OAuthFailure(Exception):
    pass


class OAuthStore(PolicyStore):
    def __init__(
        self, path: Path, origin: str, clients: dict[str, str],
        now: Callable[[], float] = time.time,
    ) -> None:
        super().__init__(path)
        if not valid_origin(origin) or not clients or not all(
            key and valid_redirect(uri) for key, uri in clients.items()
        ):
            raise ValueError("Invalid MCP OAuth configuration")
        self.origin = origin
        self.resource = f"{origin}/mcp"
        self.clients = dict(clients)
        self.now = now

    def initialize(self) -> None:
        super().initialize()
        with self.transaction() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS oauth_codes (
                    digest TEXT PRIMARY KEY, client_id TEXT NOT NULL, redirect_uri TEXT NOT NULL,
                    challenge TEXT NOT NULL, expires_at INTEGER NOT NULL,
                    grant_id TEXT NOT NULL REFERENCES grants(id), resource TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_connections (
                    id TEXT PRIMARY KEY, grant_id TEXT NOT NULL REFERENCES grants(id),
                    client_id TEXT NOT NULL, resource TEXT NOT NULL,
                    access_digest TEXT UNIQUE NOT NULL, access_expires INTEGER NOT NULL,
                    refresh_digest TEXT UNIQUE NOT NULL, refresh_expires INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_requests (
                    id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires_at INTEGER NOT NULL
                );
                CREATE TABLE IF NOT EXISTS oauth_registered_clients (
                    id TEXT PRIMARY KEY, redirect_uri TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS oauth_registered_redirect
                    ON oauth_registered_clients(redirect_uri);
                CREATE TABLE IF NOT EXISTS oauth_used_refresh (
                    digest TEXT PRIMARY KEY,
                    connection_id TEXT NOT NULL REFERENCES oauth_connections(id) ON DELETE CASCADE,
                    expires_at INTEGER NOT NULL
                );
                """)

    def redirect_for(self, client_id: str) -> str | None:
        if client_id in self.clients:
            return self.clients[client_id]
        with self.transaction() as db:
            row = db.execute("SELECT redirect_uri FROM oauth_registered_clients WHERE id=?",
                             (client_id,)).fetchone()
        return row[0] if row is not None and row[0] in self.clients.values() else None

    def register(self, redirect_uri: str) -> str:
        if redirect_uri not in self.clients.values():
            raise OAuthFailure("invalid_redirect_uri")
        with self.transaction(write=True) as db:
            # This registration surface supports one fixed public-client profile per
            # allowlisted redirect (one URI, auth=none). Repeated anonymous requests
            # must not allocate permanent rows. Keep legacy IDs valid for existing
            # connections rather than deleting duplicate registrations underneath them.
            existing = db.execute(
                "SELECT id FROM oauth_registered_clients WHERE redirect_uri=? ORDER BY id LIMIT 1",
                (redirect_uri,),
            ).fetchone()
            if existing is not None:
                return str(existing[0])
            identifier = "client_" + secrets.token_urlsafe(24)
            db.execute("INSERT INTO oauth_registered_clients VALUES (?,?)", (identifier, redirect_uri))
        return identifier

    def check_client(self, client_id: str, uri: str, challenge: str, resource: str) -> None:
        if (not uri or self.redirect_for(client_id) != uri or resource != self.resource
                or _CHALLENGE.fullmatch(challenge) is None):
            raise OAuthFailure("invalid_request")

    def begin(self, request: dict[str, str]) -> str:
        self.check_client(request["client_id"], request["redirect_uri"],
                          request["code_challenge"], request["resource"])
        if request["response_type"] != "code" or request["code_challenge_method"] != "S256":
            raise OAuthFailure("invalid_request")
        now = int(self.now())
        identifier = secrets.token_urlsafe(24)
        with self.transaction(write=True) as db:
            db.execute("DELETE FROM oauth_requests WHERE expires_at<=?", (now,))
            if db.execute("SELECT count(*) FROM oauth_requests").fetchone()[0] >= 500:
                raise OAuthFailure("temporarily_unavailable")
            db.execute("INSERT INTO oauth_requests VALUES (?, ?, ?)",
                       (identifier, json.dumps(request), now + 600))
        return identifier

    def pending(self, identifier: str) -> dict[str, str] | None:
        with self.transaction() as db:
            row = db.execute("SELECT payload FROM oauth_requests WHERE id=? AND expires_at>?",
                             (identifier, int(self.now()))).fetchone()
        return _REQUEST.validate_json(row[0]) if row is not None else None

    def consent(self, identifier: str, grant_id: str | None, archive_id: str) -> tuple[str, str, str]:
        now = int(self.now())
        with self.transaction(write=True) as db:
            row = db.execute(
                "DELETE FROM oauth_requests WHERE id=? AND expires_at>? RETURNING payload",
                (identifier, now),
            ).fetchone()
            if row is None:
                raise OAuthFailure("invalid_request")
            request = _REQUEST.validate_json(row[0])
            uri = request["redirect_uri"]
            client_id = request["client_id"]
            challenge = request["code_challenge"]
            resource = request["resource"]
            self.check_client(client_id, uri, challenge, resource)
            if grant_id is None:
                return uri, request["state"], ""
            grant = db.execute("SELECT 1 FROM grants WHERE id=? AND archive_id=? AND scope IS NOT NULL",
                               (grant_id, archive_id)).fetchone()
            if grant is None:
                raise OAuthFailure("invalid_grant")
            code = secrets.token_urlsafe(32)
            db.execute("DELETE FROM oauth_codes WHERE expires_at<=?", (now,))
            db.execute("INSERT INTO oauth_codes VALUES (?, ?, ?, ?, ?, ?, ?)",
                       (token_digest(code), client_id, uri, challenge, now + 120, grant_id, resource))
        return uri, request["state"], code

    def exchange(self, form: dict[str, str]) -> Document:
        client = form.get("client_id", "")
        if self.redirect_for(client) is None:
            raise OAuthFailure("invalid_client")
        if form.get("resource", self.resource) != self.resource:
            raise OAuthFailure("invalid_target")
        now = int(self.now())
        access, refresh = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        tokens = (token_digest(access), now + ACCESS_SECONDS,
                  token_digest(refresh), now + REFRESH_SECONDS)
        invalid_refresh = False
        with self.transaction(write=True) as db:
            db.execute("DELETE FROM oauth_connections WHERE refresh_expires<=?", (now,))
            db.execute("DELETE FROM oauth_used_refresh WHERE expires_at<=?", (now,))
            if form.get("grant_type") == "authorization_code":
                if form.get("resource") != self.resource:
                    raise OAuthFailure("invalid_target")
                code = token_digest(form.get("code", ""))
                row = db.execute("""
                    SELECT c.* FROM oauth_codes c JOIN grants g ON g.id=c.grant_id
                    WHERE c.digest=? AND c.client_id=? AND c.redirect_uri=? AND c.resource=?
                      AND c.expires_at>? AND g.scope IS NOT NULL
                    """, (code, client, form.get("redirect_uri"), self.resource, now)).fetchone()
                verifier = form.get("code_verifier", "")
                if row is None or _PKCE.fullmatch(verifier) is None:
                    raise OAuthFailure("invalid_grant")
                if self.redirect_for(client) != row["redirect_uri"]:
                    raise OAuthFailure("invalid_grant")
                candidate = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest())
                if not hmac.compare_digest(candidate.decode().rstrip("="), str(row["challenge"])):
                    raise OAuthFailure("invalid_grant")
                db.execute("DELETE FROM oauth_codes WHERE digest=?", (code,))
                db.execute("INSERT INTO oauth_connections VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (
                    secrets.token_urlsafe(18), row["grant_id"], client, self.resource, *tokens,
                ))
            elif form.get("grant_type") == "refresh_token":
                presented = token_digest(form.get("refresh_token", ""))
                row = db.execute("""
                    SELECT c.id,c.refresh_expires FROM oauth_connections c
                    JOIN grants g ON g.id=c.grant_id
                    WHERE c.client_id=? AND c.refresh_digest=? AND c.refresh_expires>?
                      AND c.resource=? AND g.scope IS NOT NULL
                    """, (client, presented, now, self.resource)).fetchone()
                if row is None:
                    reused = db.execute("""
                        SELECT c.id FROM oauth_used_refresh u
                        JOIN oauth_connections c ON c.id=u.connection_id
                        WHERE u.digest=? AND u.expires_at>? AND c.client_id=? AND c.resource=?
                        """, (presented, now, client, self.resource)).fetchone()
                    if reused is not None:
                        # Delete only this token family. Its opaque access token and
                        # all descendant refresh tokens stop working in the same commit.
                        db.execute("DELETE FROM oauth_connections WHERE id=?", (reused[0],))
                    invalid_refresh = True
                else:
                    db.execute("INSERT INTO oauth_used_refresh VALUES (?,?,?)",
                               (presented, row["id"], row["refresh_expires"]))
                    db.execute("""
                        UPDATE oauth_connections SET access_digest=?,access_expires=?,
                            refresh_digest=?,refresh_expires=? WHERE id=?
                        """, (*tokens, row["id"]))
            else:
                raise OAuthFailure("unsupported_grant_type")
        # Raising inside the transaction would roll back the security revocation.
        if invalid_refresh:
            raise OAuthFailure("invalid_grant")
        return {
            "access_token": access, "refresh_token": refresh, "token_type": "Bearer",
            "expires_in": ACCESS_SECONDS,
        }

    def authenticate(self, token: str) -> tuple[str, Grant] | None:
        with self.transaction() as db:
            row = db.execute("""
                SELECT c.id AS connection_id,c.client_id,g.* FROM oauth_connections c
                JOIN grants g ON g.id=c.grant_id
                WHERE c.access_digest=? AND c.access_expires>? AND c.resource=? AND g.scope IS NOT NULL
                """, (token_digest(token), int(self.now()), self.resource)).fetchone()
        if row is None or self.redirect_for(row["client_id"]) is None:
            return None
        return row["connection_id"], self._grant(row)

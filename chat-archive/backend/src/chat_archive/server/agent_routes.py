"""Self-hosted OAuth, user consent and authorization management."""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import re
from collections.abc import Callable
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.concurrency import run_in_threadpool

from chat_archive.agent_access.management import (
    ChangeGrant,
    NewGrant,
    ResolveAddition,
    grant_document,
)
from chat_archive.agent_access.oauth import OAuthFailure, OAuthStore
from chat_archive.agent_access.policy import ChatScope, Grant, Identifier, PolicyConflict
from chat_archive.agent_access.sync import (
    BindRequest,
    LinkRequest,
    RejectSharedAdditions,
    RemoteGrant,
    RemotePolicy,
)
from chat_archive.agent_access.tools import Document, QueryTools, ToolError
from chat_archive.agent_access.transport import register_transport
from chat_archive.http_body import read_bounded_body
from chat_archive.server.agent_reader import RemoteReader
from chat_archive.server.browser_routes import BrowserGate
from chat_archive.share_draft import ShareDraftError

_PRIVATE = {"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
_FIELDS = ("client_id", "redirect_uri", "resource", "response_type", "state",
           "code_challenge", "code_challenge_method")
_REQUEST_ID = re.compile(r"[A-Za-z0-9_-]{32}\Z")


class Consent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    grant_id: Identifier | None


class Registration(BaseModel):
    model_config = ConfigDict(extra="ignore")

    redirect_uris: tuple[Identifier, ...] = Field(min_length=1, max_length=1)
    token_endpoint_auth_method: str = "none"


class RemoteAccess(BaseModel):
    model_config = ConfigDict(extra="forbid")
    enabled: bool


def _oauth_error(exc: OAuthFailure) -> Response:
    return JSONResponse({"error": str(exc)}, status_code=400, headers=_PRIVATE)


def register_remote_routes(
    app: FastAPI, gate: BrowserGate, archive_id: Callable[[], str], reader: RemoteReader,
    store: OAuthStore,
) -> None:
    links = RemotePolicy(store)

    def active_issuer(request: Request) -> str | None:
        issuer, enabled = store.remote_settings()
        if enabled and issuer and request.headers.get("host") == urlsplit(issuer).netloc:
            return issuer
        return None

    def require_issuer(request: Request) -> str:
        issuer = active_issuer(request)
        if issuer is None:
            raise HTTPException(404, "Remote MCP is disabled", headers=_PRIVATE)
        return issuer

    def origin_for(request: Request) -> str | None:
        issuer = active_issuer(request)
        origin = request.headers.get("origin")
        return issuer if origin in {None, issuer} and request.headers.get("sec-fetch-site") != "cross-site" else None

    tools = QueryTools(reader, store.additions)
    register_transport(
        app, tools, authorize=store.authenticate, origin_for=origin_for,
        challenge=lambda issuer: f'Bearer resource_metadata="{issuer}/.well-known/oauth-protected-resource/mcp"',
    )

    @app.get("/.well-known/oauth-protected-resource/mcp", include_in_schema=False)
    @app.get("/.well-known/oauth-protected-resource", include_in_schema=False)
    def resource_metadata(request: Request, response: Response) -> Document:
        issuer = require_issuer(request)
        response.headers.update(_PRIVATE)
        return {"resource": f"{issuer}/mcp", "authorization_servers": [issuer],
                "bearer_methods_supported": ["header"]}

    @app.get("/.well-known/oauth-authorization-server", include_in_schema=False)
    def issuer_metadata(request: Request, response: Response) -> Document:
        issuer = require_issuer(request)
        response.headers.update(_PRIVATE)
        return {
            "issuer": issuer, "authorization_endpoint": f"{issuer}/oauth/authorize",
            "token_endpoint": f"{issuer}/oauth/token", "response_types_supported": ["code"],
            "registration_endpoint": f"{issuer}/oauth/register",
            "grant_types_supported": ["authorization_code", "refresh_token"],
            "token_endpoint_auth_methods_supported": ["none"],
            "code_challenge_methods_supported": ["S256"],
            "authorization_response_iss_parameter_supported": True,
        }

    @app.post("/oauth/register", include_in_schema=False)
    async def register(request: Request) -> Response:
        if origin_for(request) is None:
            return Response(status_code=403, headers=_PRIVATE)
        try:
            body = Registration.model_validate_json(await read_bounded_body(request, 8192))
            if body.token_endpoint_auth_method != "none":
                raise OAuthFailure("invalid_client_metadata")
            uri = body.redirect_uris[0]
            identifier = await run_in_threadpool(store.register, uri)
        except (ShareDraftError, ValidationError):
            return _oauth_error(OAuthFailure("invalid_client_metadata"))
        except OAuthFailure as exc:
            return _oauth_error(exc)
        return JSONResponse({
            "client_id": identifier, "redirect_uris": [uri], "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
        }, status_code=201, headers=_PRIVATE)

    @app.get("/oauth/authorize", include_in_schema=False)
    def authorize(request: Request) -> Response:
        require_issuer(request)
        values: dict[str, str] = {}
        for field in _FIELDS:
            matches = request.query_params.getlist(field)
            if len(matches) != 1 or not 1 <= len(matches[0]) <= 2048:
                return _oauth_error(OAuthFailure("invalid_request"))
            values[field] = matches[0]
        try:
            identifier = store.begin(values)
        except OAuthFailure as exc:
            return _oauth_error(exc)
        target = f"/remote/settings?mcp_request={identifier}#agent-access"
        if gate.session(request) is None:
            target = f"/login?mcp_request={identifier}"
        return RedirectResponse(target, status_code=303, headers=_PRIVATE)

    @app.post("/oauth/token", include_in_schema=False)
    async def token(request: Request) -> Response:
        if origin_for(request) is None:
            return Response(status_code=403, headers=_PRIVATE)
        if request.headers.get("content-type", "").partition(";")[0] != "application/x-www-form-urlencoded":
            return _oauth_error(OAuthFailure("invalid_request"))
        try:
            body = await read_bounded_body(request, 8192)
            entries = parse_qsl(body.decode(), keep_blank_values=True, strict_parsing=True,
                                max_num_fields=12, errors="strict")
            values = dict(entries)
            if len(entries) != len(values):
                raise OAuthFailure("invalid_request")
            result = await run_in_threadpool(store.exchange, values)
        except (ValueError, ShareDraftError):
            return _oauth_error(OAuthFailure("invalid_request"))
        except OAuthFailure as exc:
            return _oauth_error(exc)
        return JSONResponse(result, headers={**_PRIVATE, "Pragma": "no-cache"})

    def manage(request: Request, response: Response) -> str:
        if request.headers.get("authorization") is not None:
            raise HTTPException(403, "Browser session required", headers=_PRIVATE)
        if request.method != "GET":
            gate.guard_origin(request)
        gate.require(request)
        response.headers.update(_PRIVATE)
        return archive_id()

    def validate(archive: str, account: str, chats: tuple[str, ...]) -> None:
        if not reader.owns_account(archive, account) or any(
            not reader.owns_chat(archive, account, chat) for chat in chats
        ):
            raise HTTPException(422, "Unknown archive account or conversation", headers=_PRIVATE)

    def settings_document(archive: str) -> Document:
        origin, enabled = store.remote_settings()
        return {"enabled": enabled, "address": f"{origin}/mcp" if origin else "",
                "grants": [grant_document(item) for item in store.list_grants(archive)]}

    @app.get("/api/v1/agent-access")
    def status(request: Request, response: Response) -> Document:
        return settings_document(manage(request, response))

    @app.put("/api/v1/agent-access")
    def set_access(body: RemoteAccess, request: Request, response: Response) -> Document:
        archive = manage(request, response)
        try:
            store.set_remote_access(body.enabled, request.headers.get("origin", ""))
        except OAuthFailure as exc:
            raise HTTPException(422, "Open settings at the archive HTTPS address", headers=_PRIVATE) from exc
        return settings_document(archive)

    @app.post("/api/v1/agent-access/grants", status_code=201)
    def create(body: NewGrant, request: Request, response: Response) -> Document:
        archive = manage(request, response)
        validate(archive, body.account_id, body.scope.conversation_ids)
        try:
            return grant_document(store.create(archive, body.account_id, body.scope, body.operation_id))
        except PolicyConflict as exc:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE) from exc

    @app.put("/api/v1/agent-access/grants/{grant_id}")
    def update(grant_id: str, body: ChangeGrant, request: Request, response: Response) -> Document:
        archive = manage(request, response)
        try:
            if body.scope is not None:
                current = next((g for g in store.list_grants(archive) if g.id == grant_id), None)
                if current is None:
                    raise PolicyConflict
                validate(archive, current.account_id, body.scope.conversation_ids)
            grant = store.change(grant_id, archive, expected_version=body.version,
                                 operation_id=body.operation_id, scope=body.scope)
        except PolicyConflict as exc:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE) from exc
        return grant_document(grant)

    @app.get("/api/v1/agent-access/oauth/{request_id}")
    def pending(request_id: str, request: Request, response: Response) -> Document:
        manage(request, response)
        require_issuer(request)
        pending_request = store.pending(request_id) if _REQUEST_ID.fullmatch(request_id) else None
        if pending_request is None:
            raise HTTPException(404, "Authorization request expired", headers=_PRIVATE)
        client_id = pending_request["client_id"]
        return {"client_id": "AI 客户端" if client_id.startswith("client_") else client_id,
                "redirect_uri": pending_request["redirect_uri"]}

    @app.post("/api/v1/agent-access/oauth/{request_id}")
    def consent(request_id: str, body: Consent, request: Request, response: Response) -> Document:
        archive = manage(request, response)
        issuer = require_issuer(request)
        try:
            target, state, code = store.consent(request_id, body.grant_id, archive)
        except OAuthFailure as exc:
            raise HTTPException(409, "Authorization request changed", headers=_PRIVATE) from exc
        query = {"state": state, "code": code} if code else {"state": state, "error": "access_denied"}
        query["iss"] = issuer
        separator = "&" if "?" in target else "?"
        return {"redirect_uri": target + separator + urlencode(query)}

    @app.post("/api/v1/agent-access/grants/{grant_id}/link")
    def link(grant_id: str, body: LinkRequest, request: Request, response: Response) -> Document:
        archive = manage(request, response)
        grant = store.get_grant(grant_id, archive)
        if grant is None or grant.scope is None or not reader.account_link_verified(
            archive, body.account_id, grant.account_id,
        ):
            raise HTTPException(409, "Account identity has not been matched", headers=_PRIVATE)
        return {"code": links.offer(grant, body.account_id)}

    @app.post("/api/v1/agent-access/sync/claim")
    def claim(body: BindRequest, response: Response) -> Document:
        response.headers.update(_PRIVATE)
        receipt = links.claim(body.code, body.account_id)
        if receipt is None:
            raise HTTPException(401, "Link has expired", headers=_PRIVATE)
        return receipt.model_dump(mode="json")

    def sync_identity(request: Request, response: Response) -> tuple[Grant, str]:
        response.headers.update(_PRIVATE)
        scheme, _, value = request.headers.get("authorization", "").partition(" ")
        identity = links.read(value) if scheme.lower() == "bearer" and 1 <= len(value) <= 128 else None
        if identity is None:
            raise HTTPException(401, "Link has expired", headers=_PRIVATE)
        grant, source = identity
        if not reader.account_link_verified(grant.archive_id, source, grant.account_id):
            raise HTTPException(409, "Account identity has changed", headers=_PRIVATE)
        return identity

    def remote_grant(grant: Grant, source: str) -> Document:
        return RemoteGrant(id=grant.id, archive_id=grant.archive_id,
                           account_id=grant.account_id, version=grant.version,
                           scope=grant.scope, linked_account_id=source,
                           declined_conversation_ids=store.declined(grant)).model_dump(mode="json")

    @app.get("/api/v1/agent-access/sync/current")
    def current(request: Request, response: Response) -> Document:
        return remote_grant(*sync_identity(request, response))

    @app.post("/api/v1/agent-access/sync/decline")
    def decline_shared(body: RejectSharedAdditions, request: Request, response: Response) -> Document:
        grant, source = sync_identity(request, response)
        if body.version != grant.version or grant.scope is None:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE)
        ids = tuple(chat for chat in body.conversation_ids if reader.owns_chat(
            grant.archive_id, grant.account_id, chat,
        ))
        try:
            store.decline(grant, ids)
        except PolicyConflict as exc:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE) from exc
        return remote_grant(grant, source)

    @app.put("/api/v1/agent-access/sync/current")
    def change_shared(body: ChangeGrant, request: Request, response: Response) -> Document:
        grant, source = sync_identity(request, response)
        if body.scope is not None:
            validate(grant.archive_id, grant.account_id, body.scope.conversation_ids)
        try:
            new = store.change(grant.id, grant.archive_id, expected_version=body.version,
                               operation_id=body.operation_id, scope=body.scope)
        except PolicyConflict as exc:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE) from exc
        return remote_grant(new, source)

    def additions(ticket: str, archive: str) -> tuple[Grant, tuple[str, ...]]:
        try:
            identifier, version, requested = tools.proposed_additions(ticket)
        except ToolError as exc:
            raise HTTPException(409, "Authorization request expired", headers=_PRIVATE) from exc
        grant = store.get_grant(identifier, archive)
        if grant is None or grant.scope is None or version != grant.version:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE)
        missing = store.additions(grant, requested)
        return grant, tuple(chat for chat in missing if reader.owns_chat(archive, grant.account_id, chat))

    @app.get("/api/v1/agent-access/additions/{ticket}")
    def get_additions(ticket: str, request: Request, response: Response) -> Document:
        grant, ids = additions(ticket, manage(request, response))
        return {"grant": grant_document(grant), "conversation_ids": ids}

    @app.post("/api/v1/agent-access/additions/{ticket}")
    def resolve_addition(
        ticket: str, body: ResolveAddition, request: Request, response: Response,
    ) -> Document:
        grant, ids = additions(ticket, manage(request, response))
        if not ids:
            return grant_document(grant)
        try:
            if not body.allow:
                store.decline(grant, ids)
                return grant_document(grant)
            if grant.scope is None:
                raise ValueError("grant_scope_missing")
            scope = ChatScope(mode="selected", conversation_ids=(*grant.scope.conversation_ids, *ids))
            return update(grant.id, ChangeGrant(
                version=grant.version, operation_id=f"addition:{ticket[-96:]}", scope=scope,
            ), request, response)
        except (PolicyConflict, ValueError) as exc:
            raise HTTPException(409, "Authorization changed", headers=_PRIVATE) from exc

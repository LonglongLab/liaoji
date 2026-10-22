"""Short, session-bound management confirmation; restart always fails closed."""
# pyright: reportUnusedFunction=false
# FastAPI registers the decorated functions.
from __future__ import annotations

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from pydantic import BaseModel, ConfigDict, Field
from starlette.responses import Response

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.ports.browser_auth import BrowserAuthRejected, BrowserAuthThrottled
from chat_archive.server.browser_routes import BrowserGate, browser_throttled_response, request_source


class ManagementProof(BaseModel):
    model_config = ConfigDict(extra="forbid")
    password: str = Field(min_length=1, max_length=512, repr=False)
    second_factor: str | None = Field(default=None, max_length=128, repr=False)


@dataclass(frozen=True)
class _ManagementGrant:
    expires_at: float
    totp_enabled: bool
    setup: bool


class BrowserManagement:
    def __init__(self, gate: BrowserGate, *, clock: Callable[[], float] = time.time) -> None:
        self.gate = gate
        self.clock = clock
        self._grants: dict[str, _ManagementGrant] = {}
        self._lock = threading.Lock()

    def grant(self, session_id: str, seconds: int = 300, *, setup: bool = False, verified_totp: bool | None = None) -> int:
        now = self.clock()
        factor = self.gate.store.state().totp_enabled if verified_totp is None else verified_totp
        with self._lock:
            self._grants = {key: item for key, item in self._grants.items() if item.expires_at > now}
            if len(self._grants) >= 1024:
                self._grants.pop(next(iter(self._grants)))
            self._grants[session_id] = _ManagementGrant(now + seconds, factor, setup)
        return int(now + seconds)

    def require(self, request: Request) -> None:
        self.gate.guard_origin(request)
        context = self.gate.require_fresh(request)
        factor = self.gate.store.state().totp_enabled
        with self._lock:
            grant = self._grants.get(context.session_id)
            fresh = grant is not None and grant.expires_at > self.clock() and (grant.setup or grant.totp_enabled == factor)
        if not fresh:
            raise ArchiveError(ErrorCode.REMOTE_REAUTH_FRESH_REQUIRED)

    def register(self, app: FastAPI) -> None:
        @app.post("/api/v1/auth/management/verify", response_model=None)
        async def verify(request: Request, body: ManagementProof) -> dict[str, object] | Response:
            self.gate.guard_origin(request)
            context = self.gate.require_fresh(request)
            factor = self.gate.store.state().totp_enabled
            try:
                if factor:
                    await run_in_threadpool(self.gate.store.confirm_full_mfa, body.password,
                                            body.second_factor or "", source=request_source(request))
                else:
                    await run_in_threadpool(self.gate.store.confirm_password, body.password,
                                            source=request_source(request))
            except BrowserAuthThrottled as error:
                return browser_throttled_response(error)
            except BrowserAuthRejected:
                raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
            return {"verified": True, "expires_at": self.grant(context.session_id, verified_totp=factor)}

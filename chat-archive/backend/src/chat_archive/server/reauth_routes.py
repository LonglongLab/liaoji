"""本机应用锁端点（仅自托管远程端，PSU-2.213；PSU-2.221 改为按设备）。

应用锁是**本机**的便利锁，像手机指纹：每台浏览器（设备 Cookie 标识）各自开启、
各自录入 Windows Hello / Touch ID / 指纹；验证不了时用登录口令解锁。开关只影响
本机，别的设备不受影响。

授权分级：

- C（只需原登录身份、锁定态可用）：`reauth/state`、`reauth/challenge`（unlock）、
  `reauth/verify`、`reauth/password`（口令解锁）、`reauth/lock`、`DELETE reauth/notice`。
- D（需解锁）：`GET passkeys`；`reauth/challenge`（manage）在本机开锁时也需解锁。
- E（需解锁＋新鲜验证）：通行密钥注册与删除、`PUT reauth/policy`。录入前总要
  重新输入登录口令；口令解锁本身也算一次新鲜验证，所以「口令解锁 → 关闭」
  不必再验证一次。

解锁授权只经 `__Host-` HttpOnly Cookie 下发，**响应 JSON 里永远没有它**。
"""

# pyright: reportUnusedFunction=false

from __future__ import annotations

import json
import secrets
import time
from typing import Any, Literal

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.ports.browser_auth import BrowserAuthRejected, BrowserAuthThrottled
from chat_archive.ports.browser_reauth import (
    BrowserReauthConflict,
    BrowserReauthFreshRequired,
    BrowserReauthRejected,
    BrowserReauthStore,
    PasskeySummary,
)
from chat_archive.server.browser_routes import BrowserGate, request_source
from chat_archive.server.error_contract import archive_error_response
from chat_archive.server.webauthn_service import WebAuthnService

_MAX_CREDENTIAL_JSON = 16 * 1024


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


def _bounded_credential(value: dict[str, Any]) -> dict[str, Any]:
    if len(json.dumps(value, separators=(",", ":"))) > _MAX_CREDENTIAL_JSON:
        raise ValueError("credential too large")
    return value


class ReauthChallengeRequest(_StrictBody):
    purpose: Literal["unlock", "manage"]


class ReauthVerifyRequest(_StrictBody):
    challenge_id: str = Field(min_length=1, max_length=128)
    credential: dict[str, Any]

    @field_validator("credential")
    @classmethod
    def bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_credential(value)


class ReauthPasswordRequest(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)


class PasskeyRegistrationOptionsRequest(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)


class PasskeyRegistrationVerifyRequest(_StrictBody):
    challenge_id: str = Field(min_length=1, max_length=128)
    credential: dict[str, Any]
    label: str | None = Field(default=None, max_length=80)

    @field_validator("credential")
    @classmethod
    def bounded(cls, value: dict[str, Any]) -> dict[str, Any]:
        return _bounded_credential(value)


class ReauthPolicyRequest(_StrictBody):
    enabled: bool
    policy_version: int = Field(ge=0)


def _throttled(error: BrowserAuthThrottled) -> JSONResponse:
    response = archive_error_response(ArchiveError(ErrorCode.REMOTE_SIGN_IN_THROTTLED))
    response.headers["Retry-After"] = str(max(1, error.retry_after_seconds))
    return response


def _summary(item: PasskeySummary) -> dict[str, object]:
    return {
        "id": item.id,
        "label": item.label,
        "created_at": item.created_at,
        "last_used_at": item.last_used_at,
        "backed_up": item.backed_up,
    }


def register_reauth_routes(
    app: FastAPI,
    gate: BrowserGate,
    webauthn: WebAuthnService,
    *,
    now: Any = None,
) -> None:
    if gate.reauth is None:
        raise ValueError("reauth routes need a BrowserReauthStore on the gate")
    reauth: BrowserReauthStore = gate.reauth
    clock = (lambda: int(time.time())) if now is None else now

    @app.exception_handler(BrowserReauthRejected)
    def _rejected(_request: Request, _error: BrowserReauthRejected) -> Response:
        return archive_error_response(ArchiveError(ErrorCode.REMOTE_REAUTH_REJECTED))

    @app.exception_handler(BrowserReauthConflict)
    def _conflict(_request: Request, _error: BrowserReauthConflict) -> Response:
        return archive_error_response(ArchiveError(ErrorCode.REMOTE_REAUTH_CONFLICT))

    @app.exception_handler(BrowserReauthFreshRequired)
    def _fresh(_request: Request, _error: BrowserReauthFreshRequired) -> Response:
        return archive_error_response(ArchiveError(ErrorCode.REMOTE_REAUTH_FRESH_REQUIRED))

    def _require_available() -> None:
        if not webauthn.available():
            raise ArchiveError(ErrorCode.REMOTE_REAUTH_UNAVAILABLE, retryable=True)

    def _state(session_id: str, grant_value: str | None) -> dict[str, object]:
        state = reauth.session_state(session_id, grant_value)
        return {
            # 这台服务器能不能用通行密钥（RP 配置就绪）。浏览器本身支不支持由前端判断；
            # 两者任一不行，锁定页直接展开「使用密码解锁」。
            "supported": webauthn.available(),
            "enabled": state.enabled,
            "locked": state.enabled and not state.unlocked,
            "policy_version": state.policy_version,
            "unlocked_until": state.unlocked_until,
            "fresh_until": state.fresh_until,
            "has_passkeys": state.passkey_count > 0,
            # 旧版账户级应用锁升级时被关闭：提示一次「请在需要的设备上重新开启」。
            "migration_notice": state.migration_notice,
        }

    def _allow(session_id: str) -> list[tuple[bytes, tuple[str, ...]]]:
        return [(item.credential_id, item.transports) for item in reauth.list_passkeys(session_id)]

    async def _confirm_password(request: Request, password: str) -> None:
        await run_in_threadpool(
            gate.store.confirm_password, password, source=request_source(request)
        )

    # ------------------------------------------------------------------ C 级

    @app.get("/api/v1/auth/reauth/state")
    def reauth_state(request: Request) -> Response:
        context = gate.require_identity(request)
        # 升级前就登录着的会话在这里补绑设备；已绑定的只顺延设备 Cookie。
        device = gate.bind_device(request, context.session_id)
        response = JSONResponse(content=_state(context.session_id, gate.grant_value(request)))
        gate.attach_device(response, device)
        return response

    @app.post("/api/v1/auth/reauth/challenge")
    def reauth_challenge(request: Request, body: ReauthChallengeRequest) -> Response:
        gate.guard_origin(request)
        context = gate.require_identity(request)
        _require_available()
        enabled = reauth.lock_enabled(context.session_id)
        if body.purpose == "unlock" and not enabled:
            raise BrowserReauthConflict()
        if body.purpose == "manage" and enabled:
            # 管理用途不是解锁的替代路径：锁定态不能借它打开一个新鲜窗口。
            gate.require(request)
        try:
            reauth.guard_backoff(context.session_id)
        except BrowserAuthThrottled as error:
            return _throttled(error)
        allow = _allow(context.session_id)
        if not allow:
            raise BrowserReauthConflict()
        record = reauth.create_challenge(context.session_id, body.purpose, secrets.token_bytes(32))
        options = webauthn.authentication_options(
            challenge=record.challenge,
            timeout_seconds=reauth.challenge_seconds,
            allow=allow,
        )
        return JSONResponse(
            content={
                "challenge_id": record.id,
                "purpose": record.purpose,
                "expires_at": record.expires_at,
                "options": options,
            }
        )

    @app.post("/api/v1/auth/reauth/verify")
    def reauth_verify(request: Request, body: ReauthVerifyRequest) -> Response:
        gate.guard_origin(request)
        context = gate.require_identity(request)
        _require_available()
        try:
            reauth.guard_backoff(context.session_id)
        except BrowserAuthThrottled as error:
            return _throttled(error)
        try:
            record = reauth.consume_challenge(
                context.session_id, body.challenge_id, ("unlock", "manage")
            )
            if record.purpose == "manage" and reauth.lock_enabled(context.session_id):
                gate.require(request)
            verified = webauthn.verify_authentication(
                credential=body.credential,
                challenge=record.challenge,
                passkeys=reauth.list_passkeys(context.session_id),
                user_handle=reauth.user_handle(context.session_id),
            )
            token = reauth.complete_verification(
                context.session_id,
                record,
                passkey_id=verified.passkey.id,
                new_sign_count=verified.new_sign_count,
                backed_up=verified.backed_up,
            )
        except BrowserReauthRejected:
            reauth.record_failure(context.session_id)
            raise
        grant_value = token.value if token is not None else gate.grant_value(request)
        response = JSONResponse(content=_state(context.session_id, grant_value))
        if token is not None:
            gate.attach_grant(response, token, now=clock())
        return response

    @app.post("/api/v1/auth/reauth/password")
    async def reauth_password(request: Request, body: ReauthPasswordRequest) -> Response:
        """用登录口令解锁本机（PSU-2.221）：指纹验证不了、浏览器不支持时的退路。

        与通行密钥失败共用本会话的退避，另有口令本身按来源的退避。通过后签发与
        通行密钥同样期限的解锁授权，并打开一个短的新鲜窗口（紧接着关锁不必再验）。
        """
        gate.guard_origin(request)
        context = gate.require_identity(request)
        if not reauth.lock_enabled(context.session_id):
            raise BrowserReauthConflict()
        try:
            await run_in_threadpool(reauth.guard_backoff, context.session_id)
            await _confirm_password(request, body.password)
        except BrowserAuthThrottled as error:
            return _throttled(error)
        except BrowserAuthRejected:
            await run_in_threadpool(reauth.record_failure, context.session_id)
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        token = await run_in_threadpool(reauth.unlock_with_password, context.session_id)
        response = JSONResponse(content=_state(context.session_id, token.value))
        gate.attach_grant(response, token, now=clock())
        return response

    @app.post("/api/v1/auth/reauth/lock")
    def reauth_lock(request: Request) -> Response:
        gate.guard_origin(request)
        context = gate.require_identity(request)
        reauth.lock(context.session_id)
        response = JSONResponse(content=_state(context.session_id, None))
        gate.clear_grant(response)
        return response

    @app.delete("/api/v1/auth/reauth/notice", status_code=204)
    def dismiss_notice(request: Request) -> Response:
        gate.guard_origin(request)
        context = gate.require_identity(request)
        reauth.dismiss_notice(context.session_id)
        return Response(status_code=204)

    # ------------------------------------------------------------- D / E 级

    @app.get("/api/v1/auth/passkeys")
    def list_passkeys(request: Request) -> dict[str, object]:
        context = gate.require(request)
        return {
            "items": [
                _summary(
                    PasskeySummary(
                        id=item.id,
                        label=item.label,
                        created_at=item.created_at,
                        last_used_at=item.last_used_at,
                        backed_up=item.backed_up,
                    )
                )
                for item in reauth.list_passkeys(context.session_id)
            ]
        }

    @app.post("/api/v1/auth/passkeys/registration/options")
    async def registration_options(
        request: Request, body: PasskeyRegistrationOptionsRequest
    ) -> Response:
        gate.guard_origin(request)
        # 本机开着锁时 require() 已要求先解锁；录入（含重新录入）总要再输一次登录口令，
        # 只拿到会话 Cookie 的人登记不了自己的指纹。
        context = gate.require(request)
        _require_available()
        device = await run_in_threadpool(gate.bind_device, request, context.session_id)
        try:
            await _confirm_password(request, body.password)
        except BrowserAuthThrottled as error:
            return _throttled(error)
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        if await run_in_threadpool(reauth.lock_enabled, context.session_id):
            await run_in_threadpool(reauth.mark_password_fresh, context.session_id)
        record = await run_in_threadpool(
            reauth.create_challenge, context.session_id, "register", secrets.token_bytes(32)
        )
        options = webauthn.registration_options(
            user_handle=reauth.user_handle(context.session_id),
            challenge=record.challenge,
            timeout_seconds=reauth.challenge_seconds,
            # 不排除本机已有凭据：重新录入（指纹换了）要让同一个系统验证器按本机
            # user_handle 覆盖旧的那枚；旧记录由前端在登记成功后删掉。
            exclude=[],
        )
        response = JSONResponse(
            content={
                "challenge_id": record.id,
                "purpose": record.purpose,
                "expires_at": record.expires_at,
                "options": options,
            }
        )
        gate.attach_device(response, device)
        return response

    @app.post("/api/v1/auth/passkeys/registration/verify")
    def registration_verify(
        request: Request, body: PasskeyRegistrationVerifyRequest
    ) -> Response:
        gate.guard_origin(request)
        context = gate.require_fresh(request)
        _require_available()
        try:
            reauth.guard_backoff(context.session_id)
        except BrowserAuthThrottled as error:
            return _throttled(error)
        try:
            record = reauth.consume_challenge(context.session_id, body.challenge_id, ("register",))
            verified = webauthn.verify_registration(
                credential=body.credential, challenge=record.challenge
            )
            summary = reauth.add_passkey(
                context.session_id,
                record,
                credential_id=verified.credential_id,
                public_key=verified.public_key,
                sign_count=verified.sign_count,
                transports=verified.transports,
                backed_up=verified.backed_up,
                label=body.label or "",
            )
        except BrowserReauthRejected:
            reauth.record_failure(context.session_id)
            raise
        return JSONResponse(content=_summary(summary), status_code=201)

    @app.delete("/api/v1/auth/passkeys/{passkey_id}", status_code=204)
    def delete_passkey(request: Request, passkey_id: str) -> Response:
        gate.guard_origin(request)
        context = gate.require(request)
        if len(passkey_id) > 128 or not reauth.delete_passkey(context.session_id, passkey_id):
            raise ArchiveError(ErrorCode.NOT_FOUND, message="没有找到这枚通行密钥")
        return Response(status_code=204)

    @app.put("/api/v1/auth/reauth/policy")
    def put_policy(request: Request, body: ReauthPolicyRequest) -> Response:
        """开关**本机**应用锁。`policy_version` 是本机开关的 CAS 版本。"""
        gate.guard_origin(request)
        context = gate.require(request)
        if body.enabled:
            # 依赖不就绪时不允许开：开了只能靠口令解，指纹那一半形同虚设。
            _require_available()
        _policy, token = reauth.set_policy(
            context.session_id, enabled=body.enabled, expected_version=body.policy_version
        )
        response = JSONResponse(
            content=_state(context.session_id, token.value if token is not None else None)
        )
        if token is not None:
            gate.attach_grant(response, token, now=clock())
        else:
            gate.clear_grant(response)
        return response


__all__ = ["register_reauth_routes"]

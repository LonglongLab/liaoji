"""浏览器登录端点与会话闸门（仅自托管远程端，PSU-2.29）。

这一组端点说的是**本地 API 那份错误契约**（`contracts/openapi.yaml` 的 `Error`，
六个字段），不是设备令牌那套三字段的 `RemoteError`——因为吃它们的是同一份前端
代码，`ArchiveApiError` 认的就是那六个字段。同一个 app 上两份错误契约互不重叠：
路径不同，谁也没被改写。

Cookie 只带一个不透明会话 id。**设备令牌绝不下发到浏览器**：这条路径上根本
没有设备令牌可下发（见 `inprocess_archive.py`）。
"""

# pyright: reportUnusedFunction=false
# FastAPI 路由函数经装饰器注册，pyright 不把装饰器调用视为访问。

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.domain.client_info import BrowserClientInfo, valid_display_text
from chat_archive.server.error_contract import archive_error_response
from chat_archive.ports.browser_reauth import (
    BrowserReauthStore,
    DeviceCookie,
    ReauthGrantToken,
)
from chat_archive.ports.browser_auth import (
    BrowserAuthRejected,
    BrowserAuthStorageError,
    BrowserAuthStore,
    BrowserAuthThrottled,
    BrowserPasswordRejected,
    BrowserSessionContext,
    BrowserSessionToken,
    BrowserSetupClosed,
)

SESSION_COOKIE_NAME = "chat_archive_session"
# 解锁授权（PSU-2.213）。生产走 `__Host-` 前缀：必须 Secure、Path=/、不带 Domain，
# 子域名或明文 HTTP 页面都写不进来；测试的非 Secure 配置才退回普通名字。
REAUTH_COOKIE_NAME = "__Host-chat_archive_unlock"
REAUTH_COOKIE_NAME_INSECURE = "chat_archive_unlock"
# 设备标识（PSU-2.221）：应用锁是本机的，靠这枚长期 Cookie 认出「哪台浏览器」。
# 只是一个随机不透明值，服务端存摘要；登出不清它，同一浏览器再登录仍绑回原设备。
DEVICE_COOKIE_NAME = "__Host-chat_archive_device"
DEVICE_COOKIE_NAME_INSECURE = "chat_archive_device"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
_MAX_COOKIE_LENGTH = 512


class _StrictBody(BaseModel):
    model_config = ConfigDict(extra="forbid")


class BootstrapRequest(_StrictBody):
    bootstrap_token: str = Field(min_length=1, max_length=256)
    password: str = Field(min_length=1, max_length=1024)


class SignInRequest(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)
    # 登录页永远显示这一栏（写着「未开启可留空」）。只在开启 TOTP 之后才要求它，
    # 但**要不要**这一栏本身不通过接口问——那等于告诉猜的人「口令这一半猜对了」。
    second_factor: str | None = Field(default=None, max_length=64)


class TotpEnrollmentRequest(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)


class TotpEnableRequest(_StrictBody):
    code: str = Field(min_length=1, max_length=64)


class TotpDisableRequest(_StrictBody):
    password: str = Field(min_length=1, max_length=1024)


class BrowserClientInfoRequest(_StrictBody):
    model: str | None = Field(max_length=80)
    platform: str | None = Field(max_length=32)
    browser: str | None = Field(max_length=64)
    device_type: Literal["mobile", "tablet", "desktop", "unknown"]

    @field_validator("model", "platform", "browser")
    @classmethod
    def bounded_label(cls, value: str | None) -> str | None:
        if value is not None and not valid_display_text(value, 80):
            raise ValueError("invalid client display metadata")
        return None if value is None else value.strip()


def _throttled_response(error: BrowserAuthThrottled) -> JSONResponse:
    """429 + 标准 `Retry-After`。

    秒数走 HTTP 头而不是塞进错误体：`Error` 是两端共用的形状，为一个端点给它加
    一个字段，会让每一条错误响应都多带一个恒为 null 的键。
    """
    response = archive_error_response(ArchiveError(ErrorCode.REMOTE_SIGN_IN_THROTTLED))
    response.headers["Retry-After"] = str(max(1, error.retry_after_seconds))
    return response


def request_source(request: Request) -> str:
    """退避分桶用的来源。

    应用永远只监听回环，公网那一跳由反代加上（`docs/local-remote-boundary.md`
    第 4 节）。所以**只信反代追加的最后一跳**：`X-Forwarded-For` 的最后一个
    元素是它自己观察到的对端，前面的都是客户端可以随便写的。没有这个头就退回
    socket 对端；两者都判定不了就归到同一个 `unknown` 桶——按公网处理，宁可
    退避得更严，也不给一个能随手换桶的口子。
    """
    forwarded = request.headers.get("X-Forwarded-For")
    if forwarded:
        hops = [hop.strip() for hop in forwarded.split(",") if hop.strip()]
        if hops:
            return hops[-1]
    client = request.client
    if client is not None and client.host:
        return client.host
    return "unknown"


def browser_throttled_response(error: BrowserAuthThrottled) -> JSONResponse:
    """Shared public 429 envelope for authenticated browser operations."""
    return _throttled_response(error)


def origin_is_self(request: Request) -> bool:
    """非安全方法的 CSRF 判定：`Origin` 必须等于本站自己。

    TLS 在反代上终止，所以本进程看到的 scheme 恒为 http，比对只能落在
    host:port 上——而 `Host` 正是反代原样透传的那一个。攻击者的页面无论如何
    都在别的 host 上，这一比就已经拦住了；`SameSite=Strict` 是第二道。
    """
    if request.method in _SAFE_METHODS:
        return True
    if request.headers.get("Sec-Fetch-Site") == "cross-site":
        return False
    origin = request.headers.get("Origin")
    if not origin or origin == "null":
        # 浏览器对 POST 一定会带 Origin。缺席意味着这不是浏览器——而这一组
        # 端点只服务浏览器，所以缺席按拒绝处理，不照抄本地端「curl 放行」那条。
        return False
    parsed = urlsplit(origin)
    host = request.headers.get("Host", "")
    return bool(parsed.netloc) and parsed.netloc == host


def _never_ready() -> bool:
    return False


@dataclass(frozen=True)
class BrowserGate:
    """一次请求里「你是谁」与「能不能改东西」的唯一入口。

    三档（PSU-2.213 §6）：

    - `require_identity()`：只确认原登录身份，**不续期**。仅供 C 级白名单
      （capabilities、client-info、再认证状态/挑战/验证/锁定）。
    - `require()`：严格门禁。本会话所绑定的设备没开应用锁时与过去完全一样；
      开了（PSU-2.221，按设备）还要求本会话持有有效解锁授权，否则 423。只有通过
      它才刷新 `last_seen_at`。
    - `require_fresh()`：在 `require()` 之上，本机开着应用锁时还要求刚完成过
      管理用途的新鲜验证（E 级：TOTP、通行密钥、应用锁开关）。
    """

    store: BrowserAuthStore
    secure_cookie: bool = True
    reauth: BrowserReauthStore | None = None
    # WebAuthn 依赖（RP 配置、py_webauthn）是否就绪。只决定能不能用通行密钥；
    # 不就绪时仍可用登录口令解锁（PSU-2.221），所以门禁不再因此 503。
    reauth_ready: Callable[[], bool] = _never_ready

    def guard_origin(self, request: Request) -> None:
        if not origin_is_self(request):
            raise ArchiveError(ErrorCode.REMOTE_ORIGIN_REJECTED)

    @staticmethod
    def _session_cookie(request: Request) -> str | None:
        cookie = request.cookies.get(SESSION_COOKIE_NAME)
        if cookie is None or not cookie or len(cookie) > _MAX_COOKIE_LENGTH:
            return None
        return cookie

    def session(self, request: Request) -> BrowserSessionContext | None:
        """不续期地读出原登录身份。业务路由不得用它放行，只能用 `require*()`。"""
        cookie = self._session_cookie(request)
        if cookie is None:
            return None
        return self.store.authenticate(cookie, touch=False)

    @property
    def grant_cookie_name(self) -> str:
        return REAUTH_COOKIE_NAME if self.secure_cookie else REAUTH_COOKIE_NAME_INSECURE

    def grant_value(self, request: Request) -> str | None:
        value = request.cookies.get(self.grant_cookie_name)
        if not value or len(value) > _MAX_COOKIE_LENGTH:
            return None
        return value

    @property
    def device_cookie_name(self) -> str:
        return DEVICE_COOKIE_NAME if self.secure_cookie else DEVICE_COOKIE_NAME_INSECURE

    def device_value(self, request: Request) -> str | None:
        value = request.cookies.get(self.device_cookie_name)
        if not value or len(value) > _MAX_COOKIE_LENGTH:
            return None
        return value

    def lock_enabled(self, context: BrowserSessionContext) -> bool:
        """本会话绑定的那台设备是否开着应用锁。没绑定设备的会话视为未开启。"""
        return self.reauth is not None and self.reauth.lock_enabled(context.session_id)

    def is_unlocked(self, request: Request, context: BrowserSessionContext) -> bool:
        """本机没开锁恒为真；开着时看本会话的授权此刻是否仍然有效（事件流逐条复核）。"""
        if not self.lock_enabled(context):
            return True
        assert self.reauth is not None
        return self.reauth.grant_valid(context.session_id, self.grant_value(request))

    def require_identity(self, request: Request) -> BrowserSessionContext:
        context = self.session(request)
        if context is None:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REQUIRED)
        return context

    def require(self, request: Request) -> BrowserSessionContext:
        context = self.require_identity(request)
        if self.lock_enabled(context):
            assert self.reauth is not None
            if not self.reauth.grant_valid(context.session_id, self.grant_value(request)):
                raise ArchiveError(ErrorCode.REMOTE_REAUTH_REQUIRED)
        self.store.touch(context.session_id)
        return context

    def require_fresh(self, request: Request) -> BrowserSessionContext:
        context = self.require(request)
        if self.lock_enabled(context):
            assert self.reauth is not None
            if not self.reauth.has_fresh(context.session_id):
                raise ArchiveError(ErrorCode.REMOTE_REAUTH_FRESH_REQUIRED)
        return context

    def bind_device(self, request: Request, session_id: str) -> DeviceCookie | None:
        """把会话绑到这台浏览器的设备上；已绑定的会话从不改绑。"""
        if self.reauth is None:
            return None
        return self.reauth.bind_device(session_id, self.device_value(request))

    def attach_device(self, response: Response, cookie: DeviceCookie | None) -> None:
        """每次绑定都顺延设备 Cookie；没拿到（会话已绑在别的设备上）就不动它。"""
        if cookie is None or self.reauth is None:
            return
        response.set_cookie(
            self.device_cookie_name,
            cookie.value,
            max_age=self.reauth.device_cookie_seconds,
            path="/",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )

    def attach_grant(self, response: Response, token: ReauthGrantToken, *, now: int) -> None:
        response.set_cookie(
            self.grant_cookie_name,
            token.value,
            max_age=max(1, token.expires_at - now),
            path="/",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )

    def clear_grant(self, response: Response) -> None:
        response.delete_cookie(
            self.grant_cookie_name,
            path="/",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )

    def attach(self, response: Response, token: BrowserSessionToken, *, now: int) -> None:
        response.set_cookie(
            SESSION_COOKIE_NAME,
            token.value,
            max_age=max(1, token.absolute_expires_at - now),
            path="/",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )

    def clear(self, response: Response) -> None:
        response.delete_cookie(
            SESSION_COOKIE_NAME,
            path="/",
            httponly=True,
            secure=self.secure_cookie,
            samesite="strict",
        )


def register_browser_routes(
    app: FastAPI,
    gate: BrowserGate,
    *,
    now: Any = None,
    on_initialized: Callable[[], None] | None = None,
    on_setup_session: Callable[[str], None] | None = None,
) -> None:
    """登录、首次向导、TOTP 绑定与解绑。

    `on_initialized` 在向导跑完的那一刻回调一次。运行时用它删掉数据卷里的
    引导令牌明文——只在下次启动时删的话，那份明文会一直躺到重启为止。
    """

    import time

    clock = (lambda: int(time.time())) if now is None else now
    store = gate.store

    @app.exception_handler(ArchiveError)
    def _archive_error(_request: Request, error: ArchiveError) -> Response:
        return archive_error_response(error)

    @app.exception_handler(BrowserAuthStorageError)
    def _browser_storage(_request: Request, _error: BrowserAuthStorageError) -> Response:
        # 主密钥读不出来时**不能**说「登录信息不正确」——那会让人一遍遍试
        # 一个其实没错的口令。这是服务端状态问题，如实报成暂时不可用。
        return archive_error_response(
            ArchiveError(ErrorCode.REMOTE_UNAVAILABLE, retryable=True)
        )

    def _session_payload(token: BrowserSessionToken, totp_enabled: bool) -> dict[str, object]:
        return {
            "authenticated": True,
            "totp_enabled": totp_enabled,
            "idle_expires_at": token.idle_expires_at,
            "absolute_expires_at": token.absolute_expires_at,
        }

    @app.post("/api/v1/auth/bootstrap")
    async def bootstrap(request: Request, body: BootstrapRequest) -> Response:
        gate.guard_origin(request)
        source = request_source(request)
        try:
            token = await run_in_threadpool(
                store.complete_bootstrap,
                body.bootstrap_token,
                body.password,
                source=source,
            )
        except BrowserAuthThrottled as error:
            return _throttled_response(error)
        except BrowserSetupClosed:
            raise ArchiveError(ErrorCode.REMOTE_SETUP_CLOSED) from None
        except BrowserPasswordRejected as error:
            raise ArchiveError(
                ErrorCode.REMOTE_PASSWORD_WEAK, message=error.reason
            ) from None
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        if on_setup_session is not None:
            on_setup_session(token.session_id)
        if on_initialized is not None:
            on_initialized()
        device = await run_in_threadpool(gate.bind_device, request, token.session_id)
        response = JSONResponse(content=_session_payload(token, False))
        gate.attach(response, token, now=clock())
        gate.attach_device(response, device)
        return response

    @app.post("/api/v1/auth/sign-in")
    async def sign_in(request: Request, body: SignInRequest) -> Response:
        gate.guard_origin(request)
        source = request_source(request)
        try:
            token = await run_in_threadpool(
                store.login,
                body.password,
                body.second_factor,
                source=source,
            )
        except BrowserAuthThrottled as error:
            return _throttled_response(error)
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        # 先绑设备再下发会话：同一浏览器登出再登录，仍落回原设备、原来的锁。
        device = await run_in_threadpool(gate.bind_device, request, token.session_id)
        response = JSONResponse(
            content=_session_payload(token, store.state().totp_enabled)
        )
        gate.attach(response, token, now=clock())
        gate.attach_device(response, device)
        return response

    @app.post("/api/v1/auth/sign-out")
    def sign_out(request: Request) -> Response:
        gate.guard_origin(request)
        context = gate.session(request)
        if context is not None:
            store.logout(context.session_id)
        # 会话本就无效时也照样清 Cookie 并回 200：登出是个幂等的意愿，
        # 让它失败只会把一个已经过期的会话卡在浏览器里。设备 Cookie 不清：
        # 它标识的是这台浏览器，不是这次登录。
        response = JSONResponse(content={"authenticated": False})
        gate.clear(response)
        gate.clear_grant(response)
        return response

    @app.post("/api/v1/auth/client-info", status_code=204)
    def update_client_info(request: Request, body: BrowserClientInfoRequest) -> Response:
        gate.guard_origin(request)
        context = gate.require_identity(request)
        store.update_client_info(context.session_id, BrowserClientInfo(
            model=body.model, platform=body.platform, browser=body.browser,
            device_type=body.device_type,
        ))
        return Response(status_code=204)

    @app.post("/api/v1/auth/totp/enrollment")
    async def begin_totp(request: Request, body: TotpEnrollmentRequest) -> Response:
        gate.guard_origin(request)
        context = gate.require_fresh(request)
        try:
            enrollment = await run_in_threadpool(
                store.begin_totp_enrollment,
                context.session_id,
                body.password,
                source=request_source(request),
            )
        except BrowserAuthThrottled as error:
            return _throttled_response(error)
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        return JSONResponse(
            content={"secret": enrollment.secret, "otpauth_uri": enrollment.otpauth_uri}
        )

    @app.post("/api/v1/auth/totp/enable")
    def enable_totp(request: Request, body: TotpEnableRequest) -> dict[str, object]:
        gate.guard_origin(request)
        context = gate.require_fresh(request)
        try:
            codes = store.enable_totp(context.session_id, body.code)
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        return {"totp_enabled": True, "recovery_codes": list(codes)}

    @app.post("/api/v1/auth/totp/disable")
    async def disable_totp(request: Request, body: TotpDisableRequest) -> Response:
        gate.guard_origin(request)
        context = gate.require_fresh(request)
        source = request_source(request)
        try:
            await run_in_threadpool(
                store.disable_totp,
                context.session_id,
                body.password,
                source=source,
            )
        except BrowserAuthThrottled as error:
            return _throttled_response(error)
        except BrowserAuthRejected:
            raise ArchiveError(ErrorCode.REMOTE_SIGN_IN_REJECTED) from None
        return JSONResponse(content={"totp_enabled": False})


__all__ = [
    "DEVICE_COOKIE_NAME",
    "DEVICE_COOKIE_NAME_INSECURE",
    "REAUTH_COOKIE_NAME",
    "REAUTH_COOKIE_NAME_INSECURE",
    "SESSION_COOKIE_NAME",
    "BrowserGate",
    "archive_error_response",
    "browser_throttled_response",
    "origin_is_self",
    "register_browser_routes",
    "request_source",
]

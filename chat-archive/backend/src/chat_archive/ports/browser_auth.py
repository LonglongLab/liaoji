"""浏览器登录端口（仅远程端，PSU-2.29）。

设备令牌那一套是给同步客户端用的；这一套是给**人**用的：一次性引导令牌、
口令、可选 TOTP、服务端会话。两者互不相认——浏览器永远拿不到设备令牌，
设备令牌也进不了浏览器会话。

口令明文只在方法参数里出现一次，落盘只存 scrypt 摘要；会话密文只存 sha256；
TOTP 密钥必须可逆（要拿它算验证码），所以单独用数据目录里的主密钥封起来。
所有携带明文的值对象 `repr` 不含明文。

拒绝一律是同一个 `BrowserAuthRejected`：口令错、验证码错、引导令牌错、
会话不存在——**绝不区分具体原因**，与 `PairingRejected` 同一条防存在性泄漏的规矩。
唯一例外是用户自己刚输入的新口令太弱（`BrowserPasswordRejected`），那不是猜测，
是他需要知道的反馈。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol

from chat_archive.domain.client_info import BrowserClientInfo


class BrowserAuthRejected(Exception):
    """稳定错误：引导令牌/口令/验证码/会话不成立——不区分是哪一个不成立。"""


class BrowserAuthThrottled(Exception):
    """退避窗口内的尝试；`retry_after_seconds` 是还要等多久。

    退避**按来源**分桶。全局退避等于把「攻击者故意失败」变成一次对合法用户的
    锁定，而计划第 5 条明确不要账号锁定。
    """

    def __init__(self, retry_after_seconds: int) -> None:
        super().__init__("throttled")
        self.retry_after_seconds = retry_after_seconds


class BrowserPasswordRejected(Exception):
    """用户刚选的口令不符合强度要求；`reason` 会展示给他自己看。"""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class BrowserAuthStorageError(RuntimeError):
    """浏览器登录状态不可读写（主密钥丢失、库损坏）。"""


class BrowserSetupClosed(Exception):
    """引导入口已永久关闭：本部署已经初始化过了。"""


@dataclass(frozen=True)
class BrowserAuthState:
    """未认证的访客也能看到的两件事，仅此两件。"""

    initialized: bool
    totp_enabled: bool


@dataclass(frozen=True)
class BootstrapToken:
    """一次一用的引导令牌明文；只在生成的那一刻存在。"""

    value: str = field(repr=False)


@dataclass(frozen=True)
class BrowserSessionToken:
    """新会话的 Cookie 值；明文只下发这一次。"""

    session_id: str
    value: str = field(repr=False)
    idle_expires_at: int
    absolute_expires_at: int


@dataclass(frozen=True)
class BrowserSessionContext:
    """一次请求里认出的会话。

    `cursor_key` 是这条会话私有的分页签名密钥（不是任何可登录的凭据）：
    BFF 复用 `RemoteArchiveFacade` 的游标签名，那里要一个稳定的密钥，
    而**设备令牌绝不进浏览器这条路**，所以给它一个会话自己的。
    """

    session_id: str
    cursor_key: str = field(repr=False)
    totp_enabled: bool


@dataclass(frozen=True)
class BrowserSessionSummary:
    """设备与会话页能看到的会话元数据；不含任何密文。"""

    id: str
    created_at: int
    last_seen_at: int
    idle_expires_at: int
    absolute_expires_at: int
    client_info: BrowserClientInfo | None = field(default=None, repr=False)


@dataclass(frozen=True)
class TotpEnrollment:
    """绑定中的 TOTP；回填一个当前验证码之前不生效。"""

    secret: str = field(repr=False)
    otpauth_uri: str = field(repr=False)


class BrowserAuthStore(Protocol):
    def state(self) -> BrowserAuthState: ...

    def issue_bootstrap_token(self) -> BootstrapToken:
        """生成（或换发）一次性引导令牌。已初始化时抛 `BrowserSetupClosed`。"""
        ...

    def complete_bootstrap(
        self,
        bootstrap_token: str,
        password: str,
        *,
        source: str,
    ) -> BrowserSessionToken: ...

    def login(
        self,
        password: str,
        second_factor: str | None,
        *,
        source: str,
    ) -> BrowserSessionToken: ...

    def authenticate(
        self, cookie_value: str, *, touch: bool = True
    ) -> BrowserSessionContext | None: ...

    def touch(self, session_id: str) -> None: ...

    def confirm_password(self, password: str, *, source: str) -> None: ...

    def confirm_full_mfa(self, password: str, second_factor: str, *, source: str) -> None: ...

    def revoke_other_sessions(self, keep_session_id: str) -> int: ...

    def logout(self, session_id: str) -> None: ...

    def list_sessions(self) -> list[BrowserSessionSummary]: ...

    def update_client_info(self, session_id: str, info: BrowserClientInfo) -> None: ...

    def revoke_session(self, session_id: str) -> bool: ...

    def revoke_all_sessions(self) -> int: ...

    def begin_totp_enrollment(
        self,
        session_id: str,
        password: str,
        *,
        source: str,
    ) -> TotpEnrollment: ...

    def enable_totp(self, session_id: str, code: str) -> tuple[str, ...]:
        """回填一个当前验证码才真正开启，返回一次性恢复码。"""
        ...

    def disable_totp(self, session_id: str, password: str, *, source: str) -> None: ...


__all__ = [
    "BootstrapToken",
    "BrowserAuthRejected",
    "BrowserAuthState",
    "BrowserAuthStorageError",
    "BrowserAuthStore",
    "BrowserAuthThrottled",
    "BrowserPasswordRejected",
    "BrowserSessionContext",
    "BrowserSessionSummary",
    "BrowserSessionToken",
    "BrowserSetupClosed",
    "TotpEnrollment",
]

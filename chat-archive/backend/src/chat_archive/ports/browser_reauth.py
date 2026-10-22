"""可选日常再认证端口（仅自托管远程端，PSU-2.213；PSU-2.221 改为每台设备各自开启）。

原登录（口令 + 可选 TOTP）证明「你是这个账户」；这一层证明「刚刚在这台设备上
用本机登记过的系统验证（WebAuthn）或登录口令解过锁」。两者都成立，浏览器私人接口
才放行。

应用锁是**本机**的便捷锁（PSU-2.221）：每台浏览器有一个长期设备身份，开关、
通行密钥与 WebAuthn `user.id` 都挂在设备上；登录会话在登录时绑定到设备，锁定
判断只看会话绑定的设备，不看请求里带的设备 Cookie——偷走会话 Cookie 换一台机器
也还是锁着。系统验证没通过时，登录口令可以解锁本机。

落盘的东西：通行密钥只存 credential ID 与公钥；挑战一次一用；解锁授权（grant）
与设备 Cookie 只存 sha256 摘要，明文只经 `__Host-` HttpOnly Cookie 下发。**这里
没有任何可以把「已解锁」写回去的入口**——授权只能由一次真实验签或口令校验签发。

拒绝一律是同一个 `BrowserReauthRejected`：挑战过期、重放、跨会话、跨用途、
版本变了、签名不对——不区分具体原因。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal, Protocol

ReauthPurpose = Literal["register", "unlock", "manage"]


class BrowserReauthRejected(Exception):
    """挑战/验证/授权不成立；不区分是哪一个不成立。"""


class BrowserReauthConflict(Exception):
    """状态前提不满足：版本不符、没有可用凭据、删除最后一枚凭据、本机未开启等。"""


class BrowserReauthFreshRequired(Exception):
    """安全管理操作需要用途专属的新鲜验证，而本会话没有。"""


@dataclass(frozen=True)
class ReauthPolicy:
    """一台设备（浏览器）上的应用锁开关。"""

    enabled: bool
    policy_version: int
    user_handle: bytes = field(repr=False)


@dataclass(frozen=True)
class ReauthSessionState:
    """一条登录会话在再认证层上的事实（本机视角）；不含任何密文。"""

    enabled: bool
    policy_version: int
    unlocked: bool
    unlocked_until: int | None
    fresh_until: int | None
    passkey_count: int
    migration_notice: bool = False


@dataclass(frozen=True)
class DeviceCookie:
    """设备 Cookie 的明文；只在需要（重新）下发时出现。"""

    value: str = field(repr=False)
    created: bool


@dataclass(frozen=True)
class StoredPasskey:
    id: str
    credential_id: bytes = field(repr=False)
    public_key: bytes = field(repr=False)
    sign_count: int
    transports: tuple[str, ...]
    backed_up: bool
    label: str
    created_at: int
    last_used_at: int | None


@dataclass(frozen=True)
class PasskeySummary:
    """管理页看得到的元数据。"""

    id: str
    label: str
    created_at: int
    last_used_at: int | None
    backed_up: bool


@dataclass(frozen=True)
class ReauthChallenge:
    id: str
    purpose: ReauthPurpose
    challenge: bytes = field(repr=False)
    policy_version: int = 0
    lock_version: int = 0
    expires_at: int = 0


@dataclass(frozen=True)
class ReauthGrantToken:
    """新解锁授权的 Cookie 值；明文只下发这一次。"""

    value: str = field(repr=False)
    expires_at: int


class BrowserReauthStore(Protocol):
    @property
    def challenge_seconds(self) -> int: ...

    @property
    def device_cookie_seconds(self) -> int: ...

    def bind_device(self, session_id: str, cookie_value: str | None) -> DeviceCookie | None:
        """把会话绑定到浏览器带来的设备；没有可用设备就新建一台。

        已绑定的会话不改绑；返回值不为 None 时由调用方（重新）下发设备 Cookie。
        """
        ...

    def lock_enabled(self, session_id: str) -> bool:
        """会话绑定的那台设备是否开启了应用锁。"""
        ...

    def policy(self, session_id: str) -> ReauthPolicy: ...

    def user_handle(self, session_id: str) -> bytes:
        """本设备的随机不透明 WebAuthn user.id。"""
        ...

    def session_state(self, session_id: str, grant_value: str | None) -> ReauthSessionState: ...

    def grant_valid(self, session_id: str, grant_value: str | None) -> bool: ...

    def list_passkeys(self, session_id: str) -> list[StoredPasskey]: ...

    def create_challenge(
        self, session_id: str, purpose: ReauthPurpose, challenge: bytes
    ) -> ReauthChallenge: ...

    def consume_challenge(
        self, session_id: str, challenge_id: str, purposes: tuple[ReauthPurpose, ...]
    ) -> ReauthChallenge: ...

    def record_failure(self, session_id: str) -> None: ...

    def guard_backoff(self, session_id: str) -> None: ...

    def add_passkey(
        self,
        session_id: str,
        challenge: ReauthChallenge,
        *,
        credential_id: bytes,
        public_key: bytes,
        sign_count: int,
        transports: tuple[str, ...],
        backed_up: bool,
        label: str,
    ) -> PasskeySummary: ...

    def complete_verification(
        self,
        session_id: str,
        challenge: ReauthChallenge,
        *,
        passkey_id: str,
        new_sign_count: int,
        backed_up: bool,
    ) -> ReauthGrantToken | None: ...

    def unlock_with_password(self, session_id: str) -> ReauthGrantToken:
        """调用方已校验过登录口令：解锁本会话并打开管理用途的新鲜窗口。"""
        ...

    def mark_password_fresh(self, session_id: str) -> None:
        """调用方已校验过登录口令：只打开管理用途的新鲜窗口。"""
        ...

    def lock(self, session_id: str) -> None: ...

    def has_fresh(self, session_id: str) -> bool: ...

    def set_policy(
        self, session_id: str, *, enabled: bool, expected_version: int
    ) -> tuple[ReauthPolicy, ReauthGrantToken | None]: ...

    def delete_passkey(self, session_id: str, passkey_id: str) -> bool: ...

    def dismiss_notice(self, session_id: str) -> None: ...

    def reset(self) -> None: ...


__all__ = [
    "BrowserReauthConflict",
    "BrowserReauthFreshRequired",
    "BrowserReauthRejected",
    "BrowserReauthStore",
    "DeviceCookie",
    "PasskeySummary",
    "ReauthChallenge",
    "ReauthGrantToken",
    "ReauthPolicy",
    "ReauthPurpose",
    "ReauthSessionState",
    "StoredPasskey",
]

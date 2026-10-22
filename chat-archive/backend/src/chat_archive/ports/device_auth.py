"""服务端设备鉴权/配对端口（自建远程服务）。

令牌与配对码明文只在短生命周期值对象中出现，`repr` 不含明文；落盘只存 sha256。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from chat_archive.domain.client_info import SyncClientInfo


class PairingRejected(Exception):
    """稳定错误：配对码不存在/过期/错误/超限——绝不区分具体原因，防存在性泄漏。"""


class PairingThrottled(Exception):
    """Authenticated issuer exceeded its durable per-session issuance budget."""


@dataclass(frozen=True)
class BearerToken:
    device_id: str
    secret: str = field(repr=False)


@dataclass(frozen=True)
class AuthContext:
    device_id: str
    archive_id: str


@dataclass(frozen=True)
class PairingCodeIssued:
    pairing_code: str = field(repr=False)
    pairing_code_id: str
    expires_at: int


@dataclass(frozen=True)
class PairedDevice:
    archive_id: str
    device_id: str
    device_token: str = field(repr=False)


@dataclass(frozen=True)
class ArchiveSummary:
    """归档本身的非敏感描述；浏览器 BFF 靠它认出本部署托管着哪个归档。"""

    id: str
    display_name: str
    created_at: int


@dataclass(frozen=True)
class DeviceListItem:
    id: str
    display_name: str
    status: str
    created_at: int
    last_seen_at: int | None
    revoked_at: int | None
    last_local_sequence: int
    client_info: SyncClientInfo | None = field(default=None, repr=False)


class DeviceAuthStore(Protocol):
    def provision_archive(
        self, archive_id: str, display_name: str, device_name: str
    ) -> PairedDevice: ...

    def authenticate(self, token: BearerToken) -> AuthContext | None: ...

    def update_client_info(self, context: AuthContext, info: SyncClientInfo) -> None: ...

    def issue_pairing_code(self, archive_id: str, actor_device_id: str) -> PairingCodeIssued: ...

    def redeem_pairing_code(self, pairing_code: str, device_name: str) -> PairedDevice: ...

    def revoke_device(
        self, archive_id: str, actor_device_id: str, target_device_id: str
    ) -> bool: ...

    def revoke_all(self, archive_id: str, actor_device_id: str) -> int: ...

    def rotate_token(self, archive_id: str, device_id: str) -> str: ...

    def list_devices(self, archive_id: str) -> list[DeviceListItem]: ...

    def list_archives(self) -> list[ArchiveSummary]: ...


@runtime_checkable
class BrowserPairingStore(Protocol):
    """Self-hosted only; browser actors never become synthetic sync devices."""

    def ensure_browser_archive(self) -> str: ...

    def issue_browser_pairing_code(self, session_id: str) -> PairingCodeIssued: ...

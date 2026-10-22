"""远程设备凭据端口（FR-REMOTE-007/008）。

设备令牌只经受保护的存储落盘；`DeviceToken` 不实现字段 repr，避免进入日志、
异常字符串或调试输出。业务代码持有令牌的时间应尽可能短。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class RemoteDeviceProfile:
    """非敏感的远程连接描述；可安全写入普通配置与日志。"""

    base_url: str
    archive_id: str
    device_id: str


@dataclass(repr=False, frozen=True)
class DeviceToken:
    """短生命周期令牌载体；repr 不包含令牌值。"""

    value: str


class RemoteCredentialStore(Protocol):
    """单一远程档案的设备凭据存储。"""

    def get(self) -> tuple[RemoteDeviceProfile, DeviceToken] | None: ...

    def put(self, profile: RemoteDeviceProfile, token: DeviceToken) -> None: ...

    def delete(self) -> None: ...

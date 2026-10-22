"""Portable identity collection for outbound sync clients, without reading account data."""

from __future__ import annotations

import platform
import socket
from collections.abc import Callable

from chat_archive.domain.client_info import SyncClientInfo, valid_display_text


def collect_sync_client_info(
    *, hostname: Callable[[], str] = socket.gethostname, system: Callable[[], str] = platform.system,
) -> SyncClientInfo | None:
    try:
        name, operating_system = hostname().strip(), system().strip()
    except OSError:
        return None
    if not valid_display_text(name, 128) or not valid_display_text(operating_system, 32):
        return None
    return SyncClientInfo(name, operating_system, "chat-archive")

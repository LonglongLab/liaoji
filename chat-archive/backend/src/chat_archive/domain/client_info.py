"""Bounded display metadata. Never used as authentication or physical-device identity."""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Literal, cast

SYNC_CLIENT_HEADER = "X-Chat-Archive-Client"
MAX_CLIENT_HEADER_LENGTH = 2048
DeviceKind = Literal["mobile", "tablet", "desktop", "unknown"]


def valid_display_text(value: str, limit: int) -> bool:
    return bool(value.strip()) and len(value) <= limit and not any(
        ord(character) < 32 or ord(character) == 127
        or 0xD800 <= ord(character) <= 0xDFFF for character in value
    )


@dataclass(frozen=True)
class BrowserClientInfo:
    model: str | None = field(repr=False)
    platform: str | None = field(repr=False)
    browser: str | None = field(repr=False)
    device_type: DeviceKind


@dataclass(frozen=True)
class SyncClientInfo:
    computer_name: str = field(repr=False)
    platform: str = field(repr=False)
    client_name: str = field(repr=False)

    def header_value(self) -> str:
        # ASCII JSON works with HTTP headers even when a computer name contains CJK.
        return json.dumps(asdict(self), ensure_ascii=True, separators=(",", ":"))


def parse_sync_client_info(header: str | None) -> SyncClientInfo | None:
    if not header or len(header) > MAX_CLIENT_HEADER_LENGTH:
        return None
    try:
        value: object = json.loads(header)
    except (ValueError, RecursionError):
        return None
    if not isinstance(value, dict):
        return None
    body = cast(dict[object, object], value)
    if set(body) != {"computer_name", "platform", "client_name"}:
        return None
    name, platform, client = (body.get(key) for key in ("computer_name", "platform", "client_name"))
    if not isinstance(name, str) or not valid_display_text(name, 128):
        return None
    if not isinstance(platform, str) or not valid_display_text(platform, 32):
        return None
    if not isinstance(client, str) or not valid_display_text(client, 32):
        return None
    return SyncClientInfo(name.strip(), platform.strip(), client.strip())

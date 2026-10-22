"""一份在线分享草稿长什么样、多大算太大（PSU-2.181）。

两端都用这一份：本机在把草稿交给远端**之前**先判一次（超限当场说，不必往返一趟），
远端收到后**再判一次**（它不能相信任何客户端）。所以它住在两端都能导入的地方，
不在 `server/` 也不在 `local/`。

## 快照只收白名单

快照由客户端组好（隐藏名字也在客户端做掉），这里按白名单**剥掉**其余一切，而不是
拒收：前端加了一个字段不该让分享整个失败，但那个字段也不该悄悄进到服务器上的副本里。
白名单之外的东西——`sender_uid`、会话 id、账号 id、源坐标、应用卡片里转账双方的
uid——对方页面用不到，就不该出现在一份任何人拿着链接都能读的副本里。

白名单里的字段类型不对则拒收（`QUERY_INVALID`）：那不是「多了点东西」，是形状错了。
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import cast

MAX_MESSAGES = 500
MAX_SNAPSHOT_BYTES = 2 * 1024 * 1024
MAX_OBJECT_BYTES = 200 * 1024 * 1024
MAX_TOTAL_BYTES = 1024 * 1024 * 1024
MAX_TITLE_CHARS = 200
MAX_CARD_ITEMS = 50
# 用户可选的失效时间（原型第 1 块）；`None` 是永久。
EXPIRY_CHOICES: tuple[int | None, ...] = (1, 7, 30, None)

SNAPSHOT_VERSION = 1

_MESSAGE_TYPES = frozenset(
    {"text", "image", "audio", "video", "file", "link", "quote", "emoji", "app", "system", "unknown"}
)
_ATTACHMENT_KINDS = frozenset({"image", "emoji", "audio", "video", "file", "unknown"})
_ATTACHMENT_ID = re.compile(r"^[A-Za-z0-9_.:-]{1,128}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ShareDraftError(ValueError):
    """草稿不合格。`code` 是两端共用的稳定代号。"""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


QUERY_INVALID = "QUERY_INVALID"
SHARE_LIMIT_EXCEEDED = "SHARE_LIMIT_EXCEEDED"


@dataclass(frozen=True)
class ShareAttachmentDecl:
    """草稿声明要带上的一个附件：之后要按这个大小传上来。"""

    attachment_id: str
    size: int
    mime_type: str | None
    # 要带上的那份字节的 SHA-256（PSU-2.184）。远端归档里有同样的内容就在对象存储里
    # 复制，不必再传；没有这个键的（旧客户端）一律要传。
    sha256: str | None = None


@dataclass(frozen=True)
class ShareDraft:
    title: str
    message_count: int
    snapshot_json: str
    expires_in_days: int | None
    attachments: tuple[ShareAttachmentDecl, ...]

    def to_payload(self) -> dict[str, object]:
        """规范化之后的样子，本机就把这一份交给远端。"""
        return {
            "title": self.title,
            "expires_in_days": self.expires_in_days,
            "snapshot": json.loads(self.snapshot_json),
            "attachments": [_declared_payload(item) for item in self.attachments],
        }


def _declared_payload(item: ShareAttachmentDecl) -> dict[str, object]:
    payload: dict[str, object] = {
        "attachment_id": item.attachment_id,
        "size": item.size,
        "mime_type": item.mime_type,
    }
    if item.sha256 is not None:
        payload["sha256"] = item.sha256
    return payload


def _invalid() -> ShareDraftError:
    return ShareDraftError(QUERY_INVALID)


def _mapping(value: object) -> Mapping[str, object]:
    if not isinstance(value, Mapping):
        raise _invalid()
    return cast(Mapping[str, object], value)


def _sequence(value: object) -> Sequence[object]:
    if not isinstance(value, list):
        raise _invalid()
    return cast(list[object], value)


def _string(value: object, *, maximum: int, allow_empty: bool = True) -> str:
    if not isinstance(value, str) or len(value) > maximum:
        raise _invalid()
    if not allow_empty and not value.strip():
        raise _invalid()
    return value


def _optional_string(value: object, *, maximum: int) -> str | None:
    return None if value is None else _string(value, maximum=maximum)


def _optional_sha256(value: object) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or _SHA256.fullmatch(value) is None:
        raise _invalid()
    return value


def _integer(value: object, *, minimum: int = 0) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise _invalid()
    return value


def _optional_integer(value: object) -> int | None:
    return None if value is None else _integer(value)


def _optional_bool(value: object) -> bool | None:
    if value is None or isinstance(value, bool):
        return value
    raise _invalid()


def _choice(value: object, allowed: frozenset[str]) -> str:
    if not isinstance(value, str) or value not in allowed:
        raise _invalid()
    return value


def _quote(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _mapping(value)
    return {
        "sender": _optional_string(source.get("sender"), maximum=128),
        "type": _choice(source.get("type", "text"), _MESSAGE_TYPES),
        "text": _string(source.get("text", ""), maximum=20_000),
    }


def _card(value: object) -> dict[str, object] | None:
    if value is None:
        return None
    source = _mapping(value)
    items = _sequence(source.get("items", []))
    if len(items) > MAX_CARD_ITEMS:
        raise ShareDraftError(SHARE_LIMIT_EXCEEDED)
    return {
        "kind": _string(source.get("kind"), maximum=32, allow_empty=False),
        "title": _string(source.get("title", ""), maximum=2_000),
        "subtitle": _optional_string(source.get("subtitle"), maximum=4_000),
        "source_name": _optional_string(source.get("source_name"), maximum=200),
        "url": _optional_string(source.get("url"), maximum=4_000),
        "items": [
            {
                "sender": _optional_string(item.get("sender"), maximum=128),
                "kind": _string(item.get("kind", "text"), maximum=32),
                "text": _optional_string(item.get("text"), maximum=20_000),
            }
            for item in (_mapping(raw) for raw in items)
        ],
    }


def _attachment(value: object) -> dict[str, object]:
    source = _mapping(value)
    attachment_id = _string(source.get("attachment_id"), maximum=128)
    if _ATTACHMENT_ID.fullmatch(attachment_id) is None:
        raise _invalid()
    return {
        "attachment_id": attachment_id,
        "kind": _choice(source.get("kind"), _ATTACHMENT_KINDS),
        "size": _optional_integer(source.get("size")),
        "duration_ms": _optional_integer(source.get("duration_ms")),
        "file_name": _optional_string(source.get("file_name"), maximum=260),
        "mime_type": _optional_string(source.get("mime_type"), maximum=128),
    }


def _message(value: object) -> dict[str, object]:
    source = _mapping(value)
    return {
        "time": _integer(source.get("time")),
        "sender": _optional_string(source.get("sender"), maximum=128),
        "is_self": _optional_bool(source.get("is_self")),
        "type": _choice(source.get("type"), _MESSAGE_TYPES),
        "text": _string(source.get("text", ""), maximum=100_000),
        "quote": _quote(source.get("quote")),
        "card": _card(source.get("card")),
        "attachments": [_attachment(item) for item in _sequence(source.get("attachments", []))],
        "transcript": _optional_string(source.get("transcript"), maximum=20_000),
    }


def sanitize_snapshot(value: object) -> dict[str, object]:
    """按白名单重建快照：白名单外的键一律剥掉，白名单内类型不对就拒。"""
    source = _mapping(value)
    if source.get("version", SNAPSHOT_VERSION) != SNAPSHOT_VERSION:
        raise _invalid()
    raw_messages = _sequence(source.get("messages"))
    if not raw_messages:
        raise _invalid()
    if len(raw_messages) > MAX_MESSAGES:
        raise ShareDraftError(SHARE_LIMIT_EXCEEDED)
    names_hidden = source.get("names_hidden", False)
    if not isinstance(names_hidden, bool):
        raise _invalid()
    return {
        "version": SNAPSHOT_VERSION,
        "names_hidden": names_hidden,
        "messages": [_message(item) for item in raw_messages],
    }


def parse_share_draft(payload: object) -> ShareDraft:
    source = _mapping(payload)
    title = _string(source.get("title"), maximum=MAX_TITLE_CHARS, allow_empty=False)
    expires_in_days = source.get("expires_in_days")
    if isinstance(expires_in_days, bool) or expires_in_days not in EXPIRY_CHOICES:
        raise _invalid()
    snapshot = sanitize_snapshot(source.get("snapshot"))
    snapshot_json = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"))
    if len(snapshot_json.encode("utf-8")) > MAX_SNAPSHOT_BYTES:
        raise ShareDraftError(SHARE_LIMIT_EXCEEDED)
    declared: list[ShareAttachmentDecl] = []
    seen: set[str] = set()
    total = 0
    for raw in _sequence(source.get("attachments", [])):
        item = _mapping(raw)
        attachment_id = _string(item.get("attachment_id"), maximum=128)
        if _ATTACHMENT_ID.fullmatch(attachment_id) is None or attachment_id in seen:
            raise _invalid()
        seen.add(attachment_id)
        size = _integer(item.get("size"))
        if size > MAX_OBJECT_BYTES:
            raise ShareDraftError(SHARE_LIMIT_EXCEEDED)
        total += size
        declared.append(
            ShareAttachmentDecl(
                attachment_id=attachment_id,
                size=size,
                mime_type=_optional_string(item.get("mime_type"), maximum=128),
                sha256=_optional_sha256(item.get("sha256")),
            )
        )
    if total > MAX_TOTAL_BYTES:
        raise ShareDraftError(SHARE_LIMIT_EXCEEDED)
    messages = cast(list[object], snapshot["messages"])
    return ShareDraft(
        title=title,
        message_count=len(messages),
        snapshot_json=snapshot_json,
        expires_in_days=cast("int | None", expires_in_days),
        attachments=tuple(declared),
    )


__all__ = [
    "EXPIRY_CHOICES",
    "MAX_MESSAGES",
    "MAX_OBJECT_BYTES",
    "MAX_SNAPSHOT_BYTES",
    "MAX_TOTAL_BYTES",
    "SNAPSHOT_VERSION",
    "ShareAttachmentDecl",
    "ShareDraft",
    "ShareDraftError",
    "parse_share_draft",
    "sanitize_snapshot",
]

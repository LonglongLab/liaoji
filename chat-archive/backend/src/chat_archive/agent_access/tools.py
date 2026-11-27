"""Shared tool contract and output policy for parsed archive readers."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from functools import lru_cache
from io import BytesIO
from typing import Literal, Protocol, cast, runtime_checkable
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from PIL import Image
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from chat_archive.agent_access.policy import Grant, Identifier
from chat_archive.domain.app_card import AppCard

Source = Literal["local", "remote"]
SearchKind = Literal["message", "conversation", "person", "file"]
PrepareMode = Literal["none", "preview", "original"]
Document = dict[str, object]
TokenPayload = TypeAdapter(list[object])


class ToolError(Exception):
    def __init__(self, code: str) -> None:
        self.code = code


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class AccessContext(Arguments):
    requested_conversation_ids: list[Identifier] = Field(default_factory=list, max_length=10)


class Search(Arguments):
    kind: SearchKind
    query: str = Field(min_length=1, max_length=200)
    conversation_id: Identifier | None = None
    date_from: int | None = Field(default=None, ge=0)
    date_to: int | None = Field(default=None, ge=0)
    cursor: str | None = Field(default=None, max_length=4096)
    limit: int = Field(default=15, ge=1, le=30)


class MessageRef(Arguments):
    conversation_id: Identifier
    message_id: Identifier


class Context(Arguments):
    conversation_id: Identifier
    message_id: Identifier | None = None
    timestamp: int | None = Field(default=None, ge=0)
    before: int = Field(default=5, ge=0, le=15)
    after: int = Field(default=5, ge=0, le=15)

    @model_validator(mode="after")
    def one_anchor(self) -> Context:
        if (self.message_id is None) == (self.timestamp is None):
            raise ValueError("Provide one anchor")
        return self


class Messages(Arguments):
    messages: list[MessageRef] = Field(min_length=1, max_length=20)
    cursor: str | None = Field(default=None, max_length=4096)

    @model_validator(mode="after")
    def one_continuation(self) -> Messages:
        if self.cursor is not None and len(self.messages) != 1:
            raise ValueError("A continuation identifies one message")
        return self


class MessagePosition(Arguments):
    ref: MessageRef
    digest: str
    text_offset: int = Field(default=0, ge=0)
    forward_index: int = Field(default=0, ge=0)
    forward_offset: int = Field(default=0, ge=0)
    attachment_offset: int = Field(default=0, ge=0)


class FileRef(MessageRef):
    attachment_id: Identifier


class ReadFile(FileRef):
    mode: Literal["info", "text", "preview"] = "info"
    cursor: str | None = Field(default=None, max_length=4096)
    image_variant: Literal["auto", "original"] = "auto"

    @model_validator(mode="after")
    def check_mode(self) -> ReadFile:
        if self.cursor is not None and self.mode != "text":
            raise ValueError("A file continuation requires text mode")
        if self.image_variant == "original" and self.mode != "preview":
            raise ValueError("An image variant requires preview mode")
        return self


class FilePosition(Arguments):
    digest: str
    offset: int = Field(gt=0)


class Downloads(Arguments):
    files: list[FileRef] = Field(min_length=1, max_length=5)


class SyncConversation(Arguments):
    conversation_id: Identifier


@dataclass(frozen=True)
class File:
    ref: FileRef
    name: str | None
    mime_type: str | None
    size: int | None
    status: str
    variant: str | None
    open_bytes: Callable[[], Iterator[bytes]] | None = None
    open_preview: Callable[[], Iterator[bytes]] | None = None
    prepare_attempted: bool = False

    @property
    def open_original(self) -> Callable[[], Iterator[bytes]] | None:
        return None if self.variant == "thumbnail" else self.open_bytes

    @property
    def original_status(self) -> str:
        if self.open_original is not None:
            return "available"
        if self.open_preview is not None or (self.variant == "thumbnail" and self.open_bytes is not None):
            return "thumbnail_only"
        if self.status in {"not_ready", "not_synced", "discovered", "locating", "decrypting",
                           "transcoding", "evicted"}:
            return "not_ready"
        return "unavailable"

    def metadata(self) -> Document:
        data: Document = {
            "ref": self.ref.model_dump(),
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "status": self.status,
            "original_status": self.original_status,
            "variant": self.variant,
        }
        if self.prepare_attempted:
            data["prepare_attempted"] = True
        return data


@dataclass(frozen=True)
class SearchPage:
    items: list[Document]
    next_position: str | None = None
    status: str = "ready"


class ArchiveReader(Protocol):
    @property
    def source(self) -> Source: ...

    search_kinds: tuple[SearchKind, ...]

    def freshness(self, grant: Grant) -> int | None: ...
    def search(self, grant: Grant, args: Search, position: str | None) -> SearchPage: ...
    def message(self, grant: Grant, ref: MessageRef) -> Document | None: ...
    def context(self, grant: Grant, args: Context) -> list[Document] | None: ...
    def file(self, grant: Grant, ref: FileRef) -> File | None: ...


@runtime_checkable
class MediaPrepareReader(Protocol):
    @property
    def supports_media_prepare(self) -> bool: ...

    def file(self, grant: Grant, ref: FileRef, *, prepare: PrepareMode = "none") -> File | None: ...


@runtime_checkable
class ConversationSyncReader(Protocol):
    @property
    def supports_conversation_sync(self) -> bool: ...

    def sync_conversation(
        self,
        grant: Grant,
        conversation_id: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> Document: ...


def message_document(
    ref: MessageRef,
    *,
    created_at: int,
    message_type: str | None,
    sender_id: str | None,
    sender_name: str | None,
    text: str,
    reply: str | None,
    card: AppCard | None,
    attachments: list[Document],
) -> Document:
    forwarded = None
    if card is not None and card.items:
        pending = [(item, [index]) for index, item in reversed(list(enumerate(card.items)))]
        items: list[Document] = []
        source_truncated = card.truncated
        while pending:
            item, path = pending.pop()
            items.append({"path": path, "sender": item.sender, "created_at": item.created_at,
                          "kind": item.kind, "text": item.text or "", "name": item.file_name})
            if item.nested is not None:
                source_truncated = source_truncated or item.nested.truncated
                pending.extend((child, [*path, index]) for index, child in reversed(list(enumerate(item.nested.items))))
        forwarded = {
            "items": items, "source_truncated": source_truncated,
        }
    return {
        "ref": ref.model_dump(), "created_at": created_at, "message_type": message_type,
        "sender_id": sender_id, "sender_name": sender_name, "text": text,
        "reply": {"message_id": reply, "conversation_id": ref.conversation_id} if reply else None,
        "forwarded": forwarded, "attachments": attachments,
    }


@dataclass(frozen=True)
class ToolAnnotations:
    read_only: bool = True
    destructive: bool = False
    idempotent: bool = True
    open_world: bool = False

    def to_mcp(self) -> Document:
        return {
            "readOnlyHint": self.read_only,
            "destructiveHint": self.destructive,
            "idempotentHint": self.idempotent,
            "openWorldHint": self.open_world,
        }


@dataclass(frozen=True)
class Tool:
    description: str
    arguments: type[Arguments]
    annotations: ToolAnnotations = ToolAnnotations()


TOOLS = {
    "liaoji_get_access_context": Tool(
        "Read the effective scope. If the user requests additional known chat IDs, "
        "include them to obtain one link for the user to review.", AccessContext,
    ),
    "liaoji_search": Tool("Find chat records and references for direct reading.", Search),
    "liaoji_get_context": Tool("Read a bounded window within one chat.", Context),
    "liaoji_get_messages": Tool(
        "Read messages by stable references. For more content call with one message ref and its next_cursor. "
        "continuation_complete marks the end of traversal; a final fragment is not the entire message.", Messages),
    "liaoji_read_file": Tool(
        "Read attachment info, UTF-8 text, or a validated PNG/JPEG preview. For complete text, "
        "pass next_cursor as cursor with the same attachment and mode=text until "
        "continuation_complete. For original image detail request mode=preview and "
        "image_variant=original. In local desktop mode, explicit preview, original, text, or "
        "download requests prepare uncached source artifacts on demand when available; mode=info "
        "reads current status only. If the original is too large, request a download. "
        "Report unavailable, thumbnail_only, or corrupt content when it limits the answer.",
        ReadFile,
    ),
    "liaoji_get_downloads": Tool(
        "Get authenticated download URLs for available archived originals. If no URL is returned, "
        "check original_status and status before relying on the attachment.",
        Downloads,
    ),
}

SYNC_CONVERSATION_TOOL_NAME = "liaoji_sync_conversation"
SYNC_CONVERSATION_TOOL = Tool(
    "Incrementally refresh ONE known authorized conversation from local WeChat storage. "
    "Always search or read the archive first; call this ONLY when the target information is "
    "missing AND you already know the specific conversation_id, then re-query that chat. "
    "If still missing or the conversation is unknown, ask the user for details instead of "
    "scanning or polling multiple conversations.",
    SyncConversation,
    ToolAnnotations(read_only=False, destructive=False, idempotent=True, open_world=False),
)

TEXT_LIMIT = 8_192
TEXT_FILE_LIMIT = 1_048_576
IMAGE_LIMIT = 131_072
IMAGE_PIXEL_LIMIT = 4_000_000
_TEXT_MIME_TYPES = {"text/plain", "text/csv", "text/markdown"}
_RASTER_FORMATS = {
    "image/png": ("PNG", b"\x89PNG\r\n\x1a\n"),
    "image/jpeg": ("JPEG", b"\xff\xd8\xff"),
}
_PNG_END = b"\x00\x00\x00\x00IEND\xaeB`\x82"


def _read_bounded(factory: Callable[[], Iterator[bytes]], limit: int) -> bytes:
    data = bytearray()
    stream = factory()
    try:
        for chunk in stream:
            data.extend(chunk[:limit + 1 - len(data)])
            if len(data) > limit:
                break
    finally:
        close = getattr(stream, "close", None)
        if close is not None:
            close()
    return bytes(data)


def _image_status(data: bytes, format_name: str) -> str:
    # Pillow verifies the PNG chunks up to IEND but skips its final checksum.
    if format_name == "PNG" and not data.endswith(_PNG_END):
        return "corrupt"
    try:
        with Image.open(BytesIO(data), formats=[format_name]) as image:
            if image.width * image.height > IMAGE_PIXEL_LIMIT:
                return "too_large"
            if getattr(image, "is_animated", False):
                return "unsupported_format"
            image.verify()
        with Image.open(BytesIO(data), formats=[format_name]) as image:
            image.load()
    except Image.DecompressionBombError:
        return "too_large"
    except (OSError, SyntaxError, ValueError, EOFError):
        return "corrupt"
    return "ready"


def _file_text_purpose(ref: FileRef) -> str:
    return "file-text:" + hashlib.sha256(ref.model_dump_json().encode()).hexdigest()


def query_deadline() -> Callable[[], int]:
    expires = time.monotonic() + 3
    return lambda: int(time.monotonic() >= expires)


def _permitted(grant: Grant, conversation_id: str) -> bool:
    return grant.scope is not None and grant.scope.allows(conversation_id)


def _make_review_ticket_cache(cipher: Fernet) -> Callable[[str, int, tuple[str, ...], int], str]:
    @lru_cache(maxsize=512)
    def _review_ticket(
        grant_id: str, version: int, ids: tuple[str, ...], _window: int
    ) -> str:
        return cipher.encrypt(json.dumps([grant_id, version, ids]).encode()).decode()

    return _review_ticket


class QueryTools:
    def __init__(
        self, reader: ArchiveReader,
        additions: Callable[[Grant, tuple[str, ...]], tuple[str, ...]],
    ) -> None:
        self.reader = reader
        self.additions = additions
        self._cipher = Fernet(Fernet.generate_key())
        self._cached_review_ticket = _make_review_ticket_cache(self._cipher)

    def _review_ticket(
        self, grant_id: str, version: int, ids: tuple[str, ...], _window: int
    ) -> str:
        return self._cached_review_ticket(grant_id, version, ids, _window)

    def proposed_additions(self, ticket: str) -> tuple[str, int, tuple[str, ...]]:
        if len(ticket) > 8192 or not ticket.isascii():
            raise ToolError("invalid_or_stale_reference")
        try:
            raw = self._cipher.decrypt(ticket, ttl=600)
            return TypeAdapter(tuple[str, int, tuple[str, ...]]).validate_json(raw)
        except (ValueError, InvalidToken) as exc:
            raise ToolError("invalid_or_stale_reference") from exc

    def _seal(self, purpose: str, connection_id: str, grant: Grant, value: object) -> str:
        payload = [purpose, self.reader.source, connection_id, grant.id, grant.version, value]
        return self._cipher.encrypt(json.dumps(payload).encode()).decode("ascii")

    def _unseal(self, ticket: str, purpose: str, connection_id: str, grant: Grant) -> object:
        if len(ticket) > 4096 or not ticket.isascii():
            raise ToolError("invalid_or_stale_reference")
        try:
            parts = TokenPayload.validate_json(self._cipher.decrypt(ticket))
        except (ValueError, InvalidToken) as exc:
            raise ToolError("invalid_or_stale_reference") from exc
        if len(parts) != 6 or parts[:5] != [
            purpose, self.reader.source, connection_id, grant.id, grant.version,
        ]:
            raise ToolError("invalid_or_stale_reference")
        return parts[5]

    def _can_prepare_media(self) -> bool:
        return isinstance(self.reader, MediaPrepareReader) and self.reader.supports_media_prepare is True

    def _can_sync_conversation(self) -> bool:
        return isinstance(self.reader, ConversationSyncReader) and self.reader.supports_conversation_sync is True

    def available_tools(self) -> dict[str, Tool]:
        if self._can_sync_conversation():
            return {**TOOLS, SYNC_CONVERSATION_TOOL_NAME: SYNC_CONVERSATION_TOOL}
        return dict(TOOLS)

    def get_file(self, grant: Grant, ref: FileRef, *, prepare: PrepareMode = "none") -> File | None:
        if not _permitted(grant, ref.conversation_id):
            return None
        if (
            prepare != "none"
            and isinstance(self.reader, MediaPrepareReader)
            and self.reader.supports_media_prepare is True
        ):
            return cast(MediaPrepareReader, self.reader).file(grant, ref, prepare=prepare)
        return self.reader.file(grant, ref)

    def _message_page(
        self, message: Document, connection_id: str, grant: Grant,
        position: MessagePosition | None = None,
    ) -> Document:
        ref = MessageRef.model_validate(message["ref"])
        digest = hashlib.sha256(json.dumps(message, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
        if position is None:
            position = MessagePosition(ref=ref, digest=digest)
        if position.ref != ref or position.digest != digest:
            raise ToolError("invalid_or_stale_reference")
        text = TypeAdapter(str).validate_python(message["text"])
        forward = TypeAdapter[dict[str, object] | None](dict[str, object] | None).validate_python(message.get("forwarded"))
        items = TypeAdapter(list[Document]).validate_python(forward.get("items", []) if forward else [])
        attachments = TypeAdapter(list[Document]).validate_python(message.get("attachments", []))
        if position.text_offset > len(text) or position.forward_index > len(items) or position.attachment_offset > len(attachments):
            raise ToolError("invalid_or_stale_reference")
        text_end = min(len(text), position.text_offset + 4_000)
        index, offset = position.forward_index, position.forward_offset
        forwarded: list[Document] = []
        while index < len(items) and len(forwarded) < 10:
            item = items[index]
            content = TypeAdapter(str).validate_python(item["text"])
            if offset > len(content):
                raise ToolError("invalid_or_stale_reference")
            end = min(len(content), offset + 500)
            forwarded.append({**item, "text": content[offset:end],
                              "text_range": [offset, end, len(content)],
                              "truncated": offset > 0 or end < len(content)})
            if end < len(content):
                offset = end
            else:
                index, offset = index + 1, 0
        attachment_end = min(len(attachments), position.attachment_offset + 20)
        has_more = text_end < len(text) or index < len(items) or attachment_end < len(attachments)
        next_cursor = None
        if has_more:
            next_cursor = self._seal("message", connection_id, grant, MessagePosition(
                ref=ref, digest=digest, text_offset=text_end, forward_index=index,
                forward_offset=offset, attachment_offset=attachment_end,
            ).model_dump())
        source_truncated = bool(forward and forward.get("source_truncated"))
        first_page = not any((position.text_offset, position.forward_index,
                              position.forward_offset, position.attachment_offset))
        return {
            **message, "text": text[position.text_offset:text_end],
            "text_range": [position.text_offset, text_end, len(text)],
            "truncated": position.text_offset > 0 or text_end < len(text),
            "forwarded": ({"items": forwarded, "truncated": source_truncated or
                           position.forward_index > 0 or position.forward_offset > 0 or index < len(items),
                           "source_truncated": source_truncated} if forward else None),
            "attachments": attachments[position.attachment_offset:attachment_end],
            "attachment_range": [position.attachment_offset, attachment_end, len(attachments)],
            "attachments_truncated": position.attachment_offset > 0 or attachment_end < len(attachments),
            "next_cursor": next_cursor, "continuation_complete": not has_more,
            "message_complete": first_page and not has_more and not source_truncated,
        }

    def download(self, connection_id: str, grant: Grant, ticket: str) -> File:
        value = self._unseal(ticket, "download", connection_id, grant)
        payload = TokenPayload.validate_python(value)
        if len(payload) != 2 or not isinstance(payload[0], int) or payload[0] <= time.time():
            raise ToolError("invalid_or_stale_reference")
        ref = FileRef.model_validate(payload[1])
        artifact = self.get_file(grant, ref, prepare="original")
        if artifact is None or artifact.open_original is None:
            raise ToolError("unavailable")
        return artifact

    def _text_page(
        self, file: File, connection_id: str, grant: Grant, position: FilePosition | None,
    ) -> Document:
        info = file.metadata()
        factory = file.open_original
        if factory is None:
            return {**info, "preview_status": "unavailable"}
        mime = (file.mime_type or "").partition(";")[0].strip().lower()
        if mime not in _TEXT_MIME_TYPES:
            return {**info, "preview_status": "unsupported_format"}
        if file.size is not None and file.size > TEXT_FILE_LIMIT:
            return {**info, "preview_status": "too_large"}
        try:
            data = _read_bounded(factory, TEXT_FILE_LIMIT)
        except OSError:
            return {**info, "preview_status": "unavailable"}
        if len(data) > TEXT_FILE_LIMIT:
            return {**info, "preview_status": "too_large"}
        if file.size is not None and len(data) != file.size:
            return {**info, "preview_status": "unavailable"}

        digest = hashlib.sha256(data).hexdigest()
        offset = 0
        if position is not None:
            if position.digest != digest or position.offset >= len(data):
                raise ToolError("invalid_or_stale_reference")
            offset = position.offset
        else:
            try:
                data.decode("utf-8")
            except UnicodeDecodeError:
                return {**info, "preview_status": "invalid_encoding"}
        end = min(offset + TEXT_LIMIT, len(data))
        while end < len(data) and data[end] & 0xC0 == 0x80:
            end -= 1
        has_more = end < len(data)
        cursor = None
        if has_more:
            cursor = self._seal(
                _file_text_purpose(file.ref), connection_id, grant,
                FilePosition(digest=digest, offset=end).model_dump(),
            )
        return {
            **info, "text": data[offset:end].decode("utf-8"),
            "text_range": [offset, end, len(data)],
            "truncated": offset > 0 or has_more, "next_cursor": cursor,
            "continuation_complete": not has_more,
        }

    @staticmethod
    def _image_preview(file: File, original_only: bool) -> tuple[Document, Document | None]:
        use_thumbnail = not original_only and (file.open_preview is not None or file.variant == "thumbnail")
        factory = (file.open_preview or file.open_bytes) if use_thumbnail else file.open_original
        status = file.metadata()
        if factory is None:
            return {**status, "preview_status": "unavailable"}, None
        info = {**status, "preview_variant": "thumbnail" if use_thumbnail else "original"}
        mime = (file.mime_type or "").partition(";")[0].strip().lower()
        if not use_thumbnail and mime and mime not in _RASTER_FORMATS:
            return {**info, "preview_status": "unsupported_format"}, None
        if file.open_preview is not factory and file.size is not None and file.size > IMAGE_LIMIT:
            return {**info, "preview_status": "too_large"}, None
        try:
            data = _read_bounded(factory, IMAGE_LIMIT)
        except OSError:
            return {**info, "preview_status": "unavailable"}, None
        if len(data) > IMAGE_LIMIT:
            return {**info, "preview_status": "too_large"}, None
        matched = next(
            (
                (raster_kind, raster_format)
                for raster_kind, (raster_format, signature) in _RASTER_FORMATS.items()
                if data.startswith(signature)
            ),
            None,
        )
        if matched is None:
            return {**info, "preview_status": "invalid_format"}, None
        kind, format_name = matched
        preview_status = _image_status(data, format_name)
        if preview_status != "ready":
            return {**info, "preview_status": preview_status}, None
        return {**info, "preview_status": preview_status}, {
            "type": "image", "mimeType": kind, "data": base64.b64encode(data).decode(),
        }

    def call(
        self,
        name: str,
        params: object,
        connection_id: str,
        grant: Grant,
        origin: str,
        *,
        is_authorized: Callable[[], bool] | None = None,
    ) -> tuple[Document, Document | None]:
        tool = self.available_tools().get(name)
        if tool is None:
            raise ToolError("unknown_tool")
        args = tool.arguments.model_validate(params)
        if grant.scope is None:
            raise ToolError("authorization_required")
        if isinstance(args, AccessContext):
            newest = self.reader.freshness(grant)
            result: Document = {
                "scope": grant.scope.model_dump(mode="json"), "version": grant.version,
                "data_freshness": {"latest_message_at": newest},
                "search_kinds": self.reader.search_kinds,
                "capabilities": {
                    "conversation_sync": self._can_sync_conversation(),
                    "on_demand_media_prepare": self._can_prepare_media(),
                },
                "download_authentication": "bearer", "file_preview": ["utf-8", "png", "jpeg"],
                "file_text_page_bytes": TEXT_LIMIT, "file_text_max_bytes": TEXT_FILE_LIMIT,
            }
            if args.requested_conversation_ids:
                ids = self.additions(grant, tuple(args.requested_conversation_ids))
                if ids:
                    ticket = self._review_ticket(grant.id, grant.version, ids, int(time.time()) // 300)
                    settings = "/settings" if self.reader.source == "local" else "/remote/settings"
                    result["review_url"] = f"{origin}{settings}?mcp_add={quote(ticket)}#agent-access"
            return result, None
        if isinstance(args, Search):
            if args.kind not in self.reader.search_kinds:
                raise ToolError("unsupported_search_kind")
            if args.date_from is not None and args.date_to is not None and args.date_from > args.date_to:
                raise ToolError("invalid_date_range")
            if args.conversation_id is not None and not _permitted(grant, args.conversation_id):
                return {"items": [], "cursor": None, "status": "ready"}, None
            binding = hashlib.sha256(args.model_dump_json(exclude={"cursor"}).encode()).hexdigest()
            position = None
            if args.cursor is not None:
                value = self._unseal(args.cursor, binding, connection_id, grant)
                if not isinstance(value, str):
                    raise ToolError("invalid_or_stale_reference")
                position = value
            try:
                page = self.reader.search(grant, args, position)
            except sqlite3.OperationalError as exc:
                if exc.sqlite_errorcode != sqlite3.SQLITE_INTERRUPT:
                    raise
                page = SearchPage([], status="query_budget_exceeded")
            cursor = (self._seal(binding, connection_id, grant, page.next_position)
                      if page.next_position is not None else None)
            return {"items": page.items, "cursor": cursor, "status": page.status}, None
        if isinstance(args, Messages):
            position = None
            if args.cursor is not None:
                position = MessagePosition.model_validate(self._unseal(args.cursor, "message", connection_id, grant))
                if position.ref != args.messages[0]:
                    raise ToolError("invalid_or_stale_reference")
            items: list[Document] = []
            for ref in args.messages:
                message = (self.reader.message(grant, ref)
                           if _permitted(grant, ref.conversation_id) else None)
                items.append(self._message_page(message, connection_id, grant, position) if message is not None else {
                    "ref": ref.model_dump(), "status": "unavailable",
                })
            return {"items": items}, None
        if isinstance(args, Context):
            context = (self.reader.context(grant, args)
                       if _permitted(grant, args.conversation_id) else None)
            return {"items": [self._message_page(item, connection_id, grant) for item in context or []],
                    "status": "ready" if context else "unavailable"}, None
        if isinstance(args, ReadFile):
            ref = FileRef(conversation_id=args.conversation_id, message_id=args.message_id,
                          attachment_id=args.attachment_id)
            position = None
            if args.cursor is not None:
                position = FilePosition.model_validate(
                    self._unseal(args.cursor, _file_text_purpose(ref), connection_id, grant),
                )
            prepare: PrepareMode = "none"
            if args.mode == "text" or (args.mode == "preview" and args.image_variant == "original"):
                prepare = "original"
            elif args.mode == "preview":
                prepare = "preview"
            artifact = self.get_file(grant, ref, prepare=prepare)
            if artifact is None:
                return {"ref": ref.model_dump(), "status": "unavailable"}, None
            if args.mode == "text":
                return self._text_page(artifact, connection_id, grant, position), None
            if args.mode == "preview":
                return self._image_preview(artifact, args.image_variant == "original")
            return artifact.metadata(), None
        if isinstance(args, Downloads):
            downloads: list[Document] = []
            for ref in args.files:
                artifact = self.get_file(grant, ref, prepare="original")
                if artifact is None:
                    downloads.append({"ref": ref.model_dump(), "status": "unavailable"})
                    continue
                item = artifact.metadata()
                if artifact.open_original is not None:
                    ticket = self._seal(
                        "download", connection_id, grant, [int(time.time()) + 300, ref.model_dump()],
                    )
                    item.update(url=f"{origin}/mcp/files/{quote(ticket)}", authentication="bearer",
                                redacted=False)
                downloads.append(item)
            return {"files": downloads}, None
        if isinstance(args, SyncConversation):
            if not (
                isinstance(self.reader, ConversationSyncReader)
                and self.reader.supports_conversation_sync is True
            ):
                raise ToolError("unknown_tool")
            if not _permitted(grant, args.conversation_id):
                return {"conversation_id": args.conversation_id, "status": "unavailable"}, None
            return self.reader.sync_conversation(
                grant, args.conversation_id, is_authorized=is_authorized,
            ), None
        raise ToolError("unknown_tool")

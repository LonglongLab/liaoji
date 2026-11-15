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
from typing import Literal, Protocol
from urllib.parse import quote

from cryptography.fernet import Fernet, InvalidToken
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, model_validator

from chat_archive.agent_access.policy import Grant, Identifier
from chat_archive.domain.app_card import AppCard

Source = Literal["local", "remote"]
SearchKind = Literal["message", "conversation", "person", "file"]
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


class Downloads(Arguments):
    files: list[FileRef] = Field(min_length=1, max_length=5)


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

    def metadata(self) -> Document:
        return {
            "ref": self.ref.model_dump(),
            "name": self.name,
            "mime_type": self.mime_type,
            "size": self.size,
            "status": self.status,
            "variant": self.variant,
        }


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
class Tool:
    description: str
    arguments: type[Arguments]


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
    "liaoji_read_file": Tool("Read attachment metadata or a bounded text or image preview.", ReadFile),
    "liaoji_get_downloads": Tool("Get authenticated, attachment-specific download URLs.", Downloads),
}

TEXT_LIMIT = 8_192
IMAGE_LIMIT = 131_072
_RASTER_SIGNATURES = {
    "image/png": (b"\x89PNG\r\n\x1a\n",),
    "image/jpeg": (b"\xff\xd8\xff",),
}


def query_deadline() -> Callable[[], int]:
    expires = time.monotonic() + 3
    return lambda: int(time.monotonic() >= expires)


def _permitted(grant: Grant, conversation_id: str) -> bool:
    return grant.scope is not None and grant.scope.allows(conversation_id)


class QueryTools:
    def __init__(
        self, reader: ArchiveReader,
        additions: Callable[[Grant, tuple[str, ...]], tuple[str, ...]],
    ) -> None:
        self.reader = reader
        self.additions = additions
        self._cipher = Fernet(Fernet.generate_key())

    @lru_cache(maxsize=512)
    def _review_ticket(
        self, grant_id: str, version: int, ids: tuple[str, ...], _window: int
    ) -> str:
        return self._cipher.encrypt(json.dumps([grant_id, version, ids]).encode()).decode()

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

    def get_file(self, grant: Grant, ref: FileRef) -> File | None:
        if not _permitted(grant, ref.conversation_id):
            return None
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
        artifact = self.get_file(grant, ref)
        if artifact is None or artifact.open_bytes is None or artifact.variant == "thumbnail":
            raise ToolError("unavailable")
        return artifact

    @staticmethod
    def _preview(file: File, mode: str) -> tuple[Document, Document | None]:
        factory = file.open_preview if mode == "preview" and file.open_preview else file.open_bytes
        if mode == "info" or factory is None:
            return file.metadata(), None
        info = file.metadata()
        mime = file.mime_type or ""
        if mode == "preview" and file.open_preview:
            info["preview_variant"] = "thumbnail"
        if ((mode == "text" and mime not in {"text/plain", "text/csv", "text/markdown"})
                or (mode == "preview" and mime and mime not in _RASTER_SIGNATURES)):
            return {**info, "preview_status": "unsupported_format"}, None
        limit = TEXT_LIMIT if mode == "text" else IMAGE_LIMIT
        if mode == "preview" and file.open_preview is None and file.size is not None and file.size > limit:
            return {**info, "preview_status": "too_large"}, None
        data = bytearray()
        stream = factory()
        try:
            for chunk in stream:
                data.extend(chunk[:limit + 1 - len(data)])
                if len(data) > limit:
                    break
        except OSError:
            return {**info, "preview_status": "unavailable"}, None
        finally:
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        if mode == "text":
            return {
                **info, "text": bytes(data[:limit]).decode("utf-8", errors="replace"),
                "truncated": len(data) > limit,
            }, None
        if len(data) > limit:
            return {**info, "preview_status": "too_large"}, None
        kind = next((kind for kind, signatures in _RASTER_SIGNATURES.items()
                     if any(data.startswith(prefix) for prefix in signatures)), None)
        if kind is None:
            return {**info, "preview_status": "invalid_format"}, None
        return {**info, "preview_status": "ready"}, {
            "type": "image", "mimeType": kind, "data": base64.b64encode(data).decode(),
        }

    def call(
        self, name: str, params: object, connection_id: str, grant: Grant, origin: str
    ) -> tuple[Document, Document | None]:
        tool = TOOLS.get(name)
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
                "download_authentication": "bearer", "file_preview": ["utf-8", "png", "jpeg"],
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
            ref = FileRef.model_validate(args.model_dump(exclude={"mode"}))
            artifact = self.get_file(grant, ref)
            return self._preview(artifact, args.mode) if artifact else (
                {"ref": ref.model_dump(), "status": "unavailable"}, None,
            )
        if isinstance(args, Downloads):
            downloads: list[Document] = []
            for ref in args.files:
                artifact = self.get_file(grant, ref)
                if artifact is None:
                    downloads.append({"ref": ref.model_dump(), "status": "unavailable"})
                    continue
                item = artifact.metadata()
                if artifact.open_bytes is not None and artifact.variant != "thumbnail":
                    ticket = self._seal(
                        "download", connection_id, grant, [int(time.time()) + 300, ref.model_dump()],
                    )
                    item.update(url=f"{origin}/mcp/files/{quote(ticket)}", authentication="bearer",
                                redacted=False)
                downloads.append(item)
            return {"files": downloads}, None
        raise ToolError("unknown_tool")

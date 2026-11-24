"""Self-hosted SQLite MCP adapter for already-synced archive records."""

from __future__ import annotations

import mimetypes
import sqlite3
import tempfile
from collections.abc import Iterator
from contextlib import closing
from functools import partial
from pathlib import Path
from typing import BinaryIO, Literal, cast

from chat_archive.adapters.sqlite.connection import open_sqlite
from chat_archive.adapters.sqlite.metadata import SQLiteMetadataStore
from chat_archive.agent_access.policy import Grant
from chat_archive.agent_access.tools import (
    IMAGE_LIMIT,
    Context,
    Document,
    File,
    FileRef,
    MessageRef,
    Search,
    SearchKind,
    SearchPage,
    message_document,
    query_deadline,
)
from chat_archive.domain.app_card import AppCard, card_from_json
from chat_archive.domain.forward_media import card_blob_metadata
from chat_archive.domain.identity import thumbnail_blob_id
from chat_archive.ports.blob_state import BlobManifestRecord, BlobStateStore
from chat_archive.ports.metadata import MessagePosition, StoredRecord
from chat_archive.ports.objectstore import ObjectStore, ObjectStoreError
from chat_archive.search import normalize_search_text
from chat_archive.server.object_storage_config import ConfigurableObjectStore


class RemoteReader:
    source: Literal["remote"] = "remote"
    search_kinds: tuple[SearchKind, ...] = ("message", "conversation", "person", "file")

    def __init__(
        self, path: Path, records: SQLiteMetadataStore, manifests: BlobStateStore,
        objects: ObjectStore,
    ) -> None:
        self.path = path.resolve()
        self.records = records
        self.manifests = manifests
        self.objects = objects

    def _connect(self) -> sqlite3.Connection:
        return open_sqlite(self.path, read_only=True)

    def owns_account(self, archive: str, account: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute(
                "SELECT 1 FROM archive_conversation_state WHERE archive_id=? AND source_account_id=? LIMIT 1",
                (archive, account),
            ).fetchone() is not None

    def owns_chat(self, archive: str, account: str, chat: str) -> bool:
        with closing(self._connect()) as db:
            return db.execute(
                """SELECT 1 FROM archive_conversation_state
                   WHERE archive_id=? AND source_account_id=? AND conversation_id=?""",
                (archive, account, chat),
            ).fetchone() is not None

    def account_link_verified(self, archive: str, source_account: str, account: str) -> bool:
        with closing(self._connect()) as db:
            row = db.execute("""
                SELECT primary_account_id FROM archive_account_links
                WHERE archive_id=? AND source_account_id=?
                """, (archive, source_account)).fetchone()
        # A link requires an attributed stream and the same chat-ID namespace.
        return source_account == account and (row is None or row[0] == account) and (
            self.owns_account(archive, account)
        )

    def freshness(self, grant: Grant) -> int | None:
        if grant.scope is None:
            return None
        ids = grant.scope.conversation_ids
        scope = " AND conversation_id IN (" + ",".join("?" for _ in ids) + ")" if ids else ""
        with closing(self._connect()) as db:
            value = db.execute(
                "SELECT max(last_created_at) FROM archive_conversation_state"
                " WHERE archive_id=? AND source_account_id=?" + scope,
                (grant.archive_id, grant.account_id, *ids),
            ).fetchone()[0]
        return int(value) if value is not None else None

    def search(self, grant: Grant, args: Search, position: str | None) -> SearchPage:
        if grant.scope is None:
            return SearchPage([])
        is_chat = args.kind == "conversation"
        is_person = args.kind == "person"
        is_file = args.kind == "file"
        if is_chat:
            table = """archive_conversation_state r LEFT JOIN archive_conversations c
                USING (archive_id,source_account_id,conversation_id)"""
            key = "r.conversation_id"
            label = "coalesce(c.display_name,r.conversation_id)"
            timestamp = "r.last_created_at"
            message = """(SELECT record_key FROM archive_records m WHERE
                m.archive_id=r.archive_id AND m.source_account_id=r.source_account_id
                AND m.conversation_id=r.conversation_id
                ORDER BY created_at DESC,source_sequence DESC,source_local_id DESC,record_key DESC
                LIMIT 1)"""
        else:
            table = "archive_records r"
            key = "r.sender_uid" if is_person else "r.record_key"
            label = "coalesce(r.sender_display_name,r.sender_uid)" if is_person else "r.content_text"
            timestamp = "r.created_at"
            message = "r.record_key"
        if is_file:
            table += (
                " JOIN json_each(CASE WHEN json_valid(r.attachment_refs)"
                " THEN r.attachment_refs ELSE '[]' END) a"
            )
            key = "r.record_key || ':' || a.value"
            label = "agent_file_name(r.app_card_json,a.value)"
        attachment = "a.value" if is_file else "NULL"
        normalized = "r.normalized_content" if args.kind == "message" else f"agent_normalize({label})"
        filters = ["r.archive_id=?", "r.source_account_id=?", f"instr({normalized},?)>0"]
        params: list[str | int] = [grant.archive_id, grant.account_id, normalize_search_text(args.query)]
        allowed = (args.conversation_id,) if args.conversation_id else grant.scope.conversation_ids
        if allowed:
            filters.append("r.conversation_id IN (" + ",".join("?" for _ in allowed) + ")")
            params.extend(allowed)
        if args.date_from is not None:
            filters.append(f"{timestamp}>=?")
            params.append(args.date_from)
        if args.date_to is not None:
            filters.append(f"{timestamp}<=?")
            params.append(args.date_to)
        rank = (
            "row_number() OVER (PARTITION BY r.sender_uid ORDER BY r.created_at DESC,r.record_key)"
            if is_person else "1"
        )
        after = ""
        if position is not None:
            ts, _, previous_key = position.partition(":")
            after = "AND (ts < ? OR (ts = ? AND row_key > ?))"
            params.extend((int(ts), int(ts), previous_key))
        with closing(self._connect()) as db:
            db.set_progress_handler(query_deadline(), 10_000)
            def normalize_label(text: str | None) -> str:
                return normalize_search_text(text or "")

            db.create_function("agent_normalize", 1, normalize_label, deterministic=True)
            if is_file:
                db.create_function("agent_file_name", 2, self._file_name, deterministic=True)
            rows = db.execute(f"""
                SELECT * FROM (
                  SELECT r.conversation_id AS chat_id,{key} AS row_key,{timestamp} AS ts,
                         {message} AS message_id,{attachment} AS attachment_id,
                         substr({label},1,240) AS summary,
                         length({label}) > 240 AS truncated,{rank} AS rank
                  FROM {table} WHERE {" AND ".join(filters)}
                ) WHERE rank=1 {after} ORDER BY ts DESC,row_key ASC LIMIT ?
                """, (*params, args.limit + 1)).fetchall()
        rows_page = rows[:args.limit]
        items: list[Document] = []
        for row in rows_page:
            ref: Document = {"conversation_id": row["chat_id"], "message_id": row["message_id"]}
            if is_person:
                ref["sender_id"] = row["row_key"]
            if is_file:
                ref["attachment_id"] = row["attachment_id"]
            items.append({"ref": ref, "summary": row["summary"], "created_at": row["ts"],
                          "truncated": bool(row["truncated"])})
        cursor = f"{rows_page[-1]['ts']}:{rows_page[-1]['row_key']}" if len(rows) > args.limit else None
        return SearchPage(items, cursor)

    @staticmethod
    def _file_name(raw: str | None, blob_id: str) -> str | None:
        metadata = card_blob_metadata(raw).get(blob_id)
        if metadata is not None:
            return metadata[1]
        card = card_from_json(raw) if raw else None
        if card is not None and card.kind == "file":
            # Top-level file cards name their primary attachment.
            return card.title or None
        return None

    def _record(self, grant: Grant, ref: MessageRef) -> StoredRecord | None:
        row = self.records.get_conversation_message(
            grant.archive_id, ref.conversation_id, record_key=ref.message_id,
            source_account_id=grant.account_id,
        )
        if row is None or (row.source_account_id, row.conversation_id) != (
            grant.account_id, ref.conversation_id,
        ):
            return None
        return row

    def _message(self, grant: Grant, row: StoredRecord) -> Document:
        reply = None
        if row.reply_to_key and self._record(grant, MessageRef(
            conversation_id=row.conversation_id, message_id=row.reply_to_key,
        )) is not None:
            reply = row.reply_to_key
        attachments: list[Document] = [
            {"attachment_id": ref, "name": self._file_name(row.app_card_json, ref)}
            for ref in row.attachment_refs
        ]
        return message_document(
            MessageRef(conversation_id=row.conversation_id, message_id=row.record_key),
            created_at=row.created_at, message_type=row.message_type, sender_id=row.sender_uid,
            sender_name=row.sender_display_name, text=row.content_text, reply=reply,
            card=card_from_json(row.app_card_json) if row.app_card_json else None,
            attachments=attachments,
        )

    def message(self, grant: Grant, ref: MessageRef) -> Document | None:
        record = self._record(grant, ref)
        return self._message(grant, record) if record is not None else None

    def context(self, grant: Grant, args: Context) -> list[Document] | None:
        anchor = args.message_id
        if anchor is None:
            with closing(self._connect()) as db:
                row = db.execute("""
                    SELECT record_key FROM archive_records WHERE archive_id=? AND
                    source_account_id=? AND conversation_id=? AND created_at>=?
                    ORDER BY created_at,source_sequence,source_local_id,record_key LIMIT 1
                    """, (grant.archive_id, grant.account_id, args.conversation_id,
                          args.timestamp)).fetchone()
            if row is None:
                return None
            anchor = str(row[0])
        record = self._record(grant, MessageRef(conversation_id=args.conversation_id,
                                                message_id=anchor))
        if record is None:
            return None
        position = MessagePosition(record.created_at, record.source_sequence,
                                   record.source_local_id, record.record_key)
        older = self.records.list_conversation_messages_before(
            grant.archive_id, args.conversation_id, before=position, limit=args.before,
            source_account_id=grant.account_id,
        ).records if args.before else ()
        newer = self.records.list_conversation_messages_in_time_order(
            grant.archive_id, args.conversation_id, after=position, limit=args.after,
            source_account_id=grant.account_id,
        ).records if args.after else ()
        return [self._message(grant, row) for row in (*older, record, *newer)
                if row.source_account_id == grant.account_id and row.conversation_id == args.conversation_id]

    def file(self, grant: Grant, ref: FileRef) -> File | None:
        record = self._record(grant, ref)
        if record is None or ref.attachment_id not in record.attachment_refs:
            return None
        name = self._file_name(record.app_card_json, ref.attachment_id)
        mime = mimetypes.guess_type(name)[0] if name else None
        card = card_from_json(record.app_card_json) if record.app_card_json else None
        manifest = self.manifests.get_manifest(grant.archive_id, ref.attachment_id)
        thumbnail = self.manifests.get_manifest(grant.archive_id, thumbnail_blob_id(ref.attachment_id))
        variant = self._variant(card, ref.attachment_id) or "archived"
        if manifest is None or manifest.state != "remote_ready":
            manifest = thumbnail
            variant = "thumbnail"
            mime = None
        if manifest is None or manifest.state != "remote_ready":
            return File(ref, name, mime, None, "not_synced", None)
        try:
            objects, generation = (
                self.objects.snapshot() if isinstance(self.objects, ConfigurableObjectStore)
                else (self.objects, 1)
            )
        except ObjectStoreError:
            return File(ref, name, mime, manifest.size, "storage_unavailable", None)
        if generation != manifest.storage_generation:
            return File(ref, name, mime, manifest.size, "storage_unavailable", None)
        preview = None
        if thumbnail is not None and thumbnail.state == "remote_ready" and (
            generation == thumbnail.storage_generation and thumbnail.size <= IMAGE_LIMIT
        ):
            preview = partial(self._stream, objects, thumbnail)
        return File(ref, name, mime, manifest.size, "ready", variant,
                    lambda: self._stream(objects, manifest), preview)

    @staticmethod
    def _variant(card: AppCard | None, blob_id: str) -> str | None:
        if card is None:
            return None
        for item in card.items:
            media = item.remote_media
            if media is not None and media["blob_id"] == blob_id:
                return media["variant"]
            nested = RemoteReader._variant(item.nested, blob_id)
            if nested is not None:
                return nested
        return None

    @staticmethod
    def _stream(objects: ObjectStore, manifest: BlobManifestRecord) -> Iterator[bytes]:
        with cast(BinaryIO, tempfile.TemporaryFile(mode="w+b")) as stream:
            try:
                attrs = objects.download_object(manifest.object_scope, manifest.object_key, stream)
            except ObjectStoreError as exc:
                raise OSError("Object unavailable") from exc
            if (attrs.size, attrs.sha256) != (manifest.size, manifest.sha256):
                raise OSError("Object could not be verified")
            stream.seek(0)
            while chunk := stream.read(64 * 1024):
                yield chunk

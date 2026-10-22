"""Export estimate, format writers, atomic publish, and job-driven worker."""

from __future__ import annotations

import csv
import hashlib
import html
import json
import shutil
import sqlite3
import time
import zipfile
from collections.abc import Callable, Iterator
from contextlib import closing
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from chat_archive.domain.errors import ArchiveError, ErrorCode, RecommendedAction
from chat_archive.domain.models import JobStatus
from chat_archive.ports.export import ExportHistoryRepository, ExportArtifactRecord
from chat_archive.ports.jobs import JobRepository
from chat_archive.query import MESSAGE_ORDER_COLUMNS
from chat_archive.version import APP_VERSION
from chat_archive.html_export import write_html_bundle
from chat_archive.job_heartbeat import JobHeartbeat


class ExportFormat(str, Enum):
    JSONL = "jsonl"
    JSON = "json"
    CSV = "csv"
    HTML = "html"


@dataclass(frozen=True)
class ExportScope:
    account_id: str
    conversation_ids: tuple[str, ...] = ()
    date_from: int | None = None
    date_to: int | None = None
    message_types: tuple[str, ...] = ()
    attachment_kinds: tuple[str, ...] = ("image", "emoji", "audio", "file")
    title: str = "聊天记录"


@dataclass(frozen=True)
class ExportEstimate:
    message_count: int
    conversation_count: int
    media_ready: int
    media_pending: int
    media_missing: int
    estimated_bytes: int


_MEDIA_READY = ("ready",)
_MEDIA_MISSING = ("failed", "source_missing", "unsupported")
_RECORD_OVERHEAD_BYTES = 256

# The manifest's birth certificate, named like the portable/update/recovery
# artifact families. A reader that meets an unknown version must refuse with
# "written by a newer version", not guess; a manifest without the key is v0
# (packages already in the wild) and reads as today.
EXPORT_MANIFEST_SCHEMA_VERSION = "chat-archive.export.v1"


# An export is a transcript, so it has to read in the same order the transcript
# does -- including inside a shared second, which is the whole point of
# MESSAGE_ORDER_COLUMNS. Built from that tuple rather than restated, because the
# two orders silently disagreeing is exactly the failure this guards against;
# `conversation_id` leads it since an export walks whole conversations in turn.
_EXPORT_ORDER_COLUMNS = ("conversation_id", *MESSAGE_ORDER_COLUMNS)
_EXPORT_ORDER_KEY = ", ".join(f"m.{column}" for column in _EXPORT_ORDER_COLUMNS)
_EXPORT_ORDER_BY = ", ".join(f"m.{column} ASC" for column in _EXPORT_ORDER_COLUMNS)
_EXPORT_ORDER_PLACEHOLDERS = ", ".join("?" for _ in _EXPORT_ORDER_COLUMNS)
_ExportMarker = tuple[str, int, int, int, str]


def scope_conditions(scope: ExportScope) -> tuple[list[str], list[str | int]]:
    conditions: list[str] = ["m.source_account_id = ?"]
    parameters: list[str | int] = [scope.account_id]
    if scope.conversation_ids:
        placeholders = ", ".join("?" for _ in scope.conversation_ids)
        conditions.append(f"m.conversation_id IN ({placeholders})")
        parameters.extend(scope.conversation_ids)
    if scope.date_from is not None:
        conditions.append("m.created_at >= ?")
        parameters.append(scope.date_from)
    if scope.date_to is not None:
        conditions.append("m.created_at <= ?")
        parameters.append(scope.date_to)
    if scope.message_types:
        placeholders = ", ".join("?" for _ in scope.message_types)
        conditions.append(f"m.message_type IN ({placeholders})")
        parameters.extend(scope.message_types)
    return conditions, parameters


def _validate_scope(scope: ExportScope) -> None:
    if not scope.title.strip() or len(scope.title) > 80 or any(not ch.isprintable() for ch in scope.title):
        raise ArchiveError(ErrorCode.QUERY_INVALID)
    if not scope.account_id:
        raise ArchiveError(ErrorCode.QUERY_INVALID)
    if (
        scope.date_from is not None
        and scope.date_to is not None
        and scope.date_from > scope.date_to
    ):
        raise ArchiveError(ErrorCode.QUERY_INVALID)


class ExportService:
    """Estimate and materialize exports from read-only archive connections."""

    def __init__(
        self,
        archive_path: Path,
        exports_root: Path,
        *,
        busy_timeout_ms: int = 5_000,
        chunk_size: int = 500,
        media_root: Path | None = None,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
        scope_connection_factory: Callable[[ExportScope], sqlite3.Connection] | None = None,
        allow_source_media_decode: bool = True,
        decode_source_image: Callable[[Path], bytes | None] | None = None,
        app_version: str = APP_VERSION,
    ) -> None:
        if chunk_size < 1:
            raise ValueError("chunk_size must be positive")
        self._archive_path = archive_path
        self._exports_root = exports_root
        self._busy_timeout_ms = busy_timeout_ms
        self._chunk_size = chunk_size
        self.media_root = media_root
        self._connection_factory = connection_factory
        self._scope_connection_factory = scope_connection_factory
        self.allow_source_media_decode = allow_source_media_decode
        self.decode_source_image = decode_source_image
        self._app_version = app_version
        self.resolve_media: Callable[[str, str], str | None] | None = None

    @property
    def exports_root(self) -> Path:
        return self._exports_root

    def deletion_pending(self, export_id: str) -> bool:
        return (self._exports_root / ".delete" / f"{export_id}.json").is_file()

    def _connect(self, scope: ExportScope | None = None) -> sqlite3.Connection:
        if scope is not None and self._scope_connection_factory is not None:
            return self._scope_connection_factory(scope)
        if self._connection_factory is not None:
            return self._connection_factory()
        uri = f"{self._archive_path.resolve(strict=False).as_uri()}?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _require_account(connection: sqlite3.Connection, account_id: str) -> sqlite3.Row:
        row = connection.execute(
            "SELECT id, display_name FROM source_accounts WHERE id = ?",
            (account_id,),
        ).fetchone()
        if row is None:
            raise ArchiveError(
                ErrorCode.SOURCE_NOT_FOUND,
                recommended_action=RecommendedAction.RESELECT_SOURCE,
            )
        return row

    def estimate(self, scope: ExportScope) -> ExportEstimate:
        _validate_scope(scope)
        conditions, parameters = scope_conditions(scope)
        where = " AND ".join(conditions)
        media_where = "a.kind IN (" + ",".join("?" for _ in scope.attachment_kinds) + ")" if scope.attachment_kinds else "0"
        with closing(self._connect(scope)) as connection:
            self._require_account(connection, scope.account_id)
            counts = connection.execute(
                f"""
                SELECT COUNT(*) AS message_count,
                       COUNT(DISTINCT m.conversation_id) AS conversation_count,
                       COALESCE(SUM(LENGTH(CAST(m.content_text AS BLOB))), 0) AS text_bytes
                FROM messages AS m
                WHERE {where}
                """,
                parameters,
            ).fetchone()
            media = connection.execute(
                f"""
                SELECT a.cache_status AS cache_status, COUNT(*) AS bucket_count
                FROM attachments AS a
                JOIN messages AS m ON m.id = a.message_id
                WHERE {where} AND ({media_where})
                GROUP BY a.cache_status
                """,
                (*parameters, *scope.attachment_kinds),
            ).fetchall()

        ready = pending = missing = 0
        for row in media:
            status = str(row["cache_status"])
            count = int(row["bucket_count"])
            if status in _MEDIA_READY:
                ready += count
            elif status in _MEDIA_MISSING:
                missing += count
            else:
                pending += count
        message_count = int(counts["message_count"])
        text_bytes = int(counts["text_bytes"])
        return ExportEstimate(
            message_count=message_count,
            conversation_count=int(counts["conversation_count"]),
            media_ready=ready,
            media_pending=pending,
            media_missing=missing,
            estimated_bytes=text_bytes * 2 + message_count * _RECORD_OVERHEAD_BYTES,
        )

    def _iter_message_chunks(
        self,
        connection: sqlite3.Connection,
        scope: ExportScope,
    ) -> Iterator[list[sqlite3.Row]]:
        conditions, parameters = scope_conditions(scope)
        marker: _ExportMarker | None = None
        while True:
            page_conditions = list(conditions)
            page_parameters: list[str | int] = list(parameters)
            if marker is not None:
                page_conditions.append(
                    f"({_EXPORT_ORDER_KEY}) > ({_EXPORT_ORDER_PLACEHOLDERS})"
                )
                page_parameters.extend(marker)
            rows = connection.execute(
                f"""
                SELECT m.id, m.conversation_id, m.sender_uid, m.sender_display_name,
                       m.created_at, m.message_type, m.content_text, m.is_self,
                       m.reply_to_id, m.parse_status,
                       m.source_message_id, m.app_card_json,
                       m.source_sequence, m.source_local_id
                FROM messages AS m
                WHERE {" AND ".join(page_conditions)}
                ORDER BY {_EXPORT_ORDER_BY}
                LIMIT ?
                """,
                (*page_parameters, self._chunk_size),
            ).fetchall()
            if not rows:
                return
            yield rows
            last = rows[-1]
            marker = (
                str(last["conversation_id"]),
                int(last["created_at"]),
                int(last["source_sequence"]),
                int(last["source_local_id"]),
                str(last["id"]),
            )

    def validate_scope(self, scope: ExportScope) -> None:
        _validate_scope(scope)
        with closing(self._connect()) as connection:
            self._require_account(connection, scope.account_id)

    def preview_coordinates(self, scope: ExportScope) -> list[tuple[str, str]]:
        """A bounded sample using exactly the same scope as the export."""
        _validate_scope(scope)
        conditions, parameters = scope_conditions(scope)
        with closing(self._connect()) as connection:
            self._require_account(connection, scope.account_id)
            rows = connection.execute(
                f"SELECT m.id, m.conversation_id FROM messages m WHERE {' AND '.join(conditions)} ORDER BY {_EXPORT_ORDER_BY} LIMIT 3",
                parameters,
            ).fetchall()
        return [(str(row["id"]), str(row["conversation_id"])) for row in rows]

    # --- artifact writing -------------------------------------------------

    def _temp_dir(self, export_id: str) -> Path:
        return self._exports_root / ".tmp" / export_id

    def final_dir(self, export_id: str) -> Path:
        return self._exports_root / export_id

    def discard_temp(self, export_id: str, *, keep_scope: bool = False) -> None:
        shutil.rmtree(self._temp_dir(export_id), ignore_errors=True)
        if not keep_scope:
            self._scope_snapshot(export_id).unlink(missing_ok=True)

    def scope_snapshot_path(self, export_id: str) -> Path:
        return self._scope_snapshot(export_id)

    @staticmethod
    def frozen_read_connection(path: Path) -> sqlite3.Connection:
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def _scope_snapshot(self, export_id: str) -> Path:
        return self._exports_root / ".scope" / f"{export_id}.sqlite"

    def _frozen_connection(self, export_id: str, scope: ExportScope, *, include_attachments: bool = True) -> sqlite3.Connection:
        """Freeze only selected domain records; no raw database or account dump."""
        path = self._scope_snapshot(export_id)
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            conditions, parameters = scope_conditions(scope)
            columns = (*_MESSAGE_FIELDS, "source_account_id", "source_message_id", "app_card_json", "source_sequence", "source_local_id")
            try:
                with closing(self._connect(scope)) as frozen:
                    frozen.execute("ATTACH DATABASE ? AS snapshot", (str(path),))
                    frozen.execute("BEGIN")
                    frozen.execute(f"CREATE TABLE snapshot.messages AS SELECT {', '.join('m.' + c for c in columns)} FROM messages m WHERE {' AND '.join(conditions)}", parameters)
                    frozen.execute("CREATE TABLE snapshot.source_accounts AS SELECT id,display_name FROM source_accounts WHERE id=?", (scope.account_id,))
                    frozen.execute("CREATE TABLE snapshot.conversations AS SELECT id,source_account_id,display_name,is_group,conversation_uid FROM conversations WHERE source_account_id=? AND id IN (SELECT DISTINCT conversation_id FROM snapshot.messages)", (scope.account_id,))
                    frozen.execute("CREATE TABLE snapshot.contacts AS SELECT source_account_id,contact_uid,display_name,remark,is_unknown FROM contacts WHERE source_account_id=? AND contact_uid IN (SELECT DISTINCT sender_uid FROM snapshot.messages)", (scope.account_id,))
                    frozen.execute("CREATE TABLE snapshot.attachments AS SELECT id,message_id,kind,cache_path,thumb_cache_path,cache_status,mime_type,file_name FROM attachments WHERE message_id IN (SELECT id FROM snapshot.messages)" + ("" if include_attachments else " AND 0"))
                    frozen.execute("CREATE INDEX snapshot.messages_order ON messages(conversation_id,created_at,source_sequence,source_local_id,id)")
                    frozen.execute("CREATE INDEX snapshot.attachments_message ON attachments(message_id)")
                    frozen.execute("CREATE INDEX snapshot.messages_id ON messages(id)")
                    frozen.execute("CREATE INDEX snapshot.conversations_id ON conversations(id)")
                    frozen.commit()
            except Exception:
                path.unlink(missing_ok=True)
                raise
        connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        connection.row_factory = sqlite3.Row
        return connection

    def write_artifact_files(
        self,
        export_id: str,
        scope: ExportScope,
        export_format: ExportFormat,
        *,
        on_chunk: Callable[[int], None] | None = None,
        on_stage: Callable[[str, int], None] | None = None,
        on_total: Callable[[int], None] | None = None,
    ) -> int:
        """Write all artifact files into the temp dir; returns the record count."""
        _validate_scope(scope)
        temp_dir = self._temp_dir(export_id)
        if temp_dir.exists():
            shutil.rmtree(temp_dir)
        temp_dir.mkdir(parents=True)

        with closing(self._frozen_connection(export_id, scope, include_attachments=export_format is ExportFormat.HTML)) as connection:
            connection.execute("BEGIN")
            account = self._require_account(connection, scope.account_id)
            record_count = 0
            if export_format is ExportFormat.HTML:
                if on_total is not None:
                    on_total(int(connection.execute("SELECT COUNT(*) FROM messages").fetchone()[0]))
                record_count = write_html_bundle(
                    connection, temp_dir, scope, self._iter_message_chunks(connection, scope),
                    media_root=self.media_root, resolve_media=self.resolve_media, on_chunk=on_chunk, on_stage=on_stage,
                    allow_source_media_decode=self.allow_source_media_decode,
                    decode_source_image=self.decode_source_image,
                )
            else:
                with _FORMAT_WRITERS[export_format](temp_dir) as writer:
                    for chunk in self._iter_message_chunks(connection, scope):
                        for row in chunk:
                            writer.add(_message_record(row))
                        record_count += len(chunk)
                        if on_chunk is not None:
                            on_chunk(record_count)
            conditions, parameters = scope_conditions(scope)
            contacts = connection.execute(
                f"""
                SELECT contact_uid, display_name, remark, is_unknown
                FROM contacts
                WHERE source_account_id = ? AND contact_uid IN (
                    SELECT DISTINCT m.sender_uid FROM messages m
                    WHERE {" AND ".join(conditions)}
                )
                ORDER BY contact_uid
                """,
                (scope.account_id, *parameters),
            ).fetchall()

        _write_json(
            temp_dir / "contacts.json",
            {
                "contacts": [
                    {
                        "contact_uid": str(row["contact_uid"]),
                        "display_name": str(row["display_name"]),
                        "remark": row["remark"],
                        "is_unknown": bool(row["is_unknown"]),
                    }
                    for row in contacts
                ]
            },
        )
        self._write_manifest(
            temp_dir,
            export_id=export_id,
            scope=scope,
            export_format=export_format,
            account_id=str(account["id"]),
            account_display_name=str(account["display_name"]),
            record_count=record_count,
        )
        return record_count

    def _write_manifest(
        self,
        temp_dir: Path,
        *,
        export_id: str,
        scope: ExportScope,
        export_format: ExportFormat,
        account_id: str,
        account_display_name: str,
        record_count: int,
    ) -> None:
        files = [
            {
                "name": path.relative_to(temp_dir).as_posix(),
                "size": path.stat().st_size,
                "sha256": _sha256_file(path),
            }
            for path in sorted(temp_dir.rglob("*"))
            if path.is_file() and path.name != "manifest.json"
        ]
        _write_json(
            temp_dir / "manifest.json",
            {
                "manifest_version": EXPORT_MANIFEST_SCHEMA_VERSION,
                **({"artifact_profile": "offline-html.v1"} if export_format is ExportFormat.HTML else {}),
                "app_version": self._app_version,
                "export_id": export_id,
                "created_at": int(time.time()),
                "account": {"id": account_id, "display_name": account_display_name},
                "format": export_format.value,
                "scope": {
                    "conversation_ids": list(scope.conversation_ids),
                    "date_from": scope.date_from,
                    "date_to": scope.date_to,
                    "message_types": list(scope.message_types),
                    "attachment_kinds": list(scope.attachment_kinds),
                    "title": scope.title,
                },
                "record_count": record_count,
                "files": files,
            },
        )

    def publish(self, export_id: str, *, on_file: Callable[[], None] | None = None, on_progress: Callable[[int, int], None] | None = None) -> int:
        """Verify the temp artifact against its manifest, then move it atomically."""
        temp_dir = self._temp_dir(export_id)
        manifest = json.loads((temp_dir / "manifest.json").read_text(encoding="utf-8"))
        expected = {str(entry["name"]): entry for entry in manifest["files"]}
        root = temp_dir.resolve()
        def checked_path(name: str) -> Path:
            relative = Path(name)
            target = (temp_dir / relative).resolve()
            if relative.is_absolute() or ".." in relative.parts or not target.is_relative_to(root):
                raise ArchiveError(ErrorCode.EXPORT_FAILED)
            return target

        if manifest["format"] == "html":
            # Verify and package in one read pass. Already compressed media is
            # stored directly; recompressing PNG/MP4/office ZIPs wastes CPU.
            stored = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".mp4", ".mov", ".mp3", ".pdf", ".zip", ".docx", ".xlsx", ".pptx"}
            last_report = time.monotonic()
            completed_bytes = 0
            total_bytes = sum(int(entry["size"]) for entry in expected.values()) + (temp_dir / "manifest.json").stat().st_size
            if on_progress is not None:
                on_progress(0, total_bytes)
            with zipfile.ZipFile(temp_dir / "archive.zip", "w", allowZip64=True) as archive:
                for name in (*expected, "manifest.json"):
                    path = checked_path(name)
                    info = zipfile.ZipInfo(name)
                    info.compress_type = zipfile.ZIP_STORED if path.suffix.lower() in stored else zipfile.ZIP_DEFLATED
                    digest = hashlib.sha256()
                    size = 0
                    with path.open("rb") as source, archive.open(info, "w", force_zip64=True) as destination:
                        for block in iter(lambda: source.read(1024 * 1024), b""):
                            destination.write(block)
                            digest.update(block)
                            size += len(block)
                            completed_bytes += len(block)
                            if on_file is not None and time.monotonic() - last_report >= 0.5:
                                on_file()
                                if on_progress is not None:
                                    on_progress(completed_bytes, total_bytes)
                                last_report = time.monotonic()
                    if name in expected and (size != int(expected[name]["size"]) or digest.hexdigest() != str(expected[name]["sha256"])):
                        raise ArchiveError(ErrorCode.EXPORT_FAILED)
                    if on_file is not None and time.monotonic() - last_report >= 0.5:
                        on_file()
                        last_report = time.monotonic()
            if on_progress is not None:
                on_progress(completed_bytes, total_bytes)
        else:
            for name, entry in expected.items():
                path = checked_path(name)
                if path.stat().st_size != int(entry["size"]) or _sha256_file(path) != str(entry["sha256"]):
                    raise ArchiveError(ErrorCode.EXPORT_FAILED)
        final_dir = self.final_dir(export_id)
        if final_dir.exists():
            raise ArchiveError(ErrorCode.EXPORT_FAILED)
        final_dir.parent.mkdir(parents=True, exist_ok=True)
        if on_file is not None:
            on_file()
        temp_dir.replace(final_dir)
        self._scope_snapshot(export_id).unlink(missing_ok=True)
        if (final_dir / "archive.zip").is_file():
            return (final_dir / "archive.zip").stat().st_size
        return sum(path.stat().st_size for path in final_dir.iterdir() if path.is_file())

    def bundle_available(self, record: ExportArtifactRecord) -> bool:
        return not self.deletion_pending(record.id) and record.status == "ready" and record.format == "html" and record.artifact_name is not None and (self.final_dir(record.artifact_name) / "archive.zip").is_file()

    def list_history(
        self,
        account_id: str,
        *,
        repository: ExportHistoryRepository,
    ) -> tuple[ExportArtifactRecord, ...]:
        """Return history, demoting ready artifacts whose files were deleted."""
        for record in repository.export_history(account_id):
            if record.status == "ready" and record.artifact_name and not self.deletion_pending(record.id):
                if not self.final_dir(record.artifact_name).is_dir() or (
                    record.format == "html"
                    and (self.final_dir(record.artifact_name) / "report.json").is_file()
                    and not (self.final_dir(record.artifact_name) / "archive.zip").is_file()
                ):
                    repository.mark_export_missing(record.id)
        return repository.export_history(account_id)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )


_MESSAGE_FIELDS = (
    "id",
    "conversation_id",
    "sender_uid",
    "sender_display_name",
    "created_at",
    "message_type",
    "content_text",
    "is_self",
    "reply_to_id",
    "parse_status",
)


def _message_record(row: sqlite3.Row) -> dict[str, object]:
    record: dict[str, object] = {}
    for field in _MESSAGE_FIELDS:
        value: object = row[field]
        if field == "is_self":
            value = bool(row[field])
        record[field] = value
    return record


class _MessageWriter:
    """Streaming writer contract: add() records, close via context manager."""

    def __init__(self, temp_dir: Path) -> None:
        self._temp_dir = temp_dir

    def __enter__(self) -> _MessageWriter:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.finish()

    def add(self, record: dict[str, object]) -> None:
        raise NotImplementedError

    def finish(self) -> None:
        """Flush and close the underlying file."""


class _JsonlWriter(_MessageWriter):
    def __init__(self, temp_dir: Path) -> None:
        super().__init__(temp_dir)
        self._stream = (temp_dir / "messages.jsonl").open("w", encoding="utf-8", newline="\n")

    def add(self, record: dict[str, object]) -> None:
        self._stream.write(json.dumps(record, ensure_ascii=False, sort_keys=True))
        self._stream.write("\n")

    def finish(self) -> None:
        self._stream.close()


class _JsonWriter(_MessageWriter):
    def __init__(self, temp_dir: Path) -> None:
        super().__init__(temp_dir)
        self._records: list[dict[str, object]] = []

    def add(self, record: dict[str, object]) -> None:
        self._records.append(record)

    def finish(self) -> None:
        _write_json(self._temp_dir / "messages.json", {"messages": self._records})


_CSV_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _guard_csv_value(value: object) -> object:
    if isinstance(value, str) and value.startswith(_CSV_FORMULA_PREFIXES):
        return f"'{value}"
    return value


class _CsvWriter(_MessageWriter):
    def __init__(self, temp_dir: Path) -> None:
        super().__init__(temp_dir)
        self._stream = (temp_dir / "messages.csv").open("w", encoding="utf-8", newline="")
        self._writer = csv.DictWriter(
            self._stream,
            fieldnames=[str(field) for field in _MESSAGE_FIELDS],
        )
        self._writer.writeheader()

    def add(self, record: dict[str, object]) -> None:
        self._writer.writerow({key: _guard_csv_value(value) for key, value in record.items()})

    def finish(self) -> None:
        self._stream.close()


_HTML_PREFIX = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>聊天记录导出</title>
<style>
body { font-family: system-ui, sans-serif; margin: 2rem auto; max-width: 48rem; }
article { border-bottom: 1px solid #ddd; padding: 0.5rem 0; }
header { color: #666; font-size: 0.8rem; }
p { margin: 0.25rem 0 0; white-space: pre-wrap; word-break: break-word; }
h2 { margin-top: 2rem; }
</style>
</head>
<body>
<h1>聊天记录导出</h1>
"""
_HTML_SUFFIX = "</body>\n</html>\n"


class _HtmlWriter(_MessageWriter):
    def __init__(self, temp_dir: Path) -> None:
        super().__init__(temp_dir)
        self._stream = (temp_dir / "viewer.html").open("w", encoding="utf-8", newline="\n")
        self._stream.write(_HTML_PREFIX)
        self._current_conversation: str | None = None

    def add(self, record: dict[str, object]) -> None:
        conversation_id = str(record["conversation_id"])
        if conversation_id != self._current_conversation:
            self._stream.write(f"<h2>{html.escape(conversation_id)}</h2>\n")
            self._current_conversation = conversation_id
        sender = record["sender_display_name"] or record["sender_uid"]
        self._stream.write(
            "<article><header>"
            f"{html.escape(str(sender))} · {html.escape(str(record['created_at']))}"
            f" · {html.escape(str(record['message_type']))}"
            "</header>"
            f"<p>{html.escape(str(record['content_text']))}</p>"
            "</article>\n"
        )

    def finish(self) -> None:
        self._stream.write(_HTML_SUFFIX)
        self._stream.close()


_FORMAT_WRITERS: dict[ExportFormat, type[_MessageWriter]] = {
    ExportFormat.JSONL: _JsonlWriter,
    ExportFormat.JSON: _JsonWriter,
    ExportFormat.CSV: _CsvWriter,
    ExportFormat.HTML: _HtmlWriter,
}


@dataclass(frozen=True)
class ExportOutcome:
    job_id: str
    export_id: str
    status: JobStatus
    record_count: int
    size_bytes: int | None


class _ExportStopped(Exception):
    def __init__(self, status: JobStatus) -> None:
        super().__init__(status.value)
        self.status = status


_STAGE_WRITE = "write"
_STAGE_PUBLISH = "publish"


class ExportWorker:
    """Drive one export job: estimate, write, verify, publish, record history."""

    def __init__(
        self,
        *,
        archive: ExportHistoryRepository,
        jobs: JobRepository,
        service: ExportService,
        heartbeat_interval_seconds: float = 10.0,
    ) -> None:
        self._archive = archive
        self._jobs = jobs
        self._service = service
        self._heartbeat_interval_seconds = heartbeat_interval_seconds
        self._last_progress = 0
        self._message_total: int | None = None

    def run(
        self,
        job_id: str,
        *,
        export_id: str,
        scope: ExportScope,
        export_format: ExportFormat,
    ) -> ExportOutcome:
        job = self._jobs.start_job(job_id)
        self._last_progress = job.progress_current
        self._jobs.report_stage_progress(job.id, stage="prepare", progress_current=0, progress_total=None, progress_unit=None)
        try:
            with JobHeartbeat(self._jobs, job.id, interval_seconds=self._heartbeat_interval_seconds):
                return self._run(job.id, export_id, scope, export_format)
        except ArchiveError as error:
            self._jobs.fail_job(
                job_id,
                error_code=error.code.value,
                details_ref=error.details_ref,
            )
            raise

    def _control(self, job_id: str, stage: str, progress: int) -> None:
        progress = max(progress, self._last_progress)
        self._last_progress = progress
        result = self._jobs.checkpoint(
            job_id,
            stage=stage,
            progress_current=progress,
            checkpoint={"batch_index": progress},
            stage_progress_current=progress if stage == _STAGE_WRITE else None,
            stage_progress_total=self._message_total if stage == _STAGE_WRITE else None,
            stage_progress_unit="items" if stage == _STAGE_WRITE else None,
        )
        if result.action != "continue":
            raise _ExportStopped(result.job.status)

    def _publish_progress(self, job_id: str, current: int, total: int) -> None:
        self._jobs.report_stage_progress(job_id, stage=_STAGE_PUBLISH, progress_current=current, progress_total=total, progress_unit="bytes")

    def _set_message_total(self, total: int) -> None:
        self._message_total = total

    def _run(
        self,
        job_id: str,
        export_id: str,
        scope: ExportScope,
        export_format: ExportFormat,
    ) -> ExportOutcome:
        if export_format is not ExportFormat.HTML:
            self._service.estimate(scope)
        existing = next(
            (
                record
                for record in self._archive.export_history(scope.account_id)
                if record.id == export_id
            ),
            None,
        )
        if existing is None:
            self._archive.record_export_started(
                ExportArtifactRecord(
                    id=export_id,
                    source_account_id=scope.account_id,
                    format=export_format.value,
                    scope_json=json.dumps(
                        {
                            "conversation_ids": list(scope.conversation_ids),
                            "date_from": scope.date_from,
                            "date_to": scope.date_to,
                            "message_types": list(scope.message_types),
                            "attachment_kinds": list(scope.attachment_kinds),
                            "title": scope.title,
                        },
                        ensure_ascii=False,
                        sort_keys=True,
                    ),
                    status="running",
                    artifact_name=None,
                    record_count=None,
                    size_bytes=None,
                    error_code=None,
                    created_at=int(time.time()),
                    finished_at=None,
                )
            )
        elif existing.status != "running" or existing.format != export_format.value:
            raise ArchiveError(ErrorCode.EXPORT_FAILED)
        try:
            record_count = self._service.write_artifact_files(
                export_id,
                scope,
                export_format,
                on_chunk=lambda written: self._control(job_id, _STAGE_WRITE, written),
                on_stage=lambda stage, written: self._control(job_id, stage, written),
                on_total=self._set_message_total,
            )
            self._control(job_id, _STAGE_PUBLISH, record_count)
            size_bytes = self._service.publish(
                export_id,
                on_file=lambda: self._control(job_id, _STAGE_PUBLISH, record_count),
                on_progress=lambda current, total: self._publish_progress(job_id, current, total),
            )
        except _ExportStopped as stopped:
            self._service.discard_temp(export_id, keep_scope=stopped.status is JobStatus.PAUSED)
            if stopped.status is JobStatus.CANCELLED:
                self._archive.finish_export(export_id, status="cancelled")
            return ExportOutcome(
                job_id=job_id,
                export_id=export_id,
                status=stopped.status,
                record_count=0,
                size_bytes=None,
            )
        except ArchiveError:
            self._service.discard_temp(export_id)
            self._archive.finish_export(
                export_id,
                status="failed",
                error_code=ErrorCode.EXPORT_FAILED.value,
            )
            raise
        except (OSError, sqlite3.Error):
            self._service.discard_temp(export_id)
            self._archive.finish_export(
                export_id,
                status="failed",
                error_code=ErrorCode.EXPORT_FAILED.value,
            )
            raise ArchiveError(ErrorCode.EXPORT_FAILED, retryable=True) from None

        self._archive.finish_export(
            export_id,
            status="ready",
            artifact_name=export_id,
            record_count=record_count,
            size_bytes=size_bytes,
        )
        completed = self._jobs.complete_job(job_id)
        return ExportOutcome(
            job_id=job_id,
            export_id=export_id,
            status=completed.status,
            record_count=record_count,
            size_bytes=size_bytes,
        )

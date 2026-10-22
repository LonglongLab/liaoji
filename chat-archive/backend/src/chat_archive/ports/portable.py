"""Vendor-neutral portable archive row contract.

Defines the six row value objects that mirror the archive-scoped tables
(`archive_records`, `record_conflicts`, `sync_batches`, `device_cursors`,
`blob_manifests`, `archive_sequences`), their JSON codec, deterministic
paging keys/cursors, and the `PortableArchiveStore` protocol implemented by
each metadata backend adapter to export/import a single archive as a
portable migration bundle.

Derived data (`archive_records.normalized_content`, `archive_stats`) is
intentionally excluded: the target backend recomputes it on import. This
module is pure — it must never import an adapter, a `server.*` module,
boto3, psycopg or sqlite3.
"""

from __future__ import annotations

import base64
import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Protocol, cast


# v2: batches and cursors carry source_account_id, because server cursors
# are scoped per upload stream. A v1 bundle is rejected rather than
# half-imported: its cursor rows cannot be attributed to an account.
PORTABLE_ARCHIVE_SCHEMA_VERSION = "chat-archive.portable-archive.v2"

_SHA256_HEX = re.compile(r"^[0-9a-f]{64}$")
_MANIFEST_STATES = frozenset({"pending_upload", "uploading_parts", "remote_ready"})


class PortableValidationError(ValueError):
    """A portable row, page or summary violates the fixed vendor-neutral contract."""


class PortableConflictError(RuntimeError):
    """A portable import target is not the empty archive the contract requires."""


class PortableSection(StrEnum):
    RECORDS = "records"
    CONFLICTS = "conflicts"
    BATCHES = "batches"
    CURSORS = "cursors"
    BLOB_MANIFESTS = "blob_manifests"
    ARCHIVE = "archive"


PORTABLE_SECTION_ORDER: tuple[PortableSection, ...] = (
    PortableSection.RECORDS,
    PortableSection.CONFLICTS,
    PortableSection.BATCHES,
    PortableSection.CURSORS,
    PortableSection.BLOB_MANIFESTS,
    PortableSection.ARCHIVE,
)


def _require_str(value: str, *, field_name: str) -> None:
    if type(value) is not str:
        raise PortableValidationError(f"{field_name} must be a string")


def _require_non_empty_str(value: str, *, field_name: str) -> None:
    _require_str(value, field_name=field_name)
    if not value:
        raise PortableValidationError(f"{field_name} must not be empty")


def _require_optional_non_empty_str(value: str | None, *, field_name: str) -> None:
    if value is not None:
        _require_non_empty_str(value, field_name=field_name)


def _require_int(value: int, *, field_name: str) -> None:
    # ``type(...) is int`` also rejects bool, which is an int subclass.
    if type(value) is not int:
        raise PortableValidationError(f"{field_name} must be an integer")


def _require_non_negative_int(value: int, *, field_name: str) -> None:
    if type(value) is not int or value < 0:
        raise PortableValidationError(f"{field_name} must be a non-negative integer")


def _require_sha256(value: str, *, field_name: str) -> None:
    if type(value) is not str or _SHA256_HEX.fullmatch(value) is None:
        raise PortableValidationError(f"{field_name} must be lowercase SHA-256 hex")


@dataclass(frozen=True)
class PortableRecordRow:
    record_key: str
    conversation_id: str
    created_at: int
    content_text: str = field(repr=False)
    content_sha256: str
    source_instance_id: str | None
    origin_device_id: str | None
    server_sequence: int

    def __post_init__(self) -> None:
        _require_non_empty_str(self.record_key, field_name="record_key")
        # ``conversation_id`` and ``created_at`` are constrained to their type
        # only. ``validate_batch`` bounds neither, so a store can legitimately
        # already hold an empty conversation_id or a negative created_at. A
        # portable row faithfully transports a row the store accepted; it is
        # not a retroactive schema gate. Rejecting them here would make one
        # such record render its whole archive permanently unexportable.
        _require_str(self.conversation_id, field_name="conversation_id")
        _require_int(self.created_at, field_name="created_at")
        _require_str(self.content_text, field_name="content_text")
        _require_sha256(self.content_sha256, field_name="content_sha256")
        _require_optional_non_empty_str(
            self.source_instance_id, field_name="source_instance_id"
        )
        _require_optional_non_empty_str(self.origin_device_id, field_name="origin_device_id")
        _require_non_negative_int(self.server_sequence, field_name="server_sequence")


@dataclass(frozen=True)
class PortableConflictRow:
    server_sequence: int
    record_key: str
    canonical_content_sha256: str
    incoming_content_sha256: str
    canonical_source_instance_id: str | None
    incoming_source_instance_id: str | None
    incoming_schema_version: str | None
    canonical_device_id: str | None
    incoming_device_id: str | None
    detected_at: int

    def __post_init__(self) -> None:
        _require_non_negative_int(self.server_sequence, field_name="server_sequence")
        _require_non_empty_str(self.record_key, field_name="record_key")
        _require_sha256(
            self.canonical_content_sha256, field_name="canonical_content_sha256"
        )
        _require_sha256(
            self.incoming_content_sha256, field_name="incoming_content_sha256"
        )
        _require_optional_non_empty_str(
            self.canonical_source_instance_id, field_name="canonical_source_instance_id"
        )
        _require_optional_non_empty_str(
            self.incoming_source_instance_id, field_name="incoming_source_instance_id"
        )
        _require_optional_non_empty_str(
            self.incoming_schema_version, field_name="incoming_schema_version"
        )
        _require_optional_non_empty_str(
            self.canonical_device_id, field_name="canonical_device_id"
        )
        _require_optional_non_empty_str(
            self.incoming_device_id, field_name="incoming_device_id"
        )
        _require_non_negative_int(self.detected_at, field_name="detected_at")


@dataclass(frozen=True)
class PortableBatchRow:
    device_id: str
    # None on batches committed before cursors were account-scoped; a portable
    # export must carry that honestly rather than invent a stream.
    source_account_id: str | None
    client_batch_id: str
    payload_sha256: str
    server_sequence_start: int
    server_sequence_end: int
    inserted_count: int
    updated_count: int
    conflict_count: int

    def __post_init__(self) -> None:
        _require_non_empty_str(self.device_id, field_name="device_id")
        if self.source_account_id is not None:
            _require_non_empty_str(self.source_account_id, field_name="source_account_id")
        _require_non_empty_str(self.client_batch_id, field_name="client_batch_id")
        _require_sha256(self.payload_sha256, field_name="payload_sha256")
        _require_non_negative_int(
            self.server_sequence_start, field_name="server_sequence_start"
        )
        _require_non_negative_int(
            self.server_sequence_end, field_name="server_sequence_end"
        )
        if self.server_sequence_end < self.server_sequence_start:
            raise PortableValidationError(
                "server_sequence_end must not precede server_sequence_start"
            )
        _require_non_negative_int(self.inserted_count, field_name="inserted_count")
        _require_non_negative_int(self.updated_count, field_name="updated_count")
        _require_non_negative_int(self.conflict_count, field_name="conflict_count")


@dataclass(frozen=True)
class PortableCursorRow:
    device_id: str
    source_account_id: str
    last_local_sequence: int

    def __post_init__(self) -> None:
        _require_non_empty_str(self.device_id, field_name="device_id")
        _require_non_empty_str(self.source_account_id, field_name="source_account_id")
        _require_non_negative_int(
            self.last_local_sequence, field_name="last_local_sequence"
        )


@dataclass(frozen=True)
class PortableBlobRow:
    blob_id: str
    size: int
    sha256: str
    state: str

    def __post_init__(self) -> None:
        _require_non_empty_str(self.blob_id, field_name="blob_id")
        _require_non_negative_int(self.size, field_name="size")
        _require_sha256(self.sha256, field_name="sha256")
        if type(self.state) is not str or self.state not in _MANIFEST_STATES:
            raise PortableValidationError("blob state is not recognized")


@dataclass(frozen=True)
class PortableArchiveRow:
    last_server_sequence: int

    def __post_init__(self) -> None:
        _require_non_negative_int(
            self.last_server_sequence, field_name="last_server_sequence"
        )


PortableRow = (
    PortableRecordRow
    | PortableConflictRow
    | PortableBatchRow
    | PortableCursorRow
    | PortableBlobRow
    | PortableArchiveRow
)


def _expected_row_type(section: PortableSection) -> type[object]:
    if section is PortableSection.RECORDS:
        return PortableRecordRow
    if section is PortableSection.CONFLICTS:
        return PortableConflictRow
    if section is PortableSection.BATCHES:
        return PortableBatchRow
    if section is PortableSection.CURSORS:
        return PortableCursorRow
    if section is PortableSection.BLOB_MANIFESTS:
        return PortableBlobRow
    return PortableArchiveRow


@dataclass(frozen=True)
class PortablePage:
    section: PortableSection
    rows: tuple[PortableRow, ...]
    next_cursor: str | None

    def __post_init__(self) -> None:
        if not isinstance(cast(object, self.section), PortableSection):
            raise PortableValidationError("portable page section is invalid")
        expected = _expected_row_type(self.section)
        if not isinstance(cast(object, self.rows), tuple) or any(
            type(row) is not expected for row in cast(tuple[object, ...], self.rows)
        ):
            raise PortableValidationError("portable page rows do not match its section")
        if self.next_cursor is not None and type(self.next_cursor) is not str:
            raise PortableValidationError("portable page cursor must be a string or null")


@dataclass(frozen=True)
class ImportSummary:
    message_count: int
    conversation_count: int
    last_server_sequence: int
    section_counts: Mapping[PortableSection, int]

    def __post_init__(self) -> None:
        _require_non_negative_int(self.message_count, field_name="message_count")
        _require_non_negative_int(
            self.conversation_count, field_name="conversation_count"
        )
        _require_non_negative_int(
            self.last_server_sequence, field_name="last_server_sequence"
        )
        if not isinstance(cast(object, self.section_counts), Mapping):
            raise PortableValidationError("section_counts must be a mapping")
        if set(self.section_counts) != set(PORTABLE_SECTION_ORDER):
            raise PortableValidationError(
                "section_counts must cover every portable section"
            )
        for count in self.section_counts.values():
            _require_non_negative_int(count, field_name="section row count")


_SECTION_FIELDS: dict[PortableSection, frozenset[str]] = {
    PortableSection.RECORDS: frozenset(
        {
            "record_key",
            "conversation_id",
            "created_at",
            "content_text",
            "content_sha256",
            "source_instance_id",
            "origin_device_id",
            "server_sequence",
        }
    ),
    PortableSection.CONFLICTS: frozenset(
        {
            "server_sequence",
            "record_key",
            "canonical_content_sha256",
            "incoming_content_sha256",
            "canonical_source_instance_id",
            "incoming_source_instance_id",
            "incoming_schema_version",
            "canonical_device_id",
            "incoming_device_id",
            "detected_at",
        }
    ),
    PortableSection.BATCHES: frozenset(
        {
            "device_id",
            "source_account_id",
            "client_batch_id",
            "payload_sha256",
            "server_sequence_start",
            "server_sequence_end",
            "inserted_count",
            "updated_count",
            "conflict_count",
        }
    ),
    PortableSection.CURSORS: frozenset(
        {"device_id", "source_account_id", "last_local_sequence"}
    ),
    PortableSection.BLOB_MANIFESTS: frozenset({"blob_id", "size", "sha256", "state"}),
    PortableSection.ARCHIVE: frozenset({"last_server_sequence"}),
}


def encode_portable_row(row: PortableRow) -> dict[str, object]:
    """Serialize one portable row to its canonical JSON-compatible field set."""
    if type(row) is PortableRecordRow:
        return {
            "record_key": row.record_key,
            "conversation_id": row.conversation_id,
            "created_at": row.created_at,
            "content_text": row.content_text,
            "content_sha256": row.content_sha256,
            "source_instance_id": row.source_instance_id,
            "origin_device_id": row.origin_device_id,
            "server_sequence": row.server_sequence,
        }
    if type(row) is PortableConflictRow:
        return {
            "server_sequence": row.server_sequence,
            "record_key": row.record_key,
            "canonical_content_sha256": row.canonical_content_sha256,
            "incoming_content_sha256": row.incoming_content_sha256,
            "canonical_source_instance_id": row.canonical_source_instance_id,
            "incoming_source_instance_id": row.incoming_source_instance_id,
            "incoming_schema_version": row.incoming_schema_version,
            "canonical_device_id": row.canonical_device_id,
            "incoming_device_id": row.incoming_device_id,
            "detected_at": row.detected_at,
        }
    if type(row) is PortableBatchRow:
        return {
            "device_id": row.device_id,
            "source_account_id": row.source_account_id,
            "client_batch_id": row.client_batch_id,
            "payload_sha256": row.payload_sha256,
            "server_sequence_start": row.server_sequence_start,
            "server_sequence_end": row.server_sequence_end,
            "inserted_count": row.inserted_count,
            "updated_count": row.updated_count,
            "conflict_count": row.conflict_count,
        }
    if type(row) is PortableCursorRow:
        return {
            "device_id": row.device_id,
            "source_account_id": row.source_account_id,
            "last_local_sequence": row.last_local_sequence,
        }
    if type(row) is PortableBlobRow:
        return {
            "blob_id": row.blob_id,
            "size": row.size,
            "sha256": row.sha256,
            "state": row.state,
        }
    if type(row) is PortableArchiveRow:
        return {"last_server_sequence": row.last_server_sequence}
    raise PortableValidationError("unsupported portable row type")


def _reject_duplicate_keys(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for name, value in pairs:
        if name in result:
            raise PortableValidationError("portable row JSON key is duplicated")
        result[name] = value
    return result


def _exact_object(value: object, names: frozenset[str]) -> dict[str, object]:
    if not isinstance(value, dict):
        raise PortableValidationError("portable row document must be a JSON object")
    document = cast(dict[object, object], value)
    if any(type(name) is not str for name in document):
        raise PortableValidationError("portable row document key is invalid")
    normalized = cast(dict[str, object], document)
    if set(normalized) != set(names):
        raise PortableValidationError("portable row field set is invalid")
    return normalized


def _string(document: Mapping[str, object], name: str) -> str:
    value = document[name]
    if type(value) is not str:
        raise PortableValidationError(f"{name} must be a JSON string")
    return value


def _optional_string(document: Mapping[str, object], name: str) -> str | None:
    value = document[name]
    if value is None:
        return None
    if type(value) is not str:
        raise PortableValidationError(f"{name} must be a JSON string or null")
    return value


def _integer(document: Mapping[str, object], name: str) -> int:
    value = document[name]
    if type(value) is not int:
        raise PortableValidationError(f"{name} must be a JSON integer")
    return value


def decode_portable_row(section: PortableSection, document: object) -> PortableRow:
    """Parse and validate one portable row, rejecting any malformed input.

    ``document`` may already be a parsed JSON object (e.g. built directly by
    a caller or reused from :func:`encode_portable_row`), or raw JSON text
    (``str``/``bytes``). Only the raw-text path can exhibit duplicate object
    keys, which are rejected there via an ``object_pairs_hook``; a
    pre-parsed ``dict`` has necessarily already lost that information.
    """
    if not isinstance(cast(object, section), PortableSection):
        raise PortableValidationError("portable section is invalid")
    if isinstance(document, (str, bytes)):
        try:
            document = json.loads(document, object_pairs_hook=_reject_duplicate_keys)
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise PortableValidationError("portable row JSON is invalid") from None
    fields = _exact_object(document, _SECTION_FIELDS[section])
    if section is PortableSection.RECORDS:
        return PortableRecordRow(
            record_key=_string(fields, "record_key"),
            conversation_id=_string(fields, "conversation_id"),
            created_at=_integer(fields, "created_at"),
            content_text=_string(fields, "content_text"),
            content_sha256=_string(fields, "content_sha256"),
            source_instance_id=_optional_string(fields, "source_instance_id"),
            origin_device_id=_optional_string(fields, "origin_device_id"),
            server_sequence=_integer(fields, "server_sequence"),
        )
    if section is PortableSection.CONFLICTS:
        return PortableConflictRow(
            server_sequence=_integer(fields, "server_sequence"),
            record_key=_string(fields, "record_key"),
            canonical_content_sha256=_string(fields, "canonical_content_sha256"),
            incoming_content_sha256=_string(fields, "incoming_content_sha256"),
            canonical_source_instance_id=_optional_string(
                fields, "canonical_source_instance_id"
            ),
            incoming_source_instance_id=_optional_string(
                fields, "incoming_source_instance_id"
            ),
            incoming_schema_version=_optional_string(fields, "incoming_schema_version"),
            canonical_device_id=_optional_string(fields, "canonical_device_id"),
            incoming_device_id=_optional_string(fields, "incoming_device_id"),
            detected_at=_integer(fields, "detected_at"),
        )
    if section is PortableSection.BATCHES:
        return PortableBatchRow(
            device_id=_string(fields, "device_id"),
            source_account_id=_optional_string(fields, "source_account_id"),
            client_batch_id=_string(fields, "client_batch_id"),
            payload_sha256=_string(fields, "payload_sha256"),
            server_sequence_start=_integer(fields, "server_sequence_start"),
            server_sequence_end=_integer(fields, "server_sequence_end"),
            inserted_count=_integer(fields, "inserted_count"),
            updated_count=_integer(fields, "updated_count"),
            conflict_count=_integer(fields, "conflict_count"),
        )
    if section is PortableSection.CURSORS:
        return PortableCursorRow(
            device_id=_string(fields, "device_id"),
            source_account_id=_string(fields, "source_account_id"),
            last_local_sequence=_integer(fields, "last_local_sequence"),
        )
    if section is PortableSection.BLOB_MANIFESTS:
        return PortableBlobRow(
            blob_id=_string(fields, "blob_id"),
            size=_integer(fields, "size"),
            sha256=_string(fields, "sha256"),
            state=_string(fields, "state"),
        )
    return PortableArchiveRow(last_server_sequence=_integer(fields, "last_server_sequence"))


def portable_row_sort_key(row: PortableRow) -> tuple[object, ...]:
    """Return the fixed per-section ordering key defined in spec §4.1."""
    if type(row) is PortableRecordRow:
        return (row.record_key,)
    if type(row) is PortableConflictRow:
        return (row.server_sequence,)
    if type(row) is PortableBatchRow:
        return (row.device_id, row.client_batch_id)
    if type(row) is PortableCursorRow:
        return (row.device_id,)
    if type(row) is PortableBlobRow:
        return (row.blob_id,)
    if type(row) is PortableArchiveRow:
        return ()
    raise PortableValidationError("unsupported portable row type")


def portable_row_cursor(row: PortableRow) -> str:
    """Derive a deterministic, opaque resume cursor from the row's sort key.

    Two rows sharing the same :func:`portable_row_sort_key` always share the
    same cursor, regardless of any other field, so an adapter can resume
    paging past a row using only this string.
    """
    parts = portable_row_sort_key(row)
    encoded = json.dumps(list(parts), ensure_ascii=True, separators=(",", ":"))
    return base64.urlsafe_b64encode(encoded.encode("utf-8")).decode("ascii")


class PortableArchiveStore(Protocol):
    def export_portable_section(
        self,
        archive_id: str,
        section: PortableSection,
        *,
        cursor: str | None,
        limit: int,
    ) -> PortablePage: ...

    def portable_archive_is_empty(self, archive_id: str) -> bool: ...

    def import_portable_section(
        self,
        archive_id: str,
        section: PortableSection,
        rows: tuple[PortableRow, ...],
    ) -> None: ...

    def finalize_portable_import(self, archive_id: str) -> ImportSummary: ...

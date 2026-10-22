"""Dialect-neutral facts shared by every backend's ``PortableArchiveStore``.

The three metadata backends keep an archive in identically named tables and
columns, and spec §4.1 fixes one sort key per section for all of them, so the
table/column/sort mappings, the cursor decoder and the in-flight import
bookkeeping live here once instead of being copied into each adapter — a
second copy is exactly how the backends' cursor and failure semantics would
start to drift apart.

Only dialect-neutral facts belong here; the SQL text itself stays per adapter
(D1 speaks SQLite's dialect, PostgreSQL does not). This module is pure: it
must never import an adapter, a ``server.*`` module, sqlite3 or psycopg.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from dataclasses import dataclass
from typing import cast

from chat_archive.ports.portable import (
    PORTABLE_SECTION_ORDER,
    PortableArchiveRow,
    PortableBatchRow,
    PortableBlobRow,
    PortableConflictError,
    PortableConflictRow,
    PortableCursorRow,
    PortableRecordRow,
    PortableRow,
    PortableSection,
    PortableValidationError,
)
from chat_archive.search import normalize_search_text


PORTABLE_BLOB_STATES = ("pending_upload", "uploading_parts", "remote_ready")

PORTABLE_TABLES: dict[PortableSection, str] = {
    PortableSection.RECORDS: "archive_records",
    PortableSection.CONFLICTS: "record_conflicts",
    PortableSection.BATCHES: "sync_batches",
    PortableSection.CURSORS: "device_cursors",
    PortableSection.BLOB_MANIFESTS: "blob_manifests",
    PortableSection.ARCHIVE: "archive_sequences",
}
# Every archive-scoped table an import target must be empty of, including the
# materialized aggregates that are not themselves a portable section.
PORTABLE_OCCUPIED_TABLES = (*PORTABLE_TABLES.values(), "archive_stats")

# The portable field set of each section, in the row type's field order.
PORTABLE_COLUMNS: dict[PortableSection, tuple[str, ...]] = {
    PortableSection.RECORDS: (
        "record_key",
        "conversation_id",
        "created_at",
        "content_text",
        "content_sha256",
        "source_instance_id",
        "origin_device_id",
        "server_sequence",
    ),
    PortableSection.CONFLICTS: (
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
    ),
    PortableSection.BATCHES: (
        "device_id",
        "source_account_id",
        "client_batch_id",
        "payload_sha256",
        "server_sequence_start",
        "server_sequence_end",
        "inserted_count",
        "updated_count",
        "conflict_count",
    ),
    PortableSection.CURSORS: ("device_id", "source_account_id", "last_local_sequence"),
    PortableSection.BLOB_MANIFESTS: ("blob_id", "size", "sha256", "state"),
    PortableSection.ARCHIVE: ("last_server_sequence",),
}

# ``normalized_content`` is derived, so it is written on import but never read
# into a portable row.
PORTABLE_INSERT_COLUMNS: dict[PortableSection, tuple[str, ...]] = {
    section: (
        (*columns, "normalized_content") if section is PortableSection.RECORDS else columns
    )
    for section, columns in PORTABLE_COLUMNS.items()
}

# spec §4.1: the fixed per-section sort key, which is also what the opaque
# export cursor encodes. It is additionally the archive-scoped primary key of
# each table (the full key is ``archive_id`` plus these columns), which is what
# lets an adapter recognize a duplicate row identity without asking the driver.
PORTABLE_SORT_COLUMNS: dict[PortableSection, tuple[str, ...]] = {
    PortableSection.RECORDS: ("record_key",),
    PortableSection.CONFLICTS: ("server_sequence",),
    PortableSection.BATCHES: ("device_id", "client_batch_id"),
    PortableSection.CURSORS: ("device_id", "source_account_id"),
    PortableSection.BLOB_MANIFESTS: ("blob_id",),
    PortableSection.ARCHIVE: (),
}
# Written out per column, parallel to PORTABLE_SORT_COLUMNS, so adding an
# int-sorted section cannot silently inherit the wrong type.
PORTABLE_SORT_KEY_TYPES: dict[PortableSection, tuple[type[object], ...]] = {
    PortableSection.RECORDS: (str,),
    PortableSection.CONFLICTS: (int,),
    PortableSection.BATCHES: (str, str),
    PortableSection.CURSORS: (str, str),
    PortableSection.BLOB_MANIFESTS: (str,),
    PortableSection.ARCHIVE: (),
}

PORTABLE_ROW_TYPES: dict[PortableSection, type[object]] = {
    PortableSection.RECORDS: PortableRecordRow,
    PortableSection.CONFLICTS: PortableConflictRow,
    PortableSection.BATCHES: PortableBatchRow,
    PortableSection.CURSORS: PortableCursorRow,
    PortableSection.BLOB_MANIFESTS: PortableBlobRow,
    PortableSection.ARCHIVE: PortableArchiveRow,
}


def decode_portable_cursor(section: PortableSection, cursor: str) -> tuple[object, ...]:
    """Reverse ``portable_row_cursor`` for one section, rejecting anything else.

    The cursor is opaque to callers but an adapter has to turn it back into the
    section's sort-key values to resume paging, so it is validated for arity
    and per-column type before it reaches a query parameter.

    Note the limit of that validation: ``portable_row_cursor`` encodes the sort
    key alone and carries no section tag, so the single-string cursors of
    ``records``, ``cursors`` and ``blob_manifests`` are byte-identical and
    mutually interchangeable. Cross-section replay is only detected when the
    arity or the key *type* differs (e.g. a records cursor fed to
    ``conflicts``). Callers must not rely on more than that.
    """
    try:
        decoded = json.loads(base64.urlsafe_b64decode(cursor.encode("ascii")))
    except ValueError:
        # UnicodeEncodeError, binascii.Error and JSONDecodeError all subclass it.
        raise PortableValidationError("portable export cursor is malformed") from None
    if not isinstance(decoded, list):
        raise PortableValidationError("portable export cursor does not match its section")
    parts = cast(list[object], decoded)
    expected = PORTABLE_SORT_KEY_TYPES[section]
    if len(parts) != len(expected):
        raise PortableValidationError("portable export cursor does not match its section")
    if any(type(part) is not kind for part, kind in zip(parts, expected, strict=True)):
        raise PortableValidationError("portable export cursor does not match its section")
    return tuple(parts)


def portable_chunks(
    rows: tuple[PortableRow, ...], size: int
) -> Iterator[tuple[PortableRow, ...]]:
    for start in range(0, len(rows), size):
        yield rows[start : start + size]


def portable_insert_parameters(archive_id: str, row: PortableRow) -> tuple[object, ...]:
    """Bind one portable row for insertion, in ``PORTABLE_INSERT_COLUMNS`` order.

    ``normalized_content`` is recomputed here rather than carried in the
    portable row: it is derived data the target backend owns.
    """
    if type(row) is PortableRecordRow:
        return (
            archive_id,
            row.record_key,
            row.conversation_id,
            row.created_at,
            row.content_text,
            row.content_sha256,
            row.source_instance_id,
            row.origin_device_id,
            row.server_sequence,
            normalize_search_text(row.content_text),
        )
    if type(row) is PortableConflictRow:
        return (
            archive_id,
            row.server_sequence,
            row.record_key,
            row.canonical_content_sha256,
            row.incoming_content_sha256,
            row.canonical_source_instance_id,
            row.incoming_source_instance_id,
            row.incoming_schema_version,
            row.canonical_device_id,
            row.incoming_device_id,
            row.detected_at,
        )
    if type(row) is PortableBatchRow:
        return (
            archive_id,
            row.device_id,
            row.source_account_id,
            row.client_batch_id,
            row.payload_sha256,
            row.server_sequence_start,
            row.server_sequence_end,
            row.inserted_count,
            row.updated_count,
            row.conflict_count,
        )
    if type(row) is PortableCursorRow:
        return (
            archive_id,
            row.device_id,
            row.source_account_id,
            row.last_local_sequence,
        )
    if type(row) is PortableBlobRow:
        return (archive_id, row.blob_id, row.size, row.sha256, row.state)
    if type(row) is PortableArchiveRow:
        return (archive_id, row.last_server_sequence)
    raise PortableValidationError("unsupported portable row type")


@dataclass
class PortableImportState:
    """Bookkeeping for one in-flight portable import on one store instance.

    ``counts`` holds what the caller *handed to* ``import_portable_section``,
    counted before the write so a section whose write does not complete leaves
    handed-in greater than persisted.

    ``seen`` holds the row identities already handed in, per section, for the
    backends that decide a duplicate themselves instead of letting the driver
    decide it (D1 cannot: its session reports a constraint violation as the
    same opaque error as an outage). It lives here, alongside ``counts``,
    because a section may arrive over several calls — the export side is paged
    — so a per-call set would let a repeat spanning two calls through, and
    because the one place that resets a section's ``counts`` is the one place
    that has to drop its stale identities too.

    ``failed_section`` names the section whose write did not complete, and is
    what finalize refuses on. Not because the row-count check would miss such a
    section — it would in fact catch it — but because a failed write is a
    structural fact about the import, and inferring it from arithmetic would
    make the refusal depend on that counting placement staying correct. The
    count check survives as an independent second line.

    A recorded failure is not necessarily terminal. If nothing had been
    persisted when it happened, re-entering with that same section retries it
    cleanly; refusing a *different* section in the meantime mutates nothing, so
    the failed section stays retryable afterwards. The trap is the other case:
    once any earlier section is durable, the empty-target guard refuses every
    re-entry — including a retry of the failed section — until an operator
    clears the target, and this instance has no way to release that. There is
    deliberately no abandon API, because that recovery path is meant to run
    through an operator; the migration CLI is a one-shot process, so its next
    run starts from a fresh instance against whatever the operator left.
    """

    counts: dict[PortableSection, int]
    # Keyed by section rather than one flat set: the sort key of ``records``,
    # ``cursors`` and ``blob_manifests`` is a single string, so a record_key
    # equal to some device_id would otherwise collide across sections.
    seen: dict[PortableSection, set[tuple[object, ...]]]
    failed_section: PortableSection | None = None

    @classmethod
    def started(cls) -> PortableImportState:
        return cls(
            counts=dict.fromkeys(PORTABLE_SECTION_ORDER, 0),
            # Not ``dict.fromkeys(..., set())``, which would hand every section
            # the same set object.
            seen={section: set() for section in PORTABLE_SECTION_ORDER},
        )


def resume_portable_import(
    section: PortableSection,
    state: PortableImportState,
    *,
    archive_is_empty: bool,
) -> None:
    """Decide whether re-entry after a failed chunk may continue.

    Three outcomes, defaulting closed:

    * anything is already persisted — the target is no longer the empty archive
      the contract requires, so refuse and leave the operator to clear it
      explicitly;
    * nothing persisted and this is the section that failed — a legitimate
      clean retry, so drop that section's stale handed-in count (keeping it
      would double-count) *and* its stale row identities (keeping those would
      make the retry reject the very rows the abandoned attempt handed in, none
      of which reached the database) and re-arm;
    * nothing persisted and this is a *different* section — the caller
      swallowed the failure and moved on, which would silently drop the failed
      section from the import.

    Shared across backends because these three branches are contract-visible:
    the same shared test file asserts each of them against every param.
    """
    if not archive_is_empty:
        raise PortableConflictError("portable import target archive is not empty")
    if section is not state.failed_section:
        raise PortableValidationError(
            "portable import cannot continue past the section whose write failed"
        )
    state.counts[section] = 0
    state.seen[section].clear()
    state.failed_section = None

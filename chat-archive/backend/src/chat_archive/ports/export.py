"""Minimal export history contract, independent of local acquisition."""

from __future__ import annotations
from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ExportArtifactRecord:
    id: str
    source_account_id: str
    format: str
    scope_json: str
    status: str
    artifact_name: str | None
    record_count: int | None
    size_bytes: int | None
    error_code: str | None
    created_at: int
    finished_at: int | None


class ExportHistoryRepository(Protocol):
    def record_export_started(self, record: ExportArtifactRecord) -> None: ...
    def finish_export(
        self,
        export_id: str,
        *,
        status: str,
        artifact_name: str | None = None,
        record_count: int | None = None,
        size_bytes: int | None = None,
        error_code: str | None = None,
    ) -> None: ...
    def mark_export_missing(self, export_id: str) -> None: ...
    def export_history(self, source_account_id: str) -> tuple[ExportArtifactRecord, ...]: ...
    def get_export(self, source_account_id: str, export_id: str) -> ExportArtifactRecord | None: ...
    def delete_export_record(self, source_account_id: str, export_id: str) -> None: ...

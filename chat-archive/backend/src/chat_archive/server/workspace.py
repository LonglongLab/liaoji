"""Self-hosted orchestration around the shared export worker/state machine."""

from __future__ import annotations
from chat_archive.version import SERVER_VERSION

import hashlib
import json
import os
import secrets
import shutil
import threading
import re
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from pathlib import Path

from chat_archive.adapters.sqlite.export_history import SQLiteExportHistory
from chat_archive.adapters.sqlite.blob_state import SqliteBlobStateStore
from chat_archive.adapters.sqlite.jobs import SQLiteJobRepository
from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.domain.models import JobStatus
from chat_archive.domain.identity import thumbnail_blob_id
from chat_archive.export import ExportFormat, ExportScope, ExportService, ExportWorker
from chat_archive.ports.blob_state import BlobStateStore, BlobManifestRecord
from chat_archive.ports.jobs import JobNotFoundError, JobRecord, JobStateError
from chat_archive.ports.objectstore import ObjectStore, ObjectStoreError
from chat_archive.server.workspace_data import RemoteWorkspaceData
from chat_archive.workspace_contract import ExportScopeRequest


class ArchiveWorkspace:
    def __init__(self, owner: RemoteWorkspace, archive_id: str) -> None:
        self.owner = owner
        self.archive_id = archive_id
        self.root = owner.root / hashlib.sha256(archive_id.encode()).hexdigest()
        self.jobs = SQLiteJobRepository(self.root / "jobs.sqlite")
        self.history = SQLiteExportHistory(self.root / "exports.sqlite")
        self.jobs.recover_after_restart(lambda job: job.task_spec is not None)
        # Queued tasks were explicitly created before the process stopped;
        # re-submit their durable specifications instead of leaving them stuck.
        with closing(sqlite3.connect(self.root / "jobs.sqlite")) as connection:
            queued = [
                str(row[0])
                for row in connection.execute("SELECT id FROM jobs WHERE status='queued'")
            ]
        for job_id in queued:
            self.submit(job_id)

    def require_account(self, account_id: str) -> None:
        with closing(self.owner.data.connection(self.archive_id)) as connection:
            self.owner.data.require_account(connection, account_id)

    def _media_root(self, export_id: str) -> Path:
        path = self.root / ".media" / export_id
        if os.name != "nt":
            return path
        # Hash names and frozen task IDs must remain stable across resume.
        # Opt this private spool into Win32 long paths without a registry change.
        # abspath normalizes components without resolving directory junctions;
        # cleanup must never recurse into a junction's target.
        absolute = os.path.abspath(path)
        if absolute.startswith("\\\\?\\"):
            return Path(absolute)
        if absolute.startswith("\\\\"):
            absolute = "UNC\\" + absolute[2:]
        return Path("\\\\?\\" + absolute)

    def service(self, export_id: str = "preview") -> ExportService:
        if (
            export_id != "preview"
            and re.fullmatch(r"export_[A-Za-z0-9_-]{8,64}", export_id) is None
        ):
            raise ArchiveError(ErrorCode.QUERY_INVALID)
        media_root = self._media_root(export_id)
        service = ExportService(
            self.owner.data.path,
            self.root / "exports",
            connection_factory=lambda: self.owner.data.connection(self.archive_id),
            scope_connection_factory=lambda scope: self.owner.data.connection(self.archive_id, scope=scope),
            media_root=media_root,
            allow_source_media_decode=False,
            app_version=SERVER_VERSION,
        )

        def resolve(account: str, blob_id: str) -> str | None:
            # The frozen selected records are the authority for which blobs
            # this job may fetch. An opaque user-controlled ID alone is not.
            snapshot = service.scope_snapshot_path(export_id)
            with closing(service.frozen_read_connection(snapshot)) as frozen:
                if (
                    frozen.execute(
                        """SELECT 1 FROM attachments a JOIN messages m ON m.id=a.message_id
                    WHERE a.id=? AND m.source_account_id=? LIMIT 1""",
                        (blob_id, account),
                    ).fetchone()
                    is None
                ):
                    return None
            manifest = self.owner.available_manifest(self.archive_id, blob_id)
            if manifest is None:
                return None
            generation = getattr(self.owner.objects, "storage_generation", None)
            if callable(generation) and manifest.storage_generation != generation():
                return None
            media_root.mkdir(parents=True, exist_ok=True)
            name = hashlib.sha256(blob_id.encode()).hexdigest() + (
                ".thumbnail.bin" if manifest.blob_id != blob_id else ".bin"
            )
            target = media_root / name
            if not target.exists():
                staging = target.with_suffix(".part")
                try:
                    with staging.open("wb") as sink:
                        attributes = self.owner.objects.download_object(
                            manifest.object_scope, manifest.object_key, sink
                        )
                    if attributes.size != manifest.size or attributes.sha256 != manifest.sha256:
                        staging.unlink(missing_ok=True)
                        return None
                    staging.replace(target)
                except (ObjectStoreError, OSError):
                    staging.unlink(missing_ok=True)
                    return None
            return name

        service.resolve_media = resolve
        return service

    def job(self, job_id: str, account_id: str) -> JobRecord:
        self.require_account(account_id)
        try:
            job = self.jobs.get_job(job_id)
        except JobNotFoundError:
            raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND) from None
        if job.source_account_id != account_id or job.kind != "export":
            raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)
        return job

    def create(self, scope: ExportScope, export_format: ExportFormat) -> tuple[str, str]:
        self.require_account(scope.account_id)
        self.service().validate_scope(scope)
        job_id = "job_" + secrets.token_urlsafe(18)
        export_id = "export_" + secrets.token_urlsafe(18)
        # Persist the task before submitting it, including the immutable scope
        # so pause/resume and process restart use exactly the same request.
        spec = {
            "export_id": export_id,
            "format": export_format.value,
            "scope": json.dumps(
                {
                    "account_id": scope.account_id,
                    "conversation_ids": list(scope.conversation_ids),
                    "date_from": scope.date_from,
                    "date_to": scope.date_to,
                    "message_types": list(scope.message_types),
                    "attachment_kinds": list(scope.attachment_kinds),
                    "title": scope.title,
                }
            ),
        }
        self.jobs.create_job(
            job_id=job_id, source_account_id=scope.account_id, kind="export", task_spec=spec
        )
        self.submit(job_id)
        return job_id, export_id

    def submit(self, job_id: str) -> None:
        self.owner.executor.submit(self._run, job_id)

    def discard(self, export_id: str) -> None:
        self.service(export_id).discard_temp(export_id)
        shutil.rmtree(self._media_root(export_id), ignore_errors=True)

    def _run(self, job_id: str) -> None:
        job = self.jobs.get_job(job_id)
        if job.status is not JobStatus.QUEUED:
            return
        spec = job.task_spec or {}
        export_id = str(spec["export_id"])
        scope = ExportScopeRequest.model_validate_json(str(spec["scope"])).to_domain()
        service = self.service(export_id)
        try:
            ExportWorker(archive=self.history, jobs=self.jobs, service=service).run(
                job_id,
                export_id=export_id,
                scope=scope,
                export_format=ExportFormat(str(spec["format"])),
            )
        except (ArchiveError, OSError, JobStateError):
            # The shared worker records domain failures. Never log content,
            # file names, object keys, or task specifications.
            if self.jobs.get_job(job_id).status not in {
                JobStatus.FAILED,
                JobStatus.CANCELLED,
                JobStatus.PAUSED,
            }:
                self.jobs.fail_job(job_id, error_code=ErrorCode.EXPORT_FAILED.value)
        finally:
            if self.jobs.get_job(job_id).status is not JobStatus.PAUSED:
                shutil.rmtree(self._media_root(export_id), ignore_errors=True)


class RemoteWorkspace:
    def __init__(
        self, metadata_path: Path, root: Path, blobs: BlobStateStore, objects: ObjectStore
    ) -> None:
        def blob_state(archive_id: str, blob_id: str) -> str | None:
            manifest = self.available_manifest(archive_id, blob_id)
            if manifest is not None:
                return "remote_ready"
            original = blobs.get_manifest(archive_id, blob_id)
            return original.state if original is not None and original.state != "remote_ready" else "pending_upload" if original is not None else None

        def blob_states(archive_id: str, blob_ids: tuple[str, ...]) -> dict[str, str | None]:
            if not isinstance(blobs, SqliteBlobStateStore):
                return {blob_id: blob_state(archive_id, blob_id) for blob_id in blob_ids}
            candidates = tuple(candidate for blob_id in blob_ids for candidate in (blob_id, thumbnail_blob_id(blob_id)))
            manifests = blobs.get_manifests(archive_id, candidates)
            generation = getattr(objects, "storage_generation", None)
            current = generation() if callable(generation) else None
            result: dict[str, str | None] = {}
            for blob_id in blob_ids:
                ready = any(
                    (manifest := manifests.get(candidate)) is not None
                    and manifest.state == "remote_ready"
                    and (current is None or manifest.storage_generation == current)
                    for candidate in (blob_id, thumbnail_blob_id(blob_id))
                )
                original = manifests.get(blob_id)
                result[blob_id] = "remote_ready" if ready else original.state if original is not None and original.state != "remote_ready" else "pending_upload" if original is not None else None
            return result

        self.data = RemoteWorkspaceData(metadata_path, blob_state, blob_states=blob_states)
        self.root = root
        self.blobs = blobs
        self.objects = objects
        self.executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="archive-export")
        self._archives: dict[str, ArchiveWorkspace] = {}
        self._lock = threading.Lock()

    def available_manifest(self, archive_id: str, blob_id: str) -> BlobManifestRecord | None:
        generation = getattr(self.objects, "storage_generation", None)
        for candidate in (blob_id, thumbnail_blob_id(blob_id)):
            manifest = self.blobs.get_manifest(archive_id, candidate)
            if (
                manifest is not None
                and manifest.state == "remote_ready"
                and (not callable(generation) or manifest.storage_generation == generation())
            ):
                return manifest
        return None

    def archive(self, archive_id: str) -> ArchiveWorkspace:
        with self._lock:
            if archive_id not in self._archives:
                self._archives[archive_id] = ArchiveWorkspace(self, archive_id)
            return self._archives[archive_id]

    def close(self) -> None:
        self.executor.shutdown(wait=True)
        for archive in self._archives.values():
            archive.jobs.close()

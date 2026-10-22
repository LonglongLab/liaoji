"""Authenticated workspace routes using the same contracts as local pages."""
# pyright: reportUnusedFunction=false

import json
from collections.abc import Callable, Iterator
from dataclasses import asdict
from contextlib import closing
from pathlib import Path

from fastapi import Depends, FastAPI, Query, Request
from fastapi.responses import FileResponse, StreamingResponse

from chat_archive.domain.errors import ArchiveError, ErrorCode
from chat_archive.domain.app_card import card_from_json
from chat_archive.domain.models import JobStatus
from chat_archive.domain.forward_media import card_media_refs
from chat_archive.export import ExportFormat
from chat_archive.export_delete import delete_export as purge_export
from chat_archive.ports.jobs import JobStateError
from chat_archive.server.browser_routes import BrowserGate
from chat_archive.server.workspace import ArchiveWorkspace, RemoteWorkspace
from chat_archive.workspace_contract import ExportRequest, ExportScopeRequest, export_view, job_view


def register_workspace_routes(
    app: FastAPI, gate: BrowserGate, workspace: RemoteWorkspace, archive_id: Callable[[], str]
) -> None:
    def context(request: Request) -> ArchiveWorkspace:
        gate.require(request)
        gate.guard_origin(request)
        return workspace.archive(archive_id())

    @app.get("/api/v1/stats/analysis")
    def analysis(
        account_id: str,
        date_from: str,
        date_to: str,
        conversation_kind: str = "all",
        conversation_id: str | None = None,
        ctx: ArchiveWorkspace = Depends(context),
    ) -> dict[str, object]:
        return asdict(
            workspace.data.analysis(
                ctx.archive_id,
                account_id,
                date_from=date_from,
                date_to=date_to,
                conversation_kind=conversation_kind,
                conversation_id=conversation_id,
            )
        )

    @app.post("/api/v1/exports/estimate")
    def estimate(
        body: ExportScopeRequest, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        ctx.require_account(body.account_id)
        return asdict(ctx.service().estimate(body.to_domain()))

    @app.post("/api/v1/exports/preview")
    def preview(
        body: ExportScopeRequest, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        ctx.require_account(body.account_id)
        service = ctx.service()
        coordinates = service.preview_coordinates(body.to_domain())
        items: list[dict[str, object]] = []
        with closing(workspace.data.connection(ctx.archive_id)) as connection:
            for identifier, _ in coordinates:
                row = connection.execute(
                    "SELECT id,conversation_id,sender_uid,sender_display_name,created_at,message_type,content_text,is_self,reply_to_id,parse_status,app_card_json FROM messages WHERE id=? AND source_account_id=?",
                    (identifier, body.account_id),
                ).fetchone()
                if row is None:
                    continue
                fields = (
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
                item = {key: row[key] for key in fields}
                item["is_self"] = None if row["is_self"] is None else bool(row["is_self"])
                variants = {
                    ref["blob_id"]: ref["variant"] for ref in card_media_refs(row["app_card_json"])
                }
                attachments: list[dict[str, object]] = []
                for attachment in connection.execute(
                    "SELECT id,message_id,kind,file_name,cache_status FROM attachments WHERE message_id=?",
                    (identifier,),
                ):
                    if attachment["kind"] not in body.attachment_kinds:
                        continue
                    manifest = (
                        workspace.available_manifest(ctx.archive_id, str(attachment["id"]))
                        if attachment["cache_status"] == "ready"
                        else None
                    )
                    available = manifest is not None and manifest.state == "remote_ready"
                    attachments.append(
                        {
                            "id": attachment["id"],
                            "message_id": identifier,
                            "kind": attachment["kind"],
                            "file_name": attachment["file_name"],
                            "mime_type": None,
                            "size": manifest.size if manifest is not None else None,
                            "cache_status": "ready" if available else "source_missing",
                            "failure_code": None if available else "ATTACHMENT_SOURCE_MISSING",
                            "attempt_count": 0,
                            "content_available": available,
                            "variant": "thumbnail"
                            if manifest is not None and manifest.blob_id != attachment["id"]
                            else variants.get(str(attachment["id"])),
                        }
                    )
                parsed_card = card_from_json(row["app_card_json"]) if row["app_card_json"] else None
                item.update(
                    reply_preview=None,
                    app_card=asdict(parsed_card) if parsed_card is not None else None,
                    transcript=None,
                    attachments=attachments,
                )
                items.append(item)
        return {"items": items, "next_cursor": None}

    @app.post("/api/v1/exports", status_code=202)
    def create(body: ExportRequest, ctx: ArchiveWorkspace = Depends(context)) -> dict[str, str]:
        job_id, export_id = ctx.create(body.scope.to_domain(), ExportFormat(body.format))
        return {"job_id": job_id, "export_id": export_id}

    @app.get("/api/v1/exports")
    def history(account_id: str, ctx: ArchiveWorkspace = Depends(context)) -> dict[str, object]:
        ctx.require_account(account_id)
        service = ctx.service()
        records = service.list_history(account_id, repository=ctx.history)
        return {
            "items": [
                {**export_view(record), "bundle_available": service.bundle_available(record), "deletion_pending": service.deletion_pending(record.id)}
                for record in records
            ]
        }

    def record(ctx: ArchiveWorkspace, export_id: str, account_id: str) -> dict[str, object]:
        ctx.require_account(account_id)
        service = ctx.service()
        for item in service.list_history(account_id, repository=ctx.history):
            if item.id == export_id:
                return {**export_view(item), "bundle_available": service.bundle_available(item), "deletion_pending": service.deletion_pending(item.id)}
        raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)

    @app.get("/api/v1/exports/{export_id}")
    def get_export(
        export_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        return record(ctx, export_id, account_id)

    @app.delete("/api/v1/exports/{export_id}")
    def delete_export_record(
        export_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, bool]:
        ctx.require_account(account_id)
        return purge_export(ctx.service(export_id), ctx.history, ctx.jobs, export_id, account_id, transient_media_root=ctx.root / ".media")

    def ready(ctx: ArchiveWorkspace, export_id: str, account_id: str) -> Path:
        item = record(ctx, export_id, account_id)
        if item["status"] != "ready" or not item["bundle_available"]:
            raise ArchiveError(ErrorCode.SOURCE_NOT_FOUND)
        return ctx.service().final_dir(export_id)

    @app.get("/api/v1/exports/{export_id}/download")
    def download(
        export_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> FileResponse:
        directory = ready(ctx, export_id, account_id)
        return FileResponse(
            directory / "archive.zip",
            media_type="application/zip",
            filename="聊天记录.zip",
            headers={"X-Content-Type-Options": "nosniff", "Cache-Control": "no-store"},
        )

    @app.get("/api/v1/exports/{export_id}/report")
    def report(
        export_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, int]:
        path = ready(ctx, export_id, account_id) / "report.json"
        value = json.loads(path.read_text(encoding="utf-8"))
        return {
            key: int(value[key]) for key in ("message_count", "included", "missing", "excluded")
        }

    @app.get("/api/v1/jobs")
    def jobs(account_id: str, ctx: ArchiveWorkspace = Depends(context)) -> dict[str, object]:
        ctx.require_account(account_id)
        return {"items": [job_view(job) for job in ctx.jobs.list_jobs(account_id)]}

    @app.get("/api/v1/jobs/{job_id}")
    def job(
        job_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        return job_view(ctx.job(job_id, account_id))

    def control(
        job_id: str, action: str, account_id: str, ctx: ArchiveWorkspace
    ) -> dict[str, object]:
        current = ctx.job(job_id, account_id)
        try:
            if action == "pause":
                updated = ctx.jobs.request_pause(job_id)
            elif action == "resume":
                updated = ctx.jobs.resume_job(job_id)
                ctx.submit(job_id)
            elif action == "cancel":
                updated = ctx.jobs.request_cancel(job_id)
                if updated.status is JobStatus.CANCELLED:
                    export_id = str((current.task_spec or {}).get("export_id", ""))
                    ctx.discard(export_id)
                    ctx.history.finish_export(export_id, status="cancelled")
            else:
                raise ArchiveError(ErrorCode.QUERY_INVALID)
        except JobStateError:
            raise ArchiveError(ErrorCode.QUERY_INVALID) from None
        return job_view(updated)

    @app.post("/api/v1/jobs/{job_id}/pause")
    def pause(
        job_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        return control(job_id, "pause", account_id, ctx)

    @app.post("/api/v1/jobs/{job_id}/resume")
    def resume(
        job_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        return control(job_id, "resume", account_id, ctx)

    @app.post("/api/v1/jobs/{job_id}/cancel")
    def cancel(
        job_id: str, account_id: str, ctx: ArchiveWorkspace = Depends(context)
    ) -> dict[str, object]:
        return control(job_id, "cancel", account_id, ctx)

    @app.get("/api/v1/jobs/{job_id}/events")
    def events(
        request: Request,
        job_id: str,
        account_id: str,
        wait_ms: int = Query(default=25000, ge=0, le=25000),
        ctx: ArchiveWorkspace = Depends(context),
    ) -> StreamingResponse:
        ctx.job(job_id, account_id)
        try:
            after = int(request.headers.get("Last-Event-ID", "0"))
        except ValueError:
            raise ArchiveError(ErrorCode.QUERY_INVALID) from None
        if after < 0:
            raise ArchiveError(ErrorCode.QUERY_INVALID)

        def stream() -> Iterator[str]:
            available = ctx.jobs.wait_events(
                account_id,
                after_sequence=after,
                timeout_seconds=wait_ms / 1000,
                limit=500,
                job_id=job_id,
            )
            # 长轮询等待期间可能已被锁定（PSU-2.213 §4.2）：输出事件前再复核一次。
            identity = gate.session(request)
            if identity is None or not gate.is_unlocked(request, identity):
                return
            for event in available:
                yield event.to_sse()

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

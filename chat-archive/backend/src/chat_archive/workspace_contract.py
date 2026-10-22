"""Shared export requests and workspace response serialization."""

from __future__ import annotations
import json
import time
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from chat_archive.export import ExportScope
from chat_archive.ports.export import ExportArtifactRecord
from chat_archive.ports.jobs import JobRecord


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ExportScopeRequest(_StrictModel):
    account_id: str = Field(min_length=1, max_length=128)
    conversation_ids: tuple[str, ...] = ()
    date_from: int | None = None
    date_to: int | None = None
    message_types: tuple[str, ...] = ()
    attachment_kinds: tuple[Literal["image", "emoji", "audio", "video", "file"], ...] = (
        "image",
        "emoji",
        "audio",
        "file",
    )
    title: str = Field(default="聊天记录", min_length=1, max_length=80)

    def to_domain(self) -> ExportScope:
        return ExportScope(
            account_id=self.account_id,
            conversation_ids=self.conversation_ids,
            date_from=self.date_from,
            date_to=self.date_to,
            message_types=self.message_types,
            attachment_kinds=self.attachment_kinds,
            title=self.title,
        )


class ExportRequest(_StrictModel):
    scope: ExportScopeRequest
    format: Literal["jsonl", "json", "csv", "html"]


def job_view(job: JobRecord) -> dict[str, object]:
    # 逆序导入(PSU-2.22)下,"已导入至哪个时间点"比行数对用户更有意义。
    # 断点水位存放在 checkpoint 里(随恢复语义一起走),这里只做只读透出;
    # 非导入类 job 或尚未提交任何批时诚实地给 null,不伪造时间维度。
    checkpoint = job.checkpoint or {}
    # 谁发起的（PSU-2.133）。自动同步每轮留一对作业记录，界面上要分得出哪条
    # 是自己点的。这个单元之前建的行没有这个键——它们全都是人点出来的。
    task_spec = job.task_spec or {}
    stored_trigger = task_spec.get("trigger")
    # 元组而不是集合：`JobTaskSpecValue` 容得下 `list[str]`，而 `list in set`
    # 要先哈希它——一份被手改过的 task_spec 会让这一行抛 TypeError。
    trigger = stored_trigger if stored_trigger in ("manual", "auto") else "manual"
    oldest = checkpoint.get("oldest_imported_at")
    # 空间不足停下时才有；别的停法一律 null——「跟空间无关」不是「空间是 0」。
    available = checkpoint.get("remote_available_bytes")
    owed = checkpoint.get("pending_bytes")
    view: dict[str, object] = {
        "id": job.id,
        "source_account_id": job.source_account_id,
        "kind": job.kind,
        "trigger": trigger,
        "status": job.status.value,
        "stage": job.stage,
        "progress_current": job.progress_current,
        "progress_total": job.progress_total,
        "stage_progress_current": job.stage_progress_current,
        "stage_progress_total": job.stage_progress_total,
        "stage_progress_unit": job.stage_progress_unit,
        "stage_started_at": job.stage_started_at,
        "oldest_imported_at": oldest if isinstance(oldest, int) else None,
        "remote_available_bytes": available if isinstance(available, int) else None,
        "pending_bytes": owed if isinstance(owed, int) else None,
        "recoverable": job.recoverable,
        "error_code": job.error_code,
        "created_at": job.created_at,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "updated_at": job.updated_at,
    }

    deferred = checkpoint.get("deferred_avatar_count")
    originals_included = task_spec.get("include_originals")
    if job.kind == "sync" and type(originals_included) is bool:
        view["originals_included"] = originals_included
    if type(deferred) is int and deferred >= 0:
        view["deferred_avatar_count"] = deferred
    activity = checkpoint.get("sync_activity")
    if job.status.value in ("running", "pausing") and activity in ("uploading", "checking", "retrying"):
        view["sync_activity"] = activity
        last_progress = checkpoint.get("sync_last_progress_at")
        view["sync_last_progress_at"] = last_progress if type(last_progress) is int else None
    phase = checkpoint.get("verification_phase")
    if job.stage == "attachment_refs" and phase in ("preparing", "publishing", "readback", "completed"):
        view["verification_phase"] = phase
        view["verification_observed_at"] = int(time.time() * 1000)
        last = checkpoint.get("verification_last_progress_at")
        view["verification_last_progress_at"] = last if type(last) is int else None
        prepared = checkpoint.get("verification_prepared_count")
        if type(prepared) is int and prepared >= 0:
            view["verification_prepared_count"] = prepared
    return view


def export_view(record: ExportArtifactRecord) -> dict[str, object]:
    try:
        scope: object = json.loads(record.scope_json)
    except json.JSONDecodeError:
        scope = {}
    return {
        "id": record.id,
        "source_account_id": record.source_account_id,
        "format": record.format,
        "scope": scope,
        "status": record.status,
        "record_count": record.record_count,
        "size_bytes": record.size_bytes,
        "error_code": record.error_code,
        "created_at": record.created_at,
        "finished_at": record.finished_at,
    }

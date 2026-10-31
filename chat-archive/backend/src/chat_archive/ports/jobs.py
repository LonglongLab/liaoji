"""Stable job lifecycle, control-point, and event-feed contract."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Literal, Protocol, runtime_checkable

from chat_archive.domain.models import JobStatus


CheckpointValue = str | int | None
Checkpoint = Mapping[str, CheckpointValue]
JobTaskSpecValue = str | int | bool | None | list[str]
JobTaskSpec = Mapping[str, JobTaskSpecValue]
ControlAction = Literal["continue", "pause", "cancel"]
# 谁发起的这条作业（PSU-2.133）。不加它，作业历史每 30 分钟一条自动记录，
# 界面上分不清哪条是自己点的。旧行没有这个键，一律读成 `manual`。
JobTrigger = Literal["manual", "auto"]
ProgressUnit = Literal["bytes", "items"]


class JobValidationError(ValueError):
    """A job command or checkpoint violates the stable contract."""


class JobStateError(RuntimeError):
    """A job transition is not allowed from its committed state."""


class JobNotFoundError(LookupError):
    """The requested opaque job ID is unknown."""


class JobRepositoryClosedError(RuntimeError):
    """The job repository no longer accepts operations."""


@dataclass(frozen=True)
class ExportJobsRemoved:
    job_ids: tuple[str, ...]
    legacy_job_history_retained: bool


@dataclass(frozen=True)
class JobRecord:
    id: str
    source_account_id: str
    kind: str
    status: JobStatus
    stage: str | None
    progress_current: int
    progress_total: int | None
    stage_progress_current: int
    stage_progress_total: int | None
    stage_progress_unit: ProgressUnit | None
    stage_started_at: int | None
    checkpoint: dict[str, CheckpointValue] | None = field(repr=False)
    task_spec: dict[str, JobTaskSpecValue] | None = field(default=None, repr=False)
    recoverable: bool = True
    pause_requested: bool = False
    cancel_requested: bool = False
    error_code: str | None = None
    details_ref: str | None = None
    created_at: int = 0
    started_at: int | None = None
    finished_at: int | None = None
    updated_at: int = 0


@dataclass(frozen=True)
class ControlPointResult:
    job: JobRecord
    action: ControlAction


@dataclass(frozen=True)
class JobStorageSettings:
    journal_mode: str
    busy_timeout_ms: int


@dataclass(frozen=True)
class JobEvent:
    sequence: int
    job_id: str
    source_account_id: str
    event_type: str
    status: JobStatus
    stage: str | None
    progress_current: int
    progress_total: int | None
    stage_progress_current: int
    stage_progress_total: int | None
    stage_progress_unit: ProgressUnit | None
    stage_started_at: int | None
    error_code: str | None
    created_at: int

    def to_sse(self) -> str:
        payload = json.dumps(
            {
                "job_id": self.job_id,
                "event_type": self.event_type,
                "status": self.status.value,
                "stage": self.stage,
                "progress_current": self.progress_current,
                "progress_total": self.progress_total,
                "stage_progress_current": self.stage_progress_current,
                "stage_progress_total": self.stage_progress_total,
                "stage_progress_unit": self.stage_progress_unit,
                "stage_started_at": self.stage_started_at,
                "error_code": self.error_code,
                "created_at": self.created_at,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return f"id: {self.sequence}\nevent: job\ndata: {payload}\n\n"


class JobRepository(Protocol):
    def purge_export_jobs(
        self, account_id: str, export_id: str, cleanup: Callable[[], None]
    ) -> ExportJobsRemoved: ...

    def create_job(
        self,
        *,
        job_id: str,
        source_account_id: str,
        kind: str,
        progress_total: int | None = None,
        recoverable: bool = True,
        task_spec: JobTaskSpec | None = None,
    ) -> JobRecord: ...

    def get_job(self, job_id: str) -> JobRecord: ...

    def list_jobs(self, source_account_id: str) -> tuple[JobRecord, ...]: ...

    def active_job_of_kind(self, source_account_id: str, kind: str) -> JobRecord | None:
        """这个账号这种作业里正占着一条工作线程的那一个，没有就是 None。

        路由拿它当「同一账号同时只许一个」的闸门（PSU-2.106）。答「哪一个」而不是
        「有没有」，理由与 `cache_conflicting_import_job` 一样：被挡住的人下一步要
        去看它，而一个布尔值指不出落点。

        判据是 `queued` / `running` / `pausing`——与 `has_running_jobs` 同一套，
        不是 `cache_conflicting_import_job` 那套。`paused` 与 `interrupted` 不占
        线程、不造成重复劳动，把它们算进来只会让一个几天前暂停的作业永久堵住那个
        按钮。
        """
        ...

    def cache_conflicting_import_job(self) -> JobRecord | None:
        """挡着缓存维护（回收、清临时快照）的那个导入作业，没有就是 None。

        答的是「哪一个」而不是「有没有」：被挡住的人下一步要去处理它，而一个布尔
        值指不出任何落点。PSU-2.88 验收时挡路的作业已经暂停了三个星期，界面却说
        「正在导入」，找到它靠的是登机器读 `jobs.sqlite`。

        多个作业同时挡着时答**最早建的**那条，答案必须稳定——界面上那句话会一直
        显示，每次问答一个不同的作业等于什么也没说。
        """
        ...

    def has_active_reclaim_jobs(self) -> bool:
        """有没有一个回收作业还在排队或在跑。回收独占写锁、跑分钟级，
        同时只该有一个（PSU-2.88）。"""
        ...

    def start_job(self, job_id: str) -> JobRecord: ...

    def request_pause(self, job_id: str) -> JobRecord: ...

    def request_cancel(self, job_id: str) -> JobRecord: ...

    def report_stage_progress(
        self,
        job_id: str,
        *,
        stage: str,
        progress_current: int,
        progress_total: int | None,
        progress_unit: ProgressUnit | None,
    ) -> JobRecord: ...

    def heartbeat(self, job_id: str, *, stage: str) -> JobRecord: ...

    def checkpoint(
        self,
        job_id: str,
        *,
        stage: str,
        progress_current: int,
        checkpoint: Checkpoint,
        stage_progress_current: int | None = None,
        stage_progress_total: int | None = None,
        stage_progress_unit: ProgressUnit | None = None,
    ) -> ControlPointResult: ...

    def resume_job(self, job_id: str) -> JobRecord: ...

    def complete_job(self, job_id: str) -> JobRecord: ...

    def interrupt_job(self, job_id: str, *, error_code: str | None = None) -> JobRecord:
        """停在半路，可以再来——不是失败，也不是跑完（PSU-2.98）。

        `interrupted` 这个状态本来只由 `recover_after_restart` 产生（进程被杀
        之后那些还挂着 running 的行）。同步作业需要同一句话：远端明确说了它此刻
        不行，已经送上去的就在那儿，下一次接着送。说成 `completed` 是撒谎，
        说成 `failed` 会让界面把一次可重试的抖动画成红色的终局。

        `error_code` 是**这一轮被按停的原因**（PSU-2.113），落在作业行自己的
        那一列上。它此前只写进 checkpoint，而界面读的是 `job.error_code`，
        两者之间一条路都没有——于是一次「远端放不下」在界面上是一张什么都
        不说的卡片。没有这句话的停法（暂停、取消、重启恢复）照旧传 None：
        「跑着被打断」本来就没有代号。
        """
        ...

    def fail_job(
        self,
        job_id: str,
        *,
        error_code: str,
        details_ref: str | None = None,
    ) -> JobRecord: ...

    def list_events(
        self,
        source_account_id: str,
        *,
        after_sequence: int,
        limit: int,
        job_id: str | None = None,
    ) -> tuple[JobEvent, ...]: ...

    def wait_events(
        self,
        source_account_id: str,
        *,
        after_sequence: int,
        timeout_seconds: float,
        limit: int,
        job_id: str | None = None,
    ) -> tuple[JobEvent, ...]: ...

    def close(self) -> None: ...


@runtime_checkable
class ActiveImportJobProbe(Protocol):
    """Optional source budget; recent-import cutoff is Unix milliseconds.

    Paused jobs do not hold a worker.
    """

    def active_import_job(self) -> JobRecord | None: ...

    def recent_import_for_scope(self, account_id: str, conversation_id: str | None, finished_after: int) -> JobRecord | None: ...

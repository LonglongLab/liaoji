"""Shared low-frequency liveness reporting for long local jobs."""
from __future__ import annotations

import threading
from chat_archive.domain.models import JobStatus
from chat_archive.ports.jobs import JobRepository, JobStateError

class JobHeartbeat:
    """Persist a low-frequency liveness signal while a long parser call blocks."""

    def __init__(
        self,
        jobs: JobRepository,
        job_id: str,
        *,
        interval_seconds: float,
    ) -> None:
        if interval_seconds <= 0:
            raise ValueError("heartbeat interval must be positive")
        self._jobs = jobs
        self._job_id = job_id
        self._interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run,
            name=f"chat-archive-heartbeat-{job_id}",
            daemon=True,
        )

    def __enter__(self) -> JobHeartbeat:
        self._thread.start()
        return self

    def __exit__(self, *_error: object) -> None:
        self._stop.set()
        self._thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                current = self._jobs.get_job(self._job_id)
                if current.status not in {JobStatus.RUNNING, JobStatus.PAUSING}:
                    return
                if current.stage is None:
                    continue
                self._jobs.heartbeat(self._job_id, stage=current.stage)
            except JobStateError:
                # The stage may have advanced between get_job and heartbeat.
                continue
            except Exception:
                # Liveness reporting is best-effort and must never fail a job.
                return

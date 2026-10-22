import { ref } from "vue";

import type { ArchiveApi, Job } from "@/api/archiveApi";

const stopStatuses: ReadonlySet<Job["status"]> = new Set([
  "completed",
  "failed",
  "cancelled",
  "paused",
]);

export const useJobFollower = (api: ArchiveApi) => {
  const job = ref<Job | null>(null);
  let lastSequence = 0;
  let controller: AbortController | null = null;
  let generation = 0;

  const read = async (jobId: string, accountId: string, expected: number): Promise<void> => {
    const result = await api.getJob(jobId, accountId);
    if (expected === generation) job.value = result;
  };
  const load = (jobId: string, accountId: string): Promise<void> => read(jobId, accountId, generation);

  const follow = async (jobId: string, accountId: string): Promise<void> => {
    controller?.abort();
    const expected = ++generation;
    controller = new AbortController();
    const signal = controller.signal;
    const isActive = (): boolean => !signal.aborted && expected === generation;
    if (job.value?.id !== jobId) {
      lastSequence = 0;
      await read(jobId, accountId, expected);
    }
    try {
      while (job.value !== null && !stopStatuses.has(job.value.status) && isActive()) {
        lastSequence = await api.readJobEvents(
          jobId,
          accountId,
          (event) => {
            if (!isActive() || job.value?.id !== event.payload.job_id) return;
            job.value = {
              ...job.value,
              status: event.payload.status,
              stage: event.payload.stage,
              progress_current: event.payload.progress_current,
              progress_total: event.payload.progress_total,
              stage_progress_current: event.payload.stage_progress_current,
              stage_progress_total: event.payload.stage_progress_total,
              stage_progress_unit: event.payload.stage_progress_unit,
              stage_started_at: event.payload.stage_started_at,
              error_code: event.payload.error_code,
              updated_at: event.payload.created_at,
            };
          },
          { lastSequence, waitMs: 25_000, signal },
        );
        if (isActive()) await read(jobId, accountId, expected);
      }
    } catch {
      if (isActive()) {
        await read(jobId, accountId, expected).catch(() => undefined);
      }
    }
  };

  const stop = (): void => {
    generation++;
    controller?.abort();
  };

  const pause = async (accountId: string): Promise<void> => {
    const expected = generation;
    if (job.value !== null) {
      const result = await api.pauseJob(job.value.id, accountId);
      if (expected === generation) job.value = result;
    }
  };
  const resume = async (accountId: string): Promise<void> => {
    const expected = generation;
    if (job.value !== null) {
      const result = await api.resumeJob(job.value.id, accountId);
      if (expected === generation) job.value = result;
    }
  };
  const cancel = async (accountId: string): Promise<void> => {
    const expected = generation;
    if (job.value !== null) {
      const result = await api.cancelJob(job.value.id, accountId);
      if (expected === generation) job.value = result;
    }
  };

  return { job, load, follow, stop, pause, resume, cancel };
};

import { computed, onBeforeUnmount, ref, watch, type Ref } from "vue";
import type { Job } from "@/api/archiveApi";
import { verificationProgress } from "@/lib/verificationProgress";

/** Anchor elapsed time to the server timestamp when a response arrives. */
export const useVerificationProgress = (job: Ref<Job | null>) => {
  const receivedAt = ref(Date.now());
  const now = ref(Date.now());
  watch(() => `${job.value?.id ?? ""}:${String(job.value?.verification_observed_at ?? job.value?.updated_at ?? 0)}`, () => {
    receivedAt.value = Date.now();
    now.value = receivedAt.value;
  });
  let timer: ReturnType<typeof globalThis.setInterval> | undefined;
  watch(() => `${job.value?.stage ?? ""}:${job.value?.status ?? ""}`, () => {
    if (timer !== undefined) globalThis.clearInterval(timer);
    const active = job.value?.status === "running" || job.value?.status === "pausing";
    timer = active && job.value?.stage === "attachment_refs" ? globalThis.setInterval(() => { now.value = Date.now(); }, 1000) : undefined;
  }, { immediate: true });
  onBeforeUnmount(() => { if (timer !== undefined) globalThis.clearInterval(timer); });
  return computed(() => verificationProgress(job.value,
    (job.value?.verification_observed_at ?? job.value?.updated_at ?? 0) + (now.value - receivedAt.value)));
};

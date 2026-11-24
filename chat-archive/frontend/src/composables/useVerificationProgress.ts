import { computed, ref, watch, type Ref } from "vue";
import type { Job } from "@/api/archiveApi";
import { useClockNow } from "@/lib/clock";
import { verificationProgress } from "@/lib/verificationProgress";

/** Anchor elapsed time to the server timestamp when a response arrives. */
export const useVerificationProgress = (job: Ref<Job | null>) => {
  const receivedAt = ref(Date.now());
  watch(() => `${job.value?.id ?? ""}:${String(job.value?.verification_observed_at ?? job.value?.updated_at ?? 0)}`, () => {
    receivedAt.value = Date.now();
  });
  const now = useClockNow(
    1000,
    () =>
      (job.value?.status === "running" || job.value?.status === "pausing") &&
      job.value.stage === "attachment_refs",
  );
  return computed(() => verificationProgress(job.value,
    (job.value?.verification_observed_at ?? job.value?.updated_at ?? 0) + (now.value - receivedAt.value)));
};

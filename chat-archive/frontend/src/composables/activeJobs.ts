/**
 * 「这个账号现在有什么在跑」——一份，四处用（PSU-2.107）。
 *
 * PSU-2.106 在总览、同步健康、导出三页各写了一份一模一样的 `jobsQuery`；
 * 全局横幅是第四处，再抄一遍就该出事了。四个观察者共用同一个 queryKey，
 * vue-query 自己合并请求，所以这不是四倍的轮询。
 *
 * 判据（`holdsAWorker`）与后端闸门 `active_job_of_kind` 逐字一致：两边不一样时，
 * 按钮亮着而请求被 409 挡回来，用户看到的是「点了没反应」。
 */
import { useQuery } from "@tanstack/vue-query";
import { computed, type ComputedRef, type Ref } from "vue";

import type { ArchiveApi, Job } from "@/api/archiveApi";
import { holdsAWorker } from "@/lib/jobStatus";

/** 有活跃作业时才轮询。一个不再变的列表不值得每两秒问一次。 */
const POLL_MS = 2000;

export interface ActiveJobs {
  jobs: ComputedRef<Job[]>;
  runningImport: ComputedRef<Job | null>;
  runningSync: ComputedRef<Job | null>;
  runningExport: ComputedRef<Job | null>;
  refetch: () => Promise<unknown>;
}

export const useActiveJobs = (
  api: ArchiveApi,
  accountId: Ref<string | null> | ComputedRef<string | null>,
  workspaceIdentity?: Ref<string>,
): ActiveJobs => {
  const query = useQuery({
    queryKey: computed(() => workspaceIdentity === undefined || workspaceIdentity.value.startsWith("local:") ? ["jobs", accountId.value] as const
        : ["jobs", workspaceIdentity.value, accountId.value] as const),
    enabled: computed(() => accountId.value !== null),
    refetchInterval: (q) =>
      (q.state.data ?? []).some((job) => holdsAWorker.has(job.status)) ? POLL_MS : false,
    queryFn: (): Promise<Job[]> => {
      const account = accountId.value;
      if (account === null) return Promise.resolve([]);
      return api.listJobs(account);
    },
  });

  const jobs = computed(() => query.data.value ?? []);
  const ofKind = (kind: Job["kind"]): ComputedRef<Job | null> =>
    computed(() => jobs.value.find((job) => job.kind === kind && holdsAWorker.has(job.status)) ?? null);

  return {
    jobs,
    runningImport: ofKind("import"),
    runningSync: ofKind("sync"),
    runningExport: ofKind("export"),
    refetch: () => query.refetch(),
  };
};

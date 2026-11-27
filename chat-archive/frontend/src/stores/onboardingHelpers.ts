import {
  type AccountSummary,
  ArchiveApiError,
  type Job,
} from "@/api/archiveApi";
import type { AccountIdentity } from "@/lib/defaultAccount";

export type OnboardingStep =
  | "loading"
  | "welcome"
  | "detecting"
  | "source"
  | "key"
  | "import"
  | "progress"
  | "overview"
  | "error";

export interface FlowError {
  code: string;
  message: string;
  retryable: boolean;
  recommendedAction: ArchiveApiError["recommendedAction"];
}

export const terminalStatuses: ReadonlySet<Job["status"]> = new Set([
  "completed",
  "failed",
  "cancelled",
]);

// 进度面板对这两种没有任何出口：没得暂停、没得重试，只剩一张「已完成」。
export const nothingLeftToWatch: ReadonlySet<Job["status"]> = new Set(["completed", "cancelled"]);

export const monitoringStops: ReadonlySet<Job["status"]> = new Set([
  ...terminalStatuses,
  "paused",
  "interrupted",
]);

// 「上次看过的」「钉住的默认」两把键都住在 `lib/defaultAccount`：远程端要的是
// 同一套语义，各写一份就是两处要记得一起改的地方（PSU-2.119）。
/** 本机档案没有归并维度：一个账号就是一个数据目录，不存在「被并掉的旧 id」。 */
export const toIdentity = (account: AccountSummary): AccountIdentity => ({
  id: account.id,
  mergedIds: [],
});

// A watched import keeps running inside the local service even when the
// browser briefly cannot reach it, so a failed poll waits and tries again
// instead of ending the watch. Back off so a service that is really gone is
// not hammered, and cap the wait so recovery is still noticed promptly.
export const followRetryDelayMs = (consecutiveFailures: number): number =>
  Math.min(30_000, 1_000 * 2 ** (consecutiveFailures - 1));

export const delay = (milliseconds: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve) => {
    const timer = globalThis.setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });

/**
 * 引导流程只认导入作业（PSU-2.107）。
 *
 * `jobs[0]` 取的是**任意种类**里最新的那一个，于是一个正在跑的同步作业会被当成
 * 导入：`step` 变成 `"progress"`，刷新页面就被丢进「建立档案 / 快照与解密」，
 * 而那一屏显示的其实是同步的计数。真机上这一步只要有同步在跑、刷新一下就能复现。
 *
 * 同步该由它自己的横幅与同步健康页负责——那是 `SyncProgressBanner` 的事。
 */
export const newestImport = (jobs: readonly Job[]): Job | null =>
  jobs.find((job) => job.kind === "import") ?? null;

// PSU-2.22:decrypting 结束、第一批(最新消息)提交后,引导页就不该再挡着
// 用户——progress_current > 0 是"档案里已有本轮可浏览消息"的诚实信号。
// decrypting 期间(progress 仍为 0)放行是假可用,继续留在引导页。
export const importReadyToBrowse = (job: Job): boolean =>
  job.kind === "import" && job.status === "running" && job.progress_current > 0;

export const flowErrorFrom = (error: unknown): FlowError => {
  if (error instanceof ArchiveApiError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      recommendedAction: error.recommendedAction,
    };
  }
  return {
    code: "SERVICE_UNAVAILABLE",
    message: "本地归档服务暂时没有响应，请确认服务正在运行后重试。",
    retryable: true,
    recommendedAction: "retry",
  };
};

import { useQuery } from "@tanstack/vue-query";
import { computed, type ComputedRef } from "vue";

import type { ArchiveApi, AutoSyncStatus } from "@/api/archiveApi";
import { explainSyncFailure } from "@/utils/syncFailure";

/**
 * 自动同步的当前状态，三处共用（PSU-2.133）。
 *
 * 设置页那张卡自己查（它还要写），这里是**只读的那两处**：总览页副标题下面
 * 那一行、同步健康页的方向说明句。共用一个 `queryKey` 意味着三处看到的是同一
 * 份答案——否则「设置页说在跑、总览页说等待中」这种事只是时间问题。
 *
 * 开着才轮询：关着的时候这一行压根不出现，每 30 秒问一次没有意义。
 */
const REFRESH_MS = 30_000;

export const useAutoSyncStatus = (
  api: ArchiveApi,
): ComputedRef<AutoSyncStatus | null> => {
  const query = useQuery({
    queryKey: ["auto-sync"] as const,
    queryFn: (): Promise<AutoSyncStatus> => api.getAutoSync(),
    refetchInterval: (current) =>
      current.state.data?.enabled === true ? REFRESH_MS : false,
    // 这一行不是这两页的主角：远端端跑的界面上压根没有这条路由，如实收下
    // 那个 404 并当作「没有自动同步」，别在控制台刷红。
    retry: false,
  });
  return computed(() => query.data.value ?? null);
};

/** 卡片之外那两处只说一句话：现在是什么状态。不重复设置项。 */
export const autoSyncHeadline = (status: AutoSyncStatus | null): string => {
  if (status === null || !status.enabled) return "";
  if (status.state === "paused") {
    const code = status.paused_error_code;
    if (code === "KEY_METHOD_UNAVAILABLE") return "密钥失效，自动同步已暂停";
    if (code === "REMOTE_AUTH_REQUIRED") return "远程登录失效，自动同步已暂停";
    if (code === "REMOTE_STORAGE_INSUFFICIENT") return "远端真的满了，自动同步已暂停";
    if (code === "REMOTE_SYNC_CONFLICT") return "上传位置对不上，自动同步已暂停";
    if (code === "CONSECUTIVE_FAILURES") return "连续 3 轮失败，自动同步已暂停";
    return `${explainSyncFailure(code).summary}，自动同步已暂停`;
  }
  if (status.state === "running") {
    const step =
      status.current_cycle?.sync_job_id != null ? "推送远端" : "更新本机档案";
    return `自动同步正在运行 · ${step}`;
  }
  const next = status.next_run_at;
  if (status.last_cycle?.outcome === "failed") {
    const cause = explainSyncFailure(status.last_cycle.error_code).summary;
    return next === null ? `${cause} · 等待重试` : `${cause} · 下一次 ${timeOfDay(next)} 重试`;
  }
  return next === null ? "自动同步已开启" : `自动同步已开启 · 下一次 ${timeOfDay(next)}`;
};

const timeOfDay = (unixSeconds: number): string => {
  const date = new Date(unixSeconds * 1000);
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

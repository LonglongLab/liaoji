import type { Job } from "@/api/archiveApi";

type Input = Pick<Job, "stage" | "status" | "stage_progress_current" | "stage_progress_total"
  | "stage_started_at" | "updated_at" | "verification_phase" | "verification_last_progress_at" | "verification_prepared_count"> & Partial<Pick<Job, "finished_at">>;

const duration = (ms: number): string => {
  const seconds = Math.max(0, Math.floor(ms / 1000));
  return seconds < 60 ? `${String(seconds)} 秒` : `${String(Math.floor(seconds / 60))} 分 ${String(seconds % 60)} 秒`;
};
const count = (value: number): string => value.toLocaleString("zh-CN");

export const verificationProgress = (job: Input | null, now: number) => {
  if (job?.stage !== "attachment_refs") return null;
  const known = job.verification_phase !== undefined;
  const phase = job.verification_phase;
  const prepared = job.verification_prepared_count ?? 0;
  const done = job.stage_progress_current;
  const total = job.stage_progress_total;
  const lastAt = job.verification_last_progress_at ?? job.stage_started_at ?? job.updated_at;
  const active = job.status === "running" || job.status === "pausing";
  const clock = active ? now : job.finished_at ?? job.updated_at;
  const waiting = active && phase !== "preparing" && known && now - job.updated_at >= 60_000;
  const title = ["interrupted", "failed"].includes(job.status) ? "检查未完成"
    : job.status === "completed" ? "本轮同步已结束"
      : job.status === "cancelled" ? "检查已取消"
        : job.status === "paused" ? "检查已暂停"
          : job.status === "pausing" ? "正在暂停检查"
            : waiting ? "正在等待服务器响应" : "正在检查图片和文件";
  return {
    title,
    line: job.status === "completed" && known ? done > 0
      ? `本轮已检查 ${count(done)} 条消息` : "本轮没有需要检查的新内容"
      : !known || phase === "preparing" ? "确认聊天中的图片和文件能正常打开"
      : `已检查 ${count(done)} 条消息${total === null ? "" : `，还剩 ${count(Math.max(0, total - done))} 条`}`,
    prepared: `已整理 ${count(prepared)} 条历史记录`,
    step: !active ? job.status === "completed" ? "检查已结束" : "检查已停止"
      : waiting ? "当前请求尚未返回" : phase === "preparing" ? "正在读取待核验队列"
      : phase === "publishing" ? "正在提交附件关联" : "正在核对远端保存结果",
    elapsed: job.stage_started_at === null ? null : `${job.status === "completed" ? "检查耗时" : "检查已用"} ${duration(clock - job.stage_started_at)}`,
    last: !active ? done === 0 && prepared === 0 ? "本轮未完成消息核验" : `最后推进距停止 ${duration(clock - lastAt)}`
      : done === 0 && prepared === 0 ? "尚未完成首条核验" : `最近推进：${duration(clock - lastAt)}前`,
    percent: known && phase !== "preparing" && total !== null && total > 0
      ? Math.min(100, Math.max(0, Math.floor(done * 100 / total))) : null,
    waiting,
  };
};

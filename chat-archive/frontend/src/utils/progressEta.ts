/**
 * 长任务的「还剩多少、大概还要多久」（PSU-2.107）。
 *
 * 两条口径，都是为了不编数字：
 *
 * 1. **按当前这一段算，不按整个作业算。** 同步的四段单件成本差一到两个数量级
 *    （原图实测每件约 1.7 秒，正文是一批一批的字节），把它们摊成一个总 ETA
 *    等于凭空造一个数。
 * 2. **时间只用服务端时钟。** `updated_at - stage_started_at` 两端都来自作业行；
 *    掺进 `Date.now()` 就把本机时钟的偏差算进了速率。作业每一轮都会 checkpoint，
 *    `updated_at` 本来就跟着走。
 *
 *    **单位是毫秒。** `Job` 的每个时间戳都来自后端的 `_now_ms()`（真机上的
 *    `updated_at` 是 13 位），所以算速率之前必须先除以 1000。按秒读会把 ETA
 *    放大一千倍——真值四分钟的那一段会显示「超过 1 天」，而 20 秒那道噪声闸
 *    变成 20 毫秒，等于没有（PSU-2.108 修）。
 */
import type { Job } from "@/api/archiveApi";

/**
 * 算进度要的全部字段，**不收整个 `Job`**。
 *
 * 契约每加一个字段，收 `Job` 的用例夹具就要跟着补一次——而这几个函数根本不看
 * 那些字段。结构化地只要这几个，`Job` 照样传得进来。
 */
export interface StageProgressInput {
  status: Job["status"];
  stage_progress_current: number;
  stage_progress_total: number | null;
  /** epoch **毫秒**——与 `Job` 的其它时间戳同一套，别按秒读。 */
  stage_started_at: number | null;
  /** 同上，毫秒。 */
  updated_at: number;
}

export interface JobProgressInput extends StageProgressInput {
  progress_current: number;
  progress_total: number | null;
  stage_progress_unit: "items" | "bytes" | null;
}

export interface JobProgress {
  done: number;
  total: number;
  /** 0–100 的整数。 */
  percent: number;
  /**
   * 这两个数的单位。回落到段级分母时**必须跟着回落**（PSU-2.110）：导入的
   * `decrypting` 段报的是 `bytes`（真机实测 `0/110592 bytes`），把它当条数印出来
   * 就是「0 / 110,592 条」——那是 108 KiB，不是十一万条消息。
   */
  unit: "items" | "bytes" | null;
}

/**
 * 样本太小的速率是噪声，拿它乘一个几万的余量会得到一个荒唐的数。
 * 两道下限一起用：跑够久，并且真的做完了一些件数。
 */
const MIN_ELAPSED_SECONDS = 20;
const MIN_DONE_ITEMS = 20;
/** 超过这个数就只说「超过 1 天」——报 37 小时是在假装这个估计有那么准。 */
const A_DAY_SECONDS = 24 * 60 * 60;

export interface StageProgress {
  done: number;
  total: number;
  /** 0–100 的整数。 */
  percent: number;
  /** 秒；算不出就是 null，界面于是一个字都不说。 */
  etaSeconds: number | null;
}

/**
 * 当前这一段的进度。没有分母（`stage_progress_total` 为空或 ≤ 0）时返回 null：
 * 这时候界面该退回「已送出 N 项」那句话，而不是显示一个零分母的百分比。
 */
export const stageProgressOf = (job: StageProgressInput): StageProgress | null => {
  const total = job.stage_progress_total;
  if (total === null || total <= 0) return null;
  const done = Math.max(0, Math.min(job.stage_progress_current, total));
  return {
    done,
    total,
    percent: percentOf(done, total),
    etaSeconds: etaSecondsOf(job, done, total),
  };
};

const etaSecondsOf = (
  job: StageProgressInput,
  done: number,
  total: number,
): number | null => {
  if (job.status !== "running") return null;
  if (job.stage_started_at === null) return null;
  // 两个都是毫秒，先换成秒再算速率——见文件头。
  const elapsedSeconds = (job.updated_at - job.stage_started_at) / 1_000;
  if (elapsedSeconds < MIN_ELAPSED_SECONDS) return null;
  if (done < MIN_DONE_ITEMS) return null;
  const remaining = total - done;
  if (remaining <= 0) return null;
  return Math.round((remaining * elapsedSeconds) / done);
};

/**
 * 把秒说成一句人话。**向上取整到显示单位**：说「还需 1 分钟」而它其实还要 90 秒，
 * 比说「还需 2 分钟」更让人觉得被骗。
 */
export const formatEta = (seconds: number): string => {
  if (seconds >= A_DAY_SECONDS) return "超过 1 天";
  if (seconds < 60) return "不到 1 分钟";
  const minutes = Math.ceil(seconds / 60);
  if (minutes < 60) return `约 ${String(minutes)} 分钟`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  return rest === 0 ? `约 ${String(hours)} 小时` : `约 ${String(hours)} 小时 ${String(rest)} 分钟`;
};

/**
 * 整个作业走到哪儿了。**分子与分母必须来自同一层**，所以这里一起给，不只给分母。
 *
 * 导入的总数报在**段级**字段上（`import_worker` 在 messages 段传
 * `stage_progress_total`），而作业级的 `progress_total` 从来没被填过
 * （`create_job(progress_total=None)`）——横幅从前读的正是后者，所以永远没有分母。
 * 回落到段级分母之后，分子也必须跟着回落到段级：今天这两个数在 messages 段恰好
 * 相等（`import_worker` 同一个 `progress` 传给了两个字段），所以拿作业级的分子
 * 除段级的分母碰巧是对的；哪天别的段也报出分母，那就会印出「800,000 / 61 条」。
 * 配对写在这里一次，横幅就不必知道这件事（PSU-2.109）。
 */
export const jobProgressOf = (job: JobProgressInput): JobProgress | null => {
  if (job.progress_total !== null && job.progress_total > 0) {
    const done = Math.max(0, Math.min(job.progress_current, job.progress_total));
    return {
      done,
      total: job.progress_total,
      percent: percentOf(done, job.progress_total),
      // 作业级那个分母数的是消息条数（`import_worker` 的 `source_message_total`）。
      unit: "items",
    };
  }
  const here = stageProgressOf(job);
  return here === null
    ? null
    : { done: here.done, total: here.total, percent: here.percent, unit: job.stage_progress_unit };
};

const percentOf = (done: number, total: number): number =>
  Math.min(100, Math.max(0, Math.round((done / total) * 100)));

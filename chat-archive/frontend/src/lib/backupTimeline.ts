import type { BackupRun } from "@/api/archiveApi";

/**
 * 「最近的备份」那条时间轴要画什么（2026-09-26 原型 A）。
 *
 * 原来的图只把读到的几轮按先后排成一排竖条：漏跑的那天根本不出现，十根一样的
 * 绿条也读不出「上次什么时候、下次什么时候、有没有断过」。这里把记录摆回真实
 * 时间上，并且在知道周期时把**该跑而没有记录**的格子补成空位。
 *
 * 空位是推断，不是账本上的行：只在知道周期（`intervalSeconds`）时才推，
 * 浏览器那一半读不到配置，就只画读到的。
 */

export type TimelineMark =
  | { kind: "run"; at: number; run: BackupRun }
  | { kind: "missed"; at: number };

export type TimelineHeadline =
  | { tone: "run"; kind: "running" }
  | { tone: "warn"; kind: "stale"; sinceLastSuccess: number | null; dueAt: number | null }
  | { tone: "warn"; kind: "failures"; failed: number; total: number; lastFailure: BackupRun }
  | { tone: "warn"; kind: "gaps"; count: number; missed: number }
  | { tone: "ok"; kind: "streak"; count: number };

export interface BackupTimeline {
  marks: TimelineMark[];
  /** 下一轮大约什么时候。正在跑、已经漏跑或不知道周期时为 null。 */
  nextAt: number | null;
  headline: TimelineHeadline;
  lastSuccess: BackupRun | null;
  /** 窗口里最早的一次成功，用来和最近一份比大小。只有一次成功时为 null。 */
  earliestSuccess: BackupRun | null;
}

/**
 * 到点之后还要等多久才算「这一格漏了」。worker 醒来有间隔（空闲时五分钟一次）、
 * 启动还有宽限期，按点准时是做不到的；四分之一个周期足够宽，又不至于把真漏跑
 * 拖到下一格才认。
 */
const LATE_FRACTION = 0.25;

/** 一次最多补多少个空位：停了半年的备份不该画出一百八十个虚线圈。 */
const MAX_MISSED = 30;

export const buildBackupTimeline = (
  runsNewestFirst: readonly BackupRun[],
  intervalSeconds: number | null,
  nowSeconds: number,
  overdue: boolean,
): BackupTimeline => {
  const runs = [...runsNewestFirst].sort((a, b) => a.started_at - b.started_at);
  const marks: TimelineMark[] = [];
  let missed = 0;

  const addMissedBetween = (from: number, to: number): void => {
    if (intervalSeconds === null || intervalSeconds <= 0) return;
    // `from` 之后每隔一个周期该有一轮；落在 `to` 之前（且已经晚到算数）的都是空位。
    for (
      let slot = from + intervalSeconds;
      slot + intervalSeconds * LATE_FRACTION < to && missed < MAX_MISSED;
      slot += intervalSeconds
    ) {
      marks.push({ kind: "missed", at: slot });
      missed += 1;
    }
  };

  runs.forEach((run, index) => {
    const previous = runs[index - 1];
    if (previous !== undefined) addMissedBetween(previous.started_at, run.started_at);
    marks.push({ kind: "run", at: run.started_at, run });
  });

  const last = runs.at(-1);
  const running = last?.status === "running";
  const middleMissed = missed;
  let trailingMissed = 0;
  let dueAt: number | null = null;
  if (last !== undefined && !running) {
    const before = marks.length;
    addMissedBetween(last.started_at, nowSeconds);
    trailingMissed = marks.length - before;
    dueAt = marks[before]?.at ?? null;
  }

  const nextAt =
    last !== undefined && !running && trailingMissed === 0 && intervalSeconds !== null
      ? last.started_at + intervalSeconds
      : null;

  const successes = runs.filter((run) => run.status === "success");
  const lastSuccess = successes.at(-1) ?? null;
  const earliestSuccess = successes.length > 1 ? (successes[0] ?? null) : null;
  const failures = runs.filter((run) => run.status === "failed");

  let headline: TimelineHeadline;
  if (running) {
    headline = { tone: "run", kind: "running" };
  } else if (overdue || trailingMissed > 0) {
    headline = {
      tone: "warn",
      kind: "stale",
      sinceLastSuccess: lastSuccess === null ? null : nowSeconds - lastSuccess.started_at,
      dueAt,
    };
  } else if (failures.length > 0) {
    headline = {
      tone: "warn",
      kind: "failures",
      failed: failures.length,
      total: runs.length,
      lastFailure: failures.at(-1) as BackupRun,
    };
  } else if (middleMissed > 0) {
    // 每一轮都成功，但中间断过：说「连续 N 次成功」就是在掩盖那几个空位。
    headline = { tone: "warn", kind: "gaps", count: runs.length, missed: middleMissed };
  } else {
    headline = { tone: "ok", kind: "streak", count: runs.length };
  }

  return { marks, nextAt, headline, lastSuccess, earliestSuccess };
};

/** 「已经 2 天」「已经 5 小时」：只说到最大的那个单位，不要「1 天 3 小时 12 分」。 */
export const formatElapsed = (seconds: number): string => {
  const hours = Math.floor(seconds / 3600);
  if (hours >= 24) return `${String(Math.floor(hours / 24))} 天`;
  if (hours >= 1) return `${String(hours)} 小时`;
  return `${String(Math.max(1, Math.floor(seconds / 60)))} 分钟`;
};

export const formatDuration = (seconds: number): string => {
  const minutes = Math.floor(seconds / 60);
  const rest = seconds % 60;
  if (minutes === 0) return `${String(rest)} 秒`;
  return rest === 0 ? `${String(minutes)} 分钟` : `${String(minutes)} 分 ${String(rest)} 秒`;
};

/** 稳定代号 → 一句人话（代号由 `server/backup_worker.py` 定义）。认不出的照原样给。 */
const FAILURE_TEXT: Readonly<Record<string, string>> = {
  BACKUP_INSUFFICIENT_DISK: "服务器磁盘空间不够",
  BACKUP_SNAPSHOT_FAILED: "给数据库做快照失败",
  BACKUP_ARCHIVE_FAILED: "打包备份文件失败",
  BACKUP_UPLOAD_FAILED: "上传到存储桶失败",
  BACKUP_CONFIGURATION_INVALID: "备份配置不可用",
  BACKUP_UNEXPECTED_FAILURE: "意外错误",
  // 服务在备份途中重启：那一轮被收口成失败（`backup_config.RUN_INTERRUPTED_CODE`）。
  BACKUP_INTERRUPTED: "备份途中服务重启，这一轮没做完",
};

export const describeBackupFailure = (code: string | null): string =>
  code === null ? "原因未记录" : (FAILURE_TEXT[code] ?? code);

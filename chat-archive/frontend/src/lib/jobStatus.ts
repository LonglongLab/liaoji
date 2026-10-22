import type { Job } from "@/api/archiveApi";

// 正占着一条工作线程的三档（PSU-2.106）。界面拿它决定「导入 / 同步」那几个按钮
// 灰不灰，判据必须与后端闸门 `active_job_of_kind` 逐字一致——两边不一样时，
// 按钮亮着而请求被 409 挡回来，用户看到的是「点了没反应」。
//
// `paused` 与 `interrupted` 故意不在里面：它们不占线程、不造成重复劳动，而把它们
// 算成挡路会让一个几天前暂停的作业永久堵死那个按钮。
export const holdsAWorker: ReadonlySet<Job["status"]> = new Set([
  "queued",
  "running",
  "pausing",
]);

export const jobStatusLabels: Record<Job["status"], string> = {
  queued: "排队中",
  running: "运行中",
  pausing: "正在暂停",
  paused: "已暂停",
  interrupted: "已中断",
  completed: "已完成",
  failed: "失败",
  cancelled: "已取消",
};

// 四档：完成＝绿，进行中＝琥珀，人自己停的（暂停 / 取消）＝安静的灰，
// 出了事（失败 / 中断）＝danger。原来后两类挤在同一个琥珀里，
// 「已取消」和「失败」看上去一样严重。（PA-UI-010）
export const jobTone = (status: Job["status"]): string => {
  if (status === "completed") return "is-safe";
  if (["failed", "interrupted"].includes(status)) return "is-danger";
  if (["cancelled", "paused"].includes(status)) return "is-neutral";
  return "is-warning";
};

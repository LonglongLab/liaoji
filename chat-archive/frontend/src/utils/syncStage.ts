/**
 * 同步任务自己的四个阶段（PSU-2.99 / `PA-PAGE-002`）。
 *
 * 顺序与理由照抄后端 `remote_sync_job.py:26-31`：按「多久能看见效果」排，不按
 * 体积排——正文是别的东西的落点，缩略图又小又是真机上 100% 覆盖的那一份，
 * 头像一次性且让列表像个样子，原图最慢。
 *
 * **这和建档那六段（快照 / 解密 / 消息导入 / 全文索引 / 回复回填 / 统计聚合）
 * 没有任何关系。** 同步任务此前套用建档的 `ArchiveTimeline`，而它的兜底是
 * `return 1`：`thumbnails` / `avatars` / `originals` 一律被画成「解密」，
 * `messages` 恰好撞名被画到「消息导入」——页面在报一个不存在的流水线位置。
 */
export interface SyncStage {
  readonly key: string;
  readonly label: string;
}

export const SYNC_STAGES: readonly SyncStage[] = [
  { key: "messages", label: "消息正文" },
  { key: "thumbnails", label: "缩略图" },
  { key: "avatars", label: "头像" },
  { key: "originals", label: "原图与文件" },
];

/**
 * 认得就给中文名，不认得就给 `null`——**不编一个**。
 *
 * 与 `utils/jobStage.ts` 逐字同一条口径。调用方拿到 `null` 时照实显示原始标识：
 * 用户拿它去搜索或报错时，`some_new_stage` 比一个意译出来的中文词有用得多。
 */
export const labelForSyncStage = (stage: string | null): string | null =>
  stage === "attachment_refs" ? "附件引用核验" :
    stage === null ? null : (SYNC_STAGES.find((item) => item.key === stage)?.label ?? null);

/**
 * 这个 stage 落在第几格；认不出来就是 `null`——**不回落到任何一格**。
 *
 * 回落是上一版最贵的那个 bug：一个未知阶段被画成「正在解密」，看的人据此
 * 以为流水线走到了一个它根本没有的位置。
 */
export const syncStagePosition = (stage: string | null): number | null => {
  if (stage === null) return null;
  if (stage === "attachment_refs") return SYNC_STAGES.length;
  const index = SYNC_STAGES.findIndex((item) => item.key === stage);
  return index < 0 ? null : index;
};

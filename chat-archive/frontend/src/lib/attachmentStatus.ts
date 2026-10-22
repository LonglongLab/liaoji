import type { AttachmentSummary } from "@/api/archiveApi";
import type { Workspace } from "@/lib/capabilities";

/**
 * 附件缓存状态的中文说法——唯一一份。
 *
 * 原本只住在 `MessageTypeRenderer.vue` 里，PSU-2.66 的搜索结果行也要说同一件事，
 * 与其抄一遍不如搬出来：同一个状态在气泡上和搜索结果里必须是同一个词。
 */
export const ATTACHMENT_STATUS_LABELS: Record<AttachmentSummary["cache_status"], string> = {
  discovered: "待定位",
  locating: "待定位",
  decrypting: "待转换",
  transcoding: "待转换",
  ready: "可查看",
  source_missing: "源文件缺失",
  unsupported: "格式不支持",
  failed: "处理失败",
  evicted: "已淘汰",
};

/**
 * 这个状态值得在列表行上单独标一句吗？
 *
 * `ready` 不必标——那是正常态。`unsupported` **也不标**，因为它是两件事共用的
 * 一个值：本机是「这种格式我们打不开」，而远端在没有 attachment-view 能力时
 * 会把整批附件都记成 `unsupported`（`remoteReadModel.ts:160`）。在结果行上印
 * 「格式不支持」，会把「这一端不做附件」说成「这个文件坏了」。那句话该由页面
 * 自己去说，不该由每一行去替它说。
 */
export const isPendingAttachmentStatus = (status: string | null | undefined): boolean =>
  status === "discovered" ||
  status === "locating" ||
  status === "decrypting" ||
  status === "transcoding" ||
  status === "source_missing" ||
  status === "failed" ||
  status === "evicted";

/** 结果行右上角那个状态词；正常态与说不清的状态都返回 null，行上就不出现。 */
export const attachmentStatusWord = (status: string | null | undefined): string | null =>
  isPendingAttachmentStatus(status)
    ? ATTACHMENT_STATUS_LABELS[status as AttachmentSummary["cache_status"]]
    : null;

/**
 * `variant === "thumbnail"` 这一格，在两端读作两句话（PSU-2.99 批 C / C0）。
 *
 * 数据是同一格：`original_available === false`，即「原图不在远端」。但它在两端
 * 意味着的**事实**不同，所以词也不同：
 *
 * - 本机端：原图早被本机配额淘汰了（真机上 39,017 张）。它不会回来。
 * - 远程端：原图还没同步上去。**再同步一次就有**，与 P13 卡在「原图与文件」
 *   那一段是同一个事实。
 *
 * 说成同一个词，等于把一件能自己解决的事说成一件没救的事。
 *
 * **不动 `unsupported`。** 那个值在远端同时表示「这一端不做附件」，改它的名字
 * 会把「这一端不做附件」说成「这个文件坏了」（见 `isPendingAttachmentStatus`）。
 */
export const attachmentVariantWord = (workspace: Workspace): string =>
  workspace === "remote" ? "未上传" : "仅缩略图";

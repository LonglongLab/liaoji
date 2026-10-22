import type { AttachmentSummary, SearchHit } from "@/api/archiveApi";

const ATTACHMENT_KINDS = new Set<AttachmentSummary["kind"]>([
  "image",
  "emoji",
  "audio",
  "video",
  "file",
  "unknown",
]);

const CACHE_STATUSES = new Set<AttachmentSummary["cache_status"]>([
  "discovered",
  "locating",
  "decrypting",
  "transcoding",
  "ready",
  "source_missing",
  "unsupported",
  "failed",
  "evicted",
]);

const ATTACHMENT_VARIANTS = new Set<NonNullable<AttachmentSummary["variant"]>>([
  "hd",
  "original",
  "thumbnail",
]);

const asAttachmentKind = (
  value: string | null | undefined,
): AttachmentSummary["kind"] =>
  value !== null && ATTACHMENT_KINDS.has(value as AttachmentSummary["kind"])
    ? (value as AttachmentSummary["kind"])
    : "unknown";

const asCacheStatus = (
  value: string | null | undefined,
): AttachmentSummary["cache_status"] =>
  value !== null && CACHE_STATUSES.has(value as AttachmentSummary["cache_status"])
    ? (value as AttachmentSummary["cache_status"])
    : "discovered";

const asVariant = (
  value: string | null | undefined,
): AttachmentSummary["variant"] =>
  value !== null && value !== undefined && ATTACHMENT_VARIANTS.has(value as NonNullable<AttachmentSummary["variant"]>)
    ? (value as NonNullable<AttachmentSummary["variant"]>)
    : null;

export const isSearchHitPreviewable = (hit: SearchHit): boolean =>
  hit.attachment_id != null &&
  hit.attachment_content_available === true &&
  (hit.attachment_kind === "image" || hit.attachment_kind === "video");

/**
 * Adapt a hydrated search hit to the existing media viewer contract.
 * Search results intentionally expose only the first attachment, so a hit
 * maps to one viewer item. Missing/legacy metadata returns null and keeps the
 * result card in its text-only fallback state.
 */
export const searchHitAttachment = (hit: SearchHit): AttachmentSummary | null => {
  if (hit.attachment_id == null) return null;
  return {
    id: hit.attachment_id,
    message_id: hit.message_id,
    kind: asAttachmentKind(hit.attachment_kind),
    file_name: hit.attachment_file_name ?? null,
    mime_type: hit.attachment_mime_type ?? null,
    // 契约里 `attachment_size` 一直都送（`SearchFileRow` 就在读它），这里却曾经
    // 硬写 null——于是从搜索结果进灯箱／文件卡的那条路上，大小凭空消失。
    size: hit.attachment_size ?? null,
    cache_status: asCacheStatus(hit.attachment_cache_status),
    failure_code: null,
    attempt_count: 0,
    content_available: hit.attachment_content_available === true,
    variant: asVariant(hit.attachment_variant),
  };
};

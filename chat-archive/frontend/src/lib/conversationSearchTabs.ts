import type { MessageType } from "@/api/archiveApi";

/**
 * 分类不是互斥的标签页，而是一个能摘掉的条件：它和关键词、日期同时生效，
 * 所以没有「全部」——摘掉分类就是全部。
 *
 * 「语音」是 PSU-2.162 才加进来的。在那之前这里写着「语音没有可检索的文本，
 * 同样不给入口」，那句话当时是对的：语音在索引里一直有行，只是 `content` 为空，
 * 所以永远不命中。现在机器转写进了索引，它才第一次有了可搜的内容。
 */
export type SearchCategory = "file" | "media" | "link" | "voice";

export const SEARCH_CATEGORIES: {
  key: SearchCategory;
  label: string;
}[] = [
  { key: "file", label: "文件" },
  { key: "media", label: "图片与视频" },
  { key: "link", label: "链接" },
  { key: "voice", label: "语音" },
];

export const CATEGORY_TYPES: Record<SearchCategory, MessageType[]> = {
  file: ["file"],
  media: ["image", "video"],
  link: ["link"],
  voice: ["audio"],
};

export const categoryLabel = (category: SearchCategory): string =>
  SEARCH_CATEGORIES.find((item) => item.key === category)?.label ?? "";

export const parseSearchCategory = (value: string | null): SearchCategory | null =>
  value === "file" || value === "media" || value === "link" || value === "voice"
    ? value
    : null;

export const TYPE_LABELS: Record<MessageType, string> = {
  text: "文本",
  image: "图片",
  audio: "语音",
  video: "视频",
  file: "文件",
  link: "链接",
  quote: "引用",
  emoji: "表情",
  app: "应用消息",
  system: "系统消息",
  unknown: "未知类型",
};

export const hitTypeLabel = (hit: { message_type: MessageType }): string =>
  TYPE_LABELS[hit.message_type];

/** 与转录保持同一口径:自己发的记作「我」,否则用归档里的名字,再退回 uid。 */
export const hitSenderName = (hit: {
  is_self: boolean;
  sender_display_name: string | null;
  sender_uid: string;
}): string => {
  if (hit.is_self) return "我";
  // 远端命中可能连 uid 都没送到（映射成空串占位）；空串不是一个可展示的标识。
  return hit.sender_display_name ?? (hit.sender_uid === "" ? "发送者未上传" : hit.sender_uid);
};

/** 摘要优先用正文;媒体消息正文常为空,退回附件文件名;都没有就只留类型标签。 */
export const hitSummary = (hit: {
  message_type: MessageType;
  snippet: string;
  attachment_file_name: string | null;
}): string => {
  const body = hit.snippet !== "" ? hit.snippet : (hit.attachment_file_name ?? "");
  const label = TYPE_LABELS[hit.message_type];
  if (hit.message_type === "text") return body;
  // 归档里的摘要对文件、链接一类已经带了「[类型]」前缀,再套一层就成了「[文件] [文件] x.docx」。
  if (body.startsWith(`[${label}]`)) return body;
  return body === "" ? `[${label}]` : `[${label}] ${body}`;
};

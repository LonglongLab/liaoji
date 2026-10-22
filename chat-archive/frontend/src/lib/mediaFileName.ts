import type { AttachmentSummary } from "@/api/archiveApi";

/**
 * 媒体附件存到磁盘时叫什么。
 *
 * 图片、视频、语音在归档里**从来没有过文件名**——微信本地库就不记（真机
 * 66,554 张图、3,352 个视频、24,938 段语音，`file_name` 全是 null）。所以这里
 * 不是「取一个名字」，是**造**一个；造出来的每一段都得说得出来历：类型词与
 * 时间来自这条消息，扩展名来自字节。
 *
 * 扩展名的来源按「谁更贴近字节」排：归档记下的 mime（本机有，图片视频 100%
 * 覆盖）→ 字节头（远端只有这一条：`attachment_refs` 是一列无类型标识，mime
 * 不在契约里）。两条都答不上来就**不写扩展名**——安一个 `.jpg` 上去，用户
 * 双击打不开时会以为是归档把文件存坏了。
 *
 * 在此之前两端都存成 `media-<64 位标识>`，没有扩展名：Windows 上双击不开，
 * 一次存二十张也认不出哪张是哪张。
 */

/** 归档记下的 mime → 扩展名。只列这套界面真的会打开的那几种。 */
const MIME_EXTENSIONS: Record<string, string> = {
  "image/png": "png",
  "image/jpeg": "jpg",
  "image/gif": "gif",
  "image/webp": "webp",
  "image/bmp": "bmp",
  "video/mp4": "mp4",
  "video/webm": "webm",
  "video/quicktime": "mov",
};

const KIND_WORDS: Partial<Record<AttachmentSummary["kind"], string>> = {
  image: "图片",
  video: "视频",
  audio: "语音",
  emoji: "表情",
};

/** 读这么多字节就够认出下面每一种；`webp` 那条要看到第 12 个。 */
export const MEDIA_SNIFF_BYTES = 12;

const startsWith = (head: Uint8Array, offset: number, bytes: readonly number[]): boolean =>
  bytes.every((byte, index) => head[offset + index] === byte);

const SIGNATURES: readonly { extension: string; matches: (head: Uint8Array) => boolean }[] = [
  { extension: "png", matches: (head) => startsWith(head, 0, [0x89, 0x50, 0x4e, 0x47]) },
  { extension: "jpg", matches: (head) => startsWith(head, 0, [0xff, 0xd8, 0xff]) },
  { extension: "gif", matches: (head) => startsWith(head, 0, [0x47, 0x49, 0x46, 0x38]) },
  // RIFF 容器同时装 webp 与 wav，认的是第 8 字节起那四个字。
  {
    extension: "webp",
    matches: (head) =>
      startsWith(head, 0, [0x52, 0x49, 0x46, 0x46]) &&
      startsWith(head, 8, [0x57, 0x45, 0x42, 0x50]),
  },
  {
    extension: "wav",
    matches: (head) =>
      startsWith(head, 0, [0x52, 0x49, 0x46, 0x46]) &&
      startsWith(head, 8, [0x57, 0x41, 0x56, 0x45]),
  },
  { extension: "bmp", matches: (head) => startsWith(head, 0, [0x42, 0x4d]) },
  // ISO-BMFF：前四个字节是 box 长度，`ftyp` 从第 4 个开始。
  { extension: "mp4", matches: (head) => startsWith(head, 4, [0x66, 0x74, 0x79, 0x70]) },
  { extension: "webm", matches: (head) => startsWith(head, 0, [0x1a, 0x45, 0xdf, 0xa3]) },
];

/**
 * 这段字节头是什么格式。认不出返回 null——沉默好过猜。
 */
export const sniffMediaExtension = (head: Uint8Array): string | null =>
  SIGNATURES.find((signature) => signature.matches(head))?.extension ?? null;

const pad = (value: number, width: number): string => String(value).padStart(width, "0");

/** 本地时区的 `20260817-152012`；界面上那条时间显示的是几点，存下来就是几点。 */
const stamp = (createdAt: number): string | null => {
  if (!Number.isFinite(createdAt) || createdAt <= 0) return null;
  const at = new Date(createdAt * 1000);
  if (Number.isNaN(at.getTime())) return null;
  return (
    `${pad(at.getFullYear(), 4)}${pad(at.getMonth() + 1, 2)}${pad(at.getDate(), 2)}` +
    `-${pad(at.getHours(), 2)}${pad(at.getMinutes(), 2)}${pad(at.getSeconds(), 2)}`
  );
};

/**
 * `sniffedExtension` 是调用方从已经取到手的字节里认出来的那个（没有就传 null）。
 * 认字节这一步留在调用方：这里是纯函数，而读 Blob 是异步的。
 */
export const mediaDownloadName = (
  attachment: AttachmentSummary,
  createdAt: number,
  sniffedExtension: string | null = null,
): string => {
  // 归档记下过名字就用它——那是事实，不是我们造的。文件类附件走的就是这一支。
  const named = attachment.file_name?.trim() ?? "";
  if (named !== "") return named;

  const extension =
    (attachment.mime_type === null
      ? null
      : MIME_EXTENSIONS[attachment.mime_type.trim().toLowerCase()] ?? null) ?? sniffedExtension;
  const word = KIND_WORDS[attachment.kind] ?? "媒体";
  // 时间答不上来时退到标识：难看，但仍然一件一个名字，不会互相覆盖。
  const suffix = stamp(createdAt) ?? attachment.id.slice(0, 12);
  return extension === null ? `${word}-${suffix}` : `${word}-${suffix}.${extension}`;
};

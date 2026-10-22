import JSZip from "jszip";

import type { AttachmentSummary } from "@/api/archiveApi";
import type { RenderMessage } from "@/lib/transcript";

import { createLimiter, EXPORT_CONCURRENCY } from "./limit";

/**
 * 压缩包导出（PSU-2.179）：哪些附件进包、放到哪、叫什么、大概多大。
 *
 * 原型 `designs/merge-export/zip-export.html` 经用户确认：「HTML」升级成压缩包，
 * 附件放进网页旁边的 `附件/` 目录，网页用相对路径找它们。这一层是纯逻辑，不碰 DOM：
 * 清单先算好，面板据此把个数与大小说在前面，取字节与改写网页都照着它走。
 */

export type BundleKind = "image" | "audio" | "video" | "file";

/** 包里的顶层附件目录。 */
export const BUNDLE_ATTACHMENT_ROOT = "附件";
/** 探针：网页里的脚本靠它判断附件目录在不在旁边（没解压就打开时它加载不到）。 */
export const BUNDLE_PROBE_PATH = `${BUNDLE_ATTACHMENT_ROOT}/.here.png`;

const FOLDER: Record<BundleKind, string> = {
  image: "图片",
  audio: "语音",
  video: "视频",
  file: "文件",
};

export interface BundleEntry {
  attachmentId: string;
  messageId: string;
  kind: BundleKind;
  /** 相对网页的路径，如 `附件/图片/2026-09-27 20-09-12.jpg`。 */
  path: string;
  /** 预计字节；不知道就是 null（面板上照实说「大小未知」）。 */
  estimatedBytes: number | null;
  /** 本机缓存已被清掉、导出时要先回源现解一份（只有本机端会出现）。 */
  needsRefetch: boolean;
}

export interface BundlePlan {
  entries: BundleEntry[];
  /** 「视频」开关关掉时只留封面的那几段。 */
  excludedVideos: string[];
  /** 一开始就知道拿不到的（本机与源都没有了），按类计数。 */
  unavailable: Record<BundleKind, number>;
  counts: Record<BundleKind, number>;
  /** 按类真正放进包里的条数（去掉取不到的、被「视频」开关排除的）。 */
  included: Record<BundleKind, number>;
  bytes: Record<BundleKind, number>;
  totalBytes: number;
  /** 有几条的大小不知道：合计是「至少」这么多。 */
  unknownSizes: number;
  /** 按类记的「大小不知道」条数：一类全都不知道时，那一行不能写成「约 0 B」。 */
  unknownByKind: Record<BundleKind, number>;
  refetchCount: number;
}

export interface PlanOptions {
  includeVideos: boolean;
  useOriginals: boolean;
  /** 本机能转 mp3（PSU-2.178）；远端的语音原样是 wav。 */
  voiceExtension: "mp3" | "wav";
}

/** 32 kbps 单声道 mp3 一秒约 4 KB（与后端 `voice_mp3.BIT_RATE` 同一个数）。 */
const MP3_BYTES_PER_SECOND = 4_000;
/** 微信缩略图实测几十 KB；大小列里记的是原图，缩略图没有单独的数，按这个估。 */
const THUMBNAIL_ESTIMATE_BYTES = 60_000;

const KINDS: readonly BundleKind[] = ["image", "audio", "video", "file"];
const zeroed = (): Record<BundleKind, number> => ({ image: 0, audio: 0, video: 0, file: 0 });

const bundleKindOf = (kind: AttachmentSummary["kind"]): BundleKind | null =>
  kind === "image" || kind === "audio" || kind === "video" || kind === "file" ? kind : null;

const pad = (value: number): string => String(value).padStart(2, "0");

/** `2026-09-27 20-09-12`：人读的发送时间，冒号在 Windows 文件名里不合法。 */
export const timeStem = (createdAt: number): string => {
  const at = new Date(createdAt * 1000);
  return (
    `${String(at.getFullYear())}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
    `${pad(at.getHours())}-${pad(at.getMinutes())}-${pad(at.getSeconds())}`
  );
};

/**
 * 文件名安全化：去掉 Windows 不允许的字符与控制字符，收掉首尾的点和空格，截长。
 * 名字只是给人读的，**不承载任何路径**——`../` 之类在这里一并被去掉。
 */
export const safeName = (raw: string, limit = 40): string => {
  const cleaned = Array.from(raw.replace(/[\\/:*?"<>|]/gu, " "))
    .filter((char) => (char.codePointAt(0) ?? 0) >= 0x20)
    .join("")
    .replace(/\s+/gu, " ")
    .replace(/^[\s.]+|[\s.]+$/gu, "");
  return Array.from(cleaned).slice(0, limit).join("");
};

const senderLabel = (message: RenderMessage): string => {
  if (message.is_self === true) return "我";
  const name = safeName(message.sender_display_name ?? "", 20);
  return name === "" ? "对方" : name;
};

const EXTENSION_BY_MIME: Record<string, string> = {
  "image/jpeg": "jpg",
  "image/png": "png",
  "image/gif": "gif",
  "image/webp": "webp",
  "video/mp4": "mp4",
  "video/quicktime": "mov",
  "audio/mpeg": "mp3",
  "audio/wav": "wav",
};

/** 由真实字节的类型定扩展名；认不得的给 `bin`，不编一个像样的。 */
export const extensionFor = (mime: string | null | undefined, fallback: string): string =>
  EXTENSION_BY_MIME[(mime ?? "").split(";")[0]?.trim().toLowerCase() ?? ""] ?? fallback;

const splitExtension = (name: string): [string, string] => {
  const dot = name.lastIndexOf(".");
  return dot <= 0 ? [name, ""] : [name.slice(0, dot), name.slice(dot)];
};

/**
 * 归档里记的字节数。**缺字段与 null 一样当「不知道」**：远端的读模型会省掉这个键，
 * 放进算术里就是 NaN，面板上会印出「约 NaN TB」（e2e 当场抓到的）。
 */
const knownSize = (attachment: AttachmentSummary): number | null =>
  typeof attachment.size === "number" && Number.isFinite(attachment.size) ? attachment.size : null;

const estimateFor = (attachment: AttachmentSummary, kind: BundleKind, options: PlanOptions) => {
  if (kind === "audio" && options.voiceExtension === "mp3") {
    const seconds = (attachment.duration_ms ?? 0) / 1000;
    return seconds > 0 ? Math.ceil(seconds * MP3_BYTES_PER_SECOND) : null;
  }
  const size = knownSize(attachment);
  if (kind === "image" && !options.useOriginals) {
    return size === null ? THUMBNAIL_ESTIMATE_BYTES : Math.min(size, THUMBNAIL_ESTIMATE_BYTES);
  }
  return size;
};

/**
 * 一条附件的目标路径（不含扩展名去重之外的任何东西）。
 *
 * - 图片、视频：按发送时间命名，扩展名等取到字节再按真实类型定（`finalizePath`）。
 * - 语音：发送时间 + 谁说的。
 * - 文件：保留原名；没有原名的给一个**看得出是兜底**的名字，不编一个像真的。
 */
const stemFor = (attachment: AttachmentSummary, kind: BundleKind, message: RenderMessage) => {
  const time = timeStem(message.created_at);
  if (kind === "audio") return `${time} ${senderLabel(message)}`;
  if (kind === "file") {
    const original = safeName(attachment.file_name ?? "", 80);
    return original === "" ? `未命名文件 ${time}` : original;
  }
  return time;
};

/**
 * 算出这批消息的打包清单。
 *
 * 表情不进 `附件/`：它小，而且是「聊天记录」的一部分，照旧内嵌在网页里。
 * 合并转发卡片里的附件不在这一批（卡片在网页里就地展开成文字）。
 */
export const planBundle = (messages: readonly RenderMessage[], options: PlanOptions): BundlePlan => {
  const entries: BundleEntry[] = [];
  const excludedVideos: string[] = [];
  const unavailable = zeroed();
  const counts = zeroed();
  const bytes = zeroed();
  const unknownByKind = zeroed();
  const taken = new Set<string>();

  for (const message of messages) {
    for (const attachment of message.attachments) {
      const kind = bundleKindOf(attachment.kind);
      if (kind === null) continue;
      counts[kind] += 1;
      if (!attachment.content_available) {
        unavailable[kind] += 1;
        continue;
      }
      if (kind === "video" && !options.includeVideos) {
        excludedVideos.push(attachment.id);
        continue;
      }
      const stem = stemFor(attachment, kind, message);
      const folder = `${BUNDLE_ATTACHMENT_ROOT}/${FOLDER[kind]}`;
      // 文件带着原扩展名去重（`报价单.pdf` 与 `报价单 (2).pdf`）；其余先按词干占位，
      // 扩展名取到字节后补上——同一秒两张图不会因为一张 jpg 一张 png 就躲过去重。
      const [base, extension] = kind === "file" ? splitExtension(stem) : [stem, ""];
      let candidate = `${folder}/${base}${extension}`;
      for (let n = 2; taken.has(candidate.toLowerCase()); n += 1) {
        candidate =
          kind === "file"
            ? `${folder}/${base} (${String(n)})${extension}`
            : `${folder}/${base}-${String(n)}`;
      }
      taken.add(candidate.toLowerCase());
      const estimate = estimateFor(attachment, kind, options);
      if (estimate === null) unknownByKind[kind] += 1;
      else bytes[kind] += estimate;
      entries.push({
        attachmentId: attachment.id,
        messageId: message.id,
        kind,
        path:
          kind === "audio" ? `${candidate}.${options.voiceExtension}` : candidate,
        estimatedBytes: estimate,
        needsRefetch: attachment.cache_status === "evicted",
      });
    }
  }
  return {
    entries,
    excludedVideos,
    unavailable,
    counts,
    included: KINDS.reduce(
      (acc, kind) => ({ ...acc, [kind]: entries.filter((entry) => entry.kind === kind).length }),
      zeroed(),
    ),
    bytes,
    totalBytes: KINDS.reduce((sum, kind) => sum + bytes[kind], 0),
    unknownSizes: KINDS.reduce((sum, kind) => sum + unknownByKind[kind], 0),
    unknownByKind,
    refetchCount: entries.filter((entry) => entry.needsRefetch).length,
  };
};

/** 图片、视频的扩展名要看真实字节（缩略图常是 jpg，wxgf 解出来是 png）。 */
export const finalizePath = (entry: BundleEntry, blob: Blob): string => {
  if (entry.kind === "image") return `${entry.path}.${extensionFor(blob.type, "jpg")}`;
  if (entry.kind === "video") return `${entry.path}.${extensionFor(blob.type, "mp4")}`;
  return entry.path;
};

export interface BundleLoaders {
  loadAttachment: (attachmentId: string) => Promise<Blob>;
  loadThumbnail: ((attachmentId: string) => Promise<Blob>) | undefined;
  /** 本机才有（PSU-2.178）；不给就按 wav 取原件。 */
  loadVoiceMp3: ((attachmentId: string) => Promise<Blob>) | undefined;
}

export interface FetchedEntry {
  entry: BundleEntry;
  path: string;
  blob: Blob;
}

export interface BundleFetchResult {
  fetched: FetchedEntry[];
  /** 取的时候才发现拿不到的，按类计数。 */
  missing: Record<BundleKind, number>;
  missingIds: Set<string>;
}

const loaderFor = (entry: BundleEntry, loaders: BundleLoaders, useOriginals: boolean) => {
  if (entry.kind === "audio" && loaders.loadVoiceMp3 !== undefined) return loaders.loadVoiceMp3;
  if (entry.kind === "image" && !useOriginals && loaders.loadThumbnail !== undefined) {
    const thumbnail = loaders.loadThumbnail;
    // 没有缩略图就退原图——与「导出网页」一直以来的做法相同。
    return (id: string) => thumbnail(id).catch(() => loaders.loadAttachment(id));
  }
  return loaders.loadAttachment;
};

/**
 * 按清单取字节。**一条取不到不拖垮整批**：记进 missing，网页里那条换成
 * 「没能带上」的占位，导完的提示里按类说缺几条。
 */
export const fetchBundle = async (
  plan: BundlePlan,
  loaders: BundleLoaders,
  options: { useOriginals: boolean; onProgress?: (done: number, total: number) => void },
): Promise<BundleFetchResult> => {
  const limit = createLimiter(EXPORT_CONCURRENCY);
  const missing = zeroed();
  const missingIds = new Set<string>();
  const fetched: FetchedEntry[] = [];
  let done = 0;
  await Promise.all(
    plan.entries.map((entry) =>
      limit(async () => {
        try {
          const blob = await loaderFor(entry, loaders, options.useOriginals)(entry.attachmentId);
          fetched.push({ entry, path: finalizePath(entry, blob), blob });
        } catch {
          missing[entry.kind] += 1;
          missingIds.add(entry.attachmentId);
        } finally {
          done += 1;
          options.onProgress?.(done, plan.entries.length);
        }
      }),
    ),
  );
  // 并发取回的顺序是乱的；按清单顺序排好，包里的条目顺序才稳定。
  const order = new Map(plan.entries.map((entry, index) => [entry.attachmentId, index]));
  fetched.sort(
    (a, b) => (order.get(a.entry.attachmentId) ?? 0) - (order.get(b.entry.attachmentId) ?? 0),
  );
  return { fetched, missing, missingIds };
};

/** 「已导出，但有 1 条语音、2 张图片没能带上。」里的那一段。 */
export const describeMissing = (missing: Record<BundleKind, number>): string | null => {
  const words: Record<BundleKind, [string, string]> = {
    image: ["张", "图片"],
    audio: ["条", "语音"],
    video: ["段", "视频"],
    file: ["个", "文件"],
  };
  const parts = KINDS.filter((kind) => missing[kind] > 0).map(
    (kind) => `${String(missing[kind])} ${words[kind][0]}${words[kind][1]}`,
  );
  return parts.length === 0 ? null : parts.join("、");
};

/** 网页在包里的名字：一眼看出该双击哪个。 */
export const BUNDLE_PAGE_NAME = "打开我.html";

/** 1×1 透明 PNG：探针文件的字节。 */
const PROBE_PNG = Uint8Array.from(
  globalThis.atob(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg==",
  ),
  (char) => char.charCodeAt(0),
);

/**
 * 打成一个 zip：`<base>/打开我.html` + `<base>/附件/...`。
 *
 * 外面再包一层同名目录：解压到哪儿都不会把网页和附件撒一地。
 * 媒体不再压缩（jpg/mp3/mp4 本来就压过，再 deflate 只费 CPU），网页压缩。
 */
export const packBundle = async (
  base: string,
  html: string,
  fetched: readonly FetchedEntry[],
): Promise<Blob> => {
  const zip = new JSZip();
  const folder = zip.folder(base);
  if (folder === null) throw new Error("could not create the bundle folder");
  folder.file(BUNDLE_PAGE_NAME, html, { compression: "DEFLATE" });
  folder.file(BUNDLE_PROBE_PATH, PROBE_PNG, { binary: true });
  for (const item of fetched) folder.file(item.path, item.blob, { compression: "STORE" });
  return zip.generateAsync({ type: "blob" });
};

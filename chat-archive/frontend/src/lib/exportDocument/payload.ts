import type { AttachmentSummary } from "@/api/archiveApi";
import { mergedForwardCopyText } from "@/lib/mergedForward";
import type { RenderMessage } from "@/lib/transcript";
import { formatDateTimeSeparator } from "@/utils/time";

import { createLimiter, EXPORT_CONCURRENCY } from "./limit";

/**
 * 导出前要准备的两样东西（PSU-2.168）：图片字节，和「复制」那一份纯文本。
 *
 * 两个都是纯函数，能在 happy-dom 上测到底——真渲染不能，但这两件事和渲染无关。
 */

/** 能画成 `<img>` 的只有这两类；语音、视频、文件在气泡里本来就不是图片。 */
const IMAGE_KINDS: ReadonlySet<AttachmentSummary["kind"]> = new Set(["image", "emoji"]);

export interface PreloadOptions {
  loadAttachment: (attachmentId: string) => Promise<Blob>;
  loadThumbnail: ((attachmentId: string) => Promise<Blob>) | undefined;
  /** 用户勾了「用原图」：跳过缩略图那一路。 */
  useOriginals: boolean;
}

/**
 * 把这批消息要用到的图片字节**先解好**。
 *
 * 为什么非要先解：`MessageTypeRenderer` 是自己异步取图的，于是「渲染完了」没有
 * 任何确定信号，等多久都是猜。先解好再挂，组件那一路 await 落在一个已经完成的
 * Promise 上，剩下要等的只有浏览器解码，`waitForImages` 才等得出头。
 *
 * **一张取不到不能拖垮整趟导出**：漏掉的那张会在文档里被 `inlineImages` 标成
 * `data-export-missing` 并计入报告，由界面如实说出来。
 */
export const preloadAttachments = async (
  messages: readonly RenderMessage[],
  options: PreloadOptions,
): Promise<Map<string, Blob>> => {
  const wanted = messages.flatMap((message) =>
    message.attachments.filter(
      (attachment) => IMAGE_KINDS.has(attachment.kind) && attachment.content_available,
    ),
  );
  const cache = new Map<string, Blob>();
  // 同时最多取几张：勾了原图时一张就是几 MB，全部同时在路上就是全部同时在内存里。
  const limit = createLimiter(EXPORT_CONCURRENCY);
  await Promise.all(
    wanted.map((attachment) =>
      limit(async () => {
        const blob = await loadOne(attachment.id, options);
        if (blob !== null) cache.set(attachment.id, blob);
      }),
    ),
  );
  return cache;
};

/**
 * 每条消息在屏外文档里**应当**放出几张图（评审 P1）：`waitForExportMedia` 等的就是这张表。
 *
 * 只算真会画出来的：字节在手（预解成功的）、是图片或表情、而且不在合并转发里——
 * 合并转发的气泡只画一张卡片，它的图在查看器里，不在气泡上（`MessageTypeRenderer`
 * 的 `loadReadyMedia` 在那里整段跳过）。算进去就是每次都干等到超时。
 *
 * 按消息分账而不是数总数：旧写法数全文档的 `<img>`，头像也算一张，于是头像先到
 * 就「数够了」，图片还没放进来就开始序列化。
 */
export const expectedInlineMedia = (
  messages: readonly RenderMessage[],
  cache: ReadonlyMap<string, Blob>,
): Map<string, number> => {
  const expected = new Map<string, number>();
  for (const message of messages) {
    if (message.message_type === "app" && message.app_card?.kind === "merged_forward") continue;
    const count = message.attachments.filter(
      (attachment) => IMAGE_KINDS.has(attachment.kind) && cache.has(attachment.id),
    ).length;
    if (count > 0) expected.set(message.id, count);
  }
  return expected;
};

const loadOne = async (
  attachmentId: string,
  options: PreloadOptions,
): Promise<Blob | null> => {
  if (!options.useOriginals && options.loadThumbnail !== undefined) {
    try {
      return await options.loadThumbnail(attachmentId);
    } catch {
      // 没有缩略图就回退原图——回退即现状，不是一次失败。
    }
  }
  try {
    return await options.loadAttachment(attachmentId);
  } catch {
    return null;
  }
};

const ATTACHMENT_WORDS: Record<AttachmentSummary["kind"], string> = {
  image: "图片",
  emoji: "表情",
  audio: "语音",
  video: "视频",
  file: "文件",
  unknown: "附件",
};

const senderOf = (message: RenderMessage): string => {
  if (message.is_self === true) return "我";
  const name = (message.sender_display_name ?? "").trim();
  if (name !== "") return name;
  const uid = (message.sender_uid ?? "").trim();
  // 归属没被告知时说「发送者未上传」，与转录里那一行同一句话。
  // 这里一旦漏掉就会印出 `undefined：`，那是把内部状态泄给读者。
  return uid === "" ? "发送者未上传" : uid;
};

const bodyOf = (message: RenderMessage): string => {
  const card = message.app_card;
  if (card !== null && card.kind === "merged_forward") {
    return mergedForwardCopyText(card);
  }
  const text = message.content_text.trim();
  if (text !== "") return text;
  const attachment = message.attachments[0];
  if (attachment !== undefined) return `[${ATTACHMENT_WORDS[attachment.kind]}]`;
  if (card !== null) {
    const title = card.title.trim();
    return title === "" ? "[卡片消息]" : `[卡片消息] ${title}`;
  }
  return "[非文本消息]";
};

/**
 * 「复制」那一份：一条一行，顺序就是转录里的顺序。
 *
 * 合并转发就地摊平（`mergedForwardCopyText`）——只印一个标题的话，粘出去的人
 * 得到的是一句没有内容的话。
 */
export const selectionCopyText = (messages: readonly RenderMessage[]): string =>
  messages
    .map(
      (message) =>
        `${formatDateTimeSeparator(message.created_at)} ${senderOf(message)}：${bodyOf(message)}`,
    )
    .join("\n");

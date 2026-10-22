import type { ArchiveApi, RemoteShare } from "@/api/archiveApi";
import {
  fetchBundle,
  planBundle,
  type BundleKind,
  type BundleLoaders,
  type BundlePlan,
} from "@/lib/exportDocument/bundle";
import type { RenderMessage } from "@/lib/transcript";

import { sha256Hex } from "./digest";
import { buildShareSnapshot } from "./snapshot";

/**
 * 生成一个分享（PSU-2.182）：取附件字节 → 建草稿 → 逐个上传 → 定稿。
 *
 * 建草稿时带上每份字节的 SHA-256（PSU-2.184）：远端归档里已有同样内容的，远端在
 * 对象存储里直接复制，应答的 `awaiting_upload` 只列还要这边传的那几个。
 *
 * 附件的取法与「网页压缩包」是同一套（`planBundle` + `fetchBundle`）：缺省缩略图、
 * 勾「用原图」才带原图、本机语音转 mp3。表情不在压缩包清单里（那边内嵌在网页里），
 * 这里另外取一遍——对方页面没有它就只剩一个「[表情]」。
 *
 * 一个附件取不到不拖垮整份：快照里照样有它（对方看到「没能带上」），只是不上传。
 */

export type ShareExpiry = 1 | 7 | 30 | null;

export interface ShareRunOptions {
  expiresInDays: ShareExpiry;
  hideNames: boolean;
  useOriginals: boolean;
  conversationName: string;
  voiceExtension: "mp3" | "wav";
  onProgress: (text: string) => void;
}

export interface ShareRunResult {
  share: RemoteShare;
  /** 取的时候才发现拿不到的，按类计数（导完的提示里说）。 */
  missing: Record<BundleKind, number>;
}

export type ShareApi = Pick<ArchiveApi, "createShare" | "uploadShareObject" | "finalizeShare">;

/**
 * 分享取字节的那两个加载器（PSU-2.185）：交出存着的那份字节——不解 wxgf、不转码。
 * 缩略图没有就退原图，由 `fetchBundle` 统一做。
 */
export interface ShareLoaders {
  loadAttachment: (attachmentId: string) => Promise<Blob>;
  loadThumbnail: ((attachmentId: string) => Promise<Blob>) | undefined;
}

export const planShare = (
  messages: readonly RenderMessage[],
  options: { useOriginals: boolean; voiceExtension: "mp3" | "wav" },
): BundlePlan =>
  planBundle(messages, {
    includeVideos: true,
    useOriginals: options.useOriginals,
    voiceExtension: options.voiceExtension,
  });

const fetchEmojis = async (
  messages: readonly RenderMessage[],
  loaders: BundleLoaders,
): Promise<Map<string, Blob>> => {
  const emojis = messages.flatMap((message) =>
    message.attachments.filter(
      (attachment) => attachment.kind === "emoji" && attachment.content_available,
    ),
  );
  const got = new Map<string, Blob>();
  await Promise.all(
    emojis.map(async (attachment) => {
      try {
        got.set(attachment.id, await loaders.loadAttachment(attachment.id));
      } catch {
        // 表情取不到就是没带上，对方看到「[表情]」。
      }
    }),
  );
  return got;
};

export const runShare = async (
  api: ShareApi,
  messages: readonly RenderMessage[],
  loaders: BundleLoaders,
  options: ShareRunOptions,
): Promise<ShareRunResult> => {
  const plan = planShare(messages, options);
  const bundle = await fetchBundle(plan, loaders, {
    useOriginals: options.useOriginals,
    onProgress: (done, total) => {
      options.onProgress(`正在准备附件 ${String(done)}/${String(total)}`);
    },
  });
  const fetched = new Map<string, Blob>(
    bundle.fetched.map((item) => [item.entry.attachmentId, item.blob]),
  );
  for (const [id, blob] of await fetchEmojis(messages, loaders)) fetched.set(id, blob);

  const built = buildShareSnapshot(messages, fetched, {
    hideNames: options.hideNames,
    conversationName: options.conversationName,
  });
  options.onProgress("正在生成链接…");
  // 告诉远端每份字节的摘要：归档里有同样内容的，远端在对象存储里复制，这边不再传（PSU-2.184）。
  const digests = new Map(
    await Promise.all(
      built.carried.map(async (item) => [item.shareId, await sha256Hex(item.blob)] as const),
    ),
  );
  const draft = await api.createShare({
    ...built.draft,
    attachments: built.draft.attachments.map((item) => {
      const sha256 = digests.get(item.attachment_id);
      return sha256 === undefined ? item : { ...item, sha256 };
    }),
    expires_in_days: options.expiresInDays,
  });
  // 旧远端的应答里没有 awaiting_upload：它不认得摘要，一律要传。
  const awaiting = draft.awaiting_upload === undefined ? null : new Set(draft.awaiting_upload);
  const pending = built.carried.filter((item) => awaiting === null || awaiting.has(item.shareId));
  let uploaded = 0;
  for (const item of pending) {
    options.onProgress(`正在上传附件 ${String(uploaded + 1)}/${String(pending.length)}`);
    await api.uploadShareObject(draft.token, item.shareId, item.blob);
    uploaded += 1;
  }
  const share = await api.finalizeShare(draft.token);
  return { share, missing: bundle.missing };
};

/** 能直接复制的完整链接：本机上远端给了；远程网页上用当前页面的 origin 拼。 */
export const shareLink = (share: Pick<RemoteShare, "url" | "path">, origin: string): string =>
  share.url ?? `${origin}${share.path}`;

/** 当前页面的 origin。放在 .ts 里：`.vue` 上没有配浏览器全局（见 `utils/clipboard.ts`）。 */
export const currentOrigin = (): string => globalThis.location.origin;

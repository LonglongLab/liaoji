import type { RemoteShare } from "@/api/archiveApi";
import { formatBytes } from "@/utils/format";

/** 还剩不到这么久就算「快过期」（原型第 4 块的黄色那一档）。 */
export const SOON_SECONDS = 24 * 3600;

const pad = (value: number): string => String(value).padStart(2, "0");

/** 「12 月 3 日 21:40」：分享的失效与生成时刻都这么说。 */
export const shareMoment = (epochSeconds: number): string => {
  const at = new Date(epochSeconds * 1000);
  return `${String(at.getMonth() + 1)} 月 ${String(at.getDate())} 日 ${pad(at.getHours())}:${pad(at.getMinutes())}`;
};

const daysUntil = (from: number, to: number): number => Math.max(1, Math.ceil((to - from) / 86_400));

export type ShareTier = "live" | "soon" | "expired";

export const shareTier = (share: Pick<RemoteShare, "state" | "expires_at">, now: number): ShareTier => {
  if (share.state === "expired") return "expired";
  if (share.expires_at !== null && share.expires_at - now <= SOON_SECONDS) return "soon";
  return "live";
};

/** 「状态」那一格：还剩几天 / 明天失效 / 永久 / 已过期 · N 天后清掉。 */
export const shareStatusWords = (
  share: Pick<RemoteShare, "state" | "expires_at" | "purge_at">,
  now: number,
): string => {
  if (share.state === "expired") {
    return share.purge_at === null
      ? "已过期"
      : `已过期 · ${String(daysUntil(now, share.purge_at))} 天后清掉`;
  }
  if (share.expires_at === null) return "永久";
  if (share.expires_at - now <= SOON_SECONDS) return "明天前失效";
  return `还剩 ${String(daysUntil(now, share.expires_at))} 天`;
};

/** 面板完成态上那一句。 */
export const expiryWords = (expiresAt: number | null): string =>
  expiresAt === null ? "永久有效，记得自己删" : `${shareMoment(expiresAt)}后失效`;

/** 「分享了什么」那一格的第二行：条数与附件。 */
export const shareContentWords = (
  share: Pick<RemoteShare, "message_count" | "attachment_count" | "attachment_bytes">,
): string => {
  const count = `${String(share.message_count)} 条`;
  if (share.attachment_count === 0) return `${count} · 纯文字`;
  return `${count} · ${String(share.attachment_count)} 个附件，${formatBytes(share.attachment_bytes)}`;
};

/** 「打开次数」那一格：只有数字和时间，不记是谁。 */
export const shareViewsWords = (share: Pick<RemoteShare, "views" | "last_viewed_at">): string =>
  share.last_viewed_at === null
    ? `${String(share.views)} 次`
    : `${String(share.views)} 次 · 最近 ${shareMoment(share.last_viewed_at)}`;

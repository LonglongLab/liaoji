import type {
  AppCard,
  AppCardItem,
  AttachmentSummary,
  PublicShare,
  ShareSnapshotMessage,
} from "@/api/archiveApi";
import type { RenderMessage } from "@/lib/transcript";

/**
 * 对方打开分享链接看到的那一页（PSU-2.183）：把快照读回界面认得的 `RenderMessage`，
 * 好让气泡、时间、引用、卡片与 App 里一模一样（`MessageTypeRenderer`）。
 *
 * 快照是 2.181 那份白名单：没有 uid、没有会话 id、没有原附件 id。这里补的全是
 * 「界面要一个值、而这里如实没有」的那几格——`sender_uid: null`（与远端 v1 记录
 * 同一个含义：没被告知）、附件的状态只分「带上了 / 没带上」。
 */

const CARD_KINDS = new Set<AppCard["kind"]>([
  "merged_forward",
  "link",
  "image",
  "voice",
  "video",
  "miniprogram",
  "channel",
  "location",
  "business_card",
  "voip",
  "file",
  "quote",
  "transfer",
  "redpacket",
  "group_payment",
  "pat",
  "card",
]);
const ITEM_KINDS = new Set<AppCardItem["kind"]>([
  "text",
  "image",
  "video",
  "voice",
  "link",
  "file",
  "record",
  "sticker",
  "unknown",
]);

const cardOf = (card: ShareSnapshotMessage["card"]): AppCard | null => {
  if (card === null) return null;
  const kind = CARD_KINDS.has(card.kind as AppCard["kind"]) ? (card.kind as AppCard["kind"]) : "link";
  return {
    kind,
    title: card.title,
    subtitle: card.subtitle,
    source_name: card.source_name,
    url: card.url,
    items: card.items.map((item) => ({
      sender: item.sender,
      created_at: null,
      kind: ITEM_KINDS.has(item.kind as AppCardItem["kind"])
        ? (item.kind as AppCardItem["kind"])
        : "unknown",
      text: item.text,
      nested: null,
      file_name: null,
      file_size: null,
      file_format: null,
      source_desc: null,
      full_md5: null,
      thumb_md5: null,
      attachment_id: null,
    })),
    item_total: card.items.length,
    truncated: false,
    quoted: null,
    transfer_amount: null,
    transfer_memo: null,
    transfer_status: null,
    transfer_time: null,
    transfer_settled_at: null,
    transfer_payer_uid: null,
    transfer_payee_uid: null,
    transfer_sender_role: null,
    packet_greeting: null,
    packet_scene: null,
    packet_cover: null,
    packet_sender_action: null,
    packet_receiver_action: null,
    packet_exclusive: false,
    packet_expires_at: null,
  };
};

/** 隐藏名字时的「成员 A」——头像画成那个字母，而不是人人都是一个「成」字。 */
const MEMBER_LABEL = /^成员 ([A-Z]+)$/u;

/**
 * 一个画着字母的头像（data: SVG）。`AvatarBadge` 取名字的第一个字当占位，对「成员 A /
 * 成员 B」来说人人都是「成」；原型要的是「头像换成字母」。只在分享页用，不动 App 的气泡。
 */
export const letterAvatar = (letters: string): string => {
  const size = letters.length > 1 ? 15 : 19;
  const svg =
    `<svg xmlns="http://www.w3.org/2000/svg" width="40" height="40" viewBox="0 0 40 40">` +
    `<rect width="40" height="40" rx="8" fill="#5b6b5f"/>` +
    `<text x="20" y="26.5" font-family="system-ui,sans-serif" font-size="${String(size)}" ` +
    `font-weight="600" fill="#fff" text-anchor="middle">${letters}</text></svg>`;
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`;
};

const avatarFor = (sender: string | null, namesHidden: boolean): string | null => {
  if (!namesHidden || sender === null) return null;
  const match = MEMBER_LABEL.exec(sender);
  return match?.[1] === undefined ? null : letterAvatar(match[1]);
};

/** 快照里的消息 → 界面的消息。`carried` 是这份分享真带上了字节的附件编号。 */
export const toRenderMessages = (
  share: Pick<PublicShare, "snapshot" | "attachments">,
): RenderMessage[] => {
  const carried = new Set(share.attachments.map((item) => item.attachment_id));
  return share.snapshot.messages.map((message, index) => {
    const id = `share_m${String(index)}`;
    const attachments: AttachmentSummary[] = message.attachments.map((item) => {
      const present = carried.has(item.attachment_id);
      return {
        id: item.attachment_id,
        message_id: id,
        kind: item.kind,
        file_name: item.file_name,
        mime_type: item.mime_type,
        size: item.size,
        duration_ms: item.duration_ms,
        cache_status: present ? "ready" : "source_missing",
        failure_code: null,
        attempt_count: 0,
        content_available: present,
        variant: null,
      };
    });
    return {
      id,
      conversation_id: "share",
      sender_uid: null,
      sender_display_name: message.sender,
      sender_avatar_ref: avatarFor(message.sender, share.snapshot.names_hidden),
      created_at: message.time,
      message_type: message.type,
      content_text: message.text,
      is_self: message.is_self,
      reply_to_id: null,
      parse_status: "parsed",
      attachments,
      reply_preview:
        message.quote === null
          ? null
          : {
              message_id: "",
              sender_display_name: message.quote.sender,
              content_text: message.quote.text,
              message_type: message.quote.type,
              attachments: [],
            },
      app_card: cardOf(message.card),
      transcript:
        message.transcript === null
          ? null
          : {
              text: message.transcript,
              status: "ok",
              engine: "",
              low_confidence: false,
              failure_code: null,
            },
    };
  });
};

/** 页首那一行：「3 月 2 日 – 3 月 5 日 · 共 6 条」。 */
export const dateRangeWords = (messages: readonly { created_at: number }[]): string => {
  if (messages.length === 0) return "";
  const day = (seconds: number): string => {
    const at = new Date(seconds * 1000);
    return `${String(at.getFullYear())} 年 ${String(at.getMonth() + 1)} 月 ${String(at.getDate())} 日`;
  };
  const times = messages.map((message) => message.created_at);
  const first = day(Math.min(...times));
  const last = day(Math.max(...times));
  return first === last ? first : `${first} – ${last}`;
};

/** 页脚那一句的后半截。 */
export const shareExpiryFooter = (expiresAt: number | null): string => {
  if (expiresAt === null) return "长期有效";
  const at = new Date(expiresAt * 1000);
  return `${String(at.getMonth() + 1)} 月 ${String(at.getDate())} 日后失效`;
};

export type ShareFetch = (url: string) => Promise<Response>;

/**
 * 附件加载器：按编号拿那 5 分钟的直读地址去取。
 *
 * 地址过期了（对象存储答 403，或者已经过了它自己说的时刻）就**重新取一次快照**拿新
 * 地址再试一次——不让读者看到一个坏图、一段放不出来的语音。再失败就如实失败。
 */
export const createShareLoader = (
  initial: PublicShare["attachments"],
  refresh: () => Promise<PublicShare>,
  fetchImpl: ShareFetch,
  now: () => number = () => Math.floor(Date.now() / 1000),
): ((attachmentId: string) => Promise<Blob>) => {
  let current = initial;
  let refreshing: Promise<void> | null = null;
  const find = (id: string) => current.find((item) => item.attachment_id === id);
  const renew = (): Promise<void> => {
    refreshing ??= refresh()
      .then((share) => {
        current = share.attachments;
      })
      .finally(() => {
        refreshing = null;
      });
    return refreshing;
  };
  const attempt = async (id: string): Promise<Response | null> => {
    const entry = find(id);
    if (entry?.url === null || entry?.url === undefined) return null;
    if (entry.expires_at !== null && entry.expires_at <= now()) return null;
    const response = await fetchImpl(entry.url);
    return response.ok ? response : null;
  };
  return async (attachmentId: string): Promise<Blob> => {
    const first = await attempt(attachmentId);
    if (first !== null) return first.blob();
    await renew();
    const second = await attempt(attachmentId);
    if (second === null) throw new Error("这个附件现在取不到");
    return second.blob();
  };
};

/** 取对象存储上的字节。放在 .ts 里：`.vue` 上没有配浏览器全局。 */
export const browserFetch: ShareFetch = (url) =>
  globalThis.fetch(url, { credentials: "omit", referrerPolicy: "no-referrer" });

/**
 * 页面自己再说一遍「别收录」（响应头 `X-Robots-Tag` 已经说过；有的抓取器只读 meta）。
 * 返回一个撤销函数：离开这一页时拿掉，免得带到别的页面上。
 */
export const markNoIndex = (): (() => void) => {
  const meta = globalThis.document.createElement("meta");
  meta.name = "robots";
  meta.content = "noindex, nofollow";
  globalThis.document.head.appendChild(meta);
  return () => {
    meta.remove();
  };
};

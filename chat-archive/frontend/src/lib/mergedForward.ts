import type { MessageSummary } from "@/api/archiveApi";

export type AppCard = NonNullable<MessageSummary["app_card"]>;
export type AppCardItem = AppCard["items"][number];

export const safeForwardAvatar = (value: string | null | undefined): string | null => {
  if (!value || value.length > 2048 || Array.from(value).some((char) => char.charCodeAt(0) < 32)) return null;
  try {
    const url = new URL(value);
    if (url.protocol !== "https:" || url.username || url.password || url.port || url.hash ||
      !["wx.qlogo.cn", "wxapp.tc.qq.com", "finder.video.qq.com", "findernd.video.qq.com"].includes(url.hostname)) return null;
    return url.href;
  } catch { return null; }
};

const KIND_LABELS: Record<AppCardItem["kind"], string> = {
  text: "文本",
  image: "图片",
  video: "视频",
  voice: "语音",
  link: "链接",
  file: "文件",
  record: "聊天记录",
  sticker: "动画表情",
  unknown: "未知条目",
};

export const mergedForwardSenderLine = (item: AppCardItem): string => {
  const sender = item.sender?.trim() ?? "";
  const qualifier = item.source_desc?.trim() ?? "";
  // WeChat writes the two together, 联系人C@中国交通建设集团有限公司, with no
  // separator of its own — the qualifier already carries its "@".
  return sender === "" ? qualifier : `${sender}${qualifier}`;
};

export const mergedForwardTitle = (card: AppCard): string =>
  card.title.trim() === "" ? "聊天记录" : card.title.trim();

export const mergedForwardKindLabel = (kind: AppCardItem["kind"]): string =>
  KIND_LABELS[kind];

/**
 * A record item's real title, or null when the archive holds none.
 *
 * `file_name` carries the dataitem's datatitle — 「甲与乙的聊天记录」 — and is
 * preferred because cards derived before APP_CARD_VERSION 5 titled the nested
 * card with the datadesc digest: every nested message flattened into one line.
 * Falling back through the nested card's title keeps rows the replay has
 * already repaired (and rows that only had a title to offer) working.
 */
export const mergedForwardRecordTitle = (item: AppCardItem): string | null => {
  const fromTitle = item.file_name?.trim() ?? "";
  if (fromTitle !== "") return fromTitle;
  if (item.nested === null) return null;
  return mergedForwardTitle(item.nested);
};

const itemBody = (item: AppCardItem): string => {
  const text = item.text?.trim() ?? "";
  if (item.kind === "text") return text === "" ? "[文本]" : text;
  if (item.kind === "record") {
    const nestedTitle = mergedForwardRecordTitle(item);
    return nestedTitle === null ? "[聊天记录]" : `[聊天记录] ${nestedTitle}`;
  }
  const label = `[${mergedForwardKindLabel(item.kind)}]`;
  return text === "" ? label : `${label} ${text}`;
};

export const mergedForwardItemSummary = (item: AppCardItem): string => {
  const sender = item.sender?.trim() ?? "";
  const body = itemBody(item);
  return sender === "" ? body : `${sender}：${body}`;
};

// The caller can supply the title it is showing — a drilled-into nested card
// derived before APP_CARD_VERSION 5 carries the flattened digest as its own.
export const mergedForwardCopyText = (card: AppCard, title?: string): string =>
  [title ?? mergedForwardTitle(card), ...card.items.flatMap((item) => [
    mergedForwardItemSummary(item),
    ...(item.reply?.subtitle ? [`  引用：${item.reply.subtitle}`] : []),
  ])].join("\n");

const QUOTED_LABELS: Partial<Record<AppCard["kind"], string>> = {
  merged_forward: "聊天记录",
  file: "文件",
  link: "链接",
  quote: "引用",
  miniprogram: "小程序",
  channel: "视频号",
  location: "位置",
  business_card: "名片",
};

/**
 * One line for a quote whose target was itself a card.
 *
 * The stored subtitle flattens such a target to its bare title — "联系人C和联系人D"
 * for a forwarded record, a filename for a file — which reads as if someone
 * had typed those words. Saying what kind of thing it was is the difference.
 * Returns null when there is nothing better to say than the subtitle.
 */
export const quotedCardSummary = (quoted: AppCard | null | undefined): string | null => {
  if (quoted === null || quoted === undefined) return null;
  const label = QUOTED_LABELS[quoted.kind];
  if (label === undefined) return null;
  const title = quoted.title.trim() === "" ? label : quoted.title.trim();
  if (quoted.kind === "merged_forward") {
    return `[${label}] ${title} · ${String(quoted.item_total)}条`;
  }
  return `[${label}] ${title}`;
};

/**
 * The reply line for a quote card, when its target was a card of its own.
 *
 * The speaker is recovered from the stored subtitle rather than guessed: the
 * archive writes it as `<speaker>：<the quoted card's title>`, so removing that
 * exact suffix is the one split that cannot cut a name out of the middle of
 * someone's sentence.
 */
export const quotedReplyPreview = (
  card: AppCard,
): { senderName: string; snippet: string; media: null } | null => {
  const snippet = quotedCardSummary(card.quoted);
  if (snippet === null) return null;
  const subtitle = card.subtitle?.trim() ?? "";
  const title = card.quoted?.title.trim() ?? "";
  const suffix = `：${title}`;
  const speaker =
    title !== "" && subtitle.endsWith(suffix) ? subtitle.slice(0, -suffix.length) : "";
  // `media: null` 不是占位：这一条是从卡片本身读出来的摘要，卡片里只有文字，
  // 被引消息的附件要由 `reply_preview` 那一路带过来。
  return { senderName: speaker === "" ? "原消息" : speaker, snippet, media: null };
};

/**
 * 记录里的条目，按**屏幕上的顺序**摊平——嵌套记录的条目排在那条 record 自己
 * 的位置上，正是 MergedForwardItems 递归渲染出来的次序。
 *
 * 图库和文件预览都要照这个顺序找条目：嵌套记录里的媒体现在也会画成可点的按钮
 * （后端开始给它们发 attachment_id 了），只遍历顶层的话，那一下点击会静静地什么
 * 都不做——比不给按钮更糟。
 */
export const mergedForwardFlatItems = (card: AppCard): AppCardItem[] => {
  const flattened: AppCardItem[] = [];
  const walk = (items: readonly AppCardItem[]): void => {
    for (const item of items) {
      if (item.nested !== null) walk(item.nested.items);
      flattened.push(item);
    }
  };
  walk(card.items);
  return flattened;
};

/**
 * WeChat's built-in text emoticons, rendered as their closest standard emoji.
 *
 * These are not sticker attachments: WeChat writes them into the message body
 * as literal text like `[OK]`, and its own client draws an image in their
 * place. Nothing has to be fetched, decrypted, or stored -- only substituted at
 * display time.
 *
 * Two rules keep this honest:
 *
 *  1. Only names on this list are ever replaced. Real archived text is full of
 *     bracketed phrases that are NOT emoticons -- one account had `[执行实体变更]`
 *     108 times, plus `[编译后端源码]`, `[ERROR]`, `[dwsSap]` and `[图片]` -- and a
 *     pattern-based replacement would silently mangle them.
 *  2. The substitution is an approximation of Tencent's artwork, never a copy of
 *     it, so callers keep the original name on the rendered element. The exact
 *     text WeChat stored stays visible on hover and to screen readers, and the
 *     archived `content_text` is never rewritten.
 *
 * Names absent from this list render as the plain text WeChat stored, which is
 * also what WeChat itself falls back to when a client does not know an
 * emoticon.
 */
export const WECHAT_EMOTICONS: Readonly<Record<string, string>> = Object.freeze({
  // Faces
  微笑: "🙂",
  撇嘴: "😖",
  色: "😍",
  发呆: "😳",
  得意: "😎",
  流泪: "😭",
  害羞: "☺️",
  闭嘴: "🤐",
  睡: "😴",
  大哭: "😣",
  尴尬: "😰",
  发怒: "😡",
  调皮: "😜",
  呲牙: "😁",
  惊讶: "😲",
  难过: "☹️",
  囧: "😲",
  抓狂: "😫",
  吐: "🤮",
  偷笑: "🤭",
  愉快: "☺️",
  白眼: "🙄",
  傲慢: "😤",
  困: "😪",
  惊恐: "😨",
  憨笑: "😄",
  悠闲: "😌",
  咒骂: "🤬",
  嘘: "🤫",
  晕: "😵",
  衰: "😩",
  擦汗: "😓",
  抠鼻: "🤏",
  坏笑: "😏",
  鄙视: "😒",
  委屈: "😣",
  快哭了: "😢",
  阴险: "😏",
  亲亲: "😘",
  可怜: "😟",
  笑脸: "😁",
  生病: "😷",
  脸红: "😊",
  破涕为笑: "😂",
  恐惧: "😨",
  失望: "😔",
  无语: "😑",
  嘿哈: "😆",
  捂脸: "🤦",
  奸笑: "😏",
  机智: "🤓",
  皱眉: "😟",
  哈欠: "🥱",
  打脸: "😳",
  哇: "😮",
  翻白眼: "🙄",
  叹气: "😮‍💨",
  苦涩: "😖",
  裂开: "🫠",
  旺柴: "🐶",
  社会社会: "🕺",
  Emm: "🤔",
  天啊: "😱",
  汗: "😓",
  发抖: "🥶",
  怄火: "😡",
  骷髅: "💀",

  // Gestures
  OK: "👌",
  好的: "👌",
  强: "👍",
  弱: "👎",
  握手: "🤝",
  胜利: "✌️",
  耶: "✌️",
  抱拳: "🙏",
  合十: "🙏",
  勾引: "👈",
  拳头: "✊",
  鼓掌: "👏",
  再见: "👋",
  加油: "💪",
  让我看看: "👀",
  疑问: "❓",
  "666": "6️⃣",

  // Objects
  嘴唇: "💋",
  爱心: "❤️",
  心碎: "💔",
  拥抱: "🤗",
  玫瑰: "🌹",
  凋谢: "🥀",
  鲜花: "💐",
  蛋糕: "🎂",
  西瓜: "🍉",
  吃瓜: "🍉",
  啤酒: "🍺",
  咖啡: "☕",
  猪头: "🐷",
  菜刀: "🔪",
  炸弹: "💣",
  便便: "💩",
  月亮: "🌙",
  太阳: "☀️",
  礼物: "🎁",
  红包: "🧧",
  微信红包: "🧧",
  庆祝: "🎉",
  烟花: "🎆",
  爆竹: "🧨",
  敲打: "🔨",
  转圈: "💃",
  跳跳: "💃",
});

export interface EmoticonSegment {
  readonly kind: "text" | "emoticon";
  /** For an emoticon this is the emoji; for text it is the text itself. */
  readonly value: string;
  /** The exact source token, brackets included. Only set for emoticons. */
  readonly token?: string;
}

// Bracketed runs only: no nesting, no line breaks, and short enough that a
// stray bracket in prose cannot swallow a paragraph.
const CANDIDATE = /\[([^[\]\n]{1,10})\]/g;

/**
 * Split message text into literal runs and recognised emoticons.
 *
 * An unrecognised bracketed phrase stays part of the surrounding text, so
 * `[执行实体变更]` survives verbatim.
 */
export function splitTextWithEmoticons(text: string): EmoticonSegment[] {
  const segments: EmoticonSegment[] = [];
  let cursor = 0;
  let pending = "";

  for (const match of text.matchAll(CANDIDATE)) {
    const emoji = WECHAT_EMOTICONS[match[1] ?? ""];
    if (emoji === undefined) continue;
    const start = match.index;
    pending += text.slice(cursor, start);
    if (pending !== "") {
      segments.push({ kind: "text", value: pending });
      pending = "";
    }
    segments.push({ kind: "emoticon", value: emoji, token: match[0] });
    cursor = start + match[0].length;
  }

  const tail = pending + text.slice(cursor);
  if (tail !== "" || segments.length === 0) {
    segments.push({ kind: "text", value: tail });
  }
  return segments;
}

/** Whether the text contains at least one recognised emoticon. */
export function hasEmoticon(text: string): boolean {
  return splitTextWithEmoticons(text).some((segment) => segment.kind === "emoticon");
}

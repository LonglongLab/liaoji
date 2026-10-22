import type { MessageSummary } from "@/api/archiveApi";

type AppCard = NonNullable<MessageSummary["app_card"]>;

/**
 * 归档写进 `content_text` 的那句预览串，反过来读。
 *
 * 后端 `domain/preview` 是写的那一侧，这里是读的那一侧。之所以要读它：附件的
 * 名字与时长**只挂在本机的附件行上**，而远端的 `attachment_refs` 是一列无类型
 * 的 blob 标识；这句正文却是两端都有的——它跟着记录本身走。
 *
 * 只认锚定的完整格式。正文里恰好含「语音 3s」几个字的散文不是时长，
 * 「[文件] 」开头才是文件名：猜错一个名字比老实说不知道更糟。
 */

/** 文件消息的正文前缀，两端同一份。 */
export const FILE_PREVIEW_PREFIX = "[文件] ";

const VOICE_PREVIEW = /^\[语音 (\d+)s\]$/;

/**
 * 这条记录里的文件名。先卡片后正文：`title` 是那个字段本来的用途，正文是给人
 * 看的预览串。两个都答不上来就返回 null——「未命名文件」是界面对「这一端没被
 * 告知」的说法，不该被一个占位串顶掉。
 */
export const fileNameFromRecord = (
  contentText: string,
  card: AppCard | null,
): string | null => {
  const title = card?.kind === "file" ? card.title.trim() : "";
  if (title !== "") return title;
  if (!contentText.startsWith(FILE_PREVIEW_PREFIX)) return null;
  return contentText.slice(FILE_PREVIEW_PREFIX.length).trim() || null;
};

/** 这条语音有多长，毫秒。正文只给到秒，所以这里也只到秒。 */
export const voiceDurationMsFromRecord = (contentText: string): number | null => {
  const matched = VOICE_PREVIEW.exec(contentText);
  if (matched?.[1] === undefined) return null;
  const seconds = Number(matched[1]);
  return Number.isSafeInteger(seconds) && seconds >= 0 ? seconds * 1000 : null;
};

/**
 * 语音时长怎么写成一行字：秒数向下取整，但不小于 1 秒。
 *
 * 这是**微信自己的口径**：归档正文里那句 `[语音 Ns]` 就是这么算的——真机
 * 24,938 条语音，`max(1, floor(ms/1000))` 与上游写下的秒数逐条吻合，一条不差
 * （其中 21 条不足一秒的，上游同样写 `1s` 而不是 `0s`）。
 *
 * 两端共用这一条。本机手上是毫秒，远端手上只有正文里那个秒数——先前本机走
 * `Math.round`，同一条 3.7 秒的语音本机印 0:04、远端印 0:03，看着像两条不同
 * 的记录。**放在这里而不是各写各的**：同一条规则散成两份，迟早各自演化。
 *
 * 真的是 0 就印 0:00：那是「这段音频没有长度」，不是「短得不够一秒」。
 */
export const formatVoiceDuration = (seconds: number | null): string => {
  if (seconds === null || !Number.isFinite(seconds) || seconds < 0) return "--:--";
  const total = seconds === 0 ? 0 : Math.max(1, Math.floor(seconds));
  return `${String(Math.floor(total / 60))}:${String(total % 60).padStart(2, "0")}`;
};

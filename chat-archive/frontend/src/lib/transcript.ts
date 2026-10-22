import type {
  AttachmentSummary,
  ConversationDetail,
  ConversationMember,
  ConversationSummary,
  MessageSummary,
} from "@/api/archiveApi";
import { formatWechatTimestamp, isSameLocalDay } from "@/utils/time";

const LONG_GAP_SECONDS = 5 * 60;

/**
 * 转录组件消费的消息形状：本地 `MessageSummary` 的结构超集。
 *
 * 远端记录比本地少三样，而「少」必须保留成类型的一部分，不能在适配时补一个
 * 貌似合理的值：`is_self: null` 是「归属没被告知」，补成 false 就是替远端断言
 * 「这是对方发的」；`sender_uid: null` 同理；`source_message_type` 只在远端送来
 * 界面认不得的类型字符串时出现，本地数据永远不带。
 * 本地 `MessageSummary` 可以原样赋值成这个类型，所有本地调用零改动。
 */
export type RenderMessage = Omit<MessageSummary, "sender_uid" | "is_self"> & {
  sender_uid: string | null;
  is_self: boolean | null;
  /**
   * 仅远端记录携带：`string` = 远端给了界面认不得的类型，`null` = 远端没被
   * 告知类型（v1 形状）。两种情况下正文都是对方解析器认真产出的，照常展示；
   * 本地消息不带这个键，本地的 unknown 仍然只给占位说明。
   */
  source_message_type?: string | null;
};

/**
 * 会话组件消费的会话形状：本地 `ConversationSummary` 的结构超集。
 * `is_group: null` 是「远端没被告知」，界面据此既不标「群聊」也不标「联系人」；
 * `media_count: null` 表示这一端根本不统计，而不是 0 个。
 */
export type RenderConversation = Omit<ConversationSummary, "is_group" | "media_count"> & {
  is_group: boolean | null;
  media_count: number | null;
};

/**
 * 会话信息抽屉消费的详情形状：本地 `ConversationDetail` 的结构超集（PSU-2.47）。
 *
 * 三处放宽，三条各自的理由：
 * - `media_count: null` —— 远程归档**不按类型统计附件**，不是「0 个媒体」；
 * - `member_count: null` —— **没有名单时是 null 不是 0**，0 会声称这个群是空的，
 *   而没有群是空的；
 * - `is_group: null` —— 「没被告知」，界面既不标群聊也不标单聊。
 *
 * `contact` 在这一端恒为 null：联系人的备注、签名、来源从来没有上行过。
 */
export type RenderConversationDetail = Omit<
  ConversationDetail,
  "is_group" | "media_count" | "member_count"
> & {
  is_group: boolean | null;
  media_count: number | null;
  member_count: number | null;
};

/**
 * 成员名单一行的形状：本地 `ConversationMember` 的结构超集。
 *
 * `is_unknown: null` 是「没被告知」而不是「这个人是认得的」——远端的通讯录里
 * 没有这一位；`contact_id` 同理。
 */
export type RenderMember = Omit<ConversationMember, "contact_id" | "is_unknown"> & {
  contact_id: string | null;
  is_unknown: boolean | null;
};

/**
 * 从**已读到的消息**里把一个源标识接回一个名字：认得就给名字，认不得就 null。
 *
 * 群转账的付款方/收款方在卡片上只有标识，名字得另外找。不走群成员名单，两条
 * 理由，都是量出来的：
 *
 *   * 本机这一端**有意不外发源标识**（`ConversationMember` 只给一个按会话加盐
 *     算出来的不透明 `id`，旁边还钉着 `assert "contact_uid" not in ...`）。要
 *     靠名单，就得先推翻那条政策。
 *   * 不必推翻。真机上 166 条群转账、两端合计 332 个人次：43 个就是我，289 个
 *     **在同一条会话里发过言**（269 个有真名，20 个全档案只剩 wxid——那 20 个
 *     名单同样救不了）。**一个都不落在名单才有、消息里没有的那一格里。**
 *
 * 于是这一份两端通用、零额外请求：转录里每条消息都带着 `sender_uid` 与
 * `sender_display_name`，转账的对面本来就是其中一条的发送者。
 *
 * 名字是空串时返回 null 而不是空串：空串在界面上是「这里什么都没有」，而实情是
 * 「这一位只留下了标识」，由调用方退回 `成员 #XXXXXX` 说清楚。
 */
export const senderNameLookup = (
  messages: Iterable<RenderMessage>,
): ((uid: string) => string | null) => {
  const byUid = new Map<string, string>();
  for (const message of messages) {
    const uid = message.sender_uid;
    if (uid === null || uid === "") continue;
    if (message.is_self) {
      byUid.set(uid, "我");
      continue;
    }
    if (byUid.has(uid)) continue;
    const label = (message.sender_display_name ?? "").trim();
    // 名字就等于标识时不算认得——那是上游没名字，退回 `成员 #XXXXXX` 更诚实。
    if (label !== "" && label !== uid) byUid.set(uid, label);
  }
  return (uid: string): string | null => byUid.get(uid) ?? null;
};

export interface TranscriptRow {
  message: RenderMessage;
  dateLabel: string | null;
}

export interface ReplyPreview {
  senderName: string;
  snippet: string;
  /**
   * 被引的那条消息里、能在引用条上直接画出来的那一件媒体；没有就是 null。
   *
   * 微信在引用里只留一行占位文字（`[图片]`、`[语音]`），所以引用条上此前永远
   * 只有字——而被引的那条就在同一份档案里，附件也在。
   */
  media: AttachmentSummary | null;
}

/**
 * 引用条上画得出来的只有图片与表情：两者都能按缩略尺寸取到 KB 量级的字节。
 *
 * 视频不在其内**不是遗漏**：档案里没有给视频存缩略图，要画就得把整段视频取回来，
 * 而引用条只是一行注脚。语音与文件同理——它们没有画面，占位文字已经是实话。
 */
const QUOTED_MEDIA_KINDS: ReadonlySet<string> = new Set(["image", "emoji"]);

/**
 * 参数收 `undefined`：契约说 `ReplyPreview.attachments` 必给，但这一端可能正
 * 对着一个比它旧的后端跑（开发时把 vite 代理指到装好的那一版就是这样），那时
 * 它没有这个键，而在 `.find` 上炸掉的是整条转录。
 */
export const quotedMediaOf = (
  attachments: readonly AttachmentSummary[] | undefined,
): AttachmentSummary | null =>
  attachments?.find(
    (attachment) =>
      QUOTED_MEDIA_KINDS.has(attachment.kind) && attachment.content_available,
  ) ?? null;

/**
 * 把分页取回的转录拼成一条从旧到新的序列。
 *
 * 两端的转录分页是**同一个约定**：接口先给最新的那一片，片内按时间升序，
 * 游标往回要更早的一片。所以拼接就是「把页的顺序倒过来，页内顺序不动」。
 * 住在这里而不是某一端的 composable 里，是因为它属于那个约定本身——
 * 本机档案与远程归档只是同一件事的两个数据源（`docs/local-remote-boundary.md`
 * 第 3 节「两端共有」）。
 *
 * 泛型只约束到「有 items」：两端的页各带各的尾随游标字段，那不是这里的事。
 */
export const flattenMessagePages = <Item>(
  pages: readonly { items: Item[] }[],
): Item[] => pages.slice().reverse().flatMap((page) => page.items);

/**
 * 每条分隔都带日期口径，而不是只有跨日那条带。归档翻的是几个月前的记录，
 * 之前同日的长间隔只印「08:42」，读的人无从判断这是今天早上还是某年某月的早上。
 * 口径本身交给 formatWechatTimestamp，今天的消息仍然只显示时刻。
 */
export const buildTranscriptRows = (
  messages: RenderMessage[],
  nowMs: number = Date.now(),
): TranscriptRow[] =>
  messages.map((message, index) => {
    const previous = messages[index - 1];
    const isNewDay =
      previous === undefined || !isSameLocalDay(previous.created_at, message.created_at);
    const hasLongGap =
      previous !== undefined && message.created_at - previous.created_at >= LONG_GAP_SECONDS;
    return {
      message,
      dateLabel:
        isNewDay || hasLongGap ? formatWechatTimestamp(message.created_at, nowMs) : null,
    };
  });

export const snippetOf = (message: RenderMessage): string => {
  const text = message.content_text.trim();
  const label = text === "" ? "[非文本消息]" : text;
  return label.length > 40 ? `${label.slice(0, 40)}…` : label;
};

export const replyPreviewFor = (
  replyToId: string | null,
  messagesById: ReadonlyMap<string, RenderMessage>,
): ReplyPreview | null => {
  if (replyToId === null) {
    return null;
  }
  const target = messagesById.get(replyToId);
  if (target === undefined) {
    return null;
  }
  return {
    senderName: target.is_self
      ? "我"
      : target.sender_display_name ?? target.sender_uid ?? "发送者未上传",
    snippet: snippetOf(target),
    media: quotedMediaOf(target.attachments),
  };
};

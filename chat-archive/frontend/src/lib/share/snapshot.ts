import type {
  ShareDraftRequest,
  ShareSnapshot,
  ShareSnapshotAttachment,
  ShareSnapshotMessage,
} from "@/api/archiveApi";
import type { RenderMessage } from "@/lib/transcript";

/**
 * 在线分享的快照（PSU-2.182）：把选中的消息收成 2.181 规定的那份白名单形状。
 *
 * **隐藏名字在这里做掉**——服务器上从来没有真名。别人按首次出现的顺序换成
 * 「成员 A / B …」，自己是「我」；引用、合并转发卡片里出现的名字按同一张表换，
 * 这样同一个人在正文和引用里是同一个字母。文件名不动：那是文件自己的名字。
 *
 * 只收渲染要用的字段。`sender_uid`、会话 id、账号 id 一概不进——服务端还会按
 * 白名单再剥一遍，但这里不给，是第一道。
 */

export const SHARE_MESSAGE_LIMIT = 500;
export const SHARE_TITLE_LIMIT = 200;
/** 隐藏名字时的标题：单聊的会话名就是对方的名字，不能留着。 */
export const HIDDEN_TITLE = "聊天记录";
export const SELF_LABEL = "我";

export interface ShareSnapshotOptions {
  hideNames: boolean;
  conversationName: string;
}

/** 一个要带上的附件：分享里的编号、原附件 id 与已经取到的字节。 */
export interface CarriedAttachment {
  shareId: string;
  sourceId: string;
  blob: Blob;
}

export interface BuiltShare {
  draft: Omit<ShareDraftRequest, "expires_in_days">;
  carried: CarriedAttachment[];
}

/** 0 → A，25 → Z，26 → AA：人再多也不会撞名。 */
export const memberLetter = (index: number): string => {
  let value = index;
  let letters = "";
  do {
    letters = String.fromCharCode(65 + (value % 26)) + letters;
    value = Math.floor(value / 26) - 1;
  } while (value >= 0);
  return letters;
};

class NameMask {
  private readonly byKey = new Map<string, string>();

  constructor(private readonly hide: boolean) {}

  /** 消息的发送人：有 uid 按 uid 认，没有就按名字认。 */
  sender(message: RenderMessage): string | null {
    if (message.is_self === true) {
      return this.hide ? SELF_LABEL : (message.sender_display_name ?? SELF_LABEL);
    }
    const name = message.sender_display_name;
    if (!this.hide) return name;
    const uidKey = message.sender_uid === null ? null : `uid:${message.sender_uid}`;
    const nameKey = name === null ? null : `name:${name}`;
    if (uidKey === null && nameKey === null) return null;
    // 同一个人可能先在引用里出现（只有名字）、后来才发言（有 uid）：两把钥匙
    // 认同一个字母，谁先见到就用谁的。
    const label =
      (uidKey === null ? undefined : this.byKey.get(uidKey)) ??
      (nameKey === null ? undefined : this.byKey.get(nameKey)) ??
      this.labelFor((uidKey ?? nameKey) as string);
    if (uidKey !== null) this.byKey.set(uidKey, label);
    if (nameKey !== null) this.byKey.set(nameKey, label);
    return label;
  }

  /** 引用、卡片条目里只有显示名。 */
  name(name: string | null): string | null {
    if (name === null || !this.hide) return name;
    return this.labelFor(`name:${name}`);
  }

  private labelFor(key: string): string {
    const known = this.byKey.get(key);
    if (known !== undefined) return known;
    const label = `成员 ${memberLetter(this.countMembers())}`;
    this.byKey.set(key, label);
    return label;
  }

  private countMembers(): number {
    return new Set(this.byKey.values()).size;
  }
}

const SNAPSHOT_KINDS = new Set<ShareSnapshotAttachment["kind"]>([
  "image",
  "emoji",
  "audio",
  "video",
  "file",
  "unknown",
]);

/**
 * 组快照。`fetched` 是已经取到字节的附件（原附件 id → 字节）；没取到的照样出现在
 * 快照里（对方页面据此画「没能带上」），只是不声明、不上传。
 */
export const buildShareSnapshot = (
  messages: readonly RenderMessage[],
  fetched: ReadonlyMap<string, Blob>,
  options: ShareSnapshotOptions,
): BuiltShare => {
  const mask = new NameMask(options.hideNames);
  const shareIds = new Map<string, string>();
  const carried: CarriedAttachment[] = [];
  const shareIdFor = (sourceId: string): string => {
    const known = shareIds.get(sourceId);
    if (known !== undefined) return known;
    const next = `a${String(shareIds.size + 1)}`;
    shareIds.set(sourceId, next);
    const blob = fetched.get(sourceId);
    if (blob !== undefined) carried.push({ shareId: next, sourceId, blob });
    return next;
  };

  const snapshotMessages: ShareSnapshotMessage[] = messages.map((message) => {
    const sender = mask.sender(message);
    const preview = message.reply_preview;
    const card = message.app_card;
    return {
      time: message.created_at,
      sender,
      is_self: message.is_self,
      type: message.message_type,
      text: message.content_text,
      quote:
        preview === null
          ? null
          : {
              sender: mask.name(preview.sender_display_name),
              type: preview.message_type,
              text: preview.content_text,
            },
      card:
        card === null
          ? null
          : {
              kind: card.kind,
              title: card.title,
              subtitle: card.subtitle,
              source_name: card.source_name,
              url: card.url,
              items: card.items.slice(0, 50).map((item) => ({
                sender: mask.name(item.sender),
                kind: item.kind,
                text: item.text,
              })),
            },
      attachments: message.attachments
        .filter((attachment) => SNAPSHOT_KINDS.has(attachment.kind))
        .map((attachment) => ({
          attachment_id: shareIdFor(attachment.id),
          kind: attachment.kind,
          size: fetched.get(attachment.id)?.size ?? attachment.size,
          duration_ms: attachment.duration_ms ?? null,
          file_name: attachment.file_name,
          mime_type: fetched.get(attachment.id)?.type || attachment.mime_type,
        })),
      transcript:
        message.transcript !== null && message.transcript.status === "ok"
          ? message.transcript.text
          : null,
    };
  });

  const snapshot: ShareSnapshot = {
    version: 1,
    names_hidden: options.hideNames,
    messages: snapshotMessages,
  };
  const rawTitle = options.hideNames ? HIDDEN_TITLE : options.conversationName.trim();
  return {
    draft: {
      title: (rawTitle || HIDDEN_TITLE).slice(0, SHARE_TITLE_LIMIT),
      snapshot,
      attachments: carried.map((item) => ({
        attachment_id: item.shareId,
        size: item.blob.size,
        mime_type: item.blob.type || null,
      })),
    },
    carried,
  };
};

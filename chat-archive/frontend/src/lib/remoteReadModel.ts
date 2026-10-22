import type {
  AttachmentSummary,
  MessageSummary,
  MessageType,
  RemoteAttachmentUrl,
  RemoteThumbnailUrl,
  RemoteConversationSummary,
  RemoteMessage,
  SearchHit,
} from "@/api/archiveApi";
import { fileNameFromRecord, voiceDurationMsFromRecord } from "@/lib/recordPreview";
import type { ShareLoaders } from "@/lib/share/runShare";
import type { RenderConversation, RenderMessage } from "@/lib/transcript";
import { displayableImage, isWxgfUndecodable } from "@/lib/wxgf/toBlob";

type AppCard = NonNullable<MessageSummary["app_card"]>;

/**
 * 远端记录 → 本地渲染形状的适配层，集中一处。
 *
 * 契约里那些字段可以是 `null`，而 **`null` 是「这个远端没被告知过」，不是「空」**。
 * 适配的规矩与旧远程渲染模型一脉相承：缺什么就保留成缺（`RenderMessage` 的可空
 * 字段为此而生），不许补一个貌似合理的值——补 `is_self: false` 就是替远端断言
 * 「这是对方发的」。
 */

/** 界面认得、能按类型渲染的消息类型。远端的 `message_type` 是自由字符串。 */
const KNOWN_TYPES = new Set<string>([
  "text",
  "image",
  "audio",
  "video",
  "file",
  "link",
  "quote",
  "emoji",
  "app",
  "system",
  "unknown",
]);

/**
 * `app_card_json` 是本地 `AppCard` 的同一份序列化（服务端 `card_to_json`），
 * 读回来时对齐 `card_from_json` 的容错语义：不是对象、`kind` 认不得，一律当
 * 没有卡片，绝不让一条脏记录把整页转录炸掉。
 */
const APP_CARD_KINDS = new Set<AppCard["kind"]>([
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

const ATTACHMENT_KIND_BY_TYPE: Partial<Record<string, AttachmentSummary["kind"]>> = {
  image: "image",
  video: "video",
  audio: "audio",
  file: "file",
  emoji: "emoji",
};

/**
 * attachment-view 能力不可用时挂在附件上的 failure code。
 * `MessageTypeRenderer` 的 `failureLabels`/`statusLabel` 认得它并给出准确文案；
 * 它刻意不落在可重试的 cache_status 上——重试也变不出这一端没有的能力。
 */
export const REMOTE_ATTACHMENT_UNAVAILABLE_CODE = "REMOTE_ATTACHMENT_VIEW_UNAVAILABLE";

export interface RemoteRenderOptions {
  /** 能力清单说了算（PSU-2.28 的规矩），不由「有没有 ref」推断。 */
  attachmentsAvailable: boolean;
  /**
   * blob 标识 → 已经取回来的 `blob:` 地址（PSU-2.47）。
   *
   * 传进来而不是在这里取：这一层是纯函数，取字节是异步的、有生命周期的，
   * 而 objectURL 是必须显式回收的引用——把它塞进一个「把数据换个形状」的
   * 函数里，就没有任何一处能负责回收了。
   *
   * 不给（或答 null）就是退回首字母。那**不是**「这个人没有头像」，而是
   * 「这一端还没有这张图」，ⓘ 里由界面说清楚。
   */
  avatarSrc?: (blobId: string | null | undefined) => string | null;
  /**
   * 这个 blob 的**原图**在远端取不取得到（PSU-2.43）。
   *
   * 只有明确答 `false` 才标「仅缩略图」。`undefined`（还没问过）与 `null`
   * （问了但没问出来）都是**未知**,未知时一个字都不声称——把它们当成 `false`
   * 就等于因为一次抖动、或者仅仅因为还没滚动到,替归档宣布一张图永久丢了。
   *
   * 与 `avatarSrc` 同一种接法:这一层是纯函数,答案是异步到达的,所以由视图
   * 持有那份响应式的表,这里只读。
   */
  originalAvailable?: (blobId: string) => boolean | null | undefined;
}

const emptyToNull = (value: string | null): string | null => (value === "" ? null : value);

const knownTypeOf = (value: string | null): MessageType | null =>
  value !== null && KNOWN_TYPES.has(value) ? (value as MessageType) : null;

export const parseRemoteAppCard = (raw: string | null): AppCard | null => {
  if (raw === null) return null;
  let decoded: unknown;
  try {
    decoded = JSON.parse(raw);
  } catch {
    return null;
  }
  if (typeof decoded !== "object" || decoded === null || Array.isArray(decoded)) return null;
  const kind = (decoded as { kind?: unknown }).kind;
  if (typeof kind !== "string" || !APP_CARD_KINDS.has(kind as AppCard["kind"])) return null;
  return decoded as AppCard;
};

/**
 * 远端记录里的文件名与语音时长，都从归档写进正文的那句预览串读回来。
 *
 * 契约里 `attachment_refs` 是一列**无类型**的 blob 标识：名字、时长、大小都不
 * 在那里。名字与时长却一直在这条记录上——文件卡的 `title`、正文的
 * `[文件] <名字>` 与 `[语音 Ns]`（真机 6,288 条文件、24,938 条语音，无一例外）。
 * 此前这两个字段在这里硬写 null / 干脆不写，后果是：每个文件都成了「未命名
 * 文件」，连带图标、类型和**能不能预览**一起丢（它们全从扩展名推）；每条语音
 * 都印「--:--」，直到点下播放为止。
 *
 * `duration_ms` 在 `AttachmentSummary` 里是**可选的**，所以当初漏掉它时 TS 一个
 * 字都没说——与 `sender_avatar_ref` 那一处是同一个陷阱。
 *
 * 规则本身在 `lib/recordPreview`：气泡上那张「附件待定位」卡要读同一句正文，
 * 两处各写一份正则迟早各自演化。
 */

const attachmentsOf = (
  message: Pick<RemoteMessage, "record_key" | "message_type" | "content_text" | "attachment_refs">,
  options: RemoteRenderOptions,
  card: AppCard | null,
): AttachmentSummary[] => {
  if (card?.kind === "merged_forward") {
    const byBlob = new Map<string, AttachmentSummary>();
    const visit = (current: AppCard, depth = 0): void => {
      if (depth > 6) return;
      for (const item of current.items) {
        if (item.nested !== null) visit(item.nested, depth + 1);
        const media = item.remote_media;
        if (!media || item.attachment_id !== media.blob_id || !message.attachment_refs.includes(media.blob_id)) continue;
        if (!(item.kind === "image" || item.kind === "video" || item.kind === "file" || item.kind === "sticker")) continue;
        byBlob.set(media.blob_id, {
          id: media.blob_id, message_id: message.record_key, kind: item.kind === "sticker" ? "emoji" : item.kind,
          file_name: item.file_name, size: media.size, mime_type: null, duration_ms: null,
          cache_status: options.attachmentsAvailable ? "ready" : "unsupported",
          failure_code: options.attachmentsAvailable ? null : REMOTE_ATTACHMENT_UNAVAILABLE_CODE,
          attempt_count: 0, content_available: options.attachmentsAvailable,
          variant: media.variant,
        });
      }
    };
    visit(card);
    return [...byBlob.values()];
  }
  const fileName = fileNameFromRecord(message.content_text, card);
  const durationMs = voiceDurationMsFromRecord(message.content_text);
  return message.attachment_refs.map((ref) => ({
    id: ref,
    message_id: message.record_key,
    // 类型说这是图片消息，它的附件就是那张图——这是记录自己的断言，不是猜。
    // 远端没有 mime/size，那两个就保持 null：文件卡少一行大小，而不是编一个。
    kind: ATTACHMENT_KIND_BY_TYPE[message.message_type ?? ""] ?? "unknown",
    file_name: fileName,
    mime_type: null,
    size: null,
    duration_ms: durationMs,
    cache_status: options.attachmentsAvailable ? "ready" : "unsupported",
    failure_code: options.attachmentsAvailable ? null : REMOTE_ATTACHMENT_UNAVAILABLE_CODE,
    attempt_count: 0,
    // **不因为「只有缩略图」就写 false。** 那个值管的是「这一端能不能取附件」,
    // 而它同时守着三处渲染:点开的入口、预览位、状态标签。把「原图没有」塞进来
    // 会把本单元要显示的那张缩略图一起关掉——一张能显示的图变成一行灰字。
    content_available: options.attachmentsAvailable,
    // 「原图不在归档里」在本机早就有一种说法,就是这个值——`MessageTypeRenderer`
    // 与 `MediaViewer` 都认得它并打上「仅缩略图」。远端复用同一条,而不是新造
    // 一套文案。未知时留 null:不知道就不说。
    variant: options.originalAvailable?.(ref) === false ? "thumbnail" : null,
  }));
};

/**
 * 远端那三样 → 本机 `VoiceTranscript` 的完整形状。
 *
 * `status: "ok"` 与 `failure_code: null` 在这里补上，不是编的：远端只收得下
 * 转成功的转写（发送方只挑 `status === "ok"`，端点又挡掉空文本），所以
 * **「有这个对象」本身就是那个状态**。补在这一层而不是让远端多存两个恒定值，
 * 是因为这一层本来就为「换形状」而存在，而恒定值存进库里会有一天和事实不符。
 *
 * 键缺席（远端到不了 v13）与 null（不是语音 / 还没发过来）都落到 null：
 * 界面对这三种情况的动作是同一个——不画那一行。
 */
const remoteTranscript = (
  value: RemoteMessage["transcript"],
): MessageSummary["transcript"] => {
  if (!value) {
    return null;
  }
  return {
    text: value.text,
    status: "ok",
    engine: value.engine,
    low_confidence: value.low_confidence,
    failure_code: null,
  };
};

export const connectRemoteAppCard = (card: AppCard | null, attachmentRefs: readonly string[]): AppCard | null => {
  const connect = (value: AppCard | null, depth = 0): AppCard | null => {
    if (value === null || depth > 6) return value;
    return {
      ...value,
      items: value.items.map((item) => {
        const media = item.remote_media;
        const valid = media && typeof media.blob_id === "string" && /^[A-Za-z0-9_.:-]{1,128}$/.test(media.blob_id)
          && ["original", "thumbnail"].includes(media.variant)
          && Number.isSafeInteger(media.size) && media.size >= 0 && /^[0-9a-f]{64}$/.test(media.sha256)
          && attachmentRefs.includes(media.blob_id);
        return {...item, attachment_id: valid ? media.blob_id : item.attachment_id,
          remote_media: valid ? media : null, nested: connect(item.nested, depth + 1)};
      }),
    };
  };
  return connect(card);
};

export const remoteMessageToRender = (
  message: RemoteMessage,
  options: RemoteRenderOptions,
): RenderMessage => {
  const known = knownTypeOf(message.message_type);
  const avatar = options.avatarSrc?.(message.sender_avatar_blob_id) ?? null;
  // 一条记录解一次卡片：附件那一侧要从里面取文件名，渲染那一侧要卡片本身。
  const card = connectRemoteAppCard(parseRemoteAppCard(message.app_card_json), message.attachment_refs);
  const preview = message.reply_preview;
  const replyPreview: MessageSummary["reply_preview"] =
    preview && preview.record_key === message.reply_to_key
      ? {
          message_id: preview.record_key,
          sender_display_name: preview.sender_display_name,
          content_text: preview.content_text,
          message_type: knownTypeOf(preview.message_type) ?? "unknown",
          // The excerpt is not the full file name or voice metadata.
          attachments: attachmentsOf({ ...preview, content_text: "" }, options, null),
        }
      : null;
  return {
    transcript: remoteTranscript(message.transcript),
    id: message.record_key,
    conversation_id: message.conversation_id,
    sender_uid: emptyToNull(message.sender_uid),
    sender_display_name: emptyToNull(message.sender_display_name),
    created_at: message.created_at,
    message_type: known ?? "unknown",
    content_text: message.content_text,
    is_self: message.is_self,
    reply_to_id: emptyToNull(message.reply_to_key),
    parse_status: "parsed",
    attachments: attachmentsOf(message, options, card),
    reply_preview: replyPreview,
    app_card: card,
    // 头像**取不到就不写这个键**，而不是写 null：`sender_avatar_ref` 在本机
    // 形状里是可选的，写 null 与不写在渲染上同样退回首字母，但不写留住了
    // 「这一端根本没有这份数据」这条事实（远端到不了 v8 时就是这样）。
    ...(avatar === null ? {} : { sender_avatar_ref: avatar }),
    ...(known === null ? { source_message_type: message.message_type } : {}),
  };
};

/**
 * 会话列表这一行的标题。名字没送到就如实用不透明标识。
 *
 * `remembered` 是跨过滤与翻页留下来的记忆表（PSU-2.44）：这一份缓存里没有这条
 * 会话，不代表它没有名字——上一份缓存里刚见过。记忆表只记**真的送到过的**
 * 名字，所以查得到就用得上，查不到才落占位。
 */
export const remoteConversationLabel = (
  item: RemoteConversationSummary,
  remembered: Readonly<Record<string, string>> = {},
): string => {
  if (item.conversation_id === "") return "未归类记录";
  if (item.display_name !== null && item.display_name !== "") return item.display_name;
  return remembered[item.conversation_id] ?? `会话标识：${item.conversation_id}`;
};

export const remoteConversationToRender = (
  item: RemoteConversationSummary,
  remembered: Readonly<Record<string, string>> = {},
  avatarSrc?: (blobId: string | null | undefined) => string | null,
): RenderConversation => ({
  id: item.conversation_id,
  display_name: remoteConversationLabel(item, remembered),
  is_group: item.is_group,
  last_message_at: item.last_created_at > 0 ? item.last_created_at : null,
  // 远端到不了 v6 时这两个键在契约上仍然在、值为 null——那与「这条会话没有
  // 可预览的消息」在界面上是同一句话（退回「N 条消息」），所以不必分开。
  // 类型过一道 `knownTypeOf`：远端的 `message_type` 是自由字符串，界面认不得
  // 的一律记成「没被告知」，而**那一句正文照样显示**——预览是归档侧算好的，
  // 不靠这里认得类型。
  last_message_type: knownTypeOf(item.last_message_type ?? null),
  last_message_preview: item.last_message_preview ?? null,
  message_count: item.message_count,
  // 远程归档没有按类型分的附件计数。保持 null，让「N 个媒体」那一处不出现，
  // 而不是印一个 0 冒充「一个都没有」。
  media_count: null,
  // 与消息同一条规矩：取不到就不写这个键。`avatar_ref` 在本机形状里是可选的，
  // **所以 TS 不会替你报缺**——这一行是这个单元里最容易漏掉的一行。
  ...(() => {
    const src = avatarSrc?.(item.avatar_blob_id) ?? null;
    return src === null ? {} : { avatar_ref: src };
  })(),
});

/**
 * 会话行上**现在看得见的那几张**头像（PSU-2.122 在会话页写下，PSU-2.128 起搜索名录共用）。
 *
 * 一页 100 条会话、屏幕上十来条：按已加载的全部去取，每张两次往返，九成是为了
 * 没人看的行。`visible === null` 是「这一端没有 `IntersectionObserver`」——那就按
 * 全部算；当成空集会画出一整页首字。
 */
export const onScreenConversationAvatars = (
  conversations: readonly Pick<RemoteConversationSummary, "conversation_id" | "avatar_blob_id">[],
  visible: readonly string[] | null,
): (string | null)[] => {
  if (visible === null) return conversations.map((item) => item.avatar_blob_id);
  const onScreen = new Set(visible);
  return conversations
    .filter((item) => onScreen.has(item.conversation_id))
    .map((item) => item.avatar_blob_id);
};

/**
 * 转录页选中了一条既问不到详情、也不在已加载页里的会话时的最后一级：只有标识，
 * 群聊与否保持「没被告知」。`message_count: 0` 不会被转录渲染。
 *
 * 记忆表命中时名字还能救回来——那是这一端见过的**真名字**，不是猜的。
 * 全都落空才印不透明标识，并由 ⓘ 说明原因。
 */
export const remoteConversationPlaceholder = (
  conversationId: string,
  remembered: Readonly<Record<string, string>> = {},
): RenderConversation =>
  remoteConversationToRender(
    {
      conversation_id: conversationId,
      display_name: null,
      is_group: null,
      message_count: 0,
      last_created_at: 0,
      last_server_sequence: 0,
      last_message_type: null,
      last_message_preview: null,
      // 占位会话没有通讯录那一侧的任何东西。`member_count` 是 null 而不是 0：
      // 0 会声称「这个群是空的」，而这里连是不是群都不知道。
      avatar_blob_id: null,
      announcement: null,
      my_group_alias: null,
      member_count: null,
    },
    remembered,
  );

export const remoteHitToSearchHit = (
  message: RemoteMessage,
  options: RemoteRenderOptions,
): SearchHit => {
  const firstRef = message.attachment_refs[0] ?? null;
  const card = parseRemoteAppCard(message.app_card_json);
  return {
    message_id: message.record_key,
    conversation_id: message.conversation_id,
    // 远端契约没有这个概念；没有本地代码在远端路径上读它。
    source_account_id: "",
    // SearchHit 的 sender_uid 是必填 string；空串经 hitSenderName 显示为
    // 「发送者未上传」，不会被当成一个真实标识展示。
    sender_uid: message.sender_uid ?? "",
    sender_display_name: emptyToNull(message.sender_display_name),
    message_type: knownTypeOf(message.message_type) ?? "unknown",
    has_attachment: message.attachment_refs.length > 0,
    created_at: message.created_at,
    // 远端搜索没有服务端摘要；整段正文交给卡片自己截断与高亮。
    snippet: message.content_text,
    // 与会话页那一侧同一个答案（`attachmentsOf`）。没有附件就没有附件名——
    // 与本机形状一致，那一端的 `attachment_file_name` 来自附件行本身。
    attachment_file_name:
      firstRef === null ? null : fileNameFromRecord(message.content_text, card),
    // 只在远端明说 true 时记「我」；false 与 null 在搜索卡上都不站边。
    is_self: message.is_self === true,
    ...(() => {
      const src = options.avatarSrc?.(message.sender_avatar_blob_id) ?? null;
      return src === null ? {} : { sender_avatar_ref: src };
    })(),
    attachment_id: firstRef,
    attachment_kind:
      firstRef === null ? null : ATTACHMENT_KIND_BY_TYPE[message.message_type ?? ""] ?? "unknown",
    attachment_mime_type: null,
    attachment_size: null,
    attachment_cache_status:
      firstRef === null ? null : options.attachmentsAvailable ? "ready" : "unsupported",
    // 搜索命中这条路**另有一份**同样的硬写 null（会话页那处在 `attachmentsOf`）。
    // 只改一处,搜索结果里的「仅缩略图」就不见了,而两处看起来都是对的。
    attachment_variant:
      firstRef !== null && options.originalAvailable?.(firstRef) === false
        ? "thumbnail"
        : null,
    attachment_content_available: firstRef !== null && options.attachmentsAvailable,
  };
};

/**
 * 这一页里有多少条记录连归属都还没送到（v1 形状）。大于 0 时界面必须说出来：
 * 否则「这条没有发送者」会被读成「这条真的没有发送者」，而不是「还没传上来」。
 */
export const unattributedRemoteCount = (messages: readonly RemoteMessage[]): number =>
  messages.filter((item) => item.is_self === null && item.sender_uid === null).length;

/**
 * 把「blob id → 预签名 URL → 字节」折叠成本地组件唯一认得的
 * `loadAttachment(id) => Promise<Blob>`。
 *
 * 字节仍从对象存储 origin 直取、不经过 API（2.31 的「下行不经 API」不变；
 * CSP 的 connect-src 已折进存储 origin）。Blob 按 id 缓存——预签名 URL 是短时
 * 授权所以从不缓存，字节没有时效。失败不留缓存：下一次点重试要重新签一个 URL。
 */
/**
 * 一次取回：签名 → 直取对象存储。**不带缓存**——缓存的寿命由调用方决定。
 *
 * 抽出来是因为头像那条路（PSU-2.122）要的是另一种寿命：字节进全应用共用的
 * store 与 Cache Storage，而不是进一个随视图一起死的 Map。
 */
export const fetchRemoteAttachmentBytes = async (
  resolveUrl: (blobId: string) => Promise<RemoteAttachmentUrl>,
  blobId: string,
): Promise<Blob> => {
  const grant = await resolveUrl(blobId);
  const response = await fetch(grant.url);
  if (!response.ok) {
    throw new Error(`附件取回失败（HTTP ${String(response.status)}）`);
  }
  return response.blob();
};

export const remoteAttachmentLoader = (
  resolveUrl: (blobId: string) => Promise<RemoteAttachmentUrl>,
): ((blobId: string) => Promise<Blob>) => {
  const cache = new Map<string, Promise<Blob>>();
  return (blobId: string): Promise<Blob> => {
    const cached = cache.get(blobId);
    if (cached !== undefined) return cached;
    // wxgf 在这里解成 `<img>` 认得的东西（PSU-2.146），与本机加载器同一个函数。
    const loading = fetchRemoteAttachmentBytes(resolveUrl, blobId)
      .then(displayableImage)
      .catch((error: unknown) => {
        cache.delete(blobId);
        throw error;
      });
    cache.set(blobId, loading);
    return loading;
  };
};

/** 后端对「这条附件没有缩略图」的说法,两端同一个（PSU-2.42 / 2.43）。 */
export const THUMBNAIL_UNAVAILABLE_CODE = "THUMBNAIL_UNAVAILABLE";

/** 后端对「远端那里没有这个 blob」的说法。`/url` 拿不到原图时就是它。 */
const REMOTE_BLOB_ABSENT_CODE = "REMOTE_ARCHIVE_NOT_FOUND";

const codeOf = (error: unknown): string | null => {
  if (typeof error !== "object" || error === null) return null;
  const code = (error as { code?: unknown }).code;
  return typeof code === "string" ? code : null;
};

/**
 * 远端的缩略图 loader:与 `remoteAttachmentLoader` 同一条路,多两件事。
 *
 * 一、**否定缓存**。34% 的图片主工件本身就是缩略图,它们**永远**没有第二份;
 * 还有没上行的那些。不记住「这个 blob 没有缩略图」,虚拟滚动下每滚回来一次就
 * 再问一次,一屏图片多一屏 404。
 *
 * 二、**顺手记下原图在不在**。那个答案在同一个响应里——页面为了画气泡本来就
 * 要发这一条——所以「该不该标仅缩略图」不多花一次往返。三态原样传出去,由视图
 * 决定怎么存:`null` 是「问不出来」,与 `false` 不是一回事。
 */
export const remoteThumbnailLoader = (
  resolveUrl: (blobId: string) => Promise<RemoteThumbnailUrl>,
  onOriginalAvailable: (blobId: string, available: boolean | null) => void = () => {},
): ((blobId: string) => Promise<Blob>) => {
  const cache = new Map<string, Promise<Blob>>();
  const withoutThumbnail = new Set<string>();
  return (blobId: string): Promise<Blob> => {
    if (withoutThumbnail.has(blobId)) {
      // 已经问过了,答案不会变。这里不发请求,直接把同一句拒绝再说一遍——
      // **形状也要同一句**:`.code` 属性得在,否则同一个 blob 第一次拒绝
      // 带 code、第二次起只剩 message,`codeOf` 判它就静默拿到 null。
      return Promise.reject(
        Object.assign(new Error("该附件没有缩略图"), {
          code: THUMBNAIL_UNAVAILABLE_CODE,
        }),
      );
    }
    const cached = cache.get(blobId);
    if (cached !== undefined) return cached;
    const loading = (async (): Promise<Blob> => {
      const grant = await resolveUrl(blobId);
      onOriginalAvailable(blobId, grant.original_available);
      const response = await fetch(grant.url);
      if (!response.ok) {
        throw new Error(`缩略图取回失败（HTTP ${String(response.status)}）`);
      }
      // 少数 `_t.dat` 本身也是 wxgf（PSU-2.146）。
      return displayableImage(await response.blob());
    })().catch((error: unknown) => {
      cache.delete(blobId);
      if (codeOf(error) === THUMBNAIL_UNAVAILABLE_CODE) withoutThumbnail.add(blobId);
      throw error;
    });
    cache.set(blobId, loading);
    return loading;
  };
};

/**
 * 远端的「取这个附件的字节」:原图取得到就取原图,取不到就退到缩略图。
 *
 * 没有这一层的话,那 39,017 张原图早被本机配额淘汰的图片会是这样:气泡上有一张
 * 缩略图、旁边标着「仅缩略图」,点「查看」却弹出「暂时无法打开附件,请重试。」
 * ——对一个**永久**状态说请重试。退到缩略图之后,远端的行为与本机对
 * `variant='thumbnail'` 那些行的行为逐字相同:你看到的就是那张缩略图,而标签
 * 已经说清楚了它是什么。
 *
 * 先看已经知道的答案,不知道再试原图:省掉几万张图每次点开都先撞一次 404,
 * 但不依赖那份表一定填好——不知道时仍然问得出正确结果。
 */
export const remoteAttachmentWithThumbnailFallback = (
  loadOriginal: (blobId: string) => Promise<Blob>,
  loadThumbnail: (blobId: string) => Promise<Blob>,
  originalAvailable: (blobId: string) => boolean | null | undefined,
): ((blobId: string) => Promise<Blob>) => {
  return async (blobId: string): Promise<Blob> => {
    if (originalAvailable(blobId) === false) return loadThumbnail(blobId);
    try {
      return await loadOriginal(blobId);
    } catch (error: unknown) {
      // 原图是这个浏览器解不了的 wxgf 时（PSU-2.146）同样退：缩略图多半是 JPEG。
      if (codeOf(error) !== REMOTE_BLOB_ABSENT_CODE && !isWxgfUndecodable(error)) throw error;
      return loadThumbnail(blobId);
    }
  };
};

/**
 * 在线分享取字节（PSU-2.185）：与上面那几个同一条路，只是**不解 wxgf**——交出对象
 * 存储里存着的那份。远端有同样内容时，建分享就在对象存储里复制，不必再传；分享页
 * 自己解码。原图不在（或已知只有缩略图）时照样退缩略图，与界面上的行为一致。
 */
export const remoteShareLoaders = (
  resolveUrl: (blobId: string) => Promise<RemoteAttachmentUrl>,
  resolveThumbnailUrl: (blobId: string) => Promise<RemoteThumbnailUrl>,
  originalAvailable: (blobId: string) => boolean | null | undefined,
): ShareLoaders => {
  const loadThumbnail = (blobId: string): Promise<Blob> =>
    fetchRemoteAttachmentBytes(resolveThumbnailUrl, blobId);
  return {
    loadAttachment: remoteAttachmentWithThumbnailFallback(
      (blobId) => fetchRemoteAttachmentBytes(resolveUrl, blobId),
      loadThumbnail,
      originalAvailable,
    ),
    loadThumbnail,
  };
};

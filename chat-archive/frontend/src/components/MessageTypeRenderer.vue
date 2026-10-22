<script setup lang="ts">
import {
  computed,
  onBeforeUnmount,
  onMounted,
  reactive,
  ref,
  shallowReactive,
  watch,
  type Ref,
} from "vue";

import type { AttachmentSummary } from "@/api/archiveApi";
import AvatarBadge from "@/components/AvatarBadge.vue";
import EmoticonText from "@/components/EmoticonText.vue";
import FileAttachmentCard from "@/components/FileAttachmentCard.vue";
import LinkPreviewCard from "@/components/LinkPreviewCard.vue";
import MergedForwardCard from "@/components/MergedForwardCard.vue";
import TextMessageViewer from "@/components/TextMessageViewer.vue";
import SpeechToTextLine from "@/components/SpeechToTextLine.vue";
import VoiceMessagePlayer from "@/components/VoiceMessagePlayer.vue";
import WxgfSticker from "@/components/WxgfSticker.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import { ATTACHMENT_STATUS_LABELS, attachmentVariantWord } from "@/lib/attachmentStatus";
import { fileVisualOf } from "@/lib/fileKinds";
import { downloadBlob } from "@/lib/filePreview/download";
import { quotedReplyPreview } from "@/lib/mergedForward";
import { quotedMediaOf } from "@/lib/transcript";
import {
  fileNameFromRecord,
  formatVoiceDuration,
  voiceDurationMsFromRecord,
} from "@/lib/recordPreview";
import { REMOTE_ATTACHMENT_UNAVAILABLE_CODE } from "@/lib/remoteReadModel";
import { useWorkspace } from "@/lib/workspaceContext";
import type { RenderMessage, ReplyPreview } from "@/lib/transcript";
import { formatDateTimeSeparator, formatDateTimeWithSeconds } from "@/utils/time";
import { isWxgfAnimation, wxgfFirstFramePng } from "@/lib/wxgf/toBlob";

const props = withDefaults(defineProps<{
  message: RenderMessage;
  replyPreview: ReplyPreview | null;
  highlighted: boolean;
  loadAttachment: ((attachmentId: string) => Promise<Blob>) | undefined;
  /**
   * KB 量级的缩略图字节，气泡里的图片优先用它。取不到（没有缩略图、
   * 或这一端还没有这条能力）就静默回退 `loadAttachment`——回退即现状。
   */
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
  /**
   * Fetch an attachment whose bytes are not local yet. A sticker only exists
   * on Tencent's CDN, and an import deliberately leaves it there, so the first
   * view of the message is what pays for it.
   */
  resolveAttachment: ((attachmentId: string) => Promise<AttachmentSummary>) | undefined;
  /**
   * False once this server has answered that controlled CDN access is off, so
   * the remaining stickers stop asking and go back to saying so plainly.
   */
  stickerFetchAvailable?: boolean;
  showSenderName?: boolean;
  /**
   * 单聊里会话对面那个人的名字。**群聊传 null**——群里「对面」不是一个固定的
   * 人，转账的对面要靠卡片上的标识说。
   *
   * 转账详情用它说出「谁付的、谁收的」：单聊的转账，对面就是会话的对面，
   * 一个标识都不用查。
   */
  counterpartName?: string | null;
  /**
   * 源标识 → 名字，认不得就 null。群转账的付款方/收款方在卡片上只有标识，名字
   * 由转录从已读到的消息里接（`senderNameLookup`）。undefined = 这一端没接这条
   * 线——和「认不得」一样退回 `成员 #XXXXXX`，不猜。
   */
  memberName?: ((uid: string) => string | null) | undefined;
  /**
   * 现在被「放大」的是哪一条、哪一层，形如 `<kind>:<消息>`（PSU-2.120）。
   *
   * 放大阅读、转账详情、红包详情都是铺满一屏的浮层，而手机上关掉一层的动作只有
   * 系统「返回」——它弹的是历史。这三层此前是组件内部的 `ref`，不压历史，于是
   * 那一下返回弹掉的是「会话」那一条，人被扔回会话列表。改成由路由说了算之后，
   * 「返回」的语义才统一成一句话：退一层。
   */
  zoom?: string | null;
  /**
   * 多选态（PSU-2.168）。圆勾必须是 `.msg-row` 的 flex 子元素才能和头像对齐，
   * 所以它住在这里而不是转录的行容器里。
   *
   * 三个缺省都得显式写出来：Vue 对缺省的 boolean prop 会强转 false，而
   * `selectable` 的缺省必须是 **true**——只传 `selectionActive` 的调用方，
   * 整屏消息会静默地全部变成不可选。有用例钉着这个缺省。
   */
  selectionActive?: boolean;
  selected?: boolean;
  selectable?: boolean;
  /**
   * 别等滚进视野，立刻取媒体（PSU-2.168）。
   *
   * 导出用的那份文档挂在屏外（`left: -10000px`），`IntersectionObserver` 永远
   * 不会说它可见，于是图片一张都不加载、导出来是一份没有图的文档。
   *
   * **这个缺陷在 Vitest 里抓不到**：happy-dom 没有 `IntersectionObserver`，
   * 下面那段直接走「观察不了就立刻加载」的兜底分支，于是用例里图片总是有的。
   * 守它的是 `e2e/merge-export.spec.ts`——那条用例当场就是红的。
   */
  eagerMedia?: boolean;
}>(), {
  showSenderName: false,
  stickerFetchAvailable: true,
  loadThumbnail: undefined,
  zoom: null,
  counterpartName: null,
  memberName: undefined,
  selectionActive: false,
  selected: false,
  selectable: true,
  eagerMedia: false,
});

const emit = defineEmits<{
  "jump-to-reply": [messageId: string];
  "retry-attachment": [attachmentId: string];
  "open-media": [attachmentId: string];
  "open-file": [attachmentId: string];
  "open-record": [messageId: string];
  "enable-sticker-fetch": [];
  "open-zoom": [zoom: string];
  "close-zoom": [];
}>();

const mediaLabels: Record<AttachmentSummary["kind"], string> = {
  image: "图片",
  emoji: "表情",
  audio: "语音",
  video: "视频",
  file: "文件",
  unknown: "附件",
};

const statusLabels = ATTACHMENT_STATUS_LABELS;
// 同一格数据、两端两句话（`attachmentVariantWord`）。组件只有一套，词跟着
// 工作区走——给远程端另拉一套组件正是这一轮问题的根。
const workspace = useWorkspace();
const variantWord = attachmentVariantWord(workspace);
// 远程端那句是**一件待办**（再同步一次就有），所以走 `PA-UI-010` 那套四档
// 状态色的 warn 档；本机端那句是一件已成事实，保持安静。
const isRemoteWorkspace = workspace === "remote";

const failureLabels: Record<string, string> = {
  SOURCE_MISSING: "没有在当前数据源中找到原文件。",
  FORMAT_UNSUPPORTED: "当前版本暂不支持这种媒体格式。",
  CACHE_QUOTA_EXCEEDED: "媒体缓存空间不足。",
  MEDIA_FAILED: "媒体处理没有完成。",
  MEDIA_LOCATE_FAILED: "定位媒体文件时发生错误。",
  AUDIO_CDN_ONLY: "微信没有把这条语音保存在本机，无法回放。",
  AUDIO_CODEC_UNAVAILABLE: "本机未安装语音解码组件，已找到语音原始数据但暂时无法转换。",
  IMAGE_KEY_UNAVAILABLE: "图片密钥尚未恢复，导入任务会自动尝试；恢复后重新导入即可补解。",
  EMOJI_CDN_DISABLED: "表情资源需要联网读取；当前未启用受控 CDN 访问。",
  EMOJI_CDN_UNAVAILABLE: "表情资源无法取回：该表情的来源地址不可用或返回的内容与记录不符。",
  [REMOTE_ATTACHMENT_UNAVAILABLE_CODE]: "此端未开放附件浏览。",
};

const rootRef = ref<HTMLElement | null>(null);
const previewUrls = reactive<Record<string, string>>({});
// wxgf 动图表情的原始字节，交给 `WxgfSticker` 逐帧播（PSU-2.146）。shallow：
// 深层代理会把 Blob 包成 Proxy，`arrayBuffer()` 在代理上直接报错。
const stickerBlobs = shallowReactive<Record<string, Blob>>({});
const loadingIds = reactive(new Set<string>());
const loadedIds = reactive(new Set<string>());
const requestedIds = reactive(new Set<string>());
const previewErrors = reactive<Record<string, string>>({});
const mediaDurations = reactive<Record<string, number>>({});
const mediaLoaded = ref(false);
/**
 * 放大阅读 / 转账详情 / 红包详情这三层。
 *
 * 与灯箱、文件预览同一套约定（PSU-2.120）：层自己开、同时告诉上面压一条历史，
 * 而路由不再点名这一层时就收掉——手机上关掉一层的动作只有系统「返回」，它弹的
 * 是历史。没有接这条线的调用方（组件测试、别的宿主）照旧能开能关，只是没有那条
 * 历史可退。
 */
const textViewerOpen = ref(false);
const transferDetailOpen = ref(false);
const packetDetailOpen = ref(false);
const ZOOM_LAYERS: Record<string, Ref<boolean>> = {
  text: textViewerOpen,
  transfer: transferDetailOpen,
  packet: packetDetailOpen,
};
const openZoom = (kind: string): void => {
  const layer = ZOOM_LAYERS[kind];
  if (layer === undefined) return;
  layer.value = true;
  emit("open-zoom", `${kind}:${props.message.id}`);
};
const closeZoom = (): void => {
  for (const layer of Object.values(ZOOM_LAYERS)) layer.value = false;
  emit("close-zoom");
};
/** 路由说的那一层：`<kind>:<消息>`，不是这一条的就当没开。 */
const zoomKind = computed(() => {
  const value = props.zoom;
  if (value === null) return null;
  const separator = value.indexOf(":");
  if (separator < 0) return null;
  return value.slice(separator + 1) === props.message.id ? value.slice(0, separator) : null;
});
// `immediate` 是为了深链：别人发来的 `?zoom=text:<消息>` 打开时就该是放大着的，
// 与灯箱、文件预览同一口径。
watch(
  zoomKind,
  (kind) => {
    for (const [name, layer] of Object.entries(ZOOM_LAYERS)) layer.value = kind === name;
  },
  { immediate: true },
);
const {
  container: transferDetailContainer,
  initial: transferDetailInitial,
  onKeydown: onTransferDetailKeydown,
} = useModalFocus(transferDetailOpen, closeZoom);
void transferDetailContainer;
void transferDetailInitial;
const {
  container: packetDetailContainer,
  initial: packetDetailInitial,
  onKeydown: onPacketDetailKeydown,
} = useModalFocus(packetDetailOpen, closeZoom);
void packetDetailContainer;
void packetDetailInitial;
let mediaObserver: IntersectionObserver | null = null;

const shortIdentity = (value: string): string => {
  let hash = 2166136261;
  for (const character of value) {
    hash ^= character.codePointAt(0) ?? 0;
    hash = Math.imul(hash, 16777619);
  }
  return Math.abs(hash >>> 0).toString(36).toUpperCase().padStart(6, "0").slice(0, 6);
};

const senderLabel = computed(() => {
  if (props.message.is_self) return "我";
  if (props.message.sender_display_name !== null) return props.message.sender_display_name;
  // 远端可能连 uid 都没送到；空值不许喂进 shortIdentity——那会捏出一个
  // 看着像真实标识的稳定「成员 #」来。
  if (props.message.sender_uid === null) return "发送者未上传";
  return `成员 #${shortIdentity(props.message.sender_uid)}`;
});

const effectiveReply = computed<ReplyPreview | null>(() => {
  const server = props.message.reply_preview;
  if (server !== null) {
    const text = server.content_text.trim();
    return {
      senderName: server.sender_display_name ?? "未知成员",
      snippet: text === "" ? `[${server.message_type}]` : text.slice(0, 40),
      // 被引的那条消息的附件（PSU-2.158）。微信在引用里只留一句 `[图片]`，
      // 而那条消息就在同一份档案里——图也在。
      //
      // 服务端这一路没给出可画的媒体时退回 `props.replyPreview`：被引的那条
      // 常常就在已经加载到的这一页里，它身上的附件是同一件东西。
      media: quotedMediaOf(server.attachments) ?? props.replyPreview?.media ?? null,
    };
  }
  if (quoteCard.value !== null) {
    // The quoted message was itself a card: say what kind of thing it was
    // rather than only its title, which reads as if someone typed it.
    const structured = quotedReplyPreview(quoteCard.value);
    if (structured !== null) {
      return { ...structured, media: props.replyPreview?.media ?? null };
    }
  }
  if (quoteCard.value?.subtitle?.trim()) {
    return {
      senderName: quoteCard.value.source_name ?? "原消息",
      snippet: quoteCard.value.subtitle.slice(0, 40),
      media: props.replyPreview?.media ?? null,
    };
  }
  return props.replyPreview;
});

/** 引用条上要画的那一张；没有、或这一端取不到字节时为 null。 */
const quotedMedia = computed<AttachmentSummary | null>(() => effectiveReply.value?.media ?? null);

const quotedMediaUrl = computed<string | undefined>(() => {
  const media = quotedMedia.value;
  return media === null ? undefined : previewUrls[media.id];
});

const hasLoadedEmoji = computed(() =>
  props.message.attachments.some(
    (attachment) =>
      attachment.kind === "emoji" &&
      attachment.content_available &&
      loadedIds.has(attachment.id),
  ),
);

const isTextual = computed(
  () =>
    props.message.parse_status !== "unsupported" &&
    ["text", "emoji", "quote"].includes(props.message.message_type) &&
    !(props.message.message_type === "emoji" && hasLoadedEmoji.value),
);

// A sticker that actually rendered is the whole message; the bubble chrome
// around it (border, background, tail) only frames empty padding. Placeholders
// and failures keep the bubble: text needs a surface to sit on.
const isStickerOnly = computed(
  () => props.message.message_type === "emoji" && hasLoadedEmoji.value,
);

const mergedForwardCard = computed(() =>
  props.message.message_type === "app" && props.message.app_card?.kind === "merged_forward"
    ? props.message.app_card
    : null,
);
const transferCard = computed(() =>
  props.message.message_type === "app" && props.message.app_card?.kind === "transfer"
    ? props.message.app_card
    : null,
);
const isSystemMessage = computed(() => props.message.message_type === "system");
const transferStatus = computed(() => transferCard.value?.transfer_status ?? "unknown");
const TRANSFER_STATUS_LABELS: Record<string, string> = {
  received: "已收款",
  pending: "待收款",
  refunded: "已退还",
  unknown: "状态未知",
};
// Refunds and receipts both settle a transfer, so one field carries the time
// and the status decides what to call it.
const TRANSFER_SETTLED_LABELS: Record<string, string> = {
  received: "收款时间",
  refunded: "退还时间",
};
const TRANSFER_STATUS_ICONS: Record<string, string> = {
  received: "✓",
  refunded: "↩",
  pending: "→",
  unknown: "?",
};
const transferStatusLabel = computed(
  () => TRANSFER_STATUS_LABELS[transferStatus.value] ?? TRANSFER_STATUS_LABELS["unknown"],
);
const transferStatusIcon = computed(
  () => TRANSFER_STATUS_ICONS[transferStatus.value] ?? TRANSFER_STATUS_ICONS["unknown"],
);
const transferAmount = computed(() => transferCard.value?.transfer_amount?.trim() || "金额未记录");
// 微信自己也只在真的写过附言时才画这一行。空就是空：null 让那一行整行不出现，
// 而不是拿「未记录」把一个本来就不存在的字段撑成有内容的样子。
const transferMemo = computed(() => transferCard.value?.transfer_memo?.trim() || null);
// The source records when the transfer started; the message time is when this
// half of it was written, and the two are not the same reading.
const transferTime = computed(() =>
  transferCard.value?.transfer_time != null
    ? formatDateTimeWithSeconds(transferCard.value.transfer_time)
    : formatDateTimeSeparator(props.message.created_at),
);
/** 名单翻得出就用名字，翻不出退回一个稳定的短标识——不编名字。 */
const memberLabelFor = (uid: string): string =>
  props.memberName?.(uid) ?? `成员 #${shortIdentity(uid)}`;
/**
 * 谁付的、谁收的。
 *
 * 两条线索，先用确定性高的那条：
 *
 * 1. **源把两端都指名了**（群转账实测 166/166 都是）——各印各的，根本不必问
 *    这条消息是哪一边发的。
 * 2. **单聊**：源只在发起那半条写了对方标识，两端得靠「发送者站哪一边」定向，
 *    而那一边由 `transfer_sender_role` 说（它读的是 paysubtype）。对面是谁则
 *    是会话的对面——名字就在手边，比标识可靠。
 *
 * `transfer_status` 在这里**不能用来推角色**：一笔转账结清之后，归档会把发起
 * 那半条也提升成 received（后端 `settle_transfers`），两半条状态一模一样。真机
 * 上就是这样翻车的——一条对方发给我的 paysubtype=1 转账，卡片上存着 received，
 * 按状态推会把付款方和收款方印反。
 *
 * 什么都定不了向就两行都不出现，不猜谁付的钱。
 */
const transferParties = computed<{ payer: string | null; payee: string | null }>(() => {
  const card = transferCard.value;
  const none = { payer: null, payee: null };
  if (card === null) return none;
  const payerUid = card.transfer_payer_uid ?? null;
  const payeeUid = card.transfer_payee_uid ?? null;
  const role = card.transfer_sender_role ?? null;
  // senderLabel 自己就把 is_self 说成「我」，两边共用同一句称呼。
  const orient = (other: string | null): { payer: string | null; payee: string | null } =>
    role === "payer"
      ? { payer: senderLabel.value, payee: other }
      : { payer: other, payee: senderLabel.value };

  // 单聊优先：转账的对面就是会话的对面，而**名字就在手边**。源里那个标识这时
  // 是多余的——单聊根本不加载群名单，拿它去翻只会把一个认得的人退成
  // `成员 #XXXXXX`（真机上有 17 条单聊连 payer_username 都写了，正是这一类）。
  if (props.counterpartName !== null && role !== null) {
    return orient(props.message.is_self ? props.counterpartName : "我");
  }
  // 群聊：源把两端都指名了（实测 166/166），各印各的，不必问这条消息哪边发的。
  if (payerUid !== null && payeeUid !== null) {
    return { payer: memberLabelFor(payerUid), payee: memberLabelFor(payeeUid) };
  }
  // 只指名了一边：靠角色把另一边接上。接不上就整行不出现。
  if (role === null) return none;
  const otherUid = role === "payer" ? payeeUid : payerUid;
  return orient(otherUid === null ? null : memberLabelFor(otherUid));
});
const transferPayer = computed(() => transferParties.value.payer);
const transferPayee = computed(() => transferParties.value.payee);
const transferSettledLabel = computed(() => TRANSFER_SETTLED_LABELS[transferStatus.value] ?? null);
const transferSettledAt = computed(() =>
  transferCard.value?.transfer_settled_at != null
    ? formatDateTimeWithSeconds(transferCard.value.transfer_settled_at)
    : null,
);

/**
 * 红包与群收款是同一种上游消息（appmsg 2001），微信也把它们画成两张不同的卡：
 * 红包是红底的封套，群收款是一张账单。共用一套字段，所以共用一个 computed。
 */
const packetCard = computed(() => {
  const card = props.message.app_card;
  if (props.message.message_type !== "app" || card == null) return null;
  return card.kind === "redpacket" || card.kind === "group_payment" ? card : null;
});
const isGroupPayment = computed(() => packetCard.value?.kind === "group_payment");
// 微信画在封套上的就是这句祝福；解析器已经把它放进 title，这里只兜住空值。
const packetGreeting = computed(
  () => packetCard.value?.packet_greeting?.trim() || packetCard.value?.title.trim() || "红包",
);
const packetScene = computed(
  () => packetCard.value?.packet_scene?.trim() || (isGroupPayment.value ? "群收款" : "微信红包"),
);
/**
 * 上游给发出方和接收方各写一行（查看红包 / 领取红包），按 is_self 取本机这一侧
 * 该看到的那行——这是微信自己的语义，不是我们编的。群收款两侧写的是同一句
 * 「每人需支付 X 元」，也就是这张账单唯一记下金额的地方。
 */
const packetAction = computed(() => {
  const card = packetCard.value;
  if (card == null) return null;
  const mine = props.message.is_self ? card.packet_sender_action : card.packet_receiver_action;
  return mine?.trim() || card.packet_receiver_action?.trim() || null;
});
const packetExpiresAt = computed(() =>
  packetCard.value?.packet_expires_at != null
    ? formatDateTimeWithSeconds(packetCard.value.packet_expires_at)
    : null,
);

/**
 * 解析器把 appmsg 33/35 与 1/5 走的是同一条 `_linkish` 分支——标题、描述、
 * 出处、URL 一样齐全。之前只有 `link` 拿到卡片，小程序落回纯文本，于是本机
 * 3064 条小程序印成「[小程序] 标题 / 来源：X / 一整条裸 URL」。同一份结构
 * 画成两种东西，不是解析缺了什么，是渲染没接上。
 */
const LINKISH_CARD_KINDS = ["link", "miniprogram", "channel"];
const linkPreviewCard = computed(() => {
  const card = props.message.app_card;
  if (card == null || !LINKISH_CARD_KINDS.includes(card.kind)) return null;
  return card;
});
const quoteCard = computed(() =>
  props.message.app_card?.kind === "quote" ? props.message.app_card : null,
);
const messageBody = computed(() => quoteCard.value?.title ?? props.message.content_text);

/**
 * 「附件待定位」那张卡上印什么。
 *
 * 这张卡此前对四种附件说同一句话——图片、语音、视频、文件长得一模一样，连是
 * 哪一类都不说。真机上它覆盖 28,537 条远端记录（语音 23,972 / 文件 3,206 /
 * 视频 1,359，字节从没上行过），于是连着几条就成了「同一条消息重复了几次」。
 *
 * 能说的只说已经知道的：类型是记录自己的断言；文件名与语音时长在正文里
 * （`lib/recordPreview`，两端都有）。图片和视频**确实**只剩类型——归档从来
 * 没记过它们的名字，这里就只印「图片」，不拿时间戳之类凑一个像身份的东西。
 */
const pendingMedia = computed<{ chip: string; name: string | null }>(() => {
  const type = props.message.message_type;
  if (type === "file") {
    const name = fileNameFromRecord(props.message.content_text, props.message.app_card ?? null);
    // 有名字就连类型一起说,与装载好的文件卡同一套判定、同一个词。
    return name === null
      ? { chip: mediaLabels.file, name: null }
      : { chip: fileVisualOf(name).label, name };
  }
  if (type === "audio") {
    const durationMs = voiceDurationMsFromRecord(props.message.content_text);
    return {
      chip:
        durationMs === null
          ? mediaLabels.audio
          : `${mediaLabels.audio} ${formatVoiceDuration(durationMs / 1000)}`,
      name: null,
    };
  }
  return { chip: mediaLabels[type === "video" ? "video" : "image"], name: null };
});

const hasReadyAudio = computed(() =>
  props.message.attachments.some(
    (attachment) => attachment.kind === "audio" && attachment.content_available,
  ),
);

const hasWideCard = computed(
  () =>
    mergedForwardCard.value !== null ||
    transferCard.value !== null ||
    packetCard.value !== null ||
    linkPreviewCard.value !== null ||
    props.message.attachments.some((attachment) =>
      ["audio", "file"].includes(attachment.kind),
    ),
);

const canOpenTextViewer = computed(() => {
  if (props.message.message_type !== "text") return false;
  const content = props.message.content_text.trim();
  return Array.from(content).length >= 180 || content.split(/\r?\n/).length >= 8;
});

const safeLink = computed(() => {
  if (props.message.message_type !== "link") return null;
  try {
    const parsed = new URL(props.message.content_text);
    return parsed.protocol === "http:" || parsed.protocol === "https:" ? parsed.href : null;
  } catch {
    return null;
  }
});

const isReadableSummary = computed(
  () =>
    props.message.parse_status !== "unsupported" &&
    // 本地的 unknown 意味着解析器放弃了这条，正文多半是残渣；远端的 unknown
    // （带 source_message_type）却是「对方的词汇表比这个构建新」，正文是对方
    // 解析器认真产出的，藏起来就是丢数据。
    (props.message.message_type !== "unknown" ||
      props.message.source_message_type !== undefined) &&
    !(props.message.message_type === "emoji" && hasLoadedEmoji.value) &&
    !["image", "audio", "video", "file"].includes(props.message.message_type) &&
    safeLink.value === null &&
    props.message.content_text.trim() !== "",
);

const canRetry = (attachment: AttachmentSummary): boolean =>
  ["source_missing", "failed", "evicted"].includes(attachment.cache_status) ||
  // A sticker's bytes are remote, so "unsupported" here only means this fetch
  // came back empty — the next one may not. EMOJI_CDN_DISABLED is the other
  // case: nothing is allowed to reach the network, so offering a retry that
  // can only fail would be a lie.
  attachment.failure_code === "EMOJI_CDN_UNAVAILABLE";

const statusLabel = (attachment: AttachmentSummary): string =>
  attachment.failure_code === "AUDIO_CDN_ONLY"
    ? "本机未保存"
    : attachment.failure_code === "AUDIO_CODEC_UNAVAILABLE"
      ? "语音解码器不可用"
    : attachment.failure_code === "EMOJI_CDN_DISABLED"
      ? "联网未启用"
    : attachment.failure_code === "EMOJI_CDN_UNAVAILABLE"
      ? "无法取回"
    : attachment.failure_code === REMOTE_ATTACHMENT_UNAVAILABLE_CODE
      ? "此端未提供"
    : statusLabels[attachment.cache_status];

const formatBytes = (value: number | null): string | null => {
  if (value === null) return null;
  if (value < 1024) return `${String(value)} B`;
  if (value < 1024 * 1024) return `${(value / 1024).toFixed(1)} KB`;
  return `${(value / (1024 * 1024)).toFixed(1)} MB`;
};

const formatDuration = (value: number | undefined): string | null => {
  if (value === undefined || !Number.isFinite(value) || value < 0) return null;
  const total = Math.round(value);
  const minutes = Math.floor(total / 60);
  const seconds = total % 60;
  return `${String(minutes)}:${String(seconds).padStart(2, "0")}`;
};

// A sticker an import chose not to fetch. `discovered` with no failure code is
// the backend saying the row is fetchable and simply has not been fetched, so
// it gets the inline slot and an attempt rather than an attachment row that
// reads like something went wrong.
//
// EMOJI_CDN_DISABLED counts too, as long as the running server has not just
// told us otherwise. That code records the configuration at the moment of the
// import, not a property of the sticker: rows written before the operator
// enabled the CDN would otherwise stay dead until a full re-import, which is
// exactly the re-import deferring was meant to make unnecessary.
// A sticker that would show if fetching were allowed. Both states qualify:
// `discovered` (this build deferred it) and EMOJI_CDN_DISABLED (an older
// import ran while fetching was off). Neither describes the sticker; both
// describe a fetch that has not happened.
const isFetchableEmoji = (attachment: AttachmentSummary): boolean =>
  attachment.kind === "emoji" &&
  !attachment.content_available &&
  props.resolveAttachment !== undefined &&
  ((attachment.cache_status === "discovered" && attachment.failure_code === null) ||
    attachment.failure_code === "EMOJI_CDN_DISABLED");

const isDeferredEmoji = (attachment: AttachmentSummary): boolean =>
  isFetchableEmoji(attachment) && props.stickerFetchAvailable;

// Fetching is off. This is the one case the user can actually resolve, so it
// gets an offer instead of an explanation: three lines of "受控 CDN 访问" told
// them what was wrong and nothing about what to do next.
const isBlockedEmoji = (attachment: AttachmentSummary): boolean =>
  isFetchableEmoji(attachment) && !props.stickerFetchAvailable;

const isInlineMedia = (attachment: AttachmentSummary): boolean =>
  (attachment.kind === "image" || attachment.kind === "emoji" || attachment.kind === "video") &&
  (attachment.content_available || isFetchableEmoji(attachment));

const markPreviewLoaded = (attachmentId: string): void => {
  loadedIds.add(attachmentId);
  Reflect.deleteProperty(previewErrors, attachmentId);
};

const markPreviewFailed = (attachmentId: string): void => {
  loadedIds.delete(attachmentId);
  Reflect.deleteProperty(stickerBlobs, attachmentId);
  const url = previewUrls[attachmentId];
  if (url !== undefined) {
    URL.revokeObjectURL(url);
    Reflect.deleteProperty(previewUrls, attachmentId);
  }
  previewErrors[attachmentId] = "媒体内容无法解码。";
};

const emojiPlaceholder = (attachment: AttachmentSummary): string => {
  if (loadingIds.has(attachment.id)) return "正在加载表情……";
  const failure = previewErrors[attachment.id];
  if (failure !== undefined) return failure;
  return isDeferredEmoji(attachment) ? "表情待加载" : "表情暂时不可用";
};

const videoPreviewUrl = (attachmentId: string): string | undefined => {
  const url = previewUrls[attachmentId];
  return url === undefined ? undefined : `${url}#t=0.001`;
};

/**
 * 气泡里的图片按缩略尺寸展示，字节也应当是缩略图的；点开大图才取原图。
 * 缩略图取不到时回退原图，不多一层文案——回退路径就是今天的行为。
 */
const loadImagePreview = async (attachment: AttachmentSummary): Promise<Blob> => {
  if (props.loadThumbnail !== undefined && attachment.kind === "image") {
    try {
      return await props.loadThumbnail(attachment.id);
    } catch {
      // THUMBNAIL_UNAVAILABLE 与临时故障走同一条回退：原图能开就该开。
    }
  }
  if (props.loadAttachment === undefined) {
    throw new Error("附件加载器不可用");
  }
  return props.loadAttachment(attachment.id);
};

/**
 * 取回的字节放到哪：wxgf 动图表情交给画布播放器，其余一律做成 object URL。
 * 动图出现在图片位置上（极少）时存成第一帧——`<img>` 放不了 HEVC。
 */
const showPreview = async (attachment: AttachmentSummary, content: Blob): Promise<void> => {
  if (isWxgfAnimation(content)) {
    if (attachment.kind === "emoji") {
      stickerBlobs[attachment.id] = content;
      return;
    }
    content = await wxgfFirstFramePng(content);
  }
  previewUrls[attachment.id] = URL.createObjectURL(content);
};

const openAttachment = async (attachment: AttachmentSummary): Promise<void> => {
  if (!attachment.content_available || props.loadAttachment === undefined) return;
  if (previewUrls[attachment.id] !== undefined || stickerBlobs[attachment.id] !== undefined) return;
  loadingIds.add(attachment.id);
  Reflect.deleteProperty(previewErrors, attachment.id);
  try {
    const content =
      attachment.kind === "image"
        ? await loadImagePreview(attachment)
        : await props.loadAttachment(attachment.id);
    await showPreview(attachment, content);
  } catch {
    previewErrors[attachment.id] = "暂时无法打开附件，请重试。";
  } finally {
    loadingIds.delete(attachment.id);
  }
};

const loadVoice = async (attachmentId: string): Promise<Blob> => {
  if (props.loadAttachment === undefined) throw new Error("语音加载器不可用");
  return props.loadAttachment(attachmentId);
};

/** 下载走的是已经取到本机的字节，不再劳烦服务端一次。 */
const downloadAttachment = async (attachment: AttachmentSummary): Promise<void> => {
  if (props.loadAttachment === undefined) return;
  loadingIds.add(attachment.id);
  Reflect.deleteProperty(previewErrors, attachment.id);
  try {
    downloadBlob(await props.loadAttachment(attachment.id), attachment.file_name ?? "attachment");
  } catch {
    previewErrors[attachment.id] = "暂时无法下载这个文件，请重试。";
  } finally {
    loadingIds.delete(attachment.id);
  }
};

const fetchDeferredEmoji = async (attachment: AttachmentSummary): Promise<void> => {
  const resolve = props.resolveAttachment;
  if (resolve === undefined || props.loadAttachment === undefined) return;
  // Once per attachment per session, whatever the outcome. Scrolling the same
  // sticker in and out of view must not turn into a stream of requests, and a
  // sticker that came back empty stays empty until asked for by hand.
  if (requestedIds.has(attachment.id)) return;
  requestedIds.add(attachment.id);
  loadingIds.add(attachment.id);
  Reflect.deleteProperty(previewErrors, attachment.id);
  try {
    const resolved = await resolve(attachment.id);
    if (!resolved.content_available) {
      // Leave the disabled case unmarked: the parent has just learned the CDN
      // is off, so this row drops out of the deferred set and renders its own
      // explanation. Writing an error here would talk over it.
      if (resolved.failure_code !== "EMOJI_CDN_DISABLED") {
        previewErrors[attachment.id] = "表情暂时取不回来。";
      }
      return;
    }
    await showPreview(attachment, await props.loadAttachment(attachment.id));
  } catch {
    previewErrors[attachment.id] = "表情暂时取不回来。";
  } finally {
    loadingIds.delete(attachment.id);
  }
};

const loadReadyMedia = async (): Promise<void> => {
  if (props.loadAttachment === undefined) return;
  // A merged forward's own media is rendered inside the forward viewer, not in
  // this collapsed bubble, so loading its bytes here would fetch thumbnails for
  // pictures the bubble never shows. The bubble is the MergedForwardCard alone.
  if (mergedForwardCard.value !== null) return;
  await Promise.all(
    props.message.attachments
      .filter((attachment) => isInlineMedia(attachment))
      .map((attachment) =>
        isDeferredEmoji(attachment)
          ? fetchDeferredEmoji(attachment)
          : openAttachment(attachment),
      ),
  );
};

/**
 * 引用条上那一张走自己这条线，不搭 `loadReadyMedia` 的车（PSU-2.158）。
 *
 * 那一条是「这个气泡自己的媒体」：它等 `IntersectionObserver`、被合并转发的
 * 气泡整段跳过、还要挑 `isInlineMedia`。引用条上的图不属于这个气泡——它属于
 * 被引的那条消息，只是画在这里——三条约束没有一条该落到它头上。
 *
 * 也不走 `fetchDeferredEmoji`：一行注脚不该替人去 CDN 取表情。
 */
watch(
  quotedMedia,
  (media) => {
    if (media !== null) void openAttachment(media);
  },
  { immediate: true },
);

const startMediaLoading = (): void => {
  if (mediaLoaded.value) return;
  mediaLoaded.value = true;
  void loadReadyMedia();
};

const onVideoMetadata = (attachmentId: string, event: Event): void => {
  const element = event.target;
  if (element instanceof HTMLVideoElement && Number.isFinite(element.duration)) {
    mediaDurations[attachmentId] = element.duration;
  }
};

const jumpToReply = (): void => {
  if (props.message.reply_to_id !== null) {
    emit("jump-to-reply", props.message.reply_to_id);
  }
};

onMounted(() => {
  if (
    props.eagerMedia ||
    typeof IntersectionObserver === "undefined" ||
    rootRef.value === null ||
    !rootRef.value.isConnected
  ) {
    startMediaLoading();
    return;
  }
  mediaObserver = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        startMediaLoading();
        mediaObserver?.disconnect();
        mediaObserver = null;
      }
    },
    { rootMargin: "240px" },
  );
  mediaObserver.observe(rootRef.value);
});

watch(
  () => props.message.attachments,
  () => {
    if (mediaLoaded.value) void loadReadyMedia();
  },
  { deep: true },
);


// Turning the setting on has to fill in the stickers already on screen. Asking
// somebody to scroll away and back, or reload, would undo the point of putting
// the switch on the sticker in the first place.
watch(
  () => props.stickerFetchAvailable,
  (available) => {
    if (available && mediaLoaded.value) void loadReadyMedia();
  },
);

watch(
  () => props.message.id,
  () => {
    for (const layer of Object.values(ZOOM_LAYERS)) layer.value = false;
  },
);

onBeforeUnmount(() => {
  mediaObserver?.disconnect();
  for (const url of Object.values(previewUrls)) URL.revokeObjectURL(url);
});
</script>

<template>
  <article
    v-if="isSystemMessage"
    class="msg-system-row"
    :class="{ 'is-highlighted': highlighted, 'is-selecting': selectionActive }"
    :data-message-id="message.id"
  >
    <!-- 多选态下系统提示也占一个圆勾的位置，但它是**不可选**的：撤回、入群退群
         这类导出来只是噪音（PSU-2.26 已经把它们移出搜索索引）。画一个虚线空勾
         并写明「不可选」，比让它整行消失诚实——它在会话里确实存在。 -->
    <span
      v-if="selectionActive"
      class="msg-tick is-blocked"
      aria-hidden="true"
    />
    <span aria-hidden="true" />
    <!-- 系统提示是微信自己渲染的模板，正文里带着它的排版：`<_wc_custom_link_>`
         与 `<a>` 在微信里只画出标签文字。原样印出来，读者要的六个字会变成三行
         href。走 EmoticonText 与其它正文同一条通道，表情符号也一并认得。 -->
    <p data-feedback-mask="message-body">
      <EmoticonText :text="message.content_text" />
      <span
        v-if="selectionActive"
        class="msg-system-unselectable"
      > · 不可选</span>
    </p>
    <span aria-hidden="true" />
  </article>
  <article
    v-else
    ref="rootRef"
    class="msg-row"
    :class="{
      'is-self': message.is_self === true,
      'is-unattributed': message.is_self === null,
      'is-highlighted': highlighted,
      'is-selecting': selectionActive,
      'is-picked': selected,
    }"
    :data-message-id="message.id"
  >
    <!-- 圆勾永远在最左，**头像的左右不变**——自己的消息在多选态下仍然靠右、
         头像仍然在右，整行只被挤出一格。PC 微信就是这样。 -->
    <span
      v-if="selectionActive"
      class="msg-tick"
      :class="{ 'is-on': selected, 'is-blocked': !selectable }"
      aria-hidden="true"
    />
    <!-- 归属没被告知（is_self === null）的行不站边：两侧头像都不画，
         气泡居中虚线框。画在左边就是替远端断言「这是对方发的」。 -->
    <AvatarBadge
      v-if="message.is_self === false"
      :name="senderLabel"
      :src="message.sender_avatar_ref"
      :alt="`${senderLabel}的头像`"
      size="sm"
    />
    <div
      class="msg-content"
      :class="{ 'has-wide-card': hasWideCard }"
    >
      <strong
        v-if="showSenderName && message.is_self !== true"
        data-feedback-mask="sender-name"
        class="msg-sender"
      >
        {{ senderLabel }}
      </strong>
      <div
        class="msg-bubble has-tail"
        :class="{
          'has-transfer': transferCard !== null,
          'has-packet': packetCard !== null,
          'has-merged-forward': mergedForwardCard !== null,
          'has-link-card': linkPreviewCard !== null,
          'has-voice': hasReadyAudio,
          'has-sticker': isStickerOnly,
        }"
      >
        <button
          v-if="transferCard !== null"
          class="transfer-card"
          type="button"
          aria-label="查看转账详情"
          @click="openZoom('transfer')"
        >
          <span
            class="transfer-card-icon"
            :class="`is-${transferStatus}`"
            aria-hidden="true"
          >{{ transferStatusIcon }}</span>
          <span
            data-feedback-mask="message-body"
            class="transfer-card-main"
          >
            <strong>{{ transferAmount }}</strong>
            <small>{{ transferStatusLabel }}</small>
          </span>
          <span
            class="transfer-card-chevron"
            aria-hidden="true"
          >›</span>
          <span
            data-feedback-mask="message-body"
            class="transfer-card-foot"
          >
            {{ transferCard.transfer_memo?.trim() || "微信转账" }}
          </span>
        </button>
        <!-- 微信的红包是一整张红色封套：图标、祝福语、底下一行「微信红包」。
             群收款用同样的结构但不是红的——它是一张要钱的账单，不是礼物。 -->
        <button
          v-else-if="packetCard !== null"
          class="packet-card"
          :class="{ 'is-bill': isGroupPayment }"
          type="button"
          :aria-label="isGroupPayment ? '查看群收款详情' : '查看红包详情'"
          @click="openZoom('packet')"
        >
          <span
            class="packet-card-icon"
            aria-hidden="true"
          >{{ isGroupPayment ? "¥" : "🧧" }}</span>
          <span
            data-feedback-mask="message-body"
            class="packet-card-main"
          >
            <strong>{{ packetGreeting }}</strong>
            <small v-if="packetAction !== null">{{ packetAction }}</small>
          </span>
          <span
            v-if="packetCard.packet_exclusive"
            class="packet-card-tag"
          >专属</span>
          <span
            data-feedback-mask="message-body"
            class="packet-card-foot"
          >{{ packetScene }}</span>
        </button>
        <MergedForwardCard
          v-else-if="mergedForwardCard !== null"
          :card="mergedForwardCard"
          :data-record-message-id="message.id"
          @open="emit('open-record', message.id)"
        />
        <LinkPreviewCard
          v-else-if="linkPreviewCard !== null"
          :card="linkPreviewCard"
          :fallback-url="safeLink"
        />
        <div
          v-else-if="isTextual"
          class="msg-text-block"
        >
          <!-- 长文本在气泡里按行折叠（PSU-2.214），全文在「放大阅读」里 -->
          <p
            data-feedback-mask="message-body"
            class="msg-text"
            :class="{ 'is-clamped': canOpenTextViewer }"
          >
            <EmoticonText :text="messageBody" />
          </p>
          <button
            v-if="canOpenTextViewer"
            class="text-action msg-text-expand"
            type="button"
            @click="openZoom('text')"
          >
            放大阅读
          </button>
        </div>
        <a
          v-else-if="safeLink !== null"
          data-feedback-mask="message-body"
          class="msg-link"
          :href="safeLink"
          target="_blank"
          rel="noreferrer"
        >
          {{ messageBody }}
        </a>
        <p
          v-else-if="isReadableSummary"
          data-feedback-mask="message-body"
          class="msg-text"
        >
          <EmoticonText :text="messageBody" />
        </p>

        <div
          v-if="message.attachments.length > 0 && mergedForwardCard === null"
          class="attachment-list"
        >
          <section
            v-for="attachment in message.attachments"
            :key="attachment.id"
            class="attachment-row"
            :class="{ 'is-inline-media': isInlineMedia(attachment) }"
            :data-attachment-id="attachment.id"
            :data-attachment-kind="attachment.kind"
          >
            <template v-if="isInlineMedia(attachment)">
              <template v-if="attachment.kind === 'emoji'">
                <WxgfSticker
                  v-if="stickerBlobs[attachment.id] !== undefined"
                  :blob="stickerBlobs[attachment.id]!"
                  :alt="message.content_text || '表情附件'"
                  @loaded="markPreviewLoaded(attachment.id)"
                  @failed="markPreviewFailed(attachment.id)"
                />
                <img
                  v-else-if="previewUrls[attachment.id] !== undefined"
                  class="attachment-emoji"
                  :src="previewUrls[attachment.id]"
                  :alt="message.content_text || '表情附件'"
                  :loading="eagerMedia ? 'eager' : 'lazy'"
                  @load="markPreviewLoaded(attachment.id)"
                  @error="markPreviewFailed(attachment.id)"
                >
                <button
                  v-else-if="isBlockedEmoji(attachment)"
                  class="emoji-unlock"
                  type="button"
                  @click.stop="emit('enable-sticker-fetch')"
                >
                  <span
                    class="emoji-unlock-mark"
                    aria-hidden="true"
                  >🙂</span>
                  <span class="emoji-unlock-copy">
                    <strong>表情图片没有存在本机</strong>
                    <small>点这里联网取回并显示</small>
                  </span>
                </button>
                <span
                  v-else
                  class="media-placeholder emoji-placeholder"
                  role="status"
                >
                  {{ emojiPlaceholder(attachment) }}
                </span>
              </template>
              <button
                v-else
                data-feedback-mask="file-name"
                class="attachment-media-button"
                type="button"
                :aria-label="`打开${mediaLabels[attachment.kind]}：${attachment.file_name ?? '未命名附件'}`"
                :aria-busy="loadingIds.has(attachment.id)"
                @click="emit('open-media', attachment.id)"
              >
                <img
                  v-if="attachment.kind === 'image' && previewUrls[attachment.id] !== undefined"
                  class="attachment-image"
                  :src="previewUrls[attachment.id]"
                  :alt="attachment.file_name ?? '图片附件'"
                  :loading="eagerMedia ? 'eager' : 'lazy'"
                  @load="markPreviewLoaded(attachment.id)"
                  @error="markPreviewFailed(attachment.id)"
                >
                <video
                  v-else-if="attachment.kind === 'video' && videoPreviewUrl(attachment.id) !== undefined"
                  class="attachment-video"
                  :src="videoPreviewUrl(attachment.id)"
                  muted
                  playsinline
                  preload="metadata"
                  tabindex="-1"
                  @loadedmetadata="onVideoMetadata(attachment.id, $event)"
                  @error="markPreviewFailed(attachment.id)"
                />
                <span
                  v-else
                  class="media-placeholder"
                  role="status"
                >
                  {{ loadingIds.has(attachment.id) ? "正在加载媒体……" : "媒体暂时不可用" }}
                </span>
                <span
                  v-if="attachment.kind === 'video' && previewUrls[attachment.id] !== undefined"
                  class="media-play-overlay"
                  aria-hidden="true"
                >
                  ▶
                </span>
                <span
                  v-if="attachment.kind === 'video' && formatDuration(mediaDurations[attachment.id]) !== null"
                  class="media-duration"
                >
                  {{ formatDuration(mediaDurations[attachment.id]) }}
                </span>
              </button>
              <div
                v-if="attachment.variant === 'thumbnail' || previewErrors[attachment.id] !== undefined"
                class="attachment-media-meta"
              >
                <span
                  v-if="attachment.variant === 'thumbnail'"
                  class="attachment-variant"
                  :class="{ 'is-warning': isRemoteWorkspace }"
                >
                  {{ variantWord }}
                </span>
                <button
                  v-if="previewErrors[attachment.id] !== undefined"
                  class="text-action attachment-action"
                  type="button"
                  @click="openAttachment(attachment)"
                >
                  重试
                </button>
              </div>
              <p
                v-if="previewErrors[attachment.id] !== undefined"
                class="attachment-reason"
                role="alert"
              >
                {{ previewErrors[attachment.id] }}
              </p>
            </template>

            <template v-else-if="attachment.kind === 'audio' && attachment.content_available">
              <VoiceMessagePlayer
                :load-audio="() => loadVoice(attachment.id)"
                :label="attachment.file_name ?? '语音消息'"
                :duration-ms="attachment.duration_ms"
              />
              <!-- 「A 常驻一行」：转写就挂在播放条下面，能像文字一样扫读。
                   没有转写时整行不出现——那是「还没转」，和「转过没内容」
                   是两回事，后者由组件自己说。 -->
              <SpeechToTextLine
                v-if="message.transcript"
                :transcript="message.transcript"
              />
            </template>

            <template v-else-if="attachment.kind === 'file'">
              <FileAttachmentCard
                :attachment="attachment"
                :status-label="statusLabel(attachment)"
                :failure-reason="attachment.failure_code === null
                  ? null
                  : failureLabels[attachment.failure_code] ?? '附件暂时不可用。'"
                :can-retry="canRetry(attachment)"
                :busy="loadingIds.has(attachment.id)"
                @preview="emit('open-file', attachment.id)"
                @download="downloadAttachment(attachment)"
                @retry="emit('retry-attachment', attachment.id)"
              />
              <p
                v-if="previewErrors[attachment.id] !== undefined"
                class="attachment-reason"
                role="alert"
              >
                {{ previewErrors[attachment.id] }}
              </p>
            </template>

            <template v-else>
              <div class="attachment-main">
                <strong data-feedback-mask="file-name">{{ attachment.file_name ?? mediaLabels[attachment.kind] }}</strong>
                <small>
                  {{ mediaLabels[attachment.kind] }}
                  <template v-if="!attachment.content_available && formatBytes(attachment.size) !== null">
                    · {{ formatBytes(attachment.size) }}
                  </template>
                </small>
              </div>
              <span
                v-if="!attachment.content_available"
                class="attachment-status"
                :class="`is-${attachment.cache_status}`"
              >
                {{ statusLabel(attachment) }}
              </span>
              <button
                v-if="attachment.content_available"
                class="text-action attachment-action"
                type="button"
                :disabled="loadingIds.has(attachment.id)"
                @click="openAttachment(attachment)"
              >
                {{ loadingIds.has(attachment.id) ? "正在打开" : "查看" }}
              </button>
              <button
                v-else-if="canRetry(attachment)"
                class="text-action attachment-action"
                type="button"
                @click="emit('retry-attachment', attachment.id)"
              >
                重试
              </button>
              <p
                v-if="attachment.failure_code !== null"
                class="attachment-reason"
              >
                {{ failureLabels[attachment.failure_code] ?? "附件暂时不可用。" }}
              </p>
              <p
                v-if="previewErrors[attachment.id] !== undefined"
                class="attachment-reason"
                role="alert"
              >
                {{ previewErrors[attachment.id] }}
              </p>
              <!-- 取不回录音时，这行字是这条消息**唯一**还能读到的内容。
                   跟着播放条一起藏起来，正好藏在最需要它的时候。 -->
              <SpeechToTextLine
                v-if="attachment.kind === 'audio' && message.transcript"
                :transcript="message.transcript"
              />
            </template>
          </section>
        </div>
        <div
          v-else-if="['image', 'audio', 'video', 'file'].includes(message.message_type)"
          class="msg-media-card"
        >
          <span class="msg-type-chip">{{ pendingMedia.chip }}</span>
          <strong
            v-if="pendingMedia.name !== null"
            data-feedback-mask="file-name"
            class="msg-media-name"
          >{{ pendingMedia.name }}</strong>
          <small>附件待定位 · 消息已归档，媒体元数据尚未就绪。</small>
          <!-- 同上：录音不在，字还在。远端尤其常见——文本几乎不花钱就发过去了，
               而录音是二十多倍的字节，可能根本没传。 -->
          <SpeechToTextLine
            v-if="message.message_type === 'audio' && message.transcript"
            :transcript="message.transcript"
          />
        </div>
        <div
          v-else-if="mergedForwardCard === null && linkPreviewCard === null && transferCard === null && packetCard === null && !isTextual && safeLink === null && !isReadableSummary"
          class="msg-media-card is-unknown"
        >
          <span class="msg-type-chip">未能识别的消息类型</span>
          <small>这条消息的原始格式暂不支持展示。</small>
        </div>
        <small
          v-if="message.parse_status === 'partial'"
          class="msg-partial"
        >
          部分内容未能完整解析
        </small>
        <small
          v-if="message.source_message_type != null"
          class="msg-partial"
        >
          未知类型：{{ message.source_message_type }}
        </small>
      </div>
      <button
        v-if="message.reply_to_id !== null"
        data-feedback-mask="message-body"
        class="msg-reply"
        type="button"
        @click="jumpToReply"
      >
        <img
          v-if="quotedMediaUrl !== undefined"
          class="msg-reply-thumb"
          :src="quotedMediaUrl"
          alt=""
        >
        <template v-if="effectiveReply !== null">
          {{ effectiveReply.senderName }}：<EmoticonText :text="effectiveReply.snippet" />
        </template>
        <template v-else>
          原消息 · 点击定位
        </template>
      </button>
      <!-- The quoted message is not in this archive, so there is nowhere to
           jump to — but the reply still points at something, and showing
           nothing was the reason a quote read as a bare sentence. -->
      <p
        v-else-if="effectiveReply !== null"
        data-feedback-mask="message-body"
        class="msg-reply is-static"
      >
        <img
          v-if="quotedMediaUrl !== undefined"
          class="msg-reply-thumb"
          :src="quotedMediaUrl"
          alt=""
        >
        {{ effectiveReply.senderName }}：<EmoticonText :text="effectiveReply.snippet" />
      </p>
      <TextMessageViewer
        :open="textViewerOpen"
        title="文字消息"
        :sender-name="senderLabel"
        :created-at="message.created_at"
        :content="message.content_text"
        @close="closeZoom"
      />
      <Teleport to="body">
        <div
          v-if="transferDetailOpen && transferCard !== null"
          class="transfer-detail-backdrop"
          role="dialog"
          aria-modal="true"
          aria-label="转账详情"
          @click.self="closeZoom"
        >
          <section
            ref="transferDetailContainer"
            class="transfer-detail-panel"
            @keydown.stop="onTransferDetailKeydown"
          >
            <button
              ref="transferDetailInitial"
              class="transfer-detail-close"
              type="button"
              aria-label="关闭转账详情"
              @click="closeZoom"
            >
              ×
            </button>
            <div class="transfer-detail-summary">
              <span
                class="transfer-detail-icon"
                :class="`is-${transferStatus}`"
                aria-hidden="true"
              >{{ transferStatusIcon }}</span>
              <p :class="`transfer-detail-status is-${transferStatus}`">
                {{ transferStatusLabel }}
              </p>
              <strong data-feedback-mask="message-body">{{ transferAmount }}</strong>
            </div>
            <dl>
              <div v-if="transferPayer !== null">
                <dt>付款方</dt><dd data-feedback-mask="contact-name">
                  {{ transferPayer }}
                </dd>
              </div>
              <div v-if="transferPayee !== null">
                <dt>收款方</dt><dd data-feedback-mask="contact-name">
                  {{ transferPayee }}
                </dd>
              </div>
              <div v-if="transferMemo !== null">
                <dt>转账说明</dt><dd data-feedback-mask="message-body">
                  {{ transferMemo }}
                </dd>
              </div>
              <div><dt>转账时间</dt><dd>{{ transferTime }}</dd></div>
              <div v-if="transferSettledLabel !== null && transferSettledAt !== null">
                <dt>{{ transferSettledLabel }}</dt><dd>{{ transferSettledAt }}</dd>
              </div>
            </dl>
            <p class="transfer-detail-note">
              来自归档消息，仅供查看。这里不能收款或改变交易状态。
            </p>
          </section>
        </div>
      </Teleport>

      <Teleport
        v-if="packetCard !== null && packetDetailOpen"
        to="body"
      >
        <div
          class="transfer-detail-backdrop"
          role="dialog"
          aria-modal="true"
          :aria-label="isGroupPayment ? '群收款详情' : '红包详情'"
          @click.self="closeZoom"
        >
          <section
            ref="packetDetailContainer"
            class="transfer-detail-panel"
            @keydown.stop="onPacketDetailKeydown"
          >
            <button
              ref="packetDetailInitial"
              class="transfer-detail-close"
              type="button"
              :aria-label="isGroupPayment ? '关闭群收款详情' : '关闭红包详情'"
              @click="closeZoom"
            >
              ×
            </button>
            <div
              class="packet-detail-summary"
              :class="{ 'is-bill': isGroupPayment }"
            >
              <span
                class="packet-detail-icon"
                aria-hidden="true"
              >{{ isGroupPayment ? "¥" : "🧧" }}</span>
              <p
                data-feedback-mask="message-body"
                class="packet-detail-scene"
              >
                {{ packetScene }}
              </p>
              <strong data-feedback-mask="message-body">{{ packetGreeting }}</strong>
            </div>
            <dl>
              <div v-if="packetAction !== null">
                <dt>{{ isGroupPayment ? "每人金额" : "本机这一侧" }}</dt>
                <dd data-feedback-mask="message-body">
                  {{ packetAction }}
                </dd>
              </div>
              <div v-if="packetCard.packet_cover">
                <dt>红包封面</dt><dd data-feedback-mask="message-body">
                  {{ packetCard.packet_cover }}
                </dd>
              </div>
              <div v-if="packetCard.packet_exclusive">
                <dt>专属红包</dt><dd>发给群里指定的一个人</dd>
              </div>
              <div v-if="packetExpiresAt !== null">
                <dt>有效期至</dt><dd>{{ packetExpiresAt }}</dd>
              </div>
            </dl>
            <!-- 微信自己会显示「已领取」，那是它向服务器问来的。本机记录里
                 一个 paymsgid 只出现一次，也没有任何领取通知，所以这里不猜。 -->
            <p class="transfer-detail-note">
              {{
                isGroupPayment
                  ? "来自归档消息，仅供查看。本机没有记录谁付过款。"
                  : "来自归档消息，仅供查看。本机没有记录红包有没有被领取、领了多少。"
              }}
            </p>
          </section>
        </div>
      </Teleport>
    </div>
    <AvatarBadge
      v-if="message.is_self === true"
      :name="senderLabel"
      :src="message.sender_avatar_ref"
      :alt="`${senderLabel}的头像`"
      size="sm"
    />
  </article>
</template>

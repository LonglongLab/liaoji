<script setup lang="ts">
import { useVirtualizer } from "@tanstack/vue-virtual";
import {
  computed,
  inject,
  nextTick,
  onBeforeUnmount,
  onMounted,
  watch,
  ref,
  type ComponentPublicInstance,
} from "vue";

import { ArchiveApiError, type AttachmentSummary } from "@/api/archiveApi";
import { archiveApiKey } from "@/api/injection";
import ExportDocument from "@/components/ExportDocument.vue";
import ExportFormatSheet from "@/components/ExportFormatSheet.vue";
import FilePreviewViewer from "@/components/FilePreviewViewer.vue";
import MediaViewer from "@/components/MediaViewer.vue";
import MessageTypeRenderer from "@/components/MessageTypeRenderer.vue";
import TranscriptTouchActions from "@/components/TranscriptTouchActions.vue";
import ShareSheet from "@/components/ShareSheet.vue";
import TranscriptSelectionDock from "@/components/TranscriptSelectionDock.vue";
import {
  describeMissing,
  fetchBundle,
  packBundle,
  planBundle,
  BUNDLE_PROBE_PATH,
  type BundlePlan,
} from "@/lib/exportDocument/bundle";
import { rewriteForBundle } from "@/lib/exportDocument/bundleDom";
import { exportFileBaseName } from "@/lib/exportDocument/document";
import type { ExportFormat } from "@/lib/exportDocument/format";
import {
  expectedInlineMedia,
  preloadAttachments,
  selectionCopyText,
} from "@/lib/exportDocument/payload";
import { planSlices } from "@/lib/exportDocument/slice";
import {
  defaultSliceRender,
  exportPaperColor,
  MAX_SLICE_HEIGHT,
  SLICE_FOOTER_HEIGHT,
  measureRows,
  packSlices,
  renderSlices,
} from "@/lib/exportDocument/toImage";
import {
  blobSrcToDataUrl,
  documentStyleSheets,
  serializeExportDocument,
  undrawnStickerCount,
  waitForExportMedia,
} from "@/lib/exportDocument/render";
import { useAutoDismiss } from "@/composables/useAutoDismiss";
import { useMarqueeSelect, type RowExtent } from "@/composables/useMarqueeSelect";
import { downloadBlob } from "@/lib/filePreview/download";
import { useMessageSelectionStore } from "@/stores/messageSelection";
import { useCapabilityStore } from "@/stores/capabilities";
import { capabilityState } from "@/lib/capabilities";
import {
  currentOrigin,
  planShare,
  runShare,
  shareLink,
  type ShareExpiry,
  type ShareLoaders,
} from "@/lib/share/runShare";
import { copyText } from "@/utils/clipboard";
import {
  buildTranscriptRows,
  replyPreviewFor,
  senderNameLookup,
  type RenderConversation,
  type RenderMessage,
  type TranscriptRow,
} from "@/lib/transcript";
import {
  distanceFromBottom,
  OLDER_MESSAGES_THRESHOLD_PX,
  shouldCompensateForItemResize,
  shouldShowScrollToBottom,
  transcriptAnchorScrollTop,
  shouldLoadNewerMessages,
  shouldLoadOlderMessages,
} from "@/lib/messageTranscriptScroll";

const props = withDefaults(defineProps<{
  conversation: RenderConversation | null;
  rows: TranscriptRow[];
  messagesById: ReadonlyMap<string, RenderMessage>;
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  windowItems: RenderMessage[] | null;
  windowHasMore: boolean;
  windowFetchingMore: boolean;
  // 定位窗口往新的那一头（PSU-2.55）。缺省 false = 「这一头读完了」，
  // 那是一个不会摆出加载入口的、安全的缺省。
  windowHasNewer?: boolean;
  windowFetchingNewer?: boolean;
  highlightedId: string | null;
  errorText: string | null;
  searchReturnAvailable: boolean;
  infoOpen: boolean;
  activeMediaId?: string | null;
  activeFileId?: string | null;
  /**
   * 放大阅读 / 转账详情 / 红包详情那一层，形如 `<kind>:<消息>`（PSU-2.120）。
   * 与灯箱、文件预览同一套约定：这三层也铺满一屏，也该由路由说了算。
   */
  activeZoom?: string | null;
  loadAttachment: (attachmentId: string) => Promise<Blob>;
  // 图片气泡与大图占位用的缩略图加载器；这一端没有就不传（= 直接取原图）。
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
  /**
   * 把一条语音取成 mp3（PSU-2.178，只有本机端有）。压缩包导出用它；
   * 不传就原样取（远端的语音是同步上去的 wav）。
   */
  loadVoiceMp3?: ((attachmentId: string) => Promise<Blob>) | undefined;
  /**
   * 在线分享取字节用的加载器（PSU-2.185）：交出**存着的那份字节**，不解 wxgf、不转 mp3。
   * 远端归档里有同样内容时，远端在对象存储里直接复制，不必再传（PSU-2.184）；解码与
   * 转码后的字节远端永远没有。分享页自己解 wxgf。界面与压缩包仍用上面那几个解码的。
   * 不传就退回界面那一套（只是少了复制）。
   */
  shareLoaders?: ShareLoaders | undefined;
  // `| undefined` 是给 exactOptionalPropertyTypes 的：withDefaults 里那个
  // 显式 undefined 缺省（= 功能关闭）才写得进来。
  resolveAttachment?: ((attachmentId: string) => Promise<AttachmentSummary>) | undefined;
  stickerFetchAvailable?: boolean;
  // 历史消息仍在导入(逆序流新→旧):滚到已加载的最早消息时,"没有更多"
  // 可能只是"还没导到",要如实告知而不是让边界看起来像档案的尽头。
  importActive?: boolean;
  // 「更多」菜单按端的能力开合：远端没有会话级搜索、会话信息与导出的端点，
  // 放一个点了没反应的入口就是假入口。缺省 true = 本地行为不变。
  searchAvailable?: boolean;
  infoAvailable?: boolean;
  exportAvailable?: boolean;
  // 缺省沿用「群聊才显示发送者名」；远端 is_group 可能未知，但名字是同步来的
  // 事实，强制显示比藏起来诚实。null = 没指定，按会话推。
  showSenderNames?: boolean | null;
}>(), {
  // Vue 对缺省的 boolean prop 会强转成 false，不是 undefined——这四个的
  // 缺省语义必须显式写出来，否则本地所有调用方一夜之间菜单被清空。
  searchAvailable: true,
  infoAvailable: true,
  exportAvailable: true,
  showSenderNames: null,
  windowHasNewer: false,
  windowFetchingNewer: false,
  activeMediaId: null,
  activeFileId: null,
  activeZoom: null,
  loadThumbnail: undefined,
  loadVoiceMp3: undefined,
  shareLoaders: undefined,
  resolveAttachment: undefined,
});

const emit = defineEmits<{
  "load-more": [];
  "load-more-window": [];
  "load-more-window-newer": [];
  "jump-to-reply": [messageId: string];
  "exit-window": [];
  "open-search": [];
  "open-info": [];
  "export": [];
  "open-media-layer": [attachmentId: string];
  "open-record": [messageId: string];
  "close-media-layer": [];
  "open-file-layer": [attachmentId: string];
  "close-file-layer": [];
  "open-zoom-layer": [zoom: string];
  "close-zoom-layer": [];
  "retry-attachment": [attachmentId: string];
  "enable-sticker-fetch": [];
  "return-to-search": [];
  /**
   * 现在真的画出来的那几条消息（虚拟窗口，含上下各 8 行 overscan）。
   *
   * 远程端据此只取这几条的发送者头像（PSU-2.124）；本机端的头像是稳定 URL、
   * 浏览器自己管缓存，**不监听也完全正确**。
   */
  "visible-change": [messageIds: readonly string[]];
  back: [];
}>();

const scrollRef = ref<HTMLElement | null>(null);
const windowScrollRef = ref<HTMLElement | null>(null);

const virtualizer = useVirtualizer(
  computed(() => ({
    count: props.rows.length,
    getScrollElement: () => scrollRef.value,
    estimateSize: () => 84,
    overscan: 8,
    getItemKey: (index: number) => props.rows[index]?.message.id ?? index,
  })),
);
/**
 * 视口上方的行长高了，就把 `scrollTop` 往下推同样多（PSU-2.125）。
 *
 * 这是拿来覆盖 `virtual-core` 一个**刻意**的默认值：它对「已量过 + 正在往上
 * 滑」的重新测量跳过补偿（注释说是要躲开「往上滑时行乱跳的级联」）。可媒体到货
 * 恰好落在这一格：行先按占位态量过一次进了缓存，图片是几百毫秒后才到的第二次。
 * 实测：每帧只下发 8px，锁定行却走了 192px——那多出来的 184px 正是一张图从
 * `min-height: 96px` 撑到 280px。读者感受到的就是「滑了像没滑多少」。
 *
 * 阅读锚点取视口中部所在的消息：Mac 字体度量下，变高的图片可能刚好在视口
 * 顶端内侧。只按视口顶端比较会漏掉它，让后面的阅读位置跳动。
 * `useVirtualizer` 全程复用同一个实例（改 options 走 `setOptions`），而这一项是
 * **实例属性**不是 option，所以在这里赋一次就够了。
 */
virtualizer.value.shouldAdjustScrollPositionOnItemSizeChange = (item, _delta, instance) => {
  const offset = instance.scrollOffset;
  const height = instance.scrollRect?.height ?? 0;
  const readingUp = instance.isScrolling && instance.scrollDirection === "backward"
    && instance.itemSizeCache.has(item.key);
  const anchor = offset === null || height === 0 || !readingUp
    ? null
    : instance.getVirtualItemForOffset(offset + height / 2)?.start ?? null;
  return shouldCompensateForItemResize(item.start, offset, anchor);
};

const virtualRows = computed(() =>
  virtualizer.value.getVirtualItems().flatMap((item) => {
    const row = props.rows[item.index];
    return row === undefined ? [] : [{ item, row }];
  }),
);
/**
 * 把虚拟窗口报上去（PSU-2.124）。
 *
 * 组件自己一直知道"现在画出来的是哪几条"——不必为头像再挂一套
 * `IntersectionObserver`。远程端此前取的是**已加载的全部**消息的发送者头像：
 * 往上翻三页就是 300 条，而屏幕上十几条。
 *
 * 监听的是 id 拼成的串而不是数组：虚拟器每滚一帧都重算一次数组，但窗口没动时
 * 内容是同一批，按数组身份监听等于每帧报一次。
 */
watch(
  () => virtualRows.value.map(({ row }) => row.message.id).join("\n"),
  () => {
    emit("visible-change", virtualRows.value.map(({ row }) => row.message.id));
  },
  { immediate: true },
);

const senderNamesShown = computed(
  () => props.showSenderNames ?? props.conversation?.is_group === true,
);
/**
 * 单聊里会话对面那个人的名字，转账详情用它说出「谁付的、谁收的」。
 *
 * **只有确知是单聊才给**：`is_group` 在远端可以是 null（「没被告知」），那时
 * 会话名可能是个群名，拿它当「对面那个人」就是编。群聊同理——群里对面不是一个
 * 固定的人，得看卡片上的标识。
 */
const counterpartName = computed(() =>
  props.conversation !== null && props.conversation.is_group === false
    ? props.conversation.display_name
    : null,
);
/**
 * 群转账的付款方/收款方在卡片上只有标识，名字从**已读到的消息**里接。两端通用、
 * 零额外请求；量过的覆盖率见 `senderNameLookup`。
 */
const memberName = computed(() =>
  senderNameLookup([...props.messagesById.values(), ...(props.windowItems ?? [])]),
);
const totalSize = computed(() => virtualizer.value.getTotalSize());
const windowRows = computed(() => buildTranscriptRows(props.windowItems ?? []));

type ViewerItem = {
  attachment: RenderMessage["attachments"][number];
  senderName: string;
  createdAt: number;
};

const mediaItems = computed<ViewerItem[]>(() => {
  const messages = props.windowItems ?? props.rows.map((row) => row.message);
  return messages.flatMap((message) =>
    // A merged forward's own media belongs to the forward viewer's own gallery,
    // not this transcript-wide one -- interleaving it would let a swipe walk
    // out of the record it came from and into unrelated conversation images.
    message.app_card?.kind === "merged_forward"
      ? []
      : message.attachments
      .filter(
        (attachment) =>
          attachment.content_available &&
          (attachment.kind === "image" || attachment.kind === "video"),
      )
      .map((attachment) => ({
        attachment,
        senderName: message.sender_display_name ?? (message.is_self ? "我" : "未知成员"),
        createdAt: message.created_at,
      })),
  );
});
const mediaViewerOpen = ref(false);
const mediaViewerIndex = ref(0);
const moreOpen = ref(false);
const showScrollToBottom = ref(false);

const showMedia = (attachmentId: string): boolean => {
  const index = mediaItems.value.findIndex(
    (item: ViewerItem) => item.attachment.id === attachmentId,
  );
  if (index < 0) return false;
  mediaViewerIndex.value = index;
  mediaViewerOpen.value = true;
  moreOpen.value = false;
  return true;
};

const openMedia = (attachmentId: string): void => {
  if (showMedia(attachmentId)) emit("open-media-layer", attachmentId);
};

const closeMedia = (): void => {
  mediaViewerOpen.value = false;
  emit("close-media-layer");
};

const openFirstMedia = (): void => {
  const first = mediaItems.value[0];
  if (first !== undefined) openMedia(first.attachment.id);
};

type FilePreviewTarget = {
  attachment: RenderMessage["attachments"][number];
  senderName: string;
  createdAt: number;
};

const filePreview = ref<FilePreviewTarget | null>(null);

const showFilePreview = (attachmentId: string): boolean => {
  const messages = props.windowItems ?? props.rows.map((row) => row.message);
  for (const message of messages) {
    const attachment = message.attachments.find((item) => item.id === attachmentId);
    if (attachment === undefined) continue;
    filePreview.value = {
      attachment,
      senderName: message.is_self ? "我" : message.sender_display_name ?? "未知成员",
      createdAt: message.created_at,
    };
    return true;
  }
  return false;
};

// 和图片灯箱一样，这一层归路由管:开一层就压一条历史,手机上的系统「返回」
// 才关得掉它;换会话时新路由不带 file,这一层也就跟着上一个会话一起收走。
const openFilePreview = (attachmentId: string): void => {
  if (showFilePreview(attachmentId)) emit("open-file-layer", attachmentId);
};

const closeFilePreview = (): void => {
  filePreview.value = null;
  emit("close-file-layer");
};

const downloadFromPreview = async (attachmentId: string): Promise<void> => {
  const target = filePreview.value;
  if (target === null) return;
  downloadBlob(
    await props.loadAttachment(attachmentId),
    target.attachment.file_name ?? "attachment",
  );
};

const mediaScopeNotice = computed(() => {
  if (props.windowItems !== null) return "当前仅浏览定位窗口内已加载的媒体。";
  if (props.hasMore) return "当前仅浏览已加载消息中的媒体；向上加载后可扩大范围。";
  return null;
});

/**
 * 转录里的一个落点：**哪一条消息**、它当初离容器顶多远。
 *
 * 记消息而不是记下标：续读会把已加载那批的下标整体往后推 100，下标在贴入
 * 前后指的不是同一条。
 */
type TranscriptAnchor = {
  messageId: string;
  viewportOffset: number;
};

/** 续读前抢下的落点；贴完就用它把读者放回原处。 */
let prependAnchor: TranscriptAnchor | null = null;
let windowPrependSnapshot: { scrollTop: number; scrollHeight: number } | null = null;
let suppressTopLoad = false;
let scrollRestoreGeneration = 0;

let fullTranscriptAnchor: TranscriptAnchor | null = null;

/**
 * 抢一个落点下来。
 *
 * `edge` 选的是拿视口哪一头那条当参照物：往上续读拿顶上那条（读者正要看它），
 * 退出定位窗口时拿底下那条（他刚才停在那儿）。
 */
const captureTranscriptAnchor = (edge: "first" | "last"): TranscriptAnchor | null => {
  const scrollElement = scrollRef.value;
  if (scrollElement === null) return null;
  const scrollBounds = scrollElement.getBoundingClientRect();
  const visibleRows = [...scrollElement.querySelectorAll<HTMLElement>(".transcript-row")].filter(
    (element) => {
      const bounds = element.getBoundingClientRect();
      return bounds.bottom > scrollBounds.top && bounds.top < scrollBounds.bottom;
    },
  );
  const visible = edge === "first" ? visibleRows[0] : visibleRows.at(-1);
  const messageId = visible?.dataset.messageId;
  if (visible === undefined || messageId === undefined) return null;
  return {
    messageId,
    viewportOffset: visible.getBoundingClientRect().top - scrollBounds.top,
  };
};

/**
 * 把读者放回落点上。
 *
 * 先拿消息在**现在这一批行里**的下标粗定位（它可能已经不在渲染窗口里了），
 * 再逐帧用它的真实位置校到当初那个偏移。贴入的那 100 条会陆续被量到，
 * 而它们全在锁定行**上方**——那部分由
 * `shouldAdjustScrollPositionOnItemSizeChange` 接住，这里只管落点。
 */
const restoreTranscriptAnchor = (anchor: TranscriptAnchor, generation: number): void => {
  const scrollElement = scrollRef.value;
  if (scrollElement === null || generation !== scrollRestoreGeneration) return;
  const index = props.rows.findIndex((row) => row.message.id === anchor.messageId);
  if (index < 0) return;
  /*
   * 落点自己算，**不走 `scrollToIndex`**：那一条会在虚拟器里留下一个「待办滚动」，
   * 之后每次测量落地都再把视口拽回那个位置一次。读者紧接着自己滑（比如一路拐到
   * 最新一条）会被那个待办拽回来，那是另一种「滑了像没滑」。
   */
  const offset = virtualizer.value.getOffsetForIndex(index, "start")?.[0];
  if (offset !== undefined) {
    scrollElement.scrollTop = transcriptAnchorScrollTop(offset, anchor.viewportOffset);
  }
  suppressTopLoad = true;
  /*
   * 粗定位用的是虚拟器手上的测量值，而新贴的那一批还都是估算值。等它们被
   * 真的量到，再用锁定行的真实位置校一次。对齐了就停——帧数尽量少，那是一段
   * 读者自己滑不动的窗口。
   */
  let remainingFrames = 4;
  const settle = (): void => {
    const currentScroll = scrollRef.value;
    if (currentScroll === null || generation !== scrollRestoreGeneration) {
      suppressTopLoad = false;
      return;
    }
    const row = currentScroll.querySelector<HTMLElement>(
      `.transcript-row[data-message-id="${CSS.escape(anchor.messageId)}"]`,
    );
    let settled = false;
    if (row !== null) {
      const rowStart =
        currentScroll.scrollTop +
        (row.getBoundingClientRect().top - currentScroll.getBoundingClientRect().top);
      const next = transcriptAnchorScrollTop(rowStart, anchor.viewportOffset);
      if (Math.abs(next - currentScroll.scrollTop) > 0.5) {
        currentScroll.scrollTop = next;
      } else {
        settled = true;
      }
    }
    remainingFrames -= 1;
    if (!settled && remainingFrames > 0 && typeof globalThis.requestAnimationFrame === "function") {
      globalThis.requestAnimationFrame(settle);
    } else {
      suppressTopLoad = false;
    }
  };
  if (typeof globalThis.requestAnimationFrame === "function") {
    globalThis.requestAnimationFrame(settle);
  } else {
    settle();
  }
};

const requestOlderMessages = (): void => {
  const scrollElement = scrollRef.value;
  if (
    scrollElement === null ||
    !shouldLoadOlderMessages(
      scrollElement.scrollTop,
      OLDER_MESSAGES_THRESHOLD_PX,
      props.hasMore,
      props.fetchingMore,
    )
  ) {
    return;
  }
  prependAnchor = captureTranscriptAnchor("first");
  emit("load-more");
};

const requestOlderWindow = (): void => {
  const scrollElement = windowScrollRef.value;
  if (
    scrollElement === null ||
    !shouldLoadOlderMessages(
      scrollElement.scrollTop,
      OLDER_MESSAGES_THRESHOLD_PX,
      props.windowHasMore,
      props.windowFetchingMore,
    )
  ) {
    return;
  }
  windowPrependSnapshot = {
    scrollTop: scrollElement.scrollTop,
    scrollHeight: scrollElement.scrollHeight,
  };
  emit("load-more-window");
};

/**
 * 往新的那一头续读（PSU-2.55）。
 *
 * 不留位置快照：内容长在下面，`scrollTop` 一个像素都不动，视口里的东西原地
 * 不变。向上那一半必须补偿高度，这一半不必——两者不对称是**事实**如此，不是
 * 漏了一段。
 */
const requestNewerWindow = (): void => {
  const scrollElement = windowScrollRef.value;
  if (
    scrollElement === null ||
    !shouldLoadNewerMessages(
      distanceFromBottom(
        scrollElement.scrollTop,
        scrollElement.clientHeight,
        scrollElement.scrollHeight,
      ),
      OLDER_MESSAGES_THRESHOLD_PX,
      props.windowHasNewer,
      props.windowFetchingNewer,
    )
  ) {
    return;
  }
  emit("load-more-window-newer");
};

const onScroll = (): void => {
  if (!suppressTopLoad) requestOlderMessages();
  const scrollElement = scrollRef.value;
  showScrollToBottom.value = scrollElement !== null && shouldShowScrollToBottom(
    distanceFromBottom(
      scrollElement.scrollTop,
      scrollElement.clientHeight,
      scrollElement.scrollHeight,
    ),
  );
};

const onWindowScroll = (): void => {
  requestOlderWindow();
  requestNewerWindow();
};

watch(
  () => props.rows.length,
  async (length, previousLength) => {
    const anchor = prependAnchor;
    if (anchor === null || length <= previousLength) return;
    prependAnchor = null;
    await nextTick();
    restoreTranscriptAnchor(anchor, scrollRestoreGeneration);
  },
);

watch(
  () => props.windowItems?.length ?? 0,
  async (length, previousLength) => {
    const snapshot = windowPrependSnapshot;
    if (snapshot === null || length <= previousLength) return;
    windowPrependSnapshot = null;
    await nextTick();
    const scrollElement = windowScrollRef.value;
    if (scrollElement === null) return;
    scrollElement.scrollTop =
      snapshot.scrollTop + scrollElement.scrollHeight - snapshot.scrollHeight;
  },
);

watch(
  () => props.windowItems,
  async (items, previousItems) => {
    const generation = ++scrollRestoreGeneration;
    if (items !== null && previousItems === null) {
      const captured = captureTranscriptAnchor("last");
      if (captured !== null) fullTranscriptAnchor = captured;
    }
    mediaViewerOpen.value = false;
    prependAnchor = null;
    if (items !== null || previousItems === null || fullTranscriptAnchor === null) return;
    const anchorToRestore = fullTranscriptAnchor;
    fullTranscriptAnchor = null;
    await nextTick();
    restoreTranscriptAnchor(anchorToRestore, generation);
  },
);

watch(
  [() => props.activeMediaId, mediaItems],
  ([attachmentId]) => {
    if (attachmentId === null) {
      mediaViewerOpen.value = false;
      return;
    }
    if (!showMedia(attachmentId)) mediaViewerOpen.value = false;
  },
  { immediate: true },
);

watch(
  [() => props.activeFileId, () => props.windowItems ?? props.rows],
  ([attachmentId]) => {
    if (attachmentId === null) {
      filePreview.value = null;
      return;
    }
    if (!showFilePreview(attachmentId)) filePreview.value = null;
  },
  { immediate: true },
);

watch(
  () => props.conversation?.id,
  () => {
    scrollRestoreGeneration += 1;
    prependAnchor = null;
    fullTranscriptAnchor = null;
    moreOpen.value = false;
    showScrollToBottom.value = false;
  },
);

const measureRow = (el: Element | ComponentPublicInstance | null): void => {
  if (el instanceof HTMLElement) {
    virtualizer.value.measureElement(el);
  }
};

const scrollToIndex = (index: number, align: "center" | "end" = "center"): void => {
  suppressTopLoad = true;
  virtualizer.value.scrollToIndex(index, { align });
  if (typeof globalThis.requestAnimationFrame === "function") {
    globalThis.requestAnimationFrame(() => {
      suppressTopLoad = false;
    });
  } else {
    suppressTopLoad = false;
  }
};

const captureReadingPosition = (): void => {
  fullTranscriptAnchor = captureTranscriptAnchor("last");
};

const scrollToMessage = async (messageId: string): Promise<void> => {
  await nextTick();
  const scrollElement = windowScrollRef.value;
  if (scrollElement === null) return;
  const row = [...scrollElement.querySelectorAll<HTMLElement>(".transcript-window-row")].find(
    (element) => element.dataset.messageId === messageId,
  );
  if (row === undefined) return;
  row.scrollIntoView({ block: "center", inline: "nearest" });
};

const scrollToLatest = (): void => {
  if (props.rows.length === 0) return;
  showScrollToBottom.value = false;
  suppressTopLoad = true;
  virtualizer.value.scrollToIndex(props.rows.length - 1, {
    align: "end",
    behavior: "smooth",
  });
  if (typeof globalThis.requestAnimationFrame === "function") {
    globalThis.requestAnimationFrame(() => { suppressTopLoad = false; });
  } else {
    suppressTopLoad = false;
  }
};

// ---------------------------------------------------------------------------
// 多选与导出（PSU-2.168）
// ---------------------------------------------------------------------------

const selection = useMessageSelectionStore();

/** 导出格式面板开着没有。 */
const sheetOpen = ref(false);
/** 分享面板开着没有（PSU-2.182）。与导出面板互斥。 */
const shareSheetOpen = ref(false);
/**
 * 右键/长按那张菜单：开在哪一条上、开在视口哪一点（交互第二版）。
 *
 * 从前钉在行的左上角，正好压住头像右边的气泡开头——看起来不像右键菜单，
 * 倒像一个盖在消息上的按钮。现在照正常的右键菜单开在光标处，碰到视口边缘往里翻。
 */
const rowMenu = ref<{ id: string; x: number; y: number; touch: boolean } | null>(null);
const rowMenuRef = ref<HTMLElement | null>(null);
let pressTimer: ReturnType<typeof globalThis.setTimeout> | null = null;
let lastTouchAt = Number.NEGATIVE_INFINITY;

/**
 * 系统提示不可选：撤回、入群退群这类导出来只是噪音，PSU-2.26 也已经把它们
 * 移出了搜索索引。判据与那里同一个字段，不另立一套。
 */
const isSelectable = (message: RenderMessage): boolean => message.message_type !== "system";

/**
 * 眼下这一屏到底是哪几行。
 *
 * 定位窗口打开时读的是那一片，否则是完整转录。**两条路都要能多选**：只做完整
 * 转录的话，从搜索定位过来的读者会发现同一个长按菜单少了一项，而那个差别没有
 * 任何道理。
 */
const activeRows = computed(() => (props.windowItems === null ? props.rows : windowRows.value));
/** 当下**已加载**的那串 id，按转录顺序。区间选与导出顺序都以它为准。 */
const loadedOrder = computed(() => activeRows.value.map((row) => row.message.id));
const selectableIds = computed(
  () =>
    new Set(
      activeRows.value.filter((row) => isSelectable(row.message)).map((row) => row.message.id),
    ),
);
const pickedMessages = computed(() => {
  // 定位窗口里的消息不一定在 `messagesById` 里（那是完整转录那一路的索引），
  // 所以先把眼下这一屏并进来再查。
  const lookup = new Map(props.messagesById);
  for (const row of activeRows.value) lookup.set(row.message.id, row.message);
  return selection.orderedIds(loadedOrder.value).flatMap((id) => {
    const message = lookup.get(id);
    return message === undefined ? [] : [message];
  });
});
/**
 * 「保存媒体」在本单元**恒灰**（PSU-2.168），要到 2.171 才点亮。
 *
 * 它有明确的禁用态语义（这批里没有可保存的媒体），所以灰着不算假入口；
 * 但**按选中内容点亮它就是假入口**——那个动作这个单元还没有实现，点下去
 * 什么都不会发生。等真做了再把这里换成按附件判断。
 */
const saveMediaReady = false;

/**
 * 多选跟着「这一屏」走：换了会话、或在完整转录与定位窗口之间来回，就退出（评审 P1）。
 *
 * store 是全局的 Pinia，选中的是 id。不退的话，到了别的会话底栏还开着、还说
 * 「导出 N 条」，点下去却什么都导不出——那几个 id 根本不在眼下这一屏里。
 * 同一段定位窗口往上往下续读不算换屏（`windowItems` 只是变长），不退。
 */
const selectionScope = computed(
  () => `${props.conversation?.id ?? ""}|${props.windowItems === null ? "full" : "window"}`,
);
watch(selectionScope, () => {
  rowMenu.value = null;
  sheetOpen.value = false;
  shareSheetOpen.value = false;
  selection.exit();
});
onBeforeUnmount(() => {
  cancelRowPress();
  selection.exit();
});

const cancelRowPress = (): void => {
  if (pressTimer !== null) {
    globalThis.clearTimeout(pressTimer);
    pressTimer = null;
  }
};

const startRowPress = (event: TouchEvent, messageId: string): void => {
  lastTouchAt = Date.now();
  cancelRowPress();
  if (selection.active) return;
  const target = event.target;
  if (target instanceof Element && target.closest(".touch-row-actions") !== null) return;
  // Text and media controls keep the browser's native gesture. Only the avatar
  // starts whole-message actions; no timer races the native selection toolbar.
  if (!(target instanceof Element) || target.closest(".avatar-badge") === null) {
    closeRowMenu();
    return;
  }
  const touch = event.touches[0];
  const x = touch?.clientX ?? 0;
  const y = touch?.clientY ?? 0;
  pressTimer = globalThis.setTimeout(() => {
    if (!hasNativeSelection()) placeRowMenu(messageId, x, y, true);
  }, 420);
};

const openRowMenu = (event: MouseEvent, messageId: string): void => {
  cancelRowPress();
  if (selection.active) return;
  const touch =
    ("pointerType" in event && event.pointerType === "touch") ||
    Date.now() - lastTouchAt < 1500;
  if (touch) {
    const target = event.target;
    if (!(target instanceof Element) || target.closest(".avatar-badge") === null) {
      closeRowMenu();
      return;
    }
    if (hasNativeSelection()) return;
    event.preventDefault();
    placeRowMenu(messageId, event.clientX, event.clientY, true);
    return;
  }
  event.preventDefault();
  placeRowMenu(messageId, event.clientX, event.clientY);
};

/** 开在这一点；量出菜单自己的尺寸之后，超出视口的那一边往里翻。 */
const placeRowMenu = (messageId: string, x: number, y: number, touch = false): void => {
  rowMenu.value = { id: messageId, x, y, touch };
  if (touch) return;
  void nextTick(() => {
    const menu = rowMenuRef.value;
    const current = rowMenu.value;
    if (menu === null || current === null) return;
    const { innerWidth, innerHeight } = globalThis;
    const width = menu.offsetWidth;
    const height = menu.offsetHeight;
    const flippedX = x + width > innerWidth - 4 ? Math.max(4, x - width) : x;
    const flippedY = y + height > innerHeight - 4 ? Math.max(4, y - height) : y;
    if (flippedX !== x || flippedY !== y) rowMenu.value = { ...current, x: flippedX, y: flippedY };
  });
};

const closeRowMenu = (): void => {
  rowMenu.value = null;
};

const rowMenuStyle = computed(() =>
  rowMenu.value === null
    ? {}
    : { left: `${String(rowMenu.value.x)}px`, top: `${String(rowMenu.value.y)}px` },
);

/**
 * 点菜单外面、滚滚轮、按 Esc、窗口失焦，都收起来——和系统右键菜单一样。
 *
 * **不挂在 `scroll` 上**：图片到货、虚拟列表补偿位置都会让转录自己滚一下，
 * 菜单会刚开就被关掉（e2e 抓到的，时红时绿）。只有人滚的才算。
 */
const onGlobalPointerDown = (event: Event): void => {
  if (rowMenu.value === null) return;
  const target = event.target;
  if (target instanceof Node && rowMenuRef.value?.contains(target) === true) return;
  if (target instanceof Element && target.closest(".touch-row-actions") !== null) return;
  closeRowMenu();
};

const enterSelection = (messageId: string): void => {
  closeRowMenu();
  selection.enter(messageId, selectableIds.value);
};

const beginSelection = (): void => {
  cancelRowPress();
  closeRowMenu();
  moreOpen.value = false;
  globalThis.getSelection()?.removeAllRanges();
  selection.begin();
};

const hasNativeSelection = (): boolean => {
  const picked = globalThis.getSelection();
  return picked !== null && !picked.isCollapsed && selectionRowId(picked) !== null;
};

const onNativeSelectionChange = (): void => {
  if (!hasNativeSelection()) return;
  cancelRowPress();
  closeRowMenu();
};

/** 选区的起点落在哪一条消息里；不在任何一条里就是 null。 */
const selectionRowId = (picked: Selection | null): string | null => {
  const node = picked?.anchorNode ?? null;
  if (node === null) return null;
  const element = node instanceof Element ? node : node.parentElement;
  return element?.closest<HTMLElement>("[data-message-id]")?.dataset.messageId ?? null;
};

/**
 * 菜单里的「复制」：有选中的文字、而且就在这一条里，就只复制选中的那段；
 * 否则复制这一条（和多选的「复制」同一种格式：时间 发送人：正文）。
 */
const copyOne = async (messageId: string): Promise<void> => {
  closeRowMenu();
  const picked = globalThis.getSelection();
  const selectedText = picked?.toString() ?? "";
  const message = activeRows.value.find((row) => row.message.id === messageId)?.message;
  if (message === undefined) return;
  const text =
    selectedText.trim() !== "" && selectionRowId(picked) === messageId
      ? selectedText
      : selectionCopyText([message]);
  // 成功不吵，和多选的「复制」一样；复制不了才说。
  const copied = await copyText(text);
  exportNotice.value = copied ? null : warnNotice("这个环境不让复制到剪贴板。");
};

/**
 * 多选态下整行可点——不用瞄准那个小圆圈。
 *
 * 必须走**捕获**阶段并就地拦下：气泡里的图片、文件、引用各自都有点击处理，
 * 冒泡阶段接的话，勾选一条图片消息会顺手把灯箱打开。
 */
const onRowClickCapture = (event: MouseEvent, messageId: string): void => {
  if (!selection.active) return;
  event.stopPropagation();
  event.preventDefault();
  if (!selectableIds.value.has(messageId)) return;
  // Shift+点击：从上一次点的那条连选到这一条（交互第二版）。
  if (event.shiftKey) selection.extendTo(messageId, loadedOrder.value, selectableIds.value);
  else selection.toggle(messageId);
};

// --- 框选（交互第二版，照 PC 微信：按住左键拉出一个灰色框）------------------

/** 两条路（完整转录 / 定位窗口）各有自己的滚动区，框选跟着眼下那一个走。 */
const activeScrollEl = computed(() =>
  props.windowItems === null ? scrollRef.value : windowScrollRef.value,
);

/**
 * 每一条消息在滚动内容里的上下沿。
 *
 * 完整转录是虚拟列表，画出来的只有十几行，拉框自动滚的时候框早就越过了它们——
 * 所以按**虚拟器的量表**算（没量过的用估计高度），不去 DOM 里找。
 * 定位窗口是一次画全的，直接量 DOM。
 */
const rowExtents = (): RowExtent[] => {
  const scroller = activeScrollEl.value;
  if (scroller === null) return [];
  const scrollerTop = scroller.getBoundingClientRect().top - scroller.scrollTop;
  if (props.windowItems !== null) {
    return [...scroller.querySelectorAll<HTMLElement>("[data-message-id]")].map((row) => {
      const box = row.getBoundingClientRect();
      return {
        id: row.dataset.messageId ?? "",
        top: box.top - scrollerTop,
        bottom: box.bottom - scrollerTop,
      };
    });
  }
  const channel = scroller.querySelector<HTMLElement>(".transcript-virtual");
  const offset = channel === null ? 0 : channel.getBoundingClientRect().top - scrollerTop;
  return virtualizer.value.measurementsCache.flatMap((item) => {
    const id = props.rows[item.index]?.message.id;
    return id === undefined ? [] : [{ id, top: offset + item.start, bottom: offset + item.end }];
  });
};

/** 多选态下开始拉框那一刻已经选了哪些：框是往它上面加，缩回来时要还原成它。 */
let pickedBeforeMarquee = new Set<string>();
const marquee = useMarqueeSelect({
  container: activeScrollEl,
  rowExtents,
  selecting: () => selection.active,
  onStart: () => {
    closeRowMenu();
    pickedBeforeMarquee = new Set(selection.active ? selection.picked : []);
  },
  onLive: (ids) => {
    selection.pickMany(ids, selectableIds.value, pickedBeforeMarquee);
  },
  onFinish: (ids) => {
    selection.pickMany(ids, selectableIds.value, pickedBeforeMarquee);
  },
});

/**
 * 多选态的键盘：`Esc` 退出，`Ctrl+A` 全选眼下已加载的。
 * 焦点在输入框里时一律不管，那里的 `Ctrl+A` 是全选文字。
 */
const onKeydown = (event: KeyboardEvent): void => {
  if (event.key === "Escape" && rowMenu.value !== null) {
    closeRowMenu();
    return;
  }
  if (!selection.active || sheetOpen.value || shareSheetOpen.value) return;
  const target = event.target;
  if (
    target instanceof HTMLElement &&
    target.closest("input, textarea, [contenteditable]") !== null
  ) {
    return;
  }
  if (event.key === "Escape") {
    selection.exit();
  } else if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "a") {
    event.preventDefault();
    selection.selectAll(loadedOrder.value, selectableIds.value);
  }
};

onMounted(() => {
  globalThis.document.addEventListener("selectionchange", onNativeSelectionChange);
  globalThis.addEventListener("keydown", onKeydown);
  globalThis.addEventListener("mousedown", onGlobalPointerDown, true);
  globalThis.addEventListener("blur", closeRowMenu);
  globalThis.addEventListener("resize", closeRowMenu);
});
onBeforeUnmount(() => {
  globalThis.document.removeEventListener("selectionchange", onNativeSelectionChange);
  globalThis.removeEventListener("keydown", onKeydown);
  globalThis.removeEventListener("mousedown", onGlobalPointerDown, true);
  globalThis.removeEventListener("blur", closeRowMenu);
  globalThis.removeEventListener("resize", closeRowMenu);
});

const copySelection = async (): Promise<void> => {
  const copied = await copyText(selectionCopyText(pickedMessages.value));
  exportNotice.value = copied ? null : warnNotice("这个环境不让复制到剪贴板。");
};

// --- 导出 -------------------------------------------------------------------

const exportFormat = ref<ExportFormat>("bundle");
const useOriginals = ref(false);
/** 「视频」那一行的开关：关掉就只留封面（原型第 5 块）。 */
const includeVideos = ref(true);
/** 导出过程中的那句进度，面板的按钮上显示。 */
const exportProgress = ref<string | null>(null);
/** 压缩包的清单：面板靠它把个数与大小说在前面，导出也照它取字节。 */
const bundlePlan = computed<BundlePlan | null>(() =>
  exportFormat.value === "bundle"
    ? planBundle(pickedMessages.value, {
        includeVideos: includeVideos.value,
        useOriginals: useOriginals.value,
        voiceExtension: props.loadVoiceMp3 === undefined ? "wav" : "mp3",
      })
    : null,
);
/** 这批里能带上的图片张数，面板上如实说，也决定「用原图」开关亮不亮。 */
const pickedImageCount = computed(() =>
  pickedMessages.value.reduce(
    (total, message) =>
      total +
      message.attachments.filter(
        (attachment) =>
          attachment.content_available &&
          (attachment.kind === "image" || attachment.kind === "emoji"),
      ).length,
    0,
  ),
);

const exportMessages = ref<RenderMessage[] | null>(null);
const exportCache = ref<Map<string, Blob>>(new Map());
const exportDocRef = ref<InstanceType<typeof ExportDocument> | null>(null);
const exportBusy = ref(false);
/**
 * 转录下沿浮着的那句话。`warn` = 有东西没做成（缺图、失败），要用户留意；
 * `info` = 纯告知（「已切成 3 张」），用平静的底色。
 */
interface TranscriptNotice {
  text: string;
  tone: "info" | "warn";
}
const warnNotice = (text: string): TranscriptNotice => ({ text, tone: "warn" });
const exportNotice = ref<TranscriptNotice | null>(null);
/**
 * 导出结果那句话浮在转录区下沿，8 秒自己走、悬停暂停、✕ 立刻关（交互第二版，
 * 用户在原型上点定「一律 8 秒」）。从前是一条满宽的框贴在底边、关不掉。
 */
const noticeDismissal = useAutoDismiss(exportNotice, 8_000);

/**
 * 屏外那份文档取字节的通道：**只从已经解好的 Map 里拿**。
 *
 * 解不到的那几张不在 Map 里，这里就 reject——组件照它自己的规矩画占位，
 * 和 App 里「这张图还没缓存」是同一个样子，不会凭空多出一种状态。
 */
const exportLoader = (attachmentId: string): Promise<Blob> => {
  const blob = exportCache.value.get(attachmentId);
  return blob === undefined
    ? Promise.reject(new Error("attachment not preloaded"))
    : Promise.resolve(blob);
};

// ——— 在线分享（PSU-2.182）———
// API 可选注入：转录组件在很多用例里单独挂载，没有 API 就没有「分享」这件事。
const shareApi = inject(archiveApiKey, null);
const capabilities = useCapabilityStore();
const shareState = computed(() =>
  capabilityState(capabilities.deployment, capabilities.states, "share"),
);
/** 「分享」出不出现：能力清单说这一端没有就不出现；未配置照样出现。 */
const canShare = computed(() => shareApi !== null && shareState.value !== "unsupported");
/**
 * 没配对时的配置入口。只有认领了增量同步的一端（本机）才谈得上「接入远程归档」；
 * 远程网页上没有这一页。
 */
const sharePairingPath = computed(() =>
  capabilityState(capabilities.deployment, capabilities.states, "incremental-sync") ===
  "unsupported"
    ? null
    : "/pairing",
);
const shareExpiry = ref<ShareExpiry>(7);
const shareHideNames = ref(false);
const shareUseOriginals = ref(false);
const shareBusy = ref(false);
const shareProgress = ref<string | null>(null);
const shareError = ref<string | null>(null);
const shareResult = ref<{ link: string; expiresAt: number | null } | null>(null);
// 分享里的语音一律是存着的那份 wav：两端一致，远端有就能复制（PSU-2.185）。
const SHARE_VOICE_EXTENSION = "wav";
const sharePlan = computed(() =>
  shareSheetOpen.value
    ? planShare(pickedMessages.value, {
        useOriginals: shareUseOriginals.value,
        voiceExtension: SHARE_VOICE_EXTENSION,
      })
    : null,
);

const openShareSheet = (): void => {
  sheetOpen.value = false;
  shareError.value = null;
  shareResult.value = null;
  shareSheetOpen.value = true;
};

const closeShareSheet = (): void => {
  shareSheetOpen.value = false;
  // 链接已经生成：这一批的事办完了，和导出完成一样退出多选。
  if (shareResult.value !== null) selection.exit();
  shareResult.value = null;
};

const runShareNow = async (): Promise<void> => {
  const messages = pickedMessages.value;
  if (shareApi === null || messages.length === 0 || shareBusy.value) return;
  shareBusy.value = true;
  shareError.value = null;
  try {
    const { share, missing } = await runShare(
      shareApi,
      messages,
      {
        loadAttachment: props.shareLoaders?.loadAttachment ?? props.loadAttachment,
        loadThumbnail:
          props.shareLoaders === undefined ? props.loadThumbnail : props.shareLoaders.loadThumbnail,
        loadVoiceMp3: undefined,
      },
      {
        expiresInDays: shareExpiry.value,
        hideNames: shareHideNames.value,
        useOriginals: shareUseOriginals.value,
        conversationName: props.conversation?.display_name ?? "",
        voiceExtension: SHARE_VOICE_EXTENSION,
        onProgress: (text) => {
          shareProgress.value = text;
        },
      },
    );
    shareResult.value = { link: shareLink(share, currentOrigin()), expiresAt: share.expires_at };
    const words = describeMissing(missing);
    if (words !== null) exportNotice.value = warnNotice(`链接已生成，但${words}`);
  } catch (error: unknown) {
    shareError.value =
      error instanceof ArchiveApiError ? error.message : "没能生成链接，请稍后再试。";
  } finally {
    shareBusy.value = false;
    shareProgress.value = null;
  }
};

const runExport = async (): Promise<void> => {
  const messages = pickedMessages.value;
  if (messages.length === 0 || exportBusy.value) return;
  const format = exportFormat.value;
  const keepOriginals = useOriginals.value;
  exportBusy.value = true;
  exportNotice.value = null;
  try {
    // 先解字节再挂：组件内部是异步取图的，没有「渲染完了」这个信号。
    exportCache.value = await preloadAttachments(messages, {
      loadAttachment: props.loadAttachment,
      loadThumbnail: props.loadThumbnail,
      useOriginals: keepOriginals,
    });
    exportMessages.value = messages;
    await nextTick();
    const node = exportDocRef.value?.$el as HTMLElement | undefined;
    if (node === undefined) throw new Error("export document did not mount");
    // 等的不只是解码：组件要等字节到手才把 `<img>` 放进 DOM，
    // 而那是 `onMounted` 之后的事（这一条是 e2e 抓到的）。
    // 按消息分账地等每一张该出现的图，头像不算数（评审 P1）。
    const unplaced = await waitForExportMedia(node, {
      expected: expectedInlineMedia(messages, exportCache.value),
      timeoutMs: 8_000,
    });
    const base = exportFileBaseName(props.conversation?.display_name ?? "", new Date());
    exportNotice.value =
      format === "bundle"
        ? await exportAsBundle(node, messages, base, keepOriginals)
        : await exportAsImages(node, base, unplaced);
    sheetOpen.value = false;
    selection.exit();
  } catch {
    exportNotice.value = warnNotice("导出没能完成，请再试一次。");
  } finally {
    exportMessages.value = null;
    exportCache.value = new Map();
    exportBusy.value = false;
    exportProgress.value = null;
  }
};

/** 少了几张图的那句话。修得好的不吵，修不好的必须讲。 */
const missingNote = (missing: number): TranscriptNotice | null =>
  missing === 0 ? null : warnNotice(`已导出，但有 ${String(missing)} 张图片没能带上。`);

/**
 * 网页（压缩包）：网页 + `附件/`（PSU-2.179，原型 `zip-export.html` 经用户确认）。
 *
 * 1. 按清单取附件字节——预加载时已经取到的图直接复用，不重复下载；本机缓存被清掉的，
 *    接口自己会回源现解一份（「导出前先补取」就是这一步）。
 * 2. 屏外文档的克隆里把附件改成 `附件/` 下的相对地址，带上查看脚本。
 * 3. 打成 zip 下载。
 *
 * 返回要向用户说的那句话：全都带上了就不吵；缺了什么按类说清楚。
 */
const exportAsBundle = async (
  node: HTMLElement,
  messages: readonly RenderMessage[],
  base: string,
  keepOriginals: boolean,
): Promise<TranscriptNotice | null> => {
  const plan = planBundle(messages, {
    includeVideos: includeVideos.value,
    useOriginals: keepOriginals,
    voiceExtension: props.loadVoiceMp3 === undefined ? "wav" : "mp3",
  });
  const cached = exportCache.value;
  const fromCache =
    (load: (id: string) => Promise<Blob>) =>
    (id: string): Promise<Blob> => {
      const blob = cached.get(id);
      return blob === undefined ? load(id) : Promise.resolve(blob);
    };
  const thumbnail = props.loadThumbnail;
  const fetched = await fetchBundle(
    plan,
    {
      loadAttachment: fromCache(props.loadAttachment),
      loadThumbnail: thumbnail === undefined ? undefined : fromCache(thumbnail),
      loadVoiceMp3: props.loadVoiceMp3,
    },
    {
      useOriginals: keepOriginals,
      onProgress: (done, total) => {
        exportProgress.value = `正在取附件 ${String(done)}/${String(total)}…`;
      },
    },
  );
  exportProgress.value = "正在打包…";
  const durations = new Map<string, number | null>();
  for (const message of messages) {
    for (const attachment of message.attachments) {
      durations.set(attachment.id, attachment.duration_ms ?? null);
    }
  }
  let inlineFailed = 0;
  const html = await serializeExportDocument({
    node,
    title: props.conversation?.display_name ?? "聊天记录",
    messageCount: messages.length,
    styleSheets: documentStyleSheets(),
    // 表情与头像仍嵌在网页里；附件图在包里，不经过这一步。
    toDataUrl: (src, width) => blobSrcToDataUrl(src, width, { keepOriginal: keepOriginals }),
    generatedAt: new Date(),
    onReport: (report) => {
      inlineFailed += report.failed;
    },
    bundle: {
      probe: BUNDLE_PROBE_PATH,
      rewrite: (clone) => {
        rewriteForBundle(clone, {
          paths: new Map(fetched.fetched.map((item) => [item.entry.attachmentId, item.path])),
          missing: fetched.missingIds,
          excludedVideos: new Set(plan.excludedVideos),
          durations,
        });
      },
    },
  });
  downloadBlob(await packBundle(base, html, fetched.fetched), `${base}.zip`);
  const lacking = { ...fetched.missing };
  for (const kind of ["image", "audio", "video", "file"] as const) {
    lacking[kind] += plan.unavailable[kind];
  }
  // 画不成的动图表情、嵌不进去的头像/表情，也算「图片没带上」。
  lacking.image += inlineFailed + undrawnStickerCount(node);
  const words = describeMissing(lacking);
  return words === null ? null : warnNotice(`已导出，但有 ${words}没能带上。`);
};

const exportAsImages = async (
  node: HTMLElement,
  base: string,
  unplaced: number,
): Promise<TranscriptNotice | null> => {
  // 页脚的高度要从每一片里让出来，否则加上它的那张图会越过画布上限。
  const slices = planSlices(measureRows(node), MAX_SLICE_HEIGHT - SLICE_FOOTER_HEIGHT);
  const blobs = await renderSlices({
    node,
    slices,
    width: node.offsetWidth,
    caption: `和「${props.conversation?.display_name ?? "对方"}」的聊天记录`,
    render: defaultSliceRender(exportPaperColor()),
  });
  const packed = await packSlices(blobs, base);
  downloadBlob(packed.blob, packed.fileName);
  // 切成了几张要说：文件名里有，但用户此刻看的是这句话。
  const sliced =
    blobs.length === 1 ? null : `太长了，已切成 ${String(blobs.length)} 张打包在一起。`;
  // 画不成的动图表情在长图上是一块白，html-to-image 不会替它报数。
  const missing = unplaced + undrawnStickerCount(node);
  const lacking = missingNote(missing);
  if (lacking === null) return sliced === null ? null : { text: sliced, tone: "info" };
  return sliced === null ? lacking : { text: `${sliced}${lacking.text}`, tone: "warn" };
};

defineExpose({ scrollToIndex, scrollToMessage, scrollToLatest, captureReadingPosition });
</script>

<template>
  <section
    class="transcript"
    aria-label="消息转录"
  >
    <header class="transcript-head">
      <button
        v-if="conversation !== null"
        class="transcript-icon transcript-back"
        type="button"
        aria-label="返回上一层"
        @click="emit('back')"
      >
        ←
      </button>
      <div class="transcript-title">
        <strong data-feedback-mask="conversation-name">{{ conversation?.display_name ?? "会话转录" }}</strong>
        <!-- 定位状态的回程路径只在标题下露一行小字，不再用通栏横幅打断阅读。 -->
        <button
          v-if="searchReturnAvailable"
          class="transcript-locate-return"
          type="button"
          @click="emit('return-to-search')"
        >
          已定位到搜索结果 · 返回搜索
        </button>
        <button
          v-else-if="windowItems !== null"
          class="transcript-locate-return"
          type="button"
          @click="emit('exit-window')"
        >
          上下文片段 · 返回完整会话
        </button>
      </div>
      <div
        v-if="conversation !== null"
        class="transcript-more-wrap"
      >
        <button
          class="transcript-icon transcript-more"
          type="button"
          aria-label="更多"
          :aria-expanded="moreOpen"
          @click="moreOpen = !moreOpen"
        >
          <!-- 原型里的更多入口是三个等距圆点，不是排版出来的省略号。 -->
          <svg
            width="22"
            height="22"
            viewBox="0 0 24 24"
            fill="currentColor"
            aria-hidden="true"
          >
            <circle
              cx="5"
              cy="12"
              r="1.7"
            />
            <circle
              cx="12"
              cy="12"
              r="1.7"
            />
            <circle
              cx="19"
              cy="12"
              r="1.7"
            />
          </svg>
        </button>
        <div
          v-if="moreOpen"
          class="transcript-menu"
          role="menu"
          aria-label="会话只读操作"
        >
          <button
            v-if="searchAvailable !== false"
            type="button"
            role="menuitem"
            @click="moreOpen = false; emit('open-search')"
          >
            查找聊天记录
          </button>
          <button
            v-if="infoAvailable !== false"
            type="button"
            role="menuitem"
            :aria-current="infoOpen ? 'true' : undefined"
            @click="moreOpen = false; emit('open-info')"
          >
            会话信息
          </button>
          <button
            type="button"
            role="menuitem"
            :disabled="mediaItems.length === 0"
            @click="openFirstMedia"
          >
            查看媒体
          </button>
          <button
            v-if="exportAvailable !== false"
            type="button"
            role="menuitem"
            @click="moreOpen = false; emit('export')"
          >
            导出会话
          </button>
          <button
            v-if="selectableIds.size > 0"
            type="button"
            role="menuitem"
            data-conversation-action="select"
            @click="beginSelection"
          >
            选择消息
          </button>
        </div>
      </div>
    </header>

    <p
      v-if="errorText !== null"
      class="transcript-error"
      role="alert"
    >
      {{ errorText }}
    </p>

    <div
      v-if="conversation === null"
      class="transcript-empty"
    >
      <strong>选择左侧的会话开始阅读</strong>
      已归档的消息按导入时的时间顺序完整保留。
    </div>

    <template v-else-if="windowItems !== null">
      <div class="transcript-stream">
        <div
          ref="windowScrollRef"
          class="transcript-scroll"
          @scroll="onWindowScroll"
          @wheel.passive="closeRowMenu"
          @mousedown="marquee.onMouseDown"
          @click.capture="marquee.onClickCapture"
        >
          <div class="transcript-window-channel">
            <div
              v-for="row in windowRows"
              :key="row.message.id"
              class="transcript-window-row"
              :data-message-id="row.message.id"
              @contextmenu="openRowMenu($event, row.message.id)"
              @touchstart.passive="startRowPress($event, row.message.id)"
              @touchend.passive="cancelRowPress"
              @touchmove.passive="cancelRowPress"
              @touchcancel.passive="cancelRowPress"
              @click.capture="onRowClickCapture($event, row.message.id)"
            >
              <div
                v-if="row.dateLabel !== null"
                class="date-chip"
              >
                {{ row.dateLabel }}
              </div>
              <MessageTypeRenderer
                :message="row.message"
                :reply-preview="replyPreviewFor(row.message.reply_to_id, messagesById)"
                :highlighted="row.message.id === highlightedId"
                :show-sender-name="senderNamesShown"
                :selection-active="selection.active"
                :selected="selection.isPicked(row.message.id)"
                :selectable="selectableIds.has(row.message.id)"
                :load-attachment="loadAttachment"
                :load-thumbnail="loadThumbnail"
                :resolve-attachment="resolveAttachment"
                :sticker-fetch-available="stickerFetchAvailable"
                :zoom="activeZoom"
                :counterpart-name="counterpartName"
                :member-name="memberName"
                @jump-to-reply="emit('jump-to-reply', $event)"
                @retry-attachment="emit('retry-attachment', $event)"
                @enable-sticker-fetch="emit('enable-sticker-fetch')"
                @open-media="openMedia"
                @open-file="openFilePreview"
                @open-record="emit('open-record', $event)"
                @open-zoom="emit('open-zoom-layer', $event)"
                @close-zoom="emit('close-zoom-layer')"
              />
              <TranscriptTouchActions
                v-if="rowMenu?.touch && rowMenu.id === row.message.id"
                :selectable="selectableIds.has(row.message.id)"
                :self="row.message.is_self === true"
                @copy="copyOne(row.message.id)"
                @select="enterSelection(row.message.id)"
                @close="closeRowMenu"
              />
            </div>
          </div>
        </div>
        <p
          v-if="windowFetchingMore"
          class="transcript-hint transcript-loading-top"
          role="status"
        >
          正在加载更早消息……
        </p>
        <p
          v-if="windowFetchingNewer"
          class="transcript-hint transcript-loading-bottom"
          role="status"
        >
          正在加载更新消息……
        </p>
      </div>
    </template>

    <p
      v-else-if="loading"
      class="transcript-hint"
    >
      正在加载消息……
    </p>
    <div
      v-else-if="rows.length === 0"
      class="transcript-empty"
    >
      <strong>这个会话还没有归档消息</strong>
    </div>
    <template v-if="conversation !== null && rows.length > 0">
      <div
        v-show="windowItems === null"
        class="transcript-stream"
      >
        <p
          v-show="fetchingMore"
          class="transcript-hint transcript-loading-top"
        >
          正在加载更早消息……
        </p>
        <p
          v-show="!fetchingMore && !hasMore && importActive"
          class="transcript-hint transcript-loading-top"
          role="status"
          data-testid="import-boundary-hint"
        >
          更早的消息仍在导入，稍后回来可以看到更多。
        </p>
        <div
          v-show="!loading"
          ref="scrollRef"
          class="transcript-scroll"
          @scroll="onScroll"
          @wheel.passive="closeRowMenu"
          @mousedown="marquee.onMouseDown"
          @click.capture="marquee.onClickCapture"
        >
          <div
            class="transcript-virtual transcript-reading-channel"
            :style="{ height: `${String(totalSize)}px` }"
          >
            <div
              v-for="{ item, row } in virtualRows"
              :key="row.message.id"
              :ref="measureRow"
              :data-index="item.index"
              :data-message-id="row.message.id"
              class="transcript-row"
              :style="{ transform: `translateY(${String(item.start)}px)` }"
              @contextmenu="openRowMenu($event, row.message.id)"
              @touchstart.passive="startRowPress($event, row.message.id)"
              @touchend.passive="cancelRowPress"
              @touchmove.passive="cancelRowPress"
              @touchcancel.passive="cancelRowPress"
              @click.capture="onRowClickCapture($event, row.message.id)"
            >
              <div
                v-if="row.dateLabel !== null"
                class="date-chip"
              >
                {{ row.dateLabel }}
              </div>
              <MessageTypeRenderer
                :message="row.message"
                :reply-preview="replyPreviewFor(row.message.reply_to_id, messagesById)"
                :highlighted="row.message.id === highlightedId"
                :show-sender-name="senderNamesShown"
                :selection-active="selection.active"
                :selected="selection.isPicked(row.message.id)"
                :selectable="selectableIds.has(row.message.id)"
                :load-attachment="loadAttachment"
                :load-thumbnail="loadThumbnail"
                :resolve-attachment="resolveAttachment"
                :sticker-fetch-available="stickerFetchAvailable"
                :zoom="activeZoom"
                :counterpart-name="counterpartName"
                :member-name="memberName"
                @jump-to-reply="emit('jump-to-reply', $event)"
                @retry-attachment="emit('retry-attachment', $event)"
                @enable-sticker-fetch="emit('enable-sticker-fetch')"
                @open-media="openMedia"
                @open-file="openFilePreview"
                @open-record="emit('open-record', $event)"
                @open-zoom="emit('open-zoom-layer', $event)"
                @close-zoom="emit('close-zoom-layer')"
              />
              <TranscriptTouchActions
                v-if="rowMenu?.touch && rowMenu.id === row.message.id"
                :selectable="selectableIds.has(row.message.id)"
                :self="row.message.is_self === true"
                @copy="copyOne(row.message.id)"
                @select="enterSelection(row.message.id)"
                @close="closeRowMenu"
              />
            </div>
          </div>
        </div>
      </div>
      <!-- 与提示同一个取法：零高度的一层钉在滚动区底边，多选底栏出来时按钮浮在底栏上方，
           不会压在「复制」上。 -->
      <div
        v-if="windowItems === null && showScrollToBottom && !sheetOpen && !shareSheetOpen"
        class="scroll-to-bottom-layer"
      >
        <button
          class="scroll-to-bottom"
          type="button"
          aria-label="滚动到最新消息"
          title="滚动到最新消息"
          @click="scrollToLatest"
        >
          ↓
        </button>
      </div>
    </template>
    <ExportFormatSheet
      v-if="selection.active && sheetOpen"
      :format="exportFormat"
      :use-originals="useOriginals"
      :plan="bundlePlan"
      :include-videos="includeVideos"
      :voice-as-mp3="loadVoiceMp3 !== undefined"
      :progress="exportProgress"
      :count="pickedMessages.length"
      :image-count="pickedImageCount"
      :conversation-name="conversation?.display_name ?? ''"
      :busy="exportBusy"
      @update:format="exportFormat = $event"
      @update:use-originals="useOriginals = $event"
      @update:include-videos="includeVideos = $event"
      @confirm="runExport"
      @close="sheetOpen = false"
    />
    <ShareSheet
      v-if="selection.active && shareSheetOpen"
      :count="pickedMessages.length"
      :conversation-name="conversation?.display_name ?? ''"
      :state="shareState === 'not_configured' ? 'not_configured' : 'available'"
      :plan="sharePlan"
      :image-count="pickedImageCount"
      :expires-in-days="shareExpiry"
      :hide-names="shareHideNames"
      :use-originals="shareUseOriginals"
      :busy="shareBusy"
      :progress="shareProgress"
      :error="shareError"
      :result="shareResult"
      :configure-path="sharePairingPath"
      @update:expires-in-days="shareExpiry = $event"
      @update:hide-names="shareHideNames = $event"
      @update:use-originals="shareUseOriginals = $event"
      @confirm="runShareNow"
      @close="closeShareSheet"
    />
    <!-- 零高度的一层：提示浮在它上面，底栏在时浮在底栏上方，不在时浮在转录底边上方。 -->
    <div class="transcript-toast-layer">
      <div
        v-if="exportNotice !== null"
        class="transcript-toast"
        :class="{ 'is-info': exportNotice.tone === 'info' }"
        role="status"
        @mouseenter="noticeDismissal.pause"
        @mouseleave="noticeDismissal.resume"
      >
        <span>{{ exportNotice.text }}</span>
        <button
          type="button"
          class="transcript-toast-close"
          aria-label="关闭提示"
          @click="noticeDismissal.dismiss"
        >
          ✕
        </button>
      </div>
    </div>
    <!-- 顶栏在多选态下**原样不动**（PC 微信就是这样），退出只有这一条 ✕ 的路，
         「已选 N 条」跟着「导出」按钮走。完整转录与定位窗口共用这一条。 -->
    <TranscriptSelectionDock
      v-if="selection.active"
      :count="pickedMessages.length"
      :has-media="saveMediaReady"
      :busy="exportBusy || shareBusy"
      :can-share="canShare"
      @export="shareSheetOpen = false; sheetOpen = true"
      @share="openShareSheet"
      @copy="copySelection"
      @range="selection.selectRange(loadedOrder, selectableIds)"
      @exit="selection.exit()"
    />
    <!-- 右键/长按那张菜单：开在光标处（交互第二版）。「多选」仍是这个功能的入口之一，
         另一个是在空白处直接拉框。 -->
    <div
      v-if="rowMenu !== null && !rowMenu.touch"
      ref="rowMenuRef"
      class="row-menu"
      role="menu"
      aria-label="消息操作"
      :style="rowMenuStyle"
      @contextmenu.prevent
    >
      <button
        data-feedback-action="复制消息"
        type="button"
        role="menuitem"
        data-row-action="copy"
        @click="copyOne(rowMenu.id)"
      >
        复制
      </button>
      <button
        data-feedback-action="多选"
        type="button"
        role="menuitem"
        data-row-action="select"
        :disabled="!selectableIds.has(rowMenu.id)"
        @click="enterSelection(rowMenu.id)"
      >
        多选
      </button>
    </div>
    <div
      v-if="marquee.boxStyle.value !== null"
      class="transcript-marquee"
      aria-hidden="true"
      :style="marquee.boxStyle.value"
    />
    <!-- 屏外那份完整渲染的导出文档（PSU-2.168）。
         **不能 display:none**：不在文档里读到的计算样式全是默认值，
         收出来会是一张白纸。 -->
    <Teleport to="body">
      <div
        v-if="exportMessages !== null"
        data-feedback-mask="message-body"
        class="export-offscreen"
        aria-hidden="true"
      >
        <ExportDocument
          ref="exportDocRef"
          :messages="exportMessages"
          :show-sender-names="senderNamesShown"
          :load-attachment="exportLoader"
          :expand-records="exportFormat === 'image'"
        />
      </div>
    </Teleport>
    <MediaViewer
      :open="mediaViewerOpen"
      :items="mediaItems"
      :initial-index="mediaViewerIndex"
      :load-attachment="loadAttachment"
      :load-thumbnail="loadThumbnail"
      :scope-notice="mediaScopeNotice"
      @close="closeMedia"
    />
    <FilePreviewViewer
      :open="filePreview !== null"
      :attachment="filePreview?.attachment ?? null"
      :sender-name="filePreview?.senderName ?? ''"
      :created-at="filePreview?.createdAt ?? null"
      :load-attachment="loadAttachment"
      @close="closeFilePreview"
      @download="downloadFromPreview"
    />
  </section>
</template>

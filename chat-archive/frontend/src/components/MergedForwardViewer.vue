<script setup lang="ts">
import { computed, ref, shallowReactive, toRef, watch } from "vue";

import type { AttachmentSummary, MessageSummary } from "@/api/archiveApi";
import FilePreviewViewer from "@/components/FilePreviewViewer.vue";
import MediaViewer from "@/components/MediaViewer.vue";
import MergedForwardItems from "@/components/MergedForwardItems.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import { downloadBlob } from "@/lib/filePreview/download";
import {
  mergedForwardCopyText,
  mergedForwardFlatItems,
  mergedForwardTitle,
} from "@/lib/mergedForward";

type AppCard = NonNullable<MessageSummary["app_card"]>;

// Structurally the MediaViewer's own MediaViewerItem, kept local for the same
// reason MessageTranscript keeps its ViewerItem local: a type imported from a
// <script setup> .vue is fragile, and structural typing makes this compatible.
interface ViewerItem {
  attachment: AttachmentSummary;
  senderName: string;
  createdAt: number;
}

const props = withDefaults(
  defineProps<{
    open: boolean;
    card: AppCard;
    // The record message's attachments and the view's loaders. Absent on an end
    // that holds the record but not the bytes; the items then render honest
    // placeholders and no viewer is reachable.
    attachments?: AttachmentSummary[];
    loadAttachment?: ((attachmentId: string) => Promise<Blob>) | undefined;
    loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
    resolveAttachment?: ((attachmentId: string) => Promise<AttachmentSummary>) | undefined;
    stickerFetchAvailable?: boolean;
    /**
     * 灯箱与文件预览也归路由管（PSU-2.120），和转录里那两层同一套约定：
     * 开一层压一条历史，手机上的系统「返回」才只关这一层——否则那一下返回
     * 会把整个合并转发层一起收掉，读者被弹回聊天记录。
     */
    activeMediaId?: string | null;
    activeFileId?: string | null;
  }>(),
  {
    attachments: () => [],
    loadAttachment: undefined,
    loadThumbnail: undefined,
    resolveAttachment: undefined,
    stickerFetchAvailable: false,
    activeMediaId: null,
    activeFileId: null,
  },
);
const emit = defineEmits<{
  close: [];
  "open-media-layer": [attachmentId: string];
  "close-media-layer": [];
  "open-file-layer": [attachmentId: string];
  "close-file-layer": [];
  "enable-sticker-fetch": [];
}>();

const moreOpen = ref(false);
const copyStatus = ref<string | null>(null);
const openRef = toRef(props, "open");
const close = (): void => emit("close");

// The drill-in stack, WeChat style: a nested record opens as a new level of
// this same page, and the back control returns one level before it closes the
// viewer. The title travels with the card because a card derived before
// APP_CARD_VERSION 5 carries the flattened digest as its title — the item's
// datatitle, resolved by the list, is the honest one.
interface DrillLevel {
  card: AppCard;
  title: string;
}
const stack = ref<DrillLevel[]>([]);
const currentCard = computed<AppCard>(() => stack.value.at(-1)?.card ?? props.card);
const currentTitle = computed(
  () => stack.value.at(-1)?.title ?? mergedForwardTitle(props.card),
);
const scrollBody = ref<HTMLElement | null>(null);

const openRecord = (card: AppCard, title: string): void => {
  stack.value = [...stack.value, { card, title }];
  if (scrollBody.value !== null) scrollBody.value.scrollTop = 0;
};

// Escape and the back control agree: pop a level, close only from the root.
const goBack = (): void => {
  if (stack.value.length === 0) {
    close();
    return;
  }
  stack.value = stack.value.slice(0, -1);
};

const modal = useModalFocus(openRef, goBack);
const itemCountLabel = computed(() => `${String(currentCard.value.item_total)} 条内容`);

// Forward media may upload a thumbnail as its main artifact, without a second
// thumbnail object. Both workspaces use the main bytes on that explicit miss.
const inlineLoad = computed(() => {
  const loadAttachment = props.loadAttachment;
  const loadThumbnail = props.loadThumbnail;
  if (loadAttachment === undefined || loadThumbnail === undefined) return loadAttachment;
  return async (attachmentId: string): Promise<Blob> => {
    try {
      return await loadThumbnail(attachmentId);
    } catch (error: unknown) {
      if (
        typeof error !== "object" || error === null ||
        !("code" in error) || error.code !== "THUMBNAIL_UNAVAILABLE"
      ) throw error;
      return loadAttachment(attachmentId);
    }
  };
});

const resolvedAttachments = shallowReactive(new Map<string, AttachmentSummary>());
const displayedAttachments = computed(() => props.attachments.map(
  (attachment) => resolvedAttachments.get(attachment.id) ?? attachment,
));
const resolveForwardAttachment = computed(() => {
  const resolve = props.resolveAttachment;
  if (resolve === undefined) return undefined;
  return async (id: string): Promise<AttachmentSummary> => {
    const owner = props.card;
    const resolved = await resolve(id);
    if (props.open && props.card === owner) resolvedAttachments.set(id, resolved);
    return resolved;
  };
});
watch(() => [props.card, props.open] as const, () => resolvedAttachments.clear());
const attachmentsById = computed(
  () => new Map(displayedAttachments.value.map((attachment) => [attachment.id, attachment])),
);

// The forward's own image/video gallery, in item order, for the shared
// MediaViewer. Correlated back to each item so the viewer header can name the
// original sender.
const mediaViewerItems = computed<ViewerItem[]>(() => {
  const items: ViewerItem[] = [];
  for (const item of mergedForwardFlatItems(props.card)) {
    if (item.attachment_id === null) continue;
    const attachment = attachmentsById.value.get(item.attachment_id);
    if (
      attachment !== undefined &&
      attachment.content_available &&
      (attachment.kind === "image" || attachment.kind === "video" || attachment.kind === "emoji")
    ) {
      items.push({
        attachment,
        senderName: item.sender ?? "未知成员",
        createdAt: item.created_at ?? 0,
      });
    }
  }
  return items;
});

const mediaViewerOpen = ref(false);
const mediaViewerIndex = ref(0);
const filePreview = ref<{ attachment: AttachmentSummary; senderName: string; createdAt: number } | null>(
  null,
);

const showMedia = (attachmentId: string): boolean => {
  const index = mediaViewerItems.value.findIndex(
    (entry) => entry.attachment.id === attachmentId,
  );
  if (index < 0) return false;
  mediaViewerIndex.value = index;
  mediaViewerOpen.value = true;
  return true;
};

const openMedia = (attachmentId: string): void => {
  if (showMedia(attachmentId)) emit("open-media-layer", attachmentId);
};

const closeMedia = (): void => {
  mediaViewerOpen.value = false;
  emit("close-media-layer");
};

const showFilePreview = (attachmentId: string): boolean => {
  for (const item of mergedForwardFlatItems(props.card)) {
    if (item.attachment_id !== attachmentId) continue;
    const attachment = attachmentsById.value.get(attachmentId);
    if (attachment === undefined) return false;
    filePreview.value = {
      attachment,
      senderName: item.sender ?? "未知成员",
      createdAt: item.created_at ?? 0,
    };
    return true;
  }
  return false;
};

const openFile = (attachmentId: string): void => {
  if (showFilePreview(attachmentId)) emit("open-file-layer", attachmentId);
};

const closeFile = (): void => {
  filePreview.value = null;
  emit("close-file-layer");
};

// 路由不再点名这一层，这一层就收掉：系统「返回」与换会话走的都是这一步。
watch(
  [() => props.activeMediaId, mediaViewerItems],
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
  [() => props.activeFileId, () => props.card],
  ([attachmentId]) => {
    if (attachmentId === null) {
      filePreview.value = null;
      return;
    }
    if (!showFilePreview(attachmentId)) filePreview.value = null;
  },
  { immediate: true },
);

const downloadFromPreview = async (attachmentId: string): Promise<void> => {
  const target = filePreview.value;
  if (target === null || props.loadAttachment === undefined) return;
  downloadBlob(await props.loadAttachment(attachmentId), target.attachment.file_name ?? "attachment");
};

watch(openRef, () => {
  moreOpen.value = false;
  copyStatus.value = null;
  mediaViewerOpen.value = false;
  filePreview.value = null;
  stack.value = [];
});

const copySummary = async (): Promise<void> => {
  try {
    await globalThis.navigator.clipboard.writeText(
      mergedForwardCopyText(currentCard.value, currentTitle.value),
    );
    copyStatus.value = "摘要已复制";
    moreOpen.value = false;
  } catch {
    copyStatus.value = "浏览器未允许复制摘要";
  }
};
</script>

<template>
  <div
    v-if="open"
    :ref="modal.container"
    class="merged-forward-layer"
    role="dialog"
    aria-modal="true"
    aria-label="合并转发详情"
    @click.self="close"
    @keydown="modal.onKeydown"
  >
    <article class="merged-forward-viewer">
      <header class="merged-forward-viewer-head">
        <button
          :ref="modal.initial"
          class="merged-forward-back"
          type="button"
          :aria-label="stack.length === 0 ? '返回会话' : '返回上一层'"
          @click="goBack"
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          >
            <path d="m15 18-6-6 6-6" />
          </svg>
        </button>
        <div class="merged-forward-viewer-title">
          <strong data-feedback-mask="message-body">{{ currentTitle }}</strong>
          <small>合并转发 · 只读归档</small>
        </div>
        <div class="merged-forward-more-wrap">
          <button
            class="merged-forward-more"
            type="button"
            aria-label="更多只读操作"
            :aria-expanded="moreOpen"
            @click="moreOpen = !moreOpen"
          >
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
            class="merged-forward-actions-layer"
            @click.self="moreOpen = false"
          >
            <section
              class="merged-forward-actions"
              aria-label="合并转发只读操作"
            >
              <strong class="merged-forward-actions-title">更多操作</strong>
              <button
                data-feedback-action="复制摘要"
                class="merged-forward-copy"
                type="button"
                @click="copySummary"
              >
                <span>复制摘要</span>
                <small>复制标题和当前已保存条目的纯文本</small>
              </button>
              <button
                class="merged-forward-export"
                type="button"
                disabled
                title="当前导出契约不支持精确消息范围"
              >
                <span>暂不支持精确导出</span>
                <small>需先扩展 ExportScope 的消息范围契约</small>
              </button>
              <button
                class="merged-forward-actions-cancel"
                type="button"
                @click="moreOpen = false"
              >
                取消
              </button>
            </section>
          </div>
        </div>
      </header>

      <p
        v-if="copyStatus !== null"
        class="merged-forward-notice"
        role="status"
      >
        {{ copyStatus }}
      </p>

      <div
        ref="scrollBody"
        class="merged-forward-viewer-scroll"
      >
        <p class="merged-forward-count">
          {{ itemCountLabel }} · 保留原发送者与时间
        </p>
        <MergedForwardItems
          :card="currentCard"
          :attachments="displayedAttachments"
          :load="inlineLoad"
          :load-sticker="loadAttachment"
          :resolve-attachment="resolveForwardAttachment"
          :sticker-fetch-available="stickerFetchAvailable"
          @open-media="openMedia"
          @open-file="openFile"
          @open-record="openRecord"
          @enable-sticker-fetch="emit('enable-sticker-fetch')"
        />
        <p
          v-if="currentCard.truncated"
          class="merged-forward-truncated merged-forward-viewer-truncated"
        >
          部分内容未完整保存
        </p>
      </div>
    </article>

    <MediaViewer
      v-if="loadAttachment !== undefined"
      :open="mediaViewerOpen"
      :items="mediaViewerItems"
      :initial-index="mediaViewerIndex"
      :load-attachment="loadAttachment"
      :load-thumbnail="loadThumbnail"
      @close="closeMedia"
    />
    <FilePreviewViewer
      v-if="loadAttachment !== undefined"
      :open="filePreview !== null"
      :attachment="filePreview?.attachment ?? null"
      :sender-name="filePreview?.senderName ?? ''"
      :created-at="filePreview?.createdAt ?? null"
      :load-attachment="loadAttachment"
      @close="closeFile"
      @download="downloadFromPreview"
    />
  </div>
</template>

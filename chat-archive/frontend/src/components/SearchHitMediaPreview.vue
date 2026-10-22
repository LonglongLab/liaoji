<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";

import type { SearchHit } from "@/api/archiveApi";
import { isSearchHitPreviewable } from "@/lib/searchHitMedia";

const props = defineProps<{
  hit: SearchHit;
  loadAttachment: ((attachmentId: string) => Promise<Blob>) | undefined;
  /** 图片命中优先取缩略图字节；取不到静默回退 `loadAttachment`。 */
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
}>();

const emit = defineEmits<{
  "open-media": [];
}>();

const rootRef = ref<HTMLElement | null>(null);
const previewUrl = ref<string | null>(null);
const loading = ref(false);
const errorText = ref<string | null>(null);
const duration = ref<number | null>(null);
let observer: IntersectionObserver | null = null;
let loadedAttachmentId: string | null = null;

const formatDuration = (value: number | null): string | null => {
  if (value === null || !Number.isFinite(value) || value < 0) return null;
  const total = Math.round(value);
  return `${String(Math.floor(total / 60))}:${String(total % 60).padStart(2, "0")}`;
};

const revokePreview = (): void => {
  if (previewUrl.value !== null) URL.revokeObjectURL(previewUrl.value);
  previewUrl.value = null;
  loadedAttachmentId = null;
  duration.value = null;
};

const loadPreview = async (): Promise<void> => {
  const attachmentId = props.hit.attachment_id ?? null;
  if (
    attachmentId === null ||
    !isSearchHitPreviewable(props.hit) ||
    props.loadAttachment === undefined
  ) {
    return;
  }
  if (loadedAttachmentId === attachmentId && previewUrl.value !== null) return;
  loading.value = true;
  errorText.value = null;
  try {
    revokePreview();
    previewUrl.value = URL.createObjectURL(await loadPreviewBlob(attachmentId));
    loadedAttachmentId = attachmentId;
  } catch {
    errorText.value = "媒体预览暂不可用";
  } finally {
    loading.value = false;
  }
};

const loadPreviewBlob = async (attachmentId: string): Promise<Blob> => {
  if (props.loadThumbnail !== undefined && props.hit.attachment_kind === "image") {
    try {
      return await props.loadThumbnail(attachmentId);
    } catch {
      // 没有缩略图不是故障；原图能开就开。
    }
  }
  if (props.loadAttachment === undefined) throw new Error("附件加载器不可用");
  return props.loadAttachment(attachmentId);
};

const activate = (): void => {
  if (previewUrl.value !== null && isSearchHitPreviewable(props.hit)) {
    emit("open-media");
    return;
  }
  void loadPreview();
};

const onKeydown = (event: KeyboardEvent): void => {
  if (event.key !== "Enter" && event.key !== " ") return;
  event.preventDefault();
  activate();
};

const startObservedLoading = (): void => {
  if (!isSearchHitPreviewable(props.hit) || props.loadAttachment === undefined) return;
  if (
    typeof IntersectionObserver === "undefined" ||
    rootRef.value === null ||
    !rootRef.value.isConnected
  ) {
    void loadPreview();
    return;
  }
  observer = new IntersectionObserver(
    (entries) => {
      if (entries.some((entry) => entry.isIntersecting)) {
        observer?.disconnect();
        observer = null;
        void loadPreview();
      }
    },
    { rootMargin: "160px" },
  );
  observer.observe(rootRef.value);
};

watch(
  () => [props.hit.message_id, props.hit.attachment_id] as const,
  () => {
    observer?.disconnect();
    observer = null;
    revokePreview();
    errorText.value = null;
    startObservedLoading();
  },
);

onMounted(startObservedLoading);

onBeforeUnmount(() => {
  observer?.disconnect();
  revokePreview();
});
</script>

<template>
  <div
    v-if="hit.attachment_id != null"
    ref="rootRef"
    class="search-hit-media"
    :class="{ 'is-video': hit.attachment_kind === 'video', 'is-disabled': !isSearchHitPreviewable(hit) }"
    role="button"
    tabindex="0"
    :aria-label="hit.attachment_kind === 'video' ? '播放视频预览' : '查看图片预览'"
    @click.stop.prevent="activate"
    @keydown.stop="onKeydown"
  >
    <img
      v-if="hit.attachment_kind === 'image' && previewUrl !== null"
      class="search-hit-media-image"
      :src="`${previewUrl}#t=0.001`"
      :alt="hit.attachment_file_name ?? '图片缩略图'"
      loading="lazy"
      @error="errorText = '媒体内容无法解码'"
    >
    <video
      v-else-if="hit.attachment_kind === 'video' && previewUrl !== null"
      class="search-hit-media-video"
      :src="previewUrl"
      muted
      playsinline
      preload="metadata"
      tabindex="-1"
      @loadedmetadata="duration = Number.isFinite(($event.target as HTMLVideoElement).duration) ? ($event.target as HTMLVideoElement).duration : null"
      @error="errorText = '媒体内容无法解码'"
    />
    <span
      v-else
      class="search-hit-media-placeholder"
      role="status"
    >
      {{ loading ? "正在加载预览……" : errorText ?? (isSearchHitPreviewable(hit) ? "加载预览" : "媒体暂不可用") }}
    </span>
    <span
      v-if="hit.attachment_kind === 'video' && previewUrl !== null"
      class="search-hit-media-play"
      aria-hidden="true"
    >
      ▶
    </span>
    <span
      v-if="hit.attachment_kind === 'video' && formatDuration(duration) !== null"
      class="search-hit-media-duration"
    >
      {{ formatDuration(duration) }}
    </span>
  </div>
</template>

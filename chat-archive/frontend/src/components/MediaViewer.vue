<script setup lang="ts">
import MiddleFilename from "@/components/MiddleFilename.vue";
import AppSelect from "@/components/ui/AppSelect.vue";
import { computed, onBeforeUnmount, reactive, ref, toRef, watch } from "vue";

import type { AttachmentSummary } from "@/api/archiveApi";
import { useModalFocus } from "@/composables/useModalFocus";
import { useSwipeBackGuard } from "@/composables/useSwipeBackGuard";
import { useZoom } from "@/composables/useZoom";
import { attachmentVariantWord } from "@/lib/attachmentStatus";
import {
  MEDIA_SNIFF_BYTES,
  mediaDownloadName,
  sniffMediaExtension,
} from "@/lib/mediaFileName";
import { useWorkspace } from "@/lib/workspaceContext";
import { isWxgfAnimation, wxgfFirstFramePng } from "@/lib/wxgf/toBlob";

// 同一格数据、两端两句话：本机是「原图被配额淘汰了」，远程是「原图还没同步
// 上来，同步一次就有」（PSU-2.99 批 C / C0）。
const variantWord = attachmentVariantWord(useWorkspace());

export interface MediaViewerItem {
  attachment: AttachmentSummary;
  senderName: string;
  createdAt: number;
}

const props = withDefaults(
  defineProps<{
    open: boolean;
    items: MediaViewerItem[];
    initialIndex: number;
    scopeNotice?: string | null;
    loadAttachment: (attachmentId: string) => Promise<Blob>;
    /**
     * 打开大图的瞬间先把缩略图铺上当占位（列表通常已把它装进缓存，几乎即时），
     * 原图到达后替换——消掉「正在加载媒体……」的空屏。取不到就退回文字占位。
     */
    loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
  }>(),
  { scopeNotice: null, loadThumbnail: undefined },
);

const emit = defineEmits<{ close: [] }>();

const activeIndex = ref(props.initialIndex);
const previewUrls = reactive<Record<string, string>>({});
const placeholderUrls = reactive<Record<string, string>>({});
const loading = ref(false);
const errorText = ref<string | null>(null);
const video = ref<HTMLVideoElement | null>(null);
const image = ref<HTMLImageElement | null>(null);
const playbackRate = ref(1);

const current = computed(() => props.items[activeIndex.value] ?? null);
const currentUrl = computed(() =>
  current.value === null
    ? null
    : previewUrls[current.value.attachment.id] ??
      placeholderUrls[current.value.attachment.id] ??
      null,
);
/**
 * 下载永远只挂原图字节：占位缩略图顶着「下载原文件」的名义存下来，
 * 是把 6 KB 说成了原件。原图没到之前不出下载入口。
 */
const downloadUrl = computed(() =>
  current.value === null ? null : previewUrls[current.value.attachment.id] ?? null,
);
/** 认出来的扩展名，按附件标识存；认不出就没有这个键。 */
const sniffedExtensions = reactive<Record<string, string>>({});
const downloadName = computed(() =>
  current.value === null
    ? ""
    : mediaDownloadName(
        current.value.attachment,
        current.value.createdAt,
        sniffedExtensions[current.value.attachment.id] ?? null,
      ),
);
const isImage = computed(() => current.value?.attachment.kind === "image");

/**
 * 图片与文档预览用同一套缩放：Ctrl/⌘ + 滚轮、Ctrl/⌘ 加减号、双击切换、
 * 触摸捏合，放大后拖着看。视频没有放大的必要，所以只对图片开。
 */
const zoom = useZoom({
  isActive: () => props.open && isImage.value && currentUrl.value !== null,
  // 图片按 contain 摆放，真正画出来的画面往往比 stage 小；按框算会让人把图拖进黑边里。
  contentSize: () => {
    const element = image.value;
    if (element === null || element.naturalWidth === 0) return null;
    const ratio = Math.min(
      element.clientWidth / element.naturalWidth,
      element.clientHeight / element.naturalHeight,
    );
    return {
      width: element.naturalWidth * ratio,
      height: element.naturalHeight * ratio,
    };
  },
});

const close = (): void => emit("close");
const openRef = toRef(props, "open");
const { container, initial, onKeydown: onModalKeydown } = useModalFocus(openRef, close);
void container;
void initial;

/**
 * 舞台自己写着 `touch-action: none`，横滑到不了浏览器手里；但头部、底栏
 * 和浮层边框上的横滑还是会一路滚到 document，被当成「历史返回」。
 * 和文件预览层同一道锁。
 */
useSwipeBackGuard(openRef);

const clampIndex = (value: number): number => {
  if (props.items.length === 0) return 0;
  return Math.min(Math.max(value, 0), props.items.length - 1);
};


const loadCurrent = async (): Promise<void> => {
  const item = current.value;
  if (item === null) return;
  const attachmentId = item.attachment.id;
  if (previewUrls[attachmentId] !== undefined) return;
  loading.value = true;
  errorText.value = null;
  if (
    props.loadThumbnail !== undefined &&
    item.attachment.kind === "image" &&
    placeholderUrls[attachmentId] === undefined
  ) {
    // 与原图加载并行；晚到、没有缩略图都无所谓——占位是纯增益。
    void props
      .loadThumbnail(attachmentId)
      .then((blob) => {
        if (previewUrls[attachmentId] === undefined) {
          placeholderUrls[attachmentId] = URL.createObjectURL(blob);
        }
      })
      .catch(() => undefined);
  }
  try {
    const loaded = await props.loadAttachment(attachmentId);
    // 加载器已把静态 wxgf 解成了 PNG；动图（只该出现在表情上）在灯箱里看第一帧，
    // 下载得到的也是这张 PNG——wxgf 在任何看图软件里都打不开（PSU-2.146）。
    const blob = isWxgfAnimation(loaded) ? await wxgfFirstFramePng(loaded) : loaded;
    // 远端的附件没有 mime（`attachment_refs` 是一列无类型标识），对象存储也一律
    // 答 `binary/octet-stream`，所以 `blob.type` 帮不上忙——扩展名只能从字节头认。
    // 认一次记一次：下载名是 computed，不能在里面等一个 Promise。
    const extension = sniffMediaExtension(
      new Uint8Array(await blob.slice(0, MEDIA_SNIFF_BYTES).arrayBuffer()),
    );
    if (extension !== null) sniffedExtensions[attachmentId] = extension;
    previewUrls[attachmentId] = URL.createObjectURL(blob);
  } catch {
    const reason = item.attachment.failure_code;
    errorText.value =
      reason === null
        ? "暂时无法打开媒体，请重试。"
        : `暂时无法打开媒体（${reason}），请重试。`;
  } finally {
    loading.value = false;
  }
};

const retry = async (): Promise<void> => {
  const item = current.value;
  if (item === null) return;
  const previous = previewUrls[item.attachment.id];
  if (previous !== undefined) URL.revokeObjectURL(previous);
  Reflect.deleteProperty(previewUrls, item.attachment.id);
  await loadCurrent();
};

const move = (delta: number): void => {
  const next = clampIndex(activeIndex.value + delta);
  if (next !== activeIndex.value) activeIndex.value = next;
};

const setPlaybackRate = (value: number): void => {
  playbackRate.value = value;
  if (video.value !== null) video.value.playbackRate = value;
};

/**
 * 触摸只留两件这套指针事件覆盖不到的事：双指捏合交给 `useZoom`，
 * 未放大时的单指滑动用来翻页。放大之后单指就是拖着看（走 pointer 事件，
 * 鼠标与触摸同一条路），此时不再抢去翻页。
 */
let swipeFromX = 0;
let swipeFromY = 0;
let swiping = false;

const onTouchStart = (event: TouchEvent): void => {
  zoom.onTouchStart(event);
  const first = event.touches.item(0);
  swiping = event.touches.length === 1 && !zoom.pannable.value && first !== null;
  if (first !== null) {
    swipeFromX = first.clientX;
    swipeFromY = first.clientY;
  }
};

const onTouchMove = (event: TouchEvent): void => {
  zoom.onTouchMove(event);
};

const onTouchEnd = (event: TouchEvent): void => {
  zoom.onTouchEnd();
  const ended = event.changedTouches.item(0);
  if (swiping && ended !== null) {
    const deltaX = ended.clientX - swipeFromX;
    const deltaY = ended.clientY - swipeFromY;
    if (Math.abs(deltaX) >= 50 && Math.abs(deltaX) > Math.abs(deltaY)) {
      move(deltaX > 0 ? -1 : 1);
    }
  }
  swiping = false;
};

const onWindowKeydown = (event: KeyboardEvent): void => {
  if (!props.open || event.defaultPrevented) return;
  if (event.key === "Escape") {
    event.preventDefault();
    close();
  } else if (event.key === "ArrowLeft") {
    event.preventDefault();
    move(-1);
  } else if (event.key === "ArrowRight") {
    event.preventDefault();
    move(1);
  }
};

const onDialogKeydown = (event: KeyboardEvent): void => {
  if (rateMenuKeys.has(event)) return;
  if (event.key === "Escape") {
    event.preventDefault();
    close();
    return;
  }
  if (event.key === "Tab") onModalKeydown(event);
  else onWindowKeydown(event);
};

// Select consumes Escape even when closed. Remember the visible menu before
// its handler runs: first Escape dismisses it, the next closes the viewer.
const rateMenuKeys = new WeakSet<KeyboardEvent>();
const onDialogKeydownCapture = (event: KeyboardEvent): void => {
  if (event.key !== "Escape") return;
  if (container.value?.querySelector('.media-viewer-rate [aria-expanded="true"]')) {
    rateMenuKeys.add(event);
  } else {
    event.preventDefault();
    event.stopPropagation();
    close();
  }
};

watch(
  () => [props.open, props.initialIndex, props.items.length] as const,
  ([open, initialIndex]) => {
    if (!open) return;
    activeIndex.value = clampIndex(initialIndex);
    void loadCurrent();
  },
  { immediate: true },
);

watch(activeIndex, () => {
  zoom.reset();
  playbackRate.value = 1;
  if (props.open) void loadCurrent();
});

if (typeof globalThis !== "undefined") {
  globalThis.addEventListener("keydown", onWindowKeydown);
}

onBeforeUnmount(() => {
  if (typeof globalThis !== "undefined") {
    globalThis.removeEventListener("keydown", onWindowKeydown);
  }
  for (const url of Object.values(previewUrls)) URL.revokeObjectURL(url);
  for (const url of Object.values(placeholderUrls)) URL.revokeObjectURL(url);
});
</script>

<template>
  <div
    v-if="open"
    class="media-viewer-backdrop"
    role="dialog"
    aria-modal="true"
    aria-label="媒体查看器"
    @click.self="close"
  >
    <section
      ref="container"
      class="media-viewer"
      @keydown.capture="onDialogKeydownCapture"
      @keydown.stop="onDialogKeydown"
    >
      <header class="media-viewer-head">
        <button
          ref="initial"
          class="media-viewer-close"
          type="button"
          aria-label="关闭媒体查看器"
          @click="close"
        >
          ←
        </button>
        <div class="media-viewer-title">
          <strong data-feedback-mask="file-name"><MiddleFilename :name="current?.attachment.file_name ?? '媒体'" /></strong>
          <small
            v-if="current !== null"
            data-feedback-mask="sender-name"
          >
            {{ current.senderName }} · {{ activeIndex + 1 }} / {{ items.length }}
          </small>
        </div>
        <a
          v-if="current !== null && downloadUrl !== null"
          data-feedback-mask="file-name"
          class="media-viewer-download"
          :href="downloadUrl"
          :download="downloadName"
        >
          {{ current.attachment.variant === "thumbnail" ? "下载当前文件" : "下载原文件" }}
        </a>
        <span
          v-else
          class="media-viewer-head-spacer"
          aria-hidden="true"
        />
      </header>

      <div
        class="media-viewer-stage"
        :class="{ 'is-pannable': zoom.pannable.value, 'is-dragging': zoom.dragging.value }"
        @touchstart="onTouchStart"
        @touchmove="onTouchMove"
        @touchend="onTouchEnd"
        @touchcancel="onTouchEnd"
        @pointerdown="zoom.onPointerDown"
        @pointermove="zoom.onPointerMove"
        @pointerup="zoom.onPointerUp"
        @pointercancel="zoom.onPointerUp"
        @dblclick="zoom.toggle"
      >
        <button
          v-if="items.length > 1"
          class="media-viewer-nav is-prev"
          type="button"
          aria-label="上一页"
          :disabled="activeIndex === 0"
          @click="move(-1)"
        >
          ‹
        </button>
        <p
          v-if="loading && currentUrl === null"
          class="media-viewer-status"
          role="status"
        >
          正在加载媒体……
        </p>
        <p
          v-else-if="errorText !== null"
          class="media-viewer-status is-error"
          role="alert"
        >
          {{ errorText }}
          <button
            class="text-action media-viewer-retry"
            type="button"
            @click="retry"
          >
            重试
          </button>
        </p>
        <img
          v-else-if="current?.attachment.kind === 'image' && currentUrl !== null"
          ref="image"
          class="media-viewer-image"
          :src="currentUrl"
          :alt="current.attachment.file_name ?? '图片附件'"
          :style="{
            transform: zoom.transform.value,
            transition: zoom.dragging.value ? 'none' : 'transform 0.16s ease-out',
          }"
          draggable="false"
        >
        <video
          v-else-if="current?.attachment.kind === 'video' && currentUrl !== null"
          ref="video"
          class="media-viewer-video"
          :src="`${currentUrl}#t=0.001`"
          controls
          autoplay
          playsinline
          @loadedmetadata="setPlaybackRate(playbackRate)"
        />
        <p
          v-else
          class="media-viewer-status"
        >
          当前媒体不可用。
        </p>
        <button
          v-if="items.length > 1"
          class="media-viewer-nav is-next"
          type="button"
          aria-label="下一页"
          :disabled="activeIndex === items.length - 1"
          @click="move(1)"
        >
          ›
        </button>
      </div>

      <footer class="media-viewer-foot">
        <div class="media-viewer-foot-copy">
          <span v-if="current?.attachment.variant === 'thumbnail'">{{ variantWord }}</span>
          <span v-else-if="zoom.pannable.value">拖动可平移 · 双击复原 · Esc 关闭</span>
          <span v-else-if="isImage && currentUrl !== null">
            Ctrl + 滚轮缩放 · 双击放大 · ← / → 切换
          </span>
          <span v-else>按 Esc 关闭 · ← / → 切换</span>
          <small v-if="scopeNotice !== null">{{ scopeNotice }}</small>
        </div>
        <!-- 和文件预览器同一套：档位阶梯、点百分比回 100%、Ctrl 滚轮/加减号 -->
        <div
          v-if="isImage && currentUrl !== null"
          class="media-viewer-zoom"
          role="group"
          aria-label="图片缩放"
        >
          <button
            type="button"
            title="缩小（Ctrl 减号 / Ctrl + 滚轮）"
            aria-label="缩小图片"
            :disabled="!zoom.canZoomOut.value"
            @click="zoom.zoomOut"
          >
            −
          </button>
          <button
            type="button"
            title="恢复 100%（Ctrl 0）"
            aria-label="重置图片缩放"
            :disabled="zoom.isDefault.value"
            @click="zoom.reset"
          >
            {{ zoom.label.value }}
          </button>
          <button
            type="button"
            title="放大（Ctrl 加号 / Ctrl + 滚轮）"
            aria-label="放大图片"
            :disabled="!zoom.canZoomIn.value"
            @click="zoom.zoomIn"
          >
            +
          </button>
        </div>
        <div
          v-else-if="current?.attachment.kind === 'video' && currentUrl !== null"
          class="media-viewer-rate"
        >
          <AppSelect
            class="media-viewer-rate-select"
            overlay-class="media-viewer-rate-panel"
            checkmark
            :model-value="playbackRate"
            :options="[0.5, 1, 1.5, 2].map(value => ({ value, label: `${String(value)}×` }))"
            aria-label="播放速度"
            :filter="false"
            @update:model-value="setPlaybackRate"
          />
        </div>
      </footer>
    </section>
  </div>
</template>

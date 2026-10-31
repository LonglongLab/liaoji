<script setup lang="ts">
import MiddleFilename from "@/components/MiddleFilename.vue";
import { computed, nextTick, onBeforeUnmount, ref, toRef, watch } from "vue";

import type { AttachmentSummary } from "@/api/archiveApi";
import FileIcon from "@/components/FileIcon.vue";
import { useDragScroll } from "@/composables/useDragScroll";
import { useModalFocus } from "@/composables/useModalFocus";
import { useSwipeBackGuard } from "@/composables/useSwipeBackGuard";
import { fitZoomPercent, useZoom } from "@/composables/useZoom";
import { fileVisualOf, formatFileSize } from "@/lib/fileKinds";
import {
  createDoubleTapDetector,
  isCoarsePointer,
  isInteractiveTarget,
  isNarrowScreen,
} from "@/lib/gestures";
import {
  PreviewParseError,
  loadPreviewDocument,
  releasePreviewDocument,
  type PreviewDocument,
} from "@/lib/filePreview/parse";
import { previewPlanOf } from "@/lib/filePreview/plan";
import { formatDateTimeSeparator } from "@/utils/time";

/**
 * 文件预览层。按类型换渲染方式，但有两件事在所有类型上都一样：
 *   1. 头部常驻「本机解析」徽章 —— 用户随时知道这份文件有没有离开本机。
 *   2. 转换有损的类型必须写明丢了什么，降级渲染不冒充原样呈现。
 *
 * 这两条原来各占一整行常驻文字，在手机上把本就不多的正文高度吃掉了。现在改成：
 * 第 1 条由徽章本身承担（文案已经写着「本机解析」）；第 2 条收进徽章旁的 ⓘ，
 * 有损时 ⓘ 会带琥珀色，所以「这份预览是降级的」仍然一眼可见，只是详情要点开。
 */
const props = defineProps<{
  open: boolean;
  attachment: AttachmentSummary | null;
  senderName: string;
  createdAt: number | null;
  loadAttachment: (attachmentId: string) => Promise<Blob>;
}>();

const emit = defineEmits<{
  close: [];
  download: [attachmentId: string];
}>();

const previewDocument = ref<PreviewDocument | null>(null);
const loading = ref(false);
const errorText = ref<string | null>(null);
const sheetIndex = ref(0);
const slideIndex = ref(0);
const wrapLines = ref(true);
const fidelityOpen = ref(false);
/** md 默认给渲染视图；想看原样再切过去。 */
const markdownSource = ref(false);

const close = (): void => emit("close");
const openRef = toRef(props, "open");
const { container, initial, onKeydown: onModalKeydown } = useModalFocus(openRef, close);
void container;
void initial;

// 打开期间左右滑归这一层，浏览器不许再拿去当「返回」。
useSwipeBackGuard(openRef);

const visual = computed(() => fileVisualOf(props.attachment?.file_name ?? null));
const plan = computed(() => previewPlanOf(visual.value));
const fileName = computed(() => props.attachment?.file_name ?? "未命名文件");
const headerMeta = computed(() =>
  [formatFileSize(props.attachment?.size), visual.value.label].filter((part) => part !== null).join(" · "),
);
const subtitle = computed(() => {
  const when = props.createdAt === null ? null : formatDateTimeSeparator(props.createdAt);
  return [props.senderName, when].filter((part) => part !== null && part !== "").join(" · ");
});

const mode = computed(() => previewDocument.value?.mode ?? null);

/**
 * md 一次解析出两个视图：渲染好的 HTML 和原始源码行。源码视图直接复用
 * 文本预览那套带行号的渲染，所以这两个 computed 同时服务 text 与 markdown。
 */
const sourceLines = computed<string[] | null>(() => {
  const document = previewDocument.value;
  if (document?.mode === "text") return document.lines;
  if (document?.mode === "markdown" && markdownSource.value) return document.lines;
  return null;
});
const renderedHtml = computed<string | null>(() => {
  const document = previewDocument.value;
  if (document?.mode === "doc") return document.html;
  if (document?.mode === "markdown" && !markdownSource.value) return document.html;
  return null;
});
const zoomable = computed(
  () => props.open && plan.value.zoom !== "none" && previewDocument.value !== null,
);

const zoom = useZoom({
  isActive: () => zoomable.value,
  onEscape: close,
});

/**
 * 和图片灯箱同一个手势：抓着内容拖。图片走 transform 平移，这里是真的滚动容器，
 * 所以只在横向真的溢出时才接管指针（放大后的表格与幻灯片），
 * 否则文本和文档里会没法选字复制。
 */
const dragScroll = useDragScroll();
/** 当前那个可滚动面板（表格或幻灯片舞台，同一时刻只会渲染一个）。 */
const panHost = ref<HTMLElement | null>(null);

/** 手指和鼠标能做的手势不是一回事，提示就得说各自那一套。 */
const touchLike = isCoarsePointer();
const gestureHint = computed(() => {
  if (touchLike) {
    return dragScroll.active.value ? "双指缩放 · 双击放大 · 单指拖动" : "双指缩放 · 双击放大";
  }
  return dragScroll.active.value ? "Ctrl + 滚轮缩放 · 拖动可平移" : "Ctrl + 滚轮缩放";
});

// 光标要在放大之后立刻变成「可抓」，不能等用户先拖一次才知道能拖。
watch([() => zoom.percent.value, sheetIndex, previewDocument], () => {
  void nextTick(() => { dragScroll.refresh(panHost.value); });
});

/**
 * 文字类改的是字号，版面跟着重排，宽度仍旧自适应；
 * 版式类（幻灯片）宽度固定成 px，倍数直接把页面撑开，靠滚动看。
 */
const reflowStyle = computed(() =>
  plan.value.zoom === "reflow" && !zoom.isDefault.value ? { zoom: zoom.factor.value } : {},
);
const slideStyle = computed(() =>
  zoom.isDefault.value
    ? {}
    : { zoom: zoom.factor.value, width: "720px", flex: "none" },
);

const sheets = computed(() =>
  previewDocument.value?.mode === "sheet" ? previewDocument.value.sheets : [],
);
const activeSheet = computed(() => sheets.value[sheetIndex.value] ?? null);
const slides = computed(() =>
  previewDocument.value?.mode === "slides" ? previewDocument.value.slides : [],
);
const activeSlide = computed(() => slides.value[slideIndex.value] ?? null);

/**
 * 本机渲染的 PDF：一页一屏，手势与幻灯片同一套。缩放不是给画布套 CSS 放大
 * （那会糊），而是倍数一变就按新的 CSS 宽度重画一次位图。
 */
const pdfCanvas = ref<HTMLCanvasElement | null>(null);
const pdfPage = ref(1);
const pdfRendering = ref(false);
const pdfError = ref<string | null>(null);
const pdfPageCount = computed(() =>
  previewDocument.value?.mode === "pdf-canvas" ? previewDocument.value.pdf.pageCount : 0,
);
/** 放大就是把画布撑宽，撑出去的部分靠滚动和拖动看。 */
const pdfPageStyle = computed(() => ({ width: `${String(zoom.factor.value * 100)}%` }));

/** 连点翻页会叠出好几次渲染，只认最后一次的成败。 */
let pdfRenderToken = 0;

const renderPdfPage = async (): Promise<void> => {
  const document = previewDocument.value;
  const canvas = pdfCanvas.value;
  if (document?.mode !== "pdf-canvas" || canvas === null) return;
  // 画布的 CSS 宽度是布局算出来的，得等这一帧铺好才量得到。
  const cssWidth = canvas.clientWidth;
  if (cssWidth <= 0) return;
  pdfRenderToken += 1;
  const token = pdfRenderToken;
  pdfRendering.value = true;
  pdfError.value = null;
  try {
    await document.pdf.renderPage(pdfPage.value, canvas, cssWidth);
  } catch {
    // 引擎已经把「被下一次渲染取消」滤掉了，走到这儿的是真画不出来。
    if (token === pdfRenderToken) pdfError.value = "这一页画不出来，翻一页再回来试试。";
  } finally {
    if (token === pdfRenderToken) pdfRendering.value = false;
  }
};

watch([pdfPage, () => zoom.percent.value, previewDocument], () => {
  void nextTick(() => { void renderPdfPage(); });
});

const release = (): void => {
  dragScroll.stop();
  releasePreviewDocument(previewDocument.value);
  previewDocument.value = null;
};

/**
 * 手机上打开就得能读：宽表格在 100% 时一屏装不下，让人先捏一下再看
 * 是把活儿推给用户。桌面维持 100%——那儿本来就装得下。
 *
 * 实际会缩的只有表格：幻灯片的 `width: min(720px, 100%)` 已经自己贴着屏宽，
 * 量出来不溢出，这里就不动它。
 */
const fitToWidth = (): void => {
  const host = panHost.value;
  if (host === null || !isNarrowScreen()) return;
  const next = fitZoomPercent(host.clientWidth, host.scrollWidth);
  if (next !== null) zoom.setPercent(next);
};

const load = async (): Promise<void> => {
  const attachment = props.attachment;
  if (attachment === null) return;
  release();
  loading.value = true;
  errorText.value = null;
  sheetIndex.value = 0;
  slideIndex.value = 0;
  pdfPage.value = 1;
  pdfError.value = null;
  fidelityOpen.value = false;
  markdownSource.value = false;
  zoom.reset();
  try {
    const blob = await props.loadAttachment(attachment.id);
    previewDocument.value = await loadPreviewDocument(
      blob,
      plan.value.mode,
      visual.value.extension,
    );
  } catch (error) {
    // 解析器说得出原因就用它的话，说不出就只说取不到，不猜。
    errorText.value =
      error instanceof PreviewParseError ? error.message : "暂时无法打开这个文件，请重试。";
  } finally {
    loading.value = false;
  }
  // 量得到宽度要等这一屏真的渲染出来，而渲染要等 loading 落回 false。
  if (previewDocument.value !== null) {
    await nextTick();
    fitToWidth();
  }
};

const retry = (): void => {
  void load();
};

const selectSheet = (index: number): void => {
  sheetIndex.value = index;
  // 换表保留倍数，只把平移归位——阅读器都是这么做的。
  dragScroll.stop();
  zoom.resetPan();
};

const selectSlide = (index: number): void => {
  slideIndex.value = index;
  dragScroll.stop();
  zoom.resetPan();
};

const stepSlide = (delta: number): void => {
  selectSlide(Math.min(slides.value.length - 1, Math.max(0, slideIndex.value + delta)));
};

const stepSheet = (delta: number): void => {
  selectSheet(Math.min(sheets.value.length - 1, Math.max(0, sheetIndex.value + delta)));
};

const stepPdfPage = (delta: number): void => {
  const next = Math.min(pdfPageCount.value, Math.max(1, pdfPage.value + delta));
  if (next === pdfPage.value) return;
  pdfPage.value = next;
  dragScroll.stop();
  zoom.resetPan();
  // 上一页翻到了底，下一页得从头开始看。
  if (panHost.value !== null) panHost.value.scrollTop = 0;
};

/**
 * 手机上的三种单指手势，按「这一下还有没有别的去处」分派：
 *   可平移时 —— 交给 `useDragScroll` 拖内容（容器已 `touch-action: none`）；
 *   不可平移时横滑 —— 翻幻灯片 / 切工作表；
 *   原地两拍 —— 在 100% 与 200% 之间来回。
 * 和图片灯箱同一套约定（见 `MediaViewer`）。这样左右滑总有确切的去处，
 * 不会再被浏览器捡去当「返回」。
 */
const SWIPE_MIN_PX = 50;
const TAP_SLOP_PX = 12;

const isDoubleTap = createDoubleTapDetector();
let swipeFromX = 0;
let swipeFromY = 0;
let swiping = false;

const onBodyTouchStart = (event: TouchEvent): void => {
  zoom.onTouchStart(event);
  const first = event.touches.item(0);
  swiping = event.touches.length === 1 && !dragScroll.active.value && first !== null;
  if (first !== null) {
    swipeFromX = first.clientX;
    swipeFromY = first.clientY;
  }
};

const toggleZoom = (): void => {
  if (zoomable.value) zoom.toggle();
};

const onBodyTouchEnd = (event: TouchEvent): void => {
  zoom.onTouchEnd();
  const ended = event.changedTouches.item(0);
  if (ended === null) {
    swiping = false;
    return;
  }
  const deltaX = ended.clientX - swipeFromX;
  const deltaY = ended.clientY - swipeFromY;
  const travelled = Math.hypot(deltaX, deltaY);

  if (swiping && travelled >= SWIPE_MIN_PX && Math.abs(deltaX) > Math.abs(deltaY)) {
    const step = deltaX > 0 ? -1 : 1;
    if (mode.value === "slides") stepSlide(step);
    else if (mode.value === "sheet") stepSheet(step);
    else if (mode.value === "pdf-canvas") stepPdfPage(step);
  } else if (travelled < TAP_SLOP_PX && !isInteractiveTarget(event.target) && isDoubleTap()) {
    toggleZoom();
  }
  swiping = false;
};

const onBodyTouchCancel = (): void => {
  zoom.onTouchEnd();
  swiping = false;
};

const onBodyDoubleClick = (event: MouseEvent): void => {
  if (!isInteractiveTarget(event.target)) toggleZoom();
};

const onDialogKeydown = (event: KeyboardEvent): void => {
  onModalKeydown(event);
};

watch(
  () => [props.open, props.attachment?.id ?? null] as const,
  ([open]) => {
    if (open) void load();
    else release();
  },
  { immediate: true },
);

onBeforeUnmount(release);
</script>

<template>
  <div
    v-if="open && attachment !== null"
    class="file-preview-backdrop"
    role="dialog"
    aria-modal="true"
    :aria-label="`文件预览：${fileName}`"
    @click.self="close"
  >
    <section
      ref="container"
      class="file-preview"
      @keydown.stop="onDialogKeydown"
    >
      <header class="file-preview-head">
        <FileIcon
          :file-name="attachment.file_name"
          size="sm"
        />
        <div class="file-preview-title">
          <strong data-feedback-mask="file-name"><MiddleFilename :name="fileName" /></strong>
          <small data-feedback-mask="sender-name">{{ headerMeta }}<template v-if="subtitle !== ''"> · {{ subtitle }}</template></small>
        </div>
        <span class="file-preview-source">本机解析</span>
        <button
          v-if="plan.fidelity !== ''"
          class="file-preview-info"
          type="button"
          aria-label="这份预览丢了什么"
          aria-controls="file-preview-fidelity"
          :aria-expanded="fidelityOpen"
          @click="fidelityOpen = !fidelityOpen"
        >
          <svg
            width="16"
            height="16"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="2"
            stroke-linecap="round"
            aria-hidden="true"
          >
            <circle
              cx="12"
              cy="12"
              r="9"
            />
            <path d="M12 11.5v4.5" />
            <path d="M12 8h.01" />
          </svg>
        </button>
        <button
          class="text-action"
          type="button"
          @click="emit('download', attachment.id)"
        >
          下载
        </button>
        <button
          ref="initial"
          class="file-preview-close"
          type="button"
          aria-label="关闭文件预览"
          @click="close"
        >
          ×
        </button>
      </header>

      <p
        v-if="plan.fidelity !== ''"
        v-show="fidelityOpen"
        id="file-preview-fidelity"
        class="file-preview-fidelity"
        role="note"
      >
        {{ plan.fidelity }}
      </p>

      <div
        v-if="previewDocument !== null"
        class="file-preview-toolbar"
      >
        <span class="file-preview-doc-meta">{{ previewDocument.meta }}</span>
        <button
          v-if="mode === 'markdown'"
          class="file-preview-chip"
          type="button"
          @click="markdownSource = !markdownSource"
        >
          {{ markdownSource ? "视图：源码" : "视图：渲染" }}
        </button>
        <button
          v-if="sourceLines !== null"
          class="file-preview-chip"
          type="button"
          @click="wrapLines = !wrapLines"
        >
          {{ wrapLines ? "自动换行：开" : "自动换行：关" }}
        </button>
        <span
          v-if="mode === 'pdf-native'"
          class="file-preview-hint"
        >缩放、翻页用阅读器自带的控件</span>
        <span
          v-else-if="zoomable"
          class="file-preview-hint"
        >{{ gestureHint }}</span>
        <div
          v-if="zoomable"
          class="zoom-control"
          role="group"
          aria-label="缩放"
        >
          <button
            type="button"
            title="缩小（Ctrl 减号 / Ctrl + 滚轮）"
            aria-label="缩小"
            :disabled="!zoom.canZoomOut.value"
            @click="zoom.zoomOut"
          >
            −
          </button>
          <button
            type="button"
            title="恢复 100%（Ctrl 0）"
            aria-label="恢复原始大小"
            :disabled="zoom.isDefault.value"
            @click="zoom.reset"
          >
            {{ zoom.label.value }}
          </button>
          <button
            type="button"
            title="放大（Ctrl 加号 / Ctrl + 滚轮）"
            aria-label="放大"
            :disabled="!zoom.canZoomIn.value"
            @click="zoom.zoomIn"
          >
            +
          </button>
        </div>
      </div>

      <div
        data-feedback-mask="file-content"
        class="file-preview-body"
        @touchstart="onBodyTouchStart"
        @touchmove="zoom.onTouchMove"
        @touchend="onBodyTouchEnd"
        @touchcancel="onBodyTouchCancel"
        @dblclick="onBodyDoubleClick"
      >
        <p
          v-if="loading"
          class="file-preview-status"
          role="status"
        >
          正在本机解析……
        </p>
        <p
          v-else-if="errorText !== null"
          class="file-preview-status is-error"
          role="alert"
        >
          {{ errorText }}
          <button
            class="text-action"
            type="button"
            @click="retry"
          >
            重试
          </button>
        </p>

        <div
          v-else-if="sourceLines !== null"
          class="file-preview-text"
        >
          <div :style="reflowStyle">
            <div
              v-for="(line, index) in sourceLines"
              :key="index"
              class="file-preview-line"
            >
              <span class="file-preview-lineno">{{ index + 1 }}</span>
              <span
                class="file-preview-linetext"
                :class="{ 'is-nowrap': !wrapLines }"
              >{{ line === "" ? " " : line }}</span>
            </div>
          </div>
        </div>

        <iframe
          v-else-if="previewDocument?.mode === 'pdf-native'"
          class="file-preview-pdf"
          :src="previewDocument.url"
          :title="`PDF 预览：${fileName}`"
        />

        <div
          v-else-if="previewDocument?.mode === 'pdf-canvas'"
          class="file-preview-pdf-pages"
        >
          <div
            ref="panHost"
            class="file-preview-pdf-stage"
            :class="{ 'is-pannable': dragScroll.active.value, 'is-dragging': dragScroll.dragging.value }"
            @pointerdown="dragScroll.onPointerDown"
            @pointermove="dragScroll.onPointerMove"
            @pointerup="dragScroll.onPointerUp"
            @pointercancel="dragScroll.onPointerUp"
          >
            <canvas
              ref="pdfCanvas"
              class="file-preview-pdf-page"
              role="img"
              :style="pdfPageStyle"
              :aria-label="`第 ${pdfPage} 页，共 ${pdfPageCount} 页`"
            />
          </div>
          <p
            v-if="pdfError !== null"
            class="file-preview-pdf-note"
            role="alert"
          >
            {{ pdfError }}
          </p>
          <div class="file-preview-slide-nav">
            <button
              class="text-action"
              type="button"
              aria-label="上一页"
              :disabled="pdfPage <= 1"
              @click="stepPdfPage(-1)"
            >
              ‹
            </button>
            <span>
              {{ pdfPage }} / {{ pdfPageCount }}<template v-if="pdfRendering"> · 正在渲染</template>
            </span>
            <button
              class="text-action"
              type="button"
              aria-label="下一页"
              :disabled="pdfPage >= pdfPageCount"
              @click="stepPdfPage(1)"
            >
              ›
            </button>
          </div>
        </div>

        <div
          v-else-if="renderedHtml !== null"
          class="file-preview-paper-scroll"
        >
          <!-- docx 与 md 转出来的 HTML 都已经过 sanitizeDocHtml 的白名单：
               标签限定、on* 与 style 去掉、javascript: 链接剥掉、
               只留内嵌 data:image 的图片。 -->
          <!-- eslint-disable vue/no-v-html -->
          <article
            class="file-preview-paper"
            :class="{ 'is-markdown': mode === 'markdown' }"
            :style="reflowStyle"
            v-html="renderedHtml"
          />
          <!-- eslint-enable vue/no-v-html -->
        </div>

        <div
          v-else-if="previewDocument?.mode === 'sheet'"
          class="file-preview-sheet-layout"
        >
          <div
            ref="panHost"
            class="file-preview-sheet-scroll"
            :class="{ 'is-pannable': dragScroll.active.value, 'is-dragging': dragScroll.dragging.value }"
            @pointerdown="dragScroll.onPointerDown"
            @pointermove="dragScroll.onPointerMove"
            @pointerup="dragScroll.onPointerUp"
            @pointercancel="dragScroll.onPointerUp"
          >
            <table
              v-if="activeSheet !== null"
              class="file-preview-sheet"
              :style="reflowStyle"
            >
              <thead>
                <tr>
                  <th class="file-preview-rowno">
                    #
                  </th>
                  <th
                    v-for="(column, index) in activeSheet.columns"
                    :key="index"
                  >
                    {{ column }}
                  </th>
                </tr>
              </thead>
              <tbody>
                <tr
                  v-for="(row, rowIndex) in activeSheet.rows"
                  :key="rowIndex"
                >
                  <td class="file-preview-rowno">
                    {{ rowIndex + 1 }}
                  </td>
                  <td
                    v-for="(cell, cellIndex) in row"
                    :key="cellIndex"
                  >
                    {{ cell }}
                  </td>
                </tr>
              </tbody>
            </table>
          </div>
          <div
            v-if="sheets.length > 1"
            class="file-preview-sheet-tabs"
          >
            <button
              v-for="(sheet, index) in sheets"
              :key="sheet.name"
              class="file-preview-tab"
              :class="{ 'is-active': index === sheetIndex }"
              type="button"
              @click="selectSheet(index)"
            >
              {{ sheet.name }}
            </button>
          </div>
        </div>

        <div
          v-else-if="previewDocument?.mode === 'slides'"
          class="file-preview-slides"
        >
          <div class="file-preview-slide-rail">
            <button
              v-for="(slide, index) in slides"
              :key="index"
              class="file-preview-thumb"
              :class="{ 'is-active': index === slideIndex }"
              type="button"
              :aria-label="`第 ${index + 1} 张幻灯片`"
              @click="selectSlide(index)"
            >
              <small>{{ index + 1 }}</small>
              <span>{{ slide.title }}</span>
            </button>
          </div>
          <div class="file-preview-slide-stage-wrap">
            <div
              ref="panHost"
              class="file-preview-slide-stage"
              :class="{ 'is-pannable': dragScroll.active.value, 'is-dragging': dragScroll.dragging.value }"
              @pointerdown="dragScroll.onPointerDown"
              @pointermove="dragScroll.onPointerMove"
              @pointerup="dragScroll.onPointerUp"
              @pointercancel="dragScroll.onPointerUp"
            >
              <article
                v-if="activeSlide !== null"
                class="file-preview-slide"
                :style="slideStyle"
              >
                <h3>{{ activeSlide.title }}</h3>
                <ul v-if="activeSlide.bullets.length > 0">
                  <li
                    v-for="(bullet, index) in activeSlide.bullets"
                    :key="index"
                  >
                    {{ bullet }}
                  </li>
                </ul>
              </article>
            </div>
            <div class="file-preview-slide-nav">
              <button
                class="text-action"
                type="button"
                aria-label="上一张"
                :disabled="slideIndex === 0"
                @click="stepSlide(-1)"
              >
                ‹
              </button>
              <span>{{ slideIndex + 1 }} / {{ slides.length }}</span>
              <button
                class="text-action"
                type="button"
                aria-label="下一张"
                :disabled="slideIndex >= slides.length - 1"
                @click="stepSlide(1)"
              >
                ›
              </button>
            </div>
          </div>
        </div>

        <div
          v-else-if="previewDocument?.mode === 'archive'"
          class="file-preview-archive"
        >
          <ul :style="reflowStyle">
            <li
              v-for="(entry, index) in previewDocument.entries"
              :key="index"
              :class="{ 'is-dir': entry.directory }"
              :style="{ paddingLeft: `${String(12 + entry.depth * 18)}px` }"
            >
              <FileIcon
                v-if="!entry.directory"
                :file-name="entry.name"
                size="sm"
              />
              <span
                v-else
                class="file-preview-folder"
                aria-hidden="true"
              />
              <span class="file-preview-entry-name">{{ entry.name }}</span>
              <small v-if="entry.size !== null">{{ entry.size }}</small>
            </li>
          </ul>
        </div>
      </div>
    </section>
  </div>
</template>

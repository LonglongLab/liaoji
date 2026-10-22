import type { ComputedRef, Ref } from "vue";
import { computed, onBeforeUnmount, onMounted, ref } from "vue";

/**
 * 缩放引擎：图片灯箱与文件预览器共用。
 *
 * 按内容分两种语义（调用方决定用哪一种，见 `PreviewPlan.zoom`）：
 *   reflow  文字类 —— 内容整体放大但宽度自适应，只上下滚，不产生横向滚动条。
 *   layout  版式类 —— 页面等比撑开，超出部分靠滚动或拖拽看。
 * 这里只管倍数与平移量，怎么把它套到 DOM 上由组件决定。
 *
 * 档位走阶梯而不是线性加减：跟常见阅读器一致，手感也更跟手。
 * 下探到 25%：手机屏幕窄，A4 版式的 docx、宽表格在 50% 时仍然一屏放不下。
 */
export const ZOOM_STEPS = [25, 33, 40, 50, 67, 80, 90, 100, 110, 125, 150, 175, 200, 250, 300, 400];

const DEFAULT_PERCENT = 100;
const MIN_PERCENT = ZOOM_STEPS[0] ?? DEFAULT_PERCENT;
const MAX_PERCENT = ZOOM_STEPS[ZOOM_STEPS.length - 1] ?? DEFAULT_PERCENT;
/** 触控板一次捏合会连发几十个 wheel 事件，节流成一档一档地走。 */
const WHEEL_INTERVAL_MS = 60;

export type ZoomOptions = {
  /** 缩放当前是否可用；关着的浮层不该响应快捷键。 */
  isActive: () => boolean;
  /**
   * 未放大时内容自身的尺寸。图片按 `object-fit: contain` 摆放，画面往往比取景框小，
   * 拿框去算平移上限会让人把图拖进黑边里。不给就按取景框自己算。
   */
  contentSize?: () => { width: number; height: number } | null;
  /** 交给调用方决定 Esc 关的是哪一层。 */
  onEscape?: () => void;
};

export type ZoomController = {
  percent: Ref<number>;
  factor: ComputedRef<number>;
  label: ComputedRef<string>;
  isDefault: ComputedRef<boolean>;
  canZoomIn: ComputedRef<boolean>;
  canZoomOut: ComputedRef<boolean>;
  dragging: Ref<boolean>;
  /** 放大到超过 100% 才需要拖着看。 */
  pannable: ComputedRef<boolean>;
  transform: ComputedRef<string>;
  zoomIn: () => void;
  zoomOut: () => void;
  /** 直接落到某一档（「适应宽度」用），平移量跟着一起缩，与逐档缩放同一条路。 */
  setPercent: (next: number) => void;
  reset: () => void;
  /** 双击：在 100% 和 200% 之间来回，和系统里看图、看 PDF 的习惯一致。 */
  toggle: () => void;
  /** 换页、换工作表时调用：保留倍数，只把平移归位。 */
  resetPan: () => void;
  onPointerDown: (event: PointerEvent) => void;
  onPointerMove: (event: PointerEvent) => void;
  onPointerUp: () => void;
  onTouchStart: (event: TouchEvent) => void;
  onTouchMove: (event: TouchEvent) => void;
  onTouchEnd: () => void;
};

const clampPercent = (value: number): number =>
  Math.min(MAX_PERCENT, Math.max(MIN_PERCENT, Math.round(value)));

/**
 * 「适应宽度」挑哪一档：按可视宽 ÷ 内容宽算出想要的倍数，取不超过它的最大一档。
 * 宁可小一档也不四舍五入到大的——差一点点也是横向溢出，
 * 一打开就得拖着看的表格，比小一号的字难读得多。
 *
 * 返回 null 表示不用动：本来就装得下，或者算出来正好是 100%。
 */
export const fitZoomPercent = (clientWidth: number, contentWidth: number): number | null => {
  if (clientWidth <= 0 || contentWidth <= clientWidth + 1) return null;
  const wanted = (clientWidth / contentWidth) * DEFAULT_PERCENT;
  const fits = ZOOM_STEPS.filter((step) => step <= wanted);
  const chosen = fits[fits.length - 1] ?? MIN_PERCENT;
  return chosen === DEFAULT_PERCENT ? null : chosen;
};

export const nextZoomStep = (current: number, direction: number): number => {
  if (direction > 0) {
    return ZOOM_STEPS.find((step) => step > current + 0.5) ?? MAX_PERCENT;
  }
  const lower = ZOOM_STEPS.filter((step) => step < current - 0.5);
  return lower[lower.length - 1] ?? MIN_PERCENT;
};

const touchGap = (touches: TouchList): number => {
  const first = touches.item(0);
  const second = touches.item(1);
  if (first === null || second === null) return 0;
  return Math.hypot(second.clientX - first.clientX, second.clientY - first.clientY);
};

export const useZoom = (options: ZoomOptions): ZoomController => {
  const percent = ref(DEFAULT_PERCENT);
  const panX = ref(0);
  const panY = ref(0);
  const dragging = ref(false);

  let panFrom: { x: number; y: number; panX: number; panY: number } | null = null;
  let panBounds: { x: number; y: number } | null = null;
  let pinch: { gap: number; percent: number } | null = null;
  let lastWheelAt = 0;

  const resetPan = (): void => {
    panX.value = 0;
    panY.value = 0;
  };

  const applyPercent = (next: number): void => {
    const clamped = clampPercent(next);
    // 平移量跟着倍数一起缩，缩小时画面自己回到框里，不用用户再拖回来。
    const ratio = clamped <= DEFAULT_PERCENT ? 0 : clamped / percent.value;
    panX.value *= ratio;
    panY.value *= ratio;
    percent.value = clamped;
  };

  const step = (direction: number): void => {
    if (!options.isActive()) return;
    applyPercent(nextZoomStep(percent.value, direction));
  };

  const zoomIn = (): void => step(1);
  const zoomOut = (): void => step(-1);
  const reset = (): void => {
    percent.value = DEFAULT_PERCENT;
    resetPan();
  };
  const toggle = (): void => {
    if (!options.isActive()) return;
    if (percent.value > DEFAULT_PERCENT) reset();
    else percent.value = 200;
  };

  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") {
      if (options.onEscape !== undefined && options.isActive()) {
        event.preventDefault();
        options.onEscape();
      }
      return;
    }
    if (!options.isActive() || !(event.ctrlKey || event.metaKey)) return;
    if (event.key === "=" || event.key === "+") {
      event.preventDefault();
      zoomIn();
    } else if (event.key === "-" || event.key === "_") {
      event.preventDefault();
      zoomOut();
    } else if (event.key === "0") {
      event.preventDefault();
      reset();
    }
  };

  const onWheel = (event: WheelEvent): void => {
    if (!options.isActive()) return;
    // 只认 Ctrl/⌘ + 滚轮：图片和文档用同一个手势，光滚轮仍然留给滚动。
    // 触控板上的捏合，浏览器发出来的也正是这个事件。
    if (!event.ctrlKey && !event.metaKey) return;
    event.preventDefault();
    if (Math.abs(event.deltaY) < 1) return;
    const now = Date.now();
    if (now - lastWheelAt < WHEEL_INTERVAL_MS) return;
    lastWheelAt = now;
    if (event.deltaY < 0) zoomIn();
    else zoomOut();
  };

  onMounted(() => {
    globalThis.addEventListener("keydown", onKeydown);
    globalThis.addEventListener("wheel", onWheel, { passive: false });
  });
  onBeforeUnmount(() => {
    globalThis.removeEventListener("keydown", onKeydown);
    globalThis.removeEventListener("wheel", onWheel);
  });

  const onPointerDown = (event: PointerEvent): void => {
    if (percent.value <= DEFAULT_PERCENT) return;
    panFrom = { x: event.clientX, y: event.clientY, panX: panX.value, panY: panY.value };
    // 拖动上限＝放大后的画面超出取景框的那一半。刚好能把每条边都拉到眼前，
    // 又不会让画面整个滑出去。
    const target = event.currentTarget;
    const scale = percent.value / DEFAULT_PERCENT;
    if (target instanceof Element) {
      const frame = target.getBoundingClientRect();
      const content = options.contentSize?.() ?? frame;
      panBounds = {
        x: Math.max(0, (content.width * scale - frame.width) / 2),
        y: Math.max(0, (content.height * scale - frame.height) / 2),
      };
      if (target instanceof HTMLElement) target.setPointerCapture(event.pointerId);
    } else {
      panBounds = null;
    }
    dragging.value = true;
  };

  const onPointerMove = (event: PointerEvent): void => {
    const from = panFrom;
    if (from === null) return;
    const bounds = panBounds;
    const nextX = from.panX + (event.clientX - from.x);
    const nextY = from.panY + (event.clientY - from.y);
    panX.value = bounds === null ? nextX : Math.min(bounds.x, Math.max(-bounds.x, nextX));
    panY.value = bounds === null ? nextY : Math.min(bounds.y, Math.max(-bounds.y, nextY));
  };

  const onPointerUp = (): void => {
    if (panFrom === null) return;
    panFrom = null;
    panBounds = null;
    dragging.value = false;
  };

  // 触摸捏合：容器上写 touch-action 把手势交出来，就不必去 preventDefault
  // （React/Vue 挂的 touchmove 在现代浏览器里是被动监听，preventDefault 不生效）。
  const onTouchStart = (event: TouchEvent): void => {
    if (event.touches.length !== 2 || !options.isActive()) return;
    pinch = { gap: touchGap(event.touches), percent: percent.value };
  };

  const onTouchMove = (event: TouchEvent): void => {
    const start = pinch;
    if (start === null || start.gap === 0 || event.touches.length !== 2) return;
    applyPercent(start.percent * (touchGap(event.touches) / start.gap));
  };

  const onTouchEnd = (): void => {
    pinch = null;
  };

  return {
    percent,
    factor: computed(() => percent.value / DEFAULT_PERCENT),
    label: computed(() => `${String(percent.value)}%`),
    isDefault: computed(() => percent.value === DEFAULT_PERCENT),
    canZoomIn: computed(() => percent.value < MAX_PERCENT),
    canZoomOut: computed(() => percent.value > MIN_PERCENT),
    dragging,
    pannable: computed(() => percent.value > DEFAULT_PERCENT),
    transform: computed(
      () =>
        `translate(${String(Math.round(panX.value))}px, ${String(Math.round(panY.value))}px) scale(${String(percent.value / DEFAULT_PERCENT)})`,
    ),
    zoomIn,
    zoomOut,
    setPercent: applyPercent,
    reset,
    toggle,
    resetPan,
    onPointerDown,
    onPointerMove,
    onPointerUp,
    onTouchStart,
    onTouchMove,
    onTouchEnd,
  };
};

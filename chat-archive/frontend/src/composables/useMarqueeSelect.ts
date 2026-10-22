import { computed, onBeforeUnmount, ref, type Ref } from "vue";

/**
 * 按住左键拉出一个灰色框来选消息（交互第二版，照 PC 微信）。
 *
 * 手势怎么分：
 *
 * - **在空白处按下**（不在任何一条消息的气泡里）：拖动超过几个像素就开始拉框。
 * - **在气泡里按下**：先当选文字。只要还在这一条里拖，就是选文字；一旦拖到
 *   **别的消息**上，就清掉文字选区、从按下的那一点起改成拉框。复制一句话很常用，
 *   不能让框选把它吃掉。
 * - 多选态下同样如此，只是框碰到的会**实时**并进已选里；阅读态下框只是一个框，
 *   松手才进多选态——微信就是这样，拉的过程中不出圆勾。
 *
 * 拉到滚动区上下沿会自动滚动，框的起点钉在内容上、跟着内容走。
 *
 * 框碰到哪几条**只看竖直方向**：横向只要拉出一点，这几行里左右两边的消息都算。
 * 自己的消息在右、对方在左，只看竖直方向两边都好选。
 */

/** 一条消息在滚动内容里的上下沿（内容坐标 = 相对滚动内容顶部，不受 `scrollTop` 影响）。 */
export interface RowExtent {
  id: string;
  top: number;
  bottom: number;
}

/** 框的上下沿（内容坐标）碰到了哪几条，按传进来的顺序给。擦边（恰好相接）不算。 */
export const rowsTouched = (rows: readonly RowExtent[], a: number, b: number): string[] => {
  const top = Math.min(a, b);
  const bottom = Math.max(a, b);
  return rows.filter((row) => row.top < bottom && row.bottom > top).map((row) => row.id);
};

/** 拖过这么远才算拉框，不然就是一次点击。 */
export const MARQUEE_THRESHOLD_PX = 4;
/** 指针离滚动区上下沿这么近就开始自动滚。 */
const EDGE_PX = 28;
const AUTO_SCROLL_STEP_PX = 14;
const AUTO_SCROLL_INTERVAL_MS = 30;

/** 在这些东西上按下永远不起框：它们有自己的拖动或点击语义。 */
const INTERACTIVE = "button, a, input, textarea, select, audio, video, [role='menu'], [role='slider']";
/** 气泡：在这里按下先当选文字。 */
const BUBBLE = ".msg-content";
const ROW = "[data-message-id]";

export interface MarqueeOptions {
  container: Ref<HTMLElement | null>;
  /** 当下每一条消息在内容里的位置。虚拟列表要给**全部**已加载的行，不只是画出来的那几行。 */
  rowExtents: () => RowExtent[];
  /** 多选态下拉框：框碰到的实时报上来。 */
  onLive: (ids: string[]) => void;
  /** 松手。`ids` 是框最后碰到的那几条。 */
  onFinish: (ids: string[]) => void;
  /** 开始拉框的那一刻（调用方据此记下「拉框之前已经选了哪些」）。 */
  onStart: () => void;
  selecting: () => boolean;
}

interface Box {
  x0: number;
  y0: number;
  x1: number;
  y1: number;
  clientX: number;
  clientY: number;
}

interface Pending {
  clientX: number;
  clientY: number;
  /** 在气泡里按下的：拖到别的消息上才转成拉框。 */
  inBubble: boolean;
  rowId: string | null;
}

const rowIdOf = (target: EventTarget | null): string | null => {
  if (!(target instanceof Element)) return null;
  return target.closest<HTMLElement>(ROW)?.dataset.messageId ?? null;
};

export const useMarqueeSelect = (options: MarqueeOptions) => {
  const box = ref<Box | null>(null);
  let pending: Pending | null = null;
  let timer: ReturnType<typeof globalThis.setInterval> | null = null;
  /** 刚拉完框：紧跟着的那次 click 不能再被当成点选。 */
  let swallowClick = false;

  const toContent = (clientX: number, clientY: number): { x: number; y: number } => {
    const el = options.container.value;
    if (el === null) return { x: clientX, y: clientY };
    const rect = el.getBoundingClientRect();
    return { x: clientX - rect.left + el.scrollLeft, y: clientY - rect.top + el.scrollTop };
  };

  const touched = (): string[] => {
    const current = box.value;
    return current === null ? [] : rowsTouched(options.rowExtents(), current.y0, current.y1);
  };

  const stopAutoScroll = (): void => {
    if (timer !== null) {
      globalThis.clearInterval(timer);
      timer = null;
    }
  };

  const follow = (clientX: number, clientY: number): void => {
    const current = box.value;
    if (current === null) return;
    const point = toContent(clientX, clientY);
    box.value = { ...current, x1: point.x, y1: point.y, clientX, clientY };
    if (options.selecting()) options.onLive(touched());
  };

  const autoScroll = (clientY: number): void => {
    stopAutoScroll();
    const el = options.container.value;
    if (el === null) return;
    const rect = el.getBoundingClientRect();
    const direction = clientY < rect.top + EDGE_PX ? -1 : clientY > rect.bottom - EDGE_PX ? 1 : 0;
    if (direction === 0) return;
    timer = globalThis.setInterval(() => {
      const current = box.value;
      if (current === null) return;
      el.scrollTop += direction * AUTO_SCROLL_STEP_PX;
      // 指针没动，内容动了：用钳在滚动区里的那一点重算框的终点。
      const y = Math.min(Math.max(current.clientY, rect.top + 1), rect.bottom - 1);
      follow(current.clientX, y);
    }, AUTO_SCROLL_INTERVAL_MS);
  };

  const begin = (clientX: number, clientY: number): void => {
    const point = toContent(clientX, clientY);
    box.value = { x0: point.x, y0: point.y, x1: point.x, y1: point.y, clientX, clientY };
    options.onStart();
  };

  const onMouseMove = (event: MouseEvent): void => {
    if (pending !== null && box.value === null) {
      if (pending.inBubble) {
        // 还在按下的那一条里：是在选文字，不管。
        const here = rowIdOf(event.target);
        if (here === null || here === pending.rowId) return;
        globalThis.getSelection()?.removeAllRanges();
      } else {
        const moved =
          Math.abs(event.clientX - pending.clientX) + Math.abs(event.clientY - pending.clientY);
        if (moved < MARQUEE_THRESHOLD_PX) return;
      }
      begin(pending.clientX, pending.clientY);
      pending = null;
    }
    if (box.value === null) return;
    event.preventDefault();
    follow(event.clientX, event.clientY);
    autoScroll(event.clientY);
  };

  const finish = (): void => {
    globalThis.removeEventListener("mousemove", onMouseMove);
    globalThis.removeEventListener("mouseup", onMouseUp);
    stopAutoScroll();
    pending = null;
    if (box.value === null) return;
    const ids = touched();
    box.value = null;
    swallowClick = true;
    // click 紧跟在 mouseup 之后同步派发；这一拍之后就不再吞了。
    globalThis.setTimeout(() => {
      swallowClick = false;
    }, 0);
    options.onFinish(ids);
  };

  const onMouseUp = (): void => {
    finish();
  };

  /** 挂在滚动区的 `mousedown` 上。 */
  const onMouseDown = (event: MouseEvent): void => {
    if (event.button !== 0 || event.shiftKey || event.ctrlKey || event.metaKey) return;
    const target = event.target;
    if (!(target instanceof Element) || target.closest(INTERACTIVE) !== null) return;
    const inBubble = target.closest(BUBBLE) !== null;
    // 空白处按下：挡掉浏览器自己的文字拖选，不然拉框时满屏蓝底。
    // 多选态下行本来就不可选文字（整行是点选区），同样挡掉。
    if (!inBubble || options.selecting()) event.preventDefault();
    pending = {
      clientX: event.clientX,
      clientY: event.clientY,
      inBubble: inBubble && !options.selecting(),
      rowId: rowIdOf(target),
    };
    globalThis.addEventListener("mousemove", onMouseMove);
    globalThis.addEventListener("mouseup", onMouseUp);
  };

  /** 挂在滚动区的捕获阶段 `click` 上：刚拉完框的那次 click 不算点选。 */
  const onClickCapture = (event: MouseEvent): void => {
    if (!swallowClick) return;
    swallowClick = false;
    event.stopPropagation();
    event.preventDefault();
  };

  /** 框在视口里的位置（`position: fixed`），裁在滚动区里面。 */
  const boxStyle = computed(() => {
    const current = box.value;
    const el = options.container.value;
    if (current === null || el === null) return null;
    const rect = el.getBoundingClientRect();
    const toClientY = (y: number): number => y - el.scrollTop + rect.top;
    const toClientX = (x: number): number => x - el.scrollLeft + rect.left;
    const top = Math.max(rect.top, Math.min(toClientY(current.y0), toClientY(current.y1)));
    const bottom = Math.min(rect.bottom, Math.max(toClientY(current.y0), toClientY(current.y1)));
    const left = Math.max(rect.left, Math.min(toClientX(current.x0), toClientX(current.x1)));
    const right = Math.min(rect.right, Math.max(toClientX(current.x0), toClientX(current.x1)));
    return {
      top: `${String(top)}px`,
      left: `${String(left)}px`,
      width: `${String(Math.max(0, right - left))}px`,
      height: `${String(Math.max(0, bottom - top))}px`,
    };
  });

  onBeforeUnmount(finish);

  return { onMouseDown, onClickCapture, boxStyle, dragging: computed(() => box.value !== null) };
};

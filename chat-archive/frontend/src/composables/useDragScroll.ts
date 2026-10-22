import type { Ref } from "vue";
import { ref } from "vue";

/**
 * 抓着内容拖动。图片是 transform 平移（见 `useZoom`），而文档/表格/幻灯片是
 * 真的滚动容器——两边手势要一样，底下的机制却不同，这里管的是后者。
 *
 * **只在横向真的溢出时才接管指针**：
 *   - 纵向本来就能滚轮滚（手机上还带原生惯性），没必要抢；
 *   - 文本和文档在阅读缩放下永远不横向溢出，抢了指针会让人没法选字复制。
 * 于是这个手势正好出现在它唯一有用的地方：放大后的表格与幻灯片。
 *
 * 接管之后两轴一起走，抬手还要补一段滑行：容器在可平移态写了
 * `touch-action: none` 把手势从浏览器手里要过来（样式见 `.is-pannable`），
 * 原生的甩动惯性也就一并没了。不补回来，手机上单指拖会比原生滚动还涩。
 */
export type DragScroll = {
  active: Ref<boolean>;
  dragging: Ref<boolean>;
  /** 挂在滚动容器上；`refresh` 由内容变化后调用，重新判断要不要接管。 */
  refresh: (element: HTMLElement | null) => void;
  onPointerDown: (event: PointerEvent) => void;
  onPointerMove: (event: PointerEvent) => void;
  onPointerUp: (event: PointerEvent) => void;
  /** 掐掉还在滑行的惯性。换表、换页、重新加载时调用，免得新内容被上一把甩动接着推。 */
  stop: () => void;
};

/** 每 16.7ms（一帧）保留的速度比例。0.94 大致对应「一甩滑半秒」。 */
const FRICTION = 0.94;
/** px/ms。低于这个就停，不然会无限逼近零。 */
const MIN_VELOCITY = 0.02;
/** 两帧靠得太近，算出来的速度全是噪声。 */
const MIN_SAMPLE_MS = 8;
/** 手指在抬起前已经停了这么久，就当他是「放下」而不是「甩出去」。 */
const FLING_WINDOW_MS = 80;
/** 掉帧时别让一步跨出半屏。 */
const MAX_FRAME_MS = 50;

export const useDragScroll = (): DragScroll => {
  const active = ref(false);
  const dragging = ref(false);
  let from: { x: number; y: number; left: number; top: number } | null = null;
  let sample: { x: number; y: number; at: number } | null = null;
  let velocity = { x: 0, y: 0 };
  let glide: number | null = null;

  const now = (): number => globalThis.performance.now();

  const stop = (): void => {
    if (glide !== null) {
      globalThis.cancelAnimationFrame(glide);
      glide = null;
    }
    velocity = { x: 0, y: 0 };
  };

  const refresh = (element: HTMLElement | null): void => {
    active.value = element !== null && element.scrollWidth > element.clientWidth + 1;
  };

  const glideStep = (target: HTMLElement, previousAt: number): void => {
    const at = now();
    const elapsed = Math.min(MAX_FRAME_MS, Math.max(1, at - previousAt));
    const before = { left: target.scrollLeft, top: target.scrollTop };
    target.scrollLeft = before.left + velocity.x * elapsed;
    target.scrollTop = before.top + velocity.y * elapsed;
    const decay = FRICTION ** (elapsed / 16.7);
    velocity = { x: velocity.x * decay, y: velocity.y * decay };
    // 撞到内容尽头就停：浏览器把 scrollLeft 夹住了，再推也不动。
    const moved = target.scrollLeft !== before.left || target.scrollTop !== before.top;
    if (!moved || Math.hypot(velocity.x, velocity.y) < MIN_VELOCITY) {
      glide = null;
      velocity = { x: 0, y: 0 };
      return;
    }
    glide = globalThis.requestAnimationFrame(() => {
      glideStep(target, at);
    });
  };

  const onPointerDown = (event: PointerEvent): void => {
    const target = event.currentTarget;
    if (!(target instanceof HTMLElement)) return;
    // 按住＝叫停：滑行中再按一下，画面要立刻定在手指下面。
    stop();
    refresh(target);
    if (!active.value) return;
    from = {
      x: event.clientX,
      y: event.clientY,
      left: target.scrollLeft,
      top: target.scrollTop,
    };
    sample = { x: event.clientX, y: event.clientY, at: now() };
    dragging.value = true;
    target.setPointerCapture(event.pointerId);
  };

  const onPointerMove = (event: PointerEvent): void => {
    const start = from;
    const target = event.currentTarget;
    if (start === null || !(target instanceof HTMLElement)) return;
    target.scrollLeft = start.left - (event.clientX - start.x);
    target.scrollTop = start.top - (event.clientY - start.y);

    const previous = sample;
    const at = now();
    if (previous === null) return;
    const elapsed = at - previous.at;
    if (elapsed < MIN_SAMPLE_MS) return;
    velocity = {
      x: (previous.x - event.clientX) / elapsed,
      y: (previous.y - event.clientY) / elapsed,
    };
    sample = { x: event.clientX, y: event.clientY, at };
  };

  const onPointerUp = (event: PointerEvent): void => {
    if (from === null) return;
    from = null;
    dragging.value = false;
    const target = event.currentTarget;
    if (target instanceof HTMLElement && target.hasPointerCapture(event.pointerId)) {
      target.releasePointerCapture(event.pointerId);
    }
    const last = sample;
    sample = null;
    const flung = last !== null && now() - last.at <= FLING_WINDOW_MS;
    if (!flung || !(target instanceof HTMLElement)) {
      velocity = { x: 0, y: 0 };
      return;
    }
    if (Math.hypot(velocity.x, velocity.y) < MIN_VELOCITY) {
      velocity = { x: 0, y: 0 };
      return;
    }
    // 从下一帧起算：抬手这一瞬不该凭空跳一段。
    const startedAt = now();
    glide = globalThis.requestAnimationFrame(() => {
      glideStep(target, startedAt);
    });
  };

  return { active, dragging, refresh, onPointerDown, onPointerMove, onPointerUp, stop };
};

import { onBeforeUnmount, onMounted, watch } from "vue";
import type { Ref } from "vue";

import { isInteractiveTarget } from "@/lib/gestures";

/**
 * 全屏浮层打开期间，把「左右滑」从浏览器手里要回来。
 *
 * 浏览器有两套完全不同的返回手势，得分别拦：
 *   1. Chrome/Android 的横向过滚导航——滚动链一路上溯到 document 就触发。
 *      `overscroll-behavior-x: none` 挂在根元素上能断掉它（样式见
 *      `html.is-swipe-back-locked`）；浮层里的每个滚动容器另外各自 contain。
 *   2. iOS Safari 的边缘返回是系统手势，`overscroll-behavior` 管不着，
 *      只有在 `touchstart` 上 preventDefault 才拦得住。
 *
 * 只拦从屏幕左右 EDGE_PX 内起手、且没落在控件上的单指触摸：
 * 那一下十有八九是想拖内容，而贴着边缘的关闭按钮还得能按。
 */
const EDGE_PX = 24;
const LOCK_CLASS = "is-swipe-back-locked";

/**
 * 锁按持有者计数，不是按「最后一个说话的算」。同一页上会同时挂着好几个
 * 全屏浮层（图片、文件预览，会话与搜索各一份），后挂上来的那个
 * `immediate` 一跑就是 false——不数着，它会顺手把别人正拿着的锁摘掉。
 */
let holders = 0;

export const useSwipeBackGuard = (active: Ref<boolean>): void => {
  let holding = false;

  const onTouchStart = (event: TouchEvent): void => {
    if (!active.value || event.touches.length !== 1 || !event.cancelable) return;
    const touch = event.touches.item(0);
    if (touch === null) return;
    const width = globalThis.innerWidth;
    const atEdge = touch.clientX <= EDGE_PX || touch.clientX >= width - EDGE_PX;
    if (!atEdge || isInteractiveTarget(event.target)) return;
    event.preventDefault();
  };

  const hold = (wanted: boolean): void => {
    if (wanted === holding) return;
    holding = wanted;
    holders += wanted ? 1 : -1;
    globalThis.document.documentElement.classList.toggle(LOCK_CLASS, holders > 0);
  };

  // sync：拦 touchstart 那半边直接读 active.value，锁这半边就不该晚一个 tick——
  // 两半说的必须是同一件事。切的只是一个 class，没有下游依赖要等。
  watch(active, hold, { immediate: true, flush: "sync" });

  onMounted(() => {
    globalThis.document.addEventListener("touchstart", onTouchStart, { passive: false });
  });
  onBeforeUnmount(() => {
    globalThis.document.removeEventListener("touchstart", onTouchStart);
    // 组件先卸载、浮层没走关闭路径时，这一份持有也得还回去。
    hold(false);
  });
};

export const OLDER_MESSAGES_THRESHOLD_PX = 64;
export const SCROLL_TO_BOTTOM_THRESHOLD_PX = 120;
export const FOLLOW_LATEST_THRESHOLD_PX = 40;

export const distanceFromBottom = (
  scrollTop: number,
  clientHeight: number,
  scrollHeight: number,
): number => Math.max(0, scrollHeight - clientHeight - scrollTop);

export const shouldShowScrollToBottom = (
  distance: number,
  threshold: number = SCROLL_TO_BOTTOM_THRESHOLD_PX,
): boolean => distance > Math.max(0, threshold);

export const shouldLoadOlderMessages = (
  scrollTop: number,
  threshold: number,
  hasMore: boolean,
  fetchingMore: boolean,
): boolean =>
  hasMore && !fetchingMore && scrollTop <= Math.max(0, threshold);

/**
 * 往新的那一头续读的判据（定位窗口，PSU-2.55）。
 *
 * 与上面那条同一个形状，量的是**离底还有多远**而不是 `scrollTop`：往下续读的
 * 触发点在容器底部，而那个距离才是「快到底了」这件事在两端都成立的说法。
 */
export const shouldLoadNewerMessages = (
  distance: number,
  threshold: number,
  hasMore: boolean,
  fetchingMore: boolean,
): boolean =>
  hasMore && !fetchingMore && distance <= Math.max(0, threshold);

/**
 * 续读之后把读者放回原处（PSU-2.125）。
 *
 * 参照物是**那条消息自己**：它贴后的 `start`，减掉它当初离容器顶的那段距离。
 *
 * 旧版用的是 `totalSize` 差值，那要求「贴入前后除了新贴的那几条之外
 * 什么都没变」——而这个前提在贴入的那一瞬间就不成立：`scrollTop` 归零本身
 * 会把新行拉进渲染窗口并触发测量，快照当场就过期了。
 */
export const transcriptAnchorScrollTop = (
  anchorStart: number,
  viewportOffset: number,
): number => Math.max(0, anchorStart - viewportOffset);

/**
 * 视口上方的行变高了，要不要把 `scrollTop` 往下推同样多（PSU-2.125）。
 *
 * 这是拿来覆盖 `virtual-core` 默认策略的：它对「已量过 + 正在往上滑」
 * 的重新测量跳过补偿，而媒体到货恰好落在这一格：行先按占位态
 * 量过一次进了缓存，图片是几百毫秒后才到的第二次。跳过的后果是内容把
 * 读者往下推，抵掉他刚滑的那一段——实测一张图 184px。
 *
 * 补偿阅读锚点之前的行。默认锚点是视口顶端；实际转录使用视口中部所在行，
 * 避免字体度量差异让图片恰好落在视口顶端内侧时漏掉补偿。锚点行自己不补偿。
 */
export const shouldCompensateForItemResize = (
  itemStart: number,
  scrollOffset: number | null,
  anchorStart: number | null = null,
): boolean => itemStart < (anchorStart ?? scrollOffset ?? 0);

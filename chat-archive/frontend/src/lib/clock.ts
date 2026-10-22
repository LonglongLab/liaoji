/**
 * 一秒一跳的挂钟，用来驱动倒计时（PSU-2.68）。
 *
 * 在 .ts 里而不是组件内联：`.vue` 上开着 ESLint 的 `no-undef` 且没配 browser
 * globals，`setInterval` / `clearInterval` 只能待在模块里。
 */
export const startClock = (onTick: (nowMs: number) => void): (() => void) => {
  const handle = setInterval(() => {
    onTick(Date.now());
  }, 1000);
  return () => {
    clearInterval(handle);
  };
};

/**
 * 一次性延时，返回取消函数（PSU-2.83：用来让「已复制」自己退回「复制」）。
 *
 * 和上面同理只能待在 .ts 里。返回取消函数而不是句柄，是为了让调用方能把它
 * 直接塞进 `onUnmounted`——一个不清理的 `setTimeout` 会拖住用 fake timers 的测试。
 */
export const startTimeout = (onFire: () => void, delayMs: number): (() => void) => {
  const handle = setTimeout(onFire, delayMs);
  return () => {
    clearTimeout(handle);
  };
};

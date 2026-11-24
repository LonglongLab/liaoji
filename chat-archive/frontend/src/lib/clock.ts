import { getCurrentScope, onScopeDispose, ref, watch, type Ref, type WatchSource } from "vue";

/**
 * 定时挂钟，用来驱动倒计时、等待耗时与轮询（PSU-2.68）。
 *
 * 在 .ts 里而不是组件内联：`.vue` 上开着 ESLint 的 `no-undef` 且没配 browser
 * globals，`setInterval` / `clearInterval` 只能待在模块里。
 * 若在 Vue effectScope / 组件 setup 内调用，会自动注册 `onScopeDispose` 兜底清理。
 */
export const startClock = (
  onTick: (nowMs: number) => void,
  intervalMs = 1000,
): (() => void) => {
  let stopped = false;
  const handle = globalThis.setInterval(() => {
    onTick(Date.now());
  }, intervalMs);
  const stop = (): void => {
    if (stopped) return;
    stopped = true;
    globalThis.clearInterval(handle);
  };
  if (getCurrentScope() !== undefined) {
    onScopeDispose(stop);
  }
  return stop;
};

/**
 * 响应式当前时间戳（默认每秒一跳），随当前 Vue scope 自动启停与销毁。
 */
export const useClockNow = (
  intervalMs = 1000,
  active?: WatchSource<boolean>,
): Ref<number> => {
  const now = ref(Date.now());
  let stop: (() => void) | null = null;
  const clear = (): void => {
    stop?.();
    stop = null;
  };
  const start = (): void => {
    clear();
    now.value = Date.now();
    stop = startClock((value) => {
      now.value = value;
    }, intervalMs);
  };
  if (active === undefined) {
    start();
  } else {
    watch(
      active,
      (enabled) => {
        if (enabled) start();
        else clear();
      },
      { immediate: true, flush: "sync" },
    );
  }
  onScopeDispose(clear);
  return now;
};

/**
 * 一次性延时，返回取消函数（PSU-2.83：用来让「已复制」自己退回「复制」）。
 *
 * 和上面同理只能待在 .ts 里。返回取消函数而不是句柄，是为了让调用方能把它
 * 直接塞进 `onUnmounted`——一个不清理的 `setTimeout` 会拖住用 fake timers 的测试。
 */
export const startTimeout = (onFire: () => void, delayMs: number): (() => void) => {
  const handle = globalThis.setTimeout(onFire, delayMs);
  return () => {
    globalThis.clearTimeout(handle);
  };
};

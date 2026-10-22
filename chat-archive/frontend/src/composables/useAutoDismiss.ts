import { onBeforeUnmount, ref, watch, type Ref } from "vue";

/**
 * 一条提示浮出来、过一会儿自己走（交互第二版：导出结果提示一律 8 秒）。
 *
 * 鼠标停在上面时**暂停**计时：人正在读的时候它消失，比不消失更糟。
 * 移开之后按剩下的时间接着走，不重新计满。换了一句新提示就重新计满。
 */
export const useAutoDismiss = <T>(message: Ref<T | null>, lifetimeMs: number) => {
  let timer: ReturnType<typeof globalThis.setTimeout> | null = null;
  let remaining = lifetimeMs;
  let startedAt = 0;
  const paused = ref(false);

  const clear = (): void => {
    if (timer !== null) {
      globalThis.clearTimeout(timer);
      timer = null;
    }
  };

  const run = (): void => {
    clear();
    startedAt = Date.now();
    timer = globalThis.setTimeout(() => {
      timer = null;
      message.value = null;
    }, remaining);
  };

  watch(message, (text) => {
    clear();
    paused.value = false;
    remaining = lifetimeMs;
    if (text !== null) run();
  });

  const pause = (): void => {
    if (timer === null) return;
    clear();
    remaining = Math.max(0, remaining - (Date.now() - startedAt));
    paused.value = true;
  };

  const resume = (): void => {
    if (!paused.value || message.value === null) return;
    paused.value = false;
    run();
  };

  const dismiss = (): void => {
    message.value = null;
  };

  onBeforeUnmount(clear);

  return { pause, resume, dismiss, paused };
};

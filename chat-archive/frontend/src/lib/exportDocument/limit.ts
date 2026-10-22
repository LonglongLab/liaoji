/**
 * 同时最多跑几件事的闸门（PSU-2.168 评审）。
 *
 * 导出前要取的原图和要解码的图都可能上百张。一口气 `Promise.all` 出去的话，
 * 这一百多份字节（和它们的解码结果）会同时待在内存里——勾了「用原图」时一张
 * 就是几 MB。闸门只管「同时几件」，不管顺序，也不吞掉任何一件的失败。
 */

/** 一次导出里取字节 / 解码的并发上限。 */
export const EXPORT_CONCURRENCY = 4;

export type Limiter = <T>(task: () => Promise<T>) => Promise<T>;

export const createLimiter = (max: number): Limiter => {
  const cap = Number.isFinite(max) && max >= 1 ? Math.floor(max) : 1;
  let running = 0;
  const waiting: (() => void)[] = [];

  // 空出来的名额**直接交给**排队的下一个，而不是先减、再等它醒来自己加：
  // 那样写对不对取决于微任务的先后，交接写法不依赖这个。
  const release = (): void => {
    const next = waiting.shift();
    if (next === undefined) running -= 1;
    else next();
  };

  return async <T>(task: () => Promise<T>): Promise<T> => {
    if (running >= cap) {
      await new Promise<void>((resolve) => {
        waiting.push(resolve);
      });
    } else {
      running += 1;
    }
    try {
      return await task();
    } finally {
      release();
    }
  };
};

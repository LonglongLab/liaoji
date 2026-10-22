import { nextTick, ref } from "vue";

/**
 * 「打开会话就停在最新一条」的那段定位，两端共用一份。
 *
 * 微信式默认：不恢复「上次读到哪」——那条消息常常不在首页里，会把人直接丢进
 * 上下文窗口模式并挂出返回横幅，成本远高于收益。
 *
 * 为什么不是「渲染完滚到底」一行代码：转录跑在虚拟滚动上，行高要量过才知道，
 * 而消息是分页到达的。所以这里要处理三件事——
 *
 * 1. **等数据到齐**：`loading` 期间与空列表都不动，等第一页真的有行了再滚；
 * 2. **同一会话只滚一次**：滚完就把 `pending` 放下，之后用户自己往上翻不会被
 *    新到的页拽回底部；
 * 3. **换会话即作废**：`generation` 是一个单调计数，切会话时 +1。一次定位跨过
 *    了 await，回来发现代不对就整段丢弃，并为**当前**这一代重跑一次——否则在
 *    快速连点会话列表时，最后落点会是上一个会话的底部。
 */
export interface TranscriptInitialPosition {
  /** 切换会话/账号时调用：作废在途的定位，并按需要排一次新的。 */
  reset: (shouldPosition: boolean) => void;
  /** 数据有变化时调用（消息到达、加载态变化）；不满足条件时什么都不做。 */
  schedule: () => void;
}

export const useTranscriptInitialPosition = (options: {
  /** 现在还在取第一页吗。 */
  loading: () => boolean;
  /** 已加载的转录行数；0 表示还没有可落脚的地方。 */
  rowCount: () => number;
  /** 把视图滚到 `index` 这一行，对齐到底部。 */
  scrollToIndex: (index: number) => void;
}): TranscriptInitialPosition => {
  const pending = ref(false);
  let generation = 0;
  let positioning = false;

  const schedule = async (): Promise<void> => {
    const current = generation;
    if (positioning || !pending.value || options.loading() || options.rowCount() === 0) {
      return;
    }
    positioning = true;
    try {
      if (current !== generation) return;
      await nextTick();
      const latest = options.rowCount() - 1;
      if (latest >= 0) options.scrollToIndex(latest);
    } finally {
      const isCurrent = current === generation;
      if (isCurrent) pending.value = false;
      positioning = false;
      // 这一趟服务的是已经作废的那一代，当前这一代还没人管——补一趟。
      if (!isCurrent) void schedule();
    }
  };

  return {
    reset: (shouldPosition: boolean): void => {
      generation += 1;
      pending.value = shouldPosition;
      void schedule();
    },
    schedule: (): void => {
      void schedule();
    },
  };
};

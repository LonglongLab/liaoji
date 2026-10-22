import { onScopeDispose, watch, type Ref } from "vue";

/**
 * 列表稳定之后，把**最靠前的几条可见会话**的最新一页先要回来。
 *
 * ## 为什么要有它
 *
 * 远程站点上，点开一条没读过的会话至少要等一次公网往返：手机上那是半屏空白
 * 加载态，而这段等待完全落在用户按下去之后。列表稳下来之后的那几秒机器是闲
 * 着的，先把最可能被点的那几条要回来，点击当场就有正文。
 *
 * ## 三条自己给自己划的线
 *
 * - **只取看得见的前三条**。一页 100 条会话，全预取等于把整份归档拉一遍，
 *   省下的那一次往返远抵不上多花的九十多次。
 * - **并发 1**。预取是背景活儿，它不该和用户真正在等的那次请求抢链路；跨境
 *   链路上并行几条只会把每一条都拖慢。
 * - **省流量模式下只认明确意图**。`Save-Data` 是用户说「别替我花流量」，空闲
 *   预取正是替他花的那种；而指针按下、键盘聚焦到某一行是他自己要打开它，
 *   那一份流量一秒之后照样要花，只是花在他等着的时候。
 *
 * ## 作用域
 *
 * `scope` 是「这批会话属于谁」——账号、归档。它一变，队列里排着的旧作用域
 * 就不再发：那些会话标识在新作用域下可能根本不存在，发出去只是白问，答回来
 * 还会在缓存里留下一份没人要的页。已经在途的那一次不掐（它的结果落在**它
 * 自己那条键**上，污染不了新作用域），但它落地之后不再推着队列往下走。
 */
export interface ConversationMessagePrefetchOptions<Scope> {
  /** 这批会话属于谁；`null` = 还没定下来，这时候一条都不预取。 */
  scope: Ref<Scope | null>;
  /** 按列表顺序排好的候选会话标识，通常是「现在真的在视口里的那几行」。 */
  targets: Ref<readonly string[]>;
  /** 列表稳定了没有。还在加载或翻页时不抢它的链路。 */
  settled: Ref<boolean>;
  /** 把一条会话的最新一页要回来。失败由这里咽掉，不出声。 */
  prefetch: (conversationId: string, scope: Scope) => Promise<unknown>;
  /** 最多预取几条，缺省三条。 */
  limit?: number;
  /** 空闲窗口的排期；返回一个取消函数。用例交一个同步的进来。 */
  schedule?: (task: () => void) => () => void;
  /** 现在是不是省流量模式；缺省读 `navigator.connection.saveData`。 */
  saveData?: () => boolean;
}

export interface ConversationMessagePrefetcher {
  /**
   * 现在就把这一条要回来——用户按在它上面、或者键盘焦点走到了它。
   *
   * 不排队、也不看 `Save-Data`：这是明确意图，等在空闲队列后面就失去意义了。
   */
  prefetchNow: (conversationId: string) => void;
}

/** 看得见的行里，最靠前的这几条值得提前要。 */
export const PREFETCH_VISIBLE_LIMIT = 3;

interface IdleWindow {
  requestIdleCallback?: (
    callback: () => void,
    options?: { timeout: number },
  ) => number;
  cancelIdleCallback?: (handle: number) => void;
}

/**
 * 缺省的空闲窗口。
 *
 * `requestIdleCallback` 在 Safari（也就是这份产品最主要的手机端）上没有，
 * 退到一小段延时：要的只是「别和首屏渲染抢那一下」，不是精确的空闲判定。
 */
const idleSchedule = (task: () => void): (() => void) => {
  const view = globalThis as unknown as IdleWindow;
  if (typeof view.requestIdleCallback === "function") {
    const handle = view.requestIdleCallback(task, { timeout: 2000 });
    return () => view.cancelIdleCallback?.(handle);
  }
  const handle = setTimeout(task, 200);
  return () => { clearTimeout(handle); };
};

interface SaveDataConnection {
  connection?: { saveData?: boolean };
}

const saveDataOn = (): boolean =>
  (navigator as Navigator & SaveDataConnection).connection?.saveData === true;

export const useConversationMessagePrefetch = <Scope>(
  options: ConversationMessagePrefetchOptions<Scope>,
): ConversationMessagePrefetcher => {
  const limit = options.limit ?? PREFETCH_VISIBLE_LIMIT;
  const schedule = options.schedule ?? idleSchedule;
  const saveData = options.saveData ?? saveDataOn;
  // 代次只用来丢弃**过期作用域排着的那几条**，与 `useConversationNameHydration`
  // 同一种写法；在途的那一次照常落地，它写的是它自己那条键。
  let generation = 0;
  let cancelIdle: (() => void) | null = null;
  let disposed = false;

  const runQueue = async (
    scope: Scope,
    conversationIds: readonly string[],
    current: number,
  ): Promise<void> => {
    for (const conversationId of conversationIds) {
      if (current !== generation || disposed) return;
      try {
        await options.prefetch(conversationId, scope);
      } catch {
        // 这一页本来就没人在等：报错只会把一条没人要的请求说成一次故障。
        // 真要打开它时那条查询自己会再问一次，并照常显示错误。
      }
    }
  };

  watch(
    [options.scope, options.targets, options.settled],
    () => {
      const current = ++generation;
      cancelIdle?.();
      cancelIdle = null;
      const scope = options.scope.value;
      if (disposed || scope === null || !options.settled.value) return;
      if (saveData()) return;
      cancelIdle = schedule(() => {
        if (current !== generation || disposed) return;
        void runQueue(scope, options.targets.value.slice(0, limit), current);
      });
    },
    { immediate: true },
  );

  onScopeDispose(() => {
    disposed = true;
    generation += 1;
    cancelIdle?.();
    cancelIdle = null;
  });

  return {
    prefetchNow: (conversationId: string): void => {
      const scope = options.scope.value;
      if (disposed || scope === null || conversationId === "") return;
      void options.prefetch(conversationId, scope).catch(() => undefined);
    },
  };
};

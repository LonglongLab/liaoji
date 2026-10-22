import { computed, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

/**
 * 一层浮层 = 地址栏上的一格（PSU-2.120）。
 *
 * ## 为什么浮层要惊动路由
 *
 * 手机上没有 Esc，也没有「关闭」这个手势——读者关掉一层的动作只有一个：系统「返回」。
 * 而「返回」弹的是**历史**，不是组件里的那个 `ref`。所以一层浮层如果开的时候没有压过
 * 历史，那一下返回弹掉的就是它下面那一层：点开一张图再返回，人会落在会话列表上，
 * 中间的聊天记录整页被跳过。这正是用户 2026-09-13 报的那一条。
 *
 * 反过来，只要开一层就压一条，「返回」的语义就统一成一句话：**退一层**。
 *
 * ## 这一份存在的理由
 *
 * 这套动作本机端早就在用（`?media=` / `?file=`），只是写在 `useWorkspaceRoute` 里，
 * 而远程工作区与两个独立搜索页都够不着它，于是各自把浮层留成了组件内部状态——
 * 少做是静默的，两端各自的测试全绿。把它抽成一份，新的浮层接上去只有一行，
 * 也就没有「再写一份」的动机（见 `local-remote-boundary.md` §6 第 8 条）。
 */

/**
 * 地址栏里那个值的一种读法：空串与数组都当成「没有」。
 *
 * 两端曾经逐字节相同地各写了一份，现在只有这一份。
 */
export const queryString = (value: unknown): string | null => {
  if (typeof value === "string") return value === "" ? null : value;
  if (Array.isArray(value)) {
    const first: unknown = value[0];
    return typeof first === "string" && first !== "" ? first : null;
  }
  return null;
};

/** 把当前这一份 query 原样抄成字符串表：压新的一层时底下几层要照旧带着。 */
export const queryStrings = (query: Record<string, unknown>): Record<string, string> => {
  const copied: Record<string, string> = {};
  for (const [key, value] of Object.entries(query)) {
    const normalized = queryString(value);
    if (normalized !== null) copied[key] = normalized;
  }
  return copied;
};

/**
 * 历史里还有没有「上一条」。
 *
 * 别人发来的链接直接打开的那种没有——那时候「返回」不能真的后退，否则一下就把人
 * 弹出这个站点；正确动作是 `replace` 掉这一格，原地退一层。
 */
export const canNavigateBack = (): boolean => {
  const state: unknown = globalThis.history.state;
  const previous =
    state !== null && typeof state === "object" && "back" in state
      ? (state as { back?: unknown }).back
      : null;
  return typeof previous === "string" && previous !== "";
};

export interface HistoryLayer {
  /**
   * 这一格现在的值；`null` = 这一层没开。
   *
   * 是个 getter 而不是 `ComputedRef`：调用点几乎全是模板与 `computed`，读一层浮层
   * 写成 `layer.value.value` 只会让人抄错。取值时才读底下那个 computed，依赖照样收得到。
   */
  readonly value: string | null;
  /** 开一层：压一条历史。 */
  open: (value: string) => void;
  /** 层还是那一层，只是内容换了（抽屉翻到另一段）：换值不压新历史。 */
  set: (value: string) => void;
  /** 关一层：有历史就真后退（与浏览器返回键一致），没有就原地 `replace` 掉这一格。 */
  close: () => void;
  /**
   * 还有没有自己发出的导航在路上。
   *
   * 读 `value` 判断「这一层是不是被系统『返回』关掉的」时要看它：队列没排空时
   * 地址栏停的可能是**上一步**的值，照它去改界面就会把刚做的动作顶回去。
   */
  readonly pending: boolean;
}

export const useHistoryLayer = (key: string): HistoryLayer => {
  const route = useRoute();
  const router = useRouter();

  const current = computed(() => queryString(route.query[key]));

  const withKey = (next: string): Record<string, string> => ({
    ...queryStrings(route.query),
    [key]: next,
  });

  /**
   * 一层一层来：上一次导航落地之前不发下一次。
   *
   * 路由导航是异步的，而这三个动作常常连着发（打开抽屉紧接着翻一段）。不排队的话
   * 后发的 `replace` 可能先落地、先发的 `push` 后落地，地址栏最后停在**旧值**上，
   * 界面再按它把刚选的那一段顶回去——这不是理论上的：`SearchView` 的抽屉用例
   * 就是这么红的。判断也要等到轮到自己时再做，否则判的是过期的现状。
   */
  let queue: Promise<unknown> = Promise.resolve();
  const inFlight = ref(0);
  const enqueue = (task: () => unknown): void => {
    inFlight.value += 1;
    queue = queue
      .then(() => task())
      .catch(() => undefined)
      .finally(() => {
        inFlight.value -= 1;
      });
  };

  const open = (next: string): void => {
    enqueue(() => {
      // 同一层重复开（重复点同一张图）不该各压一条历史，否则返回要按两次。
      if (current.value === next) return;
      return router.push({ query: withKey(next) });
    });
  };

  const set = (next: string): void => {
    enqueue(() => {
      if (current.value === next) return;
      return router.replace({ query: withKey(next) });
    });
  };

  const close = (): void => {
    enqueue(() => {
      if (current.value === null) return;
      if (canNavigateBack()) {
        router.back();
        return;
      }
      const fallback = queryStrings(route.query);
      Reflect.deleteProperty(fallback, key);
      return router.replace({ query: fallback });
    });
  };

  return {
    get value() {
      return current.value;
    },
    get pending() {
      return inFlight.value > 0;
    },
    open,
    set,
    close,
  };
};

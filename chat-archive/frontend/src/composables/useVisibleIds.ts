import { onScopeDispose, ref, type Ref } from "vue";

/**
 * 哪几行现在真的在视口里（PSU-2.122）。
 *
 * 为什么需要它：会话列表一页 100 条，而屏幕上看得见的是 8 条左右。此前头像
 * 是按**已加载的全部**去取的——一次 200 个请求（每张两次往返），其中九成是
 * 为了没人看的行。转录里的图早就这么做了（`MessageTypeRenderer` 的
 * `mediaObserver`），列表行上的头像只是一直没做。
 *
 * 与那处的两点不同：
 *
 * - **来回都算**：离开视口的行要退出集合，不然滚一遍 614 条会话就等于把全库
 *   头像都取了一遍，与不做视口门没有区别。退出集合不等于扔掉字节——store 的
 *   闲置队列还留着，滚回来是零请求。
 * - **`null` 是一个答案**：这一端没有 `IntersectionObserver` 时答 `null`，
 *   意思是「视口门不可用，按全部算」。答空数组会让调用方以为「什么都不用取」，
 *   那是一页空白头像。
 */

interface ObservedEntry {
  readonly target: Element;
  readonly isIntersecting: boolean;
}

/** 真的 `IntersectionObserver` 结构上满足它；用例交一个假的进来。 */
export interface ElementObserver {
  observe: (element: Element) => void;
  unobserve: (element: Element) => void;
  disconnect: () => void;
}

export interface VisibleIds {
  /**
   * 挂在每一行上：`:ref="(el) => observe(el, item.id)"`。
   *
   * Vue 在这一行卸载时会拿 `null` 再调一次**同一个闭包**，所以 id 还在——
   * 这正是能按行退观察的原因。
   */
  observe: (element: Element | null, id: string) => void;
  /** 现在看得见的 id；`null` = 这一端没有视口门，按全部算。 */
  ids: Ref<readonly string[] | null>;
}

/** 上下各留这么宽的缓冲：滚过去之前就开始取，不至于滚到了才空一下。 */
const DEFAULT_ROOT_MARGIN = "320px 0px";

export const useVisibleIds = (
  options: {
    rootMargin?: string;
    createObserver?: (
      callback: (entries: readonly ObservedEntry[]) => void,
      init: { rootMargin: string },
    ) => ElementObserver | null;
  } = {},
): VisibleIds => {
  const rootMargin = options.rootMargin ?? DEFAULT_ROOT_MARGIN;
  const create =
    options.createObserver ??
    ((callback, init) =>
      typeof IntersectionObserver === "undefined"
        ? null
        : new IntersectionObserver((entries) => { callback(entries); }, init));

  const visible = new Set<string>();
  const idOf = new Map<Element, string>();
  const elementOf = new Map<string, Element>();
  const ids = ref<readonly string[] | null>(null);

  const publish = (): void => {
    ids.value = [...visible];
  };

  const observer = create((entries) => {
    let changed = false;
    for (const entry of entries) {
      const id = idOf.get(entry.target);
      if (id === undefined) continue;
      if (entry.isIntersecting) {
        if (!visible.has(id)) {
          visible.add(id);
          changed = true;
        }
      } else if (visible.delete(id)) {
        changed = true;
      }
    }
    if (changed) publish();
  }, { rootMargin });

  if (observer !== null) publish();

  const forget = (element: Element): void => {
    const id = idOf.get(element);
    if (id === undefined) return;
    idOf.delete(element);
    if (elementOf.get(id) === element) elementOf.delete(id);
    observer?.unobserve(element);
    if (visible.delete(id)) publish();
  };

  const observe = (element: Element | null, id: string): void => {
    if (observer === null) return;
    if (element === null) {
      const previous = elementOf.get(id);
      if (previous !== undefined) forget(previous);
      return;
    }
    if (idOf.get(element) === id) return;
    // 同一个 id 换了元素（列表重排、v-for 复用）：旧的先摘掉，否则它会一直
    // 以"看不见"的身份留在 map 里，把这个 id 永远钉在集合外。
    const previous = elementOf.get(id);
    if (previous !== undefined && previous !== element) forget(previous);
    idOf.set(element, id);
    elementOf.set(id, element);
    observer.observe(element);
  };

  onScopeDispose(() => {
    observer?.disconnect();
    idOf.clear();
    elementOf.clear();
    visible.clear();
  });

  return { observe, ids };
};

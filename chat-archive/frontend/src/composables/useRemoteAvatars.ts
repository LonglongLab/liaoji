import { onScopeDispose, ref, type Ref } from "vue";

import { AvatarObjectUrlStore } from "@/lib/avatarObjectUrls";

/**
 * 远程头像在视图里的那一层（PSU-2.47）。
 *
 * 订阅按 **blob_id 的集合**整体换，不按行。虚拟滚动下按行订阅意味着每滚一屏
 * 就是一轮 subscribe/unsubscribe，而同一张图（默认头像、同一个人在多条会话里）
 * 会被反复计数；换成集合之后，一次滚动只是集合的一次差分。
 *
 * `retain` 是**替换**语义而不是追加：调用方每次把「现在想要的全部 id」交出来，
 * 不在里面的就退订。追加语义要求调用方自己记得放手，而那正是这类缓存最常见的
 * 泄漏方式。
 */
export interface RemoteAvatars {
  /** 现在想要这些 blob 的头像；不在列表里的立即退订。 */
  retain: (blobIds: readonly (string | null | undefined)[]) => void;
  /** 这个 blob 现在的 `blob:` 地址，没有就是 null。 */
  srcFor: (blobId: string | null | undefined) => string | null;
  /**
   * 全部退订并回收。
   *
   * **换账号不用调它了**（PSU-2.122）：`blob_id` 是账号加盐的，同一个 id 的
   * 字节永远是同一份，留着不会拿错图；而 store 是全应用共用的，在这里清等于
   * 把别的页面的图也一起扔掉。想"这一页不要了"用 `retain([])`。
   */
  reset: () => void;
  /** 还留着几个 objectURL。泄漏与否只能靠这个数字看出来。 */
  liveCount: Ref<number>;
}

/**
 * 交一个 `load` 进来 = 这个组件自己拥有一份 store，卸载时连字节一起扔掉。
 * 交一个 **store** 进来 = 用别人的（`sharedAvatarStore()`），卸载只退订。
 *
 * 后者是 PSU-2.122 的那条路：底部 Tab 切走再切回来不该重下一遍头像。前者留着
 * 是因为用例要的正是"这一份归我、关掉就该干净"——共享的那份没法断言回收。
 */
export const useRemoteAvatars = (
  source: ((blobId: string) => Promise<Blob>) | AvatarObjectUrlStore,
  options: { maxIdle?: number } = {},
): RemoteAvatars => {
  const owned = !(source instanceof AvatarObjectUrlStore);
  const store = owned
    ? new AvatarObjectUrlStore({
        load: source,
        ...(options.maxIdle === undefined ? {} : { maxIdle: options.maxIdle }),
      })
    : source;
  const urls = ref<Record<string, string>>({});
  const liveCount = ref(0);
  const held = new Map<string, () => void>();

  const publish = (blobId: string, url: string | null): void => {
    // 整份换掉而不是就地改：这是一个 `ref` 上的对象，就地写不会触发依赖它的
    // computed 重算。null 的那一条**不留键**——留一个 undefined 的键会让
    // 「没取到」和「取到了空的」在读的时候长得一样。
    const next = Object.fromEntries(
      Object.entries(urls.value).filter(([key]) => key !== blobId),
    );
    if (url !== null) next[blobId] = url;
    urls.value = next;
    liveCount.value = store.liveCount;
  };

  const retain = (blobIds: readonly (string | null | undefined)[]): void => {
    const wanted = new Set<string>();
    for (const id of blobIds) if (id !== null && id !== undefined && id !== "") wanted.add(id);
    for (const [id, off] of held) {
      if (wanted.has(id)) continue;
      off();
      held.delete(id);
      // 放手之后 store 可能已经把它回收了，而我们不会再收到那一声通知（订阅
      // 都退了）。留着那个地址就是留着一个**已经 revoke 的** `blob:`——图会
      // 裂开退回首字母，看起来像「这个人没有头像」。
      if (store.urlOf(id) === null) publish(id, null);
    }
    for (const id of wanted) {
      if (held.has(id)) continue;
      held.set(id, store.subscribe(id, (url) => { publish(id, url); }));
    }
    liveCount.value = store.liveCount;
  };

  const releaseAll = (): void => {
    for (const off of held.values()) off();
    held.clear();
  };

  const reset = (): void => {
    releaseAll();
    store.clear();
    urls.value = {};
    liveCount.value = 0;
  };

  onScopeDispose(() => {
    releaseAll();
    // 别人的 store 不归这里关：底部 Tab 切走一次就把整份字节扔掉，正是
    // PSU-2.122 要修的那件事。自己建的那份仍然关，否则用例断言不了回收。
    if (owned) store.close();
  });

  return {
    retain,
    srcFor: (blobId) =>
      blobId === null || blobId === undefined ? null : urls.value[blobId] ?? null,
    reset,
    liveCount,
  };
};

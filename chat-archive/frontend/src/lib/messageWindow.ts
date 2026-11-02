/** Frontend-only paging state, never sent as a wire DTO. */
export const MESSAGE_MAX_PAGES = 3;
export const MESSAGE_MAX_PAGE_ITEMS = 100;
export const CONTEXT_MAX_ITEMS = 121;
export const MESSAGE_MAX_QUERIES = 6;

export const pruneMessageMeasurements = (cache: Map<string | number | bigint, number> | undefined, ids: ReadonlySet<string>): void => {
  if (cache === undefined) return;
  for (const key of cache.keys()) if (typeof key !== "string" || !ids.has(key)) cache.delete(key);
};

export type MessagePageParam = null | string | { direction: "older" | "newer"; anchor: string };
export interface BoundedMessagePage<Item> {
  items: Item[];
  next_cursor: string | null;
  has_more?: boolean;
  window_has_older?: boolean;
  window_has_newer?: boolean;
}

export const messagePagePolicy = <Item>(idOf: (item: Item) => string) => ({
  initialPageParam: null as MessagePageParam,
  maxPages: MESSAGE_MAX_PAGES,
  getNextPageParam: (page: BoundedMessagePage<Item>): MessagePageParam | undefined => {
    if (page.next_cursor !== null) return page.next_cursor;
    const first = page.items[0];
    return page.window_has_older === true && first !== undefined ? { direction: "older", anchor: idOf(first) } : undefined;
  },
  getPreviousPageParam: (page: BoundedMessagePage<Item>): MessagePageParam | undefined => {
    const last = page.items.at(-1);
    return page.window_has_newer === true && last !== undefined ? { direction: "newer", anchor: idOf(last) } : undefined;
  },
});

/** Both directions use one lookahead row, which is not retained. */
export const fetchWindowPage = async <Item>(
  param: Exclude<MessagePageParam, string | null>,
  signal: AbortSignal,
  fetch: (id: string, options: { before: number; after: number; signal: AbortSignal }) => Promise<{ items: Item[] }>,
  idOf: (item: Item) => string,
): Promise<BoundedMessagePage<Item>> => {
  const page = await fetch(param.anchor, {
    before: param.direction === "older" ? MESSAGE_MAX_PAGE_ITEMS + 1 : 0,
    after: param.direction === "newer" ? MESSAGE_MAX_PAGE_ITEMS + 1 : 0,
    signal,
  });
  signal.throwIfAborted();
  const index = page.items.findIndex(item => idOf(item) === param.anchor);
  if (index < 0) throw new Error("这段记录的阅读位置已不可用，请重试或跳至最新。");
  const candidates = param.direction === "older" ? page.items.slice(0, index) : page.items.slice(index + 1);
  return {
    items: param.direction === "older" ? candidates.slice(-MESSAGE_MAX_PAGE_ITEMS) : candidates.slice(0, MESSAGE_MAX_PAGE_ITEMS),
    next_cursor: null,
    window_has_older: param.direction === "older" ? candidates.length > MESSAGE_MAX_PAGE_ITEMS : true,
    window_has_newer: param.direction === "newer" ? candidates.length > MESSAGE_MAX_PAGE_ITEMS : true,
  };
};

export const markCursorPage = <Item, Page extends BoundedMessagePage<Item>>(page: Page, cursor: string | null): Page => ({
  ...page,
  window_has_older: page.next_cursor !== null,
  window_has_newer: cursor !== null,
});

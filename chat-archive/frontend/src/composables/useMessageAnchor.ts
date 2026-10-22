import { computed, ref, type ComputedRef } from "vue";

import { ArchiveApiError } from "@/api/archiveApi";
import type { RenderMessage } from "@/lib/transcript";

export type JumpOutcome =
  | { kind: "loaded"; index: number }
  | { kind: "window" }
  | { kind: "error"; message: string; code: string | null };

/** 定位高亮只闪一下就退场，避免它被误读成"这条消息被选中了"。与 CSS 的 msg-highlight-flash 时长一致。 */
export const HIGHLIGHT_VISIBLE_MS = 2400;
const WINDOW_CONTEXT_SIZE = 20;
// 每个方向都**多问一条**。那一条不进窗口，它只回答「这一头还有没有」——
// 少了它，「读完了」与「刚好取满一屏」在响应里长得一模一样。
const WINDOW_REQUEST_SIZE = WINDOW_CONTEXT_SIZE + 1;

export interface AnchorWindow {
  items: RenderMessage[];
  anchorMessageId: string;
}

/**
 * 窗口从哪里取。
 *
 * 两端取窗口的端点不同（本机 `/messages/window`，远端
 * `/remote/…/messages/window`，记录模型 v9 起），但**窗口这件事**——取多少、
 * 怎么拼、闪多久、两头怎么续读——两端一个字都不差。所以这里注入的是"取"，
 * 不是"端"：写成 `if (远程)` 会让同一段行为有两份实现，而它们迟早各自演化。
 *
 * `scope` 是这一次定位属于哪一段（账号 + 会话，远端再加归档）。它变了，
 * 在途的结果就作废——读者已经走到别处，把旧窗口铺上去比什么都不做更糟。
 * `null` 表示还没选会话。
 */
export interface AnchorWindowSource {
  scope: () => string | null;
  fetch: (
    anchorMessageId: string,
    request: { before: number; after: number },
  ) => Promise<AnchorWindow>;
}

export const useMessageAnchor = (
  source: AnchorWindowSource,
  loadedMessages: ComputedRef<RenderMessage[]>,
) => {
  const windowItems = ref<RenderMessage[] | null>(null);
  const windowHasOlder = ref(false);
  const windowFetchingOlder = ref(false);
  const windowHasNewer = ref(false);
  const windowFetchingNewer = ref(false);
  const highlightedId = ref<string | null>(null);
  const inWindowMode = computed(() => windowItems.value !== null);
  let fadeTimer: ReturnType<typeof setTimeout> | null = null;
  let windowGeneration = 0;

  const clearFadeTimer = (): void => {
    if (fadeTimer === null) return;
    clearTimeout(fadeTimer);
    fadeTimer = null;
  };

  const flashHighlight = (messageId: string): void => {
    clearFadeTimer();
    highlightedId.value = messageId;
    fadeTimer = setTimeout(() => {
      fadeTimer = null;
      highlightedId.value = null;
    }, HIGHLIGHT_VISIBLE_MS);
  };

  const normalizeWindow = (
    items: RenderMessage[],
    anchorMessageId: string,
  ): { items: RenderMessage[]; hasOlder: boolean; hasNewer: boolean } => {
    const anchorIndex = items.findIndex((item) => item.id === anchorMessageId);
    if (anchorIndex < 0) return { items, hasOlder: false, hasNewer: false };
    const older = items.slice(0, anchorIndex);
    const newer = items.slice(anchorIndex + 1);
    return {
      items: [
        ...older.slice(-WINDOW_CONTEXT_SIZE),
        // 切一格而不是下标取：`noUncheckedIndexedAccess` 下后者是 `| undefined`，
        // 而这里锚点一定在——刚刚才用它算出的下标。
        ...items.slice(anchorIndex, anchorIndex + 1),
        ...newer.slice(0, WINDOW_CONTEXT_SIZE),
      ],
      hasOlder: older.length > WINDOW_CONTEXT_SIZE,
      hasNewer: newer.length > WINDOW_CONTEXT_SIZE,
    };
  };

  const failure = (error: unknown, fallback: string): JumpOutcome => ({
    kind: "error",
    message: error instanceof ArchiveApiError ? error.message : fallback,
    // 码带出去，调用方才分得清"这条定位不到"与"这一端还没有定位这回事"
    // （远端 v9 以下答 REMOTE_RECORD_MODEL_TOO_OLD）——两者该给读者的话不同。
    code: error instanceof ArchiveApiError ? error.code : null,
  });

  const jumpTo = async (messageId: string): Promise<JumpOutcome> => {
    if (windowItems.value === null) {
      const index = loadedMessages.value.findIndex((item) => item.id === messageId);
      if (index >= 0) {
        flashHighlight(messageId);
        return { kind: "loaded", index };
      }
    } else if (windowItems.value.some((item) => item.id === messageId)) {
      flashHighlight(messageId);
      return { kind: "window" };
    }
    const scope = source.scope();
    if (scope === null) {
      return { kind: "error", message: "尚未选择会话", code: null };
    }
    try {
      const generation = ++windowGeneration;
      const window = await source.fetch(messageId, {
        before: WINDOW_REQUEST_SIZE,
        after: WINDOW_REQUEST_SIZE,
      });
      if (generation !== windowGeneration || scope !== source.scope()) {
        return { kind: "window" };
      }
      const normalized = normalizeWindow(window.items, window.anchorMessageId);
      windowItems.value = normalized.items;
      windowHasOlder.value = normalized.hasOlder;
      windowHasNewer.value = normalized.hasNewer;
      flashHighlight(window.anchorMessageId);
      return { kind: "window" };
    } catch (error: unknown) {
      return failure(error, "未能定位这条消息");
    }
  };

  const loadOlderWindow = async (): Promise<string | null> => {
    if (
      windowItems.value === null ||
      !windowHasOlder.value ||
      windowFetchingOlder.value
    ) {
      return null;
    }
    if (source.scope() === null) return "尚未选择会话";
    const boundary = windowItems.value[0];
    if (boundary === undefined) {
      windowHasOlder.value = false;
      return null;
    }
    const generation = windowGeneration;
    windowFetchingOlder.value = true;
    try {
      const window = await source.fetch(boundary.id, {
        before: WINDOW_REQUEST_SIZE,
        after: 0,
      });
      if (generation !== windowGeneration) return null;
      const normalized = normalizeWindow(window.items, window.anchorMessageId);
      const currentItems = windowItems.value;
      const existingIds = new Set(currentItems.map((item) => item.id));
      const additions = normalized.items.filter((item) => !existingIds.has(item.id));
      if (additions.length > 0) {
        windowItems.value = [...additions, ...currentItems];
      }
      windowHasOlder.value = additions.length > 0 && normalized.hasOlder;
      return null;
    } catch (error: unknown) {
      return error instanceof ArchiveApiError ? error.message : "未能加载更早消息";
    } finally {
      windowFetchingOlder.value = false;
    }
  };

  /**
   * 往新的那一头续读。
   *
   * 与 `loadOlderWindow` 逐字对称，只是方向反过来：拿窗口最后那一条当新锚点，
   * 只要它之后的一段。少了这一半，定位窗口就是个死胡同——落进去以后只能往回
   * 读，永远走不回读者刚才在看的最新一条。
   */
  const loadNewerWindow = async (): Promise<string | null> => {
    if (
      windowItems.value === null ||
      !windowHasNewer.value ||
      windowFetchingNewer.value
    ) {
      return null;
    }
    if (source.scope() === null) return "尚未选择会话";
    const boundary = windowItems.value[windowItems.value.length - 1];
    if (boundary === undefined) {
      windowHasNewer.value = false;
      return null;
    }
    const generation = windowGeneration;
    windowFetchingNewer.value = true;
    try {
      const window = await source.fetch(boundary.id, {
        before: 0,
        after: WINDOW_REQUEST_SIZE,
      });
      if (generation !== windowGeneration) return null;
      const normalized = normalizeWindow(window.items, window.anchorMessageId);
      const currentItems = windowItems.value;
      const existingIds = new Set(currentItems.map((item) => item.id));
      const additions = normalized.items.filter((item) => !existingIds.has(item.id));
      if (additions.length > 0) {
        windowItems.value = [...currentItems, ...additions];
      }
      windowHasNewer.value = additions.length > 0 && normalized.hasNewer;
      return null;
    } catch (error: unknown) {
      return error instanceof ArchiveApiError ? error.message : "未能加载更新消息";
    } finally {
      windowFetchingNewer.value = false;
    }
  };

  const exitWindow = (): void => {
    windowGeneration += 1;
    clearFadeTimer();
    windowItems.value = null;
    windowHasOlder.value = false;
    windowFetchingOlder.value = false;
    windowHasNewer.value = false;
    windowFetchingNewer.value = false;
    highlightedId.value = null;
  };

  return {
    windowItems,
    highlightedId,
    windowHasOlder,
    windowFetchingOlder,
    windowHasNewer,
    windowFetchingNewer,
    inWindowMode,
    jumpTo,
    loadOlderWindow,
    loadNewerWindow,
    exitWindow,
  };
};

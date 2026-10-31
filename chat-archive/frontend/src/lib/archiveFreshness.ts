import type { InfiniteData } from "@tanstack/vue-query";
import type { ConversationPage, ConversationSummary, MessagePage } from "@/api/archiveApi";
import type { RenderMessage } from "@/lib/transcript";

// Display-only rows from the previous cursor chain, not proof of page coverage.
type ConversationCachePage = ConversationPage & { retainedItems?: ConversationSummary[] };

export const flattenConversationPages = (pages: readonly ConversationCachePage[]): ConversationSummary[] => {
  const rows = new Map<string, ConversationSummary>();
  for (const page of pages) for (const item of page.items) if (!rows.has(item.id)) rows.set(item.id, item);
  // Once the current chain ends it is authoritative, including removed matches.
  if (pages.at(-1)?.next_cursor != null) {
    for (const page of pages) for (const item of page.retainedItems ?? []) if (!rows.has(item.id)) rows.set(item.id, item);
  }
  return [...rows.values()];
};

export const rebaseConversationPages = (
  cached: InfiniteData<ConversationCachePage>, latest: ConversationPage,
): InfiniteData<ConversationCachePage> => {
  const ids = new Set(latest.items.map(item => item.id));
  const retainedItems = latest.next_cursor === null ? [] : flattenConversationPages(cached.pages).filter(item => !ids.has(item.id));
  // Rebuild lazily from the fresh cursor on the next user-requested page. Old
  // rows remain visible, but cannot cause a gap or dictate a stale end cursor.
  return { pages: [{ ...latest, retainedItems }], pageParams: [null] };
};

/** Pages run newest-to-oldest; rows INSIDE a page run oldest-to-newest. */
export const mergeLatestPage = (
  cached: InfiniteData<MessagePage>, latest: MessagePage,
): InfiniteData<MessagePage> | null => {
  const first = cached.pages[0];
  if (first === undefined || latest.items.length === 0) return cached;
  if (first.items.length === 0) return { ...cached, pages: [latest, ...cached.pages.slice(1)] };
  const byId = new Map(latest.items.map(message => [message.id, message]));
  if (!first.items.some(message => byId.has(message.id))) return null;
  // An older response may complete after another request already merged a tail.
  const end = first.items.at(-1);
  if (end !== undefined && !byId.has(end.id)) {
    return { ...cached, pages: [{ ...first, items: first.items.map(item => byId.get(item.id) ?? item) }, ...cached.pages.slice(1)] };
  }
  const items = [...first.items.filter(message => !byId.has(message.id)), ...latest.items];
  // Bound unattended sessions without dropping a reader's cached history.
  if (items.length > 240) return null;
  return { ...cached, pages: [{ ...first, items }, ...cached.pages.slice(1)] };
};

/** Serialized domain data only, in memory; no persistent chat-content cache. */
export const freezeShareMessages = (messages: readonly RenderMessage[]): RenderMessage[] =>
  JSON.parse(JSON.stringify(messages)) as RenderMessage[];

import type { QueryClient } from "@tanstack/vue-query";
import { MESSAGE_MAX_QUERIES } from "./messageWindow";

const installed = new WeakSet<QueryClient>();
/** Keep only a small set of inactive conversations; active observers are protected. */
export const installMessageQueryBudget = (client: QueryClient): void => {
  if (installed.has(client)) return;
  installed.add(client);
  let pruning = false;
  const recency = new Map<string, number>();
  let sequence = 0;
  client.getQueryCache().subscribe(event => {
    const key = event.query.queryKey as readonly unknown[];
    if (!(key[0] === "messages" || (key[0] === "remote" && key[2] === "messages"))) return;
    if (event.type === "removed") { recency.delete(event.query.queryHash); return; }
    if (event.type === "observerAdded" || event.type === "updated") recency.set(event.query.queryHash, ++sequence);
    if (pruning || !(event.type === "updated" || event.type === "observerRemoved")) return;
    pruning = true;
    try {
      const queries = client.getQueryCache().findAll({ predicate: query => query.queryKey[0] === "messages"
        || (query.queryKey[0] === "remote" && query.queryKey[2] === "messages") });
      // A reopened conversation starts at latest, not at an evicted tail.
      for (const query of queries) {
        const data = query.state.data as { pages?: { window_has_newer?: boolean }[] } | undefined;
        if (!query.isActive() && query.getObserversCount() === 0 && data?.pages?.[0]?.window_has_newer === true) client.removeQueries({ queryKey: query.queryKey, exact: true });
      }
      const remaining = queries.filter(query => client.getQueryCache().get(query.queryHash) !== undefined);
      const inactive = remaining.filter(query => query.getObserversCount() === 0 && query.state.fetchStatus !== "fetching")
        .sort((a, b) => (recency.get(a.queryHash) ?? 0) - (recency.get(b.queryHash) ?? 0));
      let excess = remaining.length - MESSAGE_MAX_QUERIES;
      for (const query of inactive) if (excess-- > 0) client.removeQueries({ queryKey: query.queryKey, exact: true });
    } finally { pruning = false; }
  });
};

/** Refresh media metadata after local synchronization without dropping readable pages. */
import type { QueryClient } from "@tanstack/vue-query";
import type { AutoSyncStatus, Job } from "@/api/archiveApi";

type SyncReceipt = Pick<Job, "id" | "kind" | "status" | "source_account_id" | "updated_at">;
interface RefreshState {
  refreshed: Set<string>;
  seen: Map<string, Set<string>>;
  autoInitialized: boolean;
  autoKey: string | null;
}
const states = new WeakMap<QueryClient, RefreshState>();
const terminal = new Set<Job["status"]>(["completed", "failed", "cancelled", "interrupted"]);
const prefixes = ["messages", "search", "conversationSearch", "conversations", "conversation", "overview-daily-stats", "overview-recent-conversations", "local-refresh"] as const;

const stateOf = (client: QueryClient): RefreshState => {
  const state = states.get(client) ?? { refreshed: new Set<string>(), seen: new Map<string, Set<string>>(), autoInitialized: false, autoKey: null };
  states.set(client, state);
  return state;
};
const remember = (set: Set<string>, key: string): void => {
  set.add(key);
  while (set.size > 512) {
    const first = set.values().next();
    if (first.done) break;
    set.delete(first.value);
  }
};
const receiptKey = (job: SyncReceipt): string =>
  `${job.source_account_id}\0${job.id}\0${job.status}\0${String(job.updated_at)}`;
const invalidate = (client: QueryClient, accountId?: string): void => {
  for (const prefix of prefixes) {
    // Active readers revalidate in place; inactive pages become stale for the
    // next visit. No route change, empty page, or persisted message copy.
    const queryKey = accountId === undefined ? [prefix] : [prefix, accountId];
    // Infinite readers use the bounded latest window; never replay all history.
    const infinite = prefix === "messages" || prefix === "conversations";
    void client.invalidateQueries({ queryKey,
      ...(infinite ? { predicate: query => !(query.state.data && typeof query.state.data === "object" && "pages" in query.state.data) } : {}),
    }).catch(() => undefined);
    if (infinite) void client.invalidateQueries({ queryKey, refetchType: "none" }).catch(() => undefined);
  }
};

export const refreshMediaAfterSync = (client: QueryClient, job: SyncReceipt): void => {
  if ((job.kind !== "sync" && job.kind !== "import") || !terminal.has(job.status) || !job.source_account_id) return;
  const state = stateOf(client);
  const key = receiptKey(job);
  if (state.refreshed.has(key)) return;
  remember(state.refreshed, key);
  invalidate(client, job.source_account_id);
};

export const observeSyncMediaJobs = (
  client: QueryClient, accountId: string | null, jobs: readonly SyncReceipt[],
): void => {
  if (accountId === null || client.getQueryState(["jobs", accountId])?.status !== "success") return;
  const state = stateOf(client);
  const receipts = jobs.filter(job => (job.kind === "sync" || job.kind === "import") && job.source_account_id === accountId && terminal.has(job.status));
  const seen = state.seen.get(accountId);
  if (seen === undefined) {
    const baseline = new Set<string>();
    receipts.forEach(job => remember(baseline, receiptKey(job)));
    state.seen.set(accountId, baseline);
    while (state.seen.size > 32) {
      const first = state.seen.keys().next();
      if (first.done) break;
      state.seen.delete(first.value);
    }
    return;
  }
  let changed = false;
  for (const job of receipts) {
    const key = receiptKey(job);
    if (!seen.has(key) && !state.refreshed.has(key)) {
      remember(state.refreshed, key);
      changed = true;
    }
    remember(seen, key);
  }
  if (changed) invalidate(client, accountId);
};

export const observeAutoSyncMedia = (
  client: QueryClient,
  cycle: Pick<NonNullable<AutoSyncStatus["last_cycle"]>, "sync_job_id" | "finished_at"> | null,
): void => {
  const state = stateOf(client);
  const key = cycle?.sync_job_id == null ? null : `${cycle.sync_job_id}\0${String(cycle.finished_at)}`;
  if (!state.autoInitialized) {
    state.autoInitialized = true;
    state.autoKey = key;
    return;
  }
  if (state.autoKey === key) return;
  state.autoKey = key;
  // A cycle can cover several accounts. Only active local queries refetch;
  // remote read-model keys and inactive account contents stay untouched.
  if (key !== null) invalidate(client);
};

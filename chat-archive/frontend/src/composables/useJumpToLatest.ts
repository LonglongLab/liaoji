import { useQueryClient } from "@tanstack/vue-query";
import { nextTick, onBeforeUnmount, ref, watch } from "vue";

/** Rebase the in-memory cursor chain from one bounded, cursor-free request. */
export const useJumpToLatest = <Page>(options: {
  target: () => { queryKey: readonly unknown[]; fetch: (signal: AbortSignal) => Promise<Page> } | null;
  onLoaded: (signal: AbortSignal) => void | Promise<void>;
}) => {
  const client = useQueryClient();
  const loading = ref(false);
  const error = ref<string | null>(null);
  let controller: AbortController | null = null;
  const cancel = (): void => {
    controller?.abort();
    controller = null;
    loading.value = false;
    error.value = null;
  };
  watch(() => JSON.stringify(options.target()?.queryKey), cancel, { flush: "sync" });
  onBeforeUnmount(cancel);

  const jump = async (): Promise<void> => {
    if (loading.value) return;
    // Capture both the key and API parameters before the first await.
    const target = options.target();
    if (target === null) return;
    const active = new AbortController();
    controller = active;
    loading.value = true;
    error.value = null;
    try {
      await client.cancelQueries({ queryKey: target.queryKey, exact: true });
      active.signal.throwIfAborted();
      const page = await target.fetch(active.signal);
      active.signal.throwIfAborted();
      // A failed request leaves all old rows and cursor state intact. Never
      // refetch an infinite query here: that would revalidate every old page.
      client.setQueryData(target.queryKey, { pages: [page], pageParams: [null] });
      await nextTick();
      active.signal.throwIfAborted();
      await options.onLoaded(active.signal);
    } catch {
      if (!active.signal.aborted) error.value = "未能加载最新记录，请重试。";
    } finally {
      if (controller === active) {
        controller = null;
        loading.value = false;
      }
    }
  };
  return { loading, error, jump };
};

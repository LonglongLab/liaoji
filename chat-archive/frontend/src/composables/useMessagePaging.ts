import { computed, type Ref } from "vue";
import { ArchiveApiError } from "@/api/archiveApi";

export const useMessagePaging = (query: {
  isFetching: Ref<boolean>; isError: Ref<boolean>; error: Ref<Error | null>;
  fetchNextPage: () => Promise<unknown>; fetchPreviousPage: () => Promise<unknown>;
}) => {
  let direction: "older" | "newer" = "older";
  let busy = false;
  const run = async (next: typeof direction, retry = false): Promise<void> => {
    if (busy || query.isFetching.value || (query.isError.value && !retry)) return;
    direction = next;
    busy = true;
    try { await (next === "older" ? query.fetchNextPage() : query.fetchPreviousPage()); }
    finally { busy = false; }
  };
  const error = computed(() => {
    if (!query.isError.value) return null;
    if (query.error.value instanceof ArchiveApiError && query.error.value.code === "REMOTE_RECORD_MODEL_TOO_OLD") {
      return "当前远端版本不支持继续向后阅读，请更新远端服务，或跳至最新记录。";
    }
    return "这段记录暂时没能加载，已保留当前位置，请重试。";
  });
  return { error, older: () => run("older"), newer: () => run("newer"), retry: () => run(direction, true) };
};

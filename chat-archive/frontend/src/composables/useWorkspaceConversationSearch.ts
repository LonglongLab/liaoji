import { computed, ref, watch, type ComputedRef, type Ref } from "vue";
import { useRoute } from "vue-router";

import {
  ArchiveApiError,
  type MessageType,
  type SearchHit,
  type SearchPage,
} from "@/api/archiveApi";
import {
  CATEGORY_TYPES,
  parseSearchCategory,
  type SearchCategory,
} from "@/lib/conversationSearchTabs";
import { dateInputToUnixEnd, dateInputToUnixStart } from "@/utils/time";

import { queryString, type WorkspaceRouteLayer } from "./useWorkspaceRoute";

/**
 * 会话内查找那一层的编排，两端一份（PSU-2.87 · W2）。
 *
 * 两端挂的是**同一块面板**（`ConversationSearchPanel`），数据来自各自的搜索端点，
 * 条件也住在同一批查询参数里（`q` / `cat`，本机还有 `date`）。在此之前，
 * 「条件从链接里装回来」「改了条件把链接 replace 掉」「结果集怎么读」各写了两份。
 *
 * ## 端口
 *
 * - `dateEnabled`：**裁定 D5**（既有裁定，PSU-2.48）。远端没有「哪几天有消息」
 *   这个端点，一个点不亮任何日子的日历会被读成「这些天都没有消息」，所以那一端
 *   不给日期入口。这是能力差异，见 `docs/local-remote-boundary.md` §5。
 * - `pages` / `error`：这一端的查询在**这个组合式之后**才被创建（它要用这里的
 *   条件当查询键），所以交进来的是两个惰性读取器，不是值。它们不是差异，是接线口。
 * - `toPage`：把这一端那一页翻成端无关形状。远端的命中要过 `remoteHitToSearchHit`
 *   （附件可用性、头像、原图在不在），本机的已经是 `SearchHit`。数据形状层的差异，
 *   不是编排差异。
 * - `errorFallback`：说不出所以然时那句话要点名是哪一端的记录。
 */
export interface WorkspaceSearchPage {
  items: SearchHit[];
  /** 整个结果集有多少条；答不出来是 `null`，不是 0。 */
  total_count: number | null;
  index_status: SearchPage["index_status"];
  conversation_counts: Record<string, number>;
}

export interface WorkspaceConversationSearchOptions<TPage> {
  routeLayer: Ref<WorkspaceRouteLayer>;
  selectedId: Ref<string | null>;
  /** 改了条件之后把链接 replace 掉（W1）。 */
  replaceSearchRoute: () => void;
  /** 见 {@link WorkspaceConversationSearchOptions} 的端口说明——裁定 D5。 */
  dateEnabled: boolean;
  pages: () => readonly TPage[];
  error: () => unknown;
  toPage: (page: TPage) => WorkspaceSearchPage;
  errorFallback: string;
}

export interface WorkspaceConversationSearch {
  queryText: Ref<string>;
  category: Ref<SearchCategory | null>;
  /** 只有本机会填它（`dateEnabled`）；远端恒为空串，于是日期范围恒为 null。 */
  date: Ref<string>;
  /** 这一层现在要不要发查询。 */
  active: ComputedRef<boolean>;
  /** 分类换算成消息类型；没选分类就是「不筛类型」。 */
  categoryTypes: ComputedRef<MessageType[] | null>;
  dateFrom: ComputedRef<number | null>;
  dateTo: ComputedRef<number | null>;
  hits: ComputedRef<SearchHit[]>;
  totalCount: ComputedRef<number | null>;
  indexStatus: ComputedRef<SearchPage["index_status"]>;
  errorText: ComputedRef<string | null>;
  /** 写进链接的那几个条件（交给 W1 的 `searchLayerParams`）。 */
  routeParams: () => Record<string, string>;
  updateSearchQuery: (value: string) => void;
  updateSearchCategory: (value: SearchCategory | null) => void;
  /** 把条件清空。回程那一条不在这里清——它归 W1。 */
  reset: () => void;
}

export const useWorkspaceConversationSearch = <TPage>(
  options: WorkspaceConversationSearchOptions<TPage>,
): WorkspaceConversationSearch => {
  const route = useRoute();

  const queryText = ref("");
  const category = ref<SearchCategory | null>(null);
  const date = ref("");

  /**
   * 条件从链接里装回来。
   *
   * `context` 层也要装：读者是带着关键词从命中列表跳过来的，回程那一趟要按同一
   * 组条件把面板摆回去。层不在这两个之一时**不动**——那时面板不在场，清掉只会
   * 让回来的那一趟看见空条件。
   */
  watch(
    [
      options.routeLayer,
      () => route.query.q,
      () => route.query.cat,
      () => route.query.date,
    ],
    ([layer]) => {
      if (layer !== "search" && layer !== "context") return;
      queryText.value = queryString(route.query.q) ?? "";
      category.value = parseSearchCategory(queryString(route.query.cat));
      date.value = options.dateEnabled ? (queryString(route.query.date) ?? "") : "";
    },
    { immediate: true },
  );

  /**
   * 只有站在查找那一层上才发这次查询（裁定 D9，收敛到本机口径）。
   *
   * 结果只有那一层看得见。远端此前在 `context` 层也发一次，本意是给回程预热，
   * 可这一端的会话内搜索在真机上曾是**秒级**操作（本机 PSU-2.23 量到 9.9 秒），
   * 为一次引用跳转预付一整次检索，代价远大于收益；回来那一趟由查询缓存承接。
   */
  const active = computed(
    () => options.selectedId.value !== null && options.routeLayer.value === "search",
  );

  const categoryTypes = computed<MessageType[] | null>(() =>
    category.value === null ? null : CATEGORY_TYPES[category.value],
  );
  const dateFrom = computed(() => dateInputToUnixStart(date.value));
  const dateTo = computed(() => dateInputToUnixEnd(date.value));

  const pages = computed(() => options.pages().map(options.toPage));
  const hits = computed(() => pages.value.flatMap((page) => page.items));
  // 整个结果集有多少条，不是已加载的那几条——那正是侧栏那一行答应过的数字。
  const totalCount = computed(() => pages.value[0]?.total_count ?? null);
  const indexStatus = computed(() => pages.value[0]?.index_status ?? "ready");
  const errorText = computed(() => {
    const error = options.error();
    if (error === null || error === undefined) return null;
    // 只有契约错误的 message 是给读者看的话；别的（`fetch failed` 之类）说的是
    // 传输层自己的事，照抄出去等于把内部细节当解释（裁定 D10，收敛到本机口径）。
    return error instanceof ArchiveApiError ? error.message : options.errorFallback;
  });

  const routeParams = (): Record<string, string> => {
    const params: Record<string, string> = {};
    if (queryText.value !== "") params.q = queryText.value;
    if (category.value !== null) params.cat = category.value;
    if (options.dateEnabled && date.value !== "") params.date = date.value;
    return params;
  };

  const updateSearchQuery = (value: string): void => {
    queryText.value = value;
    options.replaceSearchRoute();
  };

  const updateSearchCategory = (value: SearchCategory | null): void => {
    category.value = value;
    options.replaceSearchRoute();
  };

  const reset = (): void => {
    category.value = null;
    queryText.value = "";
    date.value = "";
  };

  return {
    queryText,
    category,
    date,
    active,
    categoryTypes,
    dateFrom,
    dateTo,
    hits,
    totalCount,
    indexStatus,
    errorText,
    routeParams,
    updateSearchQuery,
    updateSearchCategory,
    reset,
  };
};

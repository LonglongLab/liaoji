import { computed, ref, watch, type ComputedRef, type Ref } from "vue";

import { ArchiveApiError, type SearchHit, type SearchPage } from "@/api/archiveApi";

import {
  rememberConversationNames,
  useConversationNameHydration,
  type HydratedConversationName,
} from "./useConversationNameHydration";
import { useDebouncedTrimmed } from "./useDebouncedValue";
import type { WorkspaceSearchPage } from "./useWorkspaceConversationSearch";

/**
 * 侧栏那一个输入框与它背后的检索，两端一份（PSU-2.87 · W3）。
 *
 * 同一个输入框既筛会话名也搜正文（PSU-2.45 起两端同版式），所以「防抖那一拍
 * 也算正在搜」「结果集怎么读」「命中落在列表翻不到的会话上时名字从哪来」
 * 三件事两端本来就该是同一份。此前它们各写了一份。
 *
 * ## 为什么分成两拍
 *
 * 这一片必须夹着调用方的两份查询：查询要拿**这里的**检索词当键，而取名 hydration
 * 一挂上就立刻读**那份查询**的命中（`immediate: true`）。所以：
 *
 * 1. `useWorkspaceSidebarFilter()` —— 输入框、防抖、端无关的检索参数；
 * 2. 调用方据此建列表查询与检索查询；
 * 3. `useWorkspaceSidebarSearch()` —— 派生、名字记忆与 hydration。
 *
 * 分的是**调用顺序**，不是实现：两拍里都只有一份实现。
 */

/** 端无关的侧栏检索参数。两端的 `SearchParams` / `RemoteSearchParams` 都收得下。 */
export interface WorkspaceSidebarSearchParams {
  q: string;
  conversationIds: string[];
  senderIds: string[];
  messageTypes: null;
  hasAttachment: null;
  dateFrom: null;
  dateTo: null;
}

export interface WorkspaceSidebarFilter {
  /** 输入框里的字面值。 */
  filterInput: Ref<string>;
  /** 防抖修剪之后、真的送出去的那个词。 */
  filterText: Ref<string>;
  /** 词空着就不检索——空词不是一次有效的正文检索，只是没输入。 */
  params: ComputedRef<WorkspaceSidebarSearchParams | null>;
  /**
   * 把词写回去（`""` = 清空）。
   *
   * **两个都要写**：只写 `filterInput` 会留一拍防抖，那一拍里查询键还是旧的。
   */
  write: (value: string) => void;
}

export const useWorkspaceSidebarFilter = (): WorkspaceSidebarFilter => {
  const filterInput = ref("");
  const filterText = useDebouncedTrimmed(filterInput);
  const params = computed<WorkspaceSidebarSearchParams | null>(() =>
    filterText.value === ""
      ? null
      : {
          q: filterText.value,
          conversationIds: [],
          senderIds: [],
          messageTypes: null,
          hasAttachment: null,
          dateFrom: null,
          dateTo: null,
        },
  );
  return {
    filterInput,
    filterText,
    params,
    write: (value) => {
      filterInput.value = value;
      filterText.value = value;
    },
  };
};

export interface WorkspaceSidebarSearchOptions<TPage> {
  filter: WorkspaceSidebarFilter;
  /** 这一端检索查询已加载的那几页（惰性读取器：查询在这个调用之前才建好）。 */
  pages: () => readonly TPage[];
  error: () => unknown;
  /** 把这一端那一页翻成端无关形状。远端的命中要过 `remoteHitToSearchHit`。 */
  toPage: (page: TPage) => WorkspaceSearchPage;
  /** 会话列表与检索这两份查询里，还有没有在取第一页的。 */
  queriesLoading: () => boolean;
  /** 已经翻到过的那几页里的会话名；答不出名字的位置给 `null`。 */
  listedNames: () => readonly (HydratedConversationName | null)[];
  errorFallback: string;
  /** 取名 hydration 的两个端口（PSU-2.85），原样透传。 */
  names: {
    scope: Ref<readonly unknown[] | null>;
    resolve: (
      conversationId: string,
      scope: readonly unknown[],
    ) => Promise<HydratedConversationName | null>;
  };
}

export interface WorkspaceSidebarSearch {
  /**
   * 跨过滤与翻页留下来的会话名（PSU-2.44）。
   *
   * 列表那份 infinite query 把过滤词编进了查询键，所以换个词就是另一份缓存——
   * 没有这张表，正在读的那条会话的标题会因为「这一份缓存里没有它」而退化成
   * 不透明标识，而它明明刚在上一份里出现过。只记**真的送到过的**名字。
   */
  conversationNames: Ref<Record<string, string>>;
  hits: ComputedRef<SearchHit[]>;
  /** 整个结果集有多少条；答不出来时退回本页条数，因为那正是能数出来的。 */
  totalCount: ComputedRef<number>;
  conversationCounts: ComputedRef<Record<string, number>>;
  indexStatus: ComputedRef<SearchPage["index_status"]>;
  errorText: ComputedRef<string | null>;
  loading: ComputedRef<boolean>;
  /** 换账号时调用：上一个账号的名字挂到这一个的会话上就是张冠李戴。 */
  resetNames: () => void;
}

export const useWorkspaceSidebarSearch = <TPage>(
  options: WorkspaceSidebarSearchOptions<TPage>,
): WorkspaceSidebarSearch => {
  const conversationNames = ref<Record<string, string>>({});

  const pages = computed(() => options.pages().map(options.toPage));
  const hits = computed(() => pages.value.flatMap((page) => page.items));
  const totalCount = computed(() => pages.value[0]?.total_count ?? hits.value.length);
  const conversationCounts = computed(() => pages.value[0]?.conversation_counts ?? {});
  // 回填还没追平时如实说：不说这句，读的人会把「搜不到」读成「没有这条消息」。
  const indexStatus = computed(() => pages.value[0]?.index_status ?? "ready");
  const errorText = computed(() => {
    const error = options.error();
    if (error === null || error === undefined) return null;
    // 只有契约错误的 message 是给读者看的话（裁定 D10）。
    return error instanceof ArchiveApiError ? error.message : options.errorFallback;
  });

  // 防抖那一拍也算「正在搜」：不算的话，输入框里已经有词而结果区还空着，
  // 看起来像「搜过了，没有」。
  const loading = computed(
    () =>
      options.filter.filterInput.value.trim() !== options.filter.filterText.value ||
      (options.filter.filterText.value !== "" && options.queriesLoading()),
  );

  watch(
    options.listedNames,
    (listed) => {
      conversationNames.value = rememberConversationNames(conversationNames.value, listed);
    },
    { immediate: true },
  );

  /**
   * 命中落在列表还没翻到的会话上时，把名字问回来（PSU-2.85，两端同一份实现）。
   *
   * 作用域给的是这一端会话详情 query 键的前缀，所以补名字与「点进去」共享同一份
   * 缓存，且同一条会话在命中列表重算多少次都只问一次。
   */
  useConversationNameHydration({
    names: conversationNames,
    hits,
    scope: options.names.scope,
    resolve: options.names.resolve,
  });

  return {
    conversationNames,
    hits,
    totalCount,
    conversationCounts,
    indexStatus,
    errorText,
    loading,
    resetNames: () => {
      conversationNames.value = {};
    },
  };
};

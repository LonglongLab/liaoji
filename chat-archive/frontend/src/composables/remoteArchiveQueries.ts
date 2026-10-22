import { useInfiniteQuery, useQuery, type QueryClient } from "@tanstack/vue-query";
import { computed, ref, watch, type ComputedRef, type Ref } from "vue";

import type {
  ArchiveApi,
  MessageType,
  RemoteAccountSummary,
  RemoteConversationMembersPage,
  RemoteConversationPage,
  RemoteConversationSummary,
  RemoteListOptions,
  RemoteMessagePage,
  RemoteSearchOptions,
  RemoteSearchPage,
} from "@/api/archiveApi";
import {
  MESSAGE_FRESH_MS,
  MESSAGE_MEMORY_RETENTION_MS,
} from "@/composables/conversationQueries";
import {
  type AccountIdentity,
  accountHeir,
  resolveOpeningAccount,
} from "@/lib/defaultAccount";
import { useRemoteArchiveUiStore } from "@/stores/remoteArchiveUi";

export const REMOTE_PAGE_SIZE = 100;
export const REMOTE_INITIAL_MESSAGE_PAGE_SIZE = 40;
export const REMOTE_SEARCH_LIMIT = 100;

const validatePageCursor = <T extends { has_more: boolean; next_cursor: string | null }>(
  page: T,
): T => {
  if (page.has_more && page.next_cursor === null) {
    throw new Error("远程分页响应缺少下一页游标，请重试或检查服务版本。");
  }
  return page;
};

export const useRemoteArchiveStatusQuery = (api: ArchiveApi) =>
  useQuery({
    queryKey: ["remote", "status"] as const,
    queryFn: () => api.getRemoteArchiveStatus(),
  });

/**
 * 远程归档的账号维度（记录模型 v3，PSU-2.35b）。
 *
 * 这一问同时是能力探测：旧远端答 QUERY_INVALID——那是确定性的「没有这个
 * 维度」，不重试；消费方把失败一律当「无账号可切」，界面退回归档标识，
 * 不出假入口。
 */
export const useRemoteAccountsQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  enabled: Ref<boolean>,
) =>
  useQuery({
    queryKey: computed(() => ["remote", archiveId.value, "accounts"] as const),
    enabled: computed(
      () => enabled.value && archiveId.value !== null && archiveId.value !== "",
    ),
    retry: false,
    queryFn: () => api.listRemoteArchiveAccounts(),
  });

export interface RemoteAccountScope {
  error?: ComputedRef<Error | null>;
  reload?: () => Promise<unknown>;
  /** 归档里的账号清单；旧远端 / 空归档 / 还没答，都是空数组。 */
  accounts: ComputedRef<readonly RemoteAccountSummary[]>;
  /**
   * 当前生效的账号作用域。null = 不过滤（旧远端的既有口径）。
   * 有账号可选时永远选中一个：默认第一个，用户切过就用切的那个——
   * 与本地档「必选一个账号」同一种心智。
   */
  selectedAccountId: ComputedRef<string | null>;
  /**
   * 链接自称的那个账号换算到今天的身份，认不得就是 null（PSU-2.90）。
   *
   * 与 `selectedAccountId` 分开报，是为了让调用方能区分「链接说了一个还在的
   * 账号」与「链接什么都没说、落到了默认账号」——只有前者该去改写 URL。
   */
  preferredAccountId: ComputedRef<string | null>;
  /**
   * 账号问答已有结果（成功或失败）。列表页等这一拍再发第一页，
   * 免得先拉一版合并视图、账号一到又整页重拉。
   */
  settled: ComputedRef<boolean>;
  /** 不归任何账号的行数（账号维度启用前同步的旧记录）。 */
  unattributedCount: ComputedRef<number>;
  /** 钉住的默认账号（PSU-2.119）；null = 没钉，打开时按「上次看过的」走。 */
  pinnedAccountId: ComputedRef<string | null>;
  /**
   * 钉过默认，而它在这份归档里既不在、也没有继承者。界面要把这件事说出来，
   * 不能一声不响地换一个号给人看。
   */
  pinnedAccountLost: ComputedRef<boolean>;
}

const UNPREFERRED: Ref<string | null> = ref(null);

export const useRemoteAccountScope = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  enabled: Ref<boolean>,
  /**
   * 链接自己说的那个账号（PSU-2.46）。**优先级高于 store 里的选择**：一条发过来
   * 的链接必须在收的人那边打开同一个账号的同一条会话，而不是在他的默认账号下
   * 解析歧义或落空。认不得就当没说——不认得的账号不该把人锁在一个空作用域里。
   */
  preferred: Ref<string | null> = UNPREFERRED,
): RemoteAccountScope => {
  const remoteUi = useRemoteArchiveUiStore();
  const accountsQuery = useRemoteAccountsQuery(api, archiveId, enabled);
  const accounts = computed<readonly RemoteAccountSummary[]>(
    () => accountsQuery.data.value?.items ?? [],
  );
  // 存下来的那两个选择按归档分键，所以归档标识一到就去认领（PSU-2.119）。
  // 这一句幂等：外壳与视图各挂一次这个 composable，认领的是同一份。
  watch(archiveId, (id) => remoteUi.adoptArchive(id), { immediate: true });
  const identities = computed<readonly AccountIdentity[]>(() =>
    accounts.value.map((item) => ({
      id: item.source_account_id,
      mergedIds: item.merged_source_account_ids,
    })),
  );
  /**
   * 这一次该落在谁身上、为什么——两端同一段判断（`lib/defaultAccount`）。
   *
   * 顺序：链接 → 这次切过的 → 钉住的默认 → 上次看过的 → 清单第一个。
   * 归并过的 id 一律先换成今天的身份（PSU-2.90）：链接如此，存下来的默认与
   * 「上次」同样是会过期的 id，认不出就把人丢给清单第一个等于丢进**另一个人
   * 的会话**。
   */
  const choice = computed(() =>
    resolveOpeningAccount({
      accounts: identities.value,
      preferred: preferred.value,
      session: remoteUi.selectedAccountId,
      pinned: remoteUi.defaultAccountId,
      last: remoteUi.lastAccountId,
    }),
  );
  /**
   * 钉住的号被并掉了：跟到继承者并把新 id 写回，免得每次打开都重算一遍。
   * 写回在这里而不在解析里——解析是纯函数，副作用留给它的调用方。
   *
   * **盯的是账号清单，不是 `choice`**：`preferred` 在会话页是一条computed 环的
   * 一段（`accountScope → preferredAccountId → probedAccountId → ownerQuery →
   * accountAmbiguous → accountScope`），只有全程惰性求值才成立。`watch` 会在
   * 建立时就把源跑一遍，盯 `choice` 等于在 setup 里提前解开那个环，得到的是
   * `Cannot access 'probedAccountId' before initialization`。清单是查询数据，
   * 不在环里；而需要自愈的时刻恰好就是清单到齐的那一刻。
   */
  watch(accounts, () => {
    const { heal } = choice.value;
    if (heal !== null) remoteUi.pinDefaultAccount(heal);
  });
  const preferredAccountId = computed(() =>
    accountHeir(identities.value, preferred.value),
  );
  return {
    accounts,
    error: computed(() => accountsQuery.error.value),
    reload: () => accountsQuery.refetch(),
    selectedAccountId: computed(() => choice.value.id),
    preferredAccountId,
    settled: computed(
      () => accountsQuery.isSuccess.value || accountsQuery.isError.value,
    ),
    unattributedCount: computed(
      () => accountsQuery.data.value?.unattributed_count ?? 0,
    ),
    pinnedAccountId: computed(() => remoteUi.defaultAccountId),
    pinnedAccountLost: computed(() => choice.value.pinnedLost),
  };
};

/**
 * 「不带过滤词」的那一份引用，模块级共用一个。
 *
 * 消费方不止会话页：搜索页也拿这个查询建会话名映射，它要的就是整份列表。
 * 每个调用方各自 `ref("")` 也能用（queryKey 里都是空串），但那会让「不过滤」
 * 看起来像一个各自持有的状态，而它是同一件事。
 */
const UNFILTERED: Ref<string> = ref("");

export const useRemoteConversationsQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  enabled: Ref<boolean>,
  sourceAccountId: Ref<string | null>,
  filterText: Ref<string> = UNFILTERED,
) =>
  useInfiniteQuery({
    // 账号与关键词都进查询键：任一变了就是另一个数据集，页与页不能混在同一份
    // 缓存里。与本机 `useConversationsQuery` 的键形状同一条道理。
    queryKey: computed(
      () =>
        [
          "remote",
          archiveId.value,
          "conversations",
          sourceAccountId.value,
          filterText.value,
        ] as const,
    ),
    enabled: computed(
      () => enabled.value && archiveId.value !== null && archiveId.value !== "",
    ),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }): Promise<RemoteConversationPage> => {
      const options: RemoteListOptions = { limit: REMOTE_PAGE_SIZE };
      if (pageParam !== null) options.cursor = pageParam;
      if (sourceAccountId.value !== null) {
        options.sourceAccountId = sourceAccountId.value;
      }
      if (filterText.value !== "") options.q = filterText.value;
      return validatePageCursor(await api.listRemoteConversations(options));
    },
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.next_cursor ?? undefined) : undefined,
  });

/**
 * 一条远程会话的身份，与本机 `useConversationDetailQuery` **同一种模式**。
 *
 * 会话名从前只有一个来源：列表那份 infinite query 的已加载页。而 `filterText`
 * 进了那份查询的键——一改过滤词就是另一份缓存，`find` 落空，正在读的那条会话
 * 的标题当场退化成不透明标识。本机三道保险（详情端点、记忆表、选中时清过滤词）
 * 这一端一道都没有；这是其中的第一道。
 *
 * **失败不重试**：远端到不了 v6 答 `QUERY_INVALID`，归档里没有这条会话答
 * `NOT_FOUND`，两个都是确定性的答案，重试变不出来。失败就往下退级取名
 * （记忆表 → 已加载页 → 诚实占位），界面不因此报错。
 */
export const useRemoteConversationDetailQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  conversationId: Ref<string | null>,
  enabled: Ref<boolean>,
  sourceAccountId: Ref<string | null>,
) =>
  useQuery({
    queryKey: computed(
      () =>
        [
          "remote",
          archiveId.value,
          "conversation",
          sourceAccountId.value,
          conversationId.value,
        ] as const,
    ),
    enabled: computed(
      () =>
        enabled.value &&
        archiveId.value !== null &&
        archiveId.value !== "" &&
        conversationId.value !== null &&
        conversationId.value !== "",
    ),
    retry: false,
    queryFn: async (): Promise<RemoteConversationSummary> => {
      const conversation = conversationId.value;
      if (conversation === null || conversation === "") {
        throw new Error("尚未选择可读取的远程会话");
      }
      const options: { sourceAccountId?: string } = {};
      if (sourceAccountId.value !== null) options.sourceAccountId = sourceAccountId.value;
      return api.getRemoteConversation(conversation, options);
    },
  });

/** 一页要多少个成员。名单在抽屉里一次铺开，所以比消息那一页大。 */
export const REMOTE_MEMBER_PAGE_SIZE = 200;

/**
 * 一条远程会话的群成员名单（记录模型 v8，PSU-2.47）。
 *
 * **`retry: false`**，与详情那一条同一个理由：远端到不了 v8 时答的是
 * `QUERY_INVALID`，那是一个确定性的答案而不是故障——重试三次只会把「这一端
 * 没有名单」这句话拖慢三倍。调用方据此把抽屉关掉并说明原因。
 */
export const useRemoteConversationMembersQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  conversationId: Ref<string | null>,
  enabled: Ref<boolean>,
  sourceAccountId: Ref<string | null>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () =>
        [
          "remote",
          archiveId.value,
          "members",
          sourceAccountId.value,
          conversationId.value,
        ] as const,
    ),
    enabled: computed(
      () =>
        enabled.value &&
        archiveId.value !== null &&
        archiveId.value !== "" &&
        conversationId.value !== null &&
        conversationId.value !== "",
    ),
    retry: false,
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }): Promise<RemoteConversationMembersPage> => {
      const conversation = conversationId.value;
      if (conversation === null || conversation === "") {
        throw new Error("尚未选择可读取的远程会话");
      }
      const options: RemoteListOptions = { limit: REMOTE_MEMBER_PAGE_SIZE };
      if (pageParam !== null) options.cursor = pageParam;
      if (sourceAccountId.value !== null) {
        options.sourceAccountId = sourceAccountId.value;
      }
      return validatePageCursor(
        await api.listRemoteConversationMembers(conversation, options),
      );
    },
    getNextPageParam: (lastPage) =>
      lastPage.has_more ? (lastPage.next_cursor ?? undefined) : undefined,
  });

export const useRemoteMessagesQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  conversationId: Ref<string | null>,
  enabled: Ref<boolean>,
  sourceAccountId: Ref<string | null>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () =>
        [
          "remote",
          archiveId.value,
          "messages",
          sourceAccountId.value,
          conversationId.value,
        ] as const,
    ),
    enabled: computed(
      () =>
        enabled.value &&
        archiveId.value !== null &&
        archiveId.value !== "" &&
        conversationId.value !== null &&
        conversationId.value !== "",
    ),
    initialPageParam: null as string | null,
    // `signal` 要**真的取出来**：vue-query 只在 queryFn 读过它之后才认为这次
    // 取数可以掐断。快速切会话时，上一条那次读取在公网上还要跑好几秒。
    queryFn: async ({ pageParam, signal }): Promise<RemoteMessagePage> =>
      fetchRemoteMessagePage(api, {
        conversationId: requireRemoteConversation(conversationId.value),
        sourceAccountId: sourceAccountId.value,
        cursor: pageParam,
        signal,
      }),
    getNextPageParam: remoteNextPageParam,
    gcTime: MESSAGE_MEMORY_RETENTION_MS,
    staleTime: MESSAGE_FRESH_MS,
  });

const requireRemoteConversation = (conversationId: string | null): string => {
  if (conversationId === null || conversationId === "") {
    throw new Error("尚未选择可读取的远程会话");
  }
  return conversationId;
};

const remoteNextPageParam = (lastPage: RemoteMessagePage): string | undefined =>
  lastPage.has_more ? (lastPage.next_cursor ?? undefined) : undefined;

/**
 * 一页远程消息。首屏 40、往回翻 100（与本机同一条规则），外加取消信号。
 *
 * 预取与「点开之后要的那一页」走的是**同一个函数和同一条键**：参数差一个字，
 * 缓存里就是两份，预取回来的那一页永远没人用得上。
 */
const fetchRemoteMessagePage = async (
  api: ArchiveApi,
  params: {
    conversationId: string;
    sourceAccountId: string | null;
    cursor: string | null;
    signal?: AbortSignal;
  },
): Promise<RemoteMessagePage> => {
  const options: RemoteListOptions = {
    limit:
      params.cursor === null ? REMOTE_INITIAL_MESSAGE_PAGE_SIZE : REMOTE_PAGE_SIZE,
  };
  if (params.cursor !== null) options.cursor = params.cursor;
  if (params.sourceAccountId !== null) {
    options.sourceAccountId = params.sourceAccountId;
  }
  if (params.signal !== undefined) options.signal = params.signal;
  return validatePageCursor(
    await api.listRemoteConversationMessages(params.conversationId, options),
  );
};

/** 远程消息页的查询键前缀；末尾接会话标识就是 `useRemoteMessagesQuery` 那一条。 */
export const remoteMessagesScope = (
  archiveId: string | null,
  sourceAccountId: string | null,
): readonly unknown[] => ["remote", archiveId, "messages", sourceAccountId];

/**
 * 把一条远程会话的最新一页先要回来，放进**点开时会读的同一条键**上。
 *
 * 预取失败不出声、也不重试：这一页本来就没人在等，报错只会把一条没人要的
 * 请求说成一次故障。真要打开时那条查询自己会再问一次，并照常显示错误。
 */
export const prefetchRemoteLatestMessages = async (
  queryClient: QueryClient,
  api: ArchiveApi,
  params: {
    archiveId: string;
    sourceAccountId: string | null;
    conversationId: string;
  },
): Promise<void> => {
  await queryClient.prefetchInfiniteQuery({
    queryKey: [
      ...remoteMessagesScope(params.archiveId, params.sourceAccountId),
      params.conversationId,
    ],
    initialPageParam: null as string | null,
    queryFn: ({ pageParam, signal }) =>
      fetchRemoteMessagePage(api, {
        conversationId: params.conversationId,
        sourceAccountId: params.sourceAccountId,
        cursor: pageParam,
        signal,
      }),
    getNextPageParam: remoteNextPageParam,
    gcTime: MESSAGE_MEMORY_RETENTION_MS,
    staleTime: MESSAGE_FRESH_MS,
    retry: false,
  });
};

/**
 * 一次远程搜索的一页，连同它真的花了多久。
 *
 * 计时刻意是**端到端**而不是服务端自报的处理时间：手机用户等的就是这一段，
 * 而慢在网络还是慢在索引，对「这一屏要等多久」来说是同一件事。
 */
export interface TimedRemoteSearch {
  page: RemoteSearchPage;
  /** 浏览器观察到的整次往返，毫秒。 */
  elapsedMs: number;
}

/** 把毫秒说成人话；亚秒级不四舍五入成「0.0 秒」那种假精确。 */
export const formatElapsed = (elapsedMs: number): string =>
  elapsedMs < 1000
    ? `${elapsedMs.toFixed(0)} 毫秒`
    : `${(elapsedMs / 1000).toFixed(1)} 秒`;

/**
 * 一次远程搜索的条件，与本机 `SearchParams` **逐个同名**（PSU-2.45）。
 *
 * `q` 可以是 null：只按日期或类型筛、无条件浏览当前账号均是有效分页。
 * 只有 `params = null` 表示条件尚未准备好。
 */
export interface RemoteSearchParams {
  q: string | null;
  /** 一个维度可以给多个值（PSU-2.74）：维度之间 AND、维度之内 OR。 */
  conversationIds: string[];
  senderIds: string[];
  messageTypes: MessageType[] | null;
  hasAttachment: boolean | null;
  dateFrom: number | null;
  dateTo: number | null;
}

export const useRemoteSearchQuery = (
  api: ArchiveApi,
  archiveId: Ref<string | null>,
  params: Ref<RemoteSearchParams | null>,
  enabled: Ref<boolean>,
  sourceAccountId: Ref<string | null>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () =>
        [
          "remote",
          archiveId.value,
          "search",
          sourceAccountId.value,
          params.value,
        ] as const,
    ),
    enabled: computed(
      () =>
        enabled.value &&
        archiveId.value !== null &&
        archiveId.value !== "" &&
        params.value !== null,
    ),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam, signal }): Promise<TimedRemoteSearch> => {
      const current = params.value;
      if (current === null) {
        throw new Error("尚未提交远程搜索条件");
      }
      const options: RemoteSearchOptions = { limit: REMOTE_SEARCH_LIMIT, signal };
      if (sourceAccountId.value !== null) {
        options.sourceAccountId = sourceAccountId.value;
      }
      if (current.conversationIds.length > 0) options.conversationIds = current.conversationIds;
      if (current.senderIds.length > 0) options.senderIds = current.senderIds;
      if (current.messageTypes !== null) options.messageTypes = current.messageTypes;
      if (current.hasAttachment !== null) options.hasAttachment = current.hasAttachment;
      if (current.dateFrom !== null) options.dateFrom = current.dateFrom;
      if (current.dateTo !== null) options.dateTo = current.dateTo;
      if (pageParam !== null) options.cursor = pageParam;
      // `performance.now()` 而不是 `Date.now()`：单调钟，中途对时不会把耗时
      // 变成负数。计时**只包住这一次请求**，不含渲染。
      const startedAt = performance.now();
      const page = await api.searchRemoteArchive(current.q, options);
      return { page, elapsedMs: performance.now() - startedAt };
    },
    getNextPageParam: (last) =>
      last.page.has_more ? (last.page.next_cursor ?? undefined) : undefined,
  });

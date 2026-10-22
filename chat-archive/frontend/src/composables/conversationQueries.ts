import { useInfiniteQuery, useQuery } from "@tanstack/vue-query";
import { computed, type Ref } from "vue";

import type {
  ArchiveApi,
  ConversationListOptions,
  ConversationDetail,
  ConversationMemberPage,
  ConversationPage,
  MessageListOptions,
  MessagePage,
} from "@/api/archiveApi";

// 转录分页的拼接口径两端共用，所以住在 `@/lib/transcript`。这里转出一次，
// 免得既有调用方为一次搬家改导入。
export { flattenMessagePages } from "@/lib/transcript";

export const MESSAGE_PAGE_SIZE = 100;
export const INITIAL_MESSAGE_PAGE_SIZE = 40;

/**
 * 读过的消息页在**这个页面的内存里**留多久。
 *
 * 只是内存：不落 localStorage、不落 IndexedDB，刷新一下它就不在了。聊天正文
 * 是这份产品里最敏感的东西，能拿它换的只有「这一次会话期间来回切不必重下」。
 *
 * 30 分钟对着的是真实用法：翻到一条旧消息、去别的会话对一下、再切回来——
 * 从前这一来一回是两次公网往返，手机上就是两段白屏。
 */
export const MESSAGE_MEMORY_RETENTION_MS = 30 * 60 * 1000;

/**
 * 留着的那一页多久之后要回服务端对一次。
 *
 * 不是 30 分钟：本机这一端导入还在往里写，新消息随时到；把「留着」直接读成
 * 「不用再问」，就等于对着刚到的消息说没有。一分钟够覆盖预取→点击、以及两条
 * 会话之间来回看这两种操作，而且重验是后台进行的——正文先画出来，不闪加载态。
 */
export const MESSAGE_FRESH_MS = 60_000;

export const useConversationsQuery = (
  api: ArchiveApi,
  accountId: Ref<string | null>,
  filterText: Ref<string>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () => ["conversations", accountId.value, filterText.value] as const,
    ),
    enabled: computed(() => accountId.value !== null),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }): Promise<ConversationPage> => {
      const account = accountId.value;
      if (account === null) {
        throw new Error("尚未选择账号");
      }
      const options: ConversationListOptions = {};
      if (filterText.value !== "") options.q = filterText.value;
      if (pageParam !== null) options.cursor = pageParam;
      return api.listConversations(account, options);
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  });

export const useConversationDetailQuery = (
  api: ArchiveApi,
  accountId: Ref<string | null>,
  conversationId: Ref<string | null>,
) =>
  useQuery({
    queryKey: computed(
      () => ["conversation", accountId.value, conversationId.value] as const,
    ),
    enabled: computed(() => accountId.value !== null && conversationId.value !== null),
    queryFn: async (): Promise<ConversationDetail> => {
      const account = accountId.value;
      const conversation = conversationId.value;
      if (account === null || conversation === null) {
        throw new Error("尚未选择会话");
      }
      return api.getConversation(conversation, account);
    },
  });

export const useConversationMembersQuery = (
  api: ArchiveApi,
  accountId: Ref<string | null>,
  conversationId: Ref<string | null>,
  enabled: Ref<boolean>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () => ["conversation-members", accountId.value, conversationId.value] as const,
    ),
    enabled: computed(
      () => enabled.value && accountId.value !== null && conversationId.value !== null,
    ),
    initialPageParam: null as string | null,
    queryFn: async ({ pageParam }): Promise<ConversationMemberPage> => {
      const account = accountId.value;
      const conversation = conversationId.value;
      if (account === null || conversation === null) {
        throw new Error("尚未选择群聊");
      }
      const options: MessageListOptions = { limit: 100 };
      if (pageParam !== null) options.cursor = pageParam;
      return api.listConversationMembers(conversation, account, options);
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor,
  });

export const useMessagesQuery = (
  api: ArchiveApi,
  accountId: Ref<string | null>,
  conversationId: Ref<string | null>,
) =>
  useInfiniteQuery({
    queryKey: computed(
      () => ["messages", accountId.value, conversationId.value] as const,
    ),
    enabled: computed(() => accountId.value !== null && conversationId.value !== null),
    initialPageParam: null as string | null,
    // `signal` 必须**真的取出来**：vue-query 只在 queryFn 读过它之后才认为
    // 这次取数可以掐断，否则切走只是把结果丢掉，字节照下。
    queryFn: async ({ pageParam, signal }): Promise<MessagePage> => {
      const account = accountId.value;
      const conversation = conversationId.value;
      if (account === null || conversation === null) {
        throw new Error("尚未选择会话");
      }
      return api.listConversationMessages(
        conversation,
        account,
        messagePageOptions(pageParam, signal),
      );
    },
    getNextPageParam: (lastPage) => lastPage.next_cursor,
    gcTime: MESSAGE_MEMORY_RETENTION_MS,
    staleTime: MESSAGE_FRESH_MS,
  });

/**
 * 一页消息的读取参数。首屏 40、往回翻 100（两端同一条规则），外加取消信号。
 * 预取走的是同一个函数，所以「预取的那一页」与「点开之后要的那一页」永远是
 * 同一个请求——不同的参数会在缓存里各存一份，预取就白做了。
 */
export const messagePageOptions = (
  cursor: string | null,
  signal?: AbortSignal,
): MessageListOptions => ({
  limit: cursor === null ? INITIAL_MESSAGE_PAGE_SIZE : MESSAGE_PAGE_SIZE,
  ...(cursor === null ? {} : { cursor }),
  ...(signal === undefined ? {} : { signal }),
});

/**
 * 日历上「这天有没有消息」的数据源。按可见月份取，翻月时换 queryKey 重新取，
 * 时区偏移跟着浏览器走，日界才和转录里的分隔线对得上。
 */
export const useConversationMessageDaysQuery = (
  api: ArchiveApi,
  accountId: Ref<string | null>,
  conversationId: Ref<string | null>,
  range: Ref<{ dateFrom: number; dateTo: number } | null>,
) =>
  useQuery({
    queryKey: computed(
      () =>
        [
          "conversation-message-days",
          accountId.value,
          conversationId.value,
          range.value?.dateFrom ?? null,
          range.value?.dateTo ?? null,
        ] as const,
    ),
    enabled: computed(
      () =>
        accountId.value !== null && conversationId.value !== null && range.value !== null,
    ),
    queryFn: async (): Promise<string[]> => {
      const account = accountId.value;
      const conversation = conversationId.value;
      const window = range.value;
      if (account === null || conversation === null || window === null) {
        throw new Error("尚未选择会话");
      }
      return api.getConversationMessageDays(conversation, account, {
        dateFrom: window.dateFrom,
        dateTo: window.dateTo,
        utcOffsetMinutes: -new Date(window.dateFrom * 1000).getTimezoneOffset(),
      });
    },
  });

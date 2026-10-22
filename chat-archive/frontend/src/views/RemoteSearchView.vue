<script setup lang="ts">
import { computed, provide, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";

import type { RemoteConversationSummary, SearchHit } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { workspaceKey } from "@/lib/workspaceContext";
import AppShell from "@/components/AppShell.vue";
import RemoteUnconfiguredCard from "@/components/RemoteUnconfiguredCard.vue";
import SearchDirectory from "@/components/SearchDirectory.vue";
import SearchFilterBar from "@/components/SearchFilterBar.vue";
import SearchFilterSheet from "@/components/SearchFilterSheet.vue";
import SearchResultPanel from "@/components/SearchResultPanel.vue";
import SearchScopeRail from "@/components/SearchScopeRail.vue";
import {
  formatElapsed,
  useRemoteAccountScope,
  useRemoteArchiveStatusQuery,
  useRemoteConversationsQuery,
  useRemoteSearchQuery,
  type RemoteSearchParams,
} from "@/composables/remoteArchiveQueries";
import {
  rememberConversationNames,
  useConversationNameHydration,
} from "@/composables/useConversationNameHydration";
import { useDebouncedTrimmed } from "@/composables/useDebouncedValue";
import { useHistoryLayer } from "@/composables/useHistoryLayer";
import { useRemoteAvatars } from "@/composables/useRemoteAvatars";
import { useSearchFilterControls } from "@/composables/useSearchFilterControls";
import { capabilityState } from "@/lib/capabilities";
import { conversationDirectoryEntry } from "@/lib/conversationDirectory";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import {
  onScreenConversationAvatars,
  remoteAttachmentLoader,
  remoteAttachmentWithThumbnailFallback,
  remoteThumbnailLoader,
  remoteConversationLabel,
  remoteConversationToRender,
  remoteHitToSearchHit,
} from "@/lib/remoteReadModel";
import {
  EMPTY_FILTERS,
  filterMessageTypes,
  type FilterNames,
  type SearchFilters,
} from "@/lib/searchFilters";
import {
  LOCAL_SEARCH_CAPABILITIES,
  REMOTE_SEARCH_CAPABILITIES,
  type DirectoryEntry,
} from "@/lib/searchScopes";
import { useCapabilityStore } from "@/stores/capabilities";
import { useRemoteArchiveUiStore } from "@/stores/remoteArchiveUi";

// 这一棵树也画的是远端的档案（PSU-2.99 批 C）：结果面板底下挂着 MediaViewer，
// 它页脚那一格的词按工作区分——不声明的话，远程端会说成本机端那句「仅缩略图」。
provide(workspaceKey, "remote");

const api = useArchiveApi();
const remoteUi = useRemoteArchiveUiStore();
const router = useRouter();
/**
 * 结果里点开的那张图也归路由管（PSU-2.120），与本机搜索页同一份实现。
 */
const mediaLayer = useHistoryLayer("media");

const statusQuery = useRemoteArchiveStatusQuery(api);
const statusLoading = computed(() => statusQuery.isLoading.value);
const configured = computed(() => statusQuery.data.value?.configured === true);
const archiveId = computed(() => statusQuery.data.value?.archive_id ?? null);

/**
 * 与本机全局搜索**挂同一套壳**（PSU-2.66）。差异全部落在这一个能力对象上，
 * 组件里没有「这是哪一端」的判断——那正是「两套」的种子。
 *
 * 远端关掉三样，每一样都不是「暂时没做」：排序要远端服务端支持并升记录模型；
 * 发送人名录远端没有那个端点；文件类别靠扩展名，而远端 `attachment_refs`
 * 只有 blob 标识，没有名字。
 */
const capabilities = REMOTE_SEARCH_CAPABILITIES;

const filters = ref<SearchFilters>(EMPTY_FILTERS);
const queryInput = ref("");
const query = useDebouncedTrimmed(queryInput);
const directoryQuery = ref("");
const capNoteOpen = ref(false);
const capToggle = ref<HTMLButtonElement | null>(null);
/** 收起之后焦点得有着落——那段说明里的按钮刚刚消失，交还给开它的 ⓘ。 */
const closeCapNote = (): void => {
  capNoteOpen.value = false;
  capToggle.value?.focus();
};

// 从一条命中后退回来时把条件原样装回去（PSU-2.46）。
const pendingReturn = remoteUi.consumeSearchReturn();
if (pendingReturn !== null) {
  queryInput.value = pendingReturn.q;
  filters.value = pendingReturn.filters;
}

const capabilityStore = useCapabilityStore();
const attachmentsAvailable = computed(
  () =>
    capabilityState(capabilityStore.deployment, capabilityStore.states, "attachment-view") ===
    "available",
);
const loadOriginal = remoteAttachmentLoader((blobId) => api.getRemoteAttachmentUrl(blobId));

/**
 * 原图在不在（PSU-2.43），与会话页同一套：只存明确的答案，「问不出来」和
 * 「还没问过」在界面上都是未知，所以在这里也都是「表里没有这个键」。
 */
const originalPresence = reactive<Record<string, boolean>>({});
const originalAvailable = (blobId: string): boolean | undefined => originalPresence[blobId];
const loadThumbnail = remoteThumbnailLoader(
  (blobId) => api.getRemoteAttachmentThumbnailUrl(blobId),
  (blobId, available) => {
    if (available !== null) originalPresence[blobId] = available;
  },
);
const loadAttachment = remoteAttachmentWithThumbnailFallback(
  loadOriginal,
  loadThumbnail,
  originalAvailable,
);

// 账号作用域与会话页同一份（PSU-2.35b）：搜索只在当前选中账号的记录里进行。
const accountScope = useRemoteAccountScope(api, archiveId, configured);
const scopeEnabled = computed(() => configured.value && accountScope.settled.value);

// 两份会话查询，因为它们回答的是两个问题：名录要按输入框筛，而结果行上的
// 会话名**不能**跟着筛掉——否则在名录里搜一个字，其余行的名字就集体退化成
// 不透明标识。未筛时两者 queryKey 相同，只会真的取一次。
// 过滤走 `useRemoteConversationsQuery` 自带的服务端 `filterText`，不在已加载页
// 上做客户端 `.filter()`：那样搜一个还没翻到的名字会答「没有匹配」，而它存在。
const noFilter = ref("");
const namesQuery = useRemoteConversationsQuery(
  api,
  archiveId,
  scopeEnabled,
  accountScope.selectedAccountId,
  noFilter,
);
const conversationsQuery = useRemoteConversationsQuery(
  api,
  archiveId,
  scopeEnabled,
  accountScope.selectedAccountId,
  directoryQuery,
);
const conversations = computed(
  () => conversationsQuery.data.value?.pages.flatMap((page) => page.items) ?? [],
);
/**
 * 名字还没送到、或会话还没被翻到的，退回不透明标识——不编名字。
 *
 * 载体是 Record 而不是 computed 出来的 Map——`useConversationNameHydration`
 * 要读它判断「哪些还缺名」，也要往里写问回来的名字（PSU-2.85，四处同一份实现）。
 */
const conversationNames = ref<Record<string, string>>({});
watch(
  [() => namesQuery.data.value, conversations],
  () => {
    const listed = [
      ...(namesQuery.data.value?.pages ?? []).flatMap((page) => page.items),
      ...conversations.value,
    ].map((item: RemoteConversationSummary) => ({
      id: item.conversation_id,
      name: remoteConversationLabel(item),
    }));
    conversationNames.value = rememberConversationNames(conversationNames.value, listed);
  },
  { immediate: true },
);
const conversationNameMap = computed<ReadonlyMap<string, string>>(
  () => new Map(Object.entries(conversationNames.value)),
);
/**
 * 与本机搜索、导出共用名录条目转换；远程领域模型和头像地址仍由既有适配器处理。
 */
const directoryEntries = computed<DirectoryEntry[]>(() =>
  conversations.value.map((item) =>
    conversationDirectoryEntry(remoteConversationToRender(item, {}, avatarSrc)),
  ),
);

const names = computed<FilterNames>(() => ({
  conversations: conversationNameMap.value,
  // 远端没有发送人名录（`REMOTE_SEARCH_CAPABILITIES.senderScope` 为 false），
  // 所以这一半永远是空的——不是「还没取到」。
  senders: new Map<string, string>(),
}));

// 远端没有名录切换，所以不传 onDimensionShown——不是漏了。
const { toggleFilter, removeFilter, title, sheetOpen, sheetSegment, openSheet, closeSheet } =
  useSearchFilterControls({
    filters,
    capabilities,
    names,
    sheetLayer: useHistoryLayer("filters"),
  });

const params = computed<RemoteSearchParams>(() => ({
  q: query.value === "" ? null : query.value,
  conversationIds: [...filters.value.conversationIds],
  senderIds: [...filters.value.senderIds],
  messageTypes: filterMessageTypes(filters.value),
  hasAttachment: null,
  dateFrom: null,
  dateTo: null,
}));

const searchQuery = useRemoteSearchQuery(
  api,
  archiveId,
  params,
  scopeEnabled,
  accountScope.selectedAccountId,
);
const pages = computed(() => searchQuery.data.value?.pages ?? []);
/**
 * 命中卡上的发送者头像（PSU-2.47；PSU-2.122 改用全应用共用的那一份 store）。
 *
 * 与会话页共用同一份缓存：底部 Tab 在两页之间来回切，此前每切一次就把整份
 * 头像字节扔掉重下一遍，而两页要的往往是同一批人。
 */
const avatars = useRemoteAvatars(
  sharedAvatarStoreVia((blobId) => api.getRemoteAttachmentUrl(blobId)),
);
const avatarSrc = (blobId: string | null | undefined): string | null =>
  attachmentsAvailable.value ? avatars.srcFor(blobId) : null;

const rawHits = computed(() => pages.value.flatMap((entry) => entry.page.items));

/**
 * 名录里现在看得见的那几行（PSU-2.128），与会话页同一套视口门。
 *
 * 名录有两份实例：桌面的中列（手机上是 CSS 藏起来的，藏起来的行不会被报成可见）
 * 与 H5 抽屉里那一份（关上就卸载，卸载时报空集）。两份取并集；任何一份答 `null`
 * （这一端没有视口门）就按全部算。
 */
// 缺省是空集而不是 null：名录挂上来之前一张都不取，挂上来之后由它自己说
// 「看得见哪几行」或「这一端没有视口门」。
const visibleInColumn = ref<readonly string[] | null>([]);
const visibleInSheet = ref<readonly string[] | null>([]);
const visibleDirectoryIds = computed<readonly string[] | null>(() => {
  const column = visibleInColumn.value;
  const sheet = visibleInSheet.value;
  return column === null || sheet === null ? null : [...column, ...sheet];
});

watch(
  [rawHits, conversations, visibleDirectoryIds, () => attachmentsAvailable.value],
  () => {
    if (!attachmentsAvailable.value) {
      // 不是 `reset()`：store 是全应用共用的，清它会把别的页面的图一起扔掉。
      avatars.retain([]);
      return;
    }
    avatars.retain([
      ...rawHits.value.map((item) => item.sender_avatar_blob_id),
      ...onScreenConversationAvatars(conversations.value, visibleDirectoryIds.value),
    ]);
  },
  { immediate: true },
);
const hits = computed<SearchHit[]>(() =>
  rawHits.value.map((item) =>
    remoteHitToSearchHit(item, {
      attachmentsAvailable: attachmentsAvailable.value,
      avatarSrc,
      originalAvailable,
    }),
  ),
);
const totalCount = computed(() => pages.value[0]?.page.total_count ?? null);
// 归档和账号发现尚未完成时，查询被就绪守卫禁用；禁用不等于查询返回了空页。
const searchLoading = computed(() => !scopeEnabled.value || searchQuery.isLoading.value);
// 回填还没追平时如实说，而不是假装完整。
const indexStatus = computed(() => pages.value[0]?.page.index_status ?? "ready");

// 把耗时摆出来是「不加速，但也不假装快」。上一次的数字在新一次搜索仍在跑时
// 不显示——那会把旧耗时读成新耗时。
const elapsedText = computed(() =>
  searchQuery.isLoading.value || pages.value.length === 0
    ? null
    : formatElapsed(pages.value[0]?.elapsedMs ?? 0),
);
const errorText = computed(() =>
  searchQuery.error.value === null
    ? null
    : searchQuery.error.value instanceof Error && searchQuery.error.value.message !== ""
      ? searchQuery.error.value.message
      : "远程搜索暂时不可用。",
);

/**
 * 命中落在还没翻到的会话上时，**逐条问那几条**，而不是把会话列表一页页往下翻
 * （PSU-2.85，与本机同一份实现）。
 *
 * 这一端的代价最刺眼：真机 614 个会话要翻满七页，每页都是一次跨境往返；
 * 而命中落在的会话通常不到十个。`namesQuery` 的第一页保留——筛选 chip 的文案要它。
 */
useConversationNameHydration({
  names: conversationNames,
  hits,
  scope: computed(() => {
    const archive = archiveId.value;
    if (archive === null || archive === "") return null;
    return ["remote", archive, "conversation", accountScope.selectedAccountId.value];
  }),
  resolve: async (conversationId, scope) => {
    const account = scope[3];
    const detail = await api.getRemoteConversation(
      conversationId,
      typeof account === "string" ? { sourceAccountId: account } : {},
    );
    return detail.display_name === null
      ? null
      : { id: detail.conversation_id, name: detail.display_name };
  },
});

// 账号换了就把范围与结果一起清掉，与本机同一条：留着上一个账号的会话选择，
// 会在新账号下筛出一个空结果，看起来像「这个账号没有记录」。
watch(accountScope.selectedAccountId, () => {
  // 头像**不在这里清**（PSU-2.122）：`blob_id` 是账号加盐算出来的，同一个 id
  // 的字节永远是同一份；而 store 是全应用共用的，在这里清会连会话页正在显示
  // 的图一起扔掉。下面那条 retain 会把不再需要的标识退订。
  // 两个账号的会话标识可以撞车，留着上一个账号的名字会挂错人。
  conversationNames.value = {};
  queryInput.value = "";
  directoryQuery.value = "";
  filters.value = EMPTY_FILTERS;
});

const openHit = (hit: SearchHit): void => {
  if (hit.conversation_id === "") return;
  remoteUi.rememberSearchReturn({ q: queryInput.value, filters: filters.value });
  // 命中整条进 URL（PSU-2.46）：这样这一跳可以后退回来，链接也发得出去。
  // `account` 一起带上——远端会话的主键是三元组。
  const query: Record<string, string> = {
    conversation: hit.conversation_id,
    layer: "context",
    message: hit.message_id,
  };
  const account = accountScope.selectedAccountId.value;
  if (account !== null) query.account = account;
  void router.push({ name: "remote-conversations", query });
};
</script>

<template>
  <AppShell
    variant="app"
    mode="remote"
    :account-name="archiveId ?? undefined"
  >
    <section
      v-if="statusLoading"
      class="remote-unconfigured"
      aria-live="polite"
    >
      <div class="remote-unconfigured-card">
        <p
          class="remote-hint"
          role="status"
        >
          正在确认远程档案状态……
        </p>
      </div>
    </section>

    <section
      v-else-if="!configured"
      class="remote-unconfigured"
      aria-live="polite"
    >
      <RemoteUnconfiguredCard
        heading="远程搜索"
        summary="此设备尚未配置远程档案。完成一次性配对后，即可搜索远程档案中的文本记录。"
      >
        <p class="action-note">
          远程搜索只读取记录文本，不会下载完整数据库或创建离线副本。
        </p>
      </RemoteUnconfiguredCard>
    </section>

    <section
      v-else
      class="search-page"
    >
      <SearchScopeRail
        class="search-page-rail"
        :filters="filters"
        :capabilities="capabilities"
        @toggle="toggleFilter"
      />

      <div class="search-page-directory">
        <SearchDirectory
          title="会话名录"
          placeholder="筛选会话名称"
          :query="directoryQuery"
          :entries="directoryEntries"
          :selected-ids="[...filters.conversationIds]"
          :loading="conversationsQuery.isLoading.value"
          :fetching-more="conversationsQuery.isFetchingNextPage.value"
          :has-more="conversationsQuery.hasNextPage.value"
          @toggle="toggleFilter('conversationIds', $event)"
          @update:query="directoryQuery = $event"
          @load-more="conversationsQuery.fetchNextPage()"
          @visible-change="visibleInColumn = $event"
        />
      </div>

      <SearchResultPanel
        class="search-page-results"
        :title="title"
        :hits="hits"
        :conversation-names="conversationNameMap"
        :query="query"
        :total-count="totalCount"
        sort="newest"
        :sort-modes="capabilities.sortModes"
        :loading="searchLoading"
        :fetching="searchQuery.isFetching.value"
        :fetching-more="searchQuery.isFetchingNextPage.value"
        :has-more="searchQuery.hasNextPage.value"
        :error-text="errorText"
        :index-status="indexStatus"
        :load-attachment="loadAttachment"
        :load-thumbnail="loadThumbnail"
        :elapsed-text="elapsedText"
        :active-media-id="mediaLayer.value"
        @open-media-layer="mediaLayer.open"
        @close-media-layer="mediaLayer.close"
        @update:query="queryInput = $event"
        @open="openHit"
        @load-more="searchQuery.fetchNextPage()"
        @retry="searchQuery.refetch()"
      >
        <template #filters>
          <SearchFilterBar
            :filters="filters"
            :names="names"
            :capabilities="capabilities"
            @open="openSheet"
            @remove="removeFilter"
            @clear="filters = EMPTY_FILTERS"
          />
        </template>
        <template #title-action>
          <button
            ref="capToggle"
            class="remote-search-info"
            type="button"
            aria-label="远程搜索能匹配到什么"
            aria-controls="remote-search-cap"
            :aria-expanded="capNoteOpen"
            @click="capNoteOpen = !capNoteOpen"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2"
              stroke-linecap="round"
              aria-hidden="true"
            >
              <circle
                cx="12"
                cy="12"
                r="9"
              />
              <path d="M12 11.5v4.5" />
              <path d="M12 8h.01" />
            </svg>
          </button>
        </template>
        <template #note>
          <!--
            一段话里塞六件事，手机上就是满屏一堵墙。改成一行一条：做不到的事在
            前，为什么在后——**一条都不删**，能力差异是如实说出来的，不是藏起来的。
          -->
          <section
            v-show="capNoteOpen"
            id="remote-search-cap"
            class="remote-search-cap"
            role="note"
          >
            <header class="remote-search-cap-head">
              <h2>远程搜索能匹配到什么</h2>
              <button
                class="text-action"
                type="button"
                @click="closeCapNote"
              >
                收起
              </button>
            </header>
            <p class="remote-search-cap-lead">
              与本机走同一套索引：分页、总数，会话与类型过滤都在。下面几样这一端答不出，界面上因此也不给。
            </p>
            <ul class="remote-search-cap-list">
              <li>
                <span class="remote-search-cap-what">不能按最旧时间排</span>
                <span class="remote-search-cap-why">要远端服务端支持，并升一次记录模型</span>
              </li>
              <li>
                <span class="remote-search-cap-what">没有发送人名录</span>
                <span class="remote-search-cap-why">远端没有那个端点</span>
              </li>
              <li>
                <span class="remote-search-cap-what">文件不分 文稿／表格，也搜不到文件名</span>
                <span class="remote-search-cap-why">远端只送 blob 标识，归档里没有文件名</span>
              </li>
              <li>
                <span class="remote-search-cap-what">
                  会话最多同时选 {{ capabilities.maxSelections }} 个
                </span>
                <span class="remote-search-cap-why">
                  本机能选 {{ LOCAL_SEARCH_CAPABILITIES.maxSelections }} 个；这一端的多选是按值扇出成几次跨境请求，每多一个值就多一次往返
                </span>
              </li>
              <li>
                <span class="remote-search-cap-what">会话名录只按子串匹配</span>
                <span class="remote-search-cap-why">
                  本机搜「示软」也能找到「示例软件」，这一端不行——那要改远端两套实现
                </span>
              </li>
            </ul>
            <p
              v-if="accountScope.selectedAccountId.value !== null"
              class="remote-search-cap-foot"
            >
              搜索只在左上角当前选中账号的记录里进行。
            </p>
          </section>
        </template>
      </SearchResultPanel>

      <SearchFilterSheet
        :open="sheetOpen"
        :filters="filters"
        :capabilities="capabilities"
        :segment="sheetSegment"
        :entries="directoryEntries"
        :query="directoryQuery"
        :loading="conversationsQuery.isLoading.value"
        :fetching-more="conversationsQuery.isFetchingNextPage.value"
        :has-more="conversationsQuery.hasNextPage.value"
        @update:segment="sheetSegment = $event"
        @update:query="directoryQuery = $event"
        @load-more="conversationsQuery.fetchNextPage()"
        @visible-change="visibleInSheet = $event"
        @apply="filters = $event"
        @close="closeSheet"
      />
    </section>
  </AppShell>
</template>

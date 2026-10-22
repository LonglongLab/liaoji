<script setup lang="ts">
import { useQueryClient } from "@tanstack/vue-query";
import { computed, provide, reactive, ref, watch, type Ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import { workspaceKey } from "@/lib/workspaceContext";
import AppShell from "@/components/AppShell.vue";
import ConversationInfoDrawer from "@/components/ConversationInfoDrawer.vue";
import ConversationList from "@/components/ConversationList.vue";
import MergedForwardViewer from "@/components/MergedForwardViewer.vue";
import ConversationSearchPanel from "@/components/ConversationSearchPanel.vue";
import MessageTranscript from "@/components/MessageTranscript.vue";
import RemoteUnconfiguredCard from "@/components/RemoteUnconfiguredCard.vue";
import {
  prefetchRemoteLatestMessages,
  useRemoteAccountScope,
  useRemoteArchiveStatusQuery,
  useRemoteConversationDetailQuery,
  useRemoteConversationMembersQuery,
  useRemoteConversationsQuery,
  useRemoteMessagesQuery,
  useRemoteSearchQuery,
  type RemoteSearchParams,
} from "@/composables/remoteArchiveQueries";
import {
  PREFETCH_VISIBLE_LIMIT,
  useConversationMessagePrefetch,
} from "@/composables/useConversationMessagePrefetch";
import { useRemoteAvatars } from "@/composables/useRemoteAvatars";
import { useWorkspaceConversationSearch } from "@/composables/useWorkspaceConversationSearch";
import { queryString, useWorkspaceRoute } from "@/composables/useWorkspaceRoute";
import {
  useWorkspaceSidebarFilter,
  useWorkspaceSidebarSearch,
} from "@/composables/useWorkspaceSidebarSearch";
import { useWorkspaceTranscript } from "@/composables/useWorkspaceTranscript";
import { capabilityState } from "@/lib/capabilities";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import {
  onScreenConversationAvatars,
  remoteAttachmentLoader,
  remoteConversationPlaceholder,
  remoteConversationToRender,
  remoteHitToSearchHit,
  remoteMessageToRender,
  remoteThumbnailLoader,
  remoteAttachmentWithThumbnailFallback,
  remoteShareLoaders,
  unattributedRemoteCount,
} from "@/lib/remoteReadModel";
import {
  flattenMessagePages,
  type RenderConversation,
  type RenderConversationDetail,
  type RenderMember,
  type RenderMessage,
} from "@/lib/transcript";
import { useCapabilityStore } from "@/stores/capabilities";
import { useRemoteArchiveUiStore } from "@/stores/remoteArchiveUi";

// 这一棵树画的是远端的档案（PSU-2.99 批 C）。共用组件里少数几个词要按这个
// 分叉——「仅缩略图」在这一端读作「未上传」，因为原图只是还没同步上来。
// 由视图 provide 而不是 AppShell：视图自己的测试常把 AppShell 桩掉。
provide(workspaceKey, "remote");

const api = useArchiveApi();
const remoteUi = useRemoteArchiveUiStore();
const route = useRoute();
const router = useRouter();

const statusQuery = useRemoteArchiveStatusQuery(api);
const statusLoading = computed(() => statusQuery.isLoading.value);
const configured = computed(() => statusQuery.data.value?.configured === true);
const archiveId = computed(() => statusQuery.data.value?.archive_id ?? null);

/**
 * 远程工作区的状态住在 URL 里（PSU-2.46）。
 *
 * 到上一版为止它全在 pinia：刷新一下丢掉所在会话，浏览器后退直接退出工作区，
 * 链接分享不了。**远程端是「只有浏览器」的那一端**，这三件事在这里比在本机
 * 更该成立。键与本机同名（`conversation` / `layer` / `message`），这样两个视图
 * 共享同一套心智与同一批工具函数。
 *
 * 多一个 `account`：远端会话的主键是 `(archive, source_account, conversation)`
 * （2.35a 为 `filehelper` 跨账号撞号改的），只带 conversation 的链接在对方的默认
 * 账号下会解析歧义或落空。本机没这个问题，因为本机 `c.id` 全库唯一。
 */
/**
 * 路由与导航住在 `useWorkspaceRoute`（PSU-2.87 · W1），与本机工作台**同一份**。
 *
 * 端口交的是这一端不同的那件事：每条链接都要钉 `account`（会话主键是三元组），
 * 以及镜像进哪个 store（裁定 D1）。跨页回程那一条这一端不传——远程搜索页走它
 * 自己的表单还原（`RemoteSearchFormState`）。
 */
const workspace = useWorkspaceRoute({
  routeName: "remote-conversations",
  conversationExtras: () => {
    const account = accountScope.selectedAccountId.value;
    return account === null ? {} : { account };
  },
  // 这一端没有日期入口（裁定 D5，由 W2 的 `dateEnabled` 说了算）。
  searchLayerParams: () => search.routeParams(),
  restoreRouteState: (conversationId, layer) => {
    remoteUi.restoreRouteState(conversationId, layer);
  },
  // 离开转录去别的层之前记下阅读位置——同一个转录组件，方法就在那里，此前
 // 这一端没人调（裁定 D3，收敛到本机口径）。
  beforeLeaveTranscript: () => {
    transcript.transcriptRef.value?.captureReadingPosition();
  },
  sidebarFilter: {
    read: () => filterText.value,
    write: (value) => {
      sidebarFilter.write(value);
    },
  },
});
const {
  selectedId,
  routeLayer,
  searchReturnAvailable,
  conversationQuery,
  selectConversation,
  openSidebarSearchGroup,
  openConversationLayer,
  openSearchHit,
  openReplyContext,
  openMergedForward,
  closeConversationLayer,
  backToConversationList,
  returnToSearch,
  replaceSearchRoute,
  mediaLayer,
  fileLayer,
  zoomLayer,
  overlayFor,
} = workspace;

/**
 * 三块版式各自认领的那几格浮层（PSU-2.120）。
 *
 * 灯箱、文件预览、放大阅读都**归路由管**：开一层压一条历史，手机上的系统「返回」
 * 才关得掉它。此前这一端一处都没接，于是那一下返回弹掉的是「会话」那一条——
 * 点开一张图再返回，人落在会话列表上。
 */
const transcriptOverlay = overlayFor("transcript");
const searchOverlay = overlayFor("search");
const recordOverlay = overlayFor("record");

/**
 * 会话内查找那一层（PSU-2.87 · W2），与本机工作台**同一份**。
 *
 * `pages` / `error` 交的是惰性读取器：那份查询要拿这里的条件当键，所以它在
 * 这个调用之后才被创建。`toPage` 把远端那一页翻成端无关形状——命中要过
 * `remoteHitToSearchHit`（附件可用性、头像、原图在不在），那是数据形状层的
 * 差异，不是编排差异。
 */
const search = useWorkspaceConversationSearch({
  routeLayer,
  selectedId,
  replaceSearchRoute,
  // 远端没有「哪几天有消息」这个端点，一个点不亮任何日子的日历会被读成
  // 「这些天都没有消息」（裁定 D5）。分类过滤照给，v7 的远端本来就接。
  dateEnabled: false,
  pages: () => conversationSearch.data.value?.pages ?? [],
  error: () => conversationSearch.error.value,
  toPage: (entry) => ({
    items: entry.page.items.map((item) =>
      remoteHitToSearchHit(item, {
        attachmentsAvailable: attachmentsAvailable.value,
        avatarSrc,
        originalAvailable,
      }),
    ),
    total_count: entry.page.total_count,
    index_status: entry.page.index_status,
    conversation_counts: entry.page.conversation_counts,
  }),
  errorFallback: "暂时无法查找这条会话。",
});
const {
  queryText: searchQueryText,
  category: searchCategory,
  hits: conversationSearchHits,
  totalCount: conversationSearchTotalCount,
  indexStatus: conversationSearchIndexStatus,
  errorText: conversationSearchErrorText,
  updateSearchQuery,
  updateSearchCategory,
} = search;

const highlightedRecordKey = computed(() =>
  routeLayer.value === "record" ? null : workspace.routeMessageId.value,
);
/** 会话信息抽屉（`layer=info`，PSU-2.47）。 */
const infoOpen = computed(() => routeLayer.value === "info");
const routeAccountId = computed(() => queryString(route.query.account));

const errorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message !== "" ? error.message : fallback;

// 账号作用域（PSU-2.35b）：v3 远端按账号读，旧远端回落到不过滤的既有口径。
// 列表等账号问答落定再发第一页——先拉合并视图、账号一到又整页重拉，
// 在一条按秒计的链路上是白付两倍等待。
//
// 链接里那个 `account` 优先（PSU-2.46）；它缺席或认不得时，`probedAccountId`
// 会去问「这条会话归谁」。
const preferredAccountId = computed(() => routeAccountId.value ?? probedAccountId.value);
const accountScope = useRemoteAccountScope(api, archiveId, configured, preferredAccountId);
const listEnabled = computed(() => configured.value && accountScope.settled.value);

/**
 * 链接只带了会话、没带账号时，问一次「这条会话归谁」（PSU-2.46）。
 *
 * 远端会话的主键是 `(archive, source_account, conversation)`，所以一个裸的
 * conversation 在多账号归档里可能落在错的账号下（2.35a 那个 `filehelper` 就是
 * 例子）。**不带账号**问身份端点是一次探测：恰好一个账号独占这条会话时它会说出
 * 是谁；混合行答 null，那时保持默认账号——把合并行派给某一个账号是撒谎。
 *
 * 只在真有得挑的时候问：一个账号的归档没有歧义可解。
 */
const accountAmbiguous = computed(
  () =>
    configured.value &&
    accountScope.settled.value &&
    accountScope.accounts.value.length > 1 &&
    selectedId.value !== null &&
    routeAccountId.value === null,
);
const unscopedAccount: Ref<string | null> = ref(null);
const ownerQuery = useRemoteConversationDetailQuery(
  api,
  archiveId,
  selectedId,
  accountAmbiguous,
  unscopedAccount,
);
const probedAccountId = computed(() =>
  accountAmbiguous.value ? (ownerQuery.data.value?.source_account_id ?? null) : null,
);

// 过滤词与本机同一条路：交给服务端，不是在已加载的那一页里挑。远端能匹配的
// 比本机少（没有联系人备注与群成员名单），但走的是同一个输入框、同一个参数
// ——PSU-2.87 起连实现也是同一份（W3）。
const sidebarFilter = useWorkspaceSidebarFilter();
const { filterInput, filterText } = sidebarFilter;

// 账号换了就清空：两个账号的会话标识可以撞车（2.35a 为 filehelper 改的正是
// 这一点），留着上一个账号的名字就会把别人的名字挂到这条会话的标题上。
watch(
  () => accountScope.selectedAccountId.value,
  () => {
    sidebar.resetNames();
  },
);

const clearHighlight = (): void => {
  const conversation = selectedId.value;
  if (conversation === null) return;
  void router.replace({
    name: "remote-conversations",
    query: conversationQuery(conversation),
  });
};

const conversationsQuery = useRemoteConversationsQuery(
  api,
  archiveId,
  listEnabled,
  accountScope.selectedAccountId,
  filterText,
);
const rawConversations = computed(
  () => conversationsQuery.data.value?.pages.flatMap((page) => page.items) ?? [],
);
const conversations = computed(() =>
  rawConversations.value.map((item) =>
    remoteConversationToRender(item, conversationNames.value, avatarSrc),
  ),
);
const listErrorText = computed(() =>
  conversationsQuery.error.value === null
    ? null
    : errorMessage(conversationsQuery.error.value, "暂时无法加载远程会话。"),
);

/**
 * 侧栏那一段「聊天记录」（PSU-2.45）。同一个输入框既筛会话名也搜正文——与本机
 * **同一个** `ConversationList`、同一种两段版式，PSU-2.87 起连编排也是同一份（W3）。
 * 这一端从前写死 `search-enabled=false` 是实话：那时远端的搜索是全表扫，没有游标
 * 也没有计数，摆出来撑不住这块版式。
 *
 * 取名那一段（PSU-2.84 引入、2.85 收敛）也在里面：这一页的名字本来只有一个来源
 * ——会话列表 query 翻到过的页，而**输入框同时驱动那份查询**（`q=` 筛的是会话名
 * 不是正文，一打字它就答 0 条）。真机 614 个会话、一页 100 个：约 84% 的命中会话
 * 会因此印不透明标识，而点进去之后转录页的标题却是对的。
 */
const sidebarSearch = useRemoteSearchQuery(
  api,
  archiveId,
  computed<RemoteSearchParams | null>(() => sidebarFilter.params.value),
  listEnabled,
  accountScope.selectedAccountId,
);
const sidebar = useWorkspaceSidebarSearch({
  filter: sidebarFilter,
  pages: () => sidebarSearch.data.value?.pages ?? [],
  error: () => sidebarSearch.error.value,
  toPage: (entry) => ({
    items: entry.page.items.map((item) =>
      remoteHitToSearchHit(item, {
        attachmentsAvailable: attachmentsAvailable.value,
        avatarSrc,
        originalAvailable,
      }),
    ),
    total_count: entry.page.total_count,
    index_status: entry.page.index_status,
    conversation_counts: entry.page.conversation_counts,
  }),
  queriesLoading: () =>
    conversationsQuery.isLoading.value || sidebarSearch.isLoading.value,
  // 远端答不出名字（`display_name` 可以是 `null`）就照实印标识——那一档不变。
  listedNames: () =>
    rawConversations.value.map((item) =>
      item.display_name === null
        ? null
        : { id: item.conversation_id, name: item.display_name },
    ),
  errorFallback: "暂时无法搜索远程聊天记录。",
  // 作用域给的是 `useRemoteConversationDetailQuery` 那条键的前缀，所以同键去重、
  // 与「点进去」共享缓存、以及换账号时被 `AppShell` 既有的清理顺带带走，三件都是
  // 白拿的（PSU-2.85）。
  names: {
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
  },
});
const {
  conversationNames,
  hits: sidebarSearchHits,
  totalCount: sidebarSearchTotalCount,
  conversationCounts: sidebarSearchConversationCounts,
  indexStatus: sidebarSearchIndexStatus,
  errorText: sidebarSearchErrorText,
  loading: sidebarSearchLoading,
} = sidebar;

/**
 * 会话内查找（PSU-2.48）。与本机**同一块面板**，数据来自同一个搜索端点，只是
 * 多带一个 `conversation_id`——那是服务端在筛，不是把已加载的那几页再滤一遍。
 */
const searchLayerOpen = computed(() => routeLayer.value === "search");
const conversationSearchParams = computed<RemoteSearchParams | null>(() => {
  const conversation = selectedId.value;
  if (conversation === null || !search.active.value) return null;
  return {
    // 词空着也照查：契约里 `q` 可省，服务端按时间倒序给最新的记录——那正是
    // 微信打开面板就能看见最近消息的那一屏。
    q: searchQueryText.value === "" ? null : searchQueryText.value,
    conversationIds: [conversation],
    senderIds: [],
    messageTypes: search.categoryTypes.value,
    hasAttachment: null,
    dateFrom: search.dateFrom.value,
    dateTo: search.dateTo.value,
  };
});
const conversationSearch = useRemoteSearchQuery(
  api,
  archiveId,
  conversationSearchParams,
  listEnabled,
  accountScope.selectedAccountId,
);

// 会话名取自**会话流那一份**，不是消息里的副本——消息上根本没有这个字段，
// 正是为了避免改名之后留下一堆无法收敛的旧副本。
//
// 取名四级，与本机 `useConversationDetailQuery` 同一种模式（detail 优先、
// 列表兜底），只是这一端多了记忆表这一档：
//   1. 详情端点（记录模型 v6）——问得到就问，不必先把它翻出来；
//   2. 已加载页——详情还在路上、或这个远端到不了 v6 时；
//   3. 记忆表——换过滤词换掉了那一份缓存，但这一端见过这个名字；
//   4. 诚实占位——以上全落空，印不透明标识并由 ⓘ 说明。
const detailQuery = useRemoteConversationDetailQuery(
  api,
  archiveId,
  selectedId,
  listEnabled,
  accountScope.selectedAccountId,
);
const selectedConversation = computed<RenderConversation | null>(() => {
  const id = selectedId.value;
  if (id === null) return null;
  const detail = detailQuery.data.value;
  if (detail !== undefined && detail.conversation_id === id) {
    return remoteConversationToRender(detail, conversationNames.value, avatarSrc);
  }
  const found = rawConversations.value.find((item) => item.conversation_id === id);
  return found === undefined
    ? remoteConversationPlaceholder(id, conversationNames.value)
    : remoteConversationToRender(found, conversationNames.value, avatarSrc);
});

// 端有没有 attachment-view 由能力清单说了算，不由「有没有 ref」推断：
// 本机端与远程端都可能配了归档却没配对象存储，那时入口要出现并说明未配置，
// 而不是让人以为这一端做不到（PSU-2.28 的两种「不可用」）。
const capabilities = useCapabilityStore();
const attachmentsAvailable = computed(
  () =>
    capabilityState(capabilities.deployment, capabilities.states, "attachment-view") ===
    "available",
);

// blob id → 预签名 URL → 字节，折叠成本地组件唯一认得的 Blob 回调。
// 字节缓存活到离开本视图为止，与本机会话页的缓存寿命同一口径。
const loadOriginal = remoteAttachmentLoader((blobId) => api.getRemoteAttachmentUrl(blobId));

/**
 * 原图在不在（PSU-2.43）。答案随缩略图那次请求一起到，所以这张表是**逐渐填满**
 * 的——响应式在这里是必需的：气泡先画出来，标签随后才该出现。
 *
 * 只存明确的 true/false。契约里的 `null`（问不出来）与「还没问过」在界面上是
 * 同一件事——未知——所以它们在这里也是同一件事：这张表里没有这个键。
 */
const originalPresence = reactive<Record<string, boolean>>({});
const originalAvailable = (blobId: string): boolean | undefined => originalPresence[blobId];

const loadThumbnail = remoteThumbnailLoader(
  (blobId) => api.getRemoteAttachmentThumbnailUrl(blobId),
  (blobId, available) => {
    if (available !== null) originalPresence[blobId] = available;
  },
);

/**
 * 组件看到的 `loadAttachment`：原图取得到就取原图，取不到退到缩略图。
 *
 * 那 39,017 张原图早被本机配额淘汰的图片，点「查看」时唯一存在的字节就是缩略图。
 * 不退这一步的话，气泡上明明有图、标签也写着「仅缩略图」，点下去却弹「暂时无法
 * 打开附件，请重试。」——对一个永久状态说请重试。
 */
const loadAttachment = remoteAttachmentWithThumbnailFallback(
  loadOriginal,
  loadThumbnail,
  originalAvailable,
);

/** 在线分享取字节：存着的那份，不解 wxgf（PSU-2.185，与本机会话页同一条规矩）。 */
const shareLoaders = remoteShareLoaders(
  (blobId) => api.getRemoteAttachmentUrl(blobId),
  (blobId) => api.getRemoteAttachmentThumbnailUrl(blobId),
  originalAvailable,
);

/**
 * 头像（PSU-2.47）。字节与附件走**同一条路**（预签名 URL 直取对象存储），
 * 所以「这一端能不能取附件」就是「能不能取头像」——归档配了但没配对象存储时
 * 两者一起没有，ⓘ 里说的是「未配置」而不是「不可用」。
 *
 * 没有这个能力时连订阅都不发起：发起了也只是每张图排一次注定失败的请求。
 */
/**
 * 侧栏那一行说不说「或群成员匹配」（PSU-2.47）。
 *
 * 远端的会话名过滤现在与本机跑**同一套 OR 条件**（七列，含群成员名与群昵称）。
 * 2.46 把它写死 false 在当时是实话——那时远端只匹配一列；现在留着 false 就反过
 * 来了：让人以为筛不到的东西其实筛得到。
 */
const memberMatchEnabled = true;

// 头像用**全应用共用**的那一份 store（PSU-2.122）：底部 Tab 从「会话」切到
// 「搜索」再切回来此前会把整份字节扔掉，回来重下一遍。它自己带持久层，所以
// 关掉浏览器再打开也不必重取。
const avatars = useRemoteAvatars(
  sharedAvatarStoreVia((blobId) => api.getRemoteAttachmentUrl(blobId)),
);
const avatarSrc = (blobId: string | null | undefined): string | null =>
  attachmentsAvailable.value ? avatars.srcFor(blobId) : null;

/**
 * 换账号**不再清**头像缓存（PSU-2.122）。
 *
 * 从前这里 `avatars.reset()`，理由写的是「上一个账号的 id 在这个账号下取不到
 * 东西」。那句话只对了一半：`avatar_blob_id` 是账号加盐算出来的，所以同一个
 * id 的字节永远是同一份，留着不会拿错图，只是占内存——而内存有闲置队列兜底。
 * 现在 store 还是全应用共用的，在这里清等于把别的页面的图一起扔掉。
 * 下面那条 retain 会把不再需要的 id 退订，该回收的照样回收。
 */

const messagesQuery = useRemoteMessagesQuery(
  api,
  archiveId,
  selectedId,
  listEnabled,
  accountScope.selectedAccountId,
);
// 页序要倒过来：接口先给最新的一片，游标往回要更早的。这是两端**同一个**
// 分页约定，所以拼接也是同一个函数（`flattenMessagePages`）。
const rawMessages = computed(
  () => flattenMessagePages(messagesQuery.data.value?.pages ?? []),
);
const renderMessages = computed<RenderMessage[]>(() =>
  rawMessages.value.map((item) =>
    remoteMessageToRender(item, {
      attachmentsAvailable: attachmentsAvailable.value,
      avatarSrc,
      originalAvailable,
    }),
  ),
);
/**
 * 群成员名单（记录模型 v8，PSU-2.47）。**抽屉打开才问**：一条 614 会话的归档
 * 里，为每一条会话预取一份名单是几百次没人看的往返。
 */
const membersQuery = useRemoteConversationMembersQuery(
  api,
  archiveId,
  computed(() => (infoOpen.value ? selectedId.value : null)),
  listEnabled,
  accountScope.selectedAccountId,
);
const remoteMembers = computed<RenderMember[]>(() =>
  (membersQuery.data.value?.pages ?? [])
    .flatMap((page) => page.items)
    .map((item) => ({
      id: item.member_uid,
      display_name:
        item.display_name === "" ? `成员标识：${item.member_uid}` : item.display_name,
      group_alias: item.group_alias,
      is_self: item.is_self,
      // 两样本机有而这一端**没被告知**：远端的通讯录里没有这两列，写 null
      // 而不是 false／空串，那会声称「这个人是认得的」。
      contact_id: null,
      is_unknown: null,
      ...(() => {
        const src = avatarSrc(item.avatar_blob_id);
        return src === null ? {} : { avatar_ref: src };
      })(),
    })),
);
/**
 * 名单问不到时说的那句话。
 *
 * 远端到不了 v8 时答的是 `QUERY_INVALID`——一个确定性的答案，不是故障，所以
 * 这条查询 `retry: false`。要说出来的是「这一端还没有名单这回事」，而不是
 * 悄悄画一个空名单（那等于断言这个群没有别人）。
 */
const membersUnavailableNote = computed(() =>
  membersQuery.error.value === null
    ? null
    : "这个远程档案还没有群成员名单——本机跑过 `chat-archive-remote sync` 之后它才会出现。",
);

/**
 * 会话信息抽屉要的那一份详情。
 *
 * 三个 null 各说各的话：`media_count` 是「这一端不按类型统计附件」，
 * `member_count` 是「名单还没上传」（不是 0 人），`contact` 是「联系人的备注、
 * 签名、来源从来没有上行过」。
 */
const infoDetail = computed<RenderConversationDetail | null>(() => {
  const conversation = selectedConversation.value;
  if (conversation === null) return null;
  const detail = detailQuery.data.value;
  const roster = detail !== undefined && detail.conversation_id === conversation.id
    ? detail
    : null;
  return {
    ...conversation,
    media_count: null,
    member_count: roster?.member_count ?? null,
    announcement: roster?.announcement ?? null,
    my_group_alias: roster?.my_group_alias ?? null,
    contact: null,
  };
});

/**
 * 会话列表现在真的在视口里的那几行（PSU-2.122）。
 * `null` = 这一端没有 `IntersectionObserver`，按全部算。
 */
const visibleConversationIds = ref<readonly string[] | null>(null);

/**
 * 转录里现在真的画出来的那几条（PSU-2.124）。
 *
 * 与列表那一侧不同，这里**没有**「视口门不可用」这一档：转录是虚拟滚动的，
 * 虚拟器永远给得出窗口。缺省空数组 = 转录还没挂上来，那时也确实一条都不该取。
 */
const visibleMessageIds = ref<readonly string[]>([]);

/**
 * 现在想要哪些头像。
 *
 * 一次交出**全部**：订阅按 blob_id 的集合整体差分，不按行——虚拟滚动下按行
 * 订阅意味着每滚一屏就是一轮 subscribe/unsubscribe，而同一张图（默认头像、
 * 同一个人在多条会话里）会被反复计数。
 */
watch(
  [
    () => attachmentsAvailable.value,
    rawConversations,
    rawMessages,
    visibleConversationIds,
    visibleMessageIds,
    () => membersQuery.data.value?.pages ?? [],
  ],
  () => {
    if (!attachmentsAvailable.value) {
      // 不是 `reset()`：store 是全应用共用的，在这里清会把别的页面的图一起扔掉。
      avatars.retain([]);
      return;
    }
    // 只取**看得见的那几行**（与搜索页的名录同一个函数，PSU-2.128）。
    // 转录同理：取的是**画出来的那几条**的发送者，不是已加载的全部。翻过三页
    // 的群聊里，后者是几百条——按人去重之后仍然是几十张没人看的脸。
    const onScreenMessages = new Set(visibleMessageIds.value);
    avatars.retain([
      ...onScreenConversationAvatars(rawConversations.value, visibleConversationIds.value),
      ...rawMessages.value
        .filter((item) => onScreenMessages.has(item.record_key))
        .map((item) => item.sender_avatar_blob_id),
      ...(membersQuery.data.value?.pages ?? [])
        .flatMap((page) => page.items)
        .map((item) => item.avatar_blob_id),
    ]);
  },
  { immediate: true, deep: true },
);

/**
 * 空闲预取（本单元的热路径部分）。
 *
 * **这一端专有**，本机那一端没有接：本机读的是同一台机器上的 SQLite，点开的
 * 代价是几十毫秒，而导入还在同一个库上写——替没人点的会话去抢那把锁，省下
 * 的比赔进去的少。这条差异是**声明出来的**，不是漏掉的；两端共有的开页行为
 * （最新一页 40 条、往回翻 100 条）仍由 `workspaceParity.test.ts` 守着。
 *
 * 作用域是「这批会话属于谁」：换归档、换账号之后，旧作用域排着的那几条一律
 * 不再发——那些标识在新作用域下可能根本不存在。
 */
const queryClient = useQueryClient();
const prefetchScope = computed(() => {
  const archive = archiveId.value;
  if (archive === null || archive === "") return null;
  return { archiveId: archive, sourceAccountId: accountScope.selectedAccountId.value };
});
/**
 * 预取的候选：**看得见的行里最靠前的那几条**，去掉正在读的那一条（它已经在
 * 内存里了）。`visibleConversationIds` 为 null 是「这一端没有视口门」，那就按
 * 列表最前面几条算——那也正是没有视口门时屏幕上的那几行。
 */
const prefetchTargets = computed<readonly string[]>(() => {
  const visible = visibleConversationIds.value;
  const onScreen = visible === null ? null : new Set(visible);
  return rawConversations.value
    .map((item) => item.conversation_id)
    .filter((id) => id !== selectedId.value && (onScreen === null || onScreen.has(id)))
    .slice(0, PREFETCH_VISIBLE_LIMIT);
});
const messagePrefetch = useConversationMessagePrefetch({
  scope: prefetchScope,
  targets: prefetchTargets,
  // 列表还在翻页时不预取：那时候用户等的是列表本身，别跟它抢链路。
  settled: computed(
    () =>
      listEnabled.value &&
      !conversationsQuery.isLoading.value &&
      !conversationsQuery.isFetchingNextPage.value,
  ),
  prefetch: (conversationId, scope) =>
    prefetchRemoteLatestMessages(queryClient, api, { ...scope, conversationId }),
});

const transcriptErrorText = computed(() =>
  messagesQuery.error.value === null
    ? null
    : errorMessage(messagesQuery.error.value, "暂时无法加载远程消息。"),
);

// 这条转录还缺什么，是它的能力边界，必须说得出来；但它是一句读一次就够的话，
// 常驻一整行会在手机上把本就不多的正文高度吃掉。收进标题旁的 ⓘ。
const boundaryNoteOpen = ref(false);

// 这一页里有多少条还停在 v1 形状。大于 0 时那句说明才有意义——全都带着发送者
// 的时候再挂一句「可能缺字段」，等于教用户忽略提示。
const pendingCount = computed(() => unattributedRemoteCount(rawMessages.value));

// 发送者名的口径与本地**同一条**：群聊才逐条署名，单聊靠气泡站边区分。
// PSU-2.34 当初无条件传 true，是为了 `is_group: null`（这个远端没被告知）的
// 会话——但那个逃生口被套在了「已知不是群聊」上，于是单聊里每条消息头顶都
// 顶一行署名，与本地预览两个样。真正该走 true 的只有「不知道是不是群聊」。
const showSenderNames = computed<boolean | null>(() =>
  (selectedConversation.value?.is_group ?? null) === null ? true : null,
);

/**
 * 转录的定位与落点（PSU-2.87 · W4），与本机工作台**同一份**。
 *
 * 取窗口的端点不同（记录模型 v9，PSU-2.48），窗口这件事一个字不差。够不到 v9
 * 的远端答 `REMOTE_RECORD_MODEL_TOO_OLD`，那时退回这一端一直以来的做法——
 * 打开会话、命中翻进来才高亮——并**说出来**，而不是让读者对着一屏没有高亮的
 * 记录猜自己点错了什么。那一档是这一端独有的能力差异，所以它是个端口。
 */
const anchorUnsupported = ref(false);
const transcript = useWorkspaceTranscript({
  routeLayer,
  routeMessageId: workspace.routeMessageId,
  messages: renderMessages,
  scope: () => {
    const archive = archiveId.value;
    const conversation = selectedId.value;
    if (archive === null || conversation === null) return null;
    return `${archive}:${accountScope.selectedAccountId.value ?? ""}:${conversation}`;
  },
  fetchWindow: async (anchorMessageId, request) => {
    const conversation = selectedId.value;
    if (conversation === null) throw new Error("尚未选择会话");
    const account = accountScope.selectedAccountId.value;
    const window = await api.getRemoteMessageWindow(conversation, anchorMessageId, {
      ...request,
      ...(account === null ? {} : { sourceAccountId: account }),
    });
    return {
      items: window.items.map((item) =>
        remoteMessageToRender(item, {
          attachmentsAvailable: attachmentsAvailable.value,
          avatarSrc,
        }),
      ),
      anchorMessageId: window.anchor_message_id,
    };
  },
  loading: () => messagesQuery.isLoading.value,
  hasMore: () => messagesQuery.hasNextPage.value,
  onJumpFailed: (outcome) => {
    if (outcome.code !== "REMOTE_RECORD_MODEL_TOO_OLD") return false;
    anchorUnsupported.value = true;
    return true;
  },
});
const {
  transcriptRef,
  anchor,
  rows,
  messagesById,
  jumpError,
  loadOlderWindow,
  loadNewerWindow,
  recordCard: recordLayerCard,
  recordAttachments: recordLayerAttachments,
} = transcript;

const highlightedLoaded = computed(
  () =>
    highlightedRecordKey.value !== null && messagesById.value.has(highlightedRecordKey.value),
);

/**
 * 高亮归谁管：定位成功时归窗口（闪一下就退场），退回旧路时才归路由上那个
 * 命中标识——那一支要等命中翻进来，所以不能只闪一次。
 */
const transcriptHighlightId = computed(() =>
  anchorUnsupported.value ? highlightedRecordKey.value : anchor.highlightedId.value,
);

/**
 * 顶栏切了账号，地址栏得跟着走（PSU-2.65）。
 *
 * `account` 的优先级高于 store（2.46），可**那个参数是上一次导航自己写进去的**
 * ——打开任何一条会话都会钉上当前账号。没有这一条回写，切换就被自己刚写下的
 * 东西挡住：`remoteUi.selectedAccountId` 变了，而这一页的作用域仍旧读 URL 里的
 * 旧账号，列表一动不动。真机报的正是这一幕。
 *
 * 写的是 `account` 而不是清空：这一端的状态住在 URL 里，切完刷新还该在这个账号上。
 * `conversation` 不带过去——那条会话属于旧作用域（两个账号的会话标识会撞车，
 * 2.35a 的 `filehelper` 就是），带过去要么落空要么打开另一个人的同名会话。
 */
watch(
  () => remoteUi.selectedAccountId,
  (chosen) => {
    if (chosen === null || routeAccountId.value === chosen) return;
    // 地址栏本来就没钉住任何东西时，作用域已经跟着 store 走了，不用惊动路由。
    if (routeAccountId.value === null && selectedId.value === null) return;
    void router.replace({ name: "remote-conversations", query: { account: chosen } });
  },
);

/**
 * 书签里那个账号被并掉了：把 URL 换成它今天的身份（PSU-2.90）。
 *
 * 归并（PSU-2.86）之前存下的远端链接带的是被并掉的那条上传流 id。读侧现在认它，
 * 数据能出来了——但 URL 上还是旧值，切换器列的是 primary，**选中项对不上任何
 * 一条**，而且刷新一次又回到老 id。换掉它，书签下次就是新的。
 *
 * 用 `replace` 不用 `push`：这是用户已经在的那个位置，不该在历史里多留一格。
 * 会话 id 原样带着——它的翻译在服务端，这一侧不必也不该自己推。
 * 只在**真映射得上**时换：映射不上时 `preferredAccountId` 是 null，那是「这个
 * 账号根本不存在」，自愈不该把那件事一起盖掉。
 */
watch(
  [() => accountScope.settled.value, routeAccountId, accountScope.preferredAccountId],
  ([settled, fromLink, heir]) => {
    if (!settled || fromLink === null || heir === null || heir === fromLink) return;
    const query: Record<string, string> = { account: heir };
    const conversation = queryString(route.query.conversation);
    if (conversation !== null) query.conversation = conversation;
    void router.replace({ name: "remote-conversations", query });
  },
  { immediate: true },
);

// 退回旧路时命中要等它自己翻进来，才带读者过去。v9 的远端走不到这里：
// 窗口一取回来锚点就在视野里了。
let scrolledToKey: string | null = null;
watch([highlightedRecordKey, renderMessages], ([key, messages]) => {
  if (!anchorUnsupported.value) return;
  if (key === null || key === scrolledToKey) return;
  const index = messages.findIndex((item) => item.id === key);
  if (index < 0) return;
  scrolledToKey = key;
  transcriptRef.value?.scrollToIndex(index, "center");
});
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
        heading="远程档案"
        summary="此设备尚未配置远程档案。完成一次性配对后，即可在浏览器里分页查看和搜索远程记录。"
      >
        <p class="action-note">
          远程模式只按需加载你实际访问的页面。
          不会先下载完整数据库，也不会创建离线副本。
        </p>
      </RemoteUnconfiguredCard>
    </section>

    <section
      v-else
      class="remote-grid"
      :class="{
        'is-transcript': remoteUi.mobilePane === 'transcript',
        'has-drawer': infoOpen,
      }"
    >
      <ConversationList
        :items="conversations"
        :selected-id="selectedId"
        :filter-text="filterInput"
        :loading="conversationsQuery.isLoading.value"
        :fetching-more="conversationsQuery.isFetchingNextPage.value"
        :has-more="conversationsQuery.hasNextPage.value"
        :error-text="listErrorText"
        :search-hits="sidebarSearchHits"
        :search-loading="sidebarSearchLoading"
        :search-fetching-more="sidebarSearch.isFetchingNextPage.value"
        :search-has-more="sidebarSearch.hasNextPage.value"
        :search-total-count="sidebarSearchTotalCount"
        :search-conversation-counts="sidebarSearchConversationCounts"
        :search-error-text="sidebarSearchErrorText"
        :search-index-status="sidebarSearchIndexStatus"
        :conversation-names="conversationNames"
        :member-match-enabled="memberMatchEnabled"
        @update:filter-text="filterInput = $event"
        @select="selectConversation"
        @select-search-conversation="openSidebarSearchGroup"
        @visible-change="visibleConversationIds = $event"
        @prefetch-intent="messagePrefetch.prefetchNow($event)"
        @load-more="conversationsQuery.fetchNextPage()"
        @load-more-search="sidebarSearch.fetchNextPage()"
        @retry="conversationsQuery.refetch()"
        @retry-search="sidebarSearch.refetch()"
      />
      <div class="remote-transcript-column">
        <div
          v-if="selectedId !== null"
          class="remote-thin-bar"
        >
          <small>远程档案 · 只读</small>
          <button
            class="remote-thin-info"
            type="button"
            aria-label="远程记录还缺哪些字段"
            aria-controls="remote-thin-note"
            :aria-expanded="boundaryNoteOpen"
            @click="boundaryNoteOpen = !boundaryNoteOpen"
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
        </div>
        <p
          v-if="selectedId !== null"
          v-show="boundaryNoteOpen"
          id="remote-thin-note"
          class="remote-thin-note"
          role="note"
        >
          搜索能匹配正文，但匹配不到附件文件名（文件名不在上传的字节里）。
          <template v-if="attachmentsAvailable">
            附件与头像只在已上传到远程后才能打开；打不开的、以及只显示首字母的，
            是还没传上去，不是没有。
          </template>
          <template v-else>
            本端尚未配置对象存储，图片、文件与头像只标注类型或显示首字母。
            那是<strong>未配置</strong>，不是这一端做不到。
          </template>
          <template v-if="pendingCount > 0">
            本页有 {{ pendingCount.toLocaleString("zh-CN") }} 条记录还没有发送者信息。
            那是尚未重新上传的旧记录，不是没有发送者。
          </template>
          <template
            v-if="accountScope.selectedAccountId.value !== null
              && accountScope.unattributedCount.value > 0"
          >
            另有 {{ accountScope.unattributedCount.value.toLocaleString("zh-CN") }}
            条记录来自账号维度启用前的同步，无法证明归属。
            它们不出现在任何账号的视图里，不是丢了。
          </template>
        </p>
        <div
          v-if="anchorUnsupported && highlightedRecordKey !== null && !highlightedLoaded"
          class="transcript-banner remote-anchor-banner"
          role="status"
        >
          <span>这个远端还不支持定位到上下文；已打开命中所在会话，向上继续加载后可高亮命中。</span>
          <button
            class="text-action"
            type="button"
            @click="clearHighlight"
          >
            关闭提示
          </button>
        </div>
        <div
          v-if="transcriptErrorText !== null"
          class="transcript-error remote-transcript-error"
          role="alert"
        >
          <span>{{ transcriptErrorText }}</span>
          <button
            class="text-action"
            type="button"
            @click="messagesQuery.refetch()"
          >
            重新加载
          </button>
        </div>
        <MessageTranscript
          v-if="!(transcriptErrorText !== null && rows.length === 0)"
          ref="transcriptRef"
          :conversation="selectedConversation"
          :rows="rows"
          :messages-by-id="messagesById"
          :loading="messagesQuery.isLoading.value"
          :fetching-more="messagesQuery.isFetchingNextPage.value"
          :has-more="messagesQuery.hasNextPage.value"
          :window-items="anchor.windowItems.value"
          :window-has-more="anchor.windowHasOlder.value"
          :window-fetching-more="anchor.windowFetchingOlder.value"
          :window-has-newer="anchor.windowHasNewer.value"
          :window-fetching-newer="anchor.windowFetchingNewer.value"
          :highlighted-id="transcriptHighlightId"
          :error-text="jumpError"
          :search-return-available="searchReturnAvailable"
          :info-open="infoOpen"
          :active-media-id="transcriptOverlay.mediaId"
          :active-file-id="transcriptOverlay.fileId"
          :active-zoom="transcriptOverlay.zoom"
          :load-attachment="loadAttachment"
          :load-thumbnail="loadThumbnail"
          :share-loaders="shareLoaders"
          :search-available="true"
          :info-available="true"
          :export-available="false"
          :show-sender-names="showSenderNames"
          @visible-change="visibleMessageIds = $event"
          @back="backToConversationList"
          @load-more="messagesQuery.fetchNextPage()"
          @load-more-window="loadOlderWindow"
          @load-more-window-newer="loadNewerWindow"
          @exit-window="closeConversationLayer"
          @return-to-search="returnToSearch"
          @jump-to-reply="openReplyContext"
          @open-record="openMergedForward"
          @open-info="openConversationLayer('info')"
          @open-search="openConversationLayer('search')"
          @open-media-layer="mediaLayer.open"
          @close-media-layer="mediaLayer.close"
          @open-file-layer="fileLayer.open"
          @close-file-layer="fileLayer.close"
          @open-zoom-layer="zoomLayer.open"
          @close-zoom-layer="zoomLayer.close"
        />
        <ConversationSearchPanel
          :open="searchLayerOpen"
          :display-name="selectedConversation?.display_name ?? ''"
          :message-count="selectedConversation?.message_count ?? 0"
          :hits="conversationSearchHits"
          :loading="conversationSearch.isLoading.value"
          :fetching-more="conversationSearch.isFetchingNextPage.value"
          :has-more="conversationSearch.hasNextPage.value"
          :error-text="conversationSearchErrorText"
          :index-status="conversationSearchIndexStatus"
          :total-count="conversationSearchTotalCount"
          :category="searchCategory"
          :query="searchQueryText"
          date=""
          :date-available="false"
          :active-media-id="searchOverlay.mediaId"
          :load-attachment="loadAttachment"
          :load-thumbnail="loadThumbnail"
          @close="closeConversationLayer"
          @submit="updateSearchQuery"
          @update:category="updateSearchCategory"
          @open-hit="openSearchHit"
          @open-media-layer="mediaLayer.open"
          @close-media-layer="mediaLayer.close"
          @load-more="conversationSearch.fetchNextPage()"
          @retry="conversationSearch.refetch()"
        />
        <MergedForwardViewer
          v-if="recordLayerCard !== null"
          :open="routeLayer === 'record'"
          :card="recordLayerCard"
          :attachments="recordLayerAttachments"
          :active-media-id="recordOverlay.mediaId"
          :active-file-id="recordOverlay.fileId"
          :load-attachment="loadAttachment"
          :load-thumbnail="loadThumbnail"
          @close="closeConversationLayer"
          @open-media-layer="mediaLayer.open"
          @close-media-layer="mediaLayer.close"
          @open-file-layer="fileLayer.open"
          @close-file-layer="fileLayer.close"
        />
      </div>
      <ConversationInfoDrawer
        :conversation="infoDetail"
        :members="remoteMembers"
        :members-loading="membersQuery.isLoading.value"
        :members-fetching-more="membersQuery.isFetchingNextPage.value"
        :members-have-more="membersQuery.hasNextPage.value"
        :members-unavailable-note="membersUnavailableNote"
        :export-available="false"
        :search-available="false"
        :open="infoOpen"
        @close="closeConversationLayer"
        @load-more-members="membersQuery.fetchNextPage()"
      />
    </section>
  </AppShell>
</template>

import { computed, watch, ref, type ComputedRef, type Ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import type { SearchHit } from "@/api/archiveApi";
import {
  canNavigateBack as historyCanNavigateBack,
  queryString,
  useHistoryLayer,
  type HistoryLayer,
} from "@/composables/useHistoryLayer";

/**
 * 工作台的路由与导航，两端一份（PSU-2.87 · W1）。
 *
 * ## 为什么这件事需要一份共享实现
 *
 * 边界文档 §5 把共享分成三层：展示与数据形状早已共享，**编排**——取什么、
 * 什么时候取、点了怎么办、失败怎么退——仍是两份。行为住在编排层，所以两份编排
 * 等于每个新行为要写两次，而「写两次」在实践中退化成「本机先写、远程以后补」：
 * 追踪表 63 个单元里 13 个（21%）是这么来的。
 *
 * 这一片是那四片里的地基：`selectedId` / `routeLayer` 与全部导航动作。其余三片
 * 都从这里取「现在在哪一层、哪一条会话」。
 *
 * ## 端口是「端不同的那一件事」，不是配置面板
 *
 * 留在这里的每个选项都必须对应一条**声明过的**差异，否则它就是漏收敛：
 *
 * - `conversationExtras`：远端的会话主键是 `(归档, 账号, 会话)`（PSU-2.35a 为
 *   `filehelper` 跨账号撞号改的），所以链接要钉住 `account`；本机 `c.id` 全库
 *   唯一，没有这回事。这是**能力差异**。
 * - `restoreRouteState`：真相在路由里，两端都从路由算 `selectedId`，再把它镜像
 *   进各自的 store（字段集不同，两个 store 不并——见边界文档 §5）。这是**裁定
 *   D1** 的声明处。
 * - `externalReturn`：本机有独立搜索页（`SearchView`）那一条跨页回程；远端的
 *   独立搜索页走它自己的表单还原机制（`RemoteSearchFormState`），工作台这一侧
 *   不参与。这是**范围裁定**：跨页回程的统一超出本单元。
 *
 * 其余几个（`searchLayerParams` / `sidebarFilter` / `beforeLeaveTranscript`）
 * 不是差异，是**接线口**：W2/W3/W4 在 W1 之后才被创建，而 W1 要用到它们的状态。
 * 两端交进来的是同一份东西。
 */
export type WorkspaceRouteLayer =
  | "list"
  | "conversation"
  | "search"
  | "info"
  | "context"
  | "record";

/**
 * 地址栏里那个值的一种读法。实现住在 `useHistoryLayer`（浮层那一格与会话那一格
 * 读法必须是同一个），这里转发出去只是为了不让调用点分成两处 import。
 */
export { queryString } from "@/composables/useHistoryLayer";

/**
 * 这一次定位是从哪儿来的，回程就回哪儿（远程端 PSU-2.45 的口径，PSU-2.87 收敛
 * 为两端唯一一份）。
 *
 * `sidebar` 记的是当时侧栏那个检索词——不记就只能让人把词再打一遍；`panel`
 * 回的是这条会话自己的命中列表。两者回的不是一个地方。
 *
 * **会话 id 是这条记录的一部分**：从命中列表定位过一次之后切到别的会话，
 * 标题下不该还挂着「已定位到搜索结果」而点下去回的是另一条会话的列表。
 */
export type WorkspaceSearchReturn =
  | { kind: "sidebar"; conversationId: string; query: string }
  | { kind: "panel"; conversationId: string };

/** 跨页回程（本机的独立搜索页）。远端不传——那一端的搜索页自己还原表单。 */
export interface WorkspaceExternalReturn {
  /** 现在挂着的那条跨页回程；`null` = 没有。 */
  current: () => { conversationId: string } | null;
  /** 回那一页去。 */
  goBack: () => void;
  clear: () => void;
}

/** 侧栏那个过滤/检索输入框的读写口（状态归 W3）。 */
export interface WorkspaceSidebarFilterPort {
  /** 当前生效的检索词（防抖之后的那一个）。 */
  read: () => string;
  /** 写回输入框与防抖值；`""` = 清空。**两个都要写**，只写一个会留一拍旧查询键。 */
  write: (value: string) => void;
}

/**
 * 浮层那几格现在归哪一层用。
 *
 * `media` / `file` / `zoom` 全站只有一格，而同一时刻可能有三块版式都想认领它：
 * 转录、会话内查找面板、合并转发详情。谁在最上面就归谁——不分派的话，
 * `layer=record` 打开时底下的转录也会认领同一张图，一次「返回」看上去关掉了两层。
 */
export type WorkspaceOverlayOwner = "transcript" | "search" | "record";

export interface WorkspaceOverlay {
  /** 灯箱那一格：图片/视频的附件标识。 */
  mediaId: string | null;
  /** 文件预览那一格。 */
  fileId: string | null;
  /** 放大阅读 / 转账详情 / 红包详情那一格，形如 `<kind>:<消息>`。 */
  zoom: string | null;
}

export interface WorkspaceRouteOptions {
  /** 这一端的路由名：`conversations` / `remote-conversations`。 */
  routeName: string;
  /** 每条链接都要钉上的额外参数（远端的 `account`）。 */
  conversationExtras: () => Record<string, string>;
  /** 会话内查找那一层要写进链接的条件（`q` / `cat`，本机还有 `date`）。归 W2。 */
  searchLayerParams: () => Record<string, string>;
  /** 把路由说的那一份镜像进本端 store。 */
  restoreRouteState: (
    conversationId: string | null,
    layer: WorkspaceRouteLayer,
  ) => void;
  /** 离开转录去别的层之前记下阅读位置（归 W4 的 `transcriptRef`）。 */
  beforeLeaveTranscript: () => void;
  /** 侧栏检索词的读写（归 W3）。 */
  sidebarFilter: WorkspaceSidebarFilterPort;
  /** 见 {@link WorkspaceExternalReturn}。 */
  externalReturn?: WorkspaceExternalReturn;
}

export interface WorkspaceRoute {
  /** 路由说的那条会话。**真相在 URL 里**，store 里的是镜像（裁定 D1）。 */
  selectedId: ComputedRef<string | null>;
  routeLayer: ComputedRef<WorkspaceRouteLayer>;
  /** `message` 参数，与层无关；哪一层怎么用它归各层自己。 */
  routeMessageId: ComputedRef<string | null>;
  /** 标题下那行「已定位到搜索结果 · 返回搜索」该不该出现。 */
  searchReturnAvailable: ComputedRef<boolean>;
  conversationQuery: (
    conversationId: string,
    extra?: Record<string, string>,
  ) => Record<string, string>;
  searchLayerQuery: (
    layer: "search" | "context",
    messageId?: string | null,
  ) => Record<string, string>;
  canNavigateBack: () => boolean;
  backOrReplace: (fallback: Record<string, string>) => void;
  /** 灯箱那一层（`?media=`）：两端同一份，开一层压一条历史。 */
  mediaLayer: HistoryLayer;
  /** 文件预览那一层（`?file=`）。 */
  fileLayer: HistoryLayer;
  /** 放大阅读 / 转账 / 红包详情那一层（`?zoom=`）。 */
  zoomLayer: HistoryLayer;
  /** 这三格里，属于这一块版式的那一份（不属于它时读到 `null`）。 */
  overlayFor: (owner: WorkspaceOverlayOwner) => WorkspaceOverlay;
  goTo: (query: Record<string, string>) => void;
  selectConversation: (conversationId: string) => void;
  openSidebarSearchGroup: (group: {
    conversationId: string;
    hit: SearchHit;
    count: number;
  }) => void;
  openConversationLayer: (layer: "search" | "info") => void;
  openSearchHit: (messageId: string) => void;
  openReplyContext: (messageId: string) => void;
  openMergedForward: (messageId: string) => void;
  closeConversationLayer: () => void;
  backToConversationList: () => void;
  returnToSearch: () => void;
  replaceSearchRoute: () => void;
  clearSearchReturn: () => void;
  /** 这一端自己的编排偶尔要读它（本机的 `resetSearch`）。 */
  searchReturn: Ref<WorkspaceSearchReturn | null>;
}

export const useWorkspaceRoute = (options: WorkspaceRouteOptions): WorkspaceRoute => {
  const route = useRoute();
  const router = useRouter();

  const selectedId = computed(() => queryString(route.query.conversation));
  const routeLayer = computed<WorkspaceRouteLayer>(() => {
    if (selectedId.value === null) return "list";
    const candidate = queryString(route.query.layer);
    return candidate === "search" ||
      candidate === "info" ||
      candidate === "context" ||
      candidate === "record"
      ? candidate
      : "conversation";
  });
  const routeMessageId = computed(() => queryString(route.query.message));

  watch(
    [selectedId, routeLayer],
    ([conversation, layer]) => {
      options.restoreRouteState(conversation, layer);
    },
    { immediate: true },
  );

  const searchReturn = ref<WorkspaceSearchReturn | null>(null);
  const searchReturnAvailable = computed(() => {
    const conversation = selectedId.value;
    if (conversation === null) return false;
    if (searchReturn.value !== null) {
      return searchReturn.value.conversationId === conversation;
    }
    return options.externalReturn?.current()?.conversationId === conversation;
  });

  const conversationQuery = (
    conversationId: string,
    extra: Record<string, string> = {},
  ): Record<string, string> => ({
    conversation: conversationId,
    ...options.conversationExtras(),
    ...extra,
  });

  const searchLayerQuery = (
    layer: "search" | "context",
    messageId: string | null = null,
  ): Record<string, string> => {
    const conversation = selectedId.value;
    if (conversation === null) return {};
    const extra: Record<string, string> = { layer, ...options.searchLayerParams() };
    if (messageId !== null) extra.message = messageId;
    return conversationQuery(conversation, extra);
  };

  const goTo = (query: Record<string, string>): void => {
    void router.push({ name: options.routeName, query });
  };

  const canNavigateBack = historyCanNavigateBack;

  /**
   * 后退键要退**一层**，不是退出整个工作区。
   *
   * 有历史就真的后退（与浏览器自己的后退键一致）；没有历史——别人发来的链接
   * 直接打开的那种——就 replace 到兜底那一层，而不是把人扔出去。
   */
  const backOrReplace = (fallback: Record<string, string>): void => {
    if (canNavigateBack()) {
      router.back();
    } else {
      void router.replace({ name: options.routeName, query: fallback });
    }
  };

  const selectConversation = (conversationId: string): void => {
    options.sidebarFilter.write("");
    // 换到**别的**会话才作废回程：重选同一条不该杀掉它（裁定 D2，本机口径）。
    // 回程活到「用掉」或「换了会话」为止——中途退回会话层不该悄悄把它收走，
    // 否则从侧栏点进来的读者再也回不到那次检索（裁定 D8）。
    if (searchReturn.value?.conversationId !== conversationId) {
      searchReturn.value = null;
    }
    const external = options.externalReturn;
    if (external !== undefined && external.current()?.conversationId !== conversationId) {
      external.clear();
    }
    goTo(conversationQuery(conversationId));
  };

  /**
   * 侧栏那一行说的是「共 N 条相关聊天记录」，点它就该看到那 N 条。
   *
   * 多条先进这条会话的命中列表，只有一条才直接进上下文——那一步在只有一条时
   * 是多余的点击。关键词一路带着，否则命中列表打开时是空的，而回程也还不回去。
   */
  const openSidebarSearchGroup = (group: {
    conversationId: string;
    hit: SearchHit;
    count: number;
  }): void => {
    const query = options.sidebarFilter.read();
    options.sidebarFilter.write("");
    const keyword = query === "" ? {} : { q: query };
    // 回程记的是**侧栏**那一支：从哪来回哪去（裁定 D4）。没有检索词就没有可还原
    // 的东西，那时不记——记一条还不回去的回程比不记更糟。
    searchReturn.value =
      query === ""
        ? null
        : { kind: "sidebar", conversationId: group.conversationId, query };
    if (group.count > 1) {
      goTo(conversationQuery(group.conversationId, { layer: "search", ...keyword }));
      return;
    }
    goTo(
      conversationQuery(group.conversationId, {
        layer: "context",
        message: group.hit.message_id,
        ...keyword,
      }),
    );
  };

  const openConversationLayer = (layer: "search" | "info"): void => {
    const conversation = selectedId.value;
    if (conversation === null) return;
    if (layer === "search") {
      // 自己打开查找那一层，就没有「回搜索」这回事了：读者已经站在那张列表上。
      searchReturn.value = null;
      goTo(searchLayerQuery("search"));
      return;
    }
    goTo(conversationQuery(conversation, { layer: "info" }));
  };

  /** 命中列表里点一条：进上下文，并记住回程是那张列表而不是侧栏。 */
  const openSearchHit = (messageId: string): void => {
    const conversation = selectedId.value;
    if (conversation === null) return;
    searchReturn.value = { kind: "panel", conversationId: conversation };
    options.beforeLeaveTranscript();
    goTo(searchLayerQuery("context", messageId));
  };

  const openReplyContext = (messageId: string): void => {
    const conversation = selectedId.value;
    if (conversation === null) return;
    options.beforeLeaveTranscript();
    goTo(conversationQuery(conversation, { layer: "context", message: messageId }));
  };

  /**
   * 合并转发详情层（`layer=record`）。
   *
   * 不在这里校验卡片：转录只为 `merged_forward` 的气泡发这个事件
   * （`MessageTypeRenderer`），而「取不到卡片就不开层」归 W4 的显示判据。
   */
  const openMergedForward = (messageId: string): void => {
    const conversation = selectedId.value;
    if (conversation === null) return;
    options.beforeLeaveTranscript();
    goTo(conversationQuery(conversation, { layer: "record", message: messageId }));
  };

  const closeConversationLayer = (): void => {
    const conversation = selectedId.value;
    backOrReplace(conversation === null ? {} : conversationQuery(conversation));
  };

  const backToConversationList = (): void => {
    backOrReplace({});
  };

  /** 从哪来回哪去（裁定 D4，两端同一条）。 */
  const returnToSearch = (): void => {
    const context = searchReturn.value;
    if (context === null || context.conversationId !== selectedId.value) {
      const external = options.externalReturn;
      if (external?.current()?.conversationId === selectedId.value) {
        external.goBack();
      }
      return;
    }
    searchReturn.value = null;
    if (context.kind === "panel") {
      backOrReplace(searchLayerQuery("search"));
      return;
    }
    // 侧栏那一支：把词还给侧栏输入框，回列表。不还就只能让人再打一遍。
    options.sidebarFilter.write(context.query);
    backOrReplace({});
  };

  const replaceSearchRoute = (): void => {
    if (selectedId.value === null || routeLayer.value !== "search") return;
    void router.replace({ name: options.routeName, query: searchLayerQuery("search") });
  };

  const mediaLayer = useHistoryLayer("media");
  const fileLayer = useHistoryLayer("file");
  const zoomLayer = useHistoryLayer("zoom");

  const overlayOwner = computed<WorkspaceOverlayOwner>(() =>
    routeLayer.value === "search"
      ? "search"
      : routeLayer.value === "record"
        ? "record"
        : "transcript",
  );

  const overlayFor = (owner: WorkspaceOverlayOwner): WorkspaceOverlay => ({
    get mediaId() {
      return overlayOwner.value === owner ? mediaLayer.value : null;
    },
    get fileId() {
      return overlayOwner.value === owner ? fileLayer.value : null;
    },
    get zoom() {
      return overlayOwner.value === owner ? zoomLayer.value : null;
    },
  });

  const clearSearchReturn = (): void => {
    searchReturn.value = null;
    options.externalReturn?.clear();
  };

  return {
    selectedId,
    routeLayer,
    routeMessageId,
    searchReturnAvailable,
    conversationQuery,
    searchLayerQuery,
    canNavigateBack,
    backOrReplace,
    mediaLayer,
    fileLayer,
    zoomLayer,
    overlayFor,
    goTo,
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
    clearSearchReturn,
    searchReturn,
  };
};

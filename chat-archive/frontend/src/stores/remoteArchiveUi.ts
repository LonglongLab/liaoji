import { defineStore } from "pinia";

import type { WorkspaceRouteLayer } from "@/composables/useWorkspaceRoute";
import { remoteAccountChoices } from "@/lib/defaultAccount";
import type { SearchFilters } from "@/lib/searchFilters";

export type RemoteConversationPane = "list" | "transcript";

/**
 * 远程工作区里那几层，与本机 `ConversationRouteLayer` **同名**（PSU-2.46）。
 *
 * 这一端没有 `search` 那一层：侧栏检索不是一层，它在列表那一侧。
 *
 * `info` 是 2.47 补上的：群成员名单上行之后，这一层才有东西可打开。2.46 写下的
 * 「少一层就少一层，不在 URL 里留一个打不开的值」仍然成立——变的是它现在打得开。
 */
// `search` 是会话内查找那一层（PSU-2.48）：与本机同名，所以链接也同形。
//
// PSU-2.87 起它就是本机那个类型本身（`WorkspaceRouteLayer`）：候选集两端相同，
// 各写一份只是让「加一层」变成两处要记得改的地方。原名留作别名，调用点不动。
export type RemoteRouteLayer = WorkspaceRouteLayer;

/**
 * 离开远程搜索页去看一条命中时，那张表单当时的样子（PSU-2.46）。
 *
 * 与本机 `conversationUi.searchReturnContext` 同一件事：后退回来时结果还在，
 * 而不是让人把条件再填一遍。存的是**表单控件的值**而不是查询参数——回来要还原的
 * 是那张表单，而不是一次已经发出去的请求。
 */
export interface RemoteSearchFormState {
  q: string;
  /**
   * 远程搜索的筛选条件（PSU-2.66 是单值范围，PSU-2.75 起是多值）。与本机同一个
   * 类型：两端挂的是同一套组件，返回上下文没有理由长两个样。
   */
  filters: SearchFilters;
}

interface RemoteArchiveUiState {
  /**
   * 路由恢复出来的那条会话。**真相在 URL 里**（PSU-2.46）——这里是它的镜像，
   * 好让不看路由的消费方（比如手机版式）照旧读一个字段。
   */
  selectedConversationId: string | null;
  mobilePane: RemoteConversationPane;
  /**
   * 用户切到的远程账号（记录模型 v3，PSU-2.35b）。null = 还没切过，
   * 实际生效的作用域由 `useRemoteAccountScope` 回落到账号清单的第一个；
   * 这里不存清单，只存用户的选择。
   *
   * **应用级默认值留在这里**（与本机对齐）：URL 里的 `account` 只是为了让一条
   * 链接自己说得清是哪个账号，优先级高于它，但不该反过来把它写死。
   */
  selectedAccountId: string | null;
  /**
   * 钉住的默认账号（PSU-2.119）：每次打开都从它开始，直到取消。
   * null = 没钉，那时按「上次看过的」走。
   */
  defaultAccountId: string | null;
  /** 上次看过的账号。本机端一直有，远程端到 2.119 才补上。 */
  lastAccountId: string | null;
  /**
   * 上面两个是从哪份归档读来的。一个浏览器可以先后连上两份归档，它们的账号
   * id 不通用，所以存储按归档分键，这里记住当前认领的是哪一份。
   */
  choicesArchiveId: string | null;
  /** 见 {@link RemoteSearchFormState}。取走一次就清掉，不跨两次返回复用。 */
  searchReturn: RemoteSearchFormState | null;
}

export const useRemoteArchiveUiStore = defineStore("remoteArchiveUi", {
  state: (): RemoteArchiveUiState => ({
    selectedConversationId: null,
    mobilePane: "list",
    selectedAccountId: null,
    defaultAccountId: null,
    lastAccountId: null,
    choicesArchiveId: null,
    searchReturn: null,
  }),
  actions: {
    /**
     * 认领一份归档，把它那两个存下来的选择读进来（PSU-2.119）。
     *
     * 幂等：归档标识没变就什么都不做——`useRemoteAccountScope` 在外壳与视图里
     * 各调一次，重复读一次存储没有意义。归档标识要等 status 回来才有，在那
     * 之前认领的是 null，读到的自然也是空。
     */
    adoptArchive(archiveId: string | null): void {
      if (archiveId === this.choicesArchiveId) return;
      this.choicesArchiveId = archiveId;
      const choices = remoteAccountChoices(archiveId);
      this.defaultAccountId = choices.readPinned();
      this.lastAccountId = choices.readLast();
    },
    /**
     * 「就停在这个号」：把当前生效的账号定成**本次会话**的选择，别的一概不动。
     *
     * 设默认时用。这一端的账号是现算的（钉住的默认排在「上次看过的」前面），
     * 所以一个还没手动切过账号的人点下图钉，整份会话列表会当场换成另一个号的
     * ——他要的只是「下次打开从这儿开始」。本机端不会这样：那一端的选中账号
     * 是一个独立存着的值，钉默认碰不到它。两端要给同一个结果。
     */
    holdAccount(accountId: string | null): void {
      if (accountId === null || accountId === "") return;
      this.selectedAccountId = accountId;
    },
    /** 钉住 / 取消「打开时显示哪个号」。传 null 即取消。 */
    pinDefaultAccount(accountId: string | null): void {
      this.defaultAccountId = accountId;
      remoteAccountChoices(this.choicesArchiveId).writePinned(accountId);
    },
    selectAccount(accountId: string): void {
      if (accountId === "" || accountId === this.selectedAccountId) return;
      this.selectedAccountId = accountId;
      // 刷新、新标签页、换一台设备回来，这一条让人落回自己刚才在看的号，
      // 而不是清单里按内部 id 排在最前面的那个。
      this.lastAccountId = accountId;
      remoteAccountChoices(this.choicesArchiveId).writeLast(accountId);
      // 换了账号，上一个账号下那张表单里的会话过滤在这里筛不出东西——
      // 还原它会让人看到一个空结果，读起来像「这个账号没有记录」。
      this.searchReturn = null;
      // 旧账号下选中的会话不属于新作用域，留着会打开一条对方账号里根本不存在
      // （或同名不同人）的会话。真正的清除由视图 replace 掉那几个查询参数完成，
      // 这里只把镜像收回来。
      this.selectedConversationId = null;
      this.mobilePane = "list";
    },
    /**
     * 把 URL 说的那一份装回来（PSU-2.46）。与本机 `conversationUi` 同名同形。
     *
     * 刷新、后退、以及别人发来的一条链接，走的都是这一条——所以「所在会话」
     * 不再是一个只活在内存里的东西。
     */
    restoreRouteState(conversationId: string | null, layer: RemoteRouteLayer): void {
      this.selectedConversationId = conversationId;
      this.mobilePane = conversationId === null || layer === "list" ? "list" : "transcript";
    },
    backToList(): void {
      this.mobilePane = "list";
    },
    rememberSearchReturn(form: RemoteSearchFormState): void {
      this.searchReturn = form;
    },
    consumeSearchReturn(): RemoteSearchFormState | null {
      const form = this.searchReturn;
      this.searchReturn = null;
      return form;
    },
    reset(): void {
      this.selectedConversationId = null;
      this.mobilePane = "list";
      this.selectedAccountId = null;
      // 只收回内存里的镜像，**不动存储**：钉住的默认是用户的设定，
      // 不该被一次退出登录或换归档抹掉。下次认领时照旧读得回来。
      this.defaultAccountId = null;
      this.lastAccountId = null;
      this.choicesArchiveId = null;
      this.searchReturn = null;
    },
  },
});

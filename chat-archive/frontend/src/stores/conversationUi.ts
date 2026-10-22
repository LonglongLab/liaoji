import { defineStore } from "pinia";

import type { SearchSort } from "@/api/archiveApi";
import type { WorkspaceRouteLayer } from "@/composables/useWorkspaceRoute";
import type { SearchFilters } from "@/lib/searchFilters";

export type ConversationPane = "list" | "transcript";
/**
 * 工作台那几层。**两端同一个类型**（PSU-2.87 · W1）：候选集本来就一模一样，
 * 各写一份只是让「加一层」变成两处要记得改的地方。原名留作别名，调用点不动。
 */
export type ConversationRouteLayer = WorkspaceRouteLayer;

export type SearchReturnContext =
  | {
      kind: "conversation";
      conversationId: string;
      messageId: string;
      query: string;
      category: "file" | "media" | "link" | null;
      date: string;
    }
  | {
      kind: "global";
      conversationId: string;
      messageId: string;
      query: string;
      /**
       * 全局搜索的筛选条件（PSU-2.66 是单值范围，PSU-2.75 起是多值）。存的始终是
       * **当下那个模型**——留着已经不存在的字段，下一个人会以为还能靠它们把界面
       * 装回去。
       */
      filters: SearchFilters;
      sort: SearchSort;
    };

interface ConversationUiState {
  selectedConversationId: string | null;
  infoDrawerOpen: boolean;
  searchPanelOpen: boolean;
  mobilePane: ConversationPane;
  pendingAnchorMessageId: string | null;
  searchReturnContext: SearchReturnContext | null;
}

export const useConversationUiStore = defineStore("conversationUi", {
  state: (): ConversationUiState => ({
    selectedConversationId: null,
    infoDrawerOpen: false,
    searchPanelOpen: false,
    mobilePane: "list",
    pendingAnchorMessageId: null,
    searchReturnContext: null,
  }),
  actions: {
    restoreRouteState(
      conversationId: string | null,
      layer: ConversationRouteLayer,
    ): void {
      this.selectedConversationId = conversationId;
      this.mobilePane = conversationId === null || layer === "list" ? "list" : "transcript";
      this.searchPanelOpen = conversationId !== null && layer === "search";
      this.infoDrawerOpen = conversationId !== null && layer === "info";
    },
    selectConversation(conversationId: string): void {
      if (this.selectedConversationId !== conversationId) {
        this.infoDrawerOpen = false;
      }
      this.searchPanelOpen = false;
      this.selectedConversationId = conversationId;
      this.mobilePane = "transcript";
    },
    openConversationAt(conversationId: string, messageId: string): void {
      this.selectConversation(conversationId);
      this.pendingAnchorMessageId = messageId;
    },
    consumePendingAnchor(): string | null {
      const anchor = this.pendingAnchorMessageId;
      this.pendingAnchorMessageId = null;
      return anchor;
    },
    setSearchReturnContext(context: SearchReturnContext): void {
      this.searchReturnContext = context;
    },
    clearSearchReturnContext(): void {
      this.searchReturnContext = null;
    },
    backToList(): void {
      this.mobilePane = "list";
    },
    toggleInfoDrawer(): void {
      this.infoDrawerOpen = !this.infoDrawerOpen;
    },
    closeInfoDrawer(): void {
      this.infoDrawerOpen = false;
    },
    openSearchPanel(): void {
      this.searchPanelOpen = true;
    },
    closeSearchPanel(): void {
      this.searchPanelOpen = false;
    },
    resetForAccount(): void {
      this.selectedConversationId = null;
      this.infoDrawerOpen = false;
      this.searchPanelOpen = false;
      this.mobilePane = "list";
      this.pendingAnchorMessageId = null;
      this.searchReturnContext = null;
    },
  },
});

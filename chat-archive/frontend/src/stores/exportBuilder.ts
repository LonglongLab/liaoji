import { defineStore } from "pinia";
import type { ExportScope } from "@/api/archiveApi";
import { dateInputToUnixEnd, dateInputToUnixStart } from "@/utils/time";

const emptyNames = (): Record<string, string> => ({});

export const useExportBuilderStore = defineStore("exportBuilder", {
  state: () => ({
    workspaceIdentity: null as string | null,
    conversationIds: [] as string[],
    allConversations: false,
    conversationNames: emptyNames(),
    dateFromInput: "",
    dateToInput: "",
    customDates: false,
    includeImages: true,
    includeAudio: true,
    includeFiles: true,
    includeVideo: false,
    exportTitle: "聊天记录",
    step: 1,
    followingJobId: null as string | null,
    followingExportId: null as string | null,
  }),
  getters: {
    hasScope: (state) => state.allConversations || state.conversationIds.length > 0,
    validDates: (state) => !state.customDates || (
      state.dateFromInput !== "" && state.dateToInput !== "" &&
      dateInputToUnixStart(state.dateFromInput) !== null &&
      dateInputToUnixEnd(state.dateToInput) !== null &&
      state.dateFromInput <= state.dateToInput
    ),
  },
  actions: {
    bindWorkspace(identity: string): void {
      if ((this.workspaceIdentity !== null && this.workspaceIdentity !== identity)
          || (this.workspaceIdentity === null && identity.startsWith("remote:"))) this.$reset();
      this.workspaceIdentity = identity;
    },
    presetConversation(id: string): void {
      this.conversationIds = [id];
      this.allConversations = false;
      this.step = 1;
    },
    toggleConversation(id: string): void {
      this.allConversations = false;
      this.conversationIds = this.conversationIds.includes(id)
        ? this.conversationIds.filter((value) => value !== id) : [...this.conversationIds, id];
    },
    scopeFor(accountId: string): ExportScope {
      if (!this.hasScope || !this.validDates) throw new Error("请选择聊天和有效日期");
      const kinds: NonNullable<ExportScope["attachment_kinds"]> = [];
      if (this.includeImages) kinds.push("image", "emoji");
      if (this.includeAudio) kinds.push("audio");
      if (this.includeFiles) kinds.push("file");
      if (this.includeVideo) kinds.push("video");
      return {
        account_id: accountId,
        conversation_ids: this.allConversations ? [] : [...this.conversationIds],
        message_types: [],
        attachment_kinds: kinds,
        title: this.exportTitle.trim(),
        ...(this.customDates ? {
          date_from: dateInputToUnixStart(this.dateFromInput),
          date_to: dateInputToUnixEnd(this.dateToInput),
        } : {}),
      };
    },
    startFollowing(jobId: string, exportId: string): void {
      this.followingJobId = jobId;
      this.followingExportId = exportId;
    },
    clearFollowing(): void { this.followingJobId = null; this.followingExportId = null; },
    resetForAccount(): void { this.$reset(); },
  },
});

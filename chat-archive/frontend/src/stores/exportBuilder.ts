import { defineStore } from "pinia";
import type { ExportScope } from "@/api/archiveApi";
import { dateInputToUnixEnd, dateInputToUnixStart } from "@/utils/time";

const emptyNames = (): Record<string, string> => ({});

export type ExportPeriod = "all" | "today" | "month" | "week" | "thismonth" | "custom";
const defaultPeriod = (): ExportPeriod => "month";
interface ExportDates { from: string; to: string }
const localDateInput = (date: Date): string =>
  `${String(date.getFullYear())}-${String(date.getMonth() + 1).padStart(2, "0")}-${String(date.getDate()).padStart(2, "0")}`;
const presetExportDates = (period: Exclude<ExportPeriod, "custom">): ExportDates => {
  if (period === "all") return { from: "", to: "" };
  const now = new Date();
  const start = new Date(now);
  if (period === "thismonth") start.setDate(1);
  else if (period !== "today") start.setDate(start.getDate() - (period === "month" ? 29 : 6));
  return { from: localDateInput(start), to: localDateInput(now) };
};
export const exportDateLabel = (from: string, to: string): string =>
  from === to ? from || "全部时间" : `${from} 至 ${to}`;
const emptyRanges = (): Record<string, ExportDates> => ({});
const validDates = (from: string, to: string): boolean =>
  from <= to && dateInputToUnixStart(from) !== null && dateInputToUnixEnd(to) !== null;

export const useExportBuilderStore = defineStore("exportBuilder", {
  state: () => {
    const dates = presetExportDates("month");
    return {
      workspaceIdentity: null as string | null,
      conversationIds: [] as string[],
      allConversations: false,
      conversationNames: emptyNames(),
      dateFromInput: dates.from,
      dateToInput: dates.to,
      dateOverrides: emptyRanges(),
      period: defaultPeriod(),
      includeImages: true,
      includeAudio: true,
      includeFiles: true,
      includeVideo: false,
      step: 1,
      followingJobId: null as string | null,
      followingExportId: null as string | null,
    };
  },
  getters: {
    hasScope: (state) => state.allConversations || state.conversationIds.length > 0,
    activeOverrides: (state) => Object.entries(state.dateOverrides)
      .filter(([id]) => state.allConversations || state.conversationIds.includes(id)),
    validDates(): boolean {
      return (this.period === "all" || validDates(this.dateFromInput, this.dateToInput))
        && this.activeOverrides.every(([, range]) => (range.from === "" && range.to === "") || validDates(range.from, range.to));
    },
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
      this.dateOverrides = {};
      this.setPeriod("month");
      this.step = 1;
    },
    setPeriod(period: Exclude<ExportPeriod, "custom">): void {
      const { from, to } = presetExportDates(period);
      this.period = period;
      this.dateFromInput = from;
      this.dateToInput = to;
    },
    toggleConversation(id: string): void {
      this.allConversations = false;
      this.conversationIds = this.conversationIds.includes(id)
        ? this.conversationIds.filter((value) => value !== id) : [...this.conversationIds, id];
      if (!this.conversationIds.includes(id)) Reflect.deleteProperty(this.dateOverrides, id);
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
        title: "聊天记录",
        ...(this.activeOverrides.length ? {
          conversation_date_ranges: this.activeOverrides.map(([id, range]) => ({
            conversation_id: id,
            date_from: dateInputToUnixStart(range.from),
            date_to: dateInputToUnixEnd(range.to),
          })),
        } : {}),
        ...(this.period !== "all" ? {
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

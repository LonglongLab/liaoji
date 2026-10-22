<script setup lang="ts">
import { useQuery, useQueryClient } from "@tanstack/vue-query";
import { computed, onUnmounted, ref, watch, toRef } from "vue";

import { ArchiveApiError, type ExportEstimate, type ExportReport, type MessageSummary, type ExportSummary } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import WorkspaceAccountNotice from "@/components/WorkspaceAccountNotice.vue";
import AppShell from "@/components/AppShell.vue";
import ExportBuilderForm from "@/components/ExportBuilderForm.vue";
import ExportHistoryList from "@/components/ExportHistoryList.vue";
import ExportJobCard from "@/components/ExportJobCard.vue";
import ExportReportSheet from "@/components/ExportReportSheet.vue";
import ExportDeleteSheet from "@/components/ExportDeleteSheet.vue";
import { useExportViewport } from "@/composables/useExportViewport";
import "@/styles/export.css";
import { useExportConversations } from "@/composables/workspaceModules";
import { useJobFollower } from "@/composables/useJobFollower";
import { useActiveJobs } from "@/composables/activeJobs";
import { useExportBuilderStore } from "@/stores/exportBuilder";
import { useWorkspaceModuleScope } from "@/composables/workspaceModules";

const api = useArchiveApi();
const queryClient = useQueryClient();
const props = withDefaults(defineProps<{ mode?: "local" | "remote" }>(), { mode: "local" });
const workspace = useWorkspaceModuleScope(toRef(props, "mode"));
const { accountId, accountName } = workspace;
const store = useExportBuilderStore();
const follower = useJobFollower(api);
const viewport = useExportViewport();
const pageTab = ref<"new" | "history">("new");
const searchFocused = ref(false);
const openNewExport = (): void => {
  pageTab.value = "new";
  searchFocused.value = false;
  store.step = 1;
  const status = follower.job.value?.status;
  if (status === "completed" || status === "failed" || status === "cancelled") {
    follower.stop();
    follower.job.value = null;
    store.clearFollowing();
  }
};



const searchText = ref("");
const conversationsQuery = useExportConversations(workspace, searchText);
const conversations = conversationsQuery.conversations;

const historyQuery = useQuery({
  queryKey: computed(() => ["exports", workspace.identity.value, accountId.value] as const),
  enabled: computed(() => accountId.value !== null),
  queryFn: (): Promise<ExportSummary[]> => {
    const account = accountId.value;
    if (account === null) return Promise.resolve([]);
    return api.listExports(account);
  },
});
const historyErrorText = computed(() => {
  const error = historyQuery.error.value;
  if (error === null) return null;
  return error instanceof ArchiveApiError ? error.message : "暂时无法加载导出历史。";
});

// 「开始导出」从前只在 POST 在途的那一瞬间灰着（PSU-2.106）。作业一建出来按钮就
// 亮回来，手滑点两下＝两份内容相同的导出各写一个文件，各占一遍磁盘和时间。
//
// 后端不挡这一条：两份导出写的是两个文件，多导几份本来就允许。要挡的只是「没看见
// 已经有一个在跑」，所以闸门放在界面上。
const activeJobs = useActiveJobs(api, accountId, workspace.identity);
const runningExport = activeJobs.runningExport;

const estimate = ref<ExportEstimate | null>(null);
const estimating = ref(false);
const creating = ref(false);
const actionError = ref<string | null>(null);
const preview = ref<MessageSummary[]>([]);
const previewError = ref<string | null>(null);
const resultReport = ref<ExportReport | null>(null);
const resultBusy = ref(false);
const deleting = ref(false);
const deleteTarget = ref<ExportSummary | null>(null);
const deleteError = ref<string | null>(null);
const deletionNotice = ref<string | null>(null);
let deleteSequence = 0;
const downloadUrls = new Map<string, Set<string>>();
const revokeDownloads = (id?: string): void => {
  for (const [key, urls] of downloadUrls) {
    if (id !== undefined && key !== id) continue;
    for (const url of urls) URL.revokeObjectURL(url);
    downloadUrls.delete(key);
  }
};
const closeDelete = (): void => { if (!deleting.value) {deleteTarget.value=null;deleteError.value=null;} };
const requestDelete = (id: string): void => {
  if (deleting.value || resultBusy.value) return;
  const item = historyQuery.data.value?.find(record => record.id === id);
  if (item === undefined || item.status === "running") return;
  closeReport();
  deletionNotice.value = null;
  deleteError.value = null;
  deleteTarget.value = item;
};
const reportOpen = ref(false);
const reportLoading = ref(false);
const reportError = ref<string | null>(null);
const reportTitle = ref("");
const reportId = ref<string | null>(null);
let reportSequence = 0;
const closeReport = (): void => {
  reportSequence += 1; reportOpen.value = false; reportLoading.value = false;
  reportError.value = null; resultReport.value = null; reportId.value = null; reportTitle.value = "";
};
const scopeKey = computed(() => JSON.stringify([workspace.identity.value, accountId.value, store.conversationIds, store.allConversations, store.customDates, store.dateFromInput, store.dateToInput, store.includeImages, store.includeAudio, store.includeFiles, store.includeVideo]));
watch(scopeKey, () => { estimate.value = null; preview.value = []; previewError.value = null; });


const messageOf = (error: unknown): string =>
  error instanceof ArchiveApiError ? error.message : "服务暂时没有响应。";

const confirmDelete = async (): Promise<void> => {
  const target=deleteTarget.value;
  const account=accountId.value;
  const identity=workspace.identity.value;
  if (target===null || account===null || deleting.value) return;
  const sequence=++deleteSequence;
  const isCurrent=():boolean=>sequence===deleteSequence && account===accountId.value && identity===workspace.identity.value;
  deleting.value=true;
  deleteError.value=null;
  revokeDownloads(target.id);
  try {
    const result=await api.deleteExport(target.id,account);
    const historyKey=["exports",identity,account];
    await queryClient.cancelQueries({queryKey:historyKey,exact:true});
    queryClient.setQueryData<ExportSummary[]>(historyKey,items=>items?.filter(item=>item.id!==target.id));
    const jobsKey=identity.startsWith("local:") ? ["jobs",account] : ["jobs",identity,account];
    await queryClient.resetQueries({queryKey:jobsKey,exact:true});
    if (!isCurrent()) return;
    if(store.followingExportId===target.id){
      follower.stop();follower.job.value=null;store.clearFollowing();
      preview.value=[];previewError.value=null;estimate.value=null;
    }
    closeReport();
    deleteTarget.value=null;
    deletionNotice.value=result.legacy_job_history_retained
      ? "导出产物与记录已清理。旧任务没有导出关联，已保留以避免误删。"
      : "导出产物与记录已清理，原始聊天和附件保留。";
    await Promise.all([historyQuery.refetch(),activeJobs.refetch()]);
  } catch(error:unknown) {
    if(isCurrent()) {deleteError.value=messageOf(error);void historyQuery.refetch();}
  } finally {if(isCurrent())deleting.value=false;}
};

const runEstimate = async (): Promise<void> => {
  const account = accountId.value;
  if (account === null) return;
  estimating.value = true;
  actionError.value = null;
  try {
    const key = scopeKey.value;
    const scope = store.scopeFor(account);
    const result = await api.estimateExport(scope);
    if (key !== scopeKey.value) return;
    estimate.value = result;
    store.step = 3;
    try {
      const sample = await api.previewExport(scope);
      if (key === scopeKey.value) preview.value = workspace.normalizePreview(sample.items);
    } catch (error: unknown) {
      if (key === scopeKey.value) previewError.value = messageOf(error);
    }
  } catch (error: unknown) {
    actionError.value = messageOf(error);
  } finally {
    estimating.value = false;
  }
};

const startExport = async (): Promise<void> => {
  const account = accountId.value;
  const identity = workspace.identity.value;
  if (account === null || creating.value || runningExport.value !== null || !store.hasScope || !store.validDates || estimate.value === null || estimate.value.message_count === 0) return;
  creating.value = true;
  actionError.value = null;
  try {
    const created = await api.createExport(store.scopeFor(account), "html");
    if (identity !== workspace.identity.value) return;
    store.startFollowing(created.job_id, created.export_id);
    // 立刻把作业列表要回来（PSU-2.108）。`useActiveJobs` 只在**缓存里已经有**
    // 活跃作业时才轮询，而刚建出来的这一个还不在缓存里；下面那次 refetch 要等
    // 作业**跑完**才发生。不补这一下，`creating` 一落按钮就亮回来，
    // 「手滑点两下」照旧写出两份内容相同的导出——这条闸门要防的正是它。
    void activeJobs.refetch();

  } catch (error: unknown) {
    actionError.value = messageOf(error);
  } finally {
    creating.value = false;
  }
};

watch(workspace.identity, (identity) => {
  deleteSequence+=1;
  deleteTarget.value=null;deleteError.value=null;deleting.value=false;deletionNotice.value=null;
  revokeDownloads();
  follower.stop();
  follower.job.value = null;
  store.bindWorkspace(identity);
  closeReport();
  pageTab.value = "new";
  searchFocused.value = false;
  searchText.value = "";
  estimate.value = null;
  actionError.value = null;
}, { immediate: true });

onUnmounted(() => {
  deleteSequence+=1;
  revokeDownloads();
  follower.stop();
  reportSequence += 1;
});

const getReport = async (id: string): Promise<void> => {
  const account = accountId.value;
  const identity = workspace.identity.value;
  if (account === null) return;
  const sequence = ++reportSequence;
  reportId.value = id;
  const item = historyQuery.data.value?.find(record => record.id === id);
  reportTitle.value = typeof item?.scope.title === "string" ? item.scope.title : "聊天记录";
  reportOpen.value = true;
  reportLoading.value = true;
  reportError.value = null;
  resultReport.value = null;
  const isCurrent = (): boolean => sequence === reportSequence && reportOpen.value
    && account === accountId.value && identity === workspace.identity.value;
  try {
    const report = await api.getExportReport(id, account);
    if (isCurrent()) resultReport.value = report;
  } catch (error: unknown) { if (isCurrent()) reportError.value = messageOf(error); }
  finally { if (isCurrent()) reportLoading.value = false; }
};
const downloadResult = async (id: string): Promise<void> => {
  const account = accountId.value;
  const identity = workspace.identity.value;
  if (account === null || resultBusy.value) return;
  resultBusy.value = true;
  actionError.value = null;
  try {
    const blob = await api.downloadExport(id, account);
    if (account !== accountId.value || identity !== workspace.identity.value) return;
    const url = URL.createObjectURL(blob);
    const urls=downloadUrls.get(id) ?? new Set<string>();
    urls.add(url);downloadUrls.set(id,urls);
    const anchor = globalThis.document.createElement("a");
    anchor.href = url;
    const item = historyQuery.data.value?.find(record => record.id === id);
    const title = item?.scope.title;
    const label = typeof title === "string" ? title.replace(/[\\/:*?"<>|]/g, "").trim().slice(0, 80) : "聊天记录";
    anchor.download = `${label || '聊天记录'}.zip`;
    anchor.click();
    globalThis.setTimeout(() => {URL.revokeObjectURL(url);urls.delete(url);if(urls.size===0)downloadUrls.delete(id);}, 60_000);
  } catch (error: unknown) { actionError.value = messageOf(error); }
  finally { resultBusy.value = false; }
};
const openFolder = async (id: string): Promise<void> => {
  const account = accountId.value;
  if (account === null) return;
  actionError.value = null;
  try { await api.openExportFolder(id, account); }
  catch (error: unknown) { actionError.value = messageOf(error); }
};
watch(() => follower.job.value?.status, (status) => {
  if (status === "completed" && follower.job.value?.source_account_id === accountId.value) {
    pageTab.value = "history";
    searchFocused.value = false;
  }
});
const resumableExport = computed(() => runningExport.value ?? activeJobs.jobs.value.find(job => job.kind === "export" && job.status === "paused") ?? null);
watch(resumableExport, (job) => {
  if (job !== null && job.source_account_id === accountId.value && store.followingJobId === null) store.followingJobId = job.id;
}, { immediate: true });
watch([accountId, () => store.followingJobId], ([account, id]) => {
  if (account === null || id === null) return;
  void follower.follow(id, account).then(() => Promise.all([historyQuery.refetch(), activeJobs.refetch()]));
}, { immediate: true });

const pauseJob = async (): Promise<void> => {
  if (accountId.value !== null) await follower.pause(accountId.value);
};
const resumeJob = async (): Promise<void> => {
  const account = accountId.value;
  const jobId = store.followingJobId;
  if (account === null || jobId === null) return;
  await follower.resume(account);
  void follower.follow(jobId, account).then(() => historyQuery.refetch());
};
const cancelJob = async (): Promise<void> => {
  if (accountId.value !== null) await follower.cancel(accountId.value);
};
</script>

<template>
  <AppShell
    class="export-shell"
    :class="{ 'is-export-searching': searchFocused }"
    :style="viewport.style.value"
    variant="app"
    :account-name="accountName"
    :mode="mode"
  >
    <section
      class="export-page"
      :inert="reportOpen"
    >
      <header class="export-page-heading">
        <h1 class="page-title">
          保存聊天记录
        </h1>
      </header>
      <nav
        class="export-page-tabs"
        aria-label="导出页面"
      >
        <button
          type="button"
          :aria-pressed="pageTab === 'new'"
          @click="openNewExport"
        >
          新建导出
        </button>
        <button
          type="button"
          aria-label="查看导出记录"
          :aria-pressed="pageTab === 'history'"
          @click="pageTab = 'history'"
        >
          导出记录 <small>{{ historyQuery.data.value?.length ?? 0 }}</small>
        </button>
      </nav>
      <WorkspaceAccountNotice
        v-if="accountId === null"
        :hint="workspace.accountHint.value"
        :guide="workspace.accountGuide.value"
        :can-retry="workspace.canRetry.value"
        @retry="workspace.retryAccounts"
      />
      <div class="export-workspace">
        <div class="export-live-job">
          <ExportJobCard
            :job="follower.job.value?.source_account_id === accountId ? follower.job.value : null"
            :export-id="store.followingExportId"
            @pause="pauseJob"
            @resume="resumeJob"
            @cancel="cancelJob"
          />
        </div>
        <div
          v-if="pageTab === 'new'"
          class="export-builder-area"
        >
          <ExportBuilderForm
            :key="workspace.identity.value"
            :conversations="conversations"
            :account-id="accountId"
            :attachment-loader="workspace.loadAttachment"
            :thumbnail-loader="workspace.loadThumbnail"
            :preview="preview"
            :preview-error="previewError"
            :search-text="searchText"
            :avatar-for="conversationsQuery.avatarFor"
            :conversation-loading="conversationsQuery.isLoading.value"
            :has-more="conversationsQuery.hasNextPage.value"
            :loading-more="conversationsQuery.isFetchingNextPage.value"
            :conversation-error="conversationsQuery.error.value === null ? null : '暂时无法加载聊天列表'"
            :estimate="estimate"
            :estimating="estimating"
            :creating="creating"
            :running="runningExport !== null"
            :error-text="actionError"
            @search="searchText = $event"
            @more="conversationsQuery.fetchNextPage()"
            @retry-conversations="conversationsQuery.refetch()"
            @estimate="runEstimate"
            @start="startExport"
            @search-focus="searchFocused = $event"
            @visible-change="conversationsQuery.setVisibleIds($event)"
          />
        </div>
        <div
          v-else
          class="export-history-area"
        >
          <p
            v-if="deletionNotice !== null"
            role="status"
            class="export-note"
          >
            {{ deletionNotice }}
          </p>
          <div
            v-if="actionError !== null"
            role="alert"
            class="notice-warn"
          >
            {{ actionError }}
          </div>
          <ExportHistoryList
            :items="historyQuery.data.value ?? []"
            :allow-folder="workspace.canOpenFolder.value"
            :busy="resultBusy || deleting"
            :loading="historyQuery.isLoading.value"
            :error-text="historyErrorText"
            @download="downloadResult"
            @folder="openFolder"
            @report="getReport"
            @retry="historyQuery.refetch()"
            @delete="requestDelete"
          />
        </div>
      </div>
    </section>
    <ExportReportSheet
      :open="reportOpen"
      :title="reportTitle"
      :report="resultReport"
      :loading="reportLoading"
      :error-text="reportError"
      @close="closeReport"
      @retry="reportId !== null && getReport(reportId)"
    />
    <ExportDeleteSheet
      :open="deleteTarget !== null"
      :title="typeof deleteTarget?.scope.title === 'string' ? deleteTarget.scope.title : '聊天记录'"
      :busy="deleting"
      :error-text="deleteError"
      @close="closeDelete"
      @confirm="confirmDelete"
    />
  </AppShell>
</template>

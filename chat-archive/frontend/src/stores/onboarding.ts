import { defineStore } from "pinia";

import type {
  AccountSummary,
  ArchiveApi,
  ImportOptions,
  Job,
  SourceProbe,
  SourceSummary,
} from "@/api/archiveApi";
import {
  localAccountChoices,
  localPendingArchive,
  resolveOpeningAccount,
} from "@/lib/defaultAccount";
import { holdsAWorker } from "@/lib/jobStatus";
import {
  delay,
  type FlowError,
  flowErrorFrom,
  followRetryDelayMs,
  importReadyToBrowse,
  monitoringStops,
  newestImport,
  nothingLeftToWatch,
  type OnboardingStep,
  terminalStatuses,
  toIdentity,
} from "./onboardingHelpers";

export type { FlowError, OnboardingStep };

interface OnboardingState {
  step: OnboardingStep;
  accounts: AccountSummary[];
  probe: SourceProbe | null;
  sources: SourceSummary[];
  selectedSourceId: string | null;
  /** 钉住的默认账号（PSU-2.119）。null = 没钉，打开时按「上次看过的」走。 */
  defaultAccountId: string | null;
  currentJob: Job | null;
  pendingRetryJobId: string | null;
  lastSequence: number;
  flowError: FlowError | null;
  accountSwitchError: FlowError | null;
  riskConfirmationOpen: boolean;
  accountSwitchBusy: boolean;
  addingArchive: boolean;
  additionalArchiveReturnAccountId: string | null;
  additionalArchiveReturnJob: Job | null;
  busy: boolean;
}

// The follow loop is owned by the store, not by whichever view happened to
// start it: a newest-first import keeps running while the user browses, so
// the loop must survive route changes. Module scope keeps the controller out
// of Pinia state (it is not serializable and no view needs to see it).
let activeFollow: { jobId: string; controller: AbortController } | null = null;

export const useOnboardingStore = defineStore("onboarding", {
  state: (): OnboardingState => ({
    step: "loading",
    accounts: [],
    probe: null,
    sources: [],
    selectedSourceId: null,
    defaultAccountId: localAccountChoices.readPinned(),
    currentJob: null,
    pendingRetryJobId: null,
    lastSequence: 0,
    flowError: null,
    accountSwitchError: null,
    riskConfirmationOpen: false,
    accountSwitchBusy: false,
    addingArchive: false,
    additionalArchiveReturnAccountId: null,
    additionalArchiveReturnJob: null,
    busy: false,
  }),
  getters: {
    selectedAccount(state): AccountSummary | null {
      return state.accounts.find((item) => item.id === state.selectedSourceId) ?? null;
    },
    selectedSource(state): SourceSummary | null {
      return state.sources.find((item) => item.id === state.selectedSourceId) ?? null;
    },
    /**
     * 钉过默认，而它在当前账号清单里既不在、也没有继承者（PSU-2.119）。
     *
     * 现算而不存旗子：清单一变它就该跟着变，而一个存下来的布尔值只会停在
     * 它被写下的那一刻。清单为空时恒为 false——那是「还没加载出来」。
     */
    pinnedAccountLost(state): boolean {
      return resolveOpeningAccount({
        accounts: state.accounts.map(toIdentity),
        pinned: state.defaultAccountId,
      }).pinnedLost;
    },
    // Empty until a probe has run, and empty whenever the probe reports that no
    // method can work here; the view must not offer one the source cannot serve.
    availableKeyMethods(state): string[] {
      return state.probe === null ? [] : [...state.probe.available_key_methods];
    },
    // 全局导入横幅的数据源:当前账号有一个仍在工作的导入 job。
    activeImportJob(state): Job | null {
      const job = state.currentJob;
      if (job === null || job.kind !== "import") return null;
      // 判据用共用的那一份（`holdsAWorker`）：这里从前另写了一个值一模一样的
      // 集合，而它必须与后端闸门 `active_job_of_kind` 一致——同一件事有两份手抄，
      // 迟早只改得动其中一份（PSU-2.109）。
      return holdsAWorker.has(job.status) ? job : null;
    },
  },
  actions: {
    clearError(): void {
      this.flowError = null;
    },
    clearPendingRetry(): void {
      this.pendingRetryJobId = null;
    },
    prepareKeyRetry(job: Job | null): void {
      if (job !== null) {
        this.currentJob = job;
        this.pendingRetryJobId = job.import_scope === undefined ? null : job.id;
      } else {
        this.pendingRetryJobId = null;
      }
      this.step = "key";
    },
    clearAccountSwitchError(): void {
      this.accountSwitchError = null;
    },
    /** 钉住 / 取消「打开时显示哪个号」（PSU-2.119）。传 null 即取消。 */
    pinDefaultAccount(accountId: string | null): void {
      this.defaultAccountId = accountId;
      localAccountChoices.writePinned(accountId);
    },
    async restore(api: ArchiveApi): Promise<void> {
      this.busy = true;
      this.step = "loading";
      this.pendingRetryJobId = null;
      this.clearError();
      try {
        const bootstrap = await api.bootstrap();
        this.accounts = [...bootstrap.accounts];
        this.sources = [];
        const previous = localAccountChoices.readLast();
        const activeId = resolveOpeningAccount({
          accounts: bootstrap.accounts.map(toIdentity),
          pinned: this.defaultAccountId,
          last: previous,
          serverHint: bootstrap.active_account_id,
        }).id;
        const pendingJob = await localPendingArchive.find(this.accounts, previous, accountId => api.listJobs(accountId));
        if (pendingJob !== null) {
          this.selectedSourceId = pendingJob.source_account_id;
          this.currentJob = pendingJob;
          this.lastSequence = 0;
          this.addingArchive = activeId !== null && activeId !== pendingJob.source_account_id;
          this.additionalArchiveReturnAccountId = this.addingArchive ? activeId : null;
          this.additionalArchiveReturnJob = null;
          this.step = !this.addingArchive && importReadyToBrowse(pendingJob) ? "overview" : "progress";
          return;
        }
        this.selectedSourceId = activeId;
        if (activeId === null) {
          localAccountChoices.writeLast(null);
          // 一个账号都没有了，钉着谁都没有意义——留着只会在下次导入后
          // 复活成一个跟新档案无关的选择。
          this.pinDefaultAccount(null);
          this.step = "welcome";
          return;
        }
        localAccountChoices.writeLast(activeId);
        const jobs = await api.listJobs(activeId);
        this.currentJob = newestImport(jobs);
        // 正在跑的导入若已有可浏览消息(逆序流:最新消息已入库),直接进
        // 主界面,由全局横幅接管进度;还在解密、或已暂停/中断的任务仍回
        // 引导页处理。
        this.step =
          this.currentJob !== null && !terminalStatuses.has(this.currentJob.status)
            ? importReadyToBrowse(this.currentJob)
              ? "overview"
              : "progress"
            : "overview";
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        this.step = "error";
      } finally {
        this.busy = false;
      }
    },
    async switchAccount(api: ArchiveApi, accountId: string): Promise<boolean> {
      if (!this.accounts.some((item) => item.id === accountId)) {
        this.accountSwitchError = {
          code: "ACCOUNT_NOT_FOUND",
          message: "这个账号已不在当前档案中，请刷新账号列表后重试。",
          retryable: true,
          recommendedAction: "retry",
        };
        return false;
      }
      if (accountId === this.selectedSourceId) {
        localAccountChoices.writeLast(accountId);
        this.clearAccountSwitchError();
        return true;
      }
      this.accountSwitchBusy = true;
      this.clearAccountSwitchError();
      try {
        // Verify the target before committing the selection. This keeps the
        // current workspace intact if the local service or account is stale.
        const jobs = await api.listJobs(accountId);
        this.selectedSourceId = accountId;
        this.currentJob = newestImport(jobs);
        this.pendingRetryJobId = null;
        this.lastSequence = 0;
        this.flowError = null;
        localAccountChoices.writeLast(accountId);
        return true;
      } catch (error: unknown) {
        this.accountSwitchError = flowErrorFrom(error);
        return false;
      } finally {
        this.accountSwitchBusy = false;
      }
    },
    async detect(api: ArchiveApi): Promise<void> {
      this.busy = true;
      this.step = "detecting";
      this.pendingRetryJobId = null;
      this.clearError();
      try {
        const [sourceProbe, sourcePage] = await Promise.all([
          api.probeSources(),
          api.listSources(),
        ]);
        this.probe = sourceProbe;
        this.sources = sourcePage.items;
        this.selectedSourceId = sourcePage.items.length === 1 ? sourcePage.items[0]?.id ?? null : null;
        if (sourcePage.items.length === 0) {
          this.flowError = {
            code: "SOURCE_NOT_FOUND",
            message: "没有找到可归档的微信账号。请先登录微信，再重新检测。",
            retryable: true,
            recommendedAction: "reselect_source",
          };
          this.step = "error";
          return;
        }
        this.step = "source";
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        this.step = "error";
      } finally {
        this.busy = false;
      }
    },
    // Acquiring a key needs the client running and reading a consistent source
    // needs it closed, so the probe taken at detection is stale by design on
    // the later steps. Refresh it in place rather than restarting the flow.
    async refreshProbe(api: ArchiveApi): Promise<void> {
      this.busy = true;
      this.clearError();
      try {
        this.probe = await api.probeSources();
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
      } finally {
        this.busy = false;
      }
    },
    selectSource(sourceId: string): void {
      if (!this.sources.some((item) => item.id === sourceId)) {
        throw new Error("账号候选不存在");
      }
      this.pendingRetryJobId = null;
      this.selectedSourceId = sourceId;
    },
    confirmSource(): void {
      if (this.selectedSource === null) {
        throw new Error("请选择一个账号");
      }
      this.pendingRetryJobId = null;
      this.step = this.selectedSource.has_key ? "import" : "key";
    },
    openDebuggerConfirmation(): void {
      this.riskConfirmationOpen = true;
    },
    closeDebuggerConfirmation(): void {
      this.riskConfirmationOpen = false;
    },
    async acquireKey(
      api: ArchiveApi,
      method: string,
      userConsent: boolean,
    ): Promise<void> {
      const selectedAccountId = this.selectedSourceId;
      if (selectedAccountId === null) {
        throw new Error("请选择一个账号");
      }
      this.busy = true;
      this.clearError();
      let retryJob: Job | null = null;
      try {
        await api.acquireKey(selectedAccountId, method, userConsent);
        const selectedSource = this.selectedSource;
        if (selectedSource !== null) selectedSource.has_key = true;
        this.riskConfirmationOpen = false;
        const failed = this.currentJob;
        retryJob = this.pendingRetryJobId !== null
          && failed?.id === this.pendingRetryJobId
          && failed.source_account_id === selectedAccountId
          && failed.import_scope !== undefined
          && (failed.status === "failed" || failed.status === "interrupted")
          ? failed
          : null;
        this.pendingRetryJobId = null;
        this.step = retryJob === null ? "import" : "progress";
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        this.step = "key";
      } finally {
        this.busy = false;
      }
      if (retryJob !== null) await this.retryImport(api, retryJob);
    },
    async startImport(api: ArchiveApi, options: ImportOptions = {}): Promise<void> {
      await this.submitImport(api, account => api.createImport(account, options), options.initial_archive === true);
    },
    async retryImport(api: ArchiveApi, job: Job): Promise<boolean> {
      if (job.source_account_id !== this.selectedSourceId || job.import_scope === undefined) return false;
      return this.submitImport(api, account => api.retryImport(job.id, account), job.import_scope.initial_archive);
    },
    async submitImport(
      api: ArchiveApi,
      submit: (accountId: string) => Promise<string>,
      initialArchive: boolean,
    ): Promise<boolean> {
      const selectedAccountId = this.selectedSourceId;
      if (selectedAccountId === null) {
        throw new Error("请选择一个账号");
      }
      if (this.busy) return false;
      this.busy = true;
      this.pendingRetryJobId = null;
      this.clearError();
      try {
        const jobId = await submit(selectedAccountId);
        if (this.selectedSourceId !== selectedAccountId) return false;
        if (initialArchive) localPendingArchive.remember(selectedAccountId);
        const job = await api.getJob(jobId, selectedAccountId);
        if (this.selectedSourceId !== selectedAccountId) return false;
        this.currentJob = job;
        this.lastSequence = 0;
        this.step = "progress";
        return true;
      } catch (error: unknown) {
        if (this.selectedSourceId === selectedAccountId) this.flowError = flowErrorFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },
    // PSU-2.22:messages 阶段一开始提交批次,引导页就放行进主界面,进度
    // 交给全局横幅。追加账号的导入(addingArchive)不放行——那个账号还没
    // 进入 accounts 列表,提前切换会把用户丢进半初始化的工作区。
    maybeLeaveProgressForWorkspace(): void {
      if (this.step !== "progress" || this.addingArchive) return;
      const job = this.currentJob;
      if (job !== null && importReadyToBrowse(job)) {
        this.step = "overview";
      }
    },
    // 单例跟随:同一个 job 只允许一条 followJob 循环。视图(引导页的进度
    // 面板、主界面的导入横幅)只声明"需要跟随",循环归 store 所有,路由
    // 切换不中断——这正是"导入期间系统可用"的前提。
    ensureFollowing(api: ArchiveApi): void {
      const job = this.currentJob;
      if (job === null || (monitoringStops.has(job.status) && job.status !== "completed")) return;
      if (activeFollow !== null && activeFollow.jobId === job.id) return;
      activeFollow?.controller.abort();
      const controller = new AbortController();
      const entry = { jobId: job.id, controller };
      activeFollow = entry;
      void this.followJob(api, controller.signal).finally(() => {
        if (activeFollow === entry) activeFollow = null;
      });
    },
    // 横幅上的"前往处理":失败/暂停的导入回到引导页的任务面板。
    //
    // 跑完的（完成 / 取消）不进去：进度面板对它们没有出口，也不起跟随循环，
    // 进去就是一张退不出来的「已完成」卡——2026-09-26 真机截图就是这一刻。
    // 返回值说的是「进没进去」，调用方据此决定往哪儿走。
    reopenImportProgress(): boolean {
      const job = this.currentJob;
      if (job === null || job.kind !== "import" || nothingLeftToWatch.has(job.status)) {
        return false;
      }
      this.step = "progress";
      return true;
    },
    // 「看自动同步这一轮的进度」：按这一轮的作业 id 去取那一个，而不是信手头的
    // currentJob——同一个账号时切账号那一步直接返回，手头那份可能是早跑完的旧作业。
    async openImportJob(api: ArchiveApi, jobId: string): Promise<boolean> {
      const accountId = this.selectedSourceId;
      if (accountId === null) return false;
      const job = await api.getJob(jobId, accountId);
      if (this.selectedSourceId !== accountId) return false;
      if (this.currentJob?.id !== job.id) this.lastSequence = 0;
      this.currentJob = job;
      if (!this.reopenImportProgress()) return false;
      this.ensureFollowing(api);
      return true;
    },
    async followJob(api: ArchiveApi, signal?: AbortSignal): Promise<void> {
      const selectedAccountId = this.selectedSourceId;
      if (selectedAccountId === null || this.currentJob === null) {
        return;
      }
      let consecutiveFailures = 0;
      let reportedOutage = false;
      let jobId = this.currentJob.id;
      try {
        while (!monitoringStops.has(this.currentJob.status) && !signal?.aborted) {
          jobId = this.currentJob.id;
          // Something else (cancelAdditionalArchive, an account switch) may
          // swap currentJob/selectedSourceId out from under this loop while
          // an await below is in flight. Re-check right before every mutation
          // instead of trusting the (jobId, selectedAccountId) this iteration
          // started with — otherwise a stale getJob() response can clobber
          // state a newer flow just set, or pair an old job id with a new
          // account id.
          const stillTrackingThisJob = (): boolean =>
            this.currentJob?.id === jobId && this.selectedSourceId === selectedAccountId;
          try {
            this.lastSequence = await api.readJobEvents(
              jobId,
              selectedAccountId,
              (event) => {
                if (this.currentJob?.id !== event.payload.job_id) {
                  return;
                }
                this.currentJob = {
                  ...this.currentJob,
                  status: event.payload.status,
                  stage: event.payload.stage,
                  progress_current: event.payload.progress_current,
                  progress_total: event.payload.progress_total,
                  stage_progress_current: event.payload.stage_progress_current,
                  stage_progress_total: event.payload.stage_progress_total,
                  stage_progress_unit: event.payload.stage_progress_unit,
                  stage_started_at: event.payload.stage_started_at,
                  error_code: event.payload.error_code,
                  updated_at: event.payload.created_at,
                };
                this.maybeLeaveProgressForWorkspace();
              },
              {
                lastSequence: this.lastSequence,
                waitMs: 25_000,
                ...(signal === undefined ? {} : { signal }),
              },
            );
            if (!stillTrackingThisJob()) {
              return;
            }
            const refreshed = await api.getJob(jobId, selectedAccountId);
            if (!stillTrackingThisJob()) {
              return;
            }
            this.currentJob = refreshed;
            this.maybeLeaveProgressForWorkspace();
            consecutiveFailures = 0;
            // Only the outage this loop reported may be cleared here; an error
            // raised elsewhere is not ours to withdraw.
            if (reportedOutage) {
              reportedOutage = false;
              this.clearError();
            }
          } catch (error: unknown) {
            if (signal?.aborted) {
              return;
            }
            if (!stillTrackingThisJob()) {
              return;
            }
            consecutiveFailures += 1;
            this.flowError = flowErrorFrom(error);
            reportedOutage = true;
            await delay(followRetryDelayMs(consecutiveFailures), signal);
          }
        }
        if (
          this.currentJob.status === "completed" &&
          this.currentJob.id === jobId &&
          this.selectedSourceId === selectedAccountId
        ) {
          const builtInitialArchive = this.currentJob.import_scope?.initial_archive === true;
          const bootstrap = await api.bootstrap();
          this.accounts = [...bootstrap.accounts];
          if (this.accounts.some((account) => account.id === selectedAccountId)) {
            this.selectedSourceId = selectedAccountId;
          } else {
            this.selectedSourceId =
              bootstrap.active_account_id ??
              bootstrap.accounts[0]?.id ??
              selectedAccountId;
          }
          localAccountChoices.writeLast(this.selectedSourceId);
          if (builtInitialArchive) localPendingArchive.clearFor(selectedAccountId);
          this.addingArchive = false;
          this.additionalArchiveReturnAccountId = null;
          this.additionalArchiveReturnJob = null;
          this.step = "overview";
        }
      } catch (error: unknown) {
        if (signal?.aborted) {
          return;
        }
        this.flowError = flowErrorFrom(error);
      }
    },
    async pauseJob(api: ArchiveApi): Promise<boolean> {
      if (this.currentJob === null || this.selectedSourceId === null || this.busy) {
        return false;
      }
      this.busy = true;
      this.clearError();
      try {
        this.currentJob = await api.pauseJob(this.currentJob.id, this.selectedSourceId);
        return true;
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },
    async resumeJob(api: ArchiveApi): Promise<boolean> {
      if (this.currentJob === null || this.selectedSourceId === null || this.busy) {
        return false;
      }
      this.busy = true;
      this.clearError();
      try {
        this.currentJob = await api.resumeJob(this.currentJob.id, this.selectedSourceId);
        return true;
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },
    async cancelJob(api: ArchiveApi): Promise<boolean> {
      if (this.currentJob === null || this.selectedSourceId === null || this.busy) {
        return false;
      }
      this.busy = true;
      this.clearError();
      try {
        this.currentJob = await api.cancelJob(this.currentJob.id, this.selectedSourceId);
        return true;
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },
    recoverCurrentJob(): void {
      const errorCode = this.currentJob?.error_code;
      this.currentJob = null;
      this.pendingRetryJobId = null;
      this.lastSequence = 0;
      this.step = errorCode === "KEY_INVALID" ? "key" : "import";
    },
    startAnotherArchive(): void {
      this.step = "welcome";
      this.accounts = [];
      this.probe = null;
      this.sources = [];
      this.selectedSourceId = null;
      this.currentJob = null;
      this.pendingRetryJobId = null;
      this.lastSequence = 0;
      this.accountSwitchError = null;
      this.accountSwitchBusy = false;
      this.addingArchive = false;
      this.additionalArchiveReturnAccountId = null;
      this.additionalArchiveReturnJob = null;
      this.clearError();
    },
    startAdditionalArchive(): void {
      if (this.selectedSourceId === null || this.accounts.length === 0) {
        this.startAnotherArchive();
        return;
      }
      this.additionalArchiveReturnAccountId = this.selectedSourceId;
      this.additionalArchiveReturnJob = this.currentJob;
      this.addingArchive = true;
      this.step = "welcome";
      this.probe = null;
      this.sources = [];
      this.pendingRetryJobId = null;
      this.lastSequence = 0;
      this.riskConfirmationOpen = false;
      this.clearError();
      this.clearAccountSwitchError();
    },
    cancelAdditionalArchive(): void {
      if (!this.addingArchive) return;
      localPendingArchive.clearFor(this.selectedSourceId);
      this.selectedSourceId = this.additionalArchiveReturnAccountId;
      if (this.selectedSourceId !== null) localAccountChoices.writeLast(this.selectedSourceId);
      this.currentJob = this.additionalArchiveReturnJob;
      this.step = "overview";
      this.probe = null;
      this.sources = [];
      this.pendingRetryJobId = null;
      this.lastSequence = 0;
      this.riskConfirmationOpen = false;
      this.addingArchive = false;
      this.additionalArchiveReturnAccountId = null;
      this.additionalArchiveReturnJob = null;
      this.clearError();
      this.clearAccountSwitchError();
    },
  },
});

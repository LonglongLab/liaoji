import { defineStore } from "pinia";

import {
  type AccountSummary,
  ArchiveApiError,
  type ArchiveApi,
  type ImportOptions,
  type Job,
  type SourceProbe,
  type SourceSummary,
} from "@/api/archiveApi";
import {
  type AccountIdentity,
  localAccountChoices,
  resolveOpeningAccount,
} from "@/lib/defaultAccount";
import { holdsAWorker } from "@/lib/jobStatus";

export type OnboardingStep =
  | "loading"
  | "welcome"
  | "detecting"
  | "source"
  | "key"
  | "import"
  | "progress"
  | "overview"
  | "error";

export interface FlowError {
  code: string;
  message: string;
  retryable: boolean;
  recommendedAction: ArchiveApiError["recommendedAction"];
}

interface OnboardingState {
  step: OnboardingStep;
  accounts: AccountSummary[];
  probe: SourceProbe | null;
  sources: SourceSummary[];
  selectedSourceId: string | null;
  /** 钉住的默认账号（PSU-2.119）。null = 没钉，打开时按「上次看过的」走。 */
  defaultAccountId: string | null;
  currentJob: Job | null;
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

const terminalStatuses: ReadonlySet<Job["status"]> = new Set([
  "completed",
  "failed",
  "cancelled",
]);
// 进度面板对这两种没有任何出口：没得暂停、没得重试，只剩一张「已完成」。
const nothingLeftToWatch: ReadonlySet<Job["status"]> = new Set(["completed", "cancelled"]);
const monitoringStops: ReadonlySet<Job["status"]> = new Set([
  ...terminalStatuses,
  "paused",
]);
// 「上次看过的」「钉住的默认」两把键都住在 `lib/defaultAccount`：远程端要的是
// 同一套语义，各写一份就是两处要记得一起改的地方（PSU-2.119）。
const rememberAccountId = (accountId: string): void =>
  localAccountChoices.writeLast(accountId);

const forgetAccountId = (): void => localAccountChoices.writeLast(null);

/** 本机档案没有归并维度：一个账号就是一个数据目录，不存在「被并掉的旧 id」。 */
const toIdentity = (account: AccountSummary): AccountIdentity => ({
  id: account.id,
  mergedIds: [],
});

// A watched import keeps running inside the local service even when the
// browser briefly cannot reach it, so a failed poll waits and tries again
// instead of ending the watch. Back off so a service that is really gone is
// not hammered, and cap the wait so recovery is still noticed promptly.
const followRetryDelayMs = (consecutiveFailures: number): number =>
  Math.min(30_000, 1_000 * 2 ** (consecutiveFailures - 1));

const delay = (milliseconds: number, signal?: AbortSignal): Promise<void> =>
  new Promise((resolve) => {
    const timer = globalThis.setTimeout(resolve, milliseconds);
    signal?.addEventListener(
      "abort",
      () => {
        globalThis.clearTimeout(timer);
        resolve();
      },
      { once: true },
    );
  });


/**
 * 引导流程只认导入作业（PSU-2.107）。
 *
 * `jobs[0]` 取的是**任意种类**里最新的那一个，于是一个正在跑的同步作业会被当成
 * 导入：`step` 变成 `"progress"`，刷新页面就被丢进「建立档案 / 快照与解密」，
 * 而那一屏显示的其实是同步的计数。真机上这一步只要有同步在跑、刷新一下就能复现。
 *
 * 同步该由它自己的横幅与同步健康页负责——那是 `SyncProgressBanner` 的事。
 */
const newestImport = (jobs: readonly Job[]): Job | null =>
  jobs.find((job) => job.kind === "import") ?? null;

// PSU-2.22:decrypting 结束、第一批(最新消息)提交后,引导页就不该再挡着
// 用户——progress_current > 0 是"档案里已有本轮可浏览消息"的诚实信号。
// decrypting 期间(progress 仍为 0)放行是假可用,继续留在引导页。
const importReadyToBrowse = (job: Job): boolean =>
  job.kind === "import" && job.status === "running" && job.progress_current > 0;

// The follow loop is owned by the store, not by whichever view happened to
// start it: a newest-first import keeps running while the user browses, so
// the loop must survive route changes. Module scope keeps the controller out
// of Pinia state (it is not serializable and no view needs to see it).
let activeFollow: { jobId: string; controller: AbortController } | null = null;

const flowErrorFrom = (error: unknown): FlowError => {
  if (error instanceof ArchiveApiError) {
    return {
      code: error.code,
      message: error.message,
      retryable: error.retryable,
      recommendedAction: error.recommendedAction,
    };
  }
  return {
    code: "SERVICE_UNAVAILABLE",
    message: "本地归档服务暂时没有响应，请确认服务正在运行后重试。",
    retryable: true,
    recommendedAction: "retry",
  };
};

export const useOnboardingStore = defineStore("onboarding", {
  state: (): OnboardingState => ({
    step: "loading",
    accounts: [],
    probe: null,
    sources: [],
    selectedSourceId: null,
    defaultAccountId: localAccountChoices.readPinned(),
    currentJob: null,
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
      this.clearError();
      try {
        const bootstrap = await api.bootstrap();
        this.accounts = [...bootstrap.accounts];
        if (bootstrap.accounts.length === 0) {
          this.selectedSourceId = null;
          forgetAccountId();
          // 一个账号都没有了，钉着谁都没有意义——留着只会在下次导入后
          // 复活成一个跟新档案无关的选择。
          this.pinDefaultAccount(null);
          this.step = "welcome";
          return;
        }
        const activeId = resolveOpeningAccount({
          accounts: bootstrap.accounts.map(toIdentity),
          pinned: this.defaultAccountId,
          last: localAccountChoices.readLast(),
          serverHint: bootstrap.active_account_id,
        }).id;
        this.selectedSourceId = activeId;
        if (activeId === null) {
          this.step = "welcome";
          return;
        }
        rememberAccountId(activeId);
        this.sources = [];
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
        rememberAccountId(accountId);
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
        this.lastSequence = 0;
        this.flowError = null;
        rememberAccountId(accountId);
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
      this.selectedSourceId = sourceId;
    },
    confirmSource(): void {
      if (this.selectedSource === null) {
        throw new Error("请选择一个账号");
      }
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
      try {
        await api.acquireKey(selectedAccountId, method, userConsent);
        const selectedSource = this.selectedSource;
        if (selectedSource !== null) selectedSource.has_key = true;
        this.riskConfirmationOpen = false;
        this.step = "import";
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        this.step = "key";
      } finally {
        this.busy = false;
      }
    },
    async startImport(api: ArchiveApi, options: ImportOptions = {}): Promise<void> {
      const selectedAccountId = this.selectedSourceId;
      if (selectedAccountId === null) {
        throw new Error("请选择一个账号");
      }
      this.busy = true;
      this.clearError();
      try {
        const jobId = await api.createImport(selectedAccountId, options);
        this.currentJob = await api.getJob(jobId, selectedAccountId);
        this.lastSequence = 0;
        this.step = "progress";
      } catch (error: unknown) {
        this.flowError = flowErrorFrom(error);
        this.step = "import";
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
      if (job === null || monitoringStops.has(job.status)) return;
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
          rememberAccountId(this.selectedSourceId);
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
      this.lastSequence = 0;
      this.riskConfirmationOpen = false;
      this.clearError();
      this.clearAccountSwitchError();
    },
    cancelAdditionalArchive(): void {
      if (!this.addingArchive) return;
      this.selectedSourceId = this.additionalArchiveReturnAccountId;
      this.currentJob = this.additionalArchiveReturnJob;
      this.step = "overview";
      this.probe = null;
      this.sources = [];
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

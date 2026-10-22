import createClient from "openapi-fetch";

import type { components, operations, paths } from "./schema";

export type Bootstrap = components["schemas"]["Bootstrap"];
export type AccountSummary = components["schemas"]["AccountSummary"];
export type DeploymentCapabilities = components["schemas"]["DeploymentCapabilities"];
export type Deployment = DeploymentCapabilities["deployment"];
export type AuthState = DeploymentCapabilities["auth_state"];
export type Capability = components["schemas"]["Capability"];
export type CapabilityId = components["schemas"]["CapabilityId"];
export type RemoteShare = components["schemas"]["RemoteShare"];
export type AppCard = components["schemas"]["AppCard"];
export type AppCardItem = components["schemas"]["AppCardItem"];
export type PublicShare = components["schemas"]["PublicShare"];
export type ShareDraftRequest = components["schemas"]["ShareDraftRequest"];
export type ShareSnapshot = components["schemas"]["ShareSnapshot"];
export type ShareSnapshotMessage = components["schemas"]["ShareSnapshotMessage"];
export type ShareSnapshotAttachment = components["schemas"]["ShareSnapshotAttachment"];
export type CapabilityState = Capability["state"];
export type SourceProbe = components["schemas"]["SourceProbe"];
export type SourcePage = components["schemas"]["SourcePage"];
export type SourceSummary = components["schemas"]["SourceSummary"];
export type Job = components["schemas"]["Job"];
export type JobEventPayload = components["schemas"]["JobEventPayload"];
export type PublicError = components["schemas"]["Error"]["error"];
export type ConversationSummary = components["schemas"]["ConversationSummary"];
export type ConversationDetail = components["schemas"]["ConversationDetail"];
export type ConversationPage = components["schemas"]["ConversationPage"];
export type ConversationMember = components["schemas"]["ConversationMember"];
export type ConversationMemberPage = components["schemas"]["ConversationMemberPage"];
export type ContactSummary = components["schemas"]["ContactSummary"];
export type MessageSummary = components["schemas"]["MessageSummary"];
export type MessagePage = components["schemas"]["MessagePage"];
export type MessageWindow = components["schemas"]["MessageWindow"];
export type AttachmentSummary = components["schemas"]["AttachmentSummary"];
export type RemoteArchiveStatus = components["schemas"]["RemoteArchiveStatus"];
export type IssuedPairingCode = components["schemas"]["IssuedPairingCode"];
export type PairedDevice = components["schemas"]["PairedDevice"];
export type RemoteAccountsPage = components["schemas"]["RemoteAccountsPage"];
export type RemoteAccountSummary = components["schemas"]["RemoteAccountSummary"];
export type RemoteConversationSummary = components["schemas"]["RemoteConversationSummary"];
export type RemoteConversationPage = components["schemas"]["RemoteConversationPage"];
export type RemoteConversationMember = components["schemas"]["RemoteConversationMember"];
export type RemoteConversationMembersPage =
  components["schemas"]["RemoteConversationMembersPage"];
export type RemoteMessage = components["schemas"]["RemoteMessage"];
export type RemoteMessagePage = components["schemas"]["RemoteMessagePage"];
export type RemoteMessageWindow = components["schemas"]["RemoteMessageWindow"];
export type RemoteSearchPage = components["schemas"]["RemoteSearchPage"];
export type RemoteAttachmentUrl = components["schemas"]["RemoteAttachmentUrl"];
export type RemoteThumbnailUrl = components["schemas"]["RemoteThumbnailUrl"];
export type ObjectStorageConfig = components["schemas"]["ObjectStorageConfig"];
export type ObjectStorageGeneration = components["schemas"]["ObjectStorageGeneration"];
export type ObjectStorageConfigUpdate = components["schemas"]["ObjectStorageConfigUpdate"];
export type BrowserBackupPolicy = components["schemas"]["BrowserBackupPolicy"];
export type BrowserManagementConfirmation = components["schemas"]["BrowserManagementConfirmation"];
export type SharedBackupRequest = components["schemas"]["SharedBackupRequest"];
export type ObjectStorageVerification = components["schemas"]["ObjectStorageVerification"];
export type BackupConfig = components["schemas"]["BackupConfig"];
export type BackupConfigUpdate = components["schemas"]["BackupConfigUpdate"];
export type BackupVerification = components["schemas"]["BackupVerification"];
export type BackupRun = components["schemas"]["BackupRun"];
export type BackupRunList = components["schemas"]["BackupRunList"];
export type BackupDownload = components["schemas"]["BackupDownload"];
export type BrowserSession = components["schemas"]["BrowserSession"];
export type BrowserSessionSummary = components["schemas"]["BrowserSessionSummary"];
export type BrowserClientInfo = components["schemas"]["BrowserClientInfo"];
export type BrowserSessionPage = components["schemas"]["BrowserSessionPage"];
export type TotpEnrollment = components["schemas"]["TotpEnrollment"];
export type TotpEnabled = components["schemas"]["TotpEnabled"];
export type ReauthState = components["schemas"]["ReauthState"];
export type ReauthPurpose = components["schemas"]["ReauthChallengeRequest"]["purpose"];
export type WebAuthnChallenge = components["schemas"]["WebAuthnChallenge"];
export type PasskeySummary = components["schemas"]["PasskeySummary"];
export type PasskeyPage = components["schemas"]["PasskeyPage"];
/** 浏览器 `PublicKeyCredential.toJSON()` 的形态；由 `lib/webauthn` 产出。 */
export type WebAuthnCredentialJson = Record<string, unknown>;
export type RemoteDeviceSummary = components["schemas"]["RemoteDeviceSummary"];
export type RemoteDevicePage = components["schemas"]["RemoteDevicePage"];
export type FeedbackSubmission = components["schemas"]["FeedbackSubmission"];
export type FeedbackAttachment = components["schemas"]["FeedbackAttachment"];
export type FeedbackReceipt = components["schemas"]["FeedbackReceipt"];

export interface ConversationListOptions {
  q?: string;
  cursor?: string;
  limit?: number;
}

export interface MessageListOptions {
  cursor?: string;
  limit?: number;
  /**
   * 调用方发起的取消。快速切换会话时，上一条会话那次读取已经没人要了——
   * 没有这条线，取消只是把结果丢掉，字节照下，跨境链路上照等。
   */
  signal?: AbortSignal;
}

export interface RemoteListOptions {
  cursor?: string;
  limit?: number;
  /**
   * 只要这个账号（上传流）的行——记录模型 v3（PSU-2.35b）。
   * 缺省不过滤，行为与 v2 完全一致；只对协商到 v3 的远端使用，
   * 旧远端会答 QUERY_INVALID 而不是悄悄忽略。
   */
  sourceAccountId?: string;
  /**
   * 只保留匹配这段文字的会话——记录模型 v4（PSU-2.39）。匹配会话名称，以及
   * 名字还没送到时列表实际显示的那个不透明标识。与本机 `listConversations` 的
   * `q` 是同一个输入框的同一次输入，只是这一端匹配得更少（远程归档没有联系人
   * 备注与群成员名单）。只用于会话列表。
   */
  q?: string;
  /** 调用方发起的取消，与本机 `MessageListOptions.signal` 同义。 */
  signal?: AbortSignal;
}

export interface RemoteConversationOptions {
  /** 与列表同一个账号作用域；缺省不过滤。 */
  sourceAccountId?: string;
}

export interface RemoteSearchOptions {
  signal?: AbortSignal;
  limit?: number;
  sourceAccountId?: string;
  /**
   * 与本机 `SearchOptions` **逐个同名**（记录模型 v7，PSU-2.45）：同一个搜索
   * 表单、同一批查询参数，前端不为「这是哪一端」记第二套名字。
   *
   * 远端到不了 v7 时任何一个都会当场 QUERY_INVALID——**绝不悄悄不筛**。
   */
  conversationIds?: string[];
  senderIds?: string[];
  messageTypes?: MessageType[];
  hasAttachment?: boolean;
  dateFrom?: number;
  dateTo?: number;
  cursor?: string;
}

export interface MessageWindowOptions {
  before?: number;
  after?: number;
}

export interface RemoteMessageWindowOptions extends MessageWindowOptions {
  /** 远端会话的主键是三元组，定位也要指名是哪条上传流。 */
  sourceAccountId?: string;
}

export type SearchHit = components["schemas"]["SearchHit"];
export type SearchPage = components["schemas"]["SearchPage"];
export type MessageType = components["schemas"]["MessageType"];
export type ImportRequest = components["schemas"]["ImportRequest"];
export type ImportOptions = Partial<Omit<ImportRequest, "account_id">>;
export type CacheCategory = components["schemas"]["CacheCategory"];
export type CacheUsage = components["schemas"]["CacheUsage"];
export type CacheStatus = components["schemas"]["CacheStatus"];
export type CacheCleanupResult = components["schemas"]["CacheCleanupResult"];
export type AppSettings = components["schemas"]["AppSettings"];
export type TranscriptionCredentials =
  components["schemas"]["TranscriptionCredentials"];
/** 自动同步的开关、间隔与当前这一轮（PSU-2.133）。本机独有。 */
export type AutoSyncStatus = components["schemas"]["AutoSyncStatus"];
export type AutoSyncState = AutoSyncStatus["state"];
export type AutoSyncCycle = components["schemas"]["AutoSyncCycle"];
export type AutoSyncLastCycle = components["schemas"]["AutoSyncLastCycle"];
export type SearchIndexState = components["schemas"]["SearchIndexState"];
export type StorageReclaimPreview = components["schemas"]["StorageReclaimPreview"];
/**
 * 一份搜索结果能不能当真。四个取值各有各的用户动作，别合并：
 * `ready` 完整；`building` 正在建、这一份可能不全；`unavailable` 索引结构坏了，
 * 要人来修；`not_initialized` 这个归档从来没建过索引，点一下就能建。
 */
export type SearchIndexStatus = components["schemas"]["SearchPage"]["index_status"];
export type UpdateStatus = components["schemas"]["UpdateStatus"];
// 界面向后端「投递」一次检查的理由（PSU-2.222）。manual 以外都立即返回、不等网络。
export type UpdateCheckTrigger = NonNullable<components["schemas"]["UpdateCheckRequest"]["trigger"]>;
export type SyncHealth = components["schemas"]["SyncHealth"];
/** 在微信中清理（PSU-2.186～2.189），仅本机。 */
export type WechatCleanupReadiness = components["schemas"]["WechatCleanupReadiness"];
export type WechatCleanupDrive = components["schemas"]["WechatCleanupDrive"];
export type WechatCleanupObserved = components["schemas"]["WechatCleanupObserved"];
export type WechatCleanupKeepMonths = WechatCleanupReadiness["keep_months"];
export type ManualSyncResult = components["schemas"]["ManualSyncResult"];
export type StorageArea = components["schemas"]["StorageArea"];
export type StorageOverview = components["schemas"]["StorageOverview"];
export type StorageUnloadResult = components["schemas"]["StorageUnloadResult"];
export type ExportScope = components["schemas"]["ExportScope"];
export type ExportEstimate = components["schemas"]["ExportEstimate"];
export type ExportCreated = components["schemas"]["ExportCreated"];
export type ExportReport = components["schemas"]["ExportReport"];
export type ExportSummary = components["schemas"]["Export"];
export type ExportDeletion = components["schemas"]["ExportDeletion"];
export type ExportFormat = ExportSummary["format"];

export type SearchSort = NonNullable<
  NonNullable<operations["searchMessages"]["parameters"]["query"]>["sort"]
>;
export type FileClass = NonNullable<
  NonNullable<operations["searchMessages"]["parameters"]["query"]>["file_class"]
>[number];
export type ContactDirectoryPage = components["schemas"]["ContactDirectoryPage"];
export type ContactDirectoryEntry = components["schemas"]["ContactDirectoryEntry"];

export interface SearchOptions {
  signal?: AbortSignal;
  /**
   * 一个维度可以给多个值（PSU-2.74）：**维度之间是 AND、维度之内是 OR**。
   * 会话与发送人各最多 20 个，超过后端当场 QUERY_INVALID——不悄悄截断。
   */
  conversationIds?: string[];
  senderIds?: string[];
  messageTypes?: MessageType[];
  hasAttachment?: boolean;
  dateFrom?: number;
  dateTo?: number;
  cursor?: string;
  limit?: number;
  sort?: SearchSort;
  /** 「文件」下的第二层；必须与 `messageTypes: ["file"]` 同时给，否则后端拒。 */
  fileClasses?: FileClass[];
}

/** 会话内查找：会话已经在路径里，而这一端点的发送人仍是单值。 */
export interface ConversationSearchOptions {
  signal?: AbortSignal;
  senderId?: string;
  messageTypes?: MessageType[];
  hasAttachment?: boolean;
  dateFrom?: number;
  dateTo?: number;
  cursor?: string;
  limit?: number;
}

export interface ContactDirectoryOptions {
  q?: string;
  cursor?: string;
  limit?: number;
}

export type DailyStat = components["schemas"]["DailyStat"];
export type AnalysisPage = components["schemas"]["AnalysisPage"];
export interface AnalysisOptions {
  dateFrom: string;
  dateTo: string;
  conversationKind?: "all" | "private" | "group";
  conversationId?: string | undefined;
}
export type DailyStatsPage = components["schemas"]["DailyStatsPage"];
export type ConversationStat = components["schemas"]["ConversationStat"];
export type ConversationStatsPage = components["schemas"]["ConversationStatsPage"];

export interface DailyStatsOptions {
  dateFrom?: number;
  dateTo?: number;
}

export interface MessageDaysOptions {
  dateFrom?: number;
  dateTo?: number;
  /** 日界所用的时区偏移，分钟，东为正；默认跟随浏览器所在时区。 */
  utcOffsetMinutes?: number;
}

export interface JobEvent {
  sequence: number;
  payload: JobEventPayload;
}

export interface JobEventOptions {
  lastSequence?: number;
  waitMs?: number;
  signal?: AbortSignal;
}

export interface ArchiveApi {
  bootstrap(): Promise<Bootstrap>;
  getCapabilities(): Promise<DeploymentCapabilities>;
  openRemoteWeb(): Promise<operations["openRemoteWeb"]["responses"][200]["content"]["application/json"]>;
  getRemoteArchiveStatus(): Promise<RemoteArchiveStatus>;
  listRemoteArchiveAccounts(): Promise<RemoteAccountsPage>;
  listRemoteConversations(options?: RemoteListOptions): Promise<RemoteConversationPage>;
  /**
   * 一条远程会话的身份（记录模型 v6，PSU-2.44）。
   *
   * 存在的理由是**不必先在列表里把它翻出来**：靠列表取名会让标题取决于读者
   * 恰好加载过哪几页，一改过滤词就是另一份缓存，正在读的那条会话当场退化成
   * 不透明标识。远端到不了 v6 答 QUERY_INVALID，归档里没有这条会话答 NOT_FOUND
   * ——两种都是确定性的答案，调用方据此退级取名而不是报错。
   */
  getRemoteConversation(
    conversationId: string,
    options?: RemoteConversationOptions,
  ): Promise<RemoteConversationSummary>;
  /**
   * 一页群成员（记录模型 v8，PSU-2.47）。
   *
   * 远端到不了 v8 时答 QUERY_INVALID，**而不是空名单**：空名单是一句断言
   * （「这个群没有别人」），而实情是「这一端还没有名单这回事」。调用方据此
   * 把抽屉整个关掉并说明原因，而不是画一个「0 人」的群。
   */
  listRemoteConversationMembers(
    conversationId: string,
    options?: RemoteListOptions,
  ): Promise<RemoteConversationMembersPage>;
  listRemoteConversationMessages(
    conversationId: string,
    options?: RemoteListOptions,
  ): Promise<RemoteMessagePage>;
  /**
   * 定位到一条远程记录的上下文（记录模型 v9）。形状与本机 `getMessageWindow`
   * 相同，所以两端共用同一个组合式；远端够不到 v9 时抛
   * `REMOTE_RECORD_MODEL_TOO_OLD`，调用方据此退回「进入视野时才高亮」。
   */
  getRemoteMessageWindow(
    conversationId: string,
    anchorMessageId: string,
    options?: RemoteMessageWindowOptions,
  ): Promise<RemoteMessageWindow>;
  searchRemoteArchive(
    q: string | null,
    options?: RemoteSearchOptions,
  ): Promise<RemoteSearchPage>;
  getRemoteAttachmentUrl(blobId: string): Promise<RemoteAttachmentUrl>;
  /**
   * 入参是**主工件**的标识（`attachment_refs` 里那一个）。缩略图自己的标识由
   * 服务端派生,不走线——所以记录格式一个字节都不用改。
   *
   * 没有缩略图时答 404 `THUMBNAIL_UNAVAILABLE`,那是常态之一而不是故障。
   */
  getRemoteAttachmentThumbnailUrl(blobId: string): Promise<RemoteThumbnailUrl>;
  completeBrowserSetup(bootstrapToken: string, password: string): Promise<BrowserSession>;
  browserSignIn(password: string, secondFactor?: string): Promise<BrowserSession>;
  browserSignOut(): Promise<void>;
  beginTotpEnrollment(password: string): Promise<TotpEnrollment>;
  enableTotp(code: string): Promise<TotpEnabled>;
  disableTotp(password: string): Promise<void>;
  listBrowserSessions(): Promise<BrowserSessionPage>;
  recordBrowserClientInfo(info: BrowserClientInfo): Promise<void>;
  revokeBrowserSession(sessionId: string): Promise<void>;
  revokeAllBrowserSessions(): Promise<number>;
  /** PSU-2.221 本机应用锁：锁定时也可调用的状态查询（只说本机这台设备）。 */
  getReauthState(): Promise<ReauthState>;
  createReauthChallenge(purpose: ReauthPurpose): Promise<WebAuthnChallenge>;
  verifyReauth(challengeId: string, credential: WebAuthnCredentialJson): Promise<ReauthState>;
  lockReauth(): Promise<ReauthState>;
  /** 指纹验证不了时用登录口令解锁本机；也打开一个短的管理新鲜窗口。 */
  unlockReauthWithPassword(password: string): Promise<ReauthState>;
  /** 关掉「旧版应用锁已关闭」的一次性提示。 */
  dismissReauthNotice(): Promise<void>;
  putReauthPolicy(enabled: boolean, policyVersion: number): Promise<ReauthState>;
  listPasskeys(): Promise<PasskeyPage>;
  beginPasskeyRegistration(password: string): Promise<WebAuthnChallenge>;
  finishPasskeyRegistration(
    challengeId: string,
    credential: WebAuthnCredentialJson,
    label?: string | null,
  ): Promise<PasskeySummary>;
  deletePasskey(passkeyId: string): Promise<void>;
  listRemoteDevices(): Promise<RemoteDevicePage>;
  revokeRemoteSyncDevice(deviceId: string): Promise<void>;
  getObjectStorageConfig(): Promise<ObjectStorageConfig>;
  confirmBrowserManagement(password: string, secondFactor?: string): Promise<BrowserManagementConfirmation>;
  useAttachmentStorageForBackup(input: SharedBackupRequest): Promise<BackupConfig>;
  disableBrowserBackup(): Promise<BackupConfig>;
  updateBrowserBackupPolicy(input: BrowserBackupPolicy): Promise<BackupConfig>;
  testObjectStorageConfig(
    input: ObjectStorageConfigUpdate,
  ): Promise<ObjectStorageVerification>;
  updateObjectStorageConfig(
    input: ObjectStorageConfigUpdate,
  ): Promise<ObjectStorageConfig>;
  restoreObjectStorageGeneration(storageGeneration: number): Promise<ObjectStorageConfig>;
  getBackupConfig(): Promise<BackupConfig>;
  testBackupConfig(input: BackupConfigUpdate): Promise<BackupVerification>;
  updateBackupConfig(input: BackupConfigUpdate): Promise<BackupConfig>;
  listBackupRuns(): Promise<BackupRunList>;
  requestBackupRun(): Promise<BackupRunList>;
  getLatestBackup(): Promise<BackupDownload>;
  /** 在线分享（PSU-2.181/2.182）。两端同路径：本机经已配对的远端，远程网页走自己的 BFF。 */
  createShare(input: ShareDraftRequest): Promise<RemoteShare>;
  uploadShareObject(token: string, attachmentId: string, content: Blob): Promise<void>;
  finalizeShare(token: string): Promise<RemoteShare>;
  listShares(): Promise<RemoteShare[]>;
  extendShare(token: string): Promise<RemoteShare>;
  deleteShare(token: string): Promise<void>;
  /** 对方打开链接时那一次读（PSU-2.183）。不需要任何身份；失效答 SHARE_GONE。 */
  getPublicShare(token: string, options?: { countView?: boolean }): Promise<PublicShare>;
  probeSources(): Promise<SourceProbe>;
  listSources(): Promise<SourcePage>;
  acquireKey(sourceId: string, method: string, userConsent: boolean): Promise<void>;
  deleteKey(sourceId: string): Promise<void>;
  createImport(accountId: string, options?: ImportOptions): Promise<string>;
  listJobs(accountId: string): Promise<Job[]>;
  getJob(jobId: string, accountId: string): Promise<Job>;
  pauseJob(jobId: string, accountId: string): Promise<Job>;
  resumeJob(jobId: string, accountId: string): Promise<Job>;
  cancelJob(jobId: string, accountId: string): Promise<Job>;
  readJobEvents(
    jobId: string,
    accountId: string,
    onEvent: (event: JobEvent) => void,
    options?: JobEventOptions,
  ): Promise<number>;
  listConversations(
    accountId: string,
    options?: ConversationListOptions,
  ): Promise<ConversationPage>;
  getConversation(conversationId: string, accountId: string): Promise<ConversationDetail>;
  listConversationMembers(
    conversationId: string,
    accountId: string,
    options?: MessageListOptions,
  ): Promise<ConversationMemberPage>;
  getContact(contactId: string, accountId: string): Promise<ContactSummary>;
  listConversationMessages(
    conversationId: string,
    accountId: string,
    options?: MessageListOptions,
  ): Promise<MessagePage>;
  getMessageWindow(
    conversationId: string,
    accountId: string,
    anchorMessageId: string,
    options?: MessageWindowOptions,
  ): Promise<MessageWindow>;
  searchInConversation(
    conversationId: string,
    accountId: string,
    q: string,
    options?: ConversationSearchOptions,
  ): Promise<SearchPage>;
  getConversationMessageDays(
    conversationId: string,
    accountId: string,
    options?: MessageDaysOptions,
  ): Promise<string[]>;
  retryAttachment(attachmentId: string, accountId: string): Promise<AttachmentSummary>;
  /**
   * `audioFormat: "mp3"` 只对语音有效（PSU-2.178，本机端才有）：把解好的语音现转成
   * mp3，给压缩包导出用。对别的类型后端答 QUERY_INVALID。
   */
  getAttachmentContent(
    attachmentId: string,
    accountId: string,
    options?: { audioFormat?: "mp3" },
  ): Promise<Blob>;
  /**
   * 微信自带的缩略图字节（KB 量级）。
   *
   * 没有缩略图时后端答 404 `THUMBNAIL_UNAVAILABLE`——那是常态之一而不是故障,
   * 调用方应静默回退 `getAttachmentContent`。
   */
  getAttachmentThumbnail(attachmentId: string, accountId: string): Promise<Blob>;
  searchMessages(accountId: string, q: string, options?: SearchOptions): Promise<SearchPage>;
  /**
   * 发送人名录（PSU-2.66）：全局搜索的「发送人」维度点开的就是它。
   *
   * 名字一直在归档的 `contacts` 里，只是从来没有端点把它列出来，于是那个筛选框
   * 只能手输 uid。不含命中数——那要对全账号消息做 GROUP BY，实测 21.8 秒。
   */
  listContacts(
    accountId: string,
    options?: ContactDirectoryOptions,
  ): Promise<ContactDirectoryPage>;
  estimateExport(scope: ExportScope): Promise<ExportEstimate>;
  createExport(scope: ExportScope, format: ExportFormat): Promise<ExportCreated>;
  listExports(accountId: string): Promise<ExportSummary[]>;
  getExport(exportId: string, accountId: string): Promise<ExportSummary>;
  deleteExport(exportId: string, accountId: string): Promise<ExportDeletion>;
  previewExport(scope: ExportScope): Promise<MessagePage>;
  getExportReport(exportId: string, accountId: string): Promise<ExportReport>;
  downloadExport(exportId: string, accountId: string): Promise<Blob>;
  openExportFolder(exportId: string, accountId: string): Promise<void>;
  getDailyStats(accountId: string, options?: DailyStatsOptions): Promise<DailyStatsPage>;
  getAnalysis(accountId: string, options: AnalysisOptions): Promise<AnalysisPage>;
  getConversationStats(accountId: string, limit?: number): Promise<ConversationStatsPage>;
  getCacheStatus(accountId: string): Promise<CacheStatus>;
  previewCacheCleanup(accountId: string, category: CacheCategory): Promise<CacheUsage>;
  cleanupCache(accountId: string, category: CacheCategory): Promise<CacheCleanupResult>;
  /** 这个归档的检索索引处在哪一步，以及开启它要付多少代价（PSU-2.88）。 */
  getSearchIndexState(accountId: string): Promise<SearchIndexState>;
  /** 开启全文检索并开始建索引；建索引在后台分片进行，这里立即返回。 */
  initializeSearchIndex(accountId: string): Promise<SearchIndexState>;
  /** 关掉全文检索并删除索引结构。不删任何消息。 */
  deleteSearchIndex(accountId: string): Promise<SearchIndexState>;
  getAppSettings(): Promise<AppSettings>;
  /**
   * 「程序挂着就自己同步」的当前状态（PSU-2.133）。
   *
   * 与 `getAppSettings` 分开是因为它不只是设置：`state`、`next_run_at`、
   * `current_cycle` 每一轮都在动，而设置那份文档是静的。
   */
  getAutoSync(): Promise<AutoSyncStatus>;
  updateAutoSync(input: {
    enabled: boolean;
    intervalMinutes: number;
    includeOriginals: boolean;
  }): Promise<AutoSyncStatus>;
  updateAppSettings(
    mediaCacheQuotaBytes: number,
    autoUpdateEnabled: boolean,
    stickerFetchEnabled: boolean,
  ): Promise<AppSettings>;
  /**
   * 转写那两个开关走自己的资源，和自动同步一个道理：`updateAppSettings`
   * 是整体替换、按位置传三个值，把转写塞进去就意味着改一次主题设置
   * 可能顺手把「把录音发出去」打开。
   */
  updateTranscriptionSettings(input: {
    enabled: boolean;
    autoNew: boolean;
  }): Promise<AppSettings>;
  getTranscriptionCredentials(): Promise<TranscriptionCredentials>;
  /**
   * 先验后存，服务端一个动作。验不过时抛 `ArchiveApiError`，`code` 是
   * `TRANSCRIPTION_CREDENTIALS_REJECTED`，`message` 是厂商的稳定代号——
   * 界面靠那个代号决定说哪一句，而不是把厂商原文贴到屏幕上。
   */
  putTranscriptionCredentials(input: {
    secretId: string;
    secretKey: string;
  }): Promise<TranscriptionCredentials>;
  deleteTranscriptionCredentials(): Promise<TranscriptionCredentials>;
  getUpdateStatus(): Promise<UpdateStatus>;
  checkForUpdates(trigger?: UpdateCheckTrigger): Promise<UpdateStatus>;
  postponeUpdateReminder(version: string, until?: number): Promise<UpdateStatus>;
  applyUpdate(version: string): Promise<UpdateStatus>;
  /** 已经下载验证好的那一版，现在退出去装（PSU-2.173）。 */
  installUpdateNow(version: string): Promise<UpdateStatus>;
  rollbackUpdate(): Promise<UpdateStatus>;
  getSyncHealth(accountId: string): Promise<SyncHealth>;
  runSync(accountId: string): Promise<ManualSyncResult>;
  /**
   * 一个按钮跑完四段上传：正文 → 缩略图 → 头像 → 原图（PSU-2.70）。
   *
   * 与 `runSync` 并存而不是取代它：后者是「只把正文推上去」的最短路径。
   */
  createSyncJob(input: {
    accountId: string;
    includeOriginals: boolean;
    /** 用户看过「远端还剩多少」之后仍然选择继续（PSU-2.104）。 */
    allowLowSpace?: boolean;
  }): Promise<{ job_id: string }>;
  /** 这个会话从最早到哪天已全部在远端、建议清到哪天（PSU-2.186）。仅本机。 */
  getWechatCleanupReadiness(
    conversationId: string,
    accountId: string,
    keepMonths: WechatCleanupKeepMonths,
  ): Promise<WechatCleanupReadiness>;
  /**
   * 清理前的「先同步」（PSU-2.190）：正文按账号，原图只传这个会话要清的那一段。
   * 进度与普通同步作业一样用 `getJob` 取。
   */
  startWechatCleanupSync(
    conversationId: string,
    accountId: string,
    keepMonths: WechatCleanupKeepMonths,
  ): Promise<{ job_id: string }>;
  /** 替用户走到微信清理页、搜到会话并勾上（PSU-2.187）。止于勾选。 */
  startWechatCleanupDrive(
    conversationId: string,
    accountId: string,
    keepMonths: WechatCleanupKeepMonths,
  ): Promise<WechatCleanupDrive>;
  getWechatCleanupDrive(): Promise<WechatCleanupDrive>;
  /** 用户在微信里实际选的时间范围（PSU-2.188，浮窗的第二道校验）。 */
  getWechatCleanupObserved(sessionId?: string): Promise<WechatCleanupObserved>;
  closeWechatCleanupHint(sessionId?: string): Promise<void>;
  cancelWechatCleanupDrive(sessionId?: string): Promise<void>;
  /** 已接入的机器给下一台签发一次性配对码（PSU-2.68）。 */
  issuePairingCode(): Promise<IssuedPairingCode>;
  /** 未接入的机器兑换配对码；令牌进本机受保护存储，不回到这里。 */
  redeemPairingCode(input: {
    baseUrl: string;
    pairingCode: string;
    deviceName: string;
  }): Promise<PairedDevice>;
  getStorageOverview(accountId: string): Promise<StorageOverview>;
  unloadMediaCache(accountId: string): Promise<StorageUnloadResult>;
  /**
   * 把一条用户反馈交给外部反馈平台。**只有本地端有这条**。
   *
   * 不带 `project_id`：项目标识是那台机器的配置，由后端注入。走后端而不是从页面
   * 直连，是因为打包形态 SPA 的 CSP 是 `connect-src 'self'`，而且本仓外网请求
   * 一律由后端出。远程端不认领 `feedback` 能力，入口在那一端不出现。
   *
   * 被上游拒收时抛的 `ArchiveApiError` 带 `code === "FEEDBACK_UPSTREAM_REJECTED"`
   * 与 `rejectedPatterns`（命中的隐私模式**名字**），调用方据此指出该改哪里。
   */
  submitFeedback(input: FeedbackSubmission): Promise<FeedbackReceipt>;
  /**
   * 给**不经过本封装**的同源请求用的那个 `fetch`：反馈 widget 自己发的配置取数
   * （`configUrl`）与回执页补写联系方式（`contactUrl`）。
   *
   * ## 为什么要有它
   *
   * 本机后端配了 `--api-token` 时，每一条 `/api/` 请求都要带 `X-Archive-Token`，
   * 否则 401（`http_api.py` 的 `verify_api_token`）。本封装里的每一个方法都带，
   * 而 widget 的那两条请求是它**自己**用 `fetch` 发的——不经过这里就不带，于是：
   *
   * - 配置取数静默 401 → widget 退回内置保守值 → 页面地址里的 `wxid_…` 不再被
   *   项目自己的 `rejectPatterns` 洗掉 → 提交被平台 100% 拒收（2026-09-22 那条
   *   P0 无症状复活）；
   * - 补写联系方式恒 401 → 回执页上一颗永远存不上的「保存」。
   *
   * 所以令牌只能从**这里**交出去（它只活在本封装的闭包里，不写进任何存储），
   * widget 那边以 `fetchImpl` 接上。
   *
   * ## 只对同源加
   *
   * 解析后的地址与页面不同源时**原样放行、不加令牌**：哪天有人把 `configUrl`
   * 指到平台域名上，这个本机令牌不能跟着出网。
   *
   * 与本封装其余方法不同，它**不翻译错误**：网络层失败仍是那个 `TypeError`
   * （widget 靠它认「离线」），HTTP 错误仍是一个普通的 `Response`（widget 要读
   * 平台的 `{error}` 认出隐私拒收）。401 也**不**触发 `onUnauthenticated`——
   * 补写那条的 401 是平台说「这把 capability 不对」，不是本机会话过期。
   *
   * 可选只因为测试里的替身一大堆；真实现（`createArchiveApi`）一定给。
   */
  sameOriginFetch?: typeof globalThis.fetch;
  /** 回收前的账：能删多少、能腾多少、够不够跑 VACUUM（PSU-2.88）。 */
  previewStorageReclaim(accountId: string): Promise<StorageReclaimPreview>;
  /** 执行回收。分钟级，所以返回一个可轮询的作业。 */
  startStorageReclaim(accountId: string): Promise<{ job_id: string }>;
}

export class ArchiveApiError extends Error {
  readonly code: string;
  readonly retryable: boolean;
  readonly recommendedAction: PublicError["recommended_action"];
  readonly jobId: string | null;
  /**
   * 还要等多少秒才能再试，来自标准 `Retry-After` 头。
   *
   * 走 HTTP 头而不是错误体：`Error` 是所有端点共用的形状，为登录退避一处给它
   * 加字段，会让每一条错误响应都多带一个恒为 null 的键。
   */
  readonly retryAfterSeconds: number | null;

  /**
   * 反馈被上游拒收时，命中的隐私模式**名字**（`wxid`、`phone`）；其余情况恒为空。
   *
   * 与 `retryAfterSeconds` 同一个道理：它来自错误体里与 `error` **并列**的那一段
   * （契约的 `FeedbackRejection`），而不是共用信封里的字段——`Error` 是所有端点
   * 共用的形状，为一条端点给它加字段会让每一条错误响应都多带一个恒空的键。
   *
   * 是模式的名字，不是命中的那段文字：后端按字符集白名单过一遍才透传，所以
   * 这里拿到的东西不含用户写的任何内容。
   */
  readonly rejectedPatterns: readonly string[];

  constructor(
    error: PublicError,
    retryAfterSeconds: number | null = null,
    rejectedPatterns: readonly string[] = [],
  ) {
    super(error.message);
    this.name = "ArchiveApiError";
    this.code = error.code;
    this.retryable = error.retryable;
    this.retryAfterSeconds = retryAfterSeconds;
    this.recommendedAction = error.recommended_action ?? null;
    this.jobId = error.job_id ?? null;
    this.rejectedPatterns = rejectedPatterns;
  }

  toJSON(): object {
    return {
      name: this.name,
      code: this.code,
      message: this.message,
      retryable: this.retryable,
      recommendedAction: this.recommendedAction,
      jobId: this.jobId,
    };
  }
}

interface ArchiveApiOptions {
  baseUrl?: string;
  apiToken?: string;
  fetch?: typeof globalThis.fetch;
  /**
   * 远程端会话失效时的唯一拦截点。
   *
   * 会话可能在任何一次请求上过期，而不只是在导航时。没有这个钩子，用户会停在
   * 一个所有面板都写着「加载失败」的界面上，看不出真因是「该重新登录了」。
   */
  onUnauthenticated?: () => void;
  /**
   * 日常解锁（PSU-2.213）开启后，会话仍在但私人接口答 423 `REMOTE_REAUTH_REQUIRED`。
   * 与「该重新登录」是两回事：这里要去锁定页，而不是登录页。
   */
  onReauthRequired?: () => void;
}

/**
 * 压根没拿到响应时的兜底。
 *
 * 文案里**不能出现「本地」**：同一份前端也跑在远程端（手机浏览器），那时归档
 * 服务在另一台机器上，「本地归档服务连不上」既指错了对象，也把用户支到一个
 * 他此刻查不了的地方。两种部署都成立的说法只有这一句——而这一层拿不到
 * `deployment`（能力清单自己就可能是失败的那一次请求）。
 */
const fallbackError = (): PublicError => ({
  code: "SERVICE_UNAVAILABLE",
  message: "暂时无法连接归档服务，请检查网络或确认服务正在运行后重试。",
  retryable: true,
  recommended_action: "retry",
  job_id: null,
  details_ref: null,
});

const isRecord = (value: unknown): value is Record<string, unknown> =>
  typeof value === "object" && value !== null;

/** 这个请求地址按页面解析之后与页面同源吗。解析不了一律按「不同源」处理。 */
const isSameOrigin = (input: RequestInfo | URL): boolean => {
  const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
  try {
    return new URL(raw, location.href).origin === location.origin;
  } catch {
    return false;
  }
};

const isAbortError = (value: unknown): boolean =>
  isRecord(value) && value.name === "AbortError";

const publicErrorFrom = (value: unknown): PublicError => {
  if (!isRecord(value) || !isRecord(value.error)) {
    return fallbackError();
  }
  const error = value.error;
  if (
    typeof error.code !== "string" ||
    typeof error.message !== "string" ||
    typeof error.retryable !== "boolean"
  ) {
    return fallbackError();
  }
  const action = error.recommended_action;
  const recommendedAction =
    action === "close_wechat" ||
    action === "reselect_source" ||
    action === "retry" ||
    action === "check_disk_space"
      ? action
      : null;
  return {
    code: error.code,
    message: error.message,
    retryable: error.retryable,
    recommended_action: recommendedAction,
    job_id: typeof error.job_id === "string" ? error.job_id : null,
    details_ref: null,
  };
};

/**
 * 错误体里与 `error` 并列的那段拒收理由，没有就是空数组。
 *
 * 放在这一层而不是塞进 `submitFeedback` 自己：`unwrapWith` 本来就拿得到整个
 * 响应体，多一个端点要透传别的并列字段时也是同一处。形状不对一律当没有——
 * 这是从网络上来的东西，不是本地类型能保证的。
 */
const rejectedPatternsFrom = (value: unknown): readonly string[] => {
  if (!isRecord(value) || !isRecord(value.upstream)) return [];
  const patterns = value.upstream.patterns;
  if (!Array.isArray(patterns)) return [];
  return patterns.filter((item): item is string => typeof item === "string");
};

const retryAfterFrom = (response: Response): number | null => {
  const raw = response.headers.get("Retry-After");
  if (raw === null) return null;
  const seconds = Number.parseInt(raw, 10);
  return Number.isFinite(seconds) && seconds > 0 ? seconds : null;
};

const unwrapWith =
  (onUnauthenticated?: () => void, onReauthRequired?: () => void) =>
  <T>(result: { data?: T; error?: unknown; response: Response }): T => {
    if (result.data !== undefined) {
      return result.data;
    }
    const error = new ArchiveApiError(
      publicErrorFrom(result.error),
      retryAfterFrom(result.response),
      rejectedPatternsFrom(result.error),
    );
    if (error.code === "REMOTE_SIGN_IN_REQUIRED") onUnauthenticated?.();
    if (error.code === "REMOTE_REAUTH_REQUIRED") onReauthRequired?.();
    throw error;
  };

const jobStatuses: ReadonlySet<Job["status"]> = new Set([
  "queued",
  "running",
  "pausing",
  "paused",
  "interrupted",
  "completed",
  "failed",
  "cancelled",
]);

const isNullableString = (value: unknown): value is string | null =>
  value === null || typeof value === "string";

const isJobEventPayload = (value: unknown): value is JobEventPayload => {
  if (!isRecord(value)) {
    return false;
  }
  return (
    typeof value.job_id === "string" &&
    typeof value.event_type === "string" &&
    typeof value.status === "string" &&
    jobStatuses.has(value.status as Job["status"]) &&
    isNullableString(value.stage) &&
    typeof value.progress_current === "number" &&
    (value.progress_total === null || typeof value.progress_total === "number") &&
    typeof value.stage_progress_current === "number" &&
    (value.stage_progress_total === null || typeof value.stage_progress_total === "number") &&
    (value.stage_progress_unit === null ||
      value.stage_progress_unit === "bytes" ||
      value.stage_progress_unit === "items") &&
    (value.stage_started_at === null || typeof value.stage_started_at === "number") &&
    isNullableString(value.error_code) &&
    typeof value.created_at === "number"
  );
};

const parseFrame = (frame: string): JobEvent | null => {
  let sequence: number | null = null;
  let eventType = "message";
  const dataLines: string[] = [];
  for (const rawLine of frame.split("\n")) {
    const line = rawLine.endsWith("\r") ? rawLine.slice(0, -1) : rawLine;
    if (line.startsWith("id:")) {
      const parsed = Number.parseInt(line.slice(3).trim(), 10);
      sequence = Number.isSafeInteger(parsed) && parsed >= 0 ? parsed : null;
    } else if (line.startsWith("event:")) {
      eventType = line.slice(6).trim();
    } else if (line.startsWith("data:")) {
      dataLines.push(line.slice(5).trimStart());
    }
  }
  if (eventType !== "job" || sequence === null || dataLines.length === 0) {
    return null;
  }
  const payload: unknown = JSON.parse(dataLines.join("\n"));
  if (!isJobEventPayload(payload)) {
    throw new ArchiveApiError({
      code: "QUERY_INVALID",
      message: "任务进度格式无效，请重新获取任务状态。",
      retryable: true,
      recommended_action: "retry",
      job_id: null,
      details_ref: null,
    });
  }
  return { sequence, payload };
};

export const createArchiveApi = (options: ArchiveApiOptions = {}): ArchiveApi => {
  const unwrap = unwrapWith(options.onUnauthenticated, options.onReauthRequired);
  const apiToken = options.apiToken;
  const requestHeaders = (): HeadersInit =>
    apiToken ? { "X-Archive-Token": apiToken } : {};
  const rawFetch = options.fetch ?? globalThis.fetch;
  const sameOriginFetch: typeof globalThis.fetch = (input, init) => {
    if (!apiToken || !isSameOrigin(input)) return rawFetch(input, init);
    // `Headers` 合并而不是整个替换：widget 自己的 `accept` / `content-type` /
    // `authorization`（补写那条的 owner capability）都得原样留着。
    const headers = new Headers(
      init?.headers ?? (input instanceof Request ? input.headers : undefined),
    );
    headers.set("X-Archive-Token", apiToken);
    return rawFetch(input, { ...init, headers });
  };
  // fetch 只在压根没拿到响应时 reject：本地服务没起来、开发代理断连。
  // 不在这里翻译成 ArchiveApiError，视图拿到的就是一个裸 TypeError，
  // 于是各自套用「XX 加载失败」的兜底文案，把「服务连不上」说成「搜索坏了」。
  const fetchImpl: typeof globalThis.fetch = async (input, init) => {
    try {
      return await rawFetch(input, init);
    } catch (error: unknown) {
      // 取消是调用方自己发起的，不是服务出问题，得原样抛回去。
      if (isAbortError(error)) throw error;
      throw new ArchiveApiError(fallbackError());
    }
  };
  const client = createClient<paths>({
    baseUrl: options.baseUrl ?? "",
    fetch: fetchImpl,
  });

  const getJob = async (jobId: string, accountId: string): Promise<Job> =>
    unwrap(
      await client.GET("/api/v1/jobs/{job_id}", {
        params: { path: { job_id: jobId }, query: { account_id: accountId } },
        headers: requestHeaders(),
      }),
    );

  const controlJob = async (
    action: "pause" | "resume" | "cancel",
    jobId: string,
    accountId: string,
  ): Promise<Job> => {
    const params = { path: { job_id: jobId }, query: { account_id: accountId } };
    if (action === "pause") {
      return unwrap(
        await client.POST("/api/v1/jobs/{job_id}/pause", {
          params,
          headers: requestHeaders(),
        }),
      );
    }
    if (action === "resume") {
      return unwrap(
        await client.POST("/api/v1/jobs/{job_id}/resume", {
          params,
          headers: requestHeaders(),
        }),
      );
    }
    return unwrap(
      await client.POST("/api/v1/jobs/{job_id}/cancel", {
        params,
        headers: requestHeaders(),
      }),
    );
  };

  return {
    async bootstrap() {
      return unwrap(
        await client.GET("/api/v1/bootstrap", { headers: requestHeaders() }),
      );
    },
    async getCapabilities() {
      return unwrap(
        await client.GET("/api/v1/capabilities", { headers: requestHeaders() }),
      );
    },
    async openRemoteWeb() {
      return unwrap(await client.POST("/api/v1/remote/web/open", { headers: requestHeaders() }));
    },
    async getRemoteArchiveStatus() {
      return unwrap(
        await client.GET("/api/v1/remote/status", { headers: requestHeaders() }),
      );
    },
    async listRemoteArchiveAccounts() {
      return unwrap(
        await client.GET("/api/v1/remote/accounts", { headers: requestHeaders() }),
      );
    },
    async listRemoteConversations(options = {}) {
      const query: {
        cursor?: string;
        limit?: number;
        source_account_id?: string;
        q?: string;
      } = {};
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      if (options.q !== undefined && options.q !== "") query.q = options.q;
      return unwrap(
        await client.GET("/api/v1/remote/conversations", {
          params: { query },
          headers: requestHeaders(),
        }),
      );
    },
    async getRemoteConversation(conversationId, options = {}) {
      const query: { source_account_id?: string } = {};
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      return unwrap(
        await client.GET("/api/v1/remote/conversations/{conversation_id}", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
    },
    async listRemoteConversationMembers(conversationId, options = {}) {
      const query: { cursor?: string; limit?: number; source_account_id?: string } = {};
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      return unwrap(
        await client.GET("/api/v1/remote/conversations/{conversation_id}/members", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
    },
    async listRemoteConversationMessages(conversationId, options = {}) {
      const query: { cursor?: string; limit?: number; source_account_id?: string } = {};
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      return unwrap(
        await client.GET("/api/v1/remote/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
          ...(options.signal === undefined ? {} : { signal: options.signal }),
        }),
      );
    },
    async getRemoteMessageWindow(conversationId, anchorMessageId, options = {}) {
      const query: {
        anchor_message_id: string;
        before?: number;
        after?: number;
        source_account_id?: string;
      } = { anchor_message_id: anchorMessageId };
      if (options.before !== undefined) query.before = options.before;
      if (options.after !== undefined) query.after = options.after;
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      return unwrap(
        await client.GET("/api/v1/remote/conversations/{conversation_id}/messages/window", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
    },
    async getRemoteAttachmentUrl(blobId) {
      return unwrap(
        await client.GET("/api/v1/remote/attachments/{blob_id}/url", {
          params: { path: { blob_id: blobId } },
          headers: requestHeaders(),
        }),
      );
    },
    async getRemoteAttachmentThumbnailUrl(blobId) {
      return unwrap(
        await client.GET("/api/v1/remote/attachments/{blob_id}/thumbnail-url", {
          params: { path: { blob_id: blobId } },
          headers: requestHeaders(),
        }),
      );
    },
    async searchRemoteArchive(q, options = {}) {
      const query: {
        q?: string;
        limit?: number;
        source_account_id?: string;
        conversation_id?: string[];
        sender_id?: string[];
        type?: MessageType[];
        has_attachment?: boolean;
        date_from?: number;
        date_to?: number;
        cursor?: string;
      } = {};
      // 空串不发：那不是「没给」，那是给了一个不能用的值，服务端会 400。
      // 不给关键词只按日期或类型筛，是本机搜索页的正常用法。
      if (q !== null && q !== "") query.q = q;
      if (options.limit !== undefined) query.limit = options.limit;
      if (options.sourceAccountId !== undefined) {
        query.source_account_id = options.sourceAccountId;
      }
      if (options.conversationIds?.length) query.conversation_id = options.conversationIds;
      if (options.senderIds?.length) query.sender_id = options.senderIds;
      if (options.messageTypes !== undefined) query.type = options.messageTypes;
      if (options.hasAttachment !== undefined) query.has_attachment = options.hasAttachment;
      if (options.dateFrom !== undefined) query.date_from = options.dateFrom;
      if (options.dateTo !== undefined) query.date_to = options.dateTo;
      if (options.cursor !== undefined) query.cursor = options.cursor;
      return unwrap(
        await client.GET("/api/v1/remote/search", {
          params: { query },
          headers: requestHeaders(),
          ...(options.signal === undefined ? {} : { signal: options.signal }),
        }),
      );
    },
    async completeBrowserSetup(bootstrapToken, password) {
      return unwrap(
        await client.POST("/api/v1/auth/bootstrap", {
          body: { bootstrap_token: bootstrapToken, password },
          headers: requestHeaders(),
        }),
      );
    },
    async browserSignIn(password, secondFactor) {
      // 空串与未填是同一件事，别把一个空的两步验证码当成一次真实提交。
      const trimmed = secondFactor?.trim() ?? "";
      const body: { password: string; second_factor?: string } = { password };
      if (trimmed !== "") body.second_factor = trimmed;
      return unwrap(
        await client.POST("/api/v1/auth/sign-in", {
          body,
          headers: requestHeaders(),
        }),
      );
    },
    async browserSignOut() {
      unwrap(
        await client.POST("/api/v1/auth/sign-out", { headers: requestHeaders() }),
      );
    },
    async beginTotpEnrollment(password) {
      return unwrap(
        await client.POST("/api/v1/auth/totp/enrollment", {
          body: { password },
          headers: requestHeaders(),
        }),
      );
    },
    async enableTotp(code) {
      return unwrap(
        await client.POST("/api/v1/auth/totp/enable", {
          body: { code },
          headers: requestHeaders(),
        }),
      );
    },
    async disableTotp(password) {
      unwrap(
        await client.POST("/api/v1/auth/totp/disable", {
          body: { password },
          headers: requestHeaders(),
        }),
      );
    },
    async listBrowserSessions() {
      return unwrap(
        await client.GET("/api/v1/remote/sessions", { headers: requestHeaders() }),
      );
    },
    async recordBrowserClientInfo(info) {
      const result = await client.POST("/api/v1/auth/client-info", { body: info, headers: requestHeaders() });
      if (!result.response.ok) throw new ArchiveApiError(publicErrorFrom(result.error));
    },
    async revokeBrowserSession(sessionId) {
      unwrap(
        await client.POST("/api/v1/remote/sessions/{session_id}/revoke", {
          params: { path: { session_id: sessionId } },
          headers: requestHeaders(),
        }),
      );
    },
    async revokeAllBrowserSessions() {
      const result = unwrap(
        await client.POST("/api/v1/remote/sessions/revoke-all", {
          headers: requestHeaders(),
        }),
      );
      return result.revoked_sessions;
    },
    async getReauthState() {
      return unwrap(
        await client.GET("/api/v1/auth/reauth/state", { headers: requestHeaders() }),
      );
    },
    async createReauthChallenge(purpose) {
      return unwrap(
        await client.POST("/api/v1/auth/reauth/challenge", {
          body: { purpose },
          headers: requestHeaders(),
        }),
      );
    },
    async verifyReauth(challengeId, credential) {
      return unwrap(
        await client.POST("/api/v1/auth/reauth/verify", {
          body: { challenge_id: challengeId, credential },
          headers: requestHeaders(),
        }),
      );
    },
    async lockReauth() {
      return unwrap(
        await client.POST("/api/v1/auth/reauth/lock", { headers: requestHeaders() }),
      );
    },
    async unlockReauthWithPassword(password) {
      return unwrap(
        await client.POST("/api/v1/auth/reauth/password", {
          body: { password },
          headers: requestHeaders(),
        }),
      );
    },
    async dismissReauthNotice() {
      const result = await client.DELETE("/api/v1/auth/reauth/notice", {
        headers: requestHeaders(),
      });
      if (!result.response.ok) unwrap(result);
    },
    async putReauthPolicy(enabled, policyVersion) {
      return unwrap(
        await client.PUT("/api/v1/auth/reauth/policy", {
          body: { enabled, policy_version: policyVersion },
          headers: requestHeaders(),
        }),
      );
    },
    async listPasskeys() {
      return unwrap(
        await client.GET("/api/v1/auth/passkeys", { headers: requestHeaders() }),
      );
    },
    async beginPasskeyRegistration(password) {
      return unwrap(
        await client.POST("/api/v1/auth/passkeys/registration/options", {
          body: { password },
          headers: requestHeaders(),
        }),
      );
    },
    async finishPasskeyRegistration(challengeId, credential, label) {
      const trimmed = label?.trim() ?? "";
      return unwrap(
        await client.POST("/api/v1/auth/passkeys/registration/verify", {
          body: {
            challenge_id: challengeId,
            credential,
            label: trimmed === "" ? null : trimmed,
          },
          headers: requestHeaders(),
        }),
      );
    },
    async deletePasskey(passkeyId) {
      const result = await client.DELETE("/api/v1/auth/passkeys/{passkey_id}", {
        params: { path: { passkey_id: passkeyId } },
        headers: requestHeaders(),
      });
      if (!result.response.ok) unwrap(result);
    },
    async listRemoteDevices() {
      return unwrap(
        await client.GET("/api/v1/remote/devices", { headers: requestHeaders() }),
      );
    },
    async revokeRemoteSyncDevice(deviceId) {
      unwrap(
        await client.POST("/api/v1/remote/devices/{device_id}/revoke", {
          params: { path: { device_id: deviceId } },
          headers: requestHeaders(),
        }),
      );
    },
    async getObjectStorageConfig() {
      return unwrap(
        await client.GET("/api/v1/remote/object-storage/config", {
          headers: requestHeaders(),
        }),
      );
    },
    async confirmBrowserManagement(password, secondFactor) {
      return unwrap(await client.POST("/api/v1/auth/management/verify", {
        body: { password, second_factor: secondFactor || null }, headers: requestHeaders(),
      }));
    },
    async useAttachmentStorageForBackup(input) {
      return unwrap(await client.POST("/api/v1/remote/backup/use-object-storage", {
        body: input, headers: requestHeaders(),
      }));
    },
    async updateBrowserBackupPolicy(input) {
      return unwrap(await client.PUT("/api/v1/remote/backup/config/policy", { body: input, headers: requestHeaders() }));
    },
    async disableBrowserBackup() {
      return unwrap(await client.POST("/api/v1/remote/backup/disable", { headers: requestHeaders() }));
    },
    async testObjectStorageConfig(input) {
      return unwrap(
        await client.POST("/api/v1/remote/object-storage/config/test", {
          body: input,
          headers: requestHeaders(),
        }),
      );
    },
    async updateObjectStorageConfig(input) {
      return unwrap(
        await client.PUT("/api/v1/remote/object-storage/config", {
          body: input,
          headers: requestHeaders(),
        }),
      );
    },
    async restoreObjectStorageGeneration(storageGeneration) {
      return unwrap(
        await client.POST("/api/v1/remote/object-storage/config/restore", {
          body: { storage_generation: storageGeneration },
          headers: requestHeaders(),
        }),
      );
    },
    async getBackupConfig() {
      return unwrap(
        await client.GET("/api/v1/remote/backup/config", { headers: requestHeaders() }),
      );
    },
    async testBackupConfig(input) {
      return unwrap(
        await client.POST("/api/v1/remote/backup/config/test", {
          body: input,
          headers: requestHeaders(),
        }),
      );
    },
    async updateBackupConfig(input) {
      return unwrap(
        await client.PUT("/api/v1/remote/backup/config", {
          body: input,
          headers: requestHeaders(),
        }),
      );
    },
    async listBackupRuns() {
      return unwrap(
        await client.GET("/api/v1/remote/backup/runs", { headers: requestHeaders() }),
      );
    },
    async createShare(input) {
      return unwrap(
        await client.POST("/api/v1/remote/shares", { body: input, headers: requestHeaders() }),
      );
    },
    async uploadShareObject(token, attachmentId, content) {
      // 原始字节：openapi-fetch 会把 body 当 JSON 序列化，所以这一条自己发。
      // 浏览器按 Blob 自动带 Content-Length，两端都先按它判大小再读。
      const headers = new Headers(requestHeaders());
      headers.set("Content-Type", "application/octet-stream");
      const response = await fetchImpl(
        new Request(
          `${options.baseUrl ?? ""}/api/v1/remote/shares/${encodeURIComponent(token)}/objects/${encodeURIComponent(attachmentId)}`,
          { method: "PUT", headers, body: content },
        ),
      );
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        throw new ArchiveApiError(publicErrorFrom(body));
      }
    },
    async finalizeShare(token) {
      return unwrap(
        await client.POST("/api/v1/remote/shares/{token}/finalize", {
          params: { path: { token } },
          headers: requestHeaders(),
        }),
      );
    },
    async listShares() {
      const page = unwrap(
        await client.GET("/api/v1/remote/shares", { headers: requestHeaders() }),
      );
      return page.items;
    },
    async extendShare(token) {
      return unwrap(
        await client.POST("/api/v1/remote/shares/{token}/extend", {
          params: { path: { token } },
          headers: requestHeaders(),
        }),
      );
    },
    async getPublicShare(token, options) {
      return unwrap(
        await client.GET("/api/v1/public/shares/{token}", {
          params: {
            path: { token },
            query: options?.countView === undefined ? {} : { count_view: options.countView },
          },
        }),
      );
    },
    async deleteShare(token) {
      unwrap(
        await client.DELETE("/api/v1/remote/shares/{token}", {
          params: { path: { token } },
          headers: requestHeaders(),
        }),
      );
    },
    async requestBackupRun() {
      return unwrap(
        await client.POST("/api/v1/remote/backup/run-now", { headers: requestHeaders() }),
      );
    },
    async getLatestBackup() {
      return unwrap(
        await client.GET("/api/v1/remote/backup/latest", { headers: requestHeaders() }),
      );
    },
    async probeSources() {
      return unwrap(
        await client.GET("/api/v1/sources/probe", { headers: requestHeaders() }),
      );
    },
    async listSources() {
      return unwrap(
        await client.GET("/api/v1/sources", { headers: requestHeaders() }),
      );
    },
    async acquireKey(sourceId, method, userConsent) {
      unwrap(
        await client.POST("/api/v1/sources/{source_id}/key-acquisition", {
          params: { path: { source_id: sourceId } },
          body: { method, user_consent: userConsent },
          headers: requestHeaders(),
        }),
      );
    },
    async deleteKey(sourceId) {
      const result = await client.DELETE("/api/v1/sources/{source_id}/key", {
        params: { path: { source_id: sourceId } },
        headers: requestHeaders(),
      });
      if (!result.response.ok) {
        throw new ArchiveApiError(publicErrorFrom(result.error));
      }
    },
    async createImport(accountId, importOptions = {}) {
      const created = unwrap(
        await client.POST("/api/v1/imports", {
          body: {
            account_id: accountId,
            conversation_ids: importOptions.conversation_ids ?? [],
            date_from: importOptions.date_from ?? null,
            date_to: importOptions.date_to ?? null,
            // 发现默认开、解码默认关（PSU-2.88）；与后端契约的 default 一致。
            discover_attachments: importOptions.discover_attachments ?? true,
            decode_media: importOptions.decode_media ?? false,
            // PSU-2.88 起默认关；与后端契约的 default 保持一致。
            build_search_index: importOptions.build_search_index ?? false,
            full_rescan: importOptions.full_rescan ?? false,
          },
          headers: requestHeaders(),
        }),
      );
      return created.job_id;
    },
    async listJobs(accountId) {
      const page = unwrap(
        await client.GET("/api/v1/jobs", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
      return page.items;
    },
    async listConversations(accountId, options = {}) {
      const query: { account_id: string; q?: string; cursor?: string; limit?: number } = {
        account_id: accountId,
      };
      if (options.q !== undefined && options.q !== "") query.q = options.q;
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      return unwrap(
        await client.GET("/api/v1/conversations", {
          params: { query },
          headers: requestHeaders(),
        }),
      );
    },
    async getConversation(conversationId, accountId): Promise<ConversationDetail> {
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}", {
          params: {
            path: { conversation_id: conversationId },
            query: { account_id: accountId },
          },
          headers: requestHeaders(),
        }),
      );
    },
    async listConversationMembers(conversationId, accountId, options = {}) {
      const query: { account_id: string; cursor?: string; limit?: number } = {
        account_id: accountId,
      };
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/members", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
    },
    async getContact(contactId, accountId) {
      return unwrap(
        await client.GET("/api/v1/contacts/{contact_id}", {
          params: { path: { contact_id: contactId }, query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async listConversationMessages(conversationId, accountId, options = {}) {
      const query: { account_id: string; cursor?: string; limit?: number } = {
        account_id: accountId,
      };
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/messages", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
          ...(options.signal === undefined ? {} : { signal: options.signal }),
        }),
      );
    },
    async getMessageWindow(conversationId, accountId, anchorMessageId, options = {}) {
      const query: {
        account_id: string;
        anchor_message_id: string;
        before?: number;
        after?: number;
      } = { account_id: accountId, anchor_message_id: anchorMessageId };
      if (options.before !== undefined) query.before = options.before;
      if (options.after !== undefined) query.after = options.after;
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/messages/window", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
    },
    async searchInConversation(conversationId, accountId, q, options = {}) {
      const query: {
        account_id: string;
        q?: string;
        sender_id?: string;
        type?: MessageType[];
        has_attachment?: boolean;
        date_from?: number;
        date_to?: number;
        cursor?: string;
        limit?: number;
      } = { account_id: accountId };
      if (q !== "") query.q = q;
      if (options.senderId !== undefined) query.sender_id = options.senderId;
      if (options.messageTypes !== undefined) query.type = options.messageTypes;
      if (options.hasAttachment !== undefined) query.has_attachment = options.hasAttachment;
      if (options.dateFrom !== undefined) query.date_from = options.dateFrom;
      if (options.dateTo !== undefined) query.date_to = options.dateTo;
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/search", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
          ...(options.signal === undefined ? {} : { signal: options.signal }),
        }),
      );
    },
    async getConversationMessageDays(conversationId, accountId, options = {}) {
      const query: {
        account_id: string;
        date_from?: number;
        date_to?: number;
        utc_offset_minutes?: number;
      } = { account_id: accountId };
      if (options.dateFrom !== undefined) query.date_from = options.dateFrom;
      if (options.dateTo !== undefined) query.date_to = options.dateTo;
      if (options.utcOffsetMinutes !== undefined) {
        query.utc_offset_minutes = options.utcOffsetMinutes;
      }
      const page = unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/message-days", {
          params: { path: { conversation_id: conversationId }, query },
          headers: requestHeaders(),
        }),
      );
      return page.items;
    },
    async retryAttachment(attachmentId, accountId) {
      return unwrap(
        await client.POST("/api/v1/attachments/{attachment_id}/retry", {
          params: { path: { attachment_id: attachmentId }, query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async getAttachmentContent(attachmentId, accountId, contentOptions) {
      const query = new URLSearchParams({ account_id: accountId });
      if (contentOptions?.audioFormat !== undefined) {
        query.set("audio_format", contentOptions.audioFormat);
      }
      const response = await fetchImpl(
        new Request(
          `${options.baseUrl ?? ""}/api/v1/attachments/${encodeURIComponent(attachmentId)}/content?${query}`,
          { headers: requestHeaders() },
        ),
      );
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        throw new ArchiveApiError(publicErrorFrom(body));
      }
      return response.blob();
    },
    async getAttachmentThumbnail(attachmentId, accountId) {
      const query = new URLSearchParams({ account_id: accountId });
      const response = await fetchImpl(
        new Request(
          `${options.baseUrl ?? ""}/api/v1/attachments/${encodeURIComponent(attachmentId)}/thumbnail?${query}`,
          { headers: requestHeaders() },
        ),
      );
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        throw new ArchiveApiError(publicErrorFrom(body));
      }
      return response.blob();
    },
    async searchMessages(accountId, q, options = {}) {
      const query: {
        account_id: string;
        q: string;
        conversation_id?: string[];
        sender_id?: string[];
        type?: MessageType[];
        has_attachment?: boolean;
        date_from?: number;
        date_to?: number;
        cursor?: string;
        limit?: number;
        sort?: SearchSort;
        file_class?: FileClass[];
      } = { account_id: accountId, q };
      // 空数组不发：那不是「没给」，那是「一个都不选」，会被当成没筛。
      if (options.conversationIds?.length) query.conversation_id = options.conversationIds;
      if (options.senderIds?.length) query.sender_id = options.senderIds;
      if (options.messageTypes !== undefined) query.type = options.messageTypes;
      if (options.hasAttachment !== undefined) query.has_attachment = options.hasAttachment;
      if (options.dateFrom !== undefined) query.date_from = options.dateFrom;
      if (options.dateTo !== undefined) query.date_to = options.dateTo;
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      if (options.sort !== undefined) query.sort = options.sort;
      if (options.fileClasses?.length) query.file_class = options.fileClasses;
      return unwrap(
        await client.GET("/api/v1/search", {
          params: { query },
          headers: requestHeaders(),
          ...(options.signal === undefined ? {} : { signal: options.signal }),
        }),
      );
    },
    async listContacts(accountId, options = {}) {
      const query: { account_id: string; q?: string; cursor?: string; limit?: number } = {
        account_id: accountId,
      };
      if (options.q !== undefined && options.q !== "") query.q = options.q;
      if (options.cursor !== undefined) query.cursor = options.cursor;
      if (options.limit !== undefined) query.limit = options.limit;
      return unwrap(
        await client.GET("/api/v1/contacts", {
          params: { query },
          headers: requestHeaders(),
        }),
      );
    },
    async estimateExport(scope) {
      return unwrap(
        await client.POST("/api/v1/exports/estimate", {
          body: scope,
          headers: requestHeaders(),
        }),
      );
    },
    async createExport(scope, format) {
      return unwrap(
        await client.POST("/api/v1/exports", {
          body: { scope, format },
          headers: requestHeaders(),
        }),
      );
    },
    async listExports(accountId) {
      const page = unwrap(
        await client.GET("/api/v1/exports", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
      return page.items;
    },
    async getExport(exportId, accountId) {
      return unwrap(
        await client.GET("/api/v1/exports/{export_id}", {
          params: { path: { export_id: exportId }, query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async deleteExport(exportId, accountId) {
      return unwrap(await client.DELETE("/api/v1/exports/{export_id}", {
        params: {path:{export_id:exportId},query:{account_id:accountId}},
        headers:requestHeaders(),
      }));
    },
    async getAnalysis(accountId, options) {
      return unwrap(
        await client.GET("/api/v1/stats/analysis", {
          params: { query: {
            account_id: accountId, date_from: options.dateFrom, date_to: options.dateTo,
            ...(options.conversationKind === undefined ? {} : { conversation_kind: options.conversationKind }),
            ...(options.conversationId === undefined ? {} : { conversation_id: options.conversationId }),
          } },
          headers: requestHeaders(),
        }),
      );
    },
    async previewExport(scope) {
      return unwrap(await client.POST("/api/v1/exports/preview", { body: scope, headers: requestHeaders() }));
    },
    async getExportReport(exportId, accountId) {
      return unwrap(await client.GET("/api/v1/exports/{export_id}/report", { params: { path: { export_id: exportId }, query: { account_id: accountId } }, headers: requestHeaders() }));
    },
    async downloadExport(exportId, accountId) {
      return unwrap(await client.GET("/api/v1/exports/{export_id}/download", { params: { path: { export_id: exportId }, query: { account_id: accountId } }, headers: requestHeaders(), parseAs: "blob" }));
    },
    async openExportFolder(exportId, accountId) {
      const response = await client.POST("/api/v1/exports/{export_id}/open-folder", { params: { path: { export_id: exportId }, query: { account_id: accountId } }, headers: requestHeaders() });
      if (response.error !== undefined) throw new ArchiveApiError(publicErrorFrom(response.error));
    },
    async getDailyStats(accountId, options = {}) {
      const query: { account_id: string; date_from?: number; date_to?: number } = {
        account_id: accountId,
      };
      if (options.dateFrom !== undefined) query.date_from = options.dateFrom;
      if (options.dateTo !== undefined) query.date_to = options.dateTo;
      return unwrap(
        await client.GET("/api/v1/stats/daily", {
          params: { query },
          headers: requestHeaders(),
        }),
      );
    },
    async getConversationStats(accountId, limit) {
      const query: { account_id: string; limit?: number } = { account_id: accountId };
      if (limit !== undefined) query.limit = limit;
      return unwrap(
        await client.GET("/api/v1/stats/conversations", {
          params: { query },
          headers: requestHeaders(),
        }),
      );
    },
    async getCacheStatus(accountId) {
      return unwrap(
        await client.GET("/api/v1/cache/status", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async previewCacheCleanup(accountId, category) {
      return unwrap(
        await client.POST("/api/v1/cache/cleanup/preview", {
          body: { account_id: accountId, category, confirm: false },
          headers: requestHeaders(),
        }),
      );
    },
    async cleanupCache(accountId, category) {
      return unwrap(
        await client.POST("/api/v1/cache/cleanup", {
          body: { account_id: accountId, category, confirm: true },
          headers: requestHeaders(),
        }),
      );
    },
    async getSearchIndexState(accountId) {
      return unwrap(
        await client.GET("/api/v1/search/index", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async initializeSearchIndex(accountId) {
      return unwrap(
        await client.POST("/api/v1/search/index", {
          body: { account_id: accountId },
          headers: requestHeaders(),
        }),
      );
    },
    async deleteSearchIndex(accountId) {
      return unwrap(
        await client.DELETE("/api/v1/search/index", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async getAppSettings() {
      return unwrap(
        await client.GET("/api/v1/settings", { headers: requestHeaders() }),
      );
    },
    async updateAppSettings(mediaCacheQuotaBytes, autoUpdateEnabled, stickerFetchEnabled) {
      return unwrap(
        await client.PUT("/api/v1/settings", {
          body: {
            media_cache_quota_bytes: mediaCacheQuotaBytes,
            auto_update_enabled: autoUpdateEnabled,
            sticker_fetch_enabled: stickerFetchEnabled,
          },
          headers: requestHeaders(),
        }),
      );
    },
    async getAutoSync() {
      return unwrap(
        await client.GET("/api/v1/auto-sync", { headers: requestHeaders() }),
      );
    },
    async getTranscriptionCredentials() {
      return unwrap(
        await client.GET("/api/v1/transcription/credentials", {
          headers: requestHeaders(),
        }),
      );
    },
    async putTranscriptionCredentials(input) {
      return unwrap(
        await client.PUT("/api/v1/transcription/credentials", {
          body: { secret_id: input.secretId, secret_key: input.secretKey },
          headers: requestHeaders(),
        }),
      );
    },
    async deleteTranscriptionCredentials() {
      return unwrap(
        await client.DELETE("/api/v1/transcription/credentials", {
          headers: requestHeaders(),
        }),
      );
    },
    async updateTranscriptionSettings(input) {
      return unwrap(
        await client.PUT("/api/v1/transcription", {
          body: {
            transcription_enabled: input.enabled,
            transcription_auto_new: input.autoNew,
          },
          headers: requestHeaders(),
        }),
      );
    },
    async updateAutoSync(input) {
      return unwrap(
        await client.PUT("/api/v1/auto-sync", {
          body: {
            enabled: input.enabled,
            interval_minutes: input.intervalMinutes,
            include_originals: input.includeOriginals,
          },
          headers: requestHeaders(),
        }),
      );
    },
    async getUpdateStatus() {
      return unwrap(
        await client.GET("/api/v1/updates/status", { headers: requestHeaders() }),
      );
    },
    async checkForUpdates(trigger) {
      return unwrap(
        await client.POST("/api/v1/updates/check", {
          ...(trigger === undefined ? {} : { body: { trigger } }),
          headers: requestHeaders(),
        }),
      );
    },
    async postponeUpdateReminder(version, until) {
      return unwrap(
        await client.POST("/api/v1/updates/postpone", {
          body: { version, ...(until === undefined ? {} : { until }) },
          headers: requestHeaders(),
        }),
      );
    },
    async applyUpdate(version) {
      return unwrap(
        await client.POST("/api/v1/updates/apply", {
          body: { version, confirm: true },
          headers: requestHeaders(),
        }),
      );
    },
    async installUpdateNow(version) {
      return unwrap(
        await client.POST("/api/v1/updates/install-now", {
          body: { version, confirm: true },
          headers: requestHeaders(),
        }),
      );
    },
    async rollbackUpdate() {
      return unwrap(
        await client.POST("/api/v1/updates/rollback", {
          body: { confirm: true },
          headers: requestHeaders(),
        }),
      );
    },
    async getSyncHealth(accountId) {
      return unwrap(
        await client.GET("/api/v1/sync/health", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async runSync(accountId) {
      return unwrap(
        await client.POST("/api/v1/sync/run", {
          body: { account_id: accountId },
          headers: requestHeaders(),
        }),
      );
    },
    async createSyncJob(input) {
      return unwrap(
        await client.POST("/api/v1/sync/jobs", {
          body: {
            account_id: input.accountId,
            include_originals: input.includeOriginals,
            allow_low_space: input.allowLowSpace ?? false,
          },
          headers: requestHeaders(),
        }),
      );
    },
    async getWechatCleanupReadiness(conversationId, accountId, keepMonths) {
      return unwrap(
        await client.GET("/api/v1/conversations/{conversation_id}/wechat-cleanup", {
          params: {
            path: { conversation_id: conversationId },
            query: { account_id: accountId, keep_months: keepMonths },
          },
          headers: requestHeaders(),
        }),
      );
    },
    async startWechatCleanupSync(conversationId, accountId, keepMonths) {
      return unwrap(
        await client.POST("/api/v1/conversations/{conversation_id}/wechat-cleanup/sync", {
          params: {
            path: { conversation_id: conversationId },
            query: { account_id: accountId, keep_months: keepMonths },
          },
          headers: requestHeaders(),
        }),
      );
    },
    async startWechatCleanupDrive(conversationId, accountId, keepMonths) {
      return unwrap(
        await client.POST("/api/v1/conversations/{conversation_id}/wechat-cleanup/drive", {
          params: {
            path: { conversation_id: conversationId },
            query: { account_id: accountId, keep_months: keepMonths },
          },
          headers: requestHeaders(),
        }),
      );
    },
    async getWechatCleanupDrive() {
      return unwrap(
        await client.GET("/api/v1/wechat-cleanup/drive", { headers: requestHeaders() }),
      );
    },
    async getWechatCleanupObserved(sessionId) {
      return unwrap(
        await client.GET("/api/v1/wechat-cleanup/observed", {
          params: { query: sessionId === undefined ? {} : { session_id: sessionId } }, headers: requestHeaders(),
        }),
      );
    },
    async closeWechatCleanupHint(sessionId) {
      const result = await client.POST("/api/v1/wechat-cleanup/hint/close", {
        params: { query: sessionId === undefined ? {} : { session_id: sessionId } }, headers: requestHeaders(),
      });
      if (!result.response.ok) unwrap(result);
    },
    async cancelWechatCleanupDrive(sessionId) {
      const result = await client.POST("/api/v1/wechat-cleanup/drive/cancel", {
        params: { query: sessionId === undefined ? {} : { session_id: sessionId } }, headers: requestHeaders(),
      });
      if (!result.response.ok) unwrap(result);
    },
    async issuePairingCode() {
      return unwrap(
        await client.POST("/api/v1/remote/pairing-codes", {
          headers: requestHeaders(),
        }),
      );
    },
    async redeemPairingCode(input) {
      return unwrap(
        await client.POST("/api/v1/remote/pairing", {
          body: {
            base_url: input.baseUrl,
            pairing_code: input.pairingCode,
            device_name: input.deviceName,
          },
          headers: requestHeaders(),
        }),
      );
    },
    sameOriginFetch,
    async submitFeedback(input) {
      // 薄封装：契约怎么写就怎么发。这里**不加字段也不改字段**——`project_id`
      // 由后端注入，`context` 由调用方拼好，本层不替谁做主。
      return unwrap(
        await client.POST("/api/v1/feedback", {
          body: input,
          headers: requestHeaders(),
        }),
      );
    },
    async getStorageOverview(accountId) {
      return unwrap(
        await client.GET("/api/v1/storage/overview", {
          params: { query: { account_id: accountId } },
          headers: requestHeaders(),
        }),
      );
    },
    async previewStorageReclaim(accountId) {
      return unwrap(
        await client.POST("/api/v1/storage/reclaim/preview", {
          // 预览忽略这个字段，但契约把它列成了必填的一部分；显式传 false 比让
          // 生成的类型宽松掉更安全——执行端点靠它挡住误触。
          body: { account_id: accountId, confirm: false },
          headers: requestHeaders(),
        }),
      );
    },
    async startStorageReclaim(accountId) {
      return unwrap(
        await client.POST("/api/v1/storage/reclaim", {
          body: { account_id: accountId, confirm: true },
          headers: requestHeaders(),
        }),
      );
    },
    async unloadMediaCache(accountId) {
      return unwrap(
        await client.POST("/api/v1/storage/unload", {
          body: {
            account_id: accountId,
            confirmation: "UNLOAD_LOCAL_MEDIA_CACHE",
          },
          headers: requestHeaders(),
        }),
      );
    },
    getJob,
    pauseJob: (jobId, accountId) => controlJob("pause", jobId, accountId),
    resumeJob: (jobId, accountId) => controlJob("resume", jobId, accountId),
    cancelJob: (jobId, accountId) => controlJob("cancel", jobId, accountId),
    async readJobEvents(jobId, accountId, onEvent, eventOptions = {}) {
      const query = new URLSearchParams({
        account_id: accountId,
        wait_ms: String(eventOptions.waitMs ?? 25_000),
      });
      const headers = new Headers(requestHeaders());
      headers.set("Accept", "text/event-stream");
      if (eventOptions.lastSequence !== undefined) {
        headers.set("Last-Event-ID", String(eventOptions.lastSequence));
      }
      const requestInit: RequestInit = { headers };
      if (eventOptions.signal !== undefined) {
        requestInit.signal = eventOptions.signal;
      }
      const response = await fetchImpl(
        new Request(
          `${options.baseUrl ?? ""}/api/v1/jobs/${encodeURIComponent(jobId)}/events?${query}`,
          requestInit,
        ),
      );
      if (!response.ok) {
        const body: unknown = await response.json().catch(() => null);
        throw new ArchiveApiError(publicErrorFrom(body));
      }
      if (response.body === null) {
        return eventOptions.lastSequence ?? 0;
      }
      const reader = response.body.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      let lastSequence = eventOptions.lastSequence ?? 0;
      for (;;) {
        const { done, value } = await reader.read();
        buffer += decoder.decode(value, { stream: !done }).replaceAll("\r\n", "\n");
        let boundary = buffer.indexOf("\n\n");
        while (boundary >= 0) {
          const frame = parseFrame(buffer.slice(0, boundary));
          buffer = buffer.slice(boundary + 2);
          if (frame !== null && frame.sequence > lastSequence) {
            lastSequence = frame.sequence;
            onEvent(frame);
          }
          boundary = buffer.indexOf("\n\n");
        }
        if (done) {
          break;
        }
      }
      return lastSequence;
    },
  };
};

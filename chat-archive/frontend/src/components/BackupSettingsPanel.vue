<script setup lang="ts">
import AppSelect from "@/components/ui/AppSelect.vue";
import UnsavedChangesDialog from "@/components/UnsavedChangesDialog.vue";
import { useUnsavedChanges } from "@/composables/useUnsavedChanges";
import { useQuery } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, reactive, ref, watch } from "vue";

import {
  ArchiveApiError,
  type BackupConfig,
  type BackupRunList,
  type BackupVerification,
  type SyncHealth,
} from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import ServerManagementFields from "@/components/ServerManagementFields.vue";
import type { ServerManagementHandle } from "@/components/serverManagement";
import BackupRunTimeline from "@/components/BackupRunTimeline.vue";
import SettingsTip from "@/components/SettingsTip.vue";
import { useCapabilityStore } from "@/stores/capabilities";
import { downloadBackupParts } from "@/lib/backupDownload";
import {
  buildBackupTimeline,
  describeBackupFailure,
  formatDuration,
  formatElapsed,
} from "@/lib/backupTimeline";
import { formatTimeOfDay } from "@/utils/time";

const props = withDefaults(
  defineProps<{
    accountId?: string | null;
    /** manage: paired desktop; browser: authenticated web management; status: legacy read-only. */
    mode?: "manage" | "browser" | "status";
    /**
     * 只当「备份记录」用：同一页上方已有能改配置、立即备份的网页管理卡（PSU-2.219，
     * 能力 `backup-management`）。这时不再说“只读 / 只能在桌面端”，未配置也不重复横幅。
     */
    compact?: boolean;
  }>(),
  { accountId: null, mode: "manage", compact: false },
);

const api = useArchiveApi();
const capabilities = useCapabilityStore();
const browser = computed(() => props.mode === "browser");
const manages = computed(() => props.mode !== "status");
const proof = ref<ServerManagementHandle | null>(null);

const healthQuery = useQuery({
  queryKey: computed(() => ["backup-health", props.accountId] as const),
  enabled: computed(() => props.mode === "manage" && props.accountId !== null),
  queryFn: (): Promise<SyncHealth> => {
    if (props.accountId === null) throw new Error("尚未选择账号");
    return api.getSyncHealth(props.accountId);
  },
  retry: false,
});

/**
 * 这一端谈不谈得上备份。
 *
 * 管理那一半问远端的 `backend_type`：备份只在自建 SQLite 那一支成立，
 * postgres-s3 的灾备是 pgBackRest 的 recovery point，Worker 那一支既没有文件
 * 系统也没有长任务。浏览器那一半没有 sync-health 这条路由，所以改问能力清单——
 * 两条都是**问后端**，不在前端按别的线索推断。
 */
const supportsBackup = computed(() =>
  props.mode === "manage"
    ? healthQuery.data.value?.backend_type === "self_hosted_sqlite"
    : capabilities.states["remote-backup"] !== "unsupported",
);

const configQuery = useQuery({
  queryKey: computed(() => ["backup-config", props.accountId] as const),
  enabled: computed(() => manages.value && supportsBackup.value),
  queryFn: (): Promise<BackupConfig> => api.getBackupConfig(),
  retry: false,
});

const requestedAfterRunId = ref<number | null>(null);
const actionNote = ref<string | null>(null);
const runRefetchInterval = computed(() => {
  const waitingForRun = requestedAfterRunId.value !== null;
  return (query: { state: { data: BackupRunList | undefined } }): number | false =>
    waitingForRun || query.state.data?.last_backup_status === "running" ? 2000 : false;
});

const runsQuery = useQuery({
  queryKey: computed(() => ["backup-runs", props.accountId] as const),
  enabled: supportsBackup,
  queryFn: (): Promise<BackupRunList> => api.listBackupRuns(),
  retry: false,
  refetchInterval: runRefetchInterval,
});

const config = computed(() => configQuery.data.value ?? null);
const runs = computed(() => runsQuery.data.value ?? null);
const backupInProgress = computed(() =>
  requestedAfterRunId.value !== null || runs.value?.last_backup_status === "running",
);
watch(runs, (value) => {
  const after = requestedAfterRunId.value;
  if (after !== null && value?.runs.some((run) => run.id > after && run.status !== "running")) {
    requestedAfterRunId.value = null;
    actionNote.value = null;
  }
});
const runReadFailed = computed(() =>
  runsQuery.error.value !== null && (manages.value || runsQuery.data.value !== undefined),
);
const configured = computed(() =>
  manages.value
    ? config.value?.configured === true
    : runs.value !== null && runs.value.last_backup_status !== "not_configured",
);
const overdue = computed(() => runs.value?.backup_overdue === true);
/**
 * 远端解不开存着的凭据。恢复之后必然如此（主密钥不进备份）：`configured` 仍为 true、
 * 端点/桶/前缀能预填，但 worker 一轮都不跑。不把它单独说出来，面板显示「已配置」而
 * 备份永远不跑，要等两个周期后才由 overdue 响。
 */
const locked = computed(
  () =>
    config.value?.credentials_readable === false || runs.value?.last_backup_status === "locked",
);
/** lifecycle 模式的静默失败：规则被删了，应用一次 delete 都不发，备份堆到撑满。 */
const lifecycleMissing = computed(() => runs.value?.backup_lifecycle_covered === false);
const alerting = computed(() => overdue.value || locked.value || lifecycleMissing.value);

const loadErrorText = computed(() => {
  const error = manages.value ? configQuery.error.value
    : runsQuery.data.value === undefined ? runsQuery.error.value : null;
  if (error === null) return null;
  return error instanceof ArchiveApiError ? error.message : "暂时无法读取备份配置。";
});

// 重新加载读的是出错的那一条：浏览器那一半没有配置路由，只能重读备份记录。
const reload = (): void => {
  void (manages.value ? configQuery.refetch() : runsQuery.refetch());
};

const REGIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: "ap-nanjing", label: "南京（ap-nanjing）" },
  { value: "ap-guangzhou", label: "广州（ap-guangzhou）" },
  { value: "ap-shanghai", label: "上海（ap-shanghai）" },
  { value: "ap-beijing", label: "北京（ap-beijing）" },
  { value: "ap-chengdu", label: "成都（ap-chengdu）" },
  { value: "ap-singapore", label: "新加坡（ap-singapore）" },
];

const endpointForRegion = (region: string): string => `https://cos.${region}.myqcloud.com`;

type RetentionMode = "lifecycle" | "application";

const form = reactive<{
  region: string;
  bucket: string;
  prefix: string;
  endpoint: string;
  secretId: string;
  secretKey: string;
  intervalHours: number;
  retentionMode: RetentionMode;
}>({
  region: "ap-nanjing",
  bucket: "",
  prefix: "backup",
  endpoint: "",
  secretId: "",
  secretKey: "",
  intervalHours: 24,
  retentionMode: "lifecycle",
});

const editing = ref(false);
const busy = ref(false);
const formError = ref<string | null>(null);
const editSnapshot = ref("");
const nonSecretFields = computed(() => JSON.stringify([form.region, form.bucket, form.prefix, form.endpoint, form.intervalHours, form.retentionMode]));
const editDirty = computed(() => editing.value && (nonSecretFields.value !== editSnapshot.value || form.secretId !== "" || form.secretKey !== ""));
const editBusy = computed(() => editing.value && busy.value);
const leave = useUnsavedChanges(editDirty, editBusy);
// 验证结果只对**当前这组值**有效。任何一处改动都作废它——否则"测过了"会变成
// 一句和眼前这份配置无关的话。
const verification = ref<BackupVerification | null>(null);

const invalidate = (): void => {
  verification.value = null;
  formError.value = null;
};

const startEditing = (): void => {
  const current = config.value;
  form.region = current?.region ?? "ap-nanjing";
  form.endpoint = current?.endpoint ?? endpointForRegion(form.region);
  form.bucket = current?.bucket ?? "";
  form.prefix = current?.prefix ?? "backup";
  form.secretId = "";
  form.secretKey = "";
  form.intervalHours = current?.interval_hours ?? 24;
  form.retentionMode = current?.retention_mode === "application" ? "application" : "lifecycle";
  invalidate();
  editSnapshot.value = nonSecretFields.value;
  editing.value = true;
};

const discardEditing = (): void => {
  if (busy.value) return;
  editing.value = false;
  form.secretId = "";
  form.secretKey = "";
  proof.value?.clear();
  invalidate();
};

const cancelEditing = (): void => { void leave.discardThen(discardEditing); };

const body = () => ({
  endpoint: form.endpoint || endpointForRegion(form.region),
  bucket: form.bucket.trim(),
  region: form.region,
  prefix: form.prefix.trim(),
  secret_id: form.secretId.trim(),
  secret_key: form.secretKey,
  interval_hours: form.intervalHours,
  retention_mode: form.retentionMode,
});

const describe = (error: unknown): string =>
  error instanceof ArchiveApiError ? error.message : "操作失败，请稍后再试。";

const runTest = async (): Promise<void> => {
  busy.value = true;
  formError.value = null;
  try {
    if (browser.value && !(await proof.value?.authorize())) return;
    verification.value = await api.testBackupConfig(body());
  } catch (error) {
    proof.value?.requireProof(error);
    verification.value = null;
    formError.value = describe(error);
  } finally {
    busy.value = false;
  }
};

const save = async (): Promise<void> => {
  if (!canSave.value) return;
  busy.value = true;
  formError.value = null;
  try {
    if (browser.value && !(await proof.value?.authorize())) return;
    await api.updateBackupConfig(body());
    editing.value = false;
    verification.value = null;
    form.secretId = "";
    form.secretKey = "";
    proof.value?.clear();
    if (browser.value) void capabilities.refresh(api);
    await Promise.all([configQuery.refetch(), runsQuery.refetch()]);
  } catch (error) {
    proof.value?.requireProof(error);
    formError.value = describe(error);
  } finally {
    busy.value = false;
  }
};

const disableBackup = async (): Promise<void> => {
  const manager = proof.value;
  if (!browser.value || busy.value || manager === null || !manager.ready) return;
  busy.value = true;
  formError.value = null;
  try {
    if (!(await manager.authorize())) return;
    await api.disableBrowserBackup();
    editing.value = false;
    form.secretId = "";
    form.secretKey = "";
    manager.clear();
    await Promise.all([configQuery.refetch(), runsQuery.refetch()]);
  } catch (error: unknown) {
    manager.requireProof(error);
    formError.value = describe(error);
  } finally { busy.value = false; }
};

const backupNow = async (): Promise<void> => {
  if (backupInProgress.value) return;
  busy.value = true;
  actionNote.value = null;
  try {
    const after = Math.max(0, ...(runs.value?.runs.map((run) => run.id) ?? []));
    await api.requestBackupRun();
    requestedAfterRunId.value = after;
    // 排队，不是完成。说成"已备份"会让人以为事情已经办完了。
    actionNote.value = "已请求备份。它在远端后台跑，可能要几分钟，完成后这里会更新。";
    await runsQuery.refetch();
  } catch (error) {
    actionNote.value = describe(error);
  } finally {
    busy.value = false;
  }
};

const downloadLatest = async (): Promise<void> => {
  busy.value = true;
  actionNote.value = null;
  try {
    const download = await api.getLatestBackup();
    const count = downloadBackupParts(download);
    actionNote.value =
      `正在下载 ${String(count)} 个文件（${download.backup_id}）。` +
      "浏览器可能会问你是否允许一次下载多个文件。";
  } catch (error) {
    actionNote.value = describe(error);
  } finally {
    busy.value = false;
  }
};

const canSave = computed(
  () =>
    !busy.value &&
    (!browser.value || proof.value?.ready === true) &&
    verification.value !== null &&
    form.bucket.trim() !== "" &&
    form.secretId.trim() !== "" &&
    form.secretKey !== "",
);

onBeforeUnmount(() => { form.secretId = ""; form.secretKey = ""; });

const updateRegion = (): void => {
  form.endpoint = endpointForRegion(form.region);
  invalidate();
};

const formatTimestamp = (value: number | null | undefined): string =>
  value === null || value === undefined
    ? "从未成功过"
    : new Date(value * 1000).toLocaleString("zh-CN");

const formatBytes = (value: number | null | undefined): string => {
  if (value === null || value === undefined) return "—";
  const mib = value / (1024 * 1024);
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB` : `${mib.toFixed(1)} MiB`;
};

const STATUS_LABELS: Readonly<Record<string, string>> = {
  not_configured: "未配置",
  never: "还没有跑过",
  running: "正在备份",
  success: "上一轮成功",
  failed: "上一轮失败",
  locked: "凭据解不开，没有在跑",
};

const statusText = computed(() => STATUS_LABELS[runs.value?.last_backup_status ?? ""] ?? "未知");

// --- 最近的备份（2026-09-26 原型 A）-------------------------------------------
// 原来是按先后排的十根竖条：漏跑的那天不出现，十根一样的绿条也读不出结论。
// 现在先给一句结论，再把每一轮摆回真实时间上。

// 「现在」跟着记录一起刷新：否则页面开一整天，漏跑的格子永远补不出来。
const nowSeconds = computed(() => {
  void runsQuery.dataUpdatedAt.value;
  return Math.floor(Date.now() / 1000);
});

// 周期只有管理那一半读得到；浏览器那一半不推空位，只画读到的。
const intervalHours = computed(() => config.value?.interval_hours ?? null);

const timeline = computed(() =>
  buildBackupTimeline(
    runs.value?.runs ?? [],
    intervalHours.value === null ? null : intervalHours.value * 3600,
    nowSeconds.value,
    overdue.value,
  ),
);

const dayOf = (at: number): string => {
  const date = new Date(at * 1000);
  return `${String(date.getMonth() + 1)}/${String(date.getDate())}`;
};

/** 下一次是将来的时间，微信那套「昨天 / 星期几」只往回说，这里补「今天 / 明天」。 */
const formatUpcoming = (at: number): string => {
  const target = new Date(at * 1000);
  const today = new Date(nowSeconds.value * 1000);
  const startOf = (date: Date): number =>
    new Date(date.getFullYear(), date.getMonth(), date.getDate()).getTime();
  const days = Math.round((startOf(target) - startOf(today)) / 86_400_000);
  const prefix = days === 0 ? "今天" : days === 1 ? "明天" : dayOf(at);
  return `${prefix} ${formatTimeOfDay(at)}`;
};

type HeadlineTone = "ok" | "warn" | "run" | "danger";

const headline = computed<{ tone: HeadlineTone; text: string; detail: string | null }>(() => {
  const status = runs.value?.last_backup_status ?? "";
  if (locked.value) {
    return { tone: "danger", text: STATUS_LABELS.locked ?? "", detail: null };
  }
  if ((runs.value?.runs.length ?? 0) === 0) {
    // 没有逐轮记录时，服务端的 overdue 仍然是事实，不能退回「上一轮成功」。
    if (overdue.value) return { tone: "warn", text: "已经连着两个周期没有备份成功", detail: null };
    const tone: HeadlineTone = status === "success" ? "ok" : status === "running" ? "run" : "warn";
    return { tone, text: statusText.value, detail: null };
  }
  const line = timeline.value.headline;
  switch (line.kind) {
    case "running":
      return { tone: "run", text: "正在备份", detail: null };
    case "stale":
      return {
        tone: "warn",
        text:
          line.sinceLastSuccess === null
            ? "还没有一次成功的备份"
            : `已经 ${formatElapsed(line.sinceLastSuccess)}没有成功备份`,
        detail: line.dueAt === null ? null : `本该在 ${dayOf(line.dueAt)} ${formatTimeOfDay(line.dueAt)} 跑一轮，没有记录。`,
      };
    case "failures":
      return {
        tone: "warn",
        text: `最近 ${String(line.total)} 次里有 ${String(line.failed)} 次失败`,
        detail: `最近一次失败在 ${dayOf(line.lastFailure.started_at)}：${describeBackupFailure(line.lastFailure.failure_code)}。`,
      };
    case "gaps":
      return {
        tone: "warn",
        text: `最近 ${String(line.count)} 次都成功，但中间漏了 ${String(line.missed)} 个周期`,
        detail: null,
      };
    case "streak":
      return { tone: "ok", text: `连续 ${String(line.count)} 次成功`, detail: null };
  }
});

const HEADLINE_ICONS: Readonly<Record<HeadlineTone, string>> = {
  ok: "✓",
  warn: "!",
  run: "…",
  danger: "!",
};

const lastSuccessDetail = computed(() => {
  const run = timeline.value.lastSuccess;
  if (run === null) return null;
  const parts: string[] = [];
  if (run.finished_at !== null) parts.push(`耗时 ${formatDuration(run.finished_at - run.started_at)}`);
  if (run.verified) parts.push("已校验");
  return parts.length === 0 ? null : parts.join(" · ");
});

// 和窗口里最早那一份比：备份突然变小，多半是哪里出了问题。
const sizeComparison = computed(() => {
  const earliest = timeline.value.earliestSuccess;
  if (earliest === null || earliest.stored_bytes === null) return null;
  return `${dayOf(earliest.started_at)} 那份 ${formatBytes(earliest.stored_bytes)}`;
});

const setRetention = (mode: RetentionMode): void => {
  if (form.retentionMode === mode) return;
  form.retentionMode = mode;
  invalidate();
};

const RETENTION_LABELS: Readonly<Record<string, string>> = {
  lifecycle: "交给存储桶的过期规则",
  application: "由应用按份数清理",
};

const retentionText = computed(
  () => RETENTION_LABELS[runs.value?.backup_retention_mode ?? ""] ?? "—",
);

const lifecycleCheck = computed<{ tone: "ok" | "warn" | "unknown"; text: string; tip: string | null } | null>(() => {
  const result = verification.value;
  if (result === null) return null;
  if (result.lifecycle_prefixes === null) {
    // 问不到 ≠ 没有规则。把权限事实报成配置缺口，会让人去修一个不存在的问题。
    return {
      tone: "unknown",
      text: "? 读不到生命周期配置",
      tip: "这把密钥没有读取生命周期配置的权限，所以查不出有没有过期规则——这本身不是错误。",
    };
  }
  if (result.lifecycle_prefixes.length === 0) {
    return {
      tone: "warn",
      text: "○ 这个桶上没有过期规则",
      tip: "选择交给存储桶过期的话，旧备份会一直堆积，直到把桶撑满。",
    };
  }
  return {
    tone: "ok",
    text: `✓ 已检测到过期规则：${result.lifecycle_prefixes.join("、")}`,
    tip: null,
  };
});
</script>

<template>
  <div
    v-if="supportsBackup"
    id="backup"
    class="backup-settings"
    data-testid="backup-panel"
  >
    <div
      v-if="manages ? configQuery.isLoading.value : runsQuery.isLoading.value"
      class="settings-skeleton"
      data-reserve="backup"
      aria-busy="true"
    >
      <p class="settings-muted">
        正在读取备份配置……
      </p>
      <i aria-hidden="true" />
    </div>

    <div
      v-else-if="loadErrorText !== null"
      class="settings-banner is-danger"
      role="alert"
    >
      <div class="settings-banner-main">
        <strong>读不到备份状态</strong>
        <p>{{ loadErrorText }}</p>
      </div>
      <div class="settings-banner-actions">
        <button
          class="button secondary"
          type="button"
          @click="reload"
        >
          重新加载
        </button>
      </div>
    </div>

    <template v-else>
      <div
        v-if="runReadFailed"
        class="settings-banner is-danger"
        role="alert"
        data-testid="backup-refresh-error"
      >
        <div class="settings-banner-main">
          <strong>暂时读不到备份结果</strong>
        </div>
        <div class="settings-banner-actions">
          <button
            class="button secondary"
            type="button"
            data-testid="backup-refresh-retry"
            @click="runsQuery.refetch()"
          >
            重读结果
          </button>
        </div>
      </div>
      <p
        v-if="compact && !manages && !configured"
        class="settings-muted backup-runs-empty"
        data-testid="backup-runs-empty"
      >
        还没有备份记录
      </p>
      <div
        v-else-if="!configured && !editing"
        class="settings-banner"
        role="status"
        data-testid="backup-unconfigured"
      >
        <div class="settings-banner-main">
          <strong>
            还没配置备份：服务器上的记录只有一份
            <SettingsTip label="备份存的是什么">
              服务器欠费被回收或磁盘损坏，这份归档就没了；本机只保留近期的一部分，不是全部。
              配置好之后，服务器会定期把聊天记录打包传到你的存储桶。旧备份按你选的方式清理。
              附件本身已经在附件存储里，不在这份备份中。
            </SettingsTip>
          </strong>
          <p>服务器被回收或磁盘损坏，这份归档就没了。</p>
          <p
            v-if="!manages"
            data-testid="backup-configure-elsewhere"
          >
            配置备份要填写凭据，只能在已配对的桌面端完成。
          </p>
        </div>
        <div
          v-if="manages"
          class="settings-banner-actions"
        >
          <button
            class="button primary"
            type="button"
            data-testid="backup-configure"
            @click="startEditing"
          >
            配置备份目标
          </button>
        </div>
      </div>

      <template v-else-if="configured && !editing">
        <div
          class="backup-status"
          :class="{ 'is-alerting': alerting }"
          data-testid="backup-status"
          :role="alerting ? 'alert' : undefined"
        >
          <div
            v-if="locked"
            class="settings-banner is-danger"
          >
            <div class="settings-banner-main">
              <strong data-testid="backup-locked">
                服务器解不开存着的备份凭据，备份停了
                <SettingsTip label="为什么会这样">
                  通常发生在恢复之后：主密钥故意不进备份。端点、存储桶和前缀还在，
                  重新填一次 SecretId 和 SecretKey 即可；在那之前服务器一轮备份都不会跑。
                </SettingsTip>
              </strong>
              <p>重新填一次 SecretId 和 SecretKey 即可。</p>
            </div>
          </div>
          <!-- 过期不再单独挂横幅：下面那句结论说的就是它，两处重复一件事。 -->
          <div
            v-if="lifecycleMissing"
            class="settings-banner"
            data-testid="backup-lifecycle-missing"
          >
            <div class="settings-banner-main">
              <strong>
                旧备份不会自动清理
                <SettingsTip label="怎么处理">
                  到腾讯云对象存储控制台，给备份前缀配一条生命周期规则。
                  服务器没有删除权限，所以只能靠存储桶自己过期。
                </SettingsTip>
              </strong>
              <p>存储桶上没有检测到过期规则，旧备份会一直堆到把桶撑满。</p>
            </div>
          </div>

          <div class="settings-overview backup-overview">
            <div
              class="backup-headline"
              :class="`is-${headline.tone}`"
            >
              <span
                class="backup-headline-icon"
                aria-hidden="true"
              >{{ HEADLINE_ICONS[headline.tone] }}</span>
              <strong data-testid="backup-last-status">{{ headline.text }}</strong>
              <span
                v-if="timeline.nextAt !== null"
                class="backup-headline-next"
                data-testid="backup-next"
              >下一次 {{ formatUpcoming(timeline.nextAt) }}</span>
            </div>
            <p
              v-if="headline.detail !== null"
              class="backup-headline-detail"
            >
              {{ headline.detail }}
            </p>
            <BackupRunTimeline
              v-if="timeline.marks.length > 0"
              :marks="timeline.marks"
              :next-at="timeline.nextAt"
              :now-seconds="nowSeconds"
              :interval-hours="intervalHours"
            />
            <p
              v-else
              class="settings-muted backup-runs-empty"
              data-testid="backup-runs-empty"
            >
              还没有备份记录
            </p>
            <dl class="settings-facts backup-facts">
              <div class="settings-fact">
                <dt>上次成功</dt>
                <dd data-testid="backup-last-success">
                  {{ formatTimestamp(runs?.last_backup_at) }}
                </dd>
                <dd
                  v-if="lastSuccessDetail !== null"
                  class="backup-fact-sub"
                >
                  {{ lastSuccessDetail }}
                </dd>
              </div>
              <div class="settings-fact">
                <dt>最近一份大小</dt>
                <dd>{{ formatBytes(runs?.last_backup_bytes) }}</dd>
                <dd
                  v-if="sizeComparison !== null"
                  class="backup-fact-sub"
                >
                  {{ sizeComparison }}
                </dd>
              </div>
              <div class="settings-fact">
                <dt>旧备份清理</dt>
                <dd>{{ retentionText }}</dd>
                <dd
                  v-if="runs?.backup_lifecycle_covered != null"
                  class="backup-fact-sub"
                  :class="runs.backup_lifecycle_covered ? 'is-ok' : 'is-warn'"
                >
                  <span aria-hidden="true">{{ runs.backup_lifecycle_covered ? "✓ " : "! " }}</span><span data-testid="backup-lifecycle">{{ runs.backup_lifecycle_covered ? "已检测到，覆盖备份前缀" : "没有检测到" }}</span>
                </dd>
              </div>
            </dl>
          </div>
        </div>

        <dl
          v-if="manages"
          class="settings-facts backup-summary"
          data-testid="backup-summary"
        >
          <div class="settings-fact">
            <dt>地域</dt>
            <dd>{{ config?.region }}</dd>
          </div>
          <div class="settings-fact">
            <dt>存储桶</dt>
            <dd>{{ config?.bucket }}</dd>
          </div>
          <div class="settings-fact">
            <dt>前缀</dt>
            <dd>{{ config?.prefix }}</dd>
          </div>
          <div class="settings-fact">
            <dt>SecretId</dt>
            <dd data-testid="backup-key-hint">
              {{ config?.access_key_hint }}
            </dd>
          </div>
        </dl>
        <p
          v-else-if="!compact"
          class="settings-muted"
          data-testid="backup-read-only"
        >
          这里只读。改配置、立即备份和下载都在已配对的桌面端。
        </p>

        <div
          v-if="manages"
          class="backup-actions"
        >
          <button
            class="button primary"
            type="button"
            data-testid="backup-run-now"
            :disabled="busy || backupInProgress"
            @click="backupNow"
          >
            立即备份
          </button>
          <button
            v-if="!browser"
            class="button secondary"
            type="button"
            :disabled="busy"
            data-testid="backup-download"
            @click="downloadLatest"
          >
            下载到这台电脑
          </button>
          <SettingsTip
            v-if="!browser"
            label="为什么要下载"
          >
            服务器上的备份与服务器在同一个云账号里。下载一份到本机，才算真正多了一份；目前需要手动。
          </SettingsTip>
          <button
            :class="locked ? 'button primary' : 'button quiet'"
            type="button"
            data-testid="backup-edit"
            @click="startEditing"
          >
            {{ locked ? "重新填写凭据" : "修改配置" }}
          </button>
          <SettingsTip label="关于凭据">
            页面从不读取已保存的 SecretKey；更换凭据需要重新输入一次。
          </SettingsTip>
        </div>
        <p
          v-if="actionNote !== null"
          class="settings-muted backup-note"
          role="status"
          data-testid="backup-action-note"
        >
          {{ actionNote }}
        </p>
      </template>

      <form
        v-else
        class="backup-form settings-form"
        data-testid="backup-form"
        @submit.prevent="save"
      >
        <label class="settings-field">
          地域
          <AppSelect
            v-model="form.region"
            :disabled="busy"
            :options="REGIONS"
            aria-label="备份地域"
            @update:model-value="updateRegion"
          />
        </label>
        <label
          v-if="form.endpoint !== endpointForRegion(form.region)"
          class="settings-field is-wide"
        >
          Endpoint
          <input
            v-model="form.endpoint"
            :disabled="busy"
            type="url"
            data-testid="backup-endpoint"
            @input="invalidate"
          >
        </label>
        <label class="settings-field">
          存储桶
          <input
            v-model="form.bucket"
            :disabled="busy"
            type="text"
            autocomplete="off"
            data-testid="backup-bucket"
            @input="invalidate"
          >
        </label>
        <label class="settings-field">
          SecretId
          <input
            v-model="form.secretId"
            :disabled="busy"
            type="text"
            autocomplete="off"
            data-testid="backup-secret-id"
            @input="invalidate"
          >
        </label>
        <label class="settings-field">
          SecretKey
          <input
            v-model="form.secretKey"
            :disabled="busy"
            type="password"
            autocomplete="new-password"
            data-testid="backup-secret-key"
            @input="invalidate"
          >
        </label>
        <label class="settings-field">
          备份频率（小时，1–720）
          <input
            v-model.number="form.intervalHours"
            :disabled="busy"
            type="number"
            min="1"
            max="720"
            data-testid="backup-interval"
            @input="invalidate"
          >
        </label>
        <label class="settings-field">
          前缀
          <input
            v-model="form.prefix"
            :disabled="busy"
            type="text"
            autocomplete="off"
            data-testid="backup-prefix"
            @input="invalidate"
          >
          <span class="settings-muted backup-hint">
            不能以 <code>a/</code> 开头，那里放的是附件。
          </span>
        </label>
        <div class="settings-field is-wide">
          <span class="backup-field-title">
            历史备份怎么清
            <SettingsTip label="为什么推荐交给存储桶">
              推荐把删除权交给存储桶的过期规则。那样这把密钥不必带删除权限，服务器被攻破也删不掉备份。
            </SettingsTip>
          </span>
          <div
            class="settings-seg"
            role="group"
            aria-label="历史备份清理方式"
            data-testid="backup-retention"
          >
            <button
              type="button"
              :aria-pressed="form.retentionMode === 'lifecycle'"
              @click="setRetention('lifecycle')"
            >
              交给存储桶过期（推荐）
            </button>
            <button
              type="button"
              :aria-pressed="form.retentionMode === 'application'"
              @click="setRetention('application')"
            >
              由应用按份数清理
            </button>
          </div>
        </div>

        <div
          v-if="formError !== null"
          class="settings-banner is-danger is-wide"
          role="alert"
        >
          <div class="settings-banner-main">
            <p>{{ formError }}</p>
          </div>
        </div>
        <div
          v-if="verification !== null"
          class="settings-chips is-wide"
          data-testid="backup-verification"
        >
          <span class="settings-chip">✓ 连接正常</span>
          <span class="settings-chip">这把密钥{{ verification.can_delete ? "可以" : "不能" }}删除对象</span>
          <span
            v-if="lifecycleCheck !== null"
            class="settings-chip"
            :class="`is-${lifecycleCheck.tone}`"
          >
            {{ lifecycleCheck.text }}
            <SettingsTip
              v-if="lifecycleCheck.tip !== null"
              label="这是什么意思"
            >{{ lifecycleCheck.tip }}</SettingsTip>
          </span>
        </div>

        <div
          v-if="browser"
          class="is-wide"
        >
          <ServerManagementFields
            ref="proof"
            :disabled="busy"
          />
        </div>
        <details
          v-if="browser && configured"
          class="is-wide"
        >
          <summary>停用备份</summary>
          <p class="settings-muted">
            停用会移除服务器保存的目标与凭据；云端备份和记录仍保留。再次开启需重新填写。
          </p>
          <button
            class="button danger"
            type="button"
            :disabled="busy || !proof?.ready"
            data-testid="backup-disable"
            @click="disableBackup"
          >
            确认停用
          </button>
        </details>
        <div class="backup-actions is-wide">
          <button
            class="button secondary"
            type="button"
            :disabled="busy"
            data-testid="backup-test"
            @click="runTest"
          >
            测试连接
          </button>
          <button
            data-feedback-action="保存备份设置"
            class="button primary"
            type="submit"
            :disabled="!canSave"
            data-testid="backup-save"
          >
            保存
          </button>
          <button
            class="button quiet"
            type="button"
            :disabled="busy"
            @click="cancelEditing"
          >
            取消
          </button>
          <span
            v-if="verification === null"
            class="settings-muted"
          >测试通过后才能保存</span>
          <SettingsTip label="为什么要先测试">
            先测试再保存：验不过的配置不会覆盖正在用的那一份。改了任何一项都要重新测试。
          </SettingsTip>
        </div>
      </form>
    </template>
    <UnsavedChangesDialog
      :open="leave.open.value"
      :busy="editBusy"
      :blocked="leave.blocked.value"
      @keep="leave.keepEditing"
      @discard="leave.discard"
    />
  </div>
</template>

<style scoped>
.backup-status {
  display: grid;
  gap: 0;
}

.backup-overview {
  margin-bottom: 14px;
}

.backup-headline {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 4px 10px;
}

.backup-headline strong {
  color: var(--ink-strong);
  font-size: 17px;
}

.backup-headline-icon {
  display: grid;
  width: 24px;
  height: 24px;
  flex: none;
  place-items: center;
  border-radius: 50%;
  background: var(--accent-solid);
  color: #fff;
  font-size: 13px;
  font-weight: 700;
}

.backup-headline.is-warn .backup-headline-icon {
  background: var(--amber-text);
}

.backup-headline.is-danger .backup-headline-icon {
  background: var(--danger);
}

.backup-headline.is-run .backup-headline-icon {
  background: var(--link);
}

.backup-headline-next {
  margin-left: auto;
  color: var(--ink-soft);
  font-size: 13px;
  white-space: nowrap;
}

.backup-headline-detail {
  margin: 2px 0 0 34px;
  color: var(--ink-soft);
  font-size: 13px;
}

.backup-runs-empty {
  margin: 0;
}

.backup-facts {
  grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
  margin-top: 16px;
}

.backup-facts .backup-fact-sub {
  margin-top: 2px;
  color: var(--ink-soft);
  font-size: 12px;
  font-weight: 400;
}

.backup-fact-sub.is-ok {
  color: var(--safe-strong);
}

.backup-fact-sub.is-warn {
  color: var(--amber-text);
}

.backup-summary {
  margin-bottom: 14px;
}

.backup-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
}

.backup-note {
  margin-top: 10px;
}

.backup-hint {
  font-weight: 400;
}

.backup-field-title {
  display: flex;
  align-items: center;
  gap: 6px;
}

.settings-chip.is-ok {
  color: var(--safe-strong);
}

.settings-chip.is-warn {
  color: var(--amber-text);
}

.settings-chip {
  display: inline-flex;
  align-items: center;
  gap: 6px;
}

@media (max-width: 720px) {
  .backup-headline-next {
    flex-basis: 100%;
    margin-left: 34px;
  }

  .backup-headline-detail {
    margin-left: 0;
  }

  .backup-form {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

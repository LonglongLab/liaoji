<script setup lang="ts">
/**
 * 会话信息抽屉固定操作区里的「本机微信」（PSU-2.189）。
 *
 * 只在本机端出现：抽屉由视图按能力清单决定传不传 `state`，这里不判「是不是远程」。
 * 没同步不在入口挡——点开之后在面板里讲清楚、给出路；入口只挡「根本做不了」的
 * 没配对远端（暂时未配置，写明原因给入口）。微信没开也能查看面板、重新检查。
 *
 * 面板是第一道校验：按「保留最近」算建议范围，查这段是否全部已同步；
 * 已同步才给「打开微信并勾选」。第二道校验在微信旁的浮窗（PSU-2.188）。
 */
import { computed, inject, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterLink } from "vue-router";

import { ArchiveApiError } from "@/api/archiveApi";
import type {
  Job,
  WechatCleanupDrive,
  WechatCleanupKeepMonths,
  WechatCleanupReadiness,
} from "@/api/archiveApi";
import { archiveApiKey } from "@/api/injection";
import type { CapabilityState } from "@/api/archiveApi";
import {
  driveStopMessage,
  every,
  loadKeepMonths,
  saveKeepMonths,
} from "@/lib/wechatCleanupHint";
import { formatBytes } from "@/utils/format";

const props = defineProps<{
  conversationId: string;
  accountId: string;
  state: CapabilityState;
}>();

const KEEP_CHOICES: readonly WechatCleanupKeepMonths[] = [1, 3, 6, 12];
const STEP_LABELS: Record<string, string> = {
  wait_login: "等待微信登录",
  open_settings: "打开微信设置",
  open_storage: "进入「存储空间 → 管理」",
  open_chat_data: "进入「聊天数据 → 管理」（等数据加载）",
  search: "搜索这个会话",
  tick: "勾选，并核对勾上的就是它",
};
const TERMINAL_JOB = new Set(["completed", "failed", "cancelled", "interrupted", "paused"]);

const api = inject(archiveApiKey);
const readiness = ref<WechatCleanupReadiness | null>(null);
const loadError = ref(false);
const loading = ref(false);
const clockNow = ref(Date.now());
const checkStartedAt = ref(Date.now());
const syncStartedAt = ref(Date.now());
const syncCompleted = ref(false);
let stopClock: (() => void) | undefined;
const checkElapsed = computed(() => Math.max(0, Math.floor((clockNow.value - checkStartedAt.value) / 1000)));
const syncElapsed = computed(() => Math.max(0, Math.floor((clockNow.value - syncStartedAt.value) / 1000)));
let loadGeneration = 0;
const loadTimers = new Set<ReturnType<typeof globalThis.setTimeout>>();
const open = ref(false);
const keepMonths = ref<WechatCleanupKeepMonths>(loadKeepMonths());
const partial = ref(false);
const syncing = ref(false);
const syncFailed = ref(false);
// 正在跑的那条同步作业。点「先同步」时已经有一条整账号同步在跑，就跟着那一条等。
const syncJob = ref<Job | null>(null);
const followingAccountSync = ref(false);
const drive = ref<WechatCleanupDrive | null>(null);
const driveError = ref<string | null>(null);
let stopPolling: (() => void) | undefined;

const notConfigured = computed(() => props.state === "not_configured");
const pending = computed(() => readiness.value?.pending ?? null);
const hasPending = computed(
  () => (pending.value?.messages ?? 0) > 0 || (pending.value?.attachments ?? 0) > 0,
);
const unuploadable = computed(() => readiness.value?.unuploadable.count ?? 0);
const blocker = computed(() => readiness.value?.sync_blocker ?? null);
const disabledReason = computed((): string | null => {
  if (notConfigured.value) return "远程归档还没配置，本机记录还没有别处的副本，不能清。";
  return null;
});
const endDate = computed(() => readiness.value?.suggested_end ?? "");
// 放在脚本里而不是模板插值：界面文案句长守卫按模板里的字面量量长度（uiCopy.test.ts）。
const pendingLine = computed(() =>
  hasPending.value
    ? `还有 ${String(pending.value?.messages ?? 0)} 条消息、${String(pending.value?.attachments ?? 0)} 个文件没同步`
    : "建议范围里的文字和图片、视频、文件都已在远端",
);
// 正文按账号连续上传，跳不过别的会话；原图只传这个会话要清的那一段（PSU-2.190）。
const syncFileProgress = computed(() => {
  const job = syncJob.value;
  if (!syncing.value || followingAccountSync.value || job?.stage !== "originals") return null;
  const total = job.stage_progress_total;
  const done = job.stage_progress_current;
  if (total === null || total <= 0 || !Number.isFinite(done)) return null;
  // 作业开始时的 0/1 是阶段占位，并不是本次只剩一个文件。
  if (total === 1 && done === 0 && (pending.value?.attachments ?? 0) > 1) return null;
  const current = Math.max(0, Math.min(done, total));
  return { current, total, percent: Math.round(current / total * 100) };
});
const syncProgress = computed((): string => {
  if (followingAccountSync.value) return "等待正在运行的备份";
  const job = syncJob.value;
  if (job === null || job.status === "queued") return "正在准备同步";
  if (job.stage !== "originals") {
    return "核对与备份聊天文字";
  }
  if (syncFileProgress.value === null) return "正在准备文件";
  const done = job.stage_progress_current;
  const total = job.stage_progress_total;
  const count = total === null ? `${String(done)} 个` : `${String(done)} / ${String(total)}`;
  return `文件：${count}`;
});
const canDrive = computed(
  () =>
    readiness.value !== null &&
    !loading.value &&
    !loadError.value &&
    !syncing.value &&
    disabledReason.value === null &&
    blocker.value !== "import_stale" &&
    (!hasPending.value || partial.value),
);
const driving = computed(() => drive.value?.state === "running");
const activity = computed(() => {
  if (loading.value) return `正在检查 · ${String(checkElapsed.value)} 秒`;
  if (syncing.value) return `正在同步 · ${String(syncElapsed.value)} 秒`;
  if (driving.value) return "正在打开微信";
  return null;
});
const busy = computed(() => loading.value || syncing.value || driving.value);
const stateTitle = computed(() => {
  if (loading.value) return syncCompleted.value ? "正在复核备份" : "正在检查备份";
  if (loadError.value) return "检查未完成";
  if (disabledReason.value !== null) return "远程归档未配置";
  if (syncing.value) return "正在备份";
  if (driving.value) return drive.value?.step === "wait_login" ? "等待微信登录" : "正在打开微信";
  if (drive.value?.state === "stopped") {
    if (drive.value.reason === "cancelled") return "本次操作已取消";
    if (drive.value.reason === "login_timeout") return "等待登录已超时";
    return drive.value.reason === "wechat_not_found" ? "请打开微信主窗口" : "需要手动操作";
  }
  if (drive.value?.state === "completed") return "微信已准备好";
  if (blocker.value === "import_stale") return "需要先导入";
  if (syncCompleted.value && hasPending.value && !partial.value) return "备份未完成";
  if ((syncFailed.value || blocker.value === "remote_unavailable") && hasPending.value && !partial.value) return "同步未完成";
  if (hasPending.value && !partial.value) return "还有内容未备份";
  if (partial.value) return "已选安全范围";
  if (unuploadable.value > 0) return "未备份文件会保留";
  return "备份已确认";
});
const stateTone = computed(() => {
  if (busy.value) return "working";
  if (drive.value?.reason === "cancelled") return "neutral";
  if (loadError.value || disabledReason.value !== null || drive.value?.state === "stopped" || blocker.value === "import_stale" || (syncFailed.value && !partial.value)) return "error";
  if (hasPending.value && !partial.value) return syncCompleted.value ? "error" : "warn";
  return "ready";
});
const flowStates = computed(() => [
  loading.value ? "working" : loadError.value ? "error" : readiness.value !== null ? "done" : "todo",
  syncing.value ? "working" : readiness.value === null ? "todo" : hasPending.value && !partial.value ? "pending" : "done",
  driving.value ? "working" : drive.value?.state === "completed" ? "done" : drive.value?.state === "stopped" ? "error" : canDrive.value ? "pending" : "todo",
]);

async function load(): Promise<void> {
  if (api === undefined || notConfigured.value) return;
  const generation = ++loadGeneration;
  const conversationId = props.conversationId;
  const accountId = props.accountId;
  const months = keepMonths.value;
  loading.value = true;
  clockNow.value = Date.now();
  checkStartedAt.value = clockNow.value;
  loadError.value = false;
  readiness.value = null;
  let timeout: ReturnType<typeof globalThis.setTimeout> | undefined;
  try {
    const result = await Promise.race([
      api.getWechatCleanupReadiness(conversationId, accountId, months),
      new Promise<never>((_, reject) => {
        timeout = globalThis.setTimeout(() => reject(new Error("Readiness timeout")), 60_000);
        loadTimers.add(timeout);
      }),
    ]);
    if (generation === loadGeneration) readiness.value = result;
  } catch {
    if (generation === loadGeneration) loadError.value = true;
  } finally {
    if (timeout !== undefined) {
      globalThis.clearTimeout(timeout);
      loadTimers.delete(timeout);
    }
    if (generation === loadGeneration) loading.value = false;
  }
}

function chooseKeep(value: WechatCleanupKeepMonths): void {
  if (value === keepMonths.value) return;
  if (drive.value !== null) void cancelDrive();
  drive.value = null;
  keepMonths.value = value;
  saveKeepMonths(value);
  partial.value = false;
  syncCompleted.value = false;
  void load();
}

function stop(): void {
  stopPolling?.();
  stopPolling = undefined;
}

async function startScopedSync(): Promise<string | null> {
  if (api === undefined) return null;
  try {
    const { job_id: jobId } = await api.startWechatCleanupSync(
      props.conversationId,
      props.accountId,
      keepMonths.value,
    );
    return jobId;
  } catch (error) {
    // 已经有一条同步在跑（多半是自动同步）：它是整账号的，也会带上这个会话，跟着它等。
    if (
      error instanceof ArchiveApiError &&
      error.code === "JOB_ALREADY_RUNNING" &&
      error.jobId !== null
    ) {
      followingAccountSync.value = true;
      return error.jobId;
    }
    throw error;
  }
}

async function syncFirst(): Promise<void> {
  if (api === undefined) return;
  syncing.value = true;
  clockNow.value = Date.now();
  syncStartedAt.value = clockNow.value;
  syncCompleted.value = false;
  syncFailed.value = false;
  syncJob.value = null;
  followingAccountSync.value = false;
  try {
    const jobId = await startScopedSync();
    if (jobId === null) return;
    stop();
    stopPolling = every(() => {
      void (async () => {
        try {
          const job = await api.getJob(jobId, props.accountId);
          syncJob.value = job;
          if (!TERMINAL_JOB.has(job.status)) return;
          stop();
          syncing.value = false;
          followingAccountSync.value = false;
          syncFailed.value = job.status !== "completed";
          syncCompleted.value = job.status === "completed";
          partial.value = false;
          await load();
        } catch {
          stop();
          syncing.value = false;
          syncFailed.value = true;
        }
      })();
    }, 1_000);
  } catch {
    syncing.value = false;
    syncFailed.value = true;
  }
}

async function startDrive(): Promise<void> {
  if (api === undefined || !canDrive.value || driving.value) return;
  driveError.value = null;
  const conversation = props.conversationId;
  const account = props.accountId;
  try {
    const started = await api.startWechatCleanupDrive(
      conversation,
      account,
      keepMonths.value,
    );
    if (conversation !== props.conversationId || account !== props.accountId) {
      await api.cancelWechatCleanupDrive(started.session_id ?? undefined);
      return;
    }
    drive.value = started;
  } catch {
    driveError.value = "这台电脑上没法替你操作微信，请在微信里手动打开清理页。";
    return;
  }
  stop();
  stopPolling = every(() => {
    void (async () => {
      try {
        const current = await api.getWechatCleanupDrive();
        if (current.conversation_id !== props.conversationId || (current.account_id != null && current.account_id !== props.accountId)
          || (drive.value?.session_id != null && current.session_id !== drive.value.session_id)) { stop(); return; }
        drive.value = current;
        if (drive.value.state !== "running") stop();
      } catch {
        stop();
      }
    })();
  }, 500);
}

function isCurrentDrive(session: string | null | undefined): boolean {
  return drive.value !== null && drive.value.session_id === session;
}

async function cancelDrive(): Promise<void> {
  if (api === undefined || drive.value === null) return;
  const current = drive.value;
  try {
    await api.cancelWechatCleanupDrive(current.session_id ?? undefined);
    if (isCurrentDrive(current.session_id)) {
      stop(); drive.value = { ...current, state: "stopped", reason: "cancelled" };
    }
  } catch { driveError.value = "没能取消，请再试一次。"; }
}

function stepState(step: string): "done" | "doing" | "fail" | "todo" {
  const current = drive.value;
  if (current === null) return "todo";
  if (current.steps_done.includes(step as never)) return "done";
  if (current.state === "stopped" && current.stopped_at === step) return "fail";
  if (current.state === "running" && current.step === step) return "doing";
  return "todo";
}

onMounted(() => {
  stopClock = every(() => { clockNow.value = Date.now(); }, 1_000);
  void load();
});
watch(() => [props.conversationId, props.accountId, props.state], () => {
  if (drive.value !== null) void cancelDrive();
  ++loadGeneration;
  stop();
  readiness.value = null;
  loading.value = false;
  loadError.value = false;
  open.value = false;
  partial.value = false;
  syncing.value = false;
  syncFailed.value = false;
  syncCompleted.value = false;
  syncJob.value = null;
  drive.value = null;
  driveError.value = null;
  void load();
});
onBeforeUnmount(() => {
  ++loadGeneration;
  for (const timer of loadTimers) globalThis.clearTimeout(timer);
  loadTimers.clear();
  stop();
  stopClock?.();
});
</script>

<template>
  <section
    class="info-section wechat-cleanup"
    aria-label="本机微信"
  >
    <div class="wechat-cleanup-heading">
      <strong>{{ open ? "清理聊天文件" : "本机微信" }}</strong>
      <small
        v-if="!open && activity !== null"
        class="wechat-cleanup-activity"
      >{{ activity }}</small>
      <button
        v-if="open"
        class="text-action"
        type="button"
        @click="open = false"
      >
        收起
      </button>
    </div>
    <button
      v-if="!open"
      class="button secondary wechat-cleanup-open"
      type="button"
      :disabled="notConfigured"
      @click="open = true"
    >
      清理微信聊天文件
    </button>
    <p
      v-if="!open && notConfigured"
      class="wechat-cleanup-why"
    >
      远程归档未配置
      <RouterLink to="/pairing">
        去配置
      </RouterLink>
    </p>
    <p
      v-else-if="!open"
      class="wechat-cleanup-fine"
    >
      检查备份 → 微信确认
    </p>

    <div
      v-if="open"
      class="wechat-cleanup-panel"
      role="group"
      aria-label="在微信中清理"
    >
      <div class="wechat-cleanup-scroll">
        <ol
          class="wechat-cleanup-flow"
          aria-label="清理流程"
        >
          <li
            v-for="(label, index) in ['检查备份', '补齐备份', '微信确认']"
            :key="label"
            :data-state="flowStates[index]"
            :aria-current="flowStates[index] === 'working' || flowStates[index] === 'pending' ? 'step' : undefined"
          >
            <span aria-hidden="true">{{ flowStates[index] === 'done' ? '✓' : index + 1 }}</span>
            {{ label }}
          </li>
        </ol>
        <div class="wechat-cleanup-field">
          <span>保留最近</span>
          <div class="wechat-cleanup-seg">
            <button
              v-for="choice in KEEP_CHOICES"
              :key="choice"
              type="button"
              :class="{ on: choice === keepMonths }"
              :aria-pressed="choice === keepMonths"
              :disabled="syncing || driving"
              @click="chooseKeep(choice)"
            >
              {{ choice === 12 ? "1 年" : `${choice} 个月` }}
            </button>
          </div>
        </div>
        <div
          class="wechat-cleanup-state"
          :class="{ 'wechat-cleanup-loading': loading }"
          :data-tone="stateTone"
          role="status"
        >
          <span
            class="wechat-cleanup-symbol"
            :class="{ spinning: busy }"
            aria-hidden="true"
          >
            <svg
              v-if="!busy"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
            >
              <template v-if="stateTone === 'ready'">
                <path d="M12 3 4 6v5c0 5 8 10 8 10s8-5 8-10V6Z" />
                <path d="m8 12 3 3 5-6" />
              </template>
              <template v-else>
                <circle
                  cx="12"
                  cy="12"
                  r="9"
                />
                <path d="M12 7v6m0 3v1" />
              </template>
            </svg>
          </span>
          <strong>{{ stateTitle }}</strong>
          <small
            v-if="loading"
            aria-live="off"
          >{{ checkElapsed }} 秒</small>
          <small
            v-else-if="syncing"
            aria-live="off"
          >{{ syncElapsed }} 秒</small>
        </div>
        <p
          v-if="loading && checkElapsed >= 5"
          class="wechat-cleanup-fine wechat-cleanup-slow"
        >
          可以收起，后台继续
        </p>
        <div
          v-if="readiness !== null && hasPending && !partial"
          class="wechat-cleanup-amount"
        >
          <span v-if="(pending?.messages ?? 0) > 0"><b>{{ pending?.messages }}</b> 条文字</span>
          <span v-if="(pending?.attachments ?? 0) > 0"><b>{{ pending?.attachments }}</b> 个文件</span>
          <small v-if="(pending?.attachment_bytes ?? 0) > 0">{{ formatBytes(pending?.attachment_bytes ?? 0) }}</small>
        </div>
        <div
          v-if="readiness !== null && (!hasPending || partial)"
          class="wechat-cleanup-good wechat-cleanup-date"
        >
          <span>可清理至</span><time>{{ endDate }}</time>
        </div>
        <p
          v-if="syncing"
          class="wechat-cleanup-fine wechat-cleanup-progress"
        >
          {{ syncProgress }}
        </p>
        <div
          v-if="syncing"
          class="wechat-cleanup-transfer"
          :class="{ 'is-indeterminate': syncFileProgress === null }"
          role="progressbar"
          aria-label="文件同步进度"
          :aria-valuemin="0"
          :aria-valuemax="syncFileProgress?.total"
          :aria-valuenow="syncFileProgress?.current"
        >
          <span :style="syncFileProgress === null ? undefined : { width: `${syncFileProgress.percent}%` }" />
        </div>


        <button
          v-if="readiness !== null && hasPending && !partial && blocker !== 'import_stale'"
          class="text-action wechat-cleanup-partial"
          type="button"
          :disabled="syncing || disabledReason !== null"
          @click="partial = true"
        >
          只清已备份部分 <small>截至 {{ readiness.synced_until }}</small>
        </button>
        <div
          v-if="drive?.state === 'completed'"
          class="wechat-cleanup-confirmation"
        >
          <div><b>1</b><span>选结束日期</span><time>{{ endDate }}</time></div>
          <div><b>2</b><span>在微信点清理</span><span aria-hidden="true">›</span></div>
        </div>

        <details class="wechat-cleanup-details">
          <summary>备份详情与操作说明</summary>
          <p
            v-if="loadError"
            class="wechat-cleanup-why"
          >
            读不出这个会话的同步情况，请重新检查。
          </p>
          <p
            v-if="disabledReason !== null"
            class="wechat-cleanup-why"
          >
            {{ disabledReason }}
          </p>
          <template v-if="readiness !== null">
            <dl class="wechat-cleanup-facts">
              <div><dt>本机微信里占用</dt><dd>{{ formatBytes(readiness.local_media_bytes) }}</dd></div>
              <div><dt>同步到远端</dt><dd>截至 {{ readiness.synced_until }}</dd></div>
            </dl>
            <p class="wechat-cleanup-fine">
              清理图片、视频、文件，聊天文字保留。
            </p>
            <ul class="wechat-cleanup-checks">
              <li :data-ok="!hasPending">
                {{ pendingLine }}
              </li>
              <li
                v-if="unuploadable > 0"
                data-ok="false"
              >
                有 {{ unuploadable }} 个文件传不上去，远端永远不会有它们的副本
              </li>
              <li
                v-if="readiness.source_missing_count > 0"
                data-ok="true"
              >
                微信里没有源文件的 {{ readiness.source_missing_count }} 个不算
              </li>
            </ul>
            <p
              v-if="blocker === 'import_stale'"
              class="wechat-cleanup-bad"
            >
              最近一次导入早于 {{ endDate }}。先导入一次再来。
            </p>
            <template v-else-if="hasPending && !partial">
              <p
                v-if="syncCompleted"
                class="wechat-cleanup-bad wechat-cleanup-incomplete"
              >
                同步已结束，仍有文件没有备份成功。可以只清已备份部分。
                <RouterLink to="/sync-health">
                  查看原因
                </RouterLink>
              </p>
              <p
                v-else-if="blocker === 'remote_unavailable' || syncFailed"
                class="wechat-cleanup-bad"
              >
                要清的范围里有没同步的，现在同步不了。
              </p>
              <p
                v-else
                class="wechat-cleanup-warn"
              >
                先同步它们，才能清到 {{ readiness.keep_cutoff }}。
              </p>
              <button
                v-if="syncCompleted && blocker !== 'remote_unavailable'"
                class="text-action"
                type="button"
                :disabled="syncing"
                @click="syncFirst"
              >
                再试一次
              </button>
            </template>
            <p
              v-else-if="unuploadable > 0"
              class="wechat-cleanup-bad"
            >
              结束日期已提前到 {{ endDate }}，传不上去的那些留在微信里。
            </p>
            <button
              v-if="disabledReason === null"
              class="text-action wechat-cleanup-retry"
              type="button"
              :disabled="loading || syncing || driving"
              @click="load"
            >
              重新检查
            </button>
            <p
              v-if="driveError !== null"
              class="wechat-cleanup-bad"
            >
              {{ driveError }}
            </p>
            <ol
              v-if="drive !== null"
              class="wechat-cleanup-steps"
            >
              <li
                v-for="(label, step) in STEP_LABELS"
                :key="step"
                :data-state="stepState(step)"
              >
                {{ label }}
              </li>
            </ol>
            <p
              v-if="drive?.state === 'stopped'"
              class="wechat-cleanup-bad"
            >
              {{ driveStopMessage(drive.reason) }}
            </p>
            <p
              v-if="drive?.state === 'completed'"
              class="wechat-cleanup-fine"
            >
              <template v-if="drive.hint_window">
                剩下两步在微信里：看微信旁边的小浮窗。
              </template>
              <template v-else>
                时间选自定义，开始日期选最早。结束日期不晚于 {{ endDate }}，最后在微信点清理。
              </template>
            </p>
          </template>
        </details>
      </div>
      <div class="wechat-cleanup-actions">
        <button
          v-if="loadError && !loading"
          class="button primary wechat-cleanup-retry"
          type="button"
          @click="load"
        >
          重新检查
        </button>
        <template v-else-if="readiness !== null">
          <button
            v-if="disabledReason !== null"
            class="button primary wechat-cleanup-retry"
            type="button"
            @click="load"
          >
            重新检查
          </button>
          <RouterLink
            v-else-if="blocker === 'import_stale'"
            class="button primary"
            to="/overview"
          >
            先导入
          </RouterLink>
          <template v-else-if="hasPending && !partial">
            <RouterLink
              v-if="syncCompleted || syncFailed || blocker === 'remote_unavailable'"
              class="button primary"
              to="/sync-health"
            >
              查看原因
            </RouterLink>
            <button
              v-else
              class="button primary wechat-cleanup-go"
              type="button"
              :disabled="syncing"
              @click="syncFirst"
            >
              {{ syncing ? "正在备份" : "先同步，再清理" }}
            </button>
          </template>
          <button
            v-else-if="driving"
            class="button secondary"
            type="button"
            @click="cancelDrive"
          >
            {{ drive?.step === "wait_login" ? "取消等待" : "取消操作" }}
          </button>
          <button
            v-else-if="drive?.state !== 'completed'"
            class="button primary wechat-cleanup-go"
            type="button"
            :disabled="!canDrive || driving"
            @click="startDrive"
          >
            {{ driving ? "正在打开微信" : drive?.state === 'stopped' ? "重试打开微信" : "去微信清理" }}
          </button>
        </template>
      </div>
    </div>
  </section>
</template>

<style scoped>
.wechat-cleanup-heading {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}
.wechat-cleanup-heading strong { font-family: var(--font-display); font-size: 14px; font-weight: 700; }
.wechat-cleanup-heading .text-action {
  min-height: 44px;
  padding: 4px 7px;
  border-radius: var(--radius-btn);
  color: var(--ink-faint);
  font-size: 12px;
  text-decoration: none;
}
.wechat-cleanup-activity { color: var(--accent-ink); font-size: 12px; }
.wechat-cleanup-open { width: 100%; margin-top: 8px; }
.wechat-cleanup-panel {
  display: flex;
  min-height: 0;
  max-height: 48dvh;
  flex-direction: column;
  margin-top: 10px;
  overflow: hidden;
}
.wechat-cleanup-scroll {
  min-height: 0;
  overflow-y: auto;
  overscroll-behavior: contain;
}
.wechat-cleanup-flow {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 0;
  margin: 0 0 13px;
  padding: 0;
  list-style: none;
}
.wechat-cleanup-flow li {
  position: relative;
  display: flex;
  align-items: center;
  flex-direction: column;
  gap: 5px;
  padding: 0;
  color: var(--ink-faint);
  font-size: 12px;
  white-space: nowrap;
}
.wechat-cleanup-flow li span {
  position: relative;
  z-index: 1;
  display: grid;
  width: 21px;
  height: 21px;
  flex: none;
  border: 1px solid var(--rule);
  border-radius: 50%;
  background: var(--surface);
  place-items: center;
}
.wechat-cleanup-flow li:not(:last-child)::after {
  position: absolute;
  top: 10px;
  right: calc(-50% + 14px);
  left: calc(50% + 14px);
  height: 1px;
  background: var(--rule);
  content: "";
}
.wechat-cleanup-flow li[data-state="done"],
.wechat-cleanup-flow li[data-state="working"],
.wechat-cleanup-flow li[data-state="pending"] { color: var(--accent-ink); }
.wechat-cleanup-flow li[data-state="done"] span {
  border-color: var(--accent-ink);
  background: var(--accent-solid);
  color: var(--paper);
}
.wechat-cleanup-flow li[data-state="done"]::after { background: var(--safe-border); }
.wechat-cleanup-flow li[data-state="working"] span,
.wechat-cleanup-flow li[data-state="pending"] span {
  border-color: var(--safe-border);
  background: var(--safe-bg);
}
.wechat-cleanup-flow li[data-state="error"] { color: var(--danger); }
.wechat-cleanup-field > span { color: var(--ink-faint); font-size: 12px; }
.wechat-cleanup-seg {
  display: flex;
  gap: 2px;
  margin-top: 7px;
  padding: 3px;
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
}
.wechat-cleanup-seg button {
  min-width: 0;
  min-height: 44px;
  flex: 1;
  padding: 5px 0;
  border: 1px solid transparent;
  border-radius: var(--radius-btn);
  background: transparent;
  color: var(--ink-soft);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}
.wechat-cleanup-seg button.on {
  border-color: var(--rule);
  background: var(--paper);
  color: var(--accent-ink);
  font-weight: 600;

}
.wechat-cleanup-seg button:focus-visible,
.wechat-cleanup-details summary:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.wechat-cleanup-state {
  display: flex;
  min-height: 68px;
  align-items: center;
  justify-content: flex-start;
  flex-wrap: wrap;
  gap: 10px;
  margin-top: 12px;
  padding: 12px;
  border-radius: var(--radius-card);
  background: var(--safe-bg);
  color: var(--accent-ink);
}
.wechat-cleanup-state strong { font-size: 18px; font-weight: 700; }
.wechat-cleanup-state small { margin-left: auto; color: var(--ink-soft); font-variant-numeric: tabular-nums; }
.wechat-cleanup-state[data-tone="error"] { background: var(--danger-bg); color: var(--danger); }
.wechat-cleanup-state[data-tone="neutral"] { background: var(--surface-sunken); color: var(--ink-soft); }
.wechat-cleanup-state[data-tone="warn"] { background: var(--amber-bg); color: var(--amber-text); }
.wechat-cleanup-symbol {
  display: grid;
  width: 36px;
  height: 36px;
  flex: none;
  border-radius: var(--radius-card);
  background: var(--surface-pop);
  place-items: center;
}
.wechat-cleanup-symbol svg { width: 25px; height: 25px; }
.wechat-cleanup-symbol.spinning {
  width: 24px;
  height: 24px;
  margin: 6px;
  border: 2px solid currentColor;
  border-right-color: transparent;
  border-radius: 50%;
  background: transparent;
  animation: cleanup-spin 1s linear infinite;
}
@keyframes cleanup-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .wechat-cleanup-symbol.spinning { animation: none; } }
.wechat-cleanup-amount {
  display: flex;
  justify-content: center;
  align-items: baseline;
  flex-wrap: wrap;
  gap: 8px;
  margin: 10px 0;
  color: var(--ink-soft);
  font-size: 13px;
}
.wechat-cleanup-transfer {
  height: 6px;
  margin-top: 8px;
  overflow: hidden;
  border-radius: var(--radius-pill);
  background: var(--track);
}
.wechat-cleanup-transfer span {
  display: block;
  height: 100%;
  border-radius: inherit;
  background: var(--accent);
  transition: width 160ms ease;
}
.wechat-cleanup-transfer.is-indeterminate span {
  width: 30%;
  animation: cleanup-transfer 1.3s ease-in-out infinite;
}
@keyframes cleanup-transfer {
  from { transform: translateX(-100%); }
  to { transform: translateX(340%); }
}
@media (prefers-reduced-motion: reduce) {
  .wechat-cleanup-transfer.is-indeterminate span { animation: none; }
}
.wechat-cleanup-amount b { color: var(--ink); font-size: 24px; font-variant-numeric: tabular-nums; }
.wechat-cleanup-date {
  display: flex;
  justify-content: space-between;
  align-items: center;
  gap: 8px;
  margin-top: 10px;
  padding: 10px 12px;
  border-top: 1px solid var(--rule);
  color: var(--ink-soft);
  font-size: 12px;
}
.wechat-cleanup-date time { color: var(--accent-ink); font-size: 16px; font-weight: 700; font-variant-numeric: tabular-nums; }
.wechat-cleanup-state[data-tone="ready"] { border-radius: var(--radius-card) var(--radius-card) 0 0; }
.wechat-cleanup-state[data-tone="ready"] + .wechat-cleanup-date {
  margin-top: 0;
  border-top-color: var(--safe-border);
  border-radius: 0 0 var(--radius-card) var(--radius-card);
  background: var(--safe-bg);
}
.wechat-cleanup-actions { flex: none; }
.wechat-cleanup-actions > .button {
  display: flex;
  width: 100%;
  justify-content: center;
  align-items: center;
  gap: 14px;
  margin: 10px 0 0;
  text-decoration: none;

}
.wechat-cleanup-actions > .button::after { font-size: 21px; line-height: 1; content: "›"; }
.wechat-cleanup-partial { width: 100%; margin-top: 8px; text-decoration: none; }
.wechat-cleanup-partial small { display: block; margin-top: 3px; color: var(--ink-faint); font-size: 12px; }
.wechat-cleanup-details { margin-top: 14px; color: var(--ink-soft); font-size: 12px; }
.wechat-cleanup-details summary { cursor: pointer; }
.wechat-cleanup-details[open] summary { margin-bottom: 10px; }
.wechat-cleanup-facts { margin: 0; }
.wechat-cleanup-facts div { display: flex; justify-content: space-between; font-size: 13px; }
.wechat-cleanup-facts dt { color: var(--ink-soft); }
.wechat-cleanup-facts dd { margin: 0; }
.wechat-cleanup-fine { margin: 6px 0 0; color: var(--ink-faint); font-size: 12px; }
.wechat-cleanup-why { margin: 6px 0 0; color: var(--amber-text); font-size: 12px; }
.wechat-cleanup-checks { margin: 8px 0 0; padding-left: 18px; font-size: 12px; }
.wechat-cleanup-checks li[data-ok="false"] { color: var(--amber-text); }
.wechat-cleanup-warn,
.wechat-cleanup-bad { margin: 8px 0 0; padding: 7px 9px; border-radius: 8px; font-size: 12px; }
.wechat-cleanup-warn { background: var(--amber-bg); color: var(--amber-text); }
.wechat-cleanup-bad { background: var(--danger-bg); color: var(--danger); }
.wechat-cleanup-retry { margin: 8px 0; }
.wechat-cleanup-steps { margin: 8px 0 0; padding-left: 18px; color: var(--ink-faint); font-size: 12px; }
.wechat-cleanup-steps li[data-state="done"] { color: var(--ink-soft); }
.wechat-cleanup-steps li[data-state="doing"] { color: var(--ink); font-weight: 600; }
.wechat-cleanup-steps li[data-state="fail"] { color: var(--danger); }
.wechat-cleanup-confirmation { display: grid; gap: 6px; margin-top: 12px; }
.wechat-cleanup-confirmation div { display: flex; align-items: center; gap: 8px; padding: 9px; border: 1px solid var(--rule); border-radius: 8px; font-size: 12px; }
.wechat-cleanup-confirmation time,
.wechat-cleanup-confirmation div > span:last-child { margin-left: auto; }
</style>

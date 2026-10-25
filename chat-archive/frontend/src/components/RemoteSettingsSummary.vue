<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed } from "vue";

import type {
  BackupConfig,
  BackupRunList,
  ObjectStorageConfig,
  SyncHealth,
} from "@/api/archiveApi";
import { useCapabilityStore } from "@/stores/capabilities";
import { useArchiveApi } from "@/api/injection";
import { formatWechatTimestamp, isSameLocalDay, formatTimeOfDay } from "@/utils/time";

// 远程与备份分类的一级页：「数据怎么走」概览 + 「附件存储」「定期备份」两行，点进二级页。
// 这里的查询与两个二级页共用同一组 queryKey / queryFn：进出二级页不重复请求，
// 状态也不会一处说正常、一处说出错。读不到的一律画成中性，不当成「正常」。
const props = withDefaults(defineProps<{
  accountId?: string | null;
  mode?: "manage" | "browser";
  showsObjectStorage: boolean;
  showsBackup: boolean;
}>(), { accountId: null, mode: "manage" });
const emit = defineEmits<{ open: [item: "object-storage" | "backup"] }>();

const api = useArchiveApi();
const browser = computed(() => props.mode === "browser");
const capabilities = browser.value ? useCapabilityStore() : null;

const healthQuery = useQuery({
  queryKey: computed(() => ["object-storage-blob-count", props.accountId] as const),
  enabled: computed(() => !browser.value && props.accountId !== null),
  queryFn: (): Promise<SyncHealth> => {
    if (props.accountId === null) throw new Error("尚未选择账号");
    return api.getSyncHealth(props.accountId);
  },
  retry: false,
});

const backendType = computed(() => healthQuery.data.value?.backend_type ?? null);
const supportsManagedObjectStorage = computed(
  () => browser.value ? capabilities?.states["object-storage-config"] !== "unsupported" : backendType.value === "self_hosted_sqlite" || backendType.value === "self_hosted_postgresql",
);
const supportsBackup = computed(() => browser.value ? capabilities?.states["backup-management"] === "available" : backendType.value === "self_hosted_sqlite");

const objectStorageQuery = useQuery({
  queryKey: computed(
    () => ["object-storage-config", healthQuery.data.value?.archive_id ?? null] as const,
  ),
  enabled: computed(() => props.showsObjectStorage && supportsManagedObjectStorage.value),
  queryFn: (): Promise<ObjectStorageConfig> => api.getObjectStorageConfig(),
  retry: false,
});

const backupConfigQuery = useQuery({
  queryKey: computed(() => ["backup-config", props.accountId] as const),
  enabled: computed(() => props.showsBackup && supportsBackup.value),
  queryFn: (): Promise<BackupConfig> => api.getBackupConfig(),
  retry: false,
});

const backupRunsQuery = useQuery({
  queryKey: computed(() => ["backup-runs", props.accountId] as const),
  enabled: computed(() => props.showsBackup && supportsBackup.value),
  queryFn: (): Promise<BackupRunList> => api.listBackupRuns(),
  retry: false,
});

type Tone = "ok" | "warn" | "danger" | "run" | "unknown";
type Line = "live" | "off" | "warn" | "cut" | "unknown";
interface NodeState {
  tone: Tone;
  label: string;
  line: Line;
}

const LOADING: NodeState = { tone: "unknown", label: "正在读取…", line: "unknown" };
const UNREADABLE: NodeState = { tone: "unknown", label: "读不到状态", line: "unknown" };

const REGION_NAMES: Readonly<Record<string, string>> = {
  "ap-guangzhou": "广州",
  "ap-shanghai": "上海",
  "ap-beijing": "北京",
  "ap-nanjing": "南京",
  "ap-chengdu": "成都",
  "ap-chongqing": "重庆",
  "ap-hongkong": "中国香港",
  "ap-singapore": "新加坡",
};
const regionName = (region: string | null | undefined): string =>
  region === null || region === undefined ? "" : (REGION_NAMES[region] ?? region);

const server = computed<NodeState>(() => {
  if (browser.value) return { tone: "unknown", label: "当前服务器", line: "unknown" };
  if (props.accountId === null) return { tone: "unknown", label: "未选择账号", line: "unknown" };
  if (healthQuery.isLoading.value) return LOADING;
  const health = healthQuery.data.value;
  if (health === undefined) return UNREADABLE;
  switch (health.remote_service_status) {
    case "healthy":
      return { tone: "ok", label: "服务正常", line: "live" };
    case "degraded":
      return { tone: "warn", label: "服务有异常", line: "warn" };
    case "unavailable":
      return { tone: "danger", label: "连不上", line: "cut" };
    case "not_configured":
      return { tone: "warn", label: "还没接入", line: "off" };
  }
  return UNREADABLE;
});

const objectStorage = computed<NodeState>(() => {
  if (props.accountId !== null && healthQuery.isLoading.value) return LOADING;
  if (!supportsManagedObjectStorage.value) return UNREADABLE;
  if (objectStorageQuery.isLoading.value) return LOADING;
  const config = objectStorageQuery.data.value;
  if (config === undefined) return UNREADABLE;
  if (!config.configured) return { tone: "warn", label: "未配置", line: "off" };
  if (config.status === "healthy") return { tone: "ok", label: "正常", line: "live" };
  return { tone: "warn", label: "能力有缺口", line: "warn" };
});

const objectStorageDetail = computed(() => {
  const config = objectStorageQuery.data.value;
  if (config === undefined || !config.configured) return null;
  const place = regionName(config.region);
  const provider = config.provider === "minio" ? "MinIO" : config.provider === "s3_compatible" ? "S3 兼容服务" : "腾讯云 COS";
  return place === "" ? provider : `${provider} · ${place}`;
});

const backupLocked = computed(
  () =>
    backupConfigQuery.data.value?.credentials_readable === false ||
    backupRunsQuery.data.value?.last_backup_status === "locked",
);

const formatLastSuccess = (value: number): string =>
  isSameLocalDay(value, Date.now() / 1000)
    ? `今天 ${formatTimeOfDay(value)}`
    : formatWechatTimestamp(value);

const backup = computed<NodeState>(() => {
  if (props.accountId !== null && healthQuery.isLoading.value) return LOADING;
  if (!supportsBackup.value) return UNREADABLE;
  if (backupConfigQuery.isLoading.value || backupRunsQuery.isLoading.value) return LOADING;
  const config = backupConfigQuery.data.value;
  if (config === undefined) return UNREADABLE;
  if (!config.configured) return { tone: "warn", label: "未配置", line: "off" };
  if (backupLocked.value) return { tone: "danger", label: "凭据解不开，已停", line: "cut" };
  const runs = backupRunsQuery.data.value;
  if (runs === undefined) return UNREADABLE;
  if (runs.backup_overdue) return { tone: "warn", label: "两个周期没成功", line: "cut" };
  switch (runs.last_backup_status) {
    case "running":
      return { tone: "run", label: "正在备份", line: "live" };
    case "success":
      return {
        tone: "ok",
        label: runs.last_backup_at === null ? "上一轮成功" : `${formatLastSuccess(runs.last_backup_at)} 成功`,
        line: "live",
      };
    case "failed":
      return { tone: "warn", label: "上一轮失败", line: "warn" };
    case "never":
      return { tone: "unknown", label: "还没有跑过", line: "unknown" };
    case "not_configured":
      return { tone: "warn", label: "未配置", line: "off" };
    case "locked":
      return { tone: "danger", label: "凭据解不开，已停", line: "cut" };
  }
  return UNREADABLE;
});

const backupFrequency = computed(() => {
  const config = backupConfigQuery.data.value;
  if (config === undefined || !config.configured) return null;
  if (config.interval_hours === 24) return "每天一次";
  if (config.interval_hours === 168) return "每周一次";
  return `每 ${String(config.interval_hours)} 小时`;
});

const backupLifecycleMissing = computed(
  () => backupRunsQuery.data.value?.backup_lifecycle_covered === false,
);

const pillClass = (tone: Tone): string => (tone === "unknown" ? "settings-pill" : `settings-pill is-${tone}`);
</script>

<template>
  <div class="remote-summary">
    <div
      v-if="showsBackup && backupLocked"
      class="settings-banner is-danger"
      role="alert"
      data-testid="remote-backup-locked"
    >
      <div class="settings-banner-main">
        <strong>备份停了：服务器解不开存着的凭据</strong>
        <p>常见于恢复之后，重新填一次密钥即可。</p>
      </div>
      <div class="settings-banner-actions">
        <button
          class="button primary"
          type="button"
          @click="emit('open', 'backup')"
        >
          去重新填写
        </button>
      </div>
    </div>

    <div
      class="settings-overview"
      data-testid="remote-flow"
    >
      <div class="settings-overview-title">
        <span>数据怎么走</span>
      </div>
      <div
        class="remote-flow"
        :class="{ 'is-single': !(showsObjectStorage && showsBackup), 'is-browser': browser }"
      >
        <div
          v-if="!browser"
          class="remote-flow-node"
        >
          <b>这台电脑</b>
          <small>解析、搜索、导出</small>
        </div>
        <span
          v-if="!browser"
          class="remote-flow-line"
          :class="`is-${server.line}`"
          data-testid="remote-flow-line-server"
          :data-state="server.line"
          aria-hidden="true"
        />
        <div
          class="remote-flow-node"
          data-testid="remote-flow-server"
        >
          <b>你的服务器</b>
          <small><span :class="pillClass(server.tone)">{{ server.label }}</span></small>
        </div>
        <div class="remote-flow-stack remote-flow-lines">
          <span
            v-if="showsObjectStorage"
            class="remote-flow-line"
            :class="`is-${objectStorage.line}`"
            data-testid="remote-flow-line-object-storage"
            :data-state="objectStorage.line"
            aria-hidden="true"
          />
          <span
            v-if="showsBackup"
            class="remote-flow-line"
            :class="`is-${backup.line}`"
            data-testid="remote-flow-line-backup"
            :data-state="backup.line"
            aria-hidden="true"
          />
        </div>
        <div class="remote-flow-stack">
          <button
            v-if="showsObjectStorage"
            class="remote-flow-node"
            type="button"
            data-testid="remote-flow-object-storage"
            @click="emit('open', 'object-storage')"
          >
            <b>附件存储</b>
            <small><span :class="pillClass(objectStorage.tone)">{{ objectStorage.tone === "ok" && objectStorageDetail !== null ? objectStorageDetail : objectStorage.label }}</span></small>
          </button>
          <button
            v-if="showsBackup"
            class="remote-flow-node"
            type="button"
            data-testid="remote-flow-backup"
            @click="emit('open', 'backup')"
          >
            <b>定期备份</b>
            <small><span :class="pillClass(backup.tone)">{{ backup.label }}</span></small>
          </button>
        </div>
      </div>
    </div>

    <section class="settings-group">
      <h3 class="settings-group-title">
        远程归档
      </h3>
      <div class="settings-list">
        <button
          v-if="showsObjectStorage"
          class="settings-row"
          type="button"
          data-testid="open-object-storage"
          @click="emit('open', 'object-storage')"
        >
          <span class="settings-row-main">
            <span class="settings-row-title">附件存储</span>
            <span
              class="settings-row-sub"
              data-testid="object-storage-row-status"
            >
              <span
                v-if="objectStorage.label === '未配置'"
                class="settings-pill is-warn"
              >未配置 · 图片、语音、文件不会上传</span>
              <template v-else>
                <span :class="pillClass(objectStorage.tone)">{{ objectStorage.label }}</span>
                <span v-if="objectStorageDetail !== null">{{ objectStorageDetail }}</span>
              </template>
            </span>
          </span>
          <span
            class="settings-row-chevron"
            aria-hidden="true"
          >›</span>
        </button>
        <button
          v-if="showsBackup"
          class="settings-row"
          type="button"
          data-testid="open-backup"
          @click="emit('open', 'backup')"
        >
          <span class="settings-row-main">
            <span class="settings-row-title">定期备份</span>
            <span
              class="settings-row-sub"
              data-testid="backup-row-status"
            >
              <span :class="pillClass(backup.tone)">{{ backup.label }}</span>
              <span v-if="backupFrequency !== null">{{ backupFrequency }}</span>
              <span
                v-if="backupLifecycleMissing"
                class="remote-summary-warn"
              >旧备份不会自动清理</span>
            </span>
          </span>
          <span
            class="settings-row-chevron"
            aria-hidden="true"
          >›</span>
        </button>
      </div>
    </section>
  </div>
</template>

<style scoped>
.remote-flow.is-browser { grid-template-columns: minmax(0, 1fr) 32px minmax(0, 1fr); }

.remote-flow {
  display: grid;
  align-items: center;
  grid-template-columns: minmax(0, 1fr) 48px minmax(0, 1fr) 48px minmax(0, 1fr);
}

.remote-flow-node {
  min-width: 0;
  padding: 12px;
  border: 1px solid var(--rule);
  border-radius: 12px;
  background: var(--surface-pop);
  color: var(--ink);
  font: inherit;
  text-align: center;
}

button.remote-flow-node {
  cursor: pointer;
}

button.remote-flow-node:hover,
button.remote-flow-node:focus-visible {
  border-color: var(--link);
}

.remote-flow-node b {
  display: block;
  font-size: 13px;
}

.remote-flow-node small {
  display: flex;
  justify-content: center;
  margin-top: 3px;
}

.remote-flow-stack {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.remote-flow-lines {
  align-self: stretch;
  justify-content: space-around;
  gap: 0;
}

.remote-flow.is-single .remote-flow-lines {
  justify-content: center;
}

.remote-flow-line {
  --line-color: var(--track);
  --line-style: solid;
  position: relative;
  display: block;
  height: 0;
  margin: 0 8px;
  border-top: 2px var(--line-style) var(--line-color);
}

.remote-flow-line::after {
  position: absolute;
  top: -6px;
  right: -1px;
  border: 5px solid transparent;
  border-right: 0;
  border-left-color: var(--line-color);
  content: "";
}

.remote-flow-line.is-live {
  --line-color: #2f8a4c;
}

.remote-flow-line.is-off {
  --line-color: var(--ink-faint);
  --line-style: dashed;
}

.remote-flow-line.is-warn {
  --line-color: var(--amber-text);
  --line-style: dashed;
}

.remote-flow-line.is-cut {
  --line-color: var(--danger);
  --line-style: dashed;
}

.remote-summary-warn {
  color: var(--amber-text);
}

@media (max-width: 720px) {
  .remote-flow {
    gap: 8px;
    grid-template-columns: minmax(0, 1fr);
  }

  .remote-flow-lines {
    flex-direction: row;
    justify-content: center;
    gap: 40px;
  }

  .remote-flow-line {
    width: 0;
    height: 18px;
    margin: 0 auto;
    border-top: 0;
    border-left: 2px var(--line-style) var(--line-color);
  }

  .remote-flow-lines .remote-flow-line {
    margin: 0;
  }

  .remote-flow-line::after {
    display: none;
  }
}
</style>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import { ArchiveApiError, type BackupConfig, type BackupRunList, type ObjectStorageConfig } from "@/api/archiveApi";
import { formatDateTimeSeparator } from "@/utils/time";
import { useArchiveApi } from "@/api/injection";
import AppSelect from "@/components/ui/AppSelect.vue";
import PasswordInput from "@/components/PasswordInput.vue";
import ServerManagementFields from "@/components/ServerManagementFields.vue";
import type { ServerConfigurationState, ServerManagementHandle } from "@/components/serverManagement";
import { useCapabilityStore } from "@/stores/capabilities";

const props = withDefaults(defineProps<{ kind: "storage" | "backup"; setup?: boolean; showTitle?: boolean }>(), { setup: false, showTitle: true });
const emit = defineEmits<{ saved: []; skip: [state: ServerConfigurationState] }>();
const api = useArchiveApi();
const capabilities = useCapabilityStore();
const storage = ref<ObjectStorageConfig | null>(null);
const backup = ref<BackupConfig | null>(null);
// 备份近况只在日常设置里读：向导里还没跑过备份，也不为它多一次请求。读不到不挡配置。
const runs = ref<BackupRunList | null>(null);
const loading = ref(true);
const loadError = ref<string | null>(null);
const actionError = ref<string | null>(null);
const busy = ref(false);
const editing = ref(false);
const message = ref<string | null>(null);
const proof = ref<ServerManagementHandle | null>(null);
interface StorageForm { provider: ObjectStorageConfig["provider"]; region: string; endpoint: string; bucket: string; secretId: string; secretKey: string; interval: string; prefix: string; target: string }
const form = reactive<StorageForm>({ provider: "tencent_cos", region: "ap-nanjing", endpoint: "", bucket: "", secretId: "", secretKey: "", interval: "24", prefix: "backup", target: "other" });
const selectProvider = (value: string): void => {
  if (value === "tencent_cos" || value === "s3_compatible" || value === "minio") form.provider = value;
};
const providerOptions = [{ value: "tencent_cos", label: "腾讯云 COS" }, { value: "s3_compatible", label: "S3 兼容服务" }, { value: "minio", label: "MinIO" }];
const regions = [{ value: "ap-nanjing", label: "南京 · ap-nanjing" }, { value: "ap-guangzhou", label: "广州 · ap-guangzhou" }, { value: "ap-shanghai", label: "上海 · ap-shanghai" }, { value: "ap-beijing", label: "北京 · ap-beijing" }];
const frequencies = [{ value: "12", label: "每 12 小时" }, { value: "24", label: "每天" }, { value: "48", label: "每 2 天" }];
const isBackup = computed(() => props.kind === "backup");
const configured = computed(() => isBackup.value ? backup.value?.configured === true : storage.value?.configured === true);
const currentState = computed<ServerConfigurationState>(() => loading.value || loadError.value !== null ? "unknown" : isBackup.value && backup.value?.credentials_readable === false ? "unreadable" : configured.value ? "configured" : "not_configured");
const skip = (): void => { emit("skip", currentState.value); };
const same = computed(() => isBackup.value && form.target === "same");
const endpoint = computed(() => form.provider === "tencent_cos" && form.endpoint.trim() === "" ? `https://cos.${form.region}.myqcloud.com` : form.endpoint.trim());
const validPrefix = computed(() => /^[a-z0-9_-]{1,32}(\/[a-z0-9_-]{1,32}){0,3}$/.test(form.prefix) && form.prefix !== "a" && !form.prefix.startsWith("a/"));
const canKeepCredentials = computed(() => isBackup.value && backup.value?.configured === true && backup.value.credentials_readable && !same.value && form.secretId === "" && form.secretKey === "" && endpoint.value === backup.value.endpoint && form.bucket.trim() === backup.value.bucket && form.region.trim() === backup.value.region);
const ready = computed(() => !loading.value && !busy.value && proof.value?.ready === true &&
  (same.value ? storage.value?.configured === true && validPrefix.value : form.bucket.trim() !== "" && form.region.trim() !== "" && endpoint.value.startsWith("https://") && (canKeepCredentials.value || (form.secretId.length >= 8 && form.secretKey.length >= 8)) && (!isBackup.value || validPrefix.value)));
const title = computed(() => isBackup.value ? "定期备份" : "附件存储");
const formatSize = (bytes: number | null): string | null => bytes === null ? null : bytes >= 1024 ** 3 ? `${(bytes / 1024 ** 3).toFixed(2)} GiB` : `${(bytes / 1024 ** 2).toFixed(1)} MiB`;
/** 摘要上直接给出最近一轮的结论（原型 P19「最近成功 · 大小」）；失败与超期用警示色，不藏进记录里。 */
const latest = computed<{ warn: boolean; main: string; sub: string } | null>(() => {
  const value = runs.value;
  if (!isBackup.value || props.setup || value === null || !configured.value || backup.value?.credentials_readable === false) return null;
  const at = value.last_backup_at ?? null;
  const success = at === null ? "尚无成功备份" : `最近成功 ${formatDateTimeSeparator(at)}`;
  if (value.backup_overdue) return { warn: true, main: "已连续两个周期未成功备份", sub: success };
  switch (value.last_backup_status) {
    case "running": return { warn: false, main: "正在备份", sub: success };
    case "failed": return { warn: true, main: "上一轮失败", sub: success };
    case "success": {
      const size = formatSize(value.last_backup_bytes ?? null);
      return at === null ? null : { warn: false, main: formatDateTimeSeparator(at), sub: size === null ? "最近成功" : `最近成功 · ${size}` };
    }
    case "never": return { warn: false, main: "还没有运行过", sub: "按设定频率在服务器执行" };
    default: return null;
  }
});
const clearSecrets = (): void => { form.secretId = ""; form.secretKey = ""; };
let live = true;
const isLive = (): boolean => live;
onBeforeUnmount(() => { live = false; clearSecrets(); });

const fill = (): void => {
  const current = isBackup.value ? backup.value : storage.value;
  form.bucket = current?.bucket ?? "";
  form.region = current?.region ?? (isBackup.value ? "ap-guangzhou" : "ap-nanjing");
  form.endpoint = current?.endpoint ?? "";
  const currentEndpoint = current?.endpoint ?? "";
  const cosEndpoint = /^https:\/\/(?:[a-z0-9-]+\.)?cos\.[a-z0-9-]+\.myqcloud\.com(?:[/:]|$)/.test(currentEndpoint);
  form.provider = isBackup.value ? (currentEndpoint === "" || cosEndpoint ? "tencent_cos" : "s3_compatible") : storage.value?.provider ?? "tencent_cos";
  form.prefix = backup.value?.prefix ?? "backup";
  form.interval = String(backup.value?.interval_hours ?? 24);
  form.target = "other";
  clearSecrets();
};
const loadRuns = async (): Promise<void> => {
  try {
    const value = await api.listBackupRuns();
    if (isLive()) runs.value = value;
  } catch { if (isLive()) runs.value = null; }
};
const load = async (): Promise<void> => {
  loading.value = true;
  loadError.value = null;
  try {
    if (isBackup.value) {
      const [backupResult, storageResult] = await Promise.allSettled([api.getBackupConfig(), api.getObjectStorageConfig()]);
      if (backupResult.status === "rejected") throw backupResult.reason;
      backup.value = backupResult.value;
      storage.value = storageResult.status === "fulfilled" ? storageResult.value : null;
      if (!props.setup) void loadRuns();
    } else storage.value = await api.getObjectStorageConfig();
    fill();
    editing.value = !configured.value || (isBackup.value && backup.value?.credentials_readable === false);
  } catch (error: unknown) {
    loadError.value = error instanceof ArchiveApiError ? error.message : "暂时无法读取配置。";
  } finally { loading.value = false; }
};
onMounted(load);
const startEdit = (): void => { fill(); editing.value = true; actionError.value = null; message.value = null; };
const cancel = (): void => { if (busy.value) return; clearSecrets(); fill(); editing.value = false; actionError.value = null; };
const save = async (): Promise<void> => {
  if (!ready.value) return;
  busy.value = true;
  actionError.value = null;
  try {
    if (!(await proof.value?.authorize()) || !isLive()) return;
    if (!isBackup.value) {
      storage.value = await api.updateObjectStorageConfig({ provider: form.provider, endpoint: endpoint.value, bucket: form.bucket.trim(), region: form.region.trim(), secret_id: form.secretId, secret_key: form.secretKey });
    } else if (same.value) {
      backup.value = await api.useAttachmentStorageForBackup({ prefix: form.prefix, interval_hours: Number(form.interval) });
    } else if (canKeepCredentials.value) {
      backup.value = await api.updateBrowserBackupPolicy({ endpoint: endpoint.value, bucket: form.bucket.trim(), region: form.region.trim(), prefix: form.prefix, interval_hours: Number(form.interval) });
    } else {
      backup.value = await api.updateBackupConfig({ endpoint: endpoint.value, bucket: form.bucket.trim(), region: form.region.trim(), secret_id: form.secretId, secret_key: form.secretKey, prefix: form.prefix, interval_hours: Number(form.interval), retention_mode: backup.value?.retention_mode ?? "lifecycle", daily_keep: backup.value?.daily_keep ?? 7, weekly_keep: backup.value?.weekly_keep ?? 4, monthly_keep: backup.value?.monthly_keep ?? 12 });
    }
    clearSecrets();
    editing.value = false;
    message.value = "已保存";
    capabilities.loaded = false;
    await capabilities.load(api);
    if (isBackup.value && !props.setup) void loadRuns();
    emit("saved");
  } catch (error: unknown) {
    proof.value?.requireProof(error);
    actionError.value = error instanceof ArchiveApiError ? error.message : "连接测试失败，原配置继续生效。";
  } finally { busy.value = false; }
};
const run = async (): Promise<void> => {
  if (busy.value) return;
  busy.value = true; actionError.value = null;
  try { const value = await api.requestBackupRun(); if (isLive()) runs.value = value; message.value = "备份已排队，可稍后查看结果。"; }
  catch (error: unknown) { actionError.value = error instanceof ArchiveApiError ? error.message : "未能启动备份。"; }
  finally { busy.value = false; }
};
const disable = async (): Promise<void> => {
  const manager = proof.value;
  if (busy.value || manager === null || !manager.ready) return;
  busy.value = true;
  try { if (!(await manager.authorize())) return; backup.value = await api.disableBrowserBackup(); editing.value = false; message.value = "已停用，已有备份仍保留。"; }
  catch (error: unknown) { manager.requireProof(error); actionError.value = error instanceof ArchiveApiError ? error.message : "未能停用备份。"; }
  finally { busy.value = false; clearSecrets(); }
};
</script>

<template>
  <section
    class="server-storage-panel"
    :data-testid="`server-${kind}-panel`"
  >
    <header
      class="server-panel-heading"
      :class="{ 'is-embedded': !showTitle }"
    >
      <component
        :is="setup ? 'h1' : 'h2'"
        v-if="showTitle"
      >
        {{ title }}
      </component><span
        v-if="!loading && !loadError"
        class="settings-pill"
      >{{ isBackup && backup?.credentials_readable === false ? '凭据需重填' : configured ? (isBackup ? '已开启' : '已配置') : (isBackup ? '未开启' : '未配置') }}</span>
    </header>
    <p
      v-if="loading"
      role="status"
    >
      正在读取配置…
    </p>
    <div
      v-else-if="loadError"
      class="notice-warn"
      role="alert"
    >
      {{ loadError }} <button
        class="text-action"
        type="button"
        @click="load"
      >
        重试
      </button>
    </div>
    <template v-else>
      <template v-if="!editing">
        <p
          v-if="configured"
          class="action-note"
        >
          {{ isBackup ? `${backup?.bucket ?? ''} / ${backup?.prefix ?? ''} · 每 ${backup?.interval_hours ?? 24} 小时` : `${storage?.provider === 'minio' ? 'MinIO' : storage?.provider === 's3_compatible' ? 'S3 兼容服务' : '腾讯云 COS'} · ${storage?.bucket ?? ''}` }}
        </p>
        <div
          v-if="latest"
          class="server-backup-latest"
          :class="{ 'is-warn': latest.warn }"
          data-testid="server-backup-latest"
          :role="latest.warn ? 'alert' : undefined"
        >
          <strong>{{ latest.main }}</strong><span>{{ latest.sub }}</span>
        </div>
        <div class="button-row">
          <button
            v-if="setup"
            class="button primary"
            type="button"
            @click="emit('saved')"
          >
            使用并继续
          </button><button
            class="button quiet"
            type="button"
            @click="startEdit"
          >
            {{ configured ? '修改配置' : '配置' }}
          </button><button
            v-if="isBackup && configured && !setup"
            class="button"
            type="button"
            :disabled="busy"
            @click="run"
          >
            立即备份
          </button>
        </div>
        <details class="server-help">
          <summary>查看详情</summary><p>{{ isBackup ? '备份在服务器执行。下载备份请在已接入的电脑中操作。' : '配置保存在服务器，与已接入电脑共用。更换目标只影响新上传附件，旧附件留在原存储。' }}</p>
        </details>
      </template>
      <form
        v-else
        @submit.prevent="save"
      >
        <fieldset
          :disabled="busy"
          class="server-form-fields"
        >
          <template v-if="isBackup">
            <label class="server-field"><span>备份频率</span><AppSelect
              v-model="form.interval"
              :options="frequencies"
              :filter="false"
            /></label>
            <div
              class="server-target-choice"
              role="group"
              aria-label="备份位置"
            >
              <button
                class="button"
                type="button"
                :aria-pressed="same"
                :disabled="!storage?.configured"
                @click="form.target = 'same'"
              >
                {{ storage?.configured ? '使用附件存储' : '附件存储未配置' }}
              </button><button
                class="button"
                type="button"
                :aria-pressed="!same"
                @click="form.target = 'other'"
              >
                单独配置
              </button>
            </div>
            <p
              v-if="same"
              class="action-note"
            >
              {{ storage?.bucket }} / {{ form.prefix }} · 共用目标与访问权限
            </p>
            <p
              v-if="same && storage?.provider === 'minio'"
              class="notice-warn"
            >
              如与记录库同机，无法防护整机或磁盘损坏。
            </p>
          </template>
          <div
            v-if="!same"
            class="server-form-grid"
          >
            <label
              class="server-field"
            ><span>服务商</span><AppSelect
              :model-value="form.provider"
              :options="providerOptions"
              :filter="false"
              @update:model-value="selectProvider"
            /></label>
            <label class="server-field"><span>地域</span><AppSelect
              v-if="form.provider === 'tencent_cos'"
              v-model="form.region"
              :options="regions"
              :filter="false"
            /><input
              v-else
              v-model="form.region"
              placeholder="服务商提供的地域"
            ></label>
            <label class="server-field server-field-wide"><span>已有存储桶</span><input
              v-model="form.bucket"
              data-testid="server-bucket"
              placeholder="填写已有桶的完整名称"
              required
            ></label>
            <label class="server-field"><span>SecretId</span><input
              v-model="form.secretId"
              data-testid="server-secret-id"
              autocomplete="off"
              :required="!canKeepCredentials"
              :placeholder="canKeepCredentials ? '已保存，不更换可留空' : ''"
            ></label>
            <label class="server-field"><span>SecretKey</span><PasswordInput
              v-model="form.secretKey"
              data-testid="server-secret-key"
              autocomplete="new-password"
              :required="!canKeepCredentials"
              :placeholder="canKeepCredentials ? '不更换可留空' : ''"
            /></label>
            <label
              v-if="form.provider !== 'tencent_cos'"
              class="server-field server-field-wide"
            ><span>服务地址</span><input
              v-model="form.endpoint"
              data-testid="server-endpoint"
              placeholder="https://storage.example.com"
              required
            ></label>
          </div>
          <details class="server-help">
            <summary>高级设置与保存方式</summary><label
              v-if="isBackup"
              class="server-field"
            ><span>备份前缀</span><input v-model="form.prefix"><small>不能放在 a/ 下；清理规则仅作用于备份前缀。</small></label><label
              v-if="form.provider === 'tencent_cos'"
              class="server-field"
            ><span>自定义服务地址</span><input
              v-model="form.endpoint"
              data-testid="server-endpoint"
              placeholder="留空使用地域对应的地址"
            ></label><p>使用已有桶，不需要建桶权限。测试通过才保存；密钥保存后不再显示。</p>
          </details>
        </fieldset>
        <ServerManagementFields
          ref="proof"
          :setup="setup"
          :disabled="busy"
        />
        <p
          v-if="busy"
          role="status"
        >
          正在验证并测试连接…
        </p>
        <div class="button-row">
          <button
            class="button primary"
            type="submit"
            :disabled="!ready"
          >
            {{ busy ? '正在测试…' : setup ? (isBackup ? '测试并开启' : '测试并继续') : '测试并保存' }}
          </button><button
            v-if="configured && !setup"
            class="button quiet"
            type="button"
            :disabled="busy"
            @click="cancel"
          >
            取消
          </button><button
            v-if="setup"
            class="button quiet"
            type="button"
            :disabled="busy"
            @click="skip"
          >
            {{ isBackup ? '暂不开启' : '暂不配置' }}
          </button>
        </div>
        <details
          v-if="isBackup && configured && !setup"
          class="server-help"
        >
          <summary>停用备份</summary><p>停用会移除服务器保存的目标与凭据，云端备份和运行记录仍保留。再次开启需重新填写；上方身份验证仍必填。</p><button
            class="button danger"
            type="button"
            :disabled="busy || !proof?.ready"
            @click="disable"
          >
            确认停用
          </button>
        </details>
      </form>
      <p
        v-if="actionError"
        class="notice-warn"
        role="alert"
      >
        {{ actionError }}
      </p>
      <p
        v-if="message"
        class="info-note"
        role="status"
      >
        {{ message }}
      </p>
    </template>
    <button
      v-if="setup && (loading || loadError || !editing)"
      class="button quiet"
      type="button"
      :disabled="busy"
      @click="skip"
    >
      {{ isBackup ? '暂不开启' : '暂不配置' }}
    </button>
  </section>
</template>

<style>
.server-storage-panel{min-width:0}.server-panel-heading{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:20px}.server-panel-heading :is(h1,h2){font-size:1.25rem;margin:0}.server-panel-heading.is-embedded{justify-content:flex-end;margin-bottom:12px}.server-form-fields,.server-management-proof{border:0;padding:0;margin:0;min-width:0}.server-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;margin:18px 0}.server-field{display:flex;flex-direction:column;gap:7px;font-size:.875rem;min-width:0}.server-field>span{font-weight:700}.server-field input{width:100%;min-width:0;min-height:44px;padding:10px 12px;border:1px solid var(--rule);border-radius:8px;background:var(--paper);color:var(--ink);font:inherit}.server-field-wide{grid-column:1/-1}.server-field small{color:var(--ink-faint);font-size:.75rem}.server-help{font-size:.8125rem;color:var(--ink-faint);margin:16px 0}.server-help summary{cursor:pointer;min-height:40px;display:flex;align-items:center;gap:8px;list-style:none}.server-help summary:before{content:'›';font-size:1.125rem}.server-help[open] summary:before{transform:rotate(90deg)}.server-help p{line-height:1.7}.server-management-proof{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:16px;border-top:1px solid var(--rule);padding-top:18px;margin-top:20px}.server-management-proof legend{font-size:.8125rem;color:var(--ink-faint);padding:0 10px 0 0}.server-backup-latest{display:flex;flex-wrap:wrap;align-items:baseline;gap:4px 10px;margin:4px 0 16px}.server-backup-latest strong{font-size:1.0625rem;color:var(--ink-strong)}.server-backup-latest span{font-size:.8125rem;color:var(--ink-soft)}.server-backup-latest.is-warn strong{color:var(--amber-text)}.server-target-choice{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.server-target-choice button{flex:1}.server-target-choice button[aria-pressed=true]{border-color:var(--accent);background:var(--safe-bg)}@media(max-width:47.99rem){.server-form-grid,.server-management-proof{grid-template-columns:minmax(0,1fr)}.server-storage-panel .button-row{flex-wrap:wrap}.server-storage-panel .button-row>.primary{flex:1}}
</style>

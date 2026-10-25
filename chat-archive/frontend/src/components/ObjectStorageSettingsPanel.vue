<script setup lang="ts">
import AppSelect from "@/components/ui/AppSelect.vue";
import { useQuery } from "@tanstack/vue-query";
import { computed, onBeforeUnmount, ref, watch } from "vue";

import {
  ArchiveApiError,
  type ObjectStorageConfig,
  type ObjectStorageConfigUpdate,
  type SyncHealth,
} from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import ServerManagementFields from "@/components/ServerManagementFields.vue";
import type { ServerManagementHandle } from "@/components/serverManagement";
import { useCapabilityStore } from "@/stores/capabilities";
import SettingsConfirm from "@/components/SettingsConfirm.vue";
import SettingsTip from "@/components/SettingsTip.vue";

const props = withDefaults(defineProps<{ accountId?: string | null; mode?: "manage" | "browser" }>(), {
  accountId: null,
  mode: "manage",
});

const api = useArchiveApi();
const browser = computed(() => props.mode === "browser");
const capabilities = browser.value ? useCapabilityStore() : null;
const proof = ref<ServerManagementHandle | null>(null);

// Tencent COS regions, with the endpoint each one implies. Users pick a place,
// not a URL; the endpoint field only appears in advanced mode for the rare
// deployment that fronts COS with a custom domain.
const DEFAULT_REGION = "ap-guangzhou";
const REGIONS: ReadonlyArray<{ value: string; label: string }> = [
  { value: DEFAULT_REGION, label: "广州（ap-guangzhou）" },
  { value: "ap-shanghai", label: "上海（ap-shanghai）" },
  { value: "ap-beijing", label: "北京（ap-beijing）" },
  { value: "ap-nanjing", label: "南京（ap-nanjing）" },
  { value: "ap-chengdu", label: "成都（ap-chengdu）" },
  { value: "ap-chongqing", label: "重庆（ap-chongqing）" },
  { value: "ap-hongkong", label: "中国香港（ap-hongkong）" },
  { value: "ap-singapore", label: "新加坡（ap-singapore）" },
];

const endpointForRegion = (region: string): string =>
  `https://cos.${region}.myqcloud.com`;

const healthQuery = useQuery({
  queryKey: computed(() => ["object-storage-blob-count", props.accountId] as const),
  enabled: computed(() => !browser.value && props.accountId !== null),
  queryFn: (): Promise<SyncHealth> => {
    if (props.accountId === null) throw new Error("尚未选择账号");
    return api.getSyncHealth(props.accountId);
  },
  retry: false,
});

const supportsManagedObjectStorage = computed(() => {
  if (browser.value) return capabilities?.states["object-storage-config"] !== "unsupported";
  const backend = healthQuery.data.value?.backend_type;
  return backend === "self_hosted_sqlite" || backend === "self_hosted_postgresql";
});

const configQuery = useQuery({
  queryKey: computed(
    () => ["object-storage-config", healthQuery.data.value?.archive_id ?? null] as const,
  ),
  enabled: supportsManagedObjectStorage,
  queryFn: (): Promise<ObjectStorageConfig> => api.getObjectStorageConfig(),
  retry: false,
});

const config = computed(() => configQuery.data.value ?? null);
const configured = computed(() => config.value?.configured === true);

const loadErrorText = computed(() => {
  const error = configQuery.error.value;
  if (error === null) return null;
  return error instanceof ArchiveApiError
    ? error.message
    : "暂时无法读取对象存储配置。";
});

const editing = ref(false);
const advanced = ref(false);
const provider = ref<ObjectStorageConfigUpdate["provider"]>("tencent_cos");
const providerOptions = [{ value: "tencent_cos", label: "腾讯云 COS" }, { value: "s3_compatible", label: "S3 兼容服务" }, { value: "minio", label: "MinIO" }];
const providerLabel = computed(() => providerOptions.find(item => item.value === config.value?.provider)?.label ?? "对象存储");
const selectProvider = (value: string): void => {
  if (value === "tencent_cos" || value === "s3_compatible" || value === "minio") provider.value = value;
};
const region = ref(DEFAULT_REGION);
const bucket = ref("");
const endpoint = ref(endpointForRegion(DEFAULT_REGION));
// Credentials live only in these refs for the duration of one submit. They are
// never written to a store, the query cache, the URL, localStorage or a log.
const secretId = ref("");
const secretKey = ref("");

const busy = ref(false);
const actionError = ref<string | null>(null);
const actionMessage = ref<string | null>(null);

const moveConfirmOpen = ref(false);
const bucketConfirmation = ref("");

const affectedBlobCount = computed(() => healthQuery.data.value?.remote_blob_count ?? null);

const movesStorage = computed(() => {
  const current = config.value;
  if (current === null || !current.configured) return false;
  return (
    current.endpoint !== endpoint.value ||
    current.bucket !== bucket.value ||
    current.region !== region.value
  );
});

const canSubmit = computed(
  () =>
    !busy.value &&
    (!browser.value || proof.value?.ready === true) &&
    bucket.value.trim().length > 0 &&
    secretId.value.length > 0 &&
    secretKey.value.length > 0 &&
    endpoint.value.length > 0,
);

const bucketConfirmed = computed(
  () => bucketConfirmation.value.trim() === bucket.value.trim(),
);

const onRegionChange = (value: string): void => {
  region.value = value;
  if (!advanced.value && provider.value === "tencent_cos") endpoint.value = endpointForRegion(value);
};

watch(advanced, (enabled) => {
  if (!enabled && provider.value === "tencent_cos") endpoint.value = endpointForRegion(region.value);
});

const clearSecrets = (): void => {
  secretId.value = "";
  secretKey.value = "";
};

const startEditing = (): void => {
  const current = config.value;
  editing.value = true;
  advanced.value = false;
  actionError.value = null;
  actionMessage.value = null;
  clearSecrets();
  if (current !== null && current.configured) {
    provider.value = current.provider;
    region.value = current.region ?? DEFAULT_REGION;
    bucket.value = current.bucket ?? "";
    endpoint.value = current.endpoint ?? endpointForRegion(region.value);
    return;
  }
  region.value = DEFAULT_REGION;
  bucket.value = "";
  endpoint.value = endpointForRegion(DEFAULT_REGION);
};

const closeMoveConfirm = (): void => {
  moveConfirmOpen.value = false;
  bucketConfirmation.value = "";
};

const cancelEditing = (): void => {
  proof.value?.clear();
  editing.value = false;
  moveConfirmOpen.value = false;
  bucketConfirmation.value = "";
  clearSecrets();
};

const describeFailure = (error: unknown, fallback: string): string =>
  error instanceof ArchiveApiError ? error.message : fallback;

const submit = async (): Promise<void> => {
  if (!canSubmit.value) return;
  if (movesStorage.value && !moveConfirmOpen.value) {
    moveConfirmOpen.value = true;
    bucketConfirmation.value = "";
    return;
  }
  if (movesStorage.value && !bucketConfirmed.value) return;

  busy.value = true;
  actionError.value = null;
  actionMessage.value = null;
  try {
    if (browser.value && !(await proof.value?.authorize())) return;
    // Test then save is one action, not two buttons: a configuration that
    // cannot round-trip should never become the saved one.
    await api.testObjectStorageConfig({
      provider: provider.value,
      endpoint: endpoint.value,
      bucket: bucket.value.trim(),
      region: region.value,
      secret_id: secretId.value,
      secret_key: secretKey.value,
    });
    await api.updateObjectStorageConfig({
      provider: provider.value,
      endpoint: endpoint.value,
      bucket: bucket.value.trim(),
      region: region.value,
      secret_id: secretId.value,
      secret_key: secretKey.value,
    });
    clearSecrets();
    editing.value = false;
    proof.value?.clear();
    if (capabilities) void capabilities.refresh(api);
    moveConfirmOpen.value = false;
    bucketConfirmation.value = "";
    actionMessage.value = "已验证并保存，附件存储现在可用。";
    await configQuery.refetch();
  } catch (error: unknown) {
    proof.value?.requireProof(error);
    // Only the stable, already-sanitised message reaches the screen; the
    // provider's own error text never leaves the server.
    actionError.value = describeFailure(
      error,
      "暂时无法验证对象存储配置，请稍后重试。",
    );
    clearSecrets();
    // 确认框是遮罩：失败信息在它后面看不到，凭据也已清空、框里改不了，
    // 所以收起它回到表单；再次保存会重新要求输入桶名确认。
    closeMoveConfirm();
  } finally {
    busy.value = false;
  }
};

onBeforeUnmount(clearSecrets);

const formatTimestamp = (value: number | null): string =>
  value === null ? "尚未记录" : new Date(value * 1000).toLocaleString("zh-CN");

const regionLabel = (value: string | null | undefined): string => {
  if (value === null || value === undefined) return "—";
  return REGIONS.find((item) => item.value === value)?.label ?? value;
};

const statusTones: Readonly<Record<ObjectStorageConfig["status"], string>> = {
  healthy: "is-ok",
  degraded: "is-warn",
  not_configured: "is-warn",
};

const statusLabels: Readonly<Record<ObjectStorageConfig["status"], string>> = {
  healthy: "正常",
  degraded: "能力有缺口",
  not_configured: "未配置",
};
</script>

<template>
  <div
    v-if="supportsManagedObjectStorage"
    id="object-storage"
    class="object-storage-settings"
    data-testid="object-storage-panel"
  >
    <div
      v-if="actionError !== null"
      class="settings-banner is-danger"
      role="alert"
      data-testid="object-storage-error"
    >
      <div class="settings-banner-main">
        <p>{{ actionError }}</p>
      </div>
    </div>
    <div
      v-if="actionMessage !== null"
      class="settings-banner is-ok"
      role="status"
      data-testid="object-storage-message"
    >
      <div class="settings-banner-main">
        <p>{{ actionMessage }}</p>
      </div>
    </div>

    <div
      v-if="configQuery.isLoading.value"
      class="settings-skeleton"
      data-reserve="object-storage"
      aria-busy="true"
    >
      <p class="settings-muted">
        正在读取附件存储配置……
      </p>
      <i aria-hidden="true" />
    </div>
    <div
      v-else-if="loadErrorText !== null"
      class="settings-banner is-danger"
      role="alert"
    >
      <div class="settings-banner-main">
        <strong>读不到附件存储配置</strong>
        <p>{{ loadErrorText }}</p>
      </div>
      <div class="settings-banner-actions">
        <button
          class="button secondary"
          type="button"
          @click="configQuery.refetch()"
        >
          重新加载
        </button>
      </div>
    </div>

    <template v-else>
      <div
        v-if="!configured && !editing"
        data-testid="object-storage-unconfigured"
      >
        <div
          class="settings-banner"
          role="status"
        >
          <div class="settings-banner-main">
            <strong>还没配置附件存储</strong>
            <p>聊天记录仍可正常同步和查看；图片、语音、视频和文件要配好才会上传。</p>
          </div>
          <div class="settings-banner-actions">
            <button
              class="button primary"
              type="button"
              data-testid="object-storage-configure"
              @click="startEditing"
            >
              配置附件存储
            </button>
          </div>
        </div>
        <p class="settings-muted object-storage-note">
          配置之后不会自动补传历史附件，能补多少取决于本机文件还在不在。
        </p>
      </div>

      <div
        v-else-if="configured && !editing"
        class="object-storage-configured"
        data-testid="object-storage-summary"
      >
        <dl class="settings-facts">
          <div class="settings-fact">
            <dt>状态</dt>
            <dd>
              <span
                class="settings-pill"
                :class="statusTones[config?.status ?? 'not_configured']"
              >{{ statusLabels[config?.status ?? "not_configured"] }}</span>
            </dd>
          </div>
          <div class="settings-fact">
            <dt>存储桶</dt>
            <dd>{{ config?.bucket }}</dd>
          </div>
          <div class="settings-fact">
            <dt>地域</dt>
            <dd>{{ providerLabel }} · {{ regionLabel(config?.region) }}</dd>
          </div>
          <div class="settings-fact">
            <dt>SecretId</dt>
            <dd data-testid="object-storage-hint">
              {{ config?.access_key_hint }}
            </dd>
          </div>
          <div class="settings-fact">
            <dt>更新时间</dt>
            <dd>{{ formatTimestamp(config?.updated_at ?? null) }}</dd>
          </div>
        </dl>
        <div class="object-storage-actions">
          <button
            class="button secondary"
            type="button"
            data-testid="object-storage-replace"
            @click="startEditing"
          >
            更换凭据或存储桶
          </button>
          <SettingsTip label="为什么要重新输入">
            页面从不读取已保存的 SecretKey；更换凭据需要重新输入一次。
          </SettingsTip>
          <span class="settings-muted">存储编号：第 {{ config?.storage_generation }} 代</span>
        </div>
      </div>

      <form
        v-else
        class="object-storage-editor settings-form"
        @submit.prevent="submit"
      >
        <label class="settings-field">
          服务商
          <AppSelect
            :model-value="provider"
            :options="providerOptions"
            data-testid="object-storage-provider"
            aria-label="对象存储服务商"
            :filter="false"
            @update:model-value="selectProvider"
          />
        </label>
        <label class="settings-field">
          地域
          <AppSelect
            v-if="provider === 'tencent_cos'"
            :model-value="region"
            :options="REGIONS"
            data-testid="object-storage-region"
            aria-label="对象存储地域"
            @update:model-value="onRegionChange"
          />
          <input
            v-else
            v-model="region"
            type="text"
            aria-label="对象存储地域"
          >
        </label>
        <label class="settings-field is-wide">
          已有存储桶名称
          <input
            v-model="bucket"
            type="text"
            autocomplete="off"
            placeholder="chat-archive-1250000000"
            data-testid="object-storage-bucket"
          >
        </label>
        <label class="settings-field">
          SecretId
          <input
            v-model="secretId"
            type="text"
            autocomplete="off"
            spellcheck="false"
            data-testid="object-storage-secret-id"
          >
        </label>
        <label class="settings-field">
          SecretKey
          <input
            v-model="secretKey"
            type="password"
            autocomplete="new-password"
            spellcheck="false"
            data-testid="object-storage-secret-key"
          >
        </label>

        <div class="is-wide object-storage-endpoint">
          <label
            v-if="provider === 'tencent_cos'"
            class="object-storage-advanced-toggle"
          >
            <input
              v-model="advanced"
              type="checkbox"
              data-testid="object-storage-advanced"
            >
            <span>高级：自定义 Endpoint</span>
          </label>
          <label
            v-if="advanced || provider !== 'tencent_cos'"
            class="settings-field"
          >
            Endpoint
            <input
              v-model="endpoint"
              type="url"
              autocomplete="off"
              data-testid="object-storage-endpoint"
            >
          </label>
          <p
            v-else
            class="settings-muted"
            data-testid="object-storage-endpoint-derived"
          >
            按地域自动生成：{{ endpoint }}
          </p>
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
        <div class="is-wide object-storage-actions">
          <button
            data-feedback-action="测试并保存"
            class="button primary"
            type="submit"
            data-testid="object-storage-submit"
            :disabled="!canSubmit || (moveConfirmOpen && !bucketConfirmed)"
          >
            {{ busy ? "正在验证……" : "测试并保存" }}
          </button>
          <button
            class="button quiet"
            type="button"
            data-testid="object-storage-cancel"
            :disabled="busy"
            @click="cancelEditing"
          >
            取消
          </button>
          <SettingsTip label="保存前会做什么">
            保存前会上传一个小测试文件，读回校验、签发下载链接、再删掉。
            验证不通过就不会保存，原有配置继续可用。
          </SettingsTip>
        </div>

        <SettingsConfirm
          v-if="moveConfirmOpen"
          title="换存储桶后，已有附件在远程暂时打不开"
          data-testid="object-storage-move-confirm"
          @close="closeMoveConfirm"
        >
          <ul>
            <li v-if="affectedBlobCount !== null">
              远程当前存有 {{ affectedBlobCount }} 个附件，它们留在旧桶里，不会被删除。
            </li>
            <li v-else>
              已上传的附件留在旧桶里，不会被删除。（当前无法读取远程附件数量。）
            </li>
            <li>新配置生效后，这些旧附件在远程打不开。</li>
          </ul>
          <label class="settings-field">
            请输入新的存储桶名称以确认
            <input
              v-model="bucketConfirmation"
              type="text"
              autocomplete="off"
              data-testid="object-storage-bucket-confirm"
            >
          </label>
          <template #actions>
            <button
              class="button quiet"
              type="button"
              :disabled="busy"
              @click="closeMoveConfirm"
            >
              取消
            </button>
            <button
              data-feedback-action="换到新存储桶"
              class="button danger"
              type="submit"
              data-testid="object-storage-move-submit"
              :disabled="!canSubmit || !bucketConfirmed"
            >
              {{ busy ? "正在验证……" : "换到新存储桶" }}
            </button>
          </template>
        </SettingsConfirm>
      </form>

      <section
        v-if="(config?.generations.length ?? 0) > 1"
        class="settings-group object-storage-generations"
        data-testid="object-storage-generations"
      >
        <h3 class="settings-group-title">
          历史存储
          <SettingsTip label="为什么有多份">
            每次更换存储桶或地域都会换一份新的存储，附件记得自己放在哪一份里；旧的那份不会被删除。
          </SettingsTip>
        </h3>
        <div class="settings-list">
          <div
            v-for="item in config?.generations ?? []"
            :key="item.storage_generation"
            class="settings-row"
          >
            <div class="settings-row-main">
              <div class="settings-row-title">
                {{ item.bucket }}
              </div>
              <div class="settings-row-sub">
                <span>{{ regionLabel(item.region) }}</span>
                <span>{{ item.access_key_hint }}</span>
                <span>{{ formatTimestamp(item.updated_at) }}</span>
                <span>第 {{ item.storage_generation }} 代</span>
              </div>
            </div>
            <div class="settings-row-trail">
              <span
                v-if="item.active"
                class="settings-pill is-ok"
              >当前存储</span>
              <span
                v-else
                class="settings-muted"
              >已停用</span>
            </div>
          </div>
        </div>
      </section>
    </template>
  </div>
</template>

<style scoped>
.object-storage-configured {
  display: grid;
  gap: 14px;
  margin-bottom: 24px;
}

.object-storage-actions {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 8px 10px;
}

.object-storage-endpoint {
  display: grid;
  gap: 8px;
}

.object-storage-advanced-toggle {
  display: inline-flex;
  width: fit-content;
  align-items: center;
  gap: 6px;
  color: var(--ink-soft);
  cursor: pointer;
  font-size: 12.5px;
}

/* 不叫 .object-storage-form：main.css 里旧版同名规则会把两列表单压成一列。
   全局 .settings-field input 只管文本框，勾选框在这里归零。 */
.object-storage-editor .object-storage-advanced-toggle input {
  min-height: 0;
  margin: 0;
  padding: 0;
}

.object-storage-note {
  margin: 0 0 14px;
}

.object-storage-generations {
  margin-top: 8px;
}

@media (max-width: 720px) {
  .object-storage-editor {
    grid-template-columns: minmax(0, 1fr);
  }
}
</style>

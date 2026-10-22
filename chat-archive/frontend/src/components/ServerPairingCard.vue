<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { ArchiveApiError, type IssuedPairingCode } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import QrCode from "@/components/QrCode.vue";
import ServerManagementFields from "@/components/ServerManagementFields.vue";
import type { ServerManagementHandle } from "@/components/serverManagement";
import { startClock, startTimeout } from "@/lib/clock";
import { formatCountdown, pairingLink, remainingSeconds } from "@/lib/pairingLink";
import { copyText } from "@/utils/clipboard";

const props = withDefaults(defineProps<{ setup?: boolean }>(), { setup: false });
const emit = defineEmits<{ paired: [] }>();
const api = useArchiveApi();
const proof = ref<ServerManagementHandle | null>(null);
const issued = ref<IssuedPairingCode | null>(null);
const opened = ref(props.setup);
const busy = ref(false);
const copied = ref(false);
const paired = ref(false);
const errorText = ref<string | null>(null);
const now = ref(Date.now());
let stopClock: (() => void) | null = null;
let stopCopy: (() => void) | null = null;
let live = true;
const isLive = (): boolean => live;
let polling = false;
let lastPoll = 0;
let knownDevices = new Set<string>();
const seconds = computed(() => issued.value ? remainingSeconds(issued.value.expires_at, now.value) : 0);
const expired = computed(() => issued.value !== null && seconds.value === 0);
const link = computed(() => issued.value ? pairingLink(globalThis.location.origin, issued.value.pairing_code) : "");
onBeforeUnmount(() => { live = false; stopClock?.(); stopCopy?.(); });

const poll = async (): Promise<void> => {
  if (polling || now.value - lastPoll < 5000) return;
  polling = true; lastPoll = now.value;
  try {
    const page = await api.listRemoteDevices();
    if (live && page.items.some(device => device.status === "active" && !knownDevices.has(device.id))) {
      paired.value = true; issued.value = null; stopClock?.(); emit("paired");
    }
  } catch { /* A later tick can retry; never log device details or issued codes. */ }
  finally { polling = false; }
};
const issue = async (): Promise<void> => {
  opened.value = true;
  const manager = proof.value;
  if (busy.value || manager === null || !manager.ready) return;
  busy.value = true; errorText.value = null;
  try {
    if (!(await manager.authorize())) return;
    if (!isLive()) return;
    try {
      const existing = await api.listRemoteDevices();
      knownDevices = new Set(existing.items.map(device => device.id));
    } catch (error: unknown) {
      if (!(error instanceof ArchiveApiError) || error.code !== "REMOTE_ARCHIVE_NOT_FOUND") throw error;
      knownDevices = new Set();
    }
    const code = await api.issuePairingCode();
    if (!isLive()) return;
    issued.value = code; copied.value = false; now.value = Date.now();
    stopClock?.();
    stopClock = startClock(value => { now.value = value; if (seconds.value === 0) stopClock?.(); else void poll(); });
  } catch (error: unknown) {
    manager.requireProof(error);
    errorText.value = error instanceof ArchiveApiError ? error.message : "未能生成配对码，请重试。";
  } finally { busy.value = false; }
};
const copy = async (): Promise<void> => {
  if (!issued.value || expired.value) return;
  copied.value = await copyText(issued.value.pairing_code);
  stopCopy?.(); stopCopy = startTimeout(() => { copied.value = false; }, 2000);
};
</script>

<template>
  <section
    class="server-pairing-card"
    data-testid="server-pairing-card"
  >
    <p
      v-if="paired"
      class="info-note"
      role="status"
    >
      电脑已接入
    </p>
    <template v-else>
      <template v-if="issued">
        <p
          v-if="expired"
          class="notice-warn"
          role="status"
        >
          配对码已过期
        </p>
        <template v-else>
          <p
            class="server-pairing-code"
            data-feedback-mask="device-info"
          >
            {{ issued.pairing_code }}
          </p><div class="button-row">
            <button
              class="button"
              type="button"
              @click="copy"
            >
              {{ copied ? '已复制' : '复制' }}
            </button><span class="action-note">{{ formatCountdown(seconds) }} 后过期</span>
          </div><details class="server-help">
            <summary>二维码</summary><QrCode
              :value="link"
              label="一次性电脑配对链接"
            />
          </details>
        </template>
      </template>
      <template v-if="!issued || expired">
        <ServerManagementFields
          v-if="opened"
          ref="proof"
          :setup="setup"
          :disabled="busy"
        />
        <button
          class="button primary"
          type="button"
          :disabled="busy || (opened && !proof?.ready)"
          @click="issue"
        >
          {{ busy ? '正在生成…' : expired ? '重新生成' : '生成配对码' }}
        </button>
      </template>
      <details class="server-help">
        <summary>电脑上怎么接入？</summary><p>电脑上的聊记 → 同步健康 → 接入远程档案，填写此服务器地址并粘贴配对码。第一台电脑同样使用配对码。</p>
      </details>
    </template>
    <p
      v-if="errorText"
      role="alert"
      class="notice-warn"
    >
      {{ errorText }}
    </p>
  </section>
</template>

<style scoped>
.server-pairing-card{min-width:0}.server-pairing-code{overflow-wrap:anywhere;user-select:all;font: .875rem/1.7 monospace;padding:16px;background:var(--surface-sunken);border:1px solid var(--rule);border-radius:8px}.server-pairing-card>.button{margin-top:16px}
</style>

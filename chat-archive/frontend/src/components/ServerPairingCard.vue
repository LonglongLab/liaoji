<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";
import { ArchiveApiError, type IssuedPairingCode } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import ServerManagementFields from "@/components/ServerManagementFields.vue";
import type { ServerManagementHandle } from "@/components/serverManagement";
import { startClock, startTimeout } from "@/lib/clock";
import { formatCountdown, pairingLink, remainingSeconds } from "@/lib/pairingLink";
import { copyText } from "@/utils/clipboard";

const props = withDefaults(defineProps<{ setup?: boolean }>(), { setup: false });
const emit = defineEmits<{ paired: []; active: [value: boolean] }>();
const api = useArchiveApi();
const proof = ref<ServerManagementHandle | null>(null);
const issued = ref<IssuedPairingCode | null>(null);
const opened = ref(props.setup);
const busy = ref(false);
const copying = ref(false);
const copied = ref(false);
const copiedOnce = ref(false);
const copyFailed = ref(false);
const paired = ref(false);
const connectedDeviceName = ref("电脑");
const errorText = ref<string | null>(null);
const now = ref(Date.now());
const ttl = ref(600);
let stopClock: (() => void) | null = null;
let stopCopy: (() => void) | null = null;
let live = true;
const isLive = (): boolean => live;
let generation = 0;
let polling = false;
let lastPoll = 0;
let knownDevices = new Set<string>();
const seconds = computed(() => issued.value ? remainingSeconds(issued.value.expires_at, now.value) : 0);
const expired = computed(() => issued.value !== null && seconds.value === 0);
const percent = computed(() => Math.min(100, Math.max(0, seconds.value / ttl.value * 100)));
const link = computed(() => issued.value ? pairingLink(globalThis.location.origin, issued.value.pairing_code) : "");
watch(opened, value => emit("active", value), { immediate: true });
const close = (): void => {
  generation++; stopClock?.(); stopCopy?.(); proof.value?.clear();
  issued.value = null; copied.value = false; copiedOnce.value = false; copyFailed.value = false;
  errorText.value = null; paired.value = false; opened.value = false; busy.value = false; copying.value = false;
};
onBeforeUnmount(() => { live = false; close(); });
const poll = async (version: number): Promise<void> => {
  if (polling || now.value - lastPoll < 5000) return;
  polling = true; lastPoll = now.value;
  try {
    const page = await api.listRemoteDevices();
    const added = page.items.find(device => device.status === "active" && !knownDevices.has(device.id));
    if (isLive() && version === generation && issued.value && !expired.value && added) {
      connectedDeviceName.value = added.client_info?.computer_name ?? added.display_name;
      paired.value = true; issued.value = null; stopClock?.(); stopCopy?.(); emit("paired");
    }
  } catch { /* Retry on a later tick without logging secrets or device data. */ }
  finally { polling = false; }
};
const issue = async (): Promise<void> => {
  if (busy.value) return;
  const version = generation;
  busy.value = true; errorText.value = null;
  opened.value = true; paired.value = false; await nextTick();
  if (!isLive() || version !== generation) return;
  const manager = proof.value;
  if (!manager?.ready) { busy.value = false; return; }
  try {
    if (!(await manager.authorize()) || !isLive() || version !== generation) return;
    try {
      const existing = await api.listRemoteDevices();
      knownDevices = new Set(existing.items.map(device => device.id));
    } catch (error: unknown) {
      if (!(error instanceof ArchiveApiError) || error.code !== "REMOTE_ARCHIVE_NOT_FOUND") throw error;
      knownDevices = new Set();
    }
    if (!isLive() || version !== generation) return;
    const code = await api.issuePairingCode();
    if (!isLive() || version !== generation) return;
    issued.value = code; copied.value = false; copiedOnce.value = false; copyFailed.value = false;
    now.value = Date.now(); ttl.value = Math.max(1, remainingSeconds(code.expires_at, now.value)); lastPoll = now.value;
    stopClock?.();
    stopClock = startClock(value => {
      now.value = value;
      if (seconds.value === 0) { stopClock?.(); copied.value = false; }
      else void poll(version);
    });
  } catch (error: unknown) {
    if (!isLive() || version !== generation) return;
    manager.requireProof(error);
    errorText.value = error instanceof ArchiveApiError ? error.message : "生成失败，请重试。";
  } finally { if (isLive() && version === generation) busy.value = false; }
};
const restart = async (): Promise<void> => {
  stopClock?.(); stopCopy?.(); copying.value = false; issued.value = null; copiedOnce.value = false; copied.value = false; copyFailed.value = false;
  proof.value?.clear();
  if (props.setup) await issue();
};
const isExpired = (): boolean => expired.value;
const hasIssued = (): boolean => issued.value !== null;
const copy = async (): Promise<void> => {
  if (!issued.value || expired.value || copying.value) return;
  const version = generation; const text = link.value; copying.value = true;
  const success = await copyText(text);
  if (!isLive() || version !== generation || isExpired() || !hasIssued()) return;
  copying.value = false; copied.value = success; copyFailed.value = !success;
  if (success) copiedOnce.value = true;
  stopCopy?.(); stopCopy = startTimeout(() => { copied.value = false; }, 2000);
};
const selectInformation = (event: FocusEvent): void => (event.target as HTMLTextAreaElement).select();
</script>

<template>
  <section
    class="server-pairing-card"
    data-testid="server-pairing-card"
  >
    <header
      v-if="!setup"
      class="pair-card-head"
    >
      <h3>{{ !opened ? '同步设备' : !issued && !busy && !paired ? '确认身份' : '连接电脑' }}</h3>
      <button
        v-if="!opened"
        class="button primary"
        type="button"
        @click="issue"
      >
        ＋ 连接电脑
      </button>
      <button
        v-else
        type="button"
        class="pair-close"
        aria-label="关闭电脑连接"
        @click="close"
      >
        ×
      </button>
    </header>
    <template v-if="paired">
      <p
        class="pair-joined"
        role="status"
      >
        ✓ <span data-feedback-mask="device-info">{{ connectedDeviceName }}</span>已接入
      </p>
      <button
        class="button"
        type="button"
        @click="close(); issue()"
      >
        连接另一台电脑
      </button>
    </template>
    <template v-else>
      <div v-show="!issued && !busy && !paired">
        <ServerManagementFields
          v-if="opened"
          ref="proof"
          :setup="setup"
          :disabled="busy"
        />
      </div>
      <div
        v-if="busy"
        class="pair-ticket"
        role="status"
        aria-label="正在生成配对信息"
      >
        <span
          class="pair-spinner"
          aria-hidden="true"
        />正在生成配对信息…
      </div>
      <template v-else-if="issued">
        <div
          class="pair-ticket"
          :class="{ 'is-expired': expired }"
        >
          <span
            class="pair-ring"
            :style="{ '--remaining': `${percent}%` }"
            aria-label="配对信息剩余有效时间"
          ><span>{{ formatCountdown(seconds) }}</span></span>
          <template v-if="expired">
            <p role="status">
              已过期
            </p><button
              class="button primary"
              type="button"
              @click="restart"
            >
              重新生成
            </button>
          </template>
          <button
            v-else
            class="button primary pair-copy"
            type="button"
            data-test="copy-information"
            title="一次性授权信息，请勿转发"
            :disabled="copying"
            @click="copy"
          >
            {{ copied ? '已复制 ✓' : '复制配对信息' }}
          </button>
        </div>
        <template v-if="!expired">
          <p
            v-if="copiedOnce"
            class="pair-next"
            role="status"
          >
            到要连接的电脑上粘贴
          </p>
          <p
            v-if="copyFailed"
            role="alert"
            class="notice-warn"
          >
            复制失败，请手动复制
          </p>
          <details
            :open="copyFailed"
            class="pair-manual"
          >
            <summary>手动复制</summary>
            <textarea
              :value="link"
              readonly
              aria-label="配对信息（包含远端地址）"
              data-feedback-mask="device-info"
              @focus="selectInformation"
            />
          </details>
        </template>
      </template>
      <template v-else-if="opened">
        <p
          v-if="errorText"
          role="alert"
          class="pair-failed"
        >
          生成失败：{{ errorText }}
        </p>
        <button
          class="button primary pair-continue"
          type="button"
          :disabled="!proof?.ready"
          @click="issue"
        >
          {{ errorText ? '重试' : setup ? '生成配对信息' : '继续' }}
        </button>
      </template>
    </template>
  </section>
</template>

<style scoped>
.server-pairing-card{--pair-green:#1d6a45;min-width:0}.pair-card-head{display:flex;align-items:center;justify-content:space-between;gap:12px;min-height:44px}.pair-card-head h3{font-size:17px;margin:0}.server-pairing-card .button.primary{border-radius:12px;background:var(--pair-green);border-color:var(--pair-green);color:#fff}.pair-close{min-height:44px;width:44px;border:0;background:none;font-size:24px;color:var(--ink-soft);cursor:pointer}.pair-ticket{display:flex;align-items:center;gap:16px;margin-top:16px;padding:14px;border:1px solid #dfe9d8;border-radius:16px;background:#f3f7ee}.pair-ring{position:relative;display:grid;place-items:center;width:60px;height:60px;flex:none;border-radius:50%}.pair-ring::before{content:"";position:absolute;inset:0;border-radius:50%;background:conic-gradient(var(--pair-green) var(--remaining),#d9e3d2 0);mask:radial-gradient(farthest-side,transparent calc(100% - 4px),#000 calc(100% - 3.5px))}.pair-ring>span{font-size:13px;font-weight:600;color:#1f4f36;font-variant-numeric:tabular-nums}.pair-copy{flex:1;min-height:52px;font-size:16px;white-space:nowrap}.is-expired{background:#f7f4ee;border-color:#ebe5d8}.is-expired .pair-ring>span{color:#6c7269}.is-expired p{margin:0;color:#b2412f}.is-expired button{margin-left:auto}.pair-next{margin:12px 0 0;color:#1f4f36;font-size:14px}.pair-manual{margin-top:4px}.pair-manual summary{display:flex;align-items:center;min-height:44px;color:var(--ink-soft);font-size:13px;cursor:pointer}.pair-manual textarea{box-sizing:border-box;width:100%;height:110px;resize:vertical;padding:12px;overflow-wrap:anywhere;border:1px solid var(--rule);border-radius:12px;background:var(--surface-sunken);color:inherit;font-size:13px}.pair-failed{padding:14px;border:1px solid #f0d9d2;border-radius:16px;background:#fbf1ee;color:#b2412f;overflow-wrap:anywhere}.pair-continue{min-height:44px;margin-top:12px}.pair-joined{padding:14px;border-radius:14px;background:#eaf3e6;color:#1f4f36;font-weight:600}.pair-spinner{width:22px;height:22px;border:2px solid #d9e3d2;border-top-color:var(--pair-green);border-radius:50%;animation:pair-spin .8s linear infinite}button:focus-visible,textarea:focus-visible,summary:focus-visible{outline:2px solid var(--pair-green);outline-offset:3px}@keyframes pair-spin{to{transform:rotate(360deg)}}@media(max-width:360px){.pair-ticket{padding:12px;gap:10px}.pair-ring{width:48px;height:48px}.pair-ring>span{font-size:12px}.pair-copy{font-size:15px;padding:10px}.is-expired{flex-wrap:wrap}}@media(prefers-reduced-motion:reduce){.pair-spinner{animation:none}}
</style>

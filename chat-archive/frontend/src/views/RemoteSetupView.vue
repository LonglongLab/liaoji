<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import AppShell from "@/components/AppShell.vue";
import QrCode from "@/components/QrCode.vue";
import PasswordInput from "@/components/PasswordInput.vue";
import ServerStorageSettings from "@/components/ServerStorageSettings.vue";
import ServerPairingCard from "@/components/ServerPairingCard.vue";
import type { ServerConfigurationState } from "@/components/serverManagement";
import { useCapabilityStore } from "@/stores/capabilities";
import { useRemoteAuthStore } from "@/stores/remoteAuth";

const api = useArchiveApi();
const auth = useRemoteAuthStore();
const router = useRouter();

const fragment = new URLSearchParams(globalThis.location.hash.slice(1));
const fromLink = fragment.get("setupToken") ?? fragment.get("bootstrap_token") ?? "";
if (fromLink !== "") globalThis.history.replaceState(globalThis.history.state, "", globalThis.location.pathname + globalThis.location.search);
const bootstrapToken = ref(fromLink);
const tokenPrefilled = ref(fromLink !== "");
const capabilities = useCapabilityStore();
const later = ref<"storage" | "backup" | "pair" | "done">("storage");
const storageStatus = ref("未确认");
const backupStatus = ref("未确认");
const stateLabel = (state: ServerConfigurationState): string => ({ configured: "已配置（未更改）", not_configured: "未配置", unreadable: "凭据需重填", unknown: "未能读取" })[state];
const skipStorage = (state: ServerConfigurationState): void => { storageStatus.value = stateLabel(state); later.value = "backup"; };
const skipBackup = (state: ServerConfigurationState): void => { backupStatus.value = stateLabel(state); later.value = "pair"; };
const paired = ref(false);
const recoverySaved = ref(false);
const managementAvailable = computed(() => capabilities.states["object-storage-config"] === "available");
const backupAvailable = computed(() => capabilities.states["backup-management"] === "available");
const pairAvailable = computed(() => capabilities.states["pairing-code"] === "available");
const password = ref("");
const confirmation = ref("");
const totpCode = ref("");

const mismatch = computed(
  () => confirmation.value !== "" && confirmation.value !== password.value,
);
const canSubmitCredentials = computed(
  () =>
    !auth.busy &&
    bootstrapToken.value.trim() !== "" &&
    password.value.length >= 12 &&
    password.value === confirmation.value,
);
const throttledSeconds = computed(() => auth.failure?.retryAfterSeconds ?? null);
const errorText = computed(() => auth.failure?.message ?? null);

// 六步的名字与顺序（PA-PAGE-005）。名字来自 store 的 `step`，不另存一份：
// 两处各存一份迟早对不上，而这一段的全部价值就是「它说的位置是真的」。
const labels = { credentials: "设置口令", totp: "两步验证", storage: "附件存储", backup: "定期备份", pair: "连接电脑", done: "完成" } as const;
const order = ["credentials", "totp", "storage", "backup", "pair", "done"] as const;
const supportsStep = (key: typeof order[number]): boolean => key === "storage" ? managementAvailable.value : key === "backup" ? backupAvailable.value : key === "pair" ? pairAvailable.value : true;
const actualOrder = computed(() => order.filter(key => capabilities.authState !== "authenticated" || supportsStep(key)));
const stepNames = computed(() => actualOrder.value.map(key => labels[key]));
const currentLater = computed(() => order.find((key, index) => index >= order.indexOf(later.value) && supportsStep(key)) ?? "done");
const stepIndex = computed(() => actualOrder.value.indexOf(auth.step === "credentials" || auth.step === "totp" ? auth.step : currentLater.value));

// 绑定两步验证要口令（防止一条被窃会话绑上别人的验证器）。向导里刚刚才输过，
// 再问一遍纯属添堵，所以在这一屏的生命周期内留在**组件本地**的 ref 里——
// 不进 store、不落存储，绑完或跳过即清。
const enrollmentPassword = ref("");

const forgetPassword = (): void => {
  password.value = "";
  confirmation.value = "";
  enrollmentPassword.value = "";
};

const submitCredentials = async (): Promise<void> => {
  if (!canSubmitCredentials.value) return;
  const ok = await auth.completeSetup(api, bootstrapToken.value, password.value);
  if (!ok) return;
  bootstrapToken.value = "";
  enrollmentPassword.value = password.value;
  password.value = "";
  confirmation.value = "";
};

const startEnrollment = async (): Promise<void> => {
  await auth.beginTotpEnrollment(api, enrollmentPassword.value);
};

const skipEnrollment = (): void => {
  auth.skipTotp();
  forgetPassword();
};

const confirmEnrollment = async (): Promise<void> => {
  if (!/^\d{6}$/.test(totpCode.value)) return;
  const ok = await auth.enableTotp(api, totpCode.value);
  if (ok) {
    totpCode.value = "";
    forgetPassword();
  }
};

const warnUnsavedRecovery = (event: BeforeUnloadEvent): void => {
  if (auth.recoveryCodes.length > 0 && !recoverySaved.value) { event.preventDefault(); }
};
globalThis.addEventListener("beforeunload", warnUnsavedRecovery);
onBeforeUnmount(() => { forgetPassword(); bootstrapToken.value = ""; globalThis.removeEventListener("beforeunload", warnUnsavedRecovery); });
const finish = async (): Promise<void> => {
  if (auth.recoveryCodes.length > 0 && !recoverySaved.value) return;
  auth.reset();
  forgetPassword();
  await router.replace("/remote/conversations");
};
</script>

<template>
  <AppShell
    variant="onboarding"
    :account-name="undefined"
  >
    <section class="remote-unconfigured">
      <!--
        一次性、不可回退、还带一次性令牌的流程，最需要知道还剩多少
        （PA-PAGE-005 / §12.7.2 (h)）。与本机建档向导（第 N 步，共 5 步）
        同一套写法：一句位置 + 一条分段进度。
      -->
      <div
        class="setup-progress"
        data-testid="setup-progress"
      >
        <p class="setup-position">
          第 {{ stepIndex + 1 }} 步，共 {{ stepNames.length }} 步 · {{ stepNames[stepIndex] }}
        </p>
        <!-- 条本身是重复的视觉提示：上面那一句已经把位置说全了，所以它对
             读屏隐藏，而不是让人再听一遍六个空列表项。 -->
        <div
          class="setup-progress-track"
          aria-hidden="true"
        >
          <span
            v-for="(name, index) in stepNames"
            :key="name"
            :class="{ 'is-reached': index <= stepIndex }"
          />
        </div>
      </div>
      <div class="remote-unconfigured-card auth-card">
        <template v-if="auth.step === 'credentials'">
          <h1>设置登录口令</h1>
          <p class="action-note">
            远程档案 · 首次设置
          </p>
          <p
            v-if="tokenPrefilled"
            class="info-note"
            role="status"
          >
            安装令牌已带入
          </p>
          <details
            v-else
            class="server-help"
          >
            <summary>找不到安装令牌？</summary><p>安装结束时显示的设置链接已包含一次性引导令牌，也可在服务器执行：</p><code>docker compose exec api cat /var/lib/chat-archive/browser-bootstrap-token</code>
          </details>
          <form
            class="auth-form"
            @submit.prevent="submitCredentials"
          >
            <label
              v-if="!tokenPrefilled"
              class="auth-label"
              for="setup-token"
            >引导令牌</label>
            <input
              v-if="!tokenPrefilled"
              id="setup-token"
              v-model="bootstrapToken"
              type="text"
              autocomplete="off"
              spellcheck="false"
              :disabled="auth.busy"
              @input="auth.clearFailure()"
            >

            <label
              class="auth-label"
              for="setup-password"
            >登录口令</label>
            <PasswordInput
              id="setup-password"
              v-model="password"
              autocomplete="new-password"
              aria-describedby="setup-password-note"
              :disabled="auth.busy"
              @input="auth.clearFailure()"
            />
            <p
              id="setup-password-note"
              class="action-note auth-note"
            >
              至少 12 位，使用独立口令。
            </p>

            <label
              class="auth-label"
              for="setup-confirmation"
            >再输一次</label>
            <PasswordInput
              id="setup-confirmation"
              v-model="confirmation"
              autocomplete="new-password"
              :disabled="auth.busy"
            />
            <p
              v-if="mismatch"
              class="action-note auth-note auth-mismatch"
              role="alert"
            >
              两次输入不一致。
            </p>

            <button
              class="button primary large"
              type="submit"
              :disabled="!canSubmitCredentials"
            >
              {{ auth.busy ? "正在设置……" : "设置口令并继续" }}
            </button>
          </form>
        </template>

        <template v-else-if="auth.step === 'totp'">
          <h1>开启两步验证</h1>
          <p>
            推荐开启，登录时多一重保护。
          </p>

          <template v-if="auth.enrollment === null">
            <div class="button-row">
              <button
                class="button primary"
                type="button"
                :disabled="auth.busy"
                @click="startEnrollment"
              >
                开始绑定
              </button>
              <button
                class="button quiet"
                type="button"
                :disabled="auth.busy"
                @click="skipEnrollment"
              >
                暂时跳过
              </button>
            </div>
          </template>

          <template v-else>
            <QrCode
              :value="auth.enrollment.otpauth_uri"
              label="两步验证绑定二维码"
            />
            <p class="action-note">
              用验证器扫码。就在这台手机上设置时扫不了自己的屏幕——
              <a :href="auth.enrollment.otpauth_uri">点这里直接唤起验证器</a>，
              或手工录入密钥：
            </p>
            <p class="auth-secret">
              <code>{{ auth.enrollment.secret }}</code>
            </p>

            <form
              class="auth-form"
              @submit.prevent="confirmEnrollment"
            >
              <label
                class="auth-label"
                for="setup-totp"
              >回填一个当前验证码</label>
              <input
                id="setup-totp"
                v-model="totpCode"
                type="text"
                inputmode="numeric"
                autocomplete="one-time-code"
                aria-describedby="setup-totp-note"
                :disabled="auth.busy"
                @input="auth.clearFailure()"
              >
              <p
                id="setup-totp-note"
                class="action-note auth-note"
              >
                确认验证码后才会开启。
              </p>
              <button
                class="button primary large"
                type="submit"
                :disabled="auth.busy || totpCode.trim() === ''"
              >
                确认开启
              </button>
            </form>
          </template>
        </template>

        <template v-else-if="currentLater === 'storage'">
          <ServerStorageSettings
            v-if="managementAvailable"
            key="storage"
            kind="storage"
            setup
            @saved="storageStatus = '已配置'; later = 'backup'"
            @skip="skipStorage"
          />
          <template v-else>
            <h2>附件存储</h2><p class="info-note">
              此服务端未提供网页配置，可更新服务端或在已接入电脑上配置。
            </p><button
              class="button quiet"
              type="button"
              @click="later = 'backup'"
            >
              暂不配置
            </button>
          </template>
        </template>
        <template v-else-if="currentLater === 'backup'">
          <ServerStorageSettings
            v-if="backupAvailable"
            key="backup"
            kind="backup"
            setup
            @saved="backupStatus = '已开启'; later = 'pair'"
            @skip="skipBackup"
          />
          <template v-else>
            <h2>定期备份</h2><p class="info-note">
              此部署未提供网页备份配置。
            </p><button
              class="button quiet"
              type="button"
              @click="later = 'pair'"
            >
              暂不开启
            </button>
          </template>
        </template>
        <template v-else-if="currentLater === 'pair'">
          <h1>连接电脑</h1><ServerPairingCard
            v-if="pairAvailable"
            setup
            @paired="paired = true"
          /><p
            v-else
            class="info-note"
          >
            此服务端暂未提供网页配对码。
          </p>
          <button
            class="button quiet"
            type="button"
            @click="later = 'done'"
          >
            {{ paired ? '完成' : '稍后连接' }}
          </button>
        </template>
        <template v-else>
          <h1>设置完成</h1>
          <dl class="server-setup-summary">
            <div><dt>登录口令</dt><dd>已设置</dd></div><div><dt>两步验证</dt><dd>{{ auth.recoveryCodes.length ? '已开启' : '未开启' }}</dd></div><div v-if="managementAvailable">
              <dt>附件存储</dt><dd>{{ storageStatus }}</dd>
            </div><div v-if="backupAvailable">
              <dt>定期备份</dt><dd>{{ backupStatus }}</dd>
            </div><div v-if="pairAvailable">
              <dt>电脑</dt><dd>{{ paired ? '已接入' : '还没有接入' }}</dd>
            </div>
          </dl><p class="action-note">
            未完成的项可稍后在设置中补充。
          </p>
          <template v-if="auth.recoveryCodes.length > 0">
            <p>
              这 {{ auth.recoveryCodes.length }} 个恢复码<strong>只显示这一次</strong>。
              保存在安全的地方。
            </p>
            <ul class="auth-recovery-codes">
              <li
                v-for="code in auth.recoveryCodes"
                :key="code"
              >
                <code>{{ code }}</code>
              </li>
            </ul>
          </template>
          <p v-else>
            两步验证暂时没有开启。之后可以在「设置」里随时开。
          </p>
          <label
            v-if="auth.recoveryCodes.length"
            class="server-recovery-check"
          ><input
            v-model="recoverySaved"
            type="checkbox"
          >我已将恢复码保存在安全的地方</label>
          <button
            class="button primary large"
            type="button"
            :disabled="auth.recoveryCodes.length > 0 && !recoverySaved"
            @click="finish"
          >
            进入归档
          </button>
        </template>

        <p
          v-if="errorText !== null"
          class="notice-warn auth-error"
          role="alert"
        >
          <strong>没能完成这一步</strong>
          {{ errorText }}
          <span v-if="throttledSeconds !== null">请等待约 {{ throttledSeconds }} 秒后重试。</span>
        </p>
      </div>
    </section>
  </AppShell>
</template>

<style scoped>
.sr-only{position:absolute;width:1px;height:1px;padding:0;margin:-1px;overflow:hidden;clip-path:inset(50%);white-space:nowrap;border:0}

.server-setup-summary>div{display:flex;justify-content:space-between;gap:16px;padding:12px 0;border-bottom:1px solid var(--rule-soft)}.server-setup-summary dd{margin:0;font-weight:700}.server-recovery-check{display:flex;gap:8px;align-items:flex-start;margin:18px 0}.server-recovery-check input{width:18px;height:18px;accent-color:var(--accent-solid)}.auth-card code{overflow-wrap:anywhere}
</style>

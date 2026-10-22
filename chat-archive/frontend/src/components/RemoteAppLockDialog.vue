<script setup lang="ts">
import { computed, ref } from "vue";

import { useArchiveApi } from "@/api/injection";
import PasswordInput from "@/components/PasswordInput.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import { type AppLockMode, passkeyLabelFor } from "@/lib/appLock";
import { readBrowserClientInfo } from "@/lib/browserClientInfo";
import { isWebAuthnAvailable } from "@/lib/webauthn";
import { useReauthStore } from "@/stores/reauth";

/**
 * 本机应用锁对话框（PSU-2.221，原型 P19 对话框）。桌面居中，H5 底部抽屉。
 *
 * - 开启 / 重新录入：口令 → 系统验证 → 完成，顶部三段进度。
 * - 关闭：先确认；5 分钟内验证过就直接生效，否则走本机系统验证。系统验证没通过或
 *   这个浏览器用不了时，可改用登录口令确认。
 *
 * 系统验证框由操作系统弹出，这里只画「等你在系统弹窗里验证」。
 */
type Step = "password" | "confirm" | "verifying" | "error" | "done";

const props = defineProps<{ mode: AppLockMode }>();
const emit = defineEmits<{ close: []; finished: [notice: string] }>();

const api = useArchiveApi();
const reauth = useReauthStore();
const browserReady = isWebAuthnAvailable();

const step = ref<Step>(props.mode === "disable" ? "confirm" : "password");
const password = ref("");
const passwordError = ref<string | null>(null);
const errorTitle = ref("");
const open = ref(true);

const close = (): void => {
  if (step.value === "verifying") return;
  password.value = "";
  open.value = false;
  emit("close");
};
const { container, initial, onKeydown } = useModalFocus(open, close);
void container;
void initial;

const title = computed(() => {
  if (props.mode === "reenroll") return "重新录入";
  if (props.mode === "disable") return "关闭本机应用锁？";
  return "开启本机应用锁";
});
const passwordNote = computed(() =>
  props.mode === "disable"
    ? "输入登录口令确认关闭。"
    : "只对这台设备生效，其他设备需要在那台设备上单独开启。指纹没通过时，可以用登录口令解锁。",
);
const nextLabel = computed(() => (props.mode === "disable" ? "关闭" : "下一步"));
const doneTitle = computed(() => (props.mode === "reenroll" ? "已重新录入" : "本机应用锁已开启"));
const stepperShown = computed(() => props.mode !== "disable");
const steps = computed(() => {
  const at = step.value === "password" ? 0 : step.value === "done" ? 2 : 1;
  return ["口令", "验证", "完成"].map((label, index) => ({
    label,
    state: index < at ? "done" : index === at ? "active" : "todo",
  }));
});
/** 关闭时系统验证不了：给「改用登录口令」。开启 / 重新录入本身就要登记系统验证，没有这条路。 */
const passwordFallbackShown = computed(() => props.mode === "disable");

/** 把失败落到对话框的哪一步、怎么说。口令错回到口令那一步。 */
const settleFailure = (): void => {
  const failure = reauth.failure;
  reauth.clearFailure();
  if (failure?.code === "REMOTE_SIGN_IN_REJECTED") {
    passwordError.value = "口令不对";
    step.value = "password";
    return;
  }
  if (failure?.retryAfterSeconds !== null && failure?.retryAfterSeconds !== undefined) {
    errorTitle.value = `${String(failure.retryAfterSeconds)} 秒后再试`;
  } else if (failure?.code === "WEBAUTHN_CANCELLED") {
    errorTitle.value = "已取消";
  } else if (failure?.code === "WEBAUTHN_ALREADY_REGISTERED") {
    errorTitle.value = "这台设备已经录入过";
  } else if (failure?.code === "WEBAUTHN_UNSUPPORTED") {
    errorTitle.value = "这个浏览器用不了";
  } else if (failure?.code === "REMOTE_REAUTH_REJECTED" || failure?.code === "WEBAUTHN_FAILED") {
    errorTitle.value = "验证没通过";
  } else {
    errorTitle.value = failure?.message ?? "没有成功";
  }
  step.value = "error";
};

const deviceLabel = async (): Promise<string> => passkeyLabelFor(await readBrowserClientInfo());

const succeed = (): void => {
  password.value = "";
  if (props.mode === "disable") {
    open.value = false;
    emit("finished", "本机应用锁已关闭");
    return;
  }
  step.value = "done";
};

/** 开启 / 重新录入：口令那一步提交后登记本机系统验证。 */
const enroll = async (): Promise<void> => {
  step.value = "verifying";
  const label = await deviceLabel();
  const ok =
    props.mode === "reenroll"
      ? await reauth.reenroll(api, password.value, label)
      : await reauth.enable(api, password.value, label);
  if (ok) succeed();
  else settleFailure();
};

const usePassword = (): void => {
  password.value = "";
  passwordError.value = null;
  step.value = "password";
};

/** 关闭：确认后走本机系统验证（5 分钟内验证过则直接生效，按钮转圈即可）。 */
const disableWithPasskey = async (): Promise<void> => {
  if (!browserReady && !reauth.freshNow()) {
    usePassword();
    return;
  }
  if (!reauth.freshNow()) step.value = "verifying";
  if (await reauth.disable(api)) succeed();
  else settleFailure();
};

const disableWithPassword = async (): Promise<void> => {
  if (await reauth.disableWithPassword(api, password.value)) succeed();
  else settleFailure();
};

const next = async (): Promise<void> => {
  if (password.value === "" || reauth.busy) return;
  passwordError.value = null;
  if (props.mode === "disable") await disableWithPassword();
  else await enroll();
};

const retry = async (): Promise<void> => {
  if (props.mode === "disable") await disableWithPasskey();
  // 开启 / 重新录入：口令还在（只在成功或关闭时清空），直接再登记一次。
  else if (password.value !== "") await enroll();
  else usePassword();
};

const finish = (): void => {
  open.value = false;
  emit("finished", props.mode === "reenroll" ? "已重新录入" : "本机应用锁已开启");
};
</script>

<template>
  <div
    class="app-lock-scrim"
  >
    <div
      ref="container"
      class="app-lock-dialog"
      role="dialog"
      aria-modal="true"
      :aria-label="title"
      :aria-busy="step === 'verifying'"
      data-testid="app-lock-dialog"
      @keydown="onKeydown"
    >
      <span
        class="app-lock-grabber"
        aria-hidden="true"
      />

      <ol
        v-if="stepperShown"
        class="app-lock-steps"
        aria-label="进度"
      >
        <li
          v-for="item in steps"
          :key="item.label"
          :class="`is-${item.state}`"
          :aria-current="item.state === 'active' ? 'step' : undefined"
        >
          <span class="app-lock-step-bar" />
          {{ item.label }}
        </li>
      </ol>

      <form
        v-if="step === 'password'"
        @submit.prevent="next"
      >
        <h3>{{ title }}</h3>
        <label
          class="auth-label"
          for="app-lock-password"
        >登录口令</label>
        <PasswordInput
          id="app-lock-password"
          ref="initial"
          v-model="password"
          autocomplete="current-password"
          :aria-invalid="passwordError !== null"
          :aria-describedby="passwordError !== null ? 'app-lock-password-error' : undefined"
          @input="passwordError = null"
        />
        <p
          v-if="passwordError !== null"
          id="app-lock-password-error"
          class="app-lock-field-error"
          role="alert"
        >
          {{ passwordError }}
        </p>
        <p class="action-note app-lock-passkey-note">
          {{ passwordNote }}
        </p>
        <div class="app-lock-actions">
          <button
            class="button secondary"
            type="button"
            @click="close"
          >
            取消
          </button>
          <button
            class="button"
            :class="mode === 'disable' ? 'app-lock-danger' : 'primary'"
            type="submit"
            data-feedback-action="应用锁下一步"
            :disabled="password === '' || reauth.busy"
          >
            {{ nextLabel }}
          </button>
        </div>
      </form>

      <template v-else-if="step === 'confirm'">
        <h3>{{ title }}</h3>
        <p class="app-lock-line">
          关闭后这台设备打开时不再需要验证，其他设备不受影响。系统里保存的通行密钥可以自行删除。
        </p>
        <div class="app-lock-actions">
          <button
            ref="initial"
            class="button secondary"
            type="button"
            :disabled="reauth.busy"
            @click="close"
          >
            取消
          </button>
          <button
            class="button app-lock-danger"
            type="button"
            data-feedback-action="确认应用锁操作"
            :disabled="reauth.busy"
            @click="disableWithPasskey"
          >
            关闭
          </button>
        </div>
      </template>

      <div
        v-else-if="step === 'verifying'"
        class="app-lock-center"
        role="status"
      >
        <span class="app-lock-badge is-pulsing">
          <svg
            width="38"
            height="38"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.6"
            stroke-linecap="round"
            stroke-linejoin="round"
            aria-hidden="true"
          ><path d="M12 11v2.5a7 7 0 0 1-1.2 3.9" /><path d="M8.8 9.6A3.6 3.6 0 0 1 15.6 11v2.2" /><path d="M6.2 8a6.6 6.6 0 0 1 12 3.6v1.2" /><path d="M8.6 13v.8a9 9 0 0 1-1 4.2" /><path d="M15.4 15.8a12 12 0 0 1-1.3 3.6" /></svg>
        </span>
        <strong>在系统弹窗中验证</strong>
        <span class="app-lock-chips">
          <span>指纹</span><span>面容</span><span>设备密码</span>
        </span>
      </div>

      <template v-else-if="step === 'error'">
        <div
          class="app-lock-center"
          role="alert"
        >
          <span
            class="app-lock-badge is-error"
            aria-hidden="true"
          >!</span>
          <strong>{{ errorTitle }}</strong>
        </div>
        <div class="app-lock-actions">
          <button
            class="button secondary"
            type="button"
            @click="close"
          >
            取消
          </button>
          <button
            ref="initial"
            class="button primary"
            type="button"
            data-feedback-action="应用锁重试"
            @click="retry"
          >
            重试
          </button>
        </div>
        <button
          v-if="passwordFallbackShown"
          class="app-unlock-link is-accent app-lock-use-password"
          type="button"
          data-feedback-action="改用登录口令"
          @click="usePassword"
        >
          改用登录口令
        </button>
      </template>

      <template v-else>
        <div
          class="app-lock-center"
          role="status"
        >
          <span
            class="app-lock-badge is-done"
            aria-hidden="true"
          >
            <svg
              width="36"
              height="36"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="2.4"
              stroke-linecap="round"
              stroke-linejoin="round"
            ><path d="M5 12.5l4.5 4.5L19 7.5" /></svg>
          </span>
          <strong>{{ doneTitle }}</strong>
        </div>
        <div class="app-lock-actions is-single">
          <button
            ref="initial"
            class="button primary"
            type="button"
            @click="finish"
          >
            完成
          </button>
        </div>
      </template>
    </div>
  </div>
</template>

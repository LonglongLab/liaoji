<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
import { Fingerprint, Lock } from "@lucide/vue";
import { computed, nextTick, onMounted, ref } from "vue";
import { useRoute, useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import AppShell from "@/components/AppShell.vue";
import PasswordInput from "@/components/PasswordInput.vue";
import { localUnlockMethod } from "@/lib/appLock";
import { startClock } from "@/lib/clock";
import { isWebAuthnAvailable } from "@/lib/webauthn";
import { unlockNextPath } from "@/lib/workspaceRouting";
import { useReauthStore } from "@/stores/reauth";
import { useRemoteAuthStore } from "@/stores/remoteAuth";

/**
 * 锁定页（PSU-2.221，原型 #p21）。参照手机锁屏：指纹 / 面容 / Windows Hello 是本机的
 * 便捷解锁，登录口令是兜底。
 *
 * 系统验证框只在点了主按钮之后才弹：页面自己弹既会被 Safari 拒（没有用户手势），
 * 也会在人还没准备好时打扰他。下方常驻「使用密码解锁」，点开在同一张卡里输口令；
 * 浏览器用不了系统验证，或连续 3 次没通过时，直接展开口令框。口令解锁只验登录口令，
 * 成功后本机解锁，应用锁保持开启。
 */
const FAILURES_BEFORE_PASSWORD = 3;

const api = useArchiveApi();
const reauth = useReauthStore();
const auth = useRemoteAuthStore();
const router = useRouter();
const route = useRoute();

const method = localUnlockMethod();
const browserReady = isWebAuthnAvailable();
const unlocking = ref(false);
const failures = ref(0);
const passwordOpen = ref(false);
const password = ref("");
const passwordError = ref<string | null>(null);
const passwordInput = ref<{ focus?: () => void } | null>(null);
const unlockError = ref<string | null>(null);
const waitSeconds = ref(0);
let stopWaitTimer: (() => void) | undefined;

onMounted(() => {
  // 只为知道服务器这一半还能不能用通行密钥；读不到就按能用处理，点了再说。
  void reauth.refresh(api);
});

/** 系统验证这条路走不通：浏览器不支持，或服务器没配置 WebAuthn。 */
const passkeyUnavailable = computed(() => !browserReady || reauth.state?.supported === false);
const passwordOnly = computed(() => reauth.state !== null && (!reauth.enabled || !reauth.state.has_passkeys));
const passwordShown = computed(() => passwordOpen.value || passkeyUnavailable.value || passwordOnly.value);

const startWait = (seconds: number): void => {
  stopWaitTimer?.();
  waitSeconds.value = seconds;
  stopWaitTimer = startClock(() => {
    waitSeconds.value -= 1;
    if (waitSeconds.value <= 0) {
      stopWaitTimer?.();
      stopWaitTimer = undefined;
      waitSeconds.value = 0;
      unlockError.value = null;
    }
  });
};

const blocked = computed(() => passkeyUnavailable.value || reauth.busy || waitSeconds.value > 0);
const unlockLabel = computed(() => {
  if (passkeyUnavailable.value) return "这个浏览器用不了";
  if (waitSeconds.value > 0) return `${String(waitSeconds.value)} 秒后再试`;
  return unlocking.value ? "等待验证…" : method.unlockLabel;
});
const shownError = computed(() =>
  passkeyUnavailable.value ? "这个浏览器用不了系统验证，请用密码解锁" : unlockError.value,
);
const passwordBlocked = computed(() => password.value === "" || reauth.busy || waitSeconds.value > 0);
const passwordLabel = computed(() => {
  if (waitSeconds.value > 0) return `${String(waitSeconds.value)} 秒后再试`;
  return reauth.busy && !unlocking.value ? "正在验证…" : "解锁";
});

const leave = async (): Promise<void> => {
  // 回到锁定前原本要去的远程页面；`next` 不是站内远程路径时回会话列表。
  await router.replace(unlockNextPath(route.query.next));
};

const openPassword = async (): Promise<void> => {
  passwordOpen.value = true;
  passwordError.value = null;
  await nextTick();
  passwordInput.value?.focus?.();
};

const unlock = async (): Promise<void> => {
  if (blocked.value) return;
  unlockError.value = null;
  unlocking.value = true;
  const ok = await reauth.unlock(api);
  unlocking.value = false;
  if (ok) {
    await leave();
    return;
  }
  const failure = reauth.failure;
  reauth.clearFailure();
  if (failure?.retryAfterSeconds !== null && failure?.retryAfterSeconds !== undefined) {
    unlockError.value = "试太多次了";
    startWait(failure.retryAfterSeconds);
    return;
  }
  if (failure?.code === "WEBAUTHN_CANCELLED") {
    unlockError.value = "已取消，再试一次";
  } else if (failure?.code === "REMOTE_REAUTH_REJECTED" || failure?.code === "WEBAUTHN_FAILED") {
    unlockError.value = "验证没通过，再试一次";
  } else {
    unlockError.value = failure?.message ?? "没有解锁，再试一次";
  }
  // 连续 3 次没通过：直接展开口令框（像手机指纹失败几次后转锁屏密码）。
  failures.value += 1;
  if (failures.value >= FAILURES_BEFORE_PASSWORD && !passwordOpen.value) await openPassword();
};

const submitPassword = async (): Promise<void> => {
  if (passwordBlocked.value) return;
  passwordError.value = null;
  const ok = await reauth.unlockWithPassword(api, password.value);
  password.value = "";
  if (ok) {
    await leave();
    return;
  }
  const failure = reauth.failure;
  reauth.clearFailure();
  if (failure?.retryAfterSeconds !== null && failure?.retryAfterSeconds !== undefined) {
    passwordError.value = "试太多次了";
    startWait(failure.retryAfterSeconds);
  } else if (failure?.code === "REMOTE_SIGN_IN_REJECTED") {
    passwordError.value = "口令不对";
  } else {
    passwordError.value = failure?.message ?? "没有解锁，再试一次";
  }
};

const signOut = async (): Promise<void> => {
  await auth.signOut(api);
  await router.replace("/login");
};
</script>

<template>
  <AppShell
    variant="onboarding"
    :account-name="undefined"
  >
    <section class="remote-unconfigured">
      <div
        class="remote-unconfigured-card auth-card app-unlock-card"
        data-testid="remote-unlock"
      >
        <div class="app-unlock-main">
          <span
            class="app-lock-badge is-large"
            :class="{ 'is-pulsing': unlocking }"
            aria-hidden="true"
          >
            <Fingerprint
              v-if="unlocking"
              :size="40"
              :stroke-width="1.6"
              aria-hidden="true"
            />
            <Lock
              v-else
              :size="36"
              :stroke-width="1.8"
              aria-hidden="true"
            />
          </span>
          <h1>已锁定</h1>
          <p class="app-unlock-sub">
            验证后继续查看，1 小时内免验
          </p>

          <AppAction
            v-if="!passwordOnly"
            data-feedback-action="解锁"
            data-testid="remote-unlock-passkey"
            class="button primary large app-unlock-button"
            variant="primary"
            type="button"
            :disabled="blocked"
            @click="unlock"
          >
            <Fingerprint
              :size="18"
              :stroke-width="1.8"
              aria-hidden="true"
            />
            {{ unlockLabel }}
          </AppAction>

          <p
            v-if="shownError !== null"
            class="app-unlock-error"
            role="alert"
          >
            <span aria-hidden="true">!</span>{{ shownError }}
          </p>

          <AppAction
            v-if="!passwordShown"
            class="app-unlock-link is-accent"
            variant="quiet"
            type="button"
            data-testid="remote-unlock-password-toggle"
            data-feedback-action="使用密码解锁"
            @click="openPassword"
          >
            使用密码解锁
          </AppAction>

          <!-- 口令兜底：同一张卡内展开，不换页。 -->
          <form
            v-else
            class="app-unlock-password"
            data-testid="remote-unlock-password"
            @submit.prevent="submitPassword"
          >
            <label
              class="auth-label"
              for="unlock-password"
            >登录口令</label>
            <PasswordInput
              id="unlock-password"
              ref="passwordInput"
              v-model="password"
              autocomplete="current-password"
              :disabled="reauth.busy"
              :aria-invalid="passwordError !== null"
              :aria-describedby="passwordError !== null ? 'unlock-password-error' : undefined"
              @input="passwordError = null"
            />
            <p
              v-if="passwordError !== null"
              id="unlock-password-error"
              class="app-lock-field-error"
              role="alert"
            >
              {{ passwordError }}
            </p>
            <AppAction
              class="button primary"
              variant="primary"
              type="submit"
              data-feedback-action="口令解锁"
              :disabled="passwordBlocked"
            >
              {{ passwordLabel }}
            </AppAction>
            <p class="app-unlock-password-note">
              和登录时用的口令相同
            </p>
          </form>

          <div class="app-unlock-links">
            <AppAction
              class="app-unlock-link"
              variant="quiet"
              type="button"
              data-feedback-action="退出登录"
              :disabled="auth.busy || reauth.busy"
              @click="signOut"
            >
              退出登录
            </AppAction>
          </div>
        </div>
      </div>
    </section>
  </AppShell>
</template>

<script setup lang="ts">
import { computed, ref } from "vue";
import { useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import AppShell from "@/components/AppShell.vue";
import { useRemoteAuthStore } from "@/stores/remoteAuth";

const api = useArchiveApi();
const auth = useRemoteAuthStore();
const router = useRouter();

const password = ref("");
const secondFactor = ref("");

// 退避剩余秒数由服务端的 `Retry-After` 给，不在前端自己倒数：本地时钟与
// 服务端时钟对不上时，倒数到 0 却仍然被拒，比直接说「请稍后重试」更糟。
const throttledSeconds = computed(() => auth.failure?.retryAfterSeconds ?? null);
const errorText = computed(() => auth.failure?.message ?? null);

const submit = async (): Promise<void> => {
  if (password.value === "") return;
  const ok = await auth.signIn(api, password.value, secondFactor.value);
  if (!ok) return;
  password.value = "";
  secondFactor.value = "";
  // 去哪儿由守卫按本端能力决定；这里只是把地址栏推离登录页。
  await router.replace("/remote/conversations");
};
</script>

<template>
  <AppShell
    variant="onboarding"
    :account-name="undefined"
  >
    <section class="remote-unconfigured">
      <div class="remote-unconfigured-card auth-card">
        <h1>登录远程归档</h1>
        <p>这台服务器保存着已同步的聊天记录。请输入登录口令。</p>

        <form
          class="auth-form"
          @submit.prevent="submit"
        >
          <label
            class="auth-label"
            for="remote-password"
          >登录口令</label>
          <input
            id="remote-password"
            v-model="password"
            type="password"
            name="password"
            autocomplete="current-password"
            :disabled="auth.busy"
            @input="auth.clearFailure()"
          >

          <label
            class="auth-label"
            for="remote-second-factor"
          >两步验证码</label>
          <input
            id="remote-second-factor"
            v-model="secondFactor"
            type="text"
            autocomplete="one-time-code"
            placeholder="未开启可留空"
            aria-describedby="remote-second-factor-note"
            :disabled="auth.busy"
            @input="auth.clearFailure()"
          >
          <p
            id="remote-second-factor-note"
            class="action-note auth-note"
          >
            未开启两步验证可留空；已开启时填写验证码，恢复码也填在这里。
          </p>

          <button
            data-feedback-action="登录"
            class="button primary large"
            type="submit"
            :disabled="auth.busy || password === ''"
          >
            {{ auth.busy ? "正在登录……" : "登录" }}
          </button>
        </form>

        <p
          v-if="errorText !== null"
          class="notice-warn auth-error"
          role="alert"
        >
          <strong>无法登录</strong>
          {{ errorText }}
          <span v-if="throttledSeconds !== null">请等待约 {{ throttledSeconds }} 秒后重试。</span>
        </p>

        <div class="action-note auth-reset">
          <strong>忘记口令</strong>
          <p>请联系服务器管理员重置。重置后，按管理员提供的步骤重新设置登录口令。</p>
        </div>
      </div>
    </section>
  </AppShell>
</template>

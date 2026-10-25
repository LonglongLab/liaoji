<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import { type AppLockMode, localUnlockMethod } from "@/lib/appLock";
import { isWebAuthnAvailable } from "@/lib/webauthn";
import { unlockLocation } from "@/lib/workspaceRouting";
import { useReauthStore } from "@/stores/reauth";
import { formatDateTimeSeparator } from "@/utils/time";

import RemoteAppLockDialog from "./RemoteAppLockDialog.vue";

/**
 * 设置页「本机应用锁」卡（PSU-2.221，原型 P19）。只对这台设备生效，默认关闭。
 *
 * 参照手机的应用锁：一行开关就是全部入口，拨一下弹对话框走完。开启后才展开
 * 「立即锁定」与本机的解锁方式（可重新录入）。其他设备在各自的设置里开关，这里不列。
 * 服务器没配置时开关置灰，副标题写原因，点 ? 给出配置入口——暂时未配置也要看得见。
 */
const api = useArchiveApi();
const reauth = useReauthStore();
const router = useRouter();

const browserReady = isWebAuthnAvailable();
const method = localUnlockMethod();
const loaded = ref(false);
const helpOpen = ref(false);
const dialog = ref<AppLockMode | null>(null);
const notice = ref<{ text: string; ok: boolean } | null>(null);
let noticeTimer: ReturnType<typeof globalThis.setTimeout> | undefined;

onMounted(async () => {
  await reauth.refresh(api);
  if (reauth.state?.supported === true && !reauth.state.locked) {
    await reauth.loadPasskeys(api);
  }
  loaded.value = true;
});
onBeforeUnmount(() => {
  globalThis.clearTimeout(noticeTimer);
});

const supported = computed(() => reauth.state?.supported === true);
const enabled = computed(() => reauth.state?.enabled === true);
/** 开启要服务器与浏览器都支持；已开着的总能关（系统验证不了时用登录口令）。 */
const usable = computed(() => loaded.value && (enabled.value || (supported.value && browserReady)));
const subtitle = computed(() => {
  if (!loaded.value) return "读取中…";
  if (enabled.value) return "已开启 · 本机已解锁";
  if (!supported.value) return "服务器未配置";
  if (!browserReady) return "这个浏览器用不了";
  return method.name === "Windows Hello"
    ? "用指纹、面容或 Windows Hello 快速解锁，仅对此设备生效"
    : "用指纹、面容或设备密码快速解锁，仅对此设备生效";
});
const migrationNotice = computed(() => loaded.value && reauth.state?.migration_notice === true);
const methodDetail = computed(() => {
  const lastUsedAt = reauth.passkeys[0]?.last_used_at ?? null;
  return lastUsedAt === null ? "刚刚录入" : `上次使用 ${formatDateTimeSeparator(lastUsedAt)}`;
});

const flash = (text: string, ok = true): void => {
  globalThis.clearTimeout(noticeTimer);
  notice.value = { text, ok };
  noticeTimer = globalThis.setTimeout(() => {
    notice.value = null;
  }, 2600);
};

const toggle = (): void => {
  if (!usable.value || reauth.busy) return;
  dialog.value = enabled.value ? "disable" : "enable";
};

const reenroll = (): void => {
  if (reauth.busy || !browserReady) return;
  dialog.value = "reenroll";
};

const finished = (text: string): void => {
  dialog.value = null;
  flash(text);
};

const lockNow = async (): Promise<void> => {
  const destination = unlockLocation(router.currentRoute.value.fullPath);
  if (await reauth.lock(api)) await router.replace(destination);
};
</script>

<template>
  <section
    class="settings-group remote-reauth-panel"
    data-testid="remote-reauth"
  >
    <h3 class="settings-group-title">
      应用锁
    </h3>
    <div class="settings-list app-lock-card">
      <div class="app-lock-row">
        <span
          class="app-lock-icon"
          :class="{ 'is-on': enabled }"
          aria-hidden="true"
        >
          <svg
            width="20"
            height="20"
            viewBox="0 0 24 24"
            fill="none"
            stroke="currentColor"
            stroke-width="1.8"
            stroke-linecap="round"
            stroke-linejoin="round"
          ><rect
            x="5"
            y="11"
            width="14"
            height="9"
            rx="2"
          /><path d="M8 11V8a4 4 0 0 1 8 0v3" /></svg>
        </span>
        <div class="app-lock-copy">
          <strong id="app-lock-title">本机应用锁</strong>
          <span
            class="app-lock-subtitle"
            :class="{ 'is-on': enabled }"
            data-testid="remote-reauth-status"
          >
            {{ subtitle }}
            <button
              v-if="loaded && !supported && !enabled"
              class="app-lock-help"
              type="button"
              aria-label="怎么配置"
              :aria-expanded="helpOpen"
              aria-controls="app-lock-help"
              data-testid="remote-reauth-unsupported"
              @click="helpOpen = !helpOpen"
            >?</button>
          </span>
        </div>
        <button
          class="app-lock-switch"
          type="button"
          role="switch"
          aria-labelledby="app-lock-title"
          :aria-checked="enabled"
          :disabled="!usable || reauth.busy"
          data-feedback-action="切换应用锁"
          data-testid="app-lock-switch"
          @click="toggle"
        >
          <span class="app-lock-knob" />
        </button>
      </div>

      <div
        v-if="helpOpen && !supported"
        id="app-lock-help"
        class="app-lock-help-panel"
      >
        <span>需要管理员在服务器上配置这两项：</span>
        <code>CHAT_ARCHIVE_WEBAUTHN_RP_ID</code>
        <code>CHAT_ARCHIVE_WEBAUTHN_ORIGINS</code>
      </div>

      <div
        v-if="migrationNotice"
        class="app-lock-migration"
        role="status"
        data-testid="remote-reauth-migration"
      >
        <span>应用锁改为按设备开启，旧的设置已关闭。需要的话，在这台设备上重新开启。</span>
        <button
          class="app-lock-reenroll"
          type="button"
          data-feedback-action="关闭应用锁提示"
          @click="reauth.dismissMigrationNotice(api)"
        >
          知道了
        </button>
      </div>

      <template v-if="enabled">
        <button
          class="app-lock-row app-lock-action"
          type="button"
          data-feedback-action="立即锁定"
          :disabled="reauth.busy"
          @click="lockNow"
        >
          <span
            class="app-lock-icon is-plain"
            aria-hidden="true"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            ><rect
              x="5"
              y="11"
              width="14"
              height="9"
              rx="2"
            /><path d="M8 11V8a4 4 0 0 1 8 0v3" /><path d="M12 15v2" /></svg>
          </span>
          <span class="app-lock-copy">立即锁定</span>
          <span
            class="app-lock-chevron"
            aria-hidden="true"
          >›</span>
        </button>

        <div
          class="app-lock-method"
          data-testid="remote-reauth-method"
        >
          <span
            class="app-lock-icon is-device"
            aria-hidden="true"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.8"
              stroke-linecap="round"
              stroke-linejoin="round"
            ><path d="M12 11v2.5a7 7 0 0 1-1.2 3.9" /><path d="M8.8 9.6A3.6 3.6 0 0 1 15.6 11v2.2" /><path d="M6.2 8a6.6 6.6 0 0 1 12 3.6v1.2" /><path d="M8.6 13v.8a9 9 0 0 1-1 4.2" /><path d="M15.4 15.8a12 12 0 0 1-1.3 3.6" /></svg>
          </span>
          <span class="app-lock-copy">
            <strong data-feedback-mask="device-info">本机 · {{ method.name }}</strong>
            <small>{{ methodDetail }}</small>
          </span>
          <button
            class="app-lock-reenroll"
            type="button"
            data-feedback-action="重新录入应用锁"
            :disabled="reauth.busy || !browserReady || !supported"
            @click="reenroll"
          >
            重新录入
          </button>
        </div>
        <p class="app-lock-method-hint">
          没通过时可用登录口令解锁
        </p>
      </template>

      <p
        v-if="notice !== null"
        class="app-lock-notice"
        :class="{ 'is-ok': notice.ok }"
        :role="notice.ok ? 'status' : 'alert'"
      >
        <span
          v-if="notice.ok"
          aria-hidden="true"
        >✓</span>
        {{ notice.text }}
      </p>
    </div>

    <RemoteAppLockDialog
      v-if="dialog !== null"
      :mode="dialog"
      @close="dialog = null"
      @finished="finished"
    />
  </section>
</template>

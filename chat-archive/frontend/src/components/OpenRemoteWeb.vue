<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";
import { useArchiveApi } from "@/api/injection";

defineProps<{ compact?: boolean }>();
const api = useArchiveApi();
const host = ref<HTMLElement | null>(null);
const opener = ref<HTMLButtonElement | null>(null);
const busy = ref(false);
const message = ref("");
const needsPairing = ref(false);
const messages = {
  not_configured: "尚未绑定远端，请先接入远程档案。",
  invalid_address: "已绑定的远端地址无效，请检查配对配置。",
  unsupported: "已绑定服务不提供网页版，本机同步仍可使用。",
  unavailable: "暂时无法确认远端网页版，请检查连接后重试。",
  browser_unavailable: "未能启动系统浏览器，请检查默认浏览器设置后重试。",
} as const;
const dismiss = (restoreFocus = false): void => {
  message.value = "";
  needsPairing.value = false;
  if (restoreFocus) opener.value?.focus();
};
const onOutside = (event: PointerEvent): void => {
  if (message.value && event.target instanceof Node && !host.value?.contains(event.target)) dismiss();
};
const onEscape = (event: KeyboardEvent): void => {
  if (event.key === "Escape" && message.value) dismiss(host.value?.contains(globalThis.document.activeElement) ?? false);
};
onMounted(() => {
  globalThis.document.addEventListener("pointerdown", onOutside, true);
  globalThis.document.addEventListener("keydown", onEscape);
});
onBeforeUnmount(() => {
  globalThis.document.removeEventListener("pointerdown", onOutside, true);
  globalThis.document.removeEventListener("keydown", onEscape);
});
const open = async (): Promise<void> => {
  if (busy.value) return;
  busy.value = true;
  needsPairing.value = false;
  message.value = "";
  try {
    const result = await api.openRemoteWeb();
    if (result.status !== "opened") message.value = messages[result.status];
    needsPairing.value = result.status === "not_configured";
  } catch {
    message.value = "打开失败，请确认本机服务可用后重试。";
  } finally {
    busy.value = false;
  }
};
</script>

<template>
  <span
    ref="host"
    class="open-remote-web"
    :class="{ compact }"
  >
    <button
      ref="opener"
      type="button"
      data-feedback-action="打开远端网页版"
      :disabled="busy"
      :aria-busy="busy"
      :aria-label="compact ? '在系统浏览器中打开远程档案' : undefined"
      :title="compact ? '用系统浏览器打开，登录由远端处理' : undefined"
      @click="open"
    >
      <span>{{ busy ? "正在打开…" : compact ? "远程档案" : "打开远端网页版 ↗" }}</span>
      <svg
        v-if="compact"
        class="remote-web-external"
        viewBox="0 0 16 16"
        fill="none"
        stroke="currentColor"
        stroke-width="1.4"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      ><path d="M9 3h4v4M13 3 6 10M6 4H3v9h9v-3" /></svg>
    </button>
    <slot />
    <span
      v-if="message"
      class="open-remote-message"
      role="status"
    >
      <span class="open-remote-message-text">
        {{ message }}
        <RouterLink
          v-if="needsPairing"
          to="/pairing"
        >接入远端 ›</RouterLink>
      </span>
      <button
        type="button"
        class="open-remote-dismiss"
        aria-label="关闭提示"
        @click="dismiss(true)"
      >✕</button>
    </span>
  </span>
</template>

<style scoped>
.open-remote-web { position: relative; }
.open-remote-web:not(.compact) { display: inline-flex; flex-direction: column; align-items: flex-start; }
button { min-height: 36px; font: inherit; color: inherit; background: none; border: 0; padding: 0; cursor: pointer; }
.compact > button { display: inline-flex; align-items: center; gap: 5px; min-height: 32px; padding: 5px 8px; border-radius: var(--radius-btn); font-size: 12px; line-height: 1.34; font-weight: 400; color: var(--ink-soft); white-space: nowrap; }
.compact > button:hover { background: var(--surface-sunken); color: var(--ink-strong); }
.compact > button:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.remote-web-external { width: 12px; height: 12px; flex: none; opacity: .65; }
button:disabled { cursor: wait; opacity: .65; }
.open-remote-message { display: flex; align-items: flex-start; gap: 8px; margin-top: 12px; width: min(320px, 80vw); padding: 10px 8px 10px 14px; background: var(--surface-pop); color: var(--ink-soft); border: 1px solid var(--rule); border-radius: var(--radius-card); box-shadow: var(--shadow-pop); font-size: 14px; line-height: 1.6; white-space: normal; overflow-wrap: anywhere; }
.open-remote-message-text { flex: 1; min-width: 0; }
.open-remote-dismiss { display: grid; place-items: center; flex: none; width: 32px; min-height: 32px; border-radius: var(--radius-btn); }
.open-remote-dismiss:hover { background: var(--surface-sunken); }
.open-remote-dismiss:focus-visible { outline: 2px solid var(--link); outline-offset: 2px; }
.compact .open-remote-message { position: absolute; z-index: 30; top: 100%; right: 0; }
@media (max-width: 600px), (pointer: coarse) {
  .open-remote-dismiss { width: 44px; min-height: 44px; }
}
</style>

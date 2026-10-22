<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { ArchiveApiError } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import PasswordInput from "@/components/PasswordInput.vue";
import { useCapabilityStore } from "@/stores/capabilities";

const props = withDefaults(defineProps<{ setup?: boolean; disabled?: boolean }>(), { setup: false, disabled: false });
const api = useArchiveApi();
const capabilities = useCapabilityStore();
const password = ref("");
const code = ref("");
const forced = ref(false);
const confirmedUntil = ref(0);
const confirmed = computed(() => confirmedUntil.value > 0);
const errorText = ref<string | null>(null);
const required = computed(() => !props.setup || forced.value);
const totp = computed(() => capabilities.states.totp === "available");
const ready = computed(() => !required.value || confirmed.value || (password.value.trim() !== "" && (!totp.value || /^\d{6}$/.test(code.value))));
const clear = (): void => { password.value = ""; code.value = ""; errorText.value = null; confirmedUntil.value = 0; };
const authorize = async (): Promise<boolean> => {
  if (!required.value) return true;
  if (confirmedUntil.value > Date.now() / 1000) return true;
  if (confirmed.value) { confirmedUntil.value = 0; errorText.value = "请再次确认身份后继续。"; return false; }
  if (!ready.value) return false;
  try {
    const confirmation = await api.confirmBrowserManagement(password.value, code.value);
    confirmedUntil.value = confirmation.expires_at;
    return true;
  } catch (error: unknown) {
    errorText.value = error instanceof ArchiveApiError ? error.message : "无法确认身份，请重试。";
    return false;
  } finally {
    password.value = "";
    code.value = "";
  }
};
const requireProof = (error: unknown): void => {
  if (error instanceof ArchiveApiError && error.code === "REMOTE_REAUTH_FRESH_REQUIRED") {
    forced.value = true;
    confirmedUntil.value = 0;
    errorText.value = "请再次确认身份后继续。";
  }
};
onBeforeUnmount(clear);
defineExpose({ ready, authorize, requireProof, clear });
</script>

<template>
  <fieldset
    v-if="required && !confirmed"
    class="server-management-proof"
    :disabled="disabled"
  >
    <legend>确认身份</legend>
    <label class="server-field"><span>登录口令</span><PasswordInput
      v-model="password"
      autocomplete="current-password"
    /></label>
    <label
      v-if="totp"
      class="server-field"
    ><span>两步验证码</span><input
      v-model="code"
      inputmode="numeric"
      autocomplete="one-time-code"
      maxlength="6"
      placeholder="6 位验证码"
    ></label>
  </fieldset>
  <p
    v-if="required && confirmed"
    class="action-note"
    role="status"
  >
    身份已确认
  </p>
  <p
    v-if="errorText"
    role="alert"
    class="notice-warn"
  >
    {{ errorText }}
  </p>
</template>

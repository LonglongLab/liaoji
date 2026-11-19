<script setup lang="ts">
import { X } from "@lucide/vue";
import { onBeforeMount, onBeforeUnmount, onMounted, onUnmounted, ref } from "vue";
import FocusTrap from "primevue/focustrap";
import { isolateModal } from "@/lib/modalIsolation";

// 危险操作的确认框：风险只在这里讲，列成两三条，不写段落。
// 不用 Teleport：内容留在调用它的组件里，单测照常能在 wrapper 里找到按钮。
const props = withDefaults(defineProps<{ title: string; busy?: boolean; dismissible?: boolean; sheet?: boolean }>(), { busy: false, dismissible: false, sheet: false });
const emit = defineEmits<{ close: [] }>();
const vFocustrap = FocusTrap;

const dialog = ref<HTMLElement | null>(null);
defineExpose({ focus: () => dialog.value?.focus({ preventScroll: true }) });
let returnFocus: HTMLElement | null = null;
let isolation: ReturnType<typeof isolateModal> | null = null;
const close = (): void => { if (!props.busy && isolation?.isTop()) emit("close"); };
const onFocusIn = (event: FocusEvent): void => {
  if (isolation?.isTop() && event.target instanceof Node && !dialog.value?.contains(globalThis.document.activeElement)) dialog.value?.focus();
};

const onKeyDown = (event: KeyboardEvent): void => {
  if (event.key !== "Escape" || event.isComposing || !isolation?.isTop()) return;
  event.preventDefault();
  event.stopImmediatePropagation();
  close();
};

onBeforeMount(() => {
  returnFocus = globalThis.document.activeElement instanceof HTMLElement ? globalThis.document.activeElement : null;
});
onMounted(() => {
  if (dialog.value !== null) isolation = isolateModal(dialog.value);
  globalThis.document.addEventListener("keydown", onKeyDown);
  globalThis.document.addEventListener("focusin", onFocusIn);
  // 焦点落在对话框本身而不是确认按钮上：回车不该直接把危险操作按下去。
  dialog.value?.focus();
});
onBeforeUnmount(() => {
  globalThis.document.removeEventListener("keydown", onKeyDown);
  globalThis.document.removeEventListener("focusin", onFocusIn);
  isolation?.release();
});
onUnmounted(() => {
  if (returnFocus?.isConnected === true) returnFocus.focus();
});
</script>

<template>
  <div
    v-focustrap="{ autoFocus: false }"
    class="settings-confirm-scrim"
    :class="{ 'settings-sheet-scrim': sheet }"
    @click.self="dismissible && close()"
  >
    <div
      ref="dialog"
      class="settings-confirm"
      :class="{ 'settings-sheet': sheet }"
      role="dialog"
      aria-modal="true"
      :aria-busy="busy"
      :aria-label="title"
      tabindex="-1"
    >
      <div :class="{ 'settings-sheet-heading': sheet }">
        <h3 data-feedback-mask="confirm-title">
          {{ title }}
        </h3>
        <button
          v-if="sheet"
          type="button"
          class="settings-sheet-close"
          aria-label="关闭面板"
          :disabled="busy"
          @click="close"
        >
          <X
            :size="18"
            aria-hidden="true"
          />
        </button>
      </div>
      <slot />
      <div
        v-if="$slots.actions"
        class="settings-confirm-actions"
      >
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

<style scoped>
.settings-sheet { border: 1px solid var(--rule); }
.settings-sheet-heading { display: flex; align-items: center; justify-content: space-between; gap: 8px; margin-bottom: 12px; }
.settings-sheet-heading h3 { margin: 0; font-size: 1.2rem; }
.settings-sheet-close { display: grid; place-items: center; flex: none; width: 44px; height: 44px; border: 0; border-radius: 8px; color: var(--ink-soft); background: transparent; }
.settings-sheet-close:hover { background: var(--surface-sunken); }
@media (max-width: 47.99rem) {
  .settings-sheet-scrim { padding: 0; place-items: end center; }
  .settings-sheet { width: 100%; max-height: calc(100dvh - 16px); border-radius: 16px 16px 0 0; padding: 16px 18px max(18px, env(safe-area-inset-bottom)); }
}
</style>

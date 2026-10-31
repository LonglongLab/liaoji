<script setup lang="ts">
import { onBeforeMount, onBeforeUnmount, onMounted, onUnmounted, ref } from "vue";
import FocusTrap from "primevue/focustrap";
import { isolateModal } from "@/lib/modalIsolation";

// 危险操作的确认框：风险只在这里讲，列成两三条，不写段落。
// 不用 Teleport：内容留在调用它的组件里，单测照常能在 wrapper 里找到按钮。
const props = withDefaults(defineProps<{ title: string; busy?: boolean; dismissible?: boolean }>(), { busy: false, dismissible: false });
const emit = defineEmits<{ close: [] }>();
const vFocustrap = FocusTrap;

const dialog = ref<HTMLElement | null>(null);
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
    @click.self="dismissible && close()"
  >
    <div
      ref="dialog"
      class="settings-confirm"
      role="dialog"
      aria-modal="true"
      :aria-busy="busy"
      :aria-label="title"
      tabindex="-1"
    >
      <h3 data-feedback-mask="confirm-title">
        {{ title }}
      </h3>
      <slot />
      <div class="settings-confirm-actions">
        <slot name="actions" />
      </div>
    </div>
  </div>
</template>

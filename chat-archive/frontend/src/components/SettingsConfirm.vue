<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref } from "vue";

// 危险操作的确认框：风险只在这里讲，列成两三条，不写段落。
// 不用 Teleport：内容留在调用它的组件里，单测照常能在 wrapper 里找到按钮。
defineProps<{ title: string }>();
const emit = defineEmits<{ close: [] }>();

const dialog = ref<HTMLElement | null>(null);
let returnFocus: HTMLElement | null = null;

const onKeyDown = (event: KeyboardEvent): void => {
  if (event.key === "Escape") emit("close");
};

onMounted(() => {
  returnFocus = globalThis.document.activeElement instanceof HTMLElement ? globalThis.document.activeElement : null;
  globalThis.document.addEventListener("keydown", onKeyDown);
  // 焦点落在对话框本身而不是确认按钮上：回车不该直接把危险操作按下去。
  dialog.value?.focus();
});
onBeforeUnmount(() => {
  globalThis.document.removeEventListener("keydown", onKeyDown);
  if (returnFocus?.isConnected === true) returnFocus.focus();
});
</script>

<template>
  <div
    class="settings-confirm-scrim"
    @click.self="emit('close')"
  >
    <div
      ref="dialog"
      class="settings-confirm"
      role="dialog"
      aria-modal="true"
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

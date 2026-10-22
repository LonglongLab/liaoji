<script setup lang="ts">
import { onBeforeUnmount, ref, toRef, watch } from "vue";

import EmoticonText from "@/components/EmoticonText.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import { formatWechatTimestamp } from "@/utils/time";

const props = defineProps<{
  open: boolean;
  title: string;
  senderName: string;
  createdAt: number;
  content: string;
}>();

const emit = defineEmits<{ close: [] }>();

const fontScale = ref(1);

const close = (): void => emit("close");
const { container, initial, onKeydown: onModalKeydown } = useModalFocus(
  toRef(props, "open"),
  close,
);
void container;
void initial;

const onWindowKeydown = (event: KeyboardEvent): void => {
  if (!props.open) return;
  if (event.key !== "Escape") return;
  event.preventDefault();
  close();
};

const onDialogKeydown = (event: KeyboardEvent): void => {
  if (event.key === "Tab" || event.key === "Escape") onModalKeydown(event);
};

watch(
  () => props.open,
  (open) => {
    if (open) fontScale.value = 1;
  },
  { immediate: true },
);

if (typeof globalThis !== "undefined") {
  globalThis.addEventListener("keydown", onWindowKeydown);
}

onBeforeUnmount(() => {
  if (typeof globalThis !== "undefined") {
    globalThis.removeEventListener("keydown", onWindowKeydown);
  }
});
</script>

<template>
  <Teleport to="body">
    <div
      v-if="open"
      class="text-viewer-backdrop"
      role="dialog"
      aria-modal="true"
      aria-label="文字消息阅读器"
      @click.self="close"
    >
      <section
        ref="container"
        class="text-viewer"
        @keydown.stop="onDialogKeydown"
      >
        <header class="text-viewer-head">
          <div class="text-viewer-title">
            <strong>{{ title }}</strong>
            <small data-feedback-mask="sender-name">{{ senderName }} · {{ formatWechatTimestamp(createdAt) }}</small>
          </div>
          <div class="text-viewer-actions">
            <button
              class="text-viewer-size"
              type="button"
              aria-label="缩小文字"
              :disabled="fontScale <= 0.9"
              @click="fontScale = Math.max(0.9, Number((fontScale - 0.1).toFixed(1)))"
            >
              A−
            </button>
            <span
              class="text-viewer-scale"
              aria-live="polite"
            >
              {{ Math.round(fontScale * 100) }}%
            </span>
            <button
              class="text-viewer-size"
              type="button"
              aria-label="放大文字"
              :disabled="fontScale >= 1.6"
              @click="fontScale = Math.min(1.6, Number((fontScale + 0.1).toFixed(1)))"
            >
              A+
            </button>
            <button
              ref="initial"
              class="text-viewer-close"
              type="button"
              aria-label="关闭文字阅读器"
              @click="close"
            >
              ×
            </button>
          </div>
        </header>

        <article
          data-feedback-mask="message-body"
          class="text-viewer-content"
          tabindex="0"
          :style="{ fontSize: `${String(fontScale)}em` }"
        >
          <EmoticonText :text="content" />
        </article>

        <footer class="text-viewer-foot">
          <span>支持调整字号</span>
          <span>按 Esc 关闭</span>
        </footer>
      </section>
    </div>
  </Teleport>
</template>

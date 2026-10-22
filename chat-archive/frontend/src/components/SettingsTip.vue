<script setup lang="ts">
import { onBeforeUnmount, ref, useId, watch } from "vue";

// 设置页的说明文字收进这里：行上只留一句话，想知道为什么再点「?」。
// 用 v-show 而不是 v-if：说明始终在 DOM 里，读屏和文本断言都拿得到。
withDefaults(defineProps<{ label?: string }>(), { label: "说明" });

const open = ref(false);
const root = ref<HTMLElement | null>(null);
const popId = useId();

const onPointerDown = (event: PointerEvent): void => {
  if (root.value !== null && !root.value.contains(event.target as Node)) open.value = false;
};
const onKeyDown = (event: KeyboardEvent): void => {
  if (event.key === "Escape") open.value = false;
};
const detach = (): void => {
  globalThis.document.removeEventListener("pointerdown", onPointerDown);
  globalThis.document.removeEventListener("keydown", onKeyDown);
};

watch(open, (isOpen) => {
  if (isOpen) {
    globalThis.document.addEventListener("pointerdown", onPointerDown);
    globalThis.document.addEventListener("keydown", onKeyDown);
  } else {
    detach();
  }
});
onBeforeUnmount(detach);
</script>

<template>
  <span
    ref="root"
    class="settings-tip"
  >
    <button
      class="settings-tip-button"
      type="button"
      :aria-label="label"
      :aria-expanded="open"
      :aria-controls="popId"
      @click.stop.prevent="open = !open"
    >?</button>
    <span
      v-show="open"
      :id="popId"
      class="settings-tip-pop"
      role="note"
    >
      <slot />
    </span>
  </span>
</template>

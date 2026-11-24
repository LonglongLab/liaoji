<script setup lang="ts">
import { Eye, EyeOff } from "@lucide/vue";
import { ref } from "vue";

// Keep labels, validation descriptions and keyboard events on the native control.
defineOptions({ inheritAttrs: false });
const model = defineModel<string>({ required: true });
const input = ref<HTMLInputElement | null>(null);
const visible = ref(false);
defineExpose({ focus: () => input.value?.focus() });
</script>

<template>
  <div class="password-input">
    <input
      v-bind="$attrs"
      ref="input"
      v-model="model"
      class="password-input-control"
      :type="visible ? 'text' : 'password'"
    >
    <button
      class="password-input-toggle"
      type="button"
      :aria-label="visible ? '隐藏口令' : '显示口令'"
      :aria-pressed="visible"
      @click="visible = !visible"
    >
      <component
        :is="visible ? EyeOff : Eye"
        :size="18"
        :stroke-width="1.8"
        aria-hidden="true"
      />
    </button>
  </div>
</template>

<style scoped>
.password-input {
  position: relative;
}

.password-input-control {
  display: block;
  width: 100%;
  min-height: 44px;
  padding: 11px 48px 11px 12px;
  appearance: none;
  border: 1px solid var(--rule);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
  color: var(--ink);
  font: inherit;
  font-size: 14px;
  line-height: 20px;
}

.password-input-control:focus-visible {
  border-color: var(--accent-solid);
  outline: 2px solid var(--accent-solid);
  outline-offset: 2px;
}

.password-input-control[aria-invalid="true"] {
  border-color: var(--danger);
}

.password-input-control[aria-invalid="true"]:focus-visible {
  outline-color: var(--danger);
}

.password-input-toggle {
  position: absolute;
  inset: 0 0 0 auto;
  display: flex;
  width: 44px;
  align-items: center;
  justify-content: center;
  padding: 0;
  border: 0;
  border-radius: var(--radius-btn);
  background: transparent;
  color: var(--ink-faint);
  cursor: pointer;
}

.password-input-toggle:hover {
  color: var(--ink);
}
</style>

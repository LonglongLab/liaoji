<script setup lang="ts">
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
      <svg
        width="18"
        height="18"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7S2 12 2 12z" />
        <circle
          cx="12"
          cy="12"
          r="3"
        />
        <path
          v-if="visible"
          d="m3 3 18 18"
        />
      </svg>
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

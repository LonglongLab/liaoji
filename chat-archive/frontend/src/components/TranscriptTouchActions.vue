<script setup lang="ts">
defineProps<{ selectable: boolean; self: boolean }>();
defineEmits<{ copy: []; select: []; close: [] }>();
</script>

<template>
  <div
    class="touch-row-actions"
    :class="{ 'is-self': self }"
    role="menu"
    aria-label="消息操作"
  >
    <button
      type="button"
      role="menuitem"
      data-row-action="copy"
      @click="$emit('copy')"
    >
      复制整条
    </button>
    <button
      type="button"
      role="menuitem"
      data-row-action="select"
      :disabled="!selectable"
      @click="$emit('select')"
    >
      多选
    </button>
    <button
      type="button"
      class="touch-row-actions-close"
      aria-label="关闭消息操作"
      @click="$emit('close')"
    >
      ×
    </button>
  </div>
</template>

<style scoped>
.touch-row-actions {
  display: flex;
  flex-wrap: wrap;
  gap: 8px;
  width: fit-content;
  max-width: calc(100% - 52px);
  margin: 8px 0 0 52px;
  padding: 4px 0;
  user-select: none;
}
.touch-row-actions.is-self { margin: 8px 52px 0 auto; }
.touch-row-actions button {
  min-height: 44px;
  min-width: 44px;
  padding: 7px 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
  color: var(--ink);
  font: inherit;
  font-size: 13.5px;
  cursor: pointer;
}
.touch-row-actions button:disabled { opacity: 0.45; cursor: not-allowed; }
.touch-row-actions .touch-row-actions-close { padding: 7px; }
</style>

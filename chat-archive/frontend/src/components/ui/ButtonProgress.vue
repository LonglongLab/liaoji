<script setup lang="ts">
defineProps<{ busy: boolean }>();
</script>

<template>
  <span
    class="button-progress"
    :class="{ 'is-busy': busy }"
  >
    <span class="button-progress-label"><slot /></span>
    <span
      v-if="busy"
      class="button-progress-spinner"
      aria-hidden="true"
    />
  </span>
</template>

<style scoped>
.button-progress { position: relative; display: inline-grid; place-items: center; max-width: 100%; vertical-align: middle; }
.button-progress-label { display: inline-flex; align-items: center; justify-content: center; gap: 8px; min-width: 0; }
/* Keep the original label in flow and in the accessibility tree. */
.is-busy > .button-progress-label { opacity: 0; }
.button-progress-spinner { position: absolute; width: 18px; height: 18px; box-sizing: border-box; border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%; animation: button-progress-spin .8s linear infinite; }
@keyframes button-progress-spin { to { transform: rotate(360deg); } }
@media (prefers-reduced-motion: reduce) { .button-progress-spinner { animation: none; } }
</style>

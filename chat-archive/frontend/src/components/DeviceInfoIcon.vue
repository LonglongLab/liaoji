<script setup lang="ts">
defineProps<{ kind: "mobile" | "tablet" | "desktop" | "unknown"; sync?: boolean }>();
</script>

<template>
  <span
    class="device-info-icon"
    :class="[`is-${kind}`, { 'is-sync': sync }]"
    aria-hidden="true"
  >
    <svg
      v-if="kind === 'mobile' || kind === 'tablet'"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
    >
      <rect
        :x="kind === 'tablet' ? 4 : 6"
        y="2"
        :width="kind === 'tablet' ? 16 : 12"
        height="20"
        rx="2"
      />
      <path d="M10 18h4" />
    </svg>
    <svg
      v-else-if="kind === 'desktop'"
      viewBox="0 0 24 24"
      fill="none"
      stroke="currentColor"
      stroke-width="1.6"
    >
      <rect
        x="2"
        y="3"
        width="20"
        height="14"
        rx="2"
      /><path d="M12 17v4m-5 0h10" />
    </svg>
    <span v-else>?</span>
  </span>
</template>

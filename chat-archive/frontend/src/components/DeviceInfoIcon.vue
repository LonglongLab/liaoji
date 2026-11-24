<script setup lang="ts">
import { Monitor, Smartphone, Tablet } from "@lucide/vue";

defineProps<{ kind: "mobile" | "tablet" | "desktop" | "unknown"; sync?: boolean }>();
</script>

<template>
  <span
    class="device-info-icon"
    :class="[`is-${kind}`, { 'is-sync': sync }]"
    aria-hidden="true"
  >
    <Smartphone
      v-if="kind === 'mobile'"
      :stroke-width="1.6"
    />
    <Tablet
      v-else-if="kind === 'tablet'"
      :stroke-width="1.6"
    />
    <Monitor
      v-else-if="kind === 'desktop'"
      :stroke-width="1.6"
    />
    <span v-else>?</span>
  </span>
</template>

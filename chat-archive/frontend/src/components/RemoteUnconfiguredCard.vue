<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
import { computed } from "vue";
import { unconfiguredRemoteAction } from "@/lib/capabilities";
import { useCapabilityStore } from "@/stores/capabilities";
defineProps<{ heading: string; summary: string }>();
const capabilities = useCapabilityStore();
const action = computed(() => unconfiguredRemoteAction(capabilities.deployment));
</script>

<template>
  <div class="remote-unconfigured-card">
    <h1>{{ heading }}</h1>
    <AppAction
      class="button primary"
      :to="action.to"
      variant="primary"
    >
      {{ action.label }}
    </AppAction>
    <slot />
  </div>
</template>

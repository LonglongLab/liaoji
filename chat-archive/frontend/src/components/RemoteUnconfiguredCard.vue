<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
import { computed } from "vue";
import { useCapabilityStore } from "@/stores/capabilities";
defineProps<{ heading: string; summary: string }>();
const capabilities = useCapabilityStore();
const remote = computed(() => capabilities.deployment === "remote");
</script>

<template>
  <div class="remote-unconfigured-card">
    <h1>{{ heading }}</h1>
    <AppAction
      class="button primary"
      :to="remote ? '/setup' : '/pairing'"
      variant="primary"
    >
      {{ remote ? '完成首次设置' : '连接远程档案' }}
    </AppAction>
    <slot />
  </div>
</template>

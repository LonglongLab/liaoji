<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import { useCapabilityStore } from "@/stores/capabilities";
defineProps<{ heading: string; summary: string }>();
const capabilities = useCapabilityStore();
const remote = computed(() => capabilities.deployment === "remote");
</script>

<template>
  <div class="remote-unconfigured-card">
    <h1>{{ heading }}</h1>
    <RouterLink
      class="button primary"
      :to="remote ? '/setup' : '/pairing'"
    >
      {{ remote ? '完成首次设置' : '连接远程档案' }}
    </RouterLink>
    <slot />
  </div>
</template>

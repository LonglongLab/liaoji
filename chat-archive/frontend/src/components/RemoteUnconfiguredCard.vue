<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";

import { useCapabilityStore } from "@/stores/capabilities";

defineProps<{ heading: string; summary: string }>();

const capabilities = useCapabilityStore();
/** 端类型由后端下发；未答复时按本机应用处理（那是绝大多数情况）。 */
const isRemoteEnd = computed(() => capabilities.deployment === "remote");
</script>

<template>
  <div class="remote-unconfigured-card">
    <h1>{{ heading }}</h1>
    <p>{{ summary }}</p>

    <p
      v-if="isRemoteEnd"
      class="action-note"
    >
      这台服务器还没有建立归档。在服务器本机运行
      <code>python -m chat_archive.server.cli provision</code> 建一个，
      再用它给出的设备令牌把采集端接上来。
    </p>
    <p
      v-else
      class="action-note"
    >
      在已经接入的那台电脑上生成一个配对码，然后到
      <RouterLink to="/pairing">
        接入远程档案
      </RouterLink>
      粘进去。整个归档的第一台机器仍走命令行
      <code>chat-archive-remote adopt</code>——那一步要先在服务器上
      <code>provision</code>，一个归档只发生一次。
    </p>

    <slot />
  </div>
</template>

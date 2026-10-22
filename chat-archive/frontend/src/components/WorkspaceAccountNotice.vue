<script setup lang="ts">
defineProps<{ hint: string; guide: string | null; canRetry: boolean }>();
const emit = defineEmits<{ retry: [] }>();
</script>

<template>
  <div
    class="notice-info"
    role="status"
  >
    <p>{{ hint }}</p>
    <RouterLink
      v-if="guide !== null"
      :to="guide"
      class="text-action"
    >
      配置账号与数据
    </RouterLink>
    <button
      v-if="canRetry"
      class="text-action"
      type="button"
      @click="emit('retry')"
    >
      重新加载
    </button>
  </div>
</template>

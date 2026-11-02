<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
defineProps<{ hint: string; guide: string | null; canRetry: boolean }>();
const emit = defineEmits<{ retry: [] }>();
</script>

<template>
  <div
    class="notice-info"
    role="status"
  >
    <p>{{ hint }}</p>
    <AppAction
      v-if="guide !== null"
      :to="guide"
      class="text-action"
    >
      配置账号与数据
    </AppAction>
    <AppAction
      v-if="canRetry"
      class="text-action"
      type="button"
      @click="emit('retry')"
    >
      重新加载
    </AppAction>
  </div>
</template>

<script setup lang="ts">
import SettingsConfirm from "@/components/SettingsConfirm.vue";
defineProps<{ open: boolean; busy: boolean; blocked: boolean }>();
const emit = defineEmits<{ keep: []; discard: [] }>();
</script>

<template>
  <p
    v-if="blocked"
    role="status"
    class="action-note"
  >
    正在验证或保存，请完成后再离开。
  </p>
  <SettingsConfirm
    v-if="open"
    title="更改尚未保存"
    :busy="busy"
    @close="emit('keep')"
  >
    <p>离开会放弃这次修改。继续编辑会保留当前内容。</p>
    <template #actions>
      <button
        type="button"
        class="button secondary"
        :disabled="busy"
        @click="emit('keep')"
      >
        继续编辑
      </button>
      <button
        type="button"
        class="button danger"
        :disabled="busy"
        @click="emit('discard')"
      >
        放弃修改
      </button>
    </template>
  </SettingsConfirm>
</template>

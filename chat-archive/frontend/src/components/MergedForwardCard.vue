<script setup lang="ts">
import { ChevronRight } from "@lucide/vue";
import type { MessageSummary } from "@/api/archiveApi";
import EmoticonText from "@/components/EmoticonText.vue";
import {
  mergedForwardItemSummary,
  mergedForwardTitle,
} from "@/lib/mergedForward";

type AppCard = NonNullable<MessageSummary["app_card"]>;

const props = defineProps<{ card: AppCard }>();
const emit = defineEmits<{ open: [] }>();
</script>

<template>
  <button
    class="merged-forward-card"
    type="button"
    aria-label="打开合并转发聊天记录"
    @click="emit('open')"
  >
    <strong
      data-feedback-mask="message-body"
      class="merged-forward-title"
    >{{ mergedForwardTitle(props.card) }}</strong>
    <ol
      data-feedback-mask="message-body"
      class="merged-forward-preview"
    >
      <li
        v-for="(item, index) in props.card.items.slice(0, 3)"
        :key="`${String(index)}:${item.kind}`"
      >
        <EmoticonText :text="mergedForwardItemSummary(item)" />
      </li>
    </ol>
    <small
      v-if="props.card.truncated"
      class="merged-forward-truncated"
    >
      部分内容未完整保存
    </small>
    <span class="merged-forward-footer">
      <span>聊天记录</span>
      <ChevronRight
        :size="16"
        :stroke-width="1.8"
        aria-hidden="true"
      />
    </span>
  </button>
</template>

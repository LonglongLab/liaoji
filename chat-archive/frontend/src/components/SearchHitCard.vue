<script setup lang="ts">
import { computed } from "vue";

import type { SearchHit } from "@/api/archiveApi";
import AvatarBadge from "@/components/AvatarBadge.vue";
import EmoticonText from "@/components/EmoticonText.vue";
import SearchHitMediaPreview from "@/components/SearchHitMediaPreview.vue";
import { hitSenderName, hitSummary, hitTypeLabel } from "@/lib/conversationSearchTabs";
import { highlightParts, type HighlightPart } from "@/lib/highlight";
import { isSearchHitPreviewable } from "@/lib/searchHitMedia";
import { formatRelativeTime, formatSearchHitTime } from "@/utils/time";

const props = defineProps<{
  hit: SearchHit;
  conversationName?: string;
  compact?: boolean;
  query?: string;
  loadAttachment: ((attachmentId: string) => Promise<Blob>) | undefined;
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
}>();

const emit = defineEmits<{
  open: [hit: SearchHit];
  "open-media": [hit: SearchHit];
}>();

const summaryText = computed(() => hitSummary(props.hit));
// 会话内查找按微信的排布：两行，图片和视频只留缩略图，不写「[图片]」和文件名。
const showsThumbnailOnly = computed(
  () => props.compact && isSearchHitPreviewable(props.hit),
);
const timeText = computed(() =>
  props.compact
    ? formatSearchHitTime(props.hit.created_at)
    : formatRelativeTime(props.hit.created_at),
);
const highlightedSummary = computed<HighlightPart[]>(() =>
  highlightParts(summaryText.value, props.query),
);
</script>

<template>
  <button
    class="search-hit-card"
    type="button"
    :aria-label="`定位到聊天位置：${summaryText}`"
    :class="{ 'is-compact': compact, 'has-media': isSearchHitPreviewable(props.hit) }"
    @click="emit('open', hit)"
  >
    <div class="search-hit-card-main">
      <AvatarBadge
        :name="hitSenderName(hit)"
        :src="hit.sender_avatar_ref"
        :alt="`${hitSenderName(hit)}的头像`"
        size="sm"
      />
      <div class="search-hit-copy">
        <div class="search-hit-head panel-hit-head">
          <strong data-feedback-mask="conversation-name">{{ compact ? hitSenderName(hit) : (conversationName ?? hitSenderName(hit)) }}</strong>
          <time>{{ timeText }}</time>
        </div>
        <div
          v-if="!compact"
          class="search-hit-meta"
        >
          <span data-feedback-mask="sender-name">{{ hitSenderName(hit) }}</span>
          <span>· {{ hitTypeLabel(hit) }}</span>
          <span v-if="hit.has_attachment">· 含附件</span>
        </div>
        <span
          v-if="!showsThumbnailOnly"
          data-feedback-mask="message-preview"
          class="search-hit-snippet panel-hit-body"
        ><EmoticonText :parts="highlightedSummary" /></span>
      </div>
    </div>
    <!-- 只给真的能预览的附件留位置：文件消息没有缩略图，不该占一块「媒体暂不可用」。 -->
    <SearchHitMediaPreview
      v-if="isSearchHitPreviewable(hit)"
      :hit="hit"
      :load-attachment="loadAttachment"
      :load-thumbnail="loadThumbnail"
      @open-media="emit('open-media', hit)"
    />
    <span
      class="search-hit-locate"
    >
      定位到聊天位置
    </span>
  </button>
</template>

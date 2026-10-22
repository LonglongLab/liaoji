<script setup lang="ts">
import { computed } from "vue";

import type { SearchHit } from "@/api/archiveApi";
import FileIcon from "@/components/FileIcon.vue";
import SearchHitMediaPreview from "@/components/SearchHitMediaPreview.vue";
import { attachmentStatusWord } from "@/lib/attachmentStatus";
import { hitSenderName, hitSummary } from "@/lib/conversationSearchTabs";
import { formatFileSize } from "@/lib/fileKinds";
import { highlightParts } from "@/lib/highlight";
import { isSearchHitPreviewable } from "@/lib/searchHitMedia";
import { messageTypeIconPath } from "@/lib/searchScopes";
import { formatSearchHitTime } from "@/utils/time";

/**
 * 搜索结果的一行——微信「文件管理」那一行：
 * 左边一个图标或缩略图，中间标题（关键词高亮）＋「发送人 ｜ 会话」，
 * 右边时间和大小。
 *
 * 会话内查找的文件页签与全局搜索页共用这一个组件（PSU-2.66）。两边只差
 * `conversationName`：在一条会话里查找时不必每行都重复那条会话的名字。
 */
const props = defineProps<{
  hit: SearchHit;
  query?: string;
  /** 给了就在副行印「发送人 ｜ 会话」；不给（会话内查找）只印发送人。 */
  conversationName?: string | undefined;
  loadAttachment?: ((attachmentId: string) => Promise<Blob>) | undefined;
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
}>();

const emit = defineEmits<{
  open: [hit: SearchHit];
  "open-media": [hit: SearchHit];
}>();

/**
 * 标题：文件名优先用附件自己的名字；老归档只有摘要时剥掉「[文件] 」；
 * 不是文件的消息用与别处同一份 `hitSummary`，不另立一套摘要口径。
 */
const title = computed(() => {
  const attached = props.hit.attachment_file_name ?? "";
  if (attached !== "") return attached;
  if (props.hit.message_type === "file") return props.hit.snippet.replace(/^\[文件\]\s*/, "");
  return hitSummary(props.hit);
});

const titleParts = computed(() => highlightParts(title.value, props.query));

const showsThumbnail = computed(() => isSearchHitPreviewable(props.hit));

/**
 * 纸张图标只给**真的是文件**的那些行。一条纯文本消息旁边印一张文件纸，
 * 读起来像它带了个附件——其余类型用与范围栏同一批线性图标。
 */
const showsFileIcon = computed(
  () => props.hit.message_type === "file" || props.hit.attachment_file_name !== null,
);
const typeIconPath = computed(() => messageTypeIconPath(props.hit.message_type));

/**
 * 副行。单聊里会话名就是对方的名字，微信照印两遍——两遍里没有第二份信息，
 * 而归档里一半的命中都是单聊，所以相同就只印一遍。
 */
const subLine = computed(() => {
  const sender = hitSenderName(props.hit);
  const conversation = props.conversationName;
  if (conversation === undefined || conversation === "" || conversation === sender) {
    return sender;
  }
  return `${sender} ｜ ${conversation}`;
});

/** 归档里没有记下大小时就不显示，不用「未知」占位。 */
const sizeText = computed(() => formatFileSize(props.hit.attachment_size));

/** 字节还没到本机时右上角那个词；正常态与说不清的状态都是 null。 */
const statusWord = computed(() =>
  props.hit.attachment_id == null || props.hit.attachment_content_available === true
    ? null
    : attachmentStatusWord(props.hit.attachment_cache_status),
);
</script>

<template>
  <button
    class="search-hit-row"
    type="button"
    :aria-label="`定位到聊天位置：${title}`"
    @click="emit('open', hit)"
  >
    <!-- 图片与视频给画面，其余给纸张造型的类型图标——和气泡里的文件卡同一个组件。 -->
    <SearchHitMediaPreview
      v-if="showsThumbnail"
      class="search-hit-row-thumb"
      :hit="hit"
      :load-attachment="loadAttachment"
      :load-thumbnail="loadThumbnail"
      @open-media="emit('open-media', hit)"
    />
    <FileIcon
      v-else-if="showsFileIcon"
      :file-name="hit.attachment_file_name"
      size="lg"
    />
    <span
      v-else-if="typeIconPath !== null"
      class="search-hit-row-type"
      aria-hidden="true"
    >
      <svg
        width="20"
        height="20"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.7"
        stroke-linecap="round"
        stroke-linejoin="round"
      >
        <path :d="typeIconPath" />
      </svg>
    </span>
    <span class="search-hit-row-main">
      <span
        data-feedback-mask="message-preview"
        class="search-hit-row-title"
      ><template
        v-for="(part, index) in titleParts"
        :key="`${String(index)}:${part.text}`"
      ><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
      <small
        data-feedback-mask="conversation-name"
        class="search-hit-row-sub"
      >{{ subLine }}</small>
    </span>
    <span class="search-hit-row-side">
      <span class="search-hit-row-when">
        <em
          v-if="statusWord !== null"
          class="search-hit-row-pending"
        >{{ statusWord }}</em>
        <time>{{ formatSearchHitTime(hit.created_at) }}</time>
      </span>
      <small v-if="sizeText !== null">{{ sizeText }}</small>
    </span>
  </button>
</template>

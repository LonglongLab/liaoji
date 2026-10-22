<script setup lang="ts">
import { computed } from "vue";

import ExportRecordBlock from "@/components/ExportRecordBlock.vue";
import MessageTypeRenderer from "@/components/MessageTypeRenderer.vue";
import {
  buildTranscriptRows,
  replyPreviewFor,
  senderNameLookup,
  type RenderMessage,
} from "@/lib/transcript";

/**
 * 选中的那几条消息，完整渲染成一份可以存盘的文档（PSU-2.168）。
 *
 * **不虚拟化**，这是它存在的全部理由。转录用 `@tanstack/vue-virtual`，DOM 里只有
 * 可视那十几行——选中的 80 条里有 60 条根本不在页面上，截屏截不到它们。
 *
 * 渲染走的是 App 自己那套 `MessageTypeRenderer`：这是「导出来的样式和页面一致」
 * 唯一靠得住的做法。另写一份模板——不管在前端还是后端——两份迟早分叉，而分叉时
 * 没有任何东西会变红。
 *
 * 挂在屏外但**必须挂在文档里**：不在文档里读到的计算样式全是默认值，
 * 收出来会是一张白纸。
 */
const props = withDefaults(
  defineProps<{
    messages: RenderMessage[];
    showSenderNames: boolean;
    /**
     * 字节**挂之前就解好了**，这里收的是一个从已解好的 Map 里取的函数。
     *
     * 组件内部是异步取图的，等「渲染完了」没有确定信号；先解好再挂，等的就只剩
     * 浏览器解码那一下，`waitForImages` 才等得出头。
     */
    loadAttachment: (attachmentId: string) => Promise<Blob>;
    loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
    /** 嵌套的聊天记录卡片是否摊开：HTML 收着，PDF / 图片摊开。 */
    expandRecords?: boolean;
  }>(),
  { loadThumbnail: undefined, expandRecords: false },
);

const rows = computed(() => buildTranscriptRows(props.messages));
const messagesById = computed(
  () => new Map(props.messages.map((message) => [message.id, message])),
);
const memberName = computed(() => senderNameLookup(props.messages));

/**
 * 引用的原消息**没被选中**时也照样画出引用条。
 *
 * `replyPreviewFor` 只从传进来的这批里找，找不到就返回 null——那时引用条不出现。
 * 这是既有行为，不在这里改：编一条没有依据的摘要比少一条引用糟得多。
 */
const replyPreview = (replyToId: string | null): ReturnType<typeof replyPreviewFor> =>
  replyPreviewFor(replyToId, messagesById.value);

const recordCard = (message: RenderMessage): NonNullable<RenderMessage["app_card"]> | null =>
  message.app_card?.kind === "merged_forward" ? message.app_card : null;
</script>

<template>
  <div class="export-doc-body">
    <div
      v-for="row in rows"
      :key="row.message.id"
      class="export-doc-row"
      :data-message-id="row.message.id"
    >
      <div
        v-if="row.dateLabel !== null"
        class="date-chip"
      >
        {{ row.dateLabel }}
      </div>
      <MessageTypeRenderer
        :message="row.message"
        :reply-preview="replyPreview(row.message.reply_to_id)"
        :highlighted="false"
        :show-sender-name="showSenderNames"
        :load-attachment="loadAttachment"
        :load-thumbnail="loadThumbnail"
        :resolve-attachment="undefined"
        :sticker-fetch-available="false"
        :member-name="memberName"
        eager-media
      />
      <!-- App 里那张卡照常显示（样式一致），内容另外摊在它下面：
           文件里点不开，折起来等于丢内容。 -->
      <ExportRecordBlock
        v-if="recordCard(row.message) !== null"
        :card="recordCard(row.message)!"
        :expanded="expandRecords"
      />
    </div>
  </div>
</template>

<style scoped>
.export-doc-body {
  width: 100%;
  background: var(--paper);
  color: var(--ink);
  font-family: var(--font-body);
  font-size: 14px;
  line-height: 1.5;
}

.export-doc-row {
  /* 切图时的断点只落在行与行之间（PSU-2.169 用得上），
     所以一行整体不允许被分页切开。 */
  break-inside: avoid;
  margin-bottom: 10px;
}
</style>

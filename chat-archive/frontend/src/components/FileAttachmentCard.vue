<script setup lang="ts">
import { computed } from "vue";

import type { AttachmentSummary } from "@/api/archiveApi";
import FileIcon from "@/components/FileIcon.vue";
import { fileVisualOf, formatFileSize } from "@/lib/fileKinds";
import { previewPlanOf } from "@/lib/filePreview/plan";

/**
 * 文件附件卡片。可预览与不可预览是两种不同的卡：
 * 能预览就给「预览 / 下载」两个明确动作；不能预览的**不渲染预览按钮**，
 * 只留下载，并在下面说清楚为什么——一个点不动的灰按钮比没有按钮更糟。
 */
const props = defineProps<{
  attachment: AttachmentSummary;
  /** 附件不可用时的状态词，由调用方按契约的 cache_status 翻译。 */
  statusLabel: string;
  failureReason: string | null;
  canRetry: boolean;
  busy: boolean;
}>();

const emit = defineEmits<{
  preview: [];
  download: [];
  retry: [];
}>();

const visual = computed(() => fileVisualOf(props.attachment.file_name));
const plan = computed(() => previewPlanOf(visual.value));
const available = computed(() => props.attachment.content_available);
const canPreview = computed(() => available.value && plan.value.mode !== "none");

const fileName = computed(() => props.attachment.file_name ?? "未命名文件");
const metaText = computed(() =>
  [formatFileSize(props.attachment.size), visual.value.label].filter((part) => part !== null).join(" · "),
);
</script>

<template>
  <div
    class="file-card"
    :class="{ 'is-degraded': !available }"
  >
    <div class="file-card-head">
      <FileIcon :file-name="attachment.file_name" />
      <div class="file-card-main">
        <strong
          data-feedback-mask="file-name"
          class="file-card-name"
        >{{ fileName }}</strong>
        <small class="file-card-meta">{{ metaText }}</small>
      </div>
    </div>

    <div
      v-if="available"
      class="file-card-actions"
    >
      <button
        v-if="canPreview"
        data-feedback-mask="file-name"
        class="text-action"
        type="button"
        :disabled="busy"
        :aria-label="`预览 ${fileName}`"
        @click="emit('preview')"
      >
        {{ busy ? "正在打开" : "预览" }}
      </button>
      <span
        v-if="canPreview"
        class="file-card-divider"
        aria-hidden="true"
      />
      <button
        data-feedback-mask="file-name"
        class="text-action"
        type="button"
        :aria-label="`下载 ${fileName}`"
        @click="emit('download')"
      >
        下载
      </button>
    </div>

    <!-- 本机渲染不了：说清是格式的问题还是解析器的问题，别让用户猜。 -->
    <p
      v-if="available && !canPreview"
      class="file-card-note"
    >
      {{ plan.reason }}
    </p>

    <div
      v-if="!available"
      class="file-card-degraded"
    >
      <span
        class="attachment-status"
        :class="`is-${attachment.cache_status}`"
      >{{ statusLabel }}</span>
      <small v-if="failureReason !== null">{{ failureReason }}</small>
      <button
        v-if="canRetry"
        class="text-action"
        type="button"
        @click="emit('retry')"
      >
        重试
      </button>
    </div>
  </div>
</template>

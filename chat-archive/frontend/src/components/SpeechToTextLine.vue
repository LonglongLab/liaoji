<script setup lang="ts">
/**
 * 语音消息气泡里那一行机器转写的字。
 *
 * 名字刻意不叫 `VoiceTranscript`：这个仓库里 `MessageTranscript` 指的是
 * **会话记录视图**，两个 transcript 挨在一起谁都会看错。
 *
 * 样式是「A 常驻一行」——用户 2026-09-21 在原型上三选一选的，理由是整段语音
 * 能像文字一样扫读。代价（页面更长、群聊更挤）也是那时一并认下的。
 *
 * **永远带来源标注。** 机器写的东西不能和本人说的话在界面上不可区分，
 * 这是「诚实展示」在这一处的具体形态，不是装饰。
 */
import { computed } from "vue";

import type { components } from "@/api/schema";

type VoiceTranscript = components["schemas"]["VoiceTranscript"];

const props = defineProps<{
  transcript: VoiceTranscript;
}>();

/** 转不出来的那两种也要说话，不能安静地什么都不显示。 */
const emptyLabel = computed(() =>
  props.transcript.status === "empty"
    ? "这段录音没听出内容"
    : "这段录音没能转成文字",
);
</script>

<template>
  <div
    v-if="transcript.status === 'ok'"
    class="stt"
    data-testid="speech-to-text"
  >
    <p
      data-feedback-mask="message-body"
      class="stt-text"
    >
      <span class="stt-tag">机器转写</span>
      <span
        v-if="transcript.low_confidence"
        class="stt-tag stt-tag-low"
        data-testid="speech-to-text-low-confidence"
      >可能有误</span>
      {{ transcript.text }}
    </p>
  </div>
  <p
    v-else
    class="stt stt-absent"
    data-testid="speech-to-text-absent"
  >
    <span class="stt-tag">机器转写</span>{{ emptyLabel }}
  </p>
</template>

<style scoped>
.stt {
  margin: 6px 0 0;
  padding-top: 6px;
  border-top: 1px solid var(--line, #eceef1);
  font-size: 13px;
  line-height: 1.55;
  color: var(--ink, #1f2328);
  word-break: break-word;
}

.stt-text {
  margin: 0;
}

.stt-absent {
  color: var(--dim, #6b7280);
}

.stt-tag {
  display: inline-block;
  margin-right: 6px;
  padding: 0 4px;
  border: 1px solid #f0e2bf;
  border-radius: 4px;
  background: #fdf6e3;
  color: #8a6d3b;
  font-size: 11px;
  line-height: 16px;
  vertical-align: 1px;
}

.stt-tag-low {
  border-color: #f6ccd4;
  background: #fdecef;
  color: #a5364a;
}
</style>

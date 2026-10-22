<script setup lang="ts">
import { toRef } from "vue";
import type { ExportReport } from "@/api/archiveApi";
import { useModalFocus } from "@/composables/useModalFocus";

const props = defineProps<{ open: boolean; title: string; report: ExportReport | null; loading: boolean; errorText: string | null }>();
const emit = defineEmits<{ close: []; retry: [] }>();
const { container, initial, onKeydown } = useModalFocus(toRef(props,"open"), () => emit("close"));
void container; void initial;
</script>

<template>
  <div
    v-if="open"
    class="export-sheet-backdrop"
    @click.self="emit('close')"
  >
    <section
      ref="container"
      class="export-sheet export-report-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="导出报告"
      :aria-busy="loading"
      @keydown="onKeydown"
    >
      <header class="export-sheet-head">
        <h2>导出报告</h2><button
          ref="initial"
          type="button"
          class="export-icon-button"
          aria-label="关闭导出报告"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
      <div class="export-sheet-body">
        <h3 data-feedback-mask="conversation-name">
          {{ title }}
        </h3>
        <p
          v-if="loading"
          role="status"
          class="export-loading"
        >
          正在读取这份记录的报告…
        </p>
        <div
          v-else-if="errorText !== null"
          role="alert"
          class="export-report-error"
        >
          <strong>报告暂时无法加载</strong><p>{{ errorText }}</p><button
            type="button"
            class="button primary"
            aria-label="重新加载导出报告"
            @click="emit('retry')"
          >
            重新加载
          </button>
        </div>
        <template v-else-if="report !== null">
          <p class="export-report-summary">
            {{ report.message_count.toLocaleString('zh-CN') }} 条消息
          </p>
          <dl class="export-report-counts">
            <div><dt>附件已带上</dt><dd>{{ report.included.toLocaleString('zh-CN') }}</dd></div>
            <div :class="{ 'has-missing': report.missing > 0 }">
              <dt>没能带上</dt><dd>{{ report.missing.toLocaleString('zh-CN') }}</dd>
            </div>
            <div><dt>没有选择</dt><dd>{{ report.excluded.toLocaleString('zh-CN') }}</dd></div>
          </dl>
          <div
            v-if="report.missing > 0"
            class="notice-warn"
          >
            <strong>有附件没能带上</strong><p>有附件未能取得或保存，相关消息仍保留。</p>
          </div>
          <p
            v-else
            class="export-note"
          >
            已选择的可用附件均已带上。
          </p>
          <details class="export-help">
            <summary>“没能带上”和“没有选择”有什么区别？</summary><p>没能带上：已选择保存，但未能取得附件。没有选择：你没有勾选这一类附件，不算缺失。</p>
          </details>
        </template>
      </div>
    </section>
  </div>
</template>

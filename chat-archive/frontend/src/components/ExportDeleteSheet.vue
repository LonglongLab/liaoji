<script setup lang="ts">
import { toRef } from "vue";
import { useModalFocus } from "@/composables/useModalFocus";

const props = defineProps<{ open: boolean; title: string; busy: boolean; errorText: string | null }>();
const emit = defineEmits<{ close: []; confirm: [] }>();
const close = (): void => { if (!props.busy) emit("close"); };
const { container, initial, onKeydown } = useModalFocus(toRef(props,"open"), close);
void container; void initial;
</script>

<template>
  <div
    v-if="open"
    class="export-sheet-backdrop"
    @click.self="close"
  >
    <section
      ref="container"
      class="export-sheet export-delete-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="删除导出记录"
      :aria-busy="busy"
      @keydown="onKeydown"
    >
      <header class="export-sheet-head">
        <h2>删除这份导出？</h2>
      </header>
      <div class="export-sheet-body">
        <h3 data-feedback-mask="conversation-name">
          {{ title }}
        </h3>
        <div class="export-delete-scope">
          <section><strong>将清理</strong><p>导出包和报告</p><p>专属临时文件与范围快照</p><p>导出记录及可关联任务</p></section>
          <section><strong>仍然保留</strong><p>原始聊天和附件保留</p><p>其他导出与独立分享</p><p>已有归档备份</p></section>
        </div>
        <p class="export-note">
          删除后，本条下载与报告入口将失效。
        </p>
        <p class="export-note">
          已下载的副本无法撤回。
        </p>
        <p class="export-note">
          旧任务若没有导出关联，将保留并明确提示。
        </p>
        <p
          v-if="busy"
          role="status"
          class="export-loading"
        >
          正在关闭入口并清理专属产物…
        </p>
        <div
          v-if="errorText !== null"
          role="alert"
          class="notice-warn"
        >
          <strong>清理尚未完成</strong><p>{{ errorText }}</p>
        </div>
        <div class="button-row">
          <button
            ref="initial"
            type="button"
            class="button secondary"
            :disabled="busy"
            @click="close"
          >
            取消
          </button>
          <button
            type="button"
            class="button export-delete-confirm"
            aria-label="确认删除导出记录"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ busy ? '正在清理…' : errorText === null ? '删除记录和导出文件' : '重试删除' }}
          </button>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.export-delete-sheet { max-width: 480px; }
.export-delete-scope { display: grid; grid-template-columns: 1fr 1fr; gap: 12px; margin: 18px 0; }
.export-delete-scope section { border: 1px solid var(--rule); border-radius: 8px; padding: 12px; }
.export-delete-scope strong { font-size: 14px; }
.export-delete-scope section:first-child strong { color: var(--danger); }
.export-delete-scope section:last-child strong { color: var(--safe); }
.export-delete-scope p { font-size: 12px; margin: 7px 0; }
.export-delete-confirm { background: var(--danger-strong); color: var(--surface); border-color: var(--danger-strong); }
</style>

<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
import { toRef, useId } from "vue";
import { useModalFocus } from "@/composables/useModalFocus";

const props = defineProps<{ open: boolean; title: string; busy: boolean; errorText: string | null }>();
const emit = defineEmits<{ close: []; confirm: [] }>();
const close = (): void => { if (!props.busy) emit("close"); };
const descriptionId = useId();
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
      :aria-describedby="errorText === null ? descriptionId : undefined"
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
        <p
          v-if="errorText === null"
          :id="descriptionId"
          class="export-note export-delete-description"
        >
          删除导出记录与文件，保留原聊天和已下载的副本。
        </p>
        <div
          v-if="errorText !== null"
          role="alert"
          class="notice-warn"
        >
          {{ errorText }}
        </div>
        <div class="button-row export-delete-actions">
          <AppAction
            ref="initial"
            type="button"
            variant="secondary"
            class="button secondary"
            :disabled="busy"
            @click="close"
          >
            取消
          </AppAction>
          <AppAction
            type="button"
            variant="quiet"
            class="button export-delete-confirm"
            aria-label="确认删除导出记录"
            :disabled="busy"
            @click="emit('confirm')"
          >
            {{ busy ? '正在删除…' : errorText === null ? '删除' : '重试' }}
          </AppAction>
        </div>
      </div>
    </section>
  </div>
</template>

<style scoped>
.export-delete-sheet { max-width: 420px; }
.export-delete-description,.export-delete-sheet .notice-warn { margin: 6px 0 0; }
.export-delete-actions { justify-content: flex-end; margin-top: 20px; }
.export-delete-confirm { background: var(--danger-strong); color: var(--surface); border-color: var(--danger-strong); }
</style>

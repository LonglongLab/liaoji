<script setup lang="ts">
import type { ExportSummary } from "@/api/archiveApi";
import { formatRelativeTime } from "@/utils/time";

defineProps<{
  items: ExportSummary[];
  busy?: boolean;
  allowFolder?: boolean;
  loading: boolean;
  errorText: string | null;
}>();

const emit = defineEmits<{ retry: []; download: [id: string]; folder: [id: string]; report: [id: string]; delete: [id: string] }>();

const statusLabels: Record<ExportSummary["status"], string> = {
  running: "进行中",
  ready: "已完成",
  failed: "失败",
  cancelled: "已取消",
  missing: "文件缺失",
};

// 四档，不是三档：异常（失败 / 文件缺失）要能和「进行中」一眼分开，
// 而「已取消」是人自己停的，报红是冤枉它。（PA-UI-010）
const statusTone = (status: ExportSummary["status"]): string => {
  if (status === "ready") return "is-safe";
  if (status === "running") return "is-warning";
  if (status === "cancelled") return "is-neutral";
  return "is-danger";
};

const formatLabels: Record<ExportSummary["format"], string> = {
  jsonl: "JSONL",
  json: "JSON",
  csv: "CSV",
  html: "聊天网页包",
};

const sizeOf = (bytes: number | null): string => {
  if (bytes === null) return "—";
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
};
</script>

<template>
  <div
    class="export-history"
    aria-label="导出历史"
  >
    <span class="section-label">导出历史</span>
    <div
      v-if="errorText !== null"
      class="notice-warn"
      role="alert"
    >
      <strong>历史加载失败</strong>
      <p>{{ errorText }}</p>
      <button
        class="text-action"
        type="button"
        @click="emit('retry')"
      >
        重新加载
      </button>
    </div>
    <p
      v-else-if="loading"
      class="export-note"
    >
      正在加载历史……
    </p>
    <p
      v-else-if="items.length === 0"
      class="export-note"
    >
      还没有导出记录。
    </p>
    <div
      v-else
      class="list-stack"
    >
      <div
        v-for="item in items"
        :key="item.id"
        class="export-history-row"
      >
        <div class="job-row-head">
          <strong data-feedback-mask="conversation-name">{{ typeof item.scope.title === "string" ? item.scope.title : formatLabels[item.format] }}</strong>
          <span
            class="job-status status-badge"
            :class="item.deletion_pending ? 'is-warning' : statusTone(item.status)"
          >{{ item.deletion_pending ? '待清理' : statusLabels[item.status] }}</span>
        </div>
        <small>
          {{ item.record_count?.toLocaleString("zh-CN") ?? "—" }} 条 · {{ sizeOf(item.size_bytes) }} ·
          {{ formatRelativeTime(item.created_at) }}
          <template v-if="item.status === 'missing'">
            · 文件已被移动或删除
          </template>
          <template v-else-if="item.error_code !== null">
            · {{ item.error_code }}
          </template>
        </small>
        <div
          class="button-row"
        >
          <button
            v-if="allowFolder !== false && item.status === 'ready' && item.format === 'html' && !item.deletion_pending"
            class="button secondary"
            type="button"
            :disabled="busy"
            @click="emit('folder', item.id)"
          >
            打开保存目录
          </button>
          <button
            v-if="item.status === 'ready' && item.format === 'html' && item.bundle_available && !item.deletion_pending"
            class="button secondary"
            type="button"
            :disabled="busy"
            @click="emit('download', item.id)"
          >
            下载网页包
          </button>
          <button
            v-if="item.status === 'ready' && item.format === 'html' && item.bundle_available && !item.deletion_pending"
            class="button secondary"
            type="button"
            :disabled="busy"
            @click="emit('report', item.id)"
          >
            查看导出报告
          </button>
          <button
            class="button secondary export-delete-action"
            type="button"
            :disabled="busy || item.status === 'running'"
            :title="item.status === 'running' ? '先取消任务，等待结束后再删除' : undefined"
            @click="emit('delete', item.id)"
          >
            {{ item.deletion_pending ? '重试删除' : '删除' }}
          </button>
        </div>
        <p
          v-if="item.status === 'running'"
          class="export-note"
        >
          先取消任务，等待结束后再删除。
        </p>
      </div>
    </div>
  </div>
</template>

<style scoped>
.export-delete-action { color: var(--danger); }
</style>

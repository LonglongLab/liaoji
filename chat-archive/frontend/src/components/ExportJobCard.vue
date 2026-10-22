<script setup lang="ts">
import { computed, onMounted, onUnmounted, ref } from "vue";
import type { Job } from "@/api/archiveApi";
import { jobStatusLabels, jobTone } from "@/lib/jobStatus";

const props = defineProps<{ job: Job | null; exportId: string | null }>();
const emit = defineEmits<{ pause: []; resume: []; cancel: [] }>();
const now = ref(Date.now());
let timer: ReturnType<typeof globalThis.setInterval> | null = null;
onMounted(() => { timer = globalThis.setInterval(() => { now.value = Date.now(); }, 1000); });
onUnmounted(() => { if (timer !== null) globalThis.clearInterval(timer); });
const active = computed(() => props.job !== null && ["queued", "running", "pausing"].includes(props.job.status));
const elapsed = computed(() => {
  const job = props.job;
  if (job === null || job.started_at === null) return "尚未开始";
  const end = job.finished_at ?? now.value;
  const seconds = Math.max(0, Math.floor((end - job.started_at) / 1000));
  return seconds < 60 ? `${String(seconds)} 秒` : `${String(Math.floor(seconds / 60))} 分 ${String(seconds % 60)} 秒`;
});
const stageText = computed(() => {
  if (props.job?.status === "queued") return "等待开始";
  const stage = props.job?.stage;
  if (stage === "prepare") return "固定所选记录的范围";
  if (stage === "write") return "生成网页并整理附件";
  if (stage === "search") return "生成离线搜索索引";
  if (stage === "publish") return "核对文件并生成压缩包";
  return "准备导出";
});
const stale = computed(() => props.job !== null && ["running", "pausing"].includes(props.job.status) && now.value - props.job.updated_at > 35000);
const percent = computed(() => {
  const job = props.job;
  if (job === null || job.stage_progress_total === null || job.stage_progress_total <= 0) return null;
  return Math.min(100, Math.floor(job.stage_progress_current / job.stage_progress_total * 100));
});
const stageProgress = computed(() => {
  const job = props.job;
  if (job === null || job.stage_progress_total === null) return null;
  if (job.stage_progress_unit === "bytes") {
    return `已打包 ${(job.stage_progress_current / 1024 / 1024).toFixed(1)} MB / ${(job.stage_progress_total / 1024 / 1024).toFixed(1)} MB`;
  }
  return `本阶段 ${job.stage_progress_current.toLocaleString('zh-CN')} / ${job.stage_progress_total.toLocaleString('zh-CN')} 条`;
});
const explain = computed(() => {
  const job = props.job;
  if (job?.status === "queued") return "任务已排队，轮到后会开始，不用重复点击。";
  if (job?.status === "pausing") return "正在等待当前操作安全结束后暂停，不要重复点击。";
  if (job?.status === "paused") return "已暂停。恢复时会使用原先固定的记录范围，部分文件需要重新整理。";
  if (job?.status === "failed" || job?.status === "interrupted") return "导出没有完成；原始记录未改变。请查看错误信息后再重试。";
  if (job?.status === "cancelled") return "已取消，不会发布半成品，原始记录未改变。";
  if (job?.status === "completed") return "导出已完成，可以从导出历史打开目录或下载网页包。";
  if (stale.value) return "正在等待本机服务回应，最新状态暂未确认。请先别重复导出，恢复连接后会更新。";
  if (job?.stage === "prepare") return "记录较多时，固定范围可能需要一些时间。此阶段消息条数暂时不变。";
  if (job?.stage === "search") return "消息已整理好，正在建立可离线使用的搜索索引，条数在此阶段不再变化。";
  if (job?.stage === "publish") return "消息已整理好。较大的附件打包耗时更长，此阶段消息条数不再增加。";
  return "图片、语音和大文件耗时不同，消息条数可能暂时不变。后台会持续报告任务状态。";
});
</script>
<template>
  <section
    v-if="job !== null"
    class="export-job-card"
    aria-label="进行中的导出任务"
  >
    <div class="job-row-head">
      <strong>{{ job.status === 'completed' ? '聊天记录已保存' : job.status === 'paused' ? '导出已暂停' : job.status === 'cancelled' ? '导出已取消' : job.status === 'failed' ? '导出未完成' : '正在保存聊天记录' }}</strong><span
        class="job-status status-badge"
        :class="jobTone(job.status)"
      >{{ jobStatusLabels[job.status] }}</span>
    </div>
    <p
      v-if="active"
      class="stage-name"
    >
      {{ stageText }}
    </p>
    <div
      v-if="active"
      class="export-progress"
      :class="{ 'is-indeterminate': percent === null }"
      role="progressbar"
      :aria-label="stageText"
      :aria-valuenow="percent ?? undefined"
      :aria-valuemin="percent === null ? undefined : 0"
      :aria-valuemax="percent === null ? undefined : 100"
    >
      <span :style="percent === null ? undefined : { width: `${String(percent)}%` }" />
    </div>
    <p
      v-if="active && stageProgress !== null"
      class="export-note"
    >
      {{ stageProgress }}
    </p>
    <p>已处理 {{ job.progress_current.toLocaleString('zh-CN') }} 条记录 · 已历时 {{ elapsed }}</p>
    <p class="export-note">
      {{ explain }}
    </p>
    <p
      v-if="active && job.status !== 'queued' && !stale"
      class="export-note"
    >
      任务仍在本机后台运行。可以去其他页面，稍后从导出历史查看；请保持聊记运行。
    </p>
    <div class="button-row">
      <button
        v-if="job.status === 'running' || job.status === 'queued'"
        class="button quiet"
        type="button"
        @click="emit('pause')"
      >
        暂停
      </button>
      <button
        v-if="job.status === 'pausing'"
        class="button quiet"
        type="button"
        disabled
      >
        正在暂停……
      </button>
      <button
        v-if="job.status === 'paused'"
        class="button secondary"
        type="button"
        @click="emit('resume')"
      >
        继续导出
      </button>
      <button
        v-if="!['completed', 'failed', 'cancelled'].includes(job.status)"
        class="button quiet"
        type="button"
        @click="emit('cancel')"
      >
        取消导出
      </button>
    </div>
  </section>
</template>
<style scoped>
.export-job-card{border:1px solid var(--rule);padding:18px;border-radius:6px;background:var(--surface)}.stage-name{margin-top:12px;font-weight:600}.export-progress{height:7px;overflow:hidden;background:var(--track);margin:12px 0}.export-progress span{display:block;height:100%;background:var(--accent-solid);transition:width .3s}.is-indeterminate span{width:28%;animation:export-activity 2.2s ease-in-out infinite}@keyframes export-activity{from{transform:translateX(-100%)}to{transform:translateX(460%)}}@media(prefers-reduced-motion:reduce){.is-indeterminate span{animation:none}}
</style>

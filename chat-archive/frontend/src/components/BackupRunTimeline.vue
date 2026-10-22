<script setup lang="ts">
import { computed, ref } from "vue";

import type { BackupRun } from "@/api/archiveApi";
import {
  describeBackupFailure,
  formatDuration,
  type TimelineMark,
} from "@/lib/backupTimeline";
import { formatTimeOfDay } from "@/utils/time";

// 「最近的备份」时间轴（2026-09-26 原型 A）：横轴是真实时间，每轮一个点，
// 该跑没跑的周期画成虚线空心圈。用 HTML 按百分比摆位而不是一张缩放的 SVG——
// 那样圆和字会跟着容器宽度一起放大缩小。

const props = defineProps<{
  marks: TimelineMark[];
  nextAt: number | null;
  nowSeconds: number;
  intervalHours: number | null;
}>();

const domain = computed(() => {
  const first = props.marks[0]?.at ?? props.nowSeconds;
  const end = Math.max(props.nowSeconds, props.nextAt ?? props.nowSeconds);
  const span = Math.max(end - first, 3600);
  const pad = span * 0.04;
  return { from: first - pad, to: end + pad };
});

const leftOf = (at: number): string => {
  const { from, to } = domain.value;
  return `${(((at - from) / (to - from)) * 100).toFixed(2)}%`;
};

const dayLabel = (at: number): string => {
  const date = new Date(at * 1000);
  return `${String(date.getMonth() + 1)}/${String(date.getDate())}`;
};

// 日期只标五六个：每个点都标，二十个日期挤在一行里谁也读不了。
const labelled = computed(() => {
  const count = props.marks.length;
  const step = Math.max(1, Math.ceil(count / 6));
  return new Set(
    props.marks
      .map((_, index) => index)
      .filter((index) => index % step === 0 && count - 1 - index >= step / 2)
      .concat(count > 0 ? [count - 1] : []),
  );
});

const RUN_LABELS: Readonly<Record<BackupRun["status"], string>> = {
  running: "正在备份",
  success: "成功",
  failed: "失败",
};

const formatSize = (value: number | null): string | null => {
  if (value === null) return null;
  const mib = value / (1024 * 1024);
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB` : `${mib.toFixed(1)} MiB`;
};

const describe = (mark: TimelineMark): string[] => {
  // 点在日期轴上，提示里也用同一种日期写法；「星期日」得再换算一次才对得上。
  const head = `${dayLabel(mark.at)} ${formatTimeOfDay(mark.at)}`;
  if (mark.kind === "missed") return [`${head} · 该跑没跑`, "这一周期没有任何记录"];
  const run = mark.run;
  const lines = [`${head} · ${RUN_LABELS[run.status]}`];
  if (run.status === "failed") {
    lines.push(describeBackupFailure(run.failure_code));
  } else if (run.status === "success") {
    const parts = [formatSize(run.stored_bytes)];
    if (run.finished_at !== null) parts.push(`耗时 ${formatDuration(run.finished_at - run.started_at)}`);
    if (run.verified) parts.push("已校验");
    lines.push(parts.filter((part): part is string => part !== null).join(" · "));
  }
  return lines;
};

const markClass = (mark: TimelineMark): string =>
  mark.kind === "missed" ? "is-missed" : `is-${mark.run.status}`;

const summary = computed(() => {
  const runs = props.marks.filter((mark) => mark.kind === "run");
  const ok = runs.filter((mark) => mark.run.status === "success").length;
  const failed = runs.filter((mark) => mark.run.status === "failed").length;
  const missed = props.marks.length - runs.length;
  const tail = missed > 0 ? `，${String(missed)} 个周期没跑` : "";
  return `最近 ${String(runs.length)} 次备份：成功 ${String(ok)} 次，失败 ${String(failed)} 次${tail}`;
});

const hovered = ref<number | null>(null);
const hoveredLines = computed(() => {
  const index = hovered.value;
  const mark = index === null ? undefined : props.marks[index];
  return mark === undefined ? null : describe(mark);
});
const hoveredLeft = computed(() => {
  const index = hovered.value;
  const mark = index === null ? undefined : props.marks[index];
  return mark === undefined ? "0%" : leftOf(mark.at);
});

const hasMissed = computed(() => props.marks.some((mark) => mark.kind === "missed"));
const hasFailed = computed(() =>
  props.marks.some((mark) => mark.kind === "run" && mark.run.status === "failed"),
);
</script>

<template>
  <div class="backup-timeline">
    <div
      class="backup-timeline-track"
      role="img"
      :aria-label="summary"
      data-testid="backup-runs"
    >
      <span
        class="backup-timeline-now"
        :style="{ left: leftOf(nowSeconds) }"
        aria-hidden="true"
      ><small>现在</small></span>
      <span
        v-for="(mark, index) in marks"
        :key="`${mark.kind}-${mark.at}`"
        class="backup-timeline-mark"
        :class="markClass(mark)"
        :style="{ left: leftOf(mark.at) }"
        :data-kind="mark.kind === 'missed' ? 'missed' : mark.run.status"
        tabindex="0"
        :aria-label="describe(mark).join('，')"
        @pointerenter="hovered = index"
        @pointerleave="hovered = null"
        @focus="hovered = index"
        @blur="hovered = null"
      ><i aria-hidden="true" /></span>
      <span
        v-if="nextAt !== null"
        class="backup-timeline-mark is-next"
        :style="{ left: leftOf(nextAt) }"
        aria-hidden="true"
      ><i /></span>
      <div
        v-if="hoveredLines !== null"
        class="backup-timeline-tip"
        :style="{ left: hoveredLeft }"
        role="tooltip"
      >
        <strong>{{ hoveredLines[0] }}</strong>
        <span
          v-for="line in hoveredLines.slice(1)"
          :key="line"
        >{{ line }}</span>
      </div>
    </div>
    <div
      class="backup-timeline-axis"
      aria-hidden="true"
    >
      <small
        v-for="index in labelled"
        :key="index"
        :style="{ left: leftOf(marks[index]?.at ?? 0) }"
      >{{ dayLabel(marks[index]?.at ?? 0) }}</small>
      <small
        v-if="nextAt !== null"
        class="backup-timeline-next-label"
        :style="{ left: leftOf(nextAt) }"
      >下一次</small>
    </div>
    <div class="backup-timeline-legend">
      <span><i class="is-success" />成功</span>
      <span v-if="hasFailed"><i class="is-failed" />失败</span>
      <span v-if="hasMissed"><i class="is-missed" />该跑没跑</span>
      <span
        v-if="intervalHours !== null"
        class="backup-timeline-interval"
      >每 {{ intervalHours }} 小时一次</span>
    </div>
  </div>
</template>

<style scoped>
.backup-timeline {
  margin-top: 14px;
}

.backup-timeline-track {
  position: relative;
  height: 44px;
  margin: 0 12px;
}

.backup-timeline-track::before {
  position: absolute;
  top: 50%;
  right: 0;
  left: 0;
  height: 1px;
  background: var(--rule);
  content: "";
}

.backup-timeline-mark {
  position: absolute;
  top: 50%;
  display: grid;
  width: 28px;
  height: 36px;
  place-items: center;
  border-radius: 6px;
  cursor: default;
  outline: none;
  transform: translate(-50%, -50%);
}

.backup-timeline-mark:focus-visible {
  box-shadow: 0 0 0 2px var(--link);
}

.backup-timeline-mark > i,
.backup-timeline-legend i {
  display: block;
  width: 14px;
  height: 14px;
  box-sizing: border-box;
  border-radius: 50%;
}

.backup-timeline-mark > i {
  box-shadow: 0 0 0 2px var(--surface-pop, #fdfaf3);
}

.is-success > i,
.backup-timeline-legend i.is-success {
  background: var(--accent-solid);
}

.is-failed > i,
.backup-timeline-legend i.is-failed {
  background: var(--danger);
}

.is-failed > i {
  width: 16px;
  height: 16px;
}

.is-running > i {
  border: 2px solid var(--amber-text);
  border-right-color: transparent;
  animation: backup-spin 1.2s linear infinite;
}

.is-missed > i,
.backup-timeline-legend i.is-missed {
  border: 1.5px dashed var(--ink-faint);
  background: var(--surface-pop, #fdfaf3);
}

.is-next > i {
  border: 1.5px solid var(--rule);
  background: var(--surface-pop, #fdfaf3);
}

.backup-timeline-now {
  position: absolute;
  top: 2px;
  bottom: 6px;
  border-left: 1px dashed var(--ink-faint);
}

.backup-timeline-now small {
  position: absolute;
  top: -16px;
  color: var(--ink-faint);
  font-size: 11px;
  transform: translateX(-50%);
  white-space: nowrap;
}

.backup-timeline-axis {
  position: relative;
  height: 18px;
  margin: 2px 12px 0;
}

.backup-timeline-axis small {
  position: absolute;
  color: var(--ink-faint);
  font-size: 11px;
  transform: translateX(-50%);
  white-space: nowrap;
}

.backup-timeline-tip {
  position: absolute;
  z-index: 5;
  bottom: calc(100% + 4px);
  display: grid;
  padding: 6px 10px;
  border-radius: 8px;
  background: var(--ink-strong);
  color: #fff;
  font-size: 12px;
  line-height: 1.5;
  pointer-events: none;
  transform: translateX(-50%);
  white-space: nowrap;
}

.backup-timeline-tip strong {
  font-weight: 600;
}

.backup-timeline-legend {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
  gap: 6px 16px;
  margin-top: 6px;
  color: var(--ink-soft);
  font-size: 12px;
}

.backup-timeline-legend span {
  display: inline-flex;
  align-items: center;
  gap: 5px;
}

.backup-timeline-legend i {
  width: 10px;
  height: 10px;
}

.backup-timeline-interval {
  margin-left: auto;
}

/* 窄屏上二十个点挤在不到 300px 里：点缩小；「下一次」轴标签会和最后一个日期
   撞在一起，结论那一行已经写了下一次的时间，这里就不再标。 */
@media (max-width: 720px) {
  .backup-timeline-track,
  .backup-timeline-axis {
    margin-inline: 6px;
  }

  .backup-timeline-mark {
    width: 14px;
  }

  .backup-timeline-mark > i {
    width: 9px;
    height: 9px;
    box-shadow: 0 0 0 1.5px var(--surface-pop, #fdfaf3);
  }

  .is-failed > i {
    width: 11px;
    height: 11px;
  }

  .is-missed > i,
  .is-next > i {
    border-width: 1px;
  }

  .backup-timeline-next-label {
    display: none;
  }
}

@keyframes backup-spin {
  to {
    transform: rotate(360deg);
  }
}

@media (prefers-reduced-motion: reduce) {
  .is-running > i {
    animation: none;
  }
}
</style>

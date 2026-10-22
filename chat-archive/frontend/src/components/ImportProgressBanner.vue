<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";

import type { Job } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { useOnboardingStore } from "@/stores/onboarding";
import { formatBytes } from "@/utils/format";
import {
  formatEta,
  type JobProgress,
  jobProgressOf,
  stageProgressOf,
} from "@/utils/progressEta";
import { formatDateLabel } from "@/utils/time";

const api = useArchiveApi();
const store = useOnboardingStore();
const router = useRouter();

const collapsed = ref(false);
// 完成态只在"本会话内看着它完成"时提示一次;应用启动时读到的历史完成
// 任务不弹——那不是新信息。
const completedJobId = ref<string | null>(null);
const dismissedJobId = ref<string | null>(null);

const job = computed(() => store.currentJob);
const activeJob = computed(() => store.activeImportJob);
const failedJob = computed<Job | null>(() => {
  const current = job.value;
  return current !== null && current.kind === "import" && current.status === "failed"
    ? current
    : null;
});
const pausedJob = computed<Job | null>(() => {
  const current = job.value;
  return current !== null && current.kind === "import" && current.status === "paused"
    ? current
    : null;
});

// 横幅一出现就确保跟随循环在跑:用户可能直接刷新进主界面,此时没有任何
// 视图启动过 followJob。
watch(
  activeJob,
  (current) => {
    if (current !== null) store.ensureFollowing(api);
  },
  { immediate: true },
);

watch(
  () => [job.value?.id ?? null, job.value?.status ?? null] as const,
  ([id, status], [previousId, previousStatus]) => {
    if (
      id !== null &&
      status === "completed" &&
      previousId === id &&
      previousStatus !== null &&
      previousStatus !== "completed"
    ) {
      completedJobId.value = id;
    }
    if (id !== previousId) {
      collapsed.value = false;
    }
  },
);

const showCompleted = computed(
  () =>
    job.value !== null &&
    job.value.id === completedJobId.value &&
    job.value.id !== dismissedJobId.value,
);

// 分母从**两个**字段里取（PSU-2.107）。导入的总数一直报在段级
// `stage_progress_total` 上（`import_worker` 在 messages 段传它），而作业级的
// `progress_total` 从来没被填过——这里从前只读后者，所以真机上永远没有百分比。
//
// 分子分母的配对交给 `jobProgressOf`（PSU-2.109）：回落到段级分母时分子也得是
// 段级的，这里从前是拿作业级的分子除段级的分母，今天恰好相等而已。
const progress = computed<JobProgress | null>(() =>
  activeJob.value === null ? null : jobProgressOf(activeJob.value),
);

const percent = computed<number | null>(() => progress.value?.percent ?? null);

const etaText = computed<string | null>(() => {
  const current = activeJob.value;
  if (current === null) return null;
  const here = stageProgressOf(current);
  return here === null || here.etaSeconds === null
    ? null
    : `预计还需 ${formatEta(here.etaSeconds)}`;
});

const coverageText = computed(() => {
  const current = activeJob.value;
  if (current === null) return null;
  if (current.oldest_imported_at === null) return null;
  return `已导入至 ${formatDateLabel(current.oldest_imported_at)}`;
});

const progressText = computed(() => {
  const current = activeJob.value;
  if (current === null) return "";
  const format = new Intl.NumberFormat("zh-CN");
  const here = progress.value;
  // 没有分母时照旧只报已导入的条数——不编一个百分比出来。
  if (here === null) return `${format.format(current.progress_current)} 条`;
  // 单位跟着分母走（PSU-2.110）。`decrypting` 段报的是字节，真机实测
  // `0/110592 bytes`——照「条」印出来就是「0 / 110,592 条」，一句假话。
  if (here.unit === "bytes") return `${formatBytes(here.done)} / ${formatBytes(here.total)}`;
  return `${format.format(here.done)} / ${format.format(here.total)} 条`;
});

// 自动同步排的那一轮与人点的走同一条路，横幅也照常出现（PA-PAGE-011）。
// 加两个字是为了让「我没点过它怎么在跑」这个问题当场有答案。
const AUTO_PREFIX = "自动 · ";
const autoPrefix = (job: Job | null): string =>
  job?.trigger === "auto" ? AUTO_PREFIX : "";

const stageText = computed(() => {
  const current = activeJob.value;
  if (current === null) return "";
  const prefix = autoPrefix(current);
  if (current.stage === "decrypting") return `${prefix}正在读取与解密`;
  return `${prefix}历史消息仍在导入`;
});

const reopenProgress = (): void => {
  // 横幅显示的一定是在跑的那个；但点下去的这一刻它可能刚好跑完——那就不进
  // 开始页，进去是一张没有出口的「已完成」卡。
  if (store.reopenImportProgress()) void router.push("/start");
};

const dismissCompleted = (): void => {
  dismissedJobId.value = completedJobId.value;
};
</script>

<template>
  <!-- 进行中:常驻横幅,可折叠成小徽标,绝不假装完成。 -->
  <div
    v-if="activeJob !== null && !collapsed"
    class="import-banner is-active"
    role="status"
    aria-live="polite"
    data-testid="import-banner"
  >
    <span
      class="import-banner-spinner"
      aria-hidden="true"
    />
    <!-- 反馈录屏放行（登记在 lib/feedbackUnmaskRegistry.ts）：这一段里全是
         阶段名、日期、条数与百分比，逐个标而不是标在 .import-banner-copy 上
         ——放行按祖先链生效，标在容器上等于把以后加进来的东西也一并放了。 -->
    <span class="import-banner-copy">
      <strong data-feedback-unmask="import-stage">{{ stageText }}</strong>
      <span
        v-if="coverageText"
        data-testid="import-banner-coverage"
        data-feedback-unmask="import-coverage"
      >{{ coverageText }}</span>
      <span
        data-testid="import-banner-progress"
        data-feedback-unmask="import-progress"
      >{{ progressText }}<template v-if="percent !== null">（{{ percent }}%）</template></span>
      <span
        v-if="etaText !== null"
        data-testid="import-banner-eta"
        data-feedback-unmask="import-eta"
      >{{ etaText }}</span>
    </span>
    <span class="import-banner-actions">
      <button
        class="text-action"
        type="button"
        @click="reopenProgress"
      >
        查看详情
      </button>
      <button
        class="text-action"
        type="button"
        aria-label="折叠导入进度横幅"
        @click="collapsed = true"
      >
        折叠
      </button>
    </span>
  </div>
  <button
    v-else-if="activeJob !== null"
    class="import-banner is-collapsed"
    type="button"
    aria-label="展开导入进度横幅"
    data-testid="import-banner-collapsed"
    data-feedback-unmask="import-collapsed"
    @click="collapsed = false"
  >
    导入中<template v-if="percent !== null">
      {{ percent }}%
    </template>…
  </button>

  <div
    v-else-if="pausedJob !== null"
    class="import-banner is-paused"
    role="status"
    data-testid="import-banner-paused"
  >
    <span class="import-banner-copy">
      <strong>{{ autoPrefix(pausedJob) }}导入已暂停</strong>
      <span>历史消息尚未导完,已归档内容可正常浏览。</span>
    </span>
    <button
      class="text-action"
      type="button"
      @click="reopenProgress"
    >
      前往处理
    </button>
  </div>

  <div
    v-else-if="failedJob !== null"
    class="import-banner is-failed"
    role="alert"
    data-testid="import-banner-failed"
  >
    <span class="import-banner-copy">
      <!-- 错误码是后端定义的枚举，不含归档内容。 -->
      <strong data-feedback-unmask="error-code">{{ autoPrefix(failedJob) }}导入未完成（{{ failedJob.error_code ?? "未知错误" }}）</strong>
      <span>已归档内容保留,可重新设置归档继续。</span>
    </span>
    <button
      class="text-action"
      type="button"
      @click="reopenProgress"
    >
      前往处理
    </button>
  </div>

  <div
    v-else-if="showCompleted"
    class="import-banner is-completed"
    role="status"
    data-testid="import-banner-completed"
  >
    <span class="import-banner-copy">
      <strong>历史消息导入完成</strong>
    </span>
    <button
      class="text-action"
      type="button"
      @click="dismissCompleted"
    >
      知道了
    </button>
  </div>
</template>

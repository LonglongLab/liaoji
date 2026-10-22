<script setup lang="ts">
/**
 * 「正在同步中」——挂在 `AppShell` 上，任何页面都看得见（PSU-2.107）。
 *
 * 从前只有导入有全局横幅（`ImportProgressBanner`，它按 `kind !== "import"` 直接
 * 返回 null），同步一旦离开同步健康页就什么都看不见了。用户原话：「用户从哪个
 * 页面再进来，它其实都应该显示"正在同步中"，除非用户手动点击终止同步或者关闭了
 * 应用。」
 *
 * 数据来自服务器（`listJobs` 轮询），不是本地状态——所以刷新页面之后它还在。
 * 复用导入横幅的样式类：同一件事（后台有个长任务）不该长出两副样子。
 */
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";

import { useArchiveApi } from "@/api/injection";
import { useVerificationProgress } from "@/composables/useVerificationProgress";
import { useActiveJobs } from "@/composables/activeJobs";
import { useAutoSyncStatus } from "@/composables/autoSyncStatus";
import { useOpenAutoSyncCycle } from "@/composables/useLocalAccountSwitch";
import { useOnboardingStore } from "@/stores/onboarding";
import { formatEta, stageProgressOf } from "@/utils/progressEta";
import { labelForSyncStage } from "@/utils/syncStage";

const api = useArchiveApi();
const store = useOnboardingStore();
const router = useRouter();

const accountId = computed(() => store.selectedSourceId);
const ownJob = useActiveJobs(api, accountId).runningSync;

// 自动同步一轮挨个跑每个账号，而上面那一份只问选中的号：推的是另一个号时，
// 哪一页都看不出后台在忙。「是哪个号」取自动同步的状态，「还在不在跑」取那个号
// 的作业列表（2 秒一次），跑完就收。选中的号自己在跑时，它优先。
const autoSync = useAutoSyncStatus(api);
const otherCycle = computed(() => {
  const cycle = autoSync.value?.current_cycle ?? null;
  return cycle !== null && cycle.account_id !== accountId.value ? cycle : null;
});
const otherJob = useActiveJobs(
  api,
  computed(() => otherCycle.value?.account_id ?? null),
).runningSync;
const job = computed(() => ownJob.value ?? otherJob.value);
const verification = useVerificationProgress(job);
const otherAccountName = computed(() => {
  const cycle = otherCycle.value;
  if (ownJob.value !== null || otherJob.value === null || cycle === null) return null;
  return (
    store.accounts.find((item) => item.id === cycle.account_id)?.display_name ?? "另一个账号"
  );
});
const openAutoSyncCycle = useOpenAutoSyncCycle();

const collapsed = ref(false);
// 换了一个作业就重新展开：折叠是对**这一次**同步的处置，不是一条永久偏好。
watch(
  () => job.value?.id ?? null,
  (id, previous) => {
    if (id !== previous) collapsed.value = false;
  },
);

const stageText = computed(() => {
  const current = job.value;
  if (current === null) return "";
  // 自动同步排的那一轮与人点的走同一条路（PA-PAGE-011）。标题前加两个字，
  // 「我没点过它怎么在跑」当场就有答案。
  const prefix = current.trigger === "auto" ? "自动 · " : "";
  if (verification.value !== null) return prefix + verification.value.title;
  const stage = labelForSyncStage(current.stage);
  return stage === null
    ? `${prefix}正在同步到远程档案`
    : `${prefix}正在同步 · ${stage}`;
});

const progress = computed(() => (job.value === null ? null : stageProgressOf(job.value)));

const progressText = computed(() => {
  if (verification.value !== null) return verification.value.line;
  const current = job.value;
  if (current === null) return "";
  const count = new Intl.NumberFormat("zh-CN").format(current.progress_current);
  const here = progress.value;
  // 分母答不出时只报已送出的数，不编一个百分比。
  if (here === null) return `已送出 ${count} 项`;
  const done = new Intl.NumberFormat("zh-CN").format(here.done);
  const total = new Intl.NumberFormat("zh-CN").format(here.total);
  return `已送出 ${count} 项 · 本段 ${done} / 预计 ${total} 项（${String(here.percent)}%）`;
});

const etaText = computed(() => {
  if (verification.value !== null) return verification.value.elapsed;
  const here = progress.value;
  return here === null || here.etaSeconds === null
    ? null
    : `本段预计还需 ${formatEta(here.etaSeconds)}`;
});

const openSyncHealth = (): void => {
  const cycle = otherCycle.value;
  if (otherAccountName.value !== null && cycle !== null) {
    void openAutoSyncCycle(cycle);
    return;
  }
  void router.push("/sync-health");
};

// 同步健康页自己就画着这张进度卡，横幅在它上面再说一遍同样的数字只是噪声。
const onSyncPage = computed(() => router.currentRoute.value.path === "/sync-health");
const shown = computed(() => job.value !== null && !onSyncPage.value);
</script>

<template>
  <div
    v-if="shown && !collapsed"
    class="import-banner is-active"
    role="status"
    aria-live="polite"
    data-testid="sync-banner"
  >
    <span
      class="import-banner-spinner"
      aria-hidden="true"
    />
    <!-- 反馈录屏放行（登记在 lib/feedbackUnmaskRegistry.ts）：阶段名来自一张
         固定词表，其余是条数与百分比。逐个标而不是标在容器上。 -->
    <span class="import-banner-copy">
      <strong data-feedback-unmask="sync-stage">{{ stageText }}</strong>
      <!-- 账号名是个人信息：单独一个元素，**不**进录屏放行名单。 -->
      <span
        v-if="otherAccountName !== null"
        data-feedback-mask="account-name"
        data-testid="sync-banner-account"
      >{{ otherAccountName }}</span>
      <span
        data-testid="sync-banner-progress"
        data-feedback-unmask="sync-progress"
      >{{ progressText }}</span>
      <span
        v-if="etaText !== null"
        data-testid="sync-banner-eta"
        data-feedback-unmask="sync-eta"
      >{{ etaText }}</span>
    </span>
    <span class="import-banner-actions">
      <button
        class="text-action"
        type="button"
        data-testid="sync-banner-open"
        @click="openSyncHealth"
      >
        查看进度
      </button>
      <button
        class="text-action"
        type="button"
        aria-label="折叠同步进度横幅"
        @click="collapsed = true"
      >
        折叠
      </button>
    </span>
  </div>
  <button
    v-else-if="shown"
    class="import-banner is-collapsed"
    type="button"
    aria-label="展开同步进度横幅"
    data-testid="sync-banner-collapsed"
    data-feedback-unmask="sync-collapsed"
    @click="collapsed = false"
  >
    同步中<template v-if="verification?.percent != null">
      {{ verification.percent }}%
    </template><template v-else-if="verification === null && progress !== null">
      {{ progress.percent }}%
    </template>…
  </button>
</template>

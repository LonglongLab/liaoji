<script setup lang="ts">
import { useQuery } from "@tanstack/vue-query";
import { computed, ref, watch, toRef, onBeforeUnmount } from "vue";
import { ArchiveApiError, type AnalysisOptions } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import AppSelect from "@/components/ui/AppSelect.vue";
import AppDatePicker from "@/components/ui/AppDatePicker.vue";
import AnalysisBars from "@/components/AnalysisBars.vue";
import WorkspaceAccountNotice from "@/components/WorkspaceAccountNotice.vue";
import AppShell from "@/components/AppShell.vue";
import { downloadCsv, toCsv } from "@/lib/csv";
import { useWorkspaceModuleScope } from "@/composables/workspaceModules";
import { formatRelativeTime } from "@/utils/time";

const api = useArchiveApi();
const props = withDefaults(defineProps<{ mode?: "local" | "remote" }>(), { mode: "local" });
const workspace = useWorkspaceModuleScope(toRef(props, "mode"));
const { accountId, accountName } = workspace;

const today = (): string => new Date(Date.now() + 8 * 3_600_000).toISOString().slice(0, 10);
const dateTo = ref(today());
const dateFrom = ref("");
const preset = ref<number | null>(30);
const scope = ref("all");
const selectedName = ref("");
const selectedIsGroup = ref(false);
const directoryOpen = ref(false);
const directorySearch = ref("");
let directoryTimer: ReturnType<typeof globalThis.setTimeout> | undefined;
onBeforeUnmount(() => globalThis.clearTimeout(directoryTimer));
const searchDirectory = (event: { value: string }): void => {
  globalThis.clearTimeout(directoryTimer);
  directoryTimer = globalThis.setTimeout(() => { directorySearch.value = event.value; }, 250);
};
const directory = useQuery({
  queryKey: computed(() => ["analysis-directory", workspace.identity.value, accountId.value, directorySearch.value]),
  enabled: computed(() => directoryOpen.value && accountId.value !== null),
  queryFn: () => api.listConversations(accountId.value ?? "", { q: directorySearch.value, limit: 50 }),
});
const conversationChoices = ref<{ value: string; label: string; isGroup: boolean }[]>([]);
const directoryChoices = ref<{ value: string; label: string; isGroup: boolean }[]>([]);
watch(directory.data, page => {
  directoryChoices.value = (page?.items ?? []).map(row => ({
    value: `conv:${row.id}`, label: row.display_name, isGroup: row.is_group,
  }));
});
const scopeOptions = computed(() => {
  const choices = new Map([...conversationChoices.value, ...directoryChoices.value].map(row => [row.value, row]));
  if (scope.value.startsWith("conv:") && !choices.has(scope.value)) {
    choices.set(scope.value, {value: scope.value, label: selectedName.value, isGroup: selectedIsGroup.value});
  }
  return [
    { value: "all", label: "全部会话", isGroup: false }, { value: "private", label: "仅私聊", isGroup: false },
    { value: "group", label: "仅群聊", isGroup: false }, ...choices.values(),
  ];
});
const changeScope = (value: string): void => {
  const choice = scopeOptions.value.find(item => item.value === value);
  selectedName.value = choice?.label ?? "";
  selectedIsGroup.value = choice?.isGroup ?? false;
  scope.value = value;
};
const selectedDay = ref<string | null>(null);
const selectedHour = ref<string | null>(null);
const choosePreset = (days: number): void => {
  preset.value = days;
  dateTo.value = today();
  dateFrom.value = new Date(Date.parse(`${dateTo.value}T00:00:00Z`) - (days - 1) * 86_400_000).toISOString().slice(0, 10);
};
choosePreset(30);
const validation = computed(() => {
  if (!dateFrom.value || !dateTo.value) return "请选择开始和结束日期。";
  const length = (Date.parse(dateTo.value) - Date.parse(dateFrom.value)) / 86_400_000 + 1;
  return Number.isFinite(length) && length >= 1 && length <= 366 ? null : "开始日期不能晚于结束日期，范围最长 366 天。";
});
const options = computed<AnalysisOptions>(() => ({
  dateFrom: dateFrom.value, dateTo: dateTo.value,
  conversationKind: scope.value === "private" || scope.value === "group" ? scope.value : "all",
  conversationId: scope.value.startsWith("conv:") ? scope.value.slice(5) : undefined,
}));
const query = useQuery({
  queryKey: computed(() => ["analysis", workspace.identity.value, accountId.value, options.value]),
  enabled: computed(() => accountId.value !== null && validation.value === null),
  queryFn: () => api.getAnalysis(accountId.value ?? "", options.value),
});
const data = computed(() => validation.value === null ? query.data.value : undefined);
const error = computed(() => query.error.value === null ? null : query.error.value instanceof ArchiveApiError ? query.error.value.message : "分析暂时不可用，请重试。");
const summary = computed(() => data.value?.summary);
const groupDrill = computed(() => scope.value.startsWith("conv:") && (data.value?.selected_conversation_is_group ?? selectedIsGroup.value));
watch(data, page => {
  if (!page || scope.value.startsWith("conv:")) return;
  conversationChoices.value = page.conversations.map(row => ({
    value: `conv:${row.conversation_id}`, label: row.display_name, isGroup: row.is_group === true,
  }));
});
const scopeLabel = computed(() => scope.value === "all" ? "全部会话" : scope.value === "private" ? "仅私聊" : scope.value === "group" ? "仅群聊" : selectedName.value);
const daily = computed(() => data.value?.daily ?? []);
const hourly = computed(() => data.value?.hours ?? []);
const trendItems = computed(() => daily.value.map((row) => ({ label: row.day, count: row.message_count })));
const hourItems = computed(() => hourly.value.map((row) => ({ label: `${String(row.hour).padStart(2, "0")}:00`, count: row.message_count })));
const peakDay = computed(() => daily.value.reduce<typeof daily.value[number] | null>((peak, row) => peak === null || row.message_count > peak.message_count ? row : peak, null));
const peakHour = computed(() => hourly.value.reduce<typeof hourly.value[number] | null>((peak, row) => peak === null || row.message_count > peak.message_count ? row : peak, null));
const labels: Record<string, string> = { text: "文字", quote: "引用文字", image: "图片", audio: "语音", video: "视频", emoji: "表情", file: "文件", link: "链接", app: "应用消息", unknown: "未知类型" };
const typeLabel = (type: string): string => labels[type] ?? "其他";
const format = (count: number | undefined): string => count === undefined ? "—" : count.toLocaleString("zh-CN");
const percentage = (count: number, total: number): string => total > 0 ? `${(count / total * 100).toFixed(1)}%` : "—";
const selectedDayRow = computed(() => daily.value.find((row) => row.day === selectedDay.value));
const selectedHourRow = computed(() => hourly.value.find((row) => `${String(row.hour).padStart(2, "0")}:00` === selectedHour.value));
const selectConversation = (id: string, name: string, isGroup: boolean | null): void => { selectedName.value = name; selectedIsGroup.value = isGroup === true; scope.value = `conv:${id}`; };
const canExport = computed(() => data.value !== undefined && data.value.summary.message_count > 0 && !query.isFetching.value && error.value === null);
const exportData = (): void => {
  if (!canExport.value || data.value === undefined) return;
  const page = data.value;
  const metadata = [accountId.value ?? "", page.date_from, page.date_to, scopeLabel.value, page.timezone, page.updated_at ?? "", page.participant_ranking_truncated ? "群成员排行仅前100，占比以全部群消息计算" : page.ranking_truncated ? "会话排行仅前100" : "完整当前排行"];
  const rows = [
    ["概览", "消息总量", page.summary.message_count], ["概览", "发出", page.summary.sent_count], ["概览", "收到", page.summary.received_count],
    ["概览", "方向未知", page.summary.unknown_direction_count ?? 0],
    ["概览", "活跃日", page.summary.active_days], ["概览", "活跃会话", page.summary.conversation_count],
    ...page.daily.map((row) => ["逐日消息量", row.day, row.message_count]),
    ...page.hours.map((row) => ["小时分布", `${String(row.hour)}:00`, row.message_count]),
    ...page.types.map((row) => ["消息类型", typeLabel(row.message_type), row.message_count]),
    ...page.conversations.map((row) => ["会话消息量", row.display_name, row.message_count]),
    ...(page.participants ?? []).map(row => ["群内成员发言", row.is_self ? "我" : row.display_name, row.message_count]),
    ...(groupDrill.value ? [["成员口径", "未识别发送者消息数", page.unknown_sender_count ?? 0]] : []),
  ].map((row) => [...row, ...metadata]);
  downloadCsv("chat-analysis.csv", toCsv(["指标", "维度", "数量", "账号范围", "开始日期", "结束日期", "会话范围", "时区", "统计更新时间", "排行范围"], rows));
};
watch([dateFrom, dateTo, scope], () => { selectedDay.value = null; selectedHour.value = null; });
watch(workspace.identity, () => { globalThis.clearTimeout(directoryTimer); directorySearch.value = ""; directoryOpen.value = false; changeScope("all"); conversationChoices.value = []; directoryChoices.value = []; choosePreset(30); });
</script>
<template>
  <AppShell
    variant="app"
    :account-name="accountName"
    :mode="mode"
  >
    <section class="analysis-page">
      <header class="analysis-header">
        <div>
          <h1 class="page-title">
            聊天分析
          </h1><p class="page-sub">
            查看消息分布、会话消息量和群成员发言。
          </p>
        </div><button
          class="button primary"
          type="button"
          :disabled="!canExport"
          @click="exportData"
        >
          导出数据
        </button>
      </header>
      <div class="analysis-filters">
        <div
          class="analysis-presets"
          aria-label="时间范围"
        >
          <button
            v-for="days in [7, 30, 90]"
            :key="days"
            type="button"
            :aria-pressed="preset === days"
            @click="choosePreset(days)"
          >
            近 {{ days }} 天
          </button>
        </div>
        <label>会话 <AppSelect
          :model-value="scope"
          :options="scopeOptions"
          aria-label="分析会话范围"
          data-feedback-mask="conversation-name"
          sensitive
          :loading="directory.isFetching.value"
          @show="directoryOpen = true"
          @hide="directoryOpen = false"
          @filter="searchDirectory"
          @update:model-value="changeScope"
        /></label>
        <label>开始 <AppDatePicker
          v-model="dateFrom"
          :max="dateTo"
          aria-label="统计开始日期"
          @update:model-value="preset = null"
        /></label>
        <label>结束 <AppDatePicker
          v-model="dateTo"
          :min="dateFrom"
          aria-label="统计结束日期"
          @update:model-value="preset = null"
        /></label>
      </div>
      <p
        v-if="directory.error.value && directoryOpen"
        class="notice-warn"
        role="alert"
      >
        会话检索暂时不可用，请重新打开后重试。
      </p>
      <p
        v-if="validation"
        class="notice-warn"
        role="alert"
      >
        {{ validation }}
      </p>
      <div
        v-else-if="error"
        class="notice-warn"
        role="alert"
      >
        <p>{{ error }}</p><button
          class="text-action"
          type="button"
          @click="query.refetch()"
        >
          重新加载
        </button>
      </div>
      <WorkspaceAccountNotice
        v-else-if="accountId === null"
        :hint="workspace.accountHint.value"
        :guide="workspace.accountGuide.value"
        :can-retry="workspace.canRetry.value"
        @retry="workspace.retryAccounts"
      />
      <p
        v-else-if="query.isFetching.value"
        class="analysis-loading"
        role="status"
      >
        正在加载当前范围的分析……
      </p>
      <template v-if="data && !error">
        <div class="analysis-scope">
          <span>当前范围：<span data-feedback-mask="conversation-name">{{ scopeLabel }}</span> · 所有图表共享筛选</span><button
            v-if="scope !== 'all'"
            class="button secondary analysis-back"
            type="button"
            @click="changeScope('all')"
          >
            <span aria-hidden="true">←</span> 返回全部会话
          </button><span>北京时间 · 最后更新：{{ formatRelativeTime(data.updated_at) }}</span>
        </div>
        <p
          v-if="(data.summary.unknown_direction_count ?? 0) > 0"
          class="notice-info"
          role="status"
        >
          {{ format(data.summary.unknown_direction_count ?? 0) }} 条消息方向未知，未计入发送或接收。
        </p>
        <div class="analysis-metrics">
          <div><p>消息总量</p><strong>{{ format(summary?.message_count) }} <small>条</small></strong><p>发出 {{ format(summary?.sent_count) }} · 收到 {{ format(summary?.received_count) }}</p></div>
          <div><p>我发出的占比</p><strong>{{ (summary?.unknown_direction_count ?? 0) > 0 ? "—" : percentage(summary?.sent_count ?? 0, summary?.message_count ?? 0) }}</strong><p>{{ (summary?.unknown_direction_count ?? 0) > 0 ? "发送方向不全，暂不计算占比" : "按当前账号的发送方向统计" }}</p></div>
          <div><p>有聊天的日子</p><strong>{{ format(summary?.active_days) }} <small>天</small></strong><p>所选范围共 {{ daily.length }} 天</p></div>
          <div><p>活跃会话</p><strong>{{ format(summary?.conversation_count) }} <small>个</small></strong><p>私聊 {{ format(summary?.private_count) }} · 群聊 {{ format(summary?.group_count) }}</p></div>
        </div>
        <div class="analysis-grid">
          <section class="analysis-panel">
            <h2>消息量趋势</h2><p class="analysis-sub">
              哪段时间聊得多？点柱形查看当天概况。
            </p><AnalysisBars
              :items="trendItems"
              label="逐日消息数"
              @select="selectedDay = $event"
            /><p
              v-if="peakDay && peakDay.message_count > 0"
              class="analysis-insight"
            >
              {{ peakDay.day }} 消息最多，共 {{ format(peakDay.message_count) }} 条。
            </p><div
              v-if="selectedDayRow"
              class="analysis-insight"
              role="status"
            >
              {{ selectedDayRow.day }}：{{ format(selectedDayRow.message_count) }} 条消息 <button
                class="text-action"
                type="button"
                @click="selectedDay = null"
              >
                收起
              </button>
            </div><details>
              <summary>查看数据表</summary><div class="analysis-table">
                <table>
                  <thead><tr><th>日期</th><th>消息数</th></tr></thead><tbody>
                    <tr
                      v-for="row in daily"
                      :key="row.day"
                    >
                      <td>{{ row.day }}</td><td>{{ format(row.message_count) }}</td>
                    </tr>
                  </tbody>
                </table>
              </div>
            </details>
          </section>
          <section class="analysis-panel">
            <template v-if="groupDrill">
              <h2>群内谁发言多</h2>
              <p class="analysis-sub">
                按所选日期统计成员发言，占比以本群全部消息为分母；不代表成员之间的对话关系。
              </p>
              <p
                v-if="data.participants === undefined"
                class="chart-empty"
              >
                当前服务尚不支持成员统计，请升级服务。
              </p>
              <p
                v-else-if="!data.participants.length"
                class="chart-empty"
              >
                所选范围暂无群成员发言。
              </p>
              <ol
                v-else
                class="analysis-ranking"
                aria-label="群成员发言排行"
              >
                <li
                  v-for="(row, index) in data.participants"
                  :key="row.is_self ? 'self' : row.sender_uid ?? 'unknown'"
                >
                  <div class="analysis-member">
                    <span class="analysis-rank-index">{{ String(index + 1).padStart(2, '0') }}</span>
                    <span class="analysis-rank-name">
                      <span data-feedback-mask="contact-name">{{ row.is_self ? '我' : row.display_name }}</span>
                      <span class="analysis-track"><span :style="{ width: `${String(row.message_count / Math.max(data.summary.message_count, 1) * 100)}%` }" /></span>
                    </span>
                    <span class="analysis-participant-count">{{ format(row.message_count) }} 条<small>{{ percentage(row.message_count, data.summary.message_count) }}</small></span>
                  </div>
                </li>
              </ol>
              <p
                v-if="data.participants !== undefined"
                class="analysis-sub"
              >
                {{ data.participant_ranking_truncated ? '仅展示前 100 项 · ' : '' }}共 {{ format(data.participant_count) }} 位已识别的发言成员{{ data.summary.sent_count > 0 ? '（含我）' : '' }} · {{ format(data.unknown_sender_count) }} 条消息未识别发送者
              </p>
            </template>
            <template v-else>
              <h2>会话消息量</h2><p class="analysis-sub">
                按当前时间范围排序。群聊包含全体成员的消息，点群聊查看成员发言。
              </p>
              <p
                v-if="!data.conversations.length"
                class="chart-empty"
              >
                暂无活跃会话。
              </p>
              <ol class="analysis-ranking">
                <li
                  v-for="(row, index) in data.conversations"
                  :key="row.conversation_id"
                >
                  <button
                    type="button"
                    :aria-label="`分析${row.display_name}`"
                    @click="selectConversation(row.conversation_id, row.display_name, row.is_group)"
                  >
                    <span class="analysis-rank-index">{{ String(index + 1).padStart(2, '0') }}</span>
                    <span class="analysis-rank-name"><span data-feedback-mask="conversation-name">{{ row.display_name }}</span> <small v-if="row.is_group">群</small><span class="analysis-track"><span :style="{ width: `${String(row.message_count / (data.conversations[0]?.message_count ?? 1) * 100)}%` }" /></span></span>
                    <span>{{ format(row.message_count) }} 条 ↗</span>
                  </button>
                </li>
              </ol>
              <p class="analysis-sub">
                {{ data.ranking_truncated ? '仅展示前 100 个会话' : `共 ${String(data.conversations.length)} 个活跃会话` }} · 消息量不代表关系强弱
              </p>
            </template>
          </section>
          <section class="analysis-panel">
            <h2>常聊时段</h2><p class="analysis-sub">
              24 小时消息分布，按北京时间计算。
            </p><AnalysisBars
              :items="hourItems"
              label="按小时消息数"
              @select="selectedHour = $event"
            /><p
              v-if="peakHour && peakHour.message_count > 0"
              class="analysis-insight"
            >
              最活跃在 {{ peakHour.hour }}:00–{{ peakHour.hour }}:59，占 {{ percentage(peakHour.message_count, data.summary.message_count) }}。
            </p><p
              v-if="selectedHourRow"
              class="analysis-insight"
              role="status"
            >
              {{ selectedHourRow.hour }}:00–{{ selectedHourRow.hour }}:59，共 {{ format(selectedHourRow.message_count) }} 条。
            </p>
          </section>
          <section class="analysis-panel">
            <h2>消息类型</h2><p class="analysis-sub">
              一条消息计一次，与附件是否可用无关。
            </p><p
              v-if="!data.types.length"
              class="chart-empty"
            >
              暂无类型分布。
            </p><div
              v-for="row in data.types"
              :key="row.message_type"
              class="analysis-type"
            >
              <span>{{ typeLabel(row.message_type) }}</span><span class="analysis-track"><span :style="{ width: `${String(row.message_count / data.summary.message_count * 100)}%` }" /></span><span>{{ percentage(row.message_count, data.summary.message_count) }}<small>{{ format(row.message_count) }} 条</small></span>
            </div>
          </section>
        </div>
        <details class="analysis-definition">
          <summary>口径说明：这些数字怎么算？</summary><p>账号范围：<span data-feedback-mask="account-name">{{ accountName ?? "当前账号" }}</span>。仅统计所选范围内已归档、去重后的消息。不含系统通知，不重复计入合并转发内部消息。所有图表和导出使用同一筛选，日界按北京时间（Asia/Shanghai）计算。统计随导入更新。</p><p>未导入的时期不代表没有聊天。有聊天的日子指至少有一条符合条件的归档消息的日期。活跃会话数不等于联系人总数。发送方向按归档中的账号身份统计。</p>
        </details>
      </template>
    </section>
  </AppShell>
</template>
<style scoped>
.analysis-page { min-width: 0; }
.analysis-page .page-title { font-size: 32px; }
.analysis-header { display: flex; justify-content: space-between; align-items: center; gap: 20px; margin-bottom: 22px; }
.analysis-filters { display: flex; flex-wrap: wrap; gap: 14px; align-items: center; padding: 16px 0; border-block: 1px solid var(--rule); }
.analysis-filters label { display: flex; gap: 6px; align-items: center; font-size: 12px; color: var(--ink-soft); }
.analysis-presets { display: flex; gap: 3px; padding: 3px; border-radius: 7px; background: var(--surface-sunken); }
.analysis-presets button { border: 0; background: transparent; border-radius: 5px; padding: 6px 10px; cursor: pointer; }
.analysis-presets button[aria-pressed="true"] { background: var(--surface-pop); color: var(--accent-ink); font-weight: 650; }
.analysis-scope { display: flex; gap: 12px; flex-wrap: wrap; justify-content: space-between; margin: 14px 0; font-size: 12px; color: var(--ink-faint); }
.analysis-metrics { display: grid; grid-template-columns: repeat(4, 1fr); border: 1px solid var(--rule); border-radius: 10px; background: var(--surface); margin-bottom: 22px; }
.analysis-metrics > div { padding: 20px; border-right: 1px solid var(--rule); }
.analysis-metrics > div:last-child { border-right: 0; }
.analysis-metrics p { font-size: 12px; color: var(--ink-faint); }
.analysis-metrics strong { display: block; font-size: 30px; font-weight: 600; margin: 7px 0; font-variant-numeric: tabular-nums; }
.analysis-metrics strong small { font-weight: 400; }
.analysis-grid { display: grid; grid-template-columns: 1.45fr 1fr; gap: 20px; }
.analysis-panel { min-width: 0; padding: 22px; border: 1px solid var(--rule); background: var(--surface); border-radius: 10px; }
.analysis-panel h2 { font-size: 16px; margin-bottom: 5px; }
.analysis-sub { color: var(--ink-faint); font-size: 12px; }
.analysis-insight { margin-top: 16px; padding: 9px 12px; font-size: 12px; color: var(--accent-ink); background: var(--safe-bg); border-radius: 5px; }
.analysis-ranking { list-style: none; padding: 0; margin: 15px 0; max-height: 270px; overflow: auto; }
.analysis-ranking button, .analysis-member { display: grid; grid-template-columns: 22px 1fr auto; gap: 10px; align-items: center; width: 100%; border: 0; background: transparent; text-align: left; padding: 12px 0; cursor: pointer; font-size: 13px; }
.analysis-ranking button:hover { color: var(--accent-ink); }
.analysis-rank-index { color: var(--ink-faint); font-size: 12px; }
.analysis-rank-name { min-width: 0; overflow-wrap: anywhere; }
.analysis-rank-name small { border: 1px solid var(--rule); border-radius: 3px; padding: 0 4px; color: var(--ink-faint); }
.analysis-track { display: block; height: 6px; background: var(--track); border-radius: 4px; overflow: hidden; margin-top: 6px; }
.analysis-track > span { display: block; height: 100%; background: #80ad76; border-radius: 4px; }
.analysis-type { display: grid; grid-template-columns: 65px 1fr 70px; gap: 12px; align-items: center; margin-top: 16px; font-size: 13px; }
.analysis-type > span:last-child { text-align: right; }
.analysis-type small { display: block; color: var(--ink-faint); }
.analysis-type .analysis-track { height: 9px; margin: 0; }
details { border-top: 1px solid var(--rule); margin-top: 20px; padding-top: 15px; font-size: 12px; }
summary { cursor: pointer; color: var(--ink-faint); }
.analysis-definition p { margin-top: 12px; line-height: 1.8; color: var(--ink-soft); }
.analysis-table { overflow: auto; max-height: 240px; margin-top: 12px; }
table { border-collapse: collapse; width: 100%; }
th, td { text-align: left; border-bottom: 1px solid var(--rule); padding: 8px; }
.analysis-loading { padding: 20px 0; color: var(--ink-faint); }
@media (max-width: 1100px) { .analysis-grid { grid-template-columns: 1fr; } .analysis-metrics > div { padding: 15px; } }
@media (max-width: 700px) { .analysis-page .page-title { font-size: 27px; } .analysis-header { gap: 8px; } .analysis-header .button { white-space: nowrap; padding: 8px 10px; } .analysis-metrics { grid-template-columns: 1fr 1fr; } .analysis-metrics > div:nth-child(2) { border-right: 0; } .analysis-metrics > div:nth-child(-n+2) { border-bottom: 1px solid var(--rule); } .analysis-panel { padding: 18px; } }
</style>

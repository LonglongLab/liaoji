<script setup lang="ts">
import AppAction from "@/components/ui/AppAction.vue";
import AppSelect from "@/components/ui/AppSelect.vue";
import { ChevronDown, Paperclip, SlidersHorizontal, X } from "@lucide/vue";
import { computed, nextTick, ref, useId, watch } from "vue";
import Tabs from "primevue/tabs";
import TabList from "primevue/tablist";
import Tab from "primevue/tab";
import TabPanels from "primevue/tabpanels";
import TabPanel from "primevue/tabpanel";
import type { RenderConversation } from "@/lib/transcript";
import type { DirectoryEntry } from "@/lib/searchScopes";
import { conversationDirectoryEntry } from "@/lib/conversationDirectory";
import type { ExportEstimate, MessageSummary } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import ExportDocument from "@/components/ExportDocument.vue";
import SearchDirectory from "@/components/SearchDirectory.vue";
import ExportDateRangeSheet from "@/components/ExportDateRangeSheet.vue";
import { exportDateLabel, useExportBuilderStore, type ExportPeriod } from "@/stores/exportBuilder";

const props = defineProps<{
  conversations: RenderConversation[];
  accountId: string | null;
  attachmentLoader?: (id: string) => Promise<Blob>;
  thumbnailLoader?: (id: string) => Promise<Blob>;
  avatarFor?: (id: string) => string | null;
  estimate: ExportEstimate | null;
  preview: MessageSummary[];
  previewError: string | null;
  previewLoading: boolean;
  previewHasMore: boolean;
  previewConversationId: string | null;
  estimating: boolean;
  creating: boolean;
  running: boolean;
  errorText: string | null;
  searchText: string;
  hasMore: boolean;
  loadingMore: boolean;
  conversationLoading?: boolean;
  conversationRefreshing?: boolean;
  conversationError: string | null;
}>();
const emit = defineEmits<{ estimate: []; start: []; search: [value: string]; more: []; retryConversations: []; "search-focus": [focused: boolean]; "visible-change": [ids: readonly string[] | null]; "preview-conversation": [id: string | null]; "preview-more": [] }>();
const store = useExportBuilderStore();
const api = useArchiveApi();
const filter = ref<"all" | "contact" | "group">("all");
const chatFilters = [{key:"all",label:"全部"},{key:"contact",label:"联系人"},{key:"group",label:"群聊"}] as const;
const selectedExpanded = ref(false);
const selectedRegion = ref<HTMLElement | null>(null);
const selectionToolbar = ref<HTMLElement | null>(null);
const selectedChipsId = useId();
const dateOpen = ref(false);
const dateConversation = ref<DirectoryEntry | null>(null);
const datePending = ref(false);
const periodChoice = ref<ExportPeriod>(store.period);
const periodLabelId = useId();
const mediaPanelId = useId();
const mediaOpen = ref(false);
const mediaToggle = ref<InstanceType<typeof AppAction> | null>(null);
const rangesOpen = ref(false);
const rangesPanelId = useId();
const datePresets = [
  { value: "all", label: "全部时间" },
  { value: "today", label: "今天" },
  { value: "week", label: "最近 7 天" },
  { value: "month", label: "最近 30 天" },
  { value: "thismonth", label: "本月" },
  { value: "custom", label: "自选日期" },
] satisfies readonly { value: ExportPeriod; label: string }[];
watch(() => store.period, period => { periodChoice.value = period; });
type ConversationChoice = Pick<RenderConversation,"id" | "display_name" | "is_group" | "avatar_ref">;
const remembered = ref<Record<string,ConversationChoice>>({});
const mainVisible = ref<readonly string[] | null>([]);
watch(() => props.conversations, items => {
  const next = {...remembered.value};
  for (const item of items) {
    store.conversationNames[item.id] = item.display_name;
    next[item.id] = {id:item.id,display_name:item.display_name,is_group:item.is_group,
      ...(item.avatar_ref === undefined ? {} : {avatar_ref:item.avatar_ref})};
  }
  remembered.value = next;
}, { immediate: true });
watch(mainVisible, ids => emit("visible-change", ids));
const entry = (item: ConversationChoice): DirectoryEntry => conversationDirectoryEntry(item, props.avatarFor);
const filteredConversations = computed(() => props.conversations.filter(item => filter.value === "all"
  || (filter.value === "group" ? item.is_group === true : item.is_group === false)));
const entries = computed(() => filteredConversations.value.map(entry));
const selectedEntries = computed<DirectoryEntry[]>(() => store.conversationIds.map(id => {
  const item = remembered.value[id];
  return item === undefined ? {id,name:store.conversationNames[id] ?? "已选聊天",named:false} : entry(item);
}));
const selectedChips = computed(() => selectedExpanded.value ? selectedEntries.value : selectedEntries.value.slice(0, 4));
watch(() => store.conversationIds.length, count => {
  if (count <= 4) selectedExpanded.value = false;
});
const removeSelected = async (id: string, event: MouseEvent): Promise<void> => {
  const hadFocus = event.currentTarget === globalThis.document.activeElement;
  const index = selectedChips.value.findIndex(chat => chat.id === id);
  store.toggleConversation(id);
  if (!hadFocus) return;
  await nextTick();
  const remaining = selectedRegion.value?.querySelectorAll<HTMLButtonElement>(".export-selected-chip");
  const next = remaining?.[Math.min(index, remaining.length - 1)]
    ?? selectionToolbar.value?.querySelector<HTMLButtonElement>("button");
  next?.focus({ preventScroll: true });
};
const selection = computed(() => store.allConversations ? "全部已归档聊天" : `已选 ${String(store.conversationIds.length)} 个聊天`);
const busy = computed(() => props.creating || props.running);
const dateLabel = computed(() => exportDateLabel(store.dateFromInput, store.dateToInput));
const rangeEntries = computed(() => {
  if (!store.allConversations) return selectedEntries.value;
  const loaded = new Map(props.conversations.map(item => [item.id, entry(item)]));
  for (const [id] of store.activeOverrides) {
    if (!loaded.has(id)) loaded.set(id, { id, name: store.conversationNames[id] ?? "已选聊天", named: false });
  }
  return [...loaded.values()];
});
const estimateRows = computed(() => props.estimate?.conversations ?? []);
const previewOptions = computed(() => {
  const counts = new Map(estimateRows.value.map(chat => [chat.conversation_id, chat]));
  const chats = store.allConversations
    ? [...estimateRows.value.map(chat => ({ id: chat.conversation_id, name: chat.display_name })),
      ...store.activeOverrides.filter(([id]) => !counts.has(id)).map(([id]) => ({ id, name: store.conversationNames[id] ?? "已选聊天" }))]
    : selectedEntries.value;
  if (!chats.length) return [{ value: "", name: "聊天预览", count: null }];
  return chats.map(chat => {
    const result = counts.get(chat.id);
    const count = result?.message_count ?? (props.estimate?.conversations === undefined ? null : 0);
    const name = result?.display_name ?? chat.name;
    return {
      value: chat.id, name, count,
    };
  });
});
const previewChoice = computed(() => props.previewConversationId ?? previewOptions.value[0]?.value ?? "");
const changePreview = (id: string | number): void => {
  if (typeof id === "string") emit("preview-conversation", id || null);
};
const rangeLabelFor = (id: string): string => {
  const range = store.dateOverrides[id];
  return range === undefined ? dateLabel.value : exportDateLabel(range.from, range.to);
};
const dateDraft = computed(() => dateConversation.value === null
  ? store.period === "custom" ? { from: store.dateFromInput, to: store.dateToInput } : { from: "", to: "" }
  : store.dateOverrides[dateConversation.value.id] ?? { from: "", to: "" });
const mediaOptions = [
  {key:"includeImages",label:"图片与表情",hint:null},
  {key:"includeAudio",label:"语音",hint:null},
  {key:"includeFiles",label:"文件",hint:null},
  {key:"includeVideo",label:"视频",hint:"原视频可能让导出包变大"},
] as const;
const mediaLabel = computed(() => mediaOptions.filter(item => store[item.key]).map(item => item.label).join("、") || "仅文字与消息卡片");
const leaveSearch = (): void => {
  if (globalThis.document.activeElement instanceof HTMLInputElement) globalThis.document.activeElement.blur();
  emit("search-focus",false);
};
const go = (step: number): void => {
  if (busy.value) return;
  leaveSearch();
  if (step === 2 && store.step === 1) {
    if (store.allConversations) emit("search", "");
    if (store.period !== "custom") store.setPeriod(store.period);
  }
  store.step = step;
};
const showAttachmentDetails = async (): Promise<void> => {
  if (busy.value) return;
  mediaOpen.value = true;
  go(2);
  await nextTick();
  mediaToggle.value?.focus();
};
const submitRangeSearch = (event: Event): void => {
  if (event instanceof KeyboardEvent && event.isComposing) return;
  event.preventDefault();
  if (event.target instanceof HTMLInputElement) emit("search", event.target.value);
};
const keepFooterFocus = (event: PointerEvent): void => {
  if(event.isPrimary && event.button === 0 && globalThis.document.activeElement instanceof HTMLInputElement) event.preventDefault();
};
const toggle = (id: string): void => { if(!busy.value && !store.allConversations) store.toggleConversation(id); };
const selectListed = (): void => {
  if (busy.value || props.conversationRefreshing) return;
  store.allConversations=false;
  for(const item of filteredConversations.value) if(!store.conversationIds.includes(item.id)) store.conversationIds.push(item.id);
};
const selectAll = (): void => { if(!busy.value){store.allConversations=true;store.conversationIds=[];} };
const clearSelection = (): void => { if(!busy.value){store.allConversations=false;store.conversationIds=[];store.dateOverrides={};} };
const choosePeriod = (value: ExportPeriod): void => {
  if(busy.value)return;
  if (value === "custom") {
    datePending.value = true;
    return;
  }
  store.setPeriod(value);
};
const openDates = (chat: DirectoryEntry | null = null): void => {
  dateConversation.value = chat;
  dateOpen.value = true;
};
const onPeriodHide = (): void => {
  if (!datePending.value) return;
  // The select restores keyboard focus before the date dialog takes it.
  datePending.value = false;
  openDates();
};
const closeDates = (): void => {
  dateOpen.value = false;
  dateConversation.value = null;
  periodChoice.value = store.period;
};
const applyDates = (from: string,to: string): void => {
  if(busy.value)return;
  if (dateConversation.value !== null) {
    store.dateOverrides[dateConversation.value.id] = { from, to };
    return;
  }
  store.dateFromInput=from;store.dateToInput=to;store.period="custom";
};
const loadAttachment = (id: string): Promise<Blob> => props.accountId === null ? Promise.reject(new Error("尚未选择账号"))
  : props.attachmentLoader?.(id) ?? api.getAttachmentContent(id,props.accountId);
const loadThumbnail = (id: string): Promise<Blob> => props.accountId === null ? Promise.reject(new Error("尚未选择账号"))
  : props.thumbnailLoader?.(id) ?? api.getAttachmentThumbnail(id,props.accountId);
const submit = (): void => {
  if(store.step===3 && store.hasScope && store.validDates && props.estimate!==null && props.estimate.message_count>0
    && !busy.value) emit("start");
};
watch(busy,value=>{if(value){datePending.value=false;closeDates();leaveSearch();}});
</script>

<template>
  <div class="export-builder">
    <form
      class="export-form html-export-wizard"
      :inert="dateOpen"
      @submit.prevent="submit"
    >
      <nav
        class="wizard-steps"
        aria-label="导出步骤"
      >
        <button
          type="button"
          :disabled="busy"
          :aria-current="store.step === 1 ? 'step' : undefined"
          @click="go(1)"
        >
          <span>1</span> 选聊天
        </button>
        <button
          type="button"
          :disabled="!store.hasScope || busy"
          :aria-current="store.step === 2 ? 'step' : undefined"
          @click="go(2)"
        >
          <span>2</span> 选时间
        </button>
        <button
          type="button"
          :disabled="estimate === null || busy"
          :aria-current="store.step === 3 ? 'step' : undefined"
          @click="go(3)"
        >
          <span>3</span> 确认导出
        </button>
      </nav>
      <div
        v-if="errorText !== null"
        class="notice-warn export-action-error"
        role="alert"
      >
        <strong>暂时未能完成</strong><p>{{ errorText }}</p>
      </div>
      <section
        v-if="store.step === 1"
        class="wizard-panel export-select-panel"
      >
        <header class="wizard-heading">
          <h2>想保存哪些聊天？</h2>
          <p
            class="export-note"
            data-testid="export-fragment-hint"
          >
            这里导出整段聊天的离线网页包；如只需保存几条消息或生成长图，请在聊天页右键或长按消息选「多选 → 导出」。
          </p>
        </header>
        <div class="wizard-body">
          <SearchDirectory
            title="导出会话选择"
            placeholder="搜索联系人或群聊"
            selection-layout="inline"
            :entries="entries"
            :selected-ids="store.conversationIds"
            :all-selected="store.allConversations"
            :query="searchText"
            :loading="conversationLoading ?? false"
            :refreshing="conversationRefreshing ?? false"
            :fetching-more="loadingMore"
            :has-more="hasMore"
            :disabled="busy"
            :error-text="conversationError"
            @toggle="toggle"
            @update:query="emit('search',$event)"
            @load-more="emit('more')"
            @search-focus="emit('search-focus',$event)"
            @visible-change="mainVisible = $event"
          >
            <template #toolbar>
              <div
                ref="selectionToolbar"
                class="export-chat-toolbar"
              >
                <div
                  class="export-chat-filters"
                  aria-label="聊天类型"
                >
                  <button
                    v-for="item in chatFilters"
                    :key="item.key"
                    type="button"
                    :disabled="busy"
                    :aria-pressed="filter === item.key"
                    @click="filter = item.key"
                  >
                    {{ item.label }}
                  </button>
                  <AppAction
                    type="button"
                    variant="inline"
                    class="export-inline-action"
                    :disabled="busy"
                    @click="store.allConversations ? clearSelection() : selectAll()"
                  >
                    {{ store.allConversations ? '取消全选' : '全部聊天' }}
                  </AppAction>
                </div>
                <div class="export-loaded-summary">
                  <small>{{ hasMore ? `当前列表 ${entries.length} 个 · 还可加载` : `${entries.length} 个聊天` }}</small><AppAction
                    class="text-action"
                    type="button"
                    :disabled="busy || conversationRefreshing || entries.length === 0"
                    @click="selectListed"
                  >
                    选中当前列表
                  </AppAction>
                </div>
                <AppAction
                  v-if="conversationError !== null"
                  type="button"
                  class="text-action"
                  @click="emit('retryConversations')"
                >
                  重试加载聊天
                </AppAction>
                <section
                  v-if="selectedEntries.length > 0"
                  ref="selectedRegion"
                  class="export-selected-chats"
                  aria-label="已选聊天"
                >
                  <strong>已选 {{ selectedEntries.length }} 个</strong>
                  <div
                    :id="selectedChipsId"
                    class="export-selected-chips"
                    :class="{ 'is-expanded': selectedExpanded }"
                  >
                    <AppAction
                      v-for="chat in selectedChips"
                      :key="chat.id"
                      class="export-selected-chip"
                      :title="chat.name"
                      :aria-label="'移除 ' + chat.name"
                      :disabled="busy"
                      @click="removeSelected(chat.id, $event)"
                    >
                      <span data-feedback-mask="conversation-name">{{ chat.name }}</span>
                      <X
                        :size="16"
                        aria-hidden="true"
                      />
                    </AppAction>
                  </div>
                  <AppAction
                    v-if="selectedEntries.length > 4"
                    :aria-expanded="selectedExpanded"
                    :aria-controls="selectedChipsId"
                    @click="selectedExpanded = !selectedExpanded"
                  >
                    {{ selectedExpanded ? '收起' : '另有 ' + (selectedEntries.length - 4) + ' 个' }}
                  </AppAction>
                </section>
              </div>
            </template>
          </SearchDirectory>
        </div>
        <footer @pointerdown="keepFooterFocus">
          <div class="export-footer-copy">
            <strong>{{ store.hasScope ? selection : '还没有选择聊天' }}</strong><small>选好后继续</small>
          </div><AppAction
            type="button"
            variant="primary"
            class="button primary"
            :disabled="!store.hasScope || busy || accountId === null"
            @click="go(2)"
          >
            下一步：选时间
          </AppAction>
        </footer>
      </section>
      <section
        v-else-if="store.step === 2"
        class="wizard-panel export-options-panel"
      >
        <header class="wizard-heading">
          <h2>选要保存的时间</h2>
          <p>{{ selection }}</p>
        </header>
        <div class="wizard-body export-options-body">
          <div class="export-time-settings export-option-card">
            <h3
              :id="periodLabelId"
              class="export-period-label"
            >
              共同时间范围
            </h3>
            <AppSelect
              v-model="periodChoice"
              class="export-period-select"
              :options="datePresets"
              :filter="false"
              :disabled="busy"
              :aria-labelledby="periodLabelId"
              aria-label="时间范围"
              @update:model-value="choosePeriod"
              @hide="onPeriodHide"
            />
            <div class="export-period-summary">
              <div role="status">
                <p
                  v-if="store.period === 'all'"
                  class="export-note"
                >
                  保存所选聊天里的全部已归档消息。
                </p>
                <template v-else>
                  <strong>{{ dateLabel }}</strong>
                  <p class="export-note">
                    {{ store.dateFromInput === store.dateToInput ? '仅当天，按当前设备时间计算。' : '含起止当天，按当前设备时间计算。' }}
                  </p>
                </template>
              </div>
              <AppAction
                v-if="store.period === 'custom'"
                type="button"
                variant="secondary"
                class="button secondary"
                aria-haspopup="dialog"
                :disabled="busy"
                @click="openDates()"
              >
                修改日期
              </AppAction>
            </div>
            <p
              v-if="!store.validDates"
              role="alert"
              class="notice-warn"
            >
              请填写有效日期，结束日期不能早于开始日期。
            </p>
          </div>
          <div class="export-option-card">
            <AppAction
              class="export-media-summary"
              :aria-expanded="rangesOpen"
              :aria-controls="rangesPanelId"
              :disabled="busy"
              @click="rangesOpen = !rangesOpen"
            >
              <span class="export-media-icon"><SlidersHorizontal
                :size="18"
                aria-hidden="true"
              /></span>
              <span>
                <strong>高级设置</strong>
                <small>{{ store.activeOverrides.length ? `${store.activeOverrides.length} 个聊天使用单独范围` : '可给每个聊天单独选日期' }}</small>
              </span>
              <ChevronDown
                :size="16"
                aria-hidden="true"
              />
            </AppAction>
            <div
              v-show="rangesOpen"
              :id="rangesPanelId"
              class="export-range-list"
            >
              <div
                v-if="store.allConversations"
                class="export-range-search"
              >
                <input
                  type="search"
                  aria-label="查找要单独设置的聊天"
                  placeholder="输入聊天名称后按回车"
                  :value="searchText"
                  data-feedback-mask="search-query"
                  @search="submitRangeSearch"
                  @keydown.enter="submitRangeSearch"
                >
              </div>
              <p
                v-if="rangeEntries.length === 0"
                class="export-note"
              >
                {{ conversationLoading || conversationRefreshing ? '正在加载聊天…' : conversationError ?? '没有找到聊天。' }}
              </p>
              <div
                v-for="chat in rangeEntries"
                :key="chat.id"
                class="export-override-row"
              >
                <div>
                  <strong data-feedback-mask="conversation-name">{{ chat.name }}</strong>
                  <small>{{ store.dateOverrides[chat.id] ? rangeLabelFor(chat.id) : '跟随共同范围' }}</small>
                </div>
                <div class="export-override-actions">
                  <AppAction
                    v-if="store.dateOverrides[chat.id]"
                    :disabled="busy || (store.allConversations && conversationRefreshing)"
                    @click="delete store.dateOverrides[chat.id]"
                  >
                    恢复共同范围
                  </AppAction>
                  <AppAction
                    variant="secondary"
                    aria-haspopup="dialog"
                    :disabled="busy || (store.allConversations && conversationRefreshing)"
                    @click="openDates(chat)"
                  >
                    {{ store.dateOverrides[chat.id] ? '修改日期' : '单独选日期' }}
                  </AppAction>
                </div>
              </div>
              <AppAction
                v-if="store.allConversations && hasMore"
                :loading="loadingMore"
                :disabled="conversationRefreshing"
                @click="emit('more')"
              >
                加载更多聊天
              </AppAction>
              <AppAction
                v-if="store.allConversations && conversationError !== null"
                @click="emit('retryConversations')"
              >
                重试加载聊天
              </AppAction>
            </div>
          </div>
          <div class="export-media-settings export-option-card">
            <AppAction
              ref="mediaToggle"
              class="export-media-summary"
              :aria-expanded="mediaOpen"
              :aria-controls="mediaPanelId"
              :disabled="busy"
              @click="mediaOpen = !mediaOpen"
            >
              <span class="export-media-icon">
                <Paperclip
                  :size="18"
                  aria-hidden="true"
                />
              </span>
              <span><strong>随包保存的附件</strong><small>{{ mediaLabel }}</small></span>
              <ChevronDown
                :size="16"
                aria-hidden="true"
              />
            </AppAction>
            <div
              v-show="mediaOpen"
              :id="mediaPanelId"
              class="export-media-list"
            >
              <button
                v-for="item in mediaOptions"
                :key="item.key"
                type="button"
                role="switch"
                :aria-label="item.label"
                :aria-checked="store[item.key]"
                :disabled="busy"
                class="export-media-option"
                @click="store[item.key] = !store[item.key]"
              >
                <span>{{ item.label }}<small v-if="item.hint !== null">{{ item.hint }}</small></span><span
                  class="export-switch"
                  aria-hidden="true"
                />
              </button>
              <p
                v-if="estimate !== null"
                class="export-media-estimate"
              >
                已缓存 {{ estimate.media_ready }} · 待处理 {{ estimate.media_pending }} · 暂不可用 {{ estimate.media_missing }}
              </p>
            </div>
          </div>
        </div>
        <footer>
          <AppAction
            class="button secondary"
            variant="secondary"
            type="button"
            :disabled="busy"
            @click="go(1)"
          >
            上一步
          </AppAction><AppAction
            class="button primary"
            variant="primary"
            type="button"
            :disabled="!store.hasScope || !store.validDates || estimating || busy || accountId === null"
            @click="emit('estimate')"
          >
            {{ estimating ? '正在核对…' : '下一步：确认导出' }}
          </AppAction>
        </footer>
      </section>
      <section
        v-else
        class="wizard-panel export-preview-panel"
      >
        <header class="wizard-heading">
          <h2>确认导出</h2>
        </header>
        <div class="wizard-body export-preview-body">
          <section
            class="export-preview-sample"
            aria-label="聊天预览"
          >
            <div class="export-confirm-tools">
              <div class="export-confirm-total">
                <strong v-if="estimate !== null">
                  {{ estimate.conversation_count }} 个聊天 · {{ estimate.message_count.toLocaleString('zh-CN') }} 条消息
                </strong>
                <AppAction
                  v-if="estimate !== null && estimate.media_missing > 0"
                  class="export-confirm-warning"
                  :disabled="busy"
                  @click="showAttachmentDetails"
                >
                  {{ estimate.media_missing }} 个附件暂不可用
                </AppAction>
              </div>
              <div class="export-confirm-actions">
                <AppAction
                  :disabled="busy"
                  @click="go(2)"
                >
                  修改时间
                </AppAction>
                <AppAction
                  class="button primary"
                  variant="primary"
                  type="submit"
                  data-feedback-action="开始导出"
                  :disabled="busy || estimate === null || estimate.message_count === 0 || !store.hasScope || !store.validDates || accountId === null"
                >
                  {{ creating ? '正在创建…' : running ? '正在导出…' : '开始导出' }}
                </AppAction>
              </div>
            </div>
            <p
              v-if="estimate?.message_count === 0"
              class="export-note export-preview-empty"
              role="status"
            >
              所选时间内无消息。
            </p>
            <Tabs
              v-else
              class="export-preview-tabs"
              :value="previewChoice"
              scrollable
              :show-navigators="false"
              @update:value="changePreview"
            >
              <TabList :pt="{ tabList: { 'aria-label': '聊天预览' } }">
                <Tab
                  v-for="chat in previewOptions"
                  :key="chat.value"
                  :value="chat.value"
                  :title="chat.name"
                >
                  <span data-feedback-mask="conversation-name">{{ chat.name }}</span>
                  <small v-if="chat.count !== null">
                    {{ chat.count ? chat.count.toLocaleString('zh-CN') + ' 条' : '无消息' }}
                  </small>
                </Tab>
              </TabList>
              <TabPanels>
                <TabPanel
                  v-for="chat in previewOptions"
                  :key="chat.value"
                  :value="chat.value"
                >
                  <template v-if="chat.value === previewChoice">
                    <div class="export-preview-range">
                      {{ rangeLabelFor(chat.value) }}
                    </div>
                    <div
                      v-if="preview.length > 0"
                      class="wizard-reader"
                      tabindex="0"
                      aria-label="聊天预览内容"
                    >
                      <div class="export-doc">
                        <ExportDocument
                          :messages="preview"
                          :show-sender-names="true"
                          :load-attachment="loadAttachment"
                          :load-thumbnail="loadThumbnail"
                          :eager-media="false"
                        />
                      </div>
                      <div class="export-preview-progress">
                        <small
                          v-if="previewHasMore && chat.count !== null"
                          role="status"
                        >
                          {{ preview.length }} / {{ chat.count }} 条
                        </small>
                        <small
                          v-if="previewError !== null"
                          role="alert"
                        >{{ previewError }}</small>
                        <AppAction
                          v-if="previewHasMore"
                          :aria-disabled="previewLoading"
                          @click="!previewLoading && emit('preview-more')"
                        >
                          {{ previewLoading ? '正在加载…' : previewError !== null ? '重试加载' : '加载更多' }}
                        </AppAction>
                      </div>
                    </div>
                    <p
                      v-else
                      class="export-note export-preview-empty"
                      role="status"
                    >
                      {{ previewLoading ? '正在加载预览…' : previewError ?? (chat.count === 0 ? '所选时间内无消息，导出时跳过。' : '暂无预览。') }}
                    </p>
                    <AppAction
                      v-if="previewError !== null && preview.length === 0"
                      @click="emit('preview-conversation', previewConversationId)"
                    >
                      重试预览
                    </AppAction>
                  </template>
                </TabPanel>
              </TabPanels>
            </Tabs>
          </section>
        </div>
      </section>
    </form>
    <ExportDateRangeSheet
      :open="dateOpen"
      :from="dateDraft.from"
      :to="dateDraft.to"
      :conversation-name="dateConversation?.name"
      @close="closeDates"
      @apply="applyDates"
    />
  </div>
</template>

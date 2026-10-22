<script setup lang="ts">
import { computed, ref, watch, toRef } from "vue";
import type { RenderConversation } from "@/lib/transcript";
import type { DirectoryEntry } from "@/lib/searchScopes";
import { conversationDirectoryEntry } from "@/lib/conversationDirectory";
import type { ExportEstimate, MessageSummary } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import { useModalFocus } from "@/composables/useModalFocus";
import ExportDocument from "@/components/ExportDocument.vue";
import SearchDirectory from "@/components/SearchDirectory.vue";
import ExportDateRangeSheet from "@/components/ExportDateRangeSheet.vue";
import { useExportBuilderStore } from "@/stores/exportBuilder";

const props = defineProps<{
  conversations: RenderConversation[];
  accountId: string | null;
  attachmentLoader?: (id: string) => Promise<Blob>;
  thumbnailLoader?: (id: string) => Promise<Blob>;
  avatarFor?: (id: string) => string | null;
  estimate: ExportEstimate | null;
  preview: MessageSummary[];
  previewError: string | null;
  estimating: boolean;
  creating: boolean;
  running: boolean;
  errorText: string | null;
  searchText: string;
  hasMore: boolean;
  loadingMore: boolean;
  conversationLoading?: boolean;
  conversationError: string | null;
}>();
const emit = defineEmits<{ estimate: []; start: []; search: [value: string]; more: []; retryConversations: []; "search-focus": [focused: boolean]; "visible-change": [ids: readonly string[] | null] }>();
const store = useExportBuilderStore();
const api = useArchiveApi();
const filter = ref<"all" | "contact" | "group">("all");
const chatFilters = [{key:"all",label:"全部"},{key:"contact",label:"联系人"},{key:"group",label:"群聊"}] as const;
const selectedOpen = ref(false);
const managerQuery = ref("");
const dateOpen = ref(false);
const period = ref(store.customDates ? "custom" : "all");
type ConversationChoice = Pick<RenderConversation,"id" | "display_name" | "is_group" | "avatar_ref">;
const remembered = ref<Record<string,ConversationChoice>>({});
const mainVisible = ref<readonly string[] | null>([]);
const managerVisible = ref<readonly string[] | null>([]);
watch(() => props.conversations, items => {
  const next = {...remembered.value};
  for (const item of items) {
    store.conversationNames[item.id] = item.display_name;
    next[item.id] = {id:item.id,display_name:item.display_name,is_group:item.is_group,
      ...(item.avatar_ref === undefined ? {} : {avatar_ref:item.avatar_ref})};
  }
  remembered.value = next;
}, { immediate: true });
watch([selectedOpen,mainVisible,managerVisible], () => emit("visible-change",selectedOpen.value ? managerVisible.value : mainVisible.value));
const entry = (item: ConversationChoice): DirectoryEntry => conversationDirectoryEntry(item, props.avatarFor);
const filteredConversations = computed(() => props.conversations.filter(item => filter.value === "all"
  || (filter.value === "group" ? item.is_group === true : item.is_group === false)));
const entries = computed(() => filteredConversations.value.map(entry));
const selectedEntries = computed<DirectoryEntry[]>(() => store.conversationIds.map(id => {
  const item = remembered.value[id];
  return item === undefined ? {id,name:store.conversationNames[id] ?? "已选聊天",named:false} : entry(item);
}));
const managerEntries = computed(() => selectedEntries.value.filter(item => item.name.includes(managerQuery.value)));
const selection = computed(() => store.allConversations ? "全部已归档聊天" : `已选 ${String(store.conversationIds.length)} 个聊天`);
const busy = computed(() => props.creating || props.running);
const dateLabel = computed(() => !store.customDates ? "全部时间" : `${store.dateFromInput.replaceAll('-','.')} — ${store.dateToInput.replaceAll('-','.')}`);
const size = computed(() => {
  const bytes = props.estimate?.estimated_bytes ?? 0;
  return bytes < 1024*1024 ? `${(bytes/1024).toFixed(1)} KB` : `${(bytes/(1024*1024)).toFixed(1)} MB`;
});
const mediaOptions = [
  {key:"includeImages",label:"图片与表情",hint:"可用图片一起保存"},
  {key:"includeAudio",label:"语音",hint:"在离线网页里播放"},
  {key:"includeFiles",label:"文件",hint:"保留聊天中的文件"},
  {key:"includeVideo",label:"视频",hint:"原视频可能让导出包变大"},
] as const;
const mediaLabel = computed(() => mediaOptions.filter(item => store[item.key]).map(item => item.label).join("、") || "仅文字与消息卡片");
const leaveSearch = (): void => {
  if (globalThis.document.activeElement instanceof HTMLInputElement) globalThis.document.activeElement.blur();
  emit("search-focus",false);
};
const go = (step: number): void => { if(!busy.value){leaveSearch();store.step=step;} };
const keepFooterFocus = (event: PointerEvent): void => {
  if(event.isPrimary && event.button === 0 && globalThis.document.activeElement instanceof HTMLInputElement) event.preventDefault();
};
const toggle = (id: string): void => { if(!busy.value && !store.allConversations) store.toggleConversation(id); };
const selectListed = (): void => {
  if(busy.value)return;
  store.allConversations=false;
  for(const item of filteredConversations.value) if(!store.conversationIds.includes(item.id)) store.conversationIds.push(item.id);
};
const selectAll = (): void => { if(!busy.value){store.allConversations=true;store.conversationIds=[];} };
const clearSelection = (): void => { if(!busy.value){store.allConversations=false;store.conversationIds=[];} };
const dateInput = (date: Date): string => `${String(date.getFullYear())}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
const choosePeriod = (value: string): void => {
  if(busy.value)return;
  period.value=value;
  store.customDates=value!=="all";
  if(value==="all")return;
  const end=new Date(), start=new Date(end);
  if(value==="month")start.setDate(start.getDate()-29);
  if(value==="week")start.setDate(start.getDate()-6);
  if(value==="thismonth")start.setDate(1);
  store.dateFromInput=dateInput(start);store.dateToInput=dateInput(end);
};
const applyDates = (from: string,to: string): void => {
  if(busy.value)return;
  store.dateFromInput=from;store.dateToInput=to;store.customDates=true;period.value="custom";
};
const loadAttachment = (id: string): Promise<Blob> => props.accountId === null ? Promise.reject(new Error("尚未选择账号"))
  : props.attachmentLoader?.(id) ?? api.getAttachmentContent(id,props.accountId);
const loadThumbnail = (id: string): Promise<Blob> => props.accountId === null ? Promise.reject(new Error("尚未选择账号"))
  : props.thumbnailLoader?.(id) ?? api.getAttachmentThumbnail(id,props.accountId);
const submit = (): void => {
  if(store.step===3 && store.hasScope && store.validDates && props.estimate!==null && props.estimate.message_count>0
    && store.exportTitle.trim()!=="" && !busy.value) emit("start");
};
const {container,initial,onKeydown}=useModalFocus(toRef(selectedOpen),()=>{selectedOpen.value=false;});
void container;void initial;
watch(busy,value=>{if(value){dateOpen.value=false;selectedOpen.value=false;leaveSearch();}});
</script>

<template>
  <div class="export-builder">
    <form
      class="export-form html-export-wizard"
      :inert="selectedOpen || dateOpen"
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
          <span>3</span> 预览导出
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
          <h2>想保存哪些聊天？</h2><p>点选联系人或群聊，支持多选。</p>
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
              <div class="export-chat-toolbar">
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
                  <button
                    type="button"
                    class="export-inline-action"
                    :disabled="busy"
                    @click="store.allConversations ? clearSelection() : selectAll()"
                  >
                    {{ store.allConversations ? '取消全选' : '全部聊天' }}
                  </button>
                </div>
                <div
                  v-if="store.hasScope"
                  class="export-selection-strip"
                >
                  <strong>{{ selection }}</strong><button
                    v-if="!store.allConversations"
                    type="button"
                    class="text-action"
                    :disabled="busy"
                    @click="managerQuery = ''; selectedOpen = true"
                  >
                    查看已选
                  </button>
                </div>
                <div class="export-loaded-summary">
                  <small>{{ hasMore ? `当前列表 ${entries.length} 个 · 还可加载` : `${entries.length} 个聊天` }}</small><button
                    class="text-action"
                    type="button"
                    :disabled="busy || entries.length === 0"
                    @click="selectListed"
                  >
                    选中当前列表
                  </button>
                </div>
                <button
                  v-if="conversationError !== null"
                  type="button"
                  class="text-action"
                  @click="emit('retryConversations')"
                >
                  重试加载聊天
                </button>
              </div>
            </template>
          </SearchDirectory>
        </div>
        <footer @pointerdown="keepFooterFocus">
          <div class="export-footer-copy">
            <strong>{{ store.hasScope ? selection : '还没有选择聊天' }}</strong><small>选好后继续</small>
          </div><button
            type="button"
            class="button primary"
            :disabled="!store.hasScope || busy || accountId === null"
            @click="go(2)"
          >
            下一步：选时间
          </button>
        </footer>
      </section>
      <section
        v-else-if="store.step === 2"
        class="wizard-panel"
      >
        <header class="wizard-heading">
          <h2>保存哪段时间？</h2><p>先选时间，再决定带上哪些附件。</p>
        </header>
        <div class="wizard-body export-options-body">
          <div class="export-period-presets">
            <button
              v-for="item in [{key:'all',label:'全部时间'},{key:'month',label:'最近 30 天'},{key:'week',label:'最近 7 天'},{key:'thismonth',label:'本月'}]"
              :key="item.key"
              type="button"
              :disabled="busy"
              :aria-pressed="period === item.key"
              @click="choosePeriod(item.key)"
            >
              {{ item.label }}
            </button>
          </div>
          <button
            class="export-date-cell"
            type="button"
            :disabled="busy"
            @click="dateOpen = true"
          >
            <span>自选日期区间<small>{{ store.customDates ? dateLabel : '在日历上点开始日和结束日' }}</small></span><span aria-hidden="true">›</span>
          </button>
          <p
            v-if="!store.validDates"
            role="alert"
            class="notice-warn"
          >
            请填写有效日期，结束日期不能早于开始日期。
          </p>
          <p class="export-note">
            包含开始日和结束日 · 按当前浏览器的本地时区
          </p>
          <div class="export-media-heading">
            <h3>附件一起带上</h3><small>文字始终保留</small>
          </div>
          <div class="export-media-list">
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
              <span>{{ item.label }}<small>{{ item.hint }}</small></span><span
                class="export-switch"
                aria-hidden="true"
              />
            </button>
          </div>
          <details class="export-help">
            <summary>导出会包含什么？</summary><p>仅保存所选聊天和时间内的消息、可用附件与标准数据文件。不能取得的附件在导出报告中标明，相关消息保留。</p>
          </details>
        </div>
        <footer>
          <button
            class="button secondary"
            type="button"
            :disabled="busy"
            @click="go(1)"
          >
            上一步
          </button><div class="export-footer-copy">
            <strong>{{ selection }}</strong>
          </div><button
            class="button primary"
            type="button"
            :disabled="!store.hasScope || !store.validDates || estimating || busy || accountId === null"
            @click="emit('estimate')"
          >
            {{ estimating ? '正在核对…' : '下一步：看效果' }}
          </button>
        </footer>
      </section>
      <section
        v-else
        class="wizard-panel"
      >
        <header class="wizard-heading">
          <h2>看看效果，准备保存</h2><p>保存成网页包，解压后就能打开。</p>
        </header>
        <div class="wizard-body export-preview-body">
          <div class="export-scope-summary">
            <div>
              <small>聊天</small><strong>{{ selection }}</strong><button
                type="button"
                class="text-action"
                :disabled="busy"
                @click="go(1)"
              >
                修改聊天
              </button>
            </div><div>
              <small>时间</small><strong>{{ dateLabel }}</strong><button
                type="button"
                class="text-action"
                :disabled="busy"
                @click="go(2)"
              >
                修改时间
              </button>
            </div><div><small>附件</small><strong>{{ mediaLabel }}</strong></div>
          </div>
          <div
            v-if="estimate !== null"
            class="wizard-estimate"
          >
            <strong>{{ estimate.message_count.toLocaleString('zh-CN') }} 条消息</strong><small>消息数据约 {{ size }} · 附件另计</small>
          </div>
          <p
            v-if="estimate?.message_count === 0"
            class="notice-warn"
          >
            这段时间没有匹配的消息，请返回修改范围。
          </p>
          <div
            v-if="preview.length > 0"
            class="wizard-reader"
          >
            <h3>范围内的 {{ preview.length }} 条预览</h3><ExportDocument
              :messages="preview"
              :show-sender-names="true"
              :load-attachment="loadAttachment"
              :load-thumbnail="loadThumbnail"
            />
          </div>
          <p
            v-else
            class="export-note"
          >
            {{ previewError ?? '当前没有可展示的预览。' }}
          </p>
          <p
            v-if="estimate !== null && estimate.media_missing > 0"
            class="notice-warn"
          >
            有 {{ estimate.media_missing }} 个附件暂不可用。仍会保存消息，完成后可查看报告。
          </p>
          <label class="export-name-field"><span>记录名称</span><input
            v-model="store.exportTitle"
            type="text"
            maxlength="80"
            aria-label="导出名称"
            data-feedback-mask="conversation-name"
            :disabled="busy"
          ></label>
          <p
            v-if="store.exportTitle.trim() === ''"
            role="alert"
            class="notice-warn"
          >
            请填写导出名称，方便以后找到它。
          </p>
          <details class="export-help">
            <summary>保存后怎么打开？</summary><p>下载 ZIP 包 → 解压 → 打开“打开聊天记录.html”。移动或分享时，网页和附件文件夹一起带上。</p>
          </details>
          <details
            v-if="estimate !== null"
            class="export-help"
          >
            <summary>查看附件准备情况</summary><p>附件：{{ estimate.media_ready }} 已缓存 / {{ estimate.media_pending }} 待处理 / {{ estimate.media_missing }} 暂不可用；最终结果以导出报告为准。</p>
          </details>
        </div>
        <footer>
          <button
            class="button secondary"
            type="button"
            :disabled="busy"
            @click="go(2)"
          >
            上一步
          </button><div class="export-footer-copy">
            <small>离线网页 · ZIP 包</small>
          </div><button
            class="button primary"
            type="submit"
            data-feedback-action="开始导出"
            :disabled="busy || estimate === null || estimate.message_count === 0 || !store.hasScope || !store.validDates || store.exportTitle.trim() === '' || accountId === null"
          >
            {{ creating ? '正在创建…' : running ? '正在导出…' : '开始导出' }}
          </button>
        </footer>
      </section>
    </form>
    <ExportDateRangeSheet
      :open="dateOpen"
      :from="store.dateFromInput"
      :to="store.dateToInput"
      @close="dateOpen = false"
      @apply="applyDates"
    />
    <div
      v-if="selectedOpen"
      class="export-sheet-backdrop"
      @click.self="selectedOpen = false"
    >
      <section
        ref="container"
        class="export-sheet export-selection-sheet"
        role="dialog"
        aria-modal="true"
        aria-label="已选聊天"
        @keydown="onKeydown"
      >
        <header class="export-sheet-head">
          <h2>已选 {{ store.conversationIds.length }} 个聊天</h2><button
            ref="initial"
            type="button"
            class="export-icon-button"
            aria-label="关闭已选聊天"
            @click="selectedOpen = false"
          >
            ×
          </button>
        </header>
        <SearchDirectory
          title="已选聊天名单"
          placeholder="在已选聊天中查找"
          selection-layout="inline"
          :entries="managerEntries"
          :query="managerQuery"
          :selected-ids="store.conversationIds"
          :loading="false"
          :fetching-more="false"
          :has-more="false"
          :disabled="busy"
          @toggle="toggle"
          @update:query="managerQuery = $event"
          @visible-change="managerVisible = $event"
        />
        <footer class="export-sheet-footer">
          <button
            type="button"
            class="button secondary"
            :disabled="busy"
            @click="clearSelection"
          >
            清空已选
          </button><button
            type="button"
            class="button primary"
            @click="selectedOpen = false"
          >
            完成
          </button>
        </footer>
      </section>
    </div>
  </div>
</template>

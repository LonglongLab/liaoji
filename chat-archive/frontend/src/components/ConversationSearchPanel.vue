<script setup lang="ts">
import { computed, ref, toRef, watch } from "vue";

import type { AttachmentSummary, SearchHit, SearchIndexStatus } from "@/api/archiveApi";
import { useModalFocus } from "@/composables/useModalFocus";
import { useDebouncedTrimmed } from "@/composables/useDebouncedValue";
import MediaViewer from "@/components/MediaViewer.vue";
import SearchDateCalendar from "@/components/SearchDateCalendar.vue";
import SearchHitCard from "@/components/SearchHitCard.vue";
import SearchHitRow from "@/components/SearchHitRow.vue";
import SearchMediaGrid from "@/components/SearchMediaGrid.vue";
import {
  SEARCH_CATEGORIES,
  categoryLabel,
  hitSenderName,
  type SearchCategory,
} from "@/lib/conversationSearchTabs";
import { isSearchHitPreviewable, searchHitAttachment } from "@/lib/searchHitMedia";

const props = withDefaults(defineProps<{
  open: boolean;
  displayName: string;
  hits: SearchHit[];
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  errorText: string | null;
  indexStatus: SearchIndexStatus;
  category: SearchCategory | null;
  query: string;
  /**
   * 这一次查找一共命中多少条——**整个结果集**，不是已加载的那几条。
   * `null` = 这一端答不出总数（v7 以下的远端），那时一个字也不说，
   * 不拿"已加载 100 条"冒充"一共 100 条"。
   */
  totalCount?: number | null;
  date: string;
  /**
   * 这一端知不知道"哪几天有消息"。false 时不给日期入口：一个点不亮任何日子的
   * 日历，读起来是"这些天都没有消息"，而真相是这一端根本没被问过这件事。
   */
  dateAvailable?: boolean;
  datePanelOpen?: boolean;
  messageDays?: string[];
  messageDaysLoading?: boolean;
  activeMediaId?: string | null;
  loadAttachment?: (attachmentId: string) => Promise<Blob>;
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
}>(), {
  totalCount: null,
  dateAvailable: true,
  activeMediaId: null,
  datePanelOpen: false,
  messageDays: () => [],
  messageDaysLoading: false,
  loadAttachment: () => Promise.reject(new Error("附件加载器不可用")),
  loadThumbnail: undefined,
});

const emit = defineEmits<{
  close: [];
  submit: [query: string];
  "update:category": [category: SearchCategory | null];
  "open-date-panel": [];
  "close-date-panel": [];
  "apply-date": [date: string];
  "view-month": [year: number, month: number];
  "open-hit": [messageId: string];
  "open-media": [hit: SearchHit];
  "open-media-layer": [attachmentId: string];
  "close-media-layer": [];
  "load-more": [];
  retry: [];
}>();

const draft = ref(props.query);
const debouncedDraft = useDebouncedTrimmed(draft);
const lastRequestedQuery = ref(props.query);

const requestQuery = (value: string): void => {
  const normalized = value.trim();
  if (normalized === lastRequestedQuery.value) return;
  lastRequestedQuery.value = normalized;
  emit("submit", normalized);
};

watch(
  () => props.query,
  (value) => {
    draft.value = value;
    lastRequestedQuery.value = value;
  },
);
watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) {
      draft.value = props.query;
      lastRequestedQuery.value = props.query;
    }
  },
);
watch(debouncedDraft, (value) => {
  if (props.open) requestQuery(value);
});

const { container, initial, onKeydown } = useModalFocus(
  toRef(props, "open"),
  () => emit("close"),
);
// Only bound via template `ref="container"` / `ref="initial"`; vue-tsc's
// noUnusedLocals check can't see that as a read, so mark them intentional.
void container;
void initial;

const {
  container: dateContainer,
  onKeydown: onDateKeydown,
} = useModalFocus(
  toRef(props, "datePanelOpen"),
  () => emit("close-date-panel"),
);
void dateContainer;

/** 分类是可以摘掉的条件：再点一次同一个分类就取消，和微信一样没有「全部」。 */
const toggleCategory = (value: SearchCategory): void => {
  emit("update:category", props.category === value ? null : value);
};

const dateChipLabel = computed(() => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(props.date);
  if (match === null) return "";
  return `${String(Number(match[2]))}月${String(Number(match[3]))}日`;
});

// 确定只报日期：上层用 replace 换掉带 datePanel 的地址，日历跟着关掉，
// 不能再 back 一步，那会把刚选的日期一起退掉。
const applyDate = (date: string): void => {
  emit("apply-date", date);
};

const showsFiles = computed(() => props.category === "file");
const showsMediaGrid = computed(() => props.category === "media");
const showsHitList = computed(() => !showsFiles.value && !showsMediaGrid.value);
const isEmpty = computed(() => props.hits.length === 0);

type ViewerItem = {
  attachment: AttachmentSummary;
  senderName: string;
  createdAt: number;
};

const mediaItems = computed<ViewerItem[]>(() =>
  props.hits.flatMap((hit) => {
    const attachment = searchHitAttachment(hit);
    return attachment !== null && isSearchHitPreviewable(hit)
      ? [
          {
            attachment,
            senderName: hitSenderName(hit),
            createdAt: hit.created_at,
          },
        ]
      : [];
  }),
);
const mediaViewerOpen = ref(false);
const mediaViewerIndex = ref(0);

const showMedia = (attachmentId: string): boolean => {
  const index = mediaItems.value.findIndex((item) => item.attachment.id === attachmentId);
  if (index < 0) return false;
  mediaViewerIndex.value = index;
  mediaViewerOpen.value = true;
  return true;
};

const openMedia = (hit: SearchHit): void => {
  const attachment = searchHitAttachment(hit);
  if (attachment === null || !showMedia(attachment.id)) return;
  emit("open-media", hit);
  emit("open-media-layer", attachment.id);
};

const closeMedia = (): void => {
  mediaViewerOpen.value = false;
  emit("close-media-layer");
};

watch(
  [() => props.activeMediaId ?? null, mediaItems],
  ([attachmentId]) => {
    if (attachmentId === null) {
      mediaViewerOpen.value = false;
      return;
    }
    showMedia(attachmentId);
  },
  { immediate: true },
);

const mediaScopeNotice = computed(() =>
  props.hasMore ? "当前仅浏览已加载搜索结果中的媒体；加载更多结果后可扩大范围。" : null,
);

const loadForViewer = async (attachmentId: string): Promise<Blob> => {
  return props.loadAttachment(attachmentId);
};
</script>

<template>
  <div
    v-if="open"
    class="panel-backdrop"
    @click.self="emit('close')"
  >
    <div
      ref="container"
      class="panel-dialog"
      role="dialog"
      aria-modal="true"
      aria-label="查找聊天记录"
      @keydown="onKeydown"
    >
      <header class="panel-head">
        <strong
          data-feedback-mask="conversation-name"
          class="panel-title"
        >「{{ displayName }}」的聊天记录</strong>
        <button
          class="text-action"
          type="button"
          aria-label="取消查找"
          @click="emit('close')"
        >
          取消
        </button>
      </header>

      <p
        v-if="totalCount !== null"
        data-feedback-mask="search-query"
        class="panel-count"
        role="status"
      >
        共 {{ totalCount.toLocaleString("zh-CN") }} 条{{
          query === "" ? "聊天记录" : `与「${query}」的聊天记录`
        }}
      </p>

      <!-- 已选的分类和日期以标签的形式住在搜索框里，可以逐个摘掉，并且和关键词叠加。 -->
      <form
        class="panel-search"
        @submit.prevent="requestQuery(draft)"
      >
        <span
          class="panel-search-icon"
          aria-hidden="true"
        >⌕</span>
        <span
          v-if="category !== null"
          class="panel-filter-chip"
        >
          {{ categoryLabel(category) }}
          <button
            type="button"
            :aria-label="`移除${categoryLabel(category)}筛选`"
            @click="emit('update:category', null)"
          >
            ×
          </button>
        </span>
        <span
          v-if="date !== ''"
          class="panel-filter-chip"
        >
          {{ dateChipLabel }}
          <button
            type="button"
            aria-label="移除日期筛选"
            @click="emit('apply-date', '')"
          >
            ×
          </button>
        </span>
        <input
          ref="initial"
          v-model="draft"
          type="search"
          aria-label="在这个会话里查找"
          placeholder="搜索"
        >
      </form>

      <div
        class="panel-tabs"
        role="tablist"
        aria-label="按类型筛选"
      >
        <button
          v-for="item in SEARCH_CATEGORIES"
          :key="item.key"
          :data-tab="item.key"
          class="panel-tab"
          type="button"
          role="tab"
          :aria-label="item.label"
          :aria-selected="category === item.key"
          :class="{ 'is-active': category === item.key }"
          @click="toggleCategory(item.key)"
        >
          {{ item.label }}
        </button>
        <button
          v-if="dateAvailable"
          data-tab="date"
          class="panel-tab"
          type="button"
          role="tab"
          aria-label="日期"
          :aria-selected="datePanelOpen || date !== ''"
          :class="{ 'is-active': datePanelOpen || date !== '' }"
          @click="emit('open-date-panel')"
        >
          日期
        </button>
      </div>

      <div class="panel-results">
        <div
          v-if="errorText !== null"
          class="notice-warn"
          role="alert"
        >
          <strong>查找失败</strong>
          <p>{{ errorText }}</p>
          <button
            class="text-action"
            type="button"
            @click="emit('retry')"
          >
            重试
          </button>
        </div>
        <p
          v-if="loading"
          class="panel-hint"
        >
          正在查找……
        </p>
        <div
          v-else-if="indexStatus === 'not_initialized'"
          class="notice-warn"
          role="status"
        >
          <strong>还没有建立全文检索</strong>
          <p>这个归档默认不建索引。到搜索页可以开启，开启后这里也能搜。</p>
        </div>
        <div
          v-else-if="indexStatus === 'unavailable'"
          class="notice-warn"
          role="status"
        >
          <strong>搜索索引结构不完整</strong>
          <p>索引表缺列，无法用它作答。重建一次索引即可恢复。</p>
        </div>
        <p
          v-else-if="isEmpty"
          class="panel-empty"
          role="status"
        >
          无内容
        </p>
        <template v-else>
          <p
            v-if="indexStatus === 'building'"
            class="panel-hint"
            role="status"
          >
            搜索索引仍在构建，当前结果可能不完整。
          </p>
          <SearchMediaGrid
            v-if="showsMediaGrid"
            :hits="hits"
            :load-attachment="loadAttachment"
            :load-thumbnail="loadThumbnail"
            @open-media="openMedia"
          />
          <template v-else-if="showsFiles">
            <SearchHitRow
              v-for="item in hits"
              :key="item.message_id"
              :hit="item"
              :query="query"
              :load-attachment="loadAttachment"
              :load-thumbnail="loadThumbnail"
              @open="emit('open-hit', $event.message_id)"
            />
          </template>
          <template v-else-if="showsHitList">
            <SearchHitCard
              v-for="item in hits"
              :key="item.message_id"
              class="panel-hit"
              :hit="item"
              :conversation-name="displayName"
              :compact="true"
              :query="query"
              :load-attachment="loadAttachment"
              :load-thumbnail="loadThumbnail"
              @open="emit('open-hit', $event.message_id)"
              @open-media="openMedia"
            />
          </template>
          <button
            v-if="hasMore"
            class="button quiet"
            type="button"
            :disabled="fetchingMore"
            @click="emit('load-more')"
          >
            {{ fetchingMore ? "正在加载……" : "加载更多结果" }}
          </button>
        </template>
      </div>
      <MediaViewer
        :open="mediaViewerOpen"
        :items="mediaItems"
        :initial-index="mediaViewerIndex"
        :load-attachment="loadForViewer"
        :load-thumbnail="loadThumbnail"
        :scope-notice="mediaScopeNotice"
        @close="closeMedia"
      />
      <div
        v-if="datePanelOpen"
        class="panel-date-backdrop"
        @click.self="emit('close-date-panel')"
      >
        <div
          ref="dateContainer"
          class="panel-date-anchor"
          @keydown.stop="onDateKeydown"
        >
          <SearchDateCalendar
            :open="datePanelOpen"
            :selected="date"
            :available-days="messageDays"
            :loading="messageDaysLoading"
            @close="emit('close-date-panel')"
            @apply="applyDate"
            @view-month="(year, month) => emit('view-month', year, month)"
          />
        </div>
      </div>
    </div>
  </div>
</template>

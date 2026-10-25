<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import type {
  AttachmentSummary,
  SearchHit,
  SearchIndexState,
  SearchIndexStatus,
  SearchSort,
} from "@/api/archiveApi";
import MediaViewer from "@/components/MediaViewer.vue";
import SearchHitRow from "@/components/SearchHitRow.vue";
import SearchSortMenu from "@/components/SearchSortMenu.vue";
import { hitSenderName } from "@/lib/conversationSearchTabs";
import { formatBytes } from "@/utils/format";
import { groupHitsByRecency } from "@/lib/searchHitGrouping";
import { isSearchHitPreviewable, searchHitAttachment } from "@/lib/searchHitMedia";

/**
 * 结果区——微信文件管理右边那一栏（PSU-2.66）。
 *
 * 标题就是当前范围，放大镜展开的关键词叠加在范围之上，结果按 今天／本周／本月／
 * 年月 分组（用的是媒体宫格早就有的那一份 `groupHitsByRecency`，不另分一套）。
 *
 * 本机与远端挂的是同一个组件，差异只经 props 传进来。
 */
const props = defineProps<{
  title: string;
  hits: SearchHit[];
  conversationNames: ReadonlyMap<string, string>;
  query: string;
  /** 整个结果集的条数；`null` = 这一端答不出，那就一个字也不说。 */
  totalCount: number | null;
  sort: SearchSort;
  sortModes: readonly SearchSort[];
  loading: boolean;
  /**
   * 换条件之后正在重取（PSU-2.75）。与 `loading` 不是一回事：`loading` 是
   * 「一条结果都还没有过」，而这个是「上一屏还在，新的在路上」——那时候整屏
   * 换成一行「正在搜索……」，读者刚看到的东西连同滚动位置一起消失。
   */
  fetching?: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  errorText: string | null;
  /** 续页失败保留当前结果，只在末尾重试原游标。 */
  nextPageError?: boolean;
  indexStatus: SearchIndexStatus;
  /**
   * 索引没建时用来如实说明「这一步要做多少事」（PSU-2.88）。`null` = 还没问到，
   * 那就只说没建，不给任何数字——预估宁可缺席也不能是编的。
   */
  indexState?: SearchIndexState | null;
  /** 正在建索引的请求在路上，按钮要禁用。 */
  initializingIndex?: boolean;
  /** 开始建索引失败时的原话；不显示的话失败与成功在界面上不可区分。 */
  initializeIndexError?: string | null;
  loadAttachment?: ((attachmentId: string) => Promise<Blob>) | undefined;
  loadThumbnail?: ((attachmentId: string) => Promise<Blob>) | undefined;
  /** 远端特有的两样，本机不传就不出现。 */
  elapsedText?: string | null;
  scopeNotice?: string | null;
  /**
   * 灯箱归路由管（PSU-2.120）：开一层压一条历史，手机上的系统「返回」才只关灯箱。
   * 不接的话那一下返回弹掉的是整个搜索页——读者点了一张图，回来发现搜索条件、
   * 滚动位置全没了。
   */
  activeMediaId?: string | null;
}>();

const emit = defineEmits<{
  "update:query": [query: string];
  "update:sort": [sort: SearchSort];
  open: [hit: SearchHit];
  "load-more": [];
  retry: [];
  "initialize-index": [];
  "open-media-layer": [attachmentId: string];
  "close-media-layer": [];
}>();

const searchOpen = ref(props.query !== "");
const draft = ref(props.query);
watch(
  () => props.query,
  (value) => {
    draft.value = value;
    if (value !== "") searchOpen.value = true;
  },
);
watch(draft, (value) => emit("update:query", value));

const closeSearch = (): void => {
  searchOpen.value = false;
  draft.value = "";
};

const groups = computed(() => groupHitsByRecency(props.hits));

/**
 * 行上那一句「发送人 ｜ 会话」里的会话名。
 *
 * 名录还没翻到、或这一端根本没送过名字的，**退回不透明标识**——留空会让人以为
 * 这条命中不属于任何会话，而它其实只是名字没到。
 */
const conversationName = (hit: SearchHit): string =>
  props.conversationNames.get(hit.conversation_id) ?? hit.conversation_id;
const isEmpty = computed(() => props.hits.length === 0);

const resultsBody = ref<HTMLElement | null>(null);
const pageEnd = ref<HTMLElement | null>(null);
let pageObserver: IntersectionObserver | null = null;
let pageRequested = false;

const requestNextPage = (retry = false): void => {
  if (pageRequested || !props.hasMore || props.loading || props.fetchingMore ||
      props.fetching || (props.errorText !== null && !retry) ||
      !["ready", "building"].includes(props.indexStatus)) return;
  // 先锁定，再通知父级：同一帧的多个观察回调不能重复请求游标。
  pageRequested = true;
  emit("load-more");
};

watch(
  [resultsBody, pageEnd, () => props.hits, () => props.hasMore,
    () => props.loading, () => props.fetching, () => props.fetchingMore,
    () => props.errorText, () => props.indexStatus],
  () => {
    pageObserver?.disconnect();
    pageObserver = null;
    if (!props.fetchingMore && !props.fetching) pageRequested = false;
    const root = resultsBody.value;
    const end = pageEnd.value;
    if (root === null || end === null || !props.hasMore || props.loading ||
        props.fetching || props.fetchingMore || props.errorText !== null ||
        typeof IntersectionObserver === "undefined") return;
    // 每页落地后重新观察：底部仍在视口内的短页也会继续补齐。
    pageObserver = new IntersectionObserver((entries) => {
      if (entries.some(entry => entry.isIntersecting)) requestNextPage();
    }, { root, rootMargin: "0px 0px 160px 0px" });
    pageObserver.observe(end);
  },
  { flush: "post" },
);
onBeforeUnmount(() => pageObserver?.disconnect());

const countLine = computed(() => {
  if (props.totalCount === null) return null;
  const total = props.totalCount.toLocaleString("zh-CN");
  return props.query === ""
    ? `共 ${total} 条记录`
    : `共 ${total} 条与「${props.query}」相关的记录`;
});

type ViewerItem = { attachment: AttachmentSummary; senderName: string; createdAt: number };

const mediaItems = computed<ViewerItem[]>(() =>
  props.hits.flatMap((hit) => {
    const attachment = searchHitAttachment(hit);
    return attachment !== null && isSearchHitPreviewable(hit)
      ? [{ attachment, senderName: hitSenderName(hit), createdAt: hit.created_at }]
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
  if (attachment === null) return;
  if (showMedia(attachment.id)) emit("open-media-layer", attachment.id);
};

const closeMedia = (): void => {
  mediaViewerOpen.value = false;
  emit("close-media-layer");
};

// 路由不再点名这一层就收掉：系统「返回」与换了一批结果走的都是这一步。
watch(
  [() => props.activeMediaId ?? null, mediaItems],
  ([attachmentId]) => {
    if (attachmentId === null) {
      mediaViewerOpen.value = false;
      return;
    }
    if (!showMedia(attachmentId)) mediaViewerOpen.value = false;
  },
  { immediate: true },
);

const loadForViewer = async (attachmentId: string): Promise<Blob> => {
  if (props.loadAttachment === undefined) throw new Error("附件加载器不可用");
  return props.loadAttachment(attachmentId);
};

const mediaScopeNotice = computed(() =>
  props.hasMore ? "当前仅浏览已加载结果中的媒体；加载更多后可扩大范围。" : null,
);
</script>

<template>
  <section
    class="search-results-panel"
    aria-label="搜索结果"
  >
    <header class="search-results-head">
      <h1
        data-feedback-mask="conversation-name"
        class="search-results-title"
      >
        {{ title }}
      </h1>
      <!-- 远端把它那个 ⓘ 按钮挂进标题行。**只放得下按钮**：整段说明是块级的，
           挤进这一行 flex 会把 `flex:1; ellipsis` 的标题压成一个省略号。 -->
      <slot name="title-action" />
      <template v-if="searchOpen">
        <input
          v-model="draft"
          class="search-results-input"
          type="search"
          aria-label="搜索消息正文"
          placeholder="搜索当前范围的消息正文"
        >
        <button
          class="text-action"
          type="button"
          @click="closeSearch"
        >
          取消
        </button>
      </template>
      <button
        v-else
        data-feedback-action="搜索"
        class="search-results-magnifier"
        type="button"
        aria-label="搜索消息"
        @click="searchOpen = true"
      >
        <svg
          width="17"
          height="17"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="1.8"
          stroke-linecap="round"
          aria-hidden="true"
        >
          <circle
            cx="11"
            cy="11"
            r="6.5"
          />
          <path d="M20 20l-4.3-4.3" />
        </svg>
        搜索消息
      </button>
    </header>

    <!-- 整段说明另起一块，落在标题行下面——它原本就是这么排的。 -->
    <slot name="note" />

    <slot name="filters" />

    <div class="search-results-toolbar">
      <SearchSortMenu
        :sort="sort"
        :modes="sortModes"
        @update:sort="emit('update:sort', $event)"
      />
    </div>

    <div
      ref="resultsBody"
      class="search-results-body"
    >
      <!-- 上一屏还在时不整屏换字：一条细进度条说「在取了」，结果与滚动位置都留着。 -->
      <div
        v-if="fetching === true && !loading && !fetchingMore"
        class="search-results-progress"
        role="status"
        aria-label="正在搜索"
      />
      <div
        v-if="errorText !== null && !nextPageError"
        class="notice-warn"
        role="alert"
      >
        <strong>搜索失败</strong>
        <p>{{ errorText }}</p>
        <button
          data-feedback-action="重新搜索"
          class="text-action"
          type="button"
          @click="emit('retry')"
        >
          重新搜索
        </button>
      </div>
      <div
        v-else-if="loading"
        class="search-results-skeleton"
        role="status"
        aria-label="正在搜索"
      >
        <div
          v-for="row in 3"
          :key="row"
          class="search-results-skeleton-row"
        />
      </div>
      <div
        v-else-if="indexStatus === 'not_initialized'"
        class="notice-warn"
        role="status"
      >
        <strong>这个归档还没有建立全文检索</strong>
        <p>
          备份和同步都不需要索引，所以默认不建——它会长期占用磁盘。
          要在本地搜索，先建一次索引；建好之后新导入的消息会自动跟上。
        </p>
        <p
          v-if="indexState != null"
          class="search-index-estimate"
        >
          待索引 {{ indexState.pending_message_count.toLocaleString("zh-CN") }} 条消息，
          建完约占 {{ formatBytes(indexState.estimated_index_bytes) }}。
        </p>
        <button
          data-feedback-action="建立索引"
          type="button"
          class="search-index-action"
          :disabled="initializingIndex === true"
          @click="emit('initialize-index')"
        >
          {{ initializingIndex === true ? "正在开始…" : "建立全文检索索引" }}
        </button>
        <p
          v-if="initializeIndexError != null"
          class="search-index-error"
          role="alert"
        >
          {{ initializeIndexError }}
        </p>
      </div>
      <div
        v-else-if="indexStatus === 'unavailable'"
        class="notice-warn"
        role="status"
      >
        <strong>搜索索引结构不完整</strong>
        <p>索引表缺列，无法用它作答。重建一次索引即可恢复。</p>
      </div>
      <template v-else>
        <p
          v-if="indexStatus === 'building'"
          class="search-hint"
          role="status"
        >
          搜索索引仍在构建，当前结果可能不完整。
        </p>
        <p
          v-if="scopeNotice != null"
          class="search-hint"
        >
          {{ scopeNotice }}
        </p>
        <p
          v-if="isEmpty"
          data-feedback-mask="search-query"
          class="search-hint"
          role="status"
        >
          {{ query === "" ? "这个范围里没有记录。" : `没有找到与「${query}」相关的记录。` }}
        </p>
        <template v-else>
          <!-- 空结果时一个字也不说总数：现有 e2e 钉着「不拿假总数占位」。 -->
          <p
            v-if="countLine !== null"
            class="search-results-count"
            role="status"
          >
            {{ countLine }}<template v-if="elapsedText != null">
              · 本次用时 {{ elapsedText }}
            </template>
          </p>
          <section
            v-for="group in groups"
            :key="group.key"
            class="search-results-group"
          >
            <h2>{{ group.title }}</h2>
            <SearchHitRow
              v-for="hit in group.hits"
              :key="hit.message_id"
              :hit="hit"
              :query="query"
              :conversation-name="conversationName(hit)"
              :load-attachment="loadAttachment"
              :load-thumbnail="loadThumbnail"
              @open="emit('open', $event)"
              @open-media="openMedia"
            />
          </section>
          <div
            ref="pageEnd"
            class="search-results-page-end"
            role="status"
            aria-live="polite"
            :aria-busy="fetchingMore"
          >
            <template v-if="nextPageError && !fetchingMore">
              <span>加载失败</span>
              <button
                data-feedback-action="重试加载结果"
                class="text-action"
                type="button"
                :title="errorText ?? undefined"
                @click="requestNextPage(true)"
              >
                重试
              </button>
            </template>
            <span v-else-if="fetchingMore">正在加载…</span>
            <span v-else-if="!hasMore">已全部加载</span>
          </div>
        </template>
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
  </section>
</template>

<script setup lang="ts">
import { computed, ref, toRef, watch } from "vue";

import SearchDirectory from "@/components/SearchDirectory.vue";
import { useModalFocus } from "@/composables/useModalFocus";
import {
  DIMENSION_LABELS,
  EMPTY_FILTERS,
  FILE_CLASS_OPTIONS,
  TYPE_OPTIONS,
  fileClassesAllowed,
  filterCount,
  toggleFilterValue,
  type FilterDimension,
  type SearchFilters,
} from "@/lib/searchFilters";
import type { DirectoryEntry, SearchCapabilities } from "@/lib/searchScopes";

/**
 * H5 的筛选抽屉（PSU-2.75）。
 *
 * 手机上原来是把桌面三栏里的名录直接叠在结果上面：名录占半屏，结果占半屏，
 * 而结果才是这一页的主体。现在名录住在这里，结果拿回整屏。
 *
 * **抽屉里改的是草稿，关上才提交。** 真实归档 70 万行，每点一次就查一次是白烧；
 * 想就地生效的那条路在筛选条上——chip 的 × 是立刻的。
 */
const props = defineProps<{
  open: boolean;
  filters: SearchFilters;
  capabilities: SearchCapabilities;
  segment: FilterDimension;
  entries: DirectoryEntry[];
  /** 名录当前生效的过滤词——透传给名录，重开时框里的词才等于列表的筛。 */
  query?: string | undefined;
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
}>();

const emit = defineEmits<{
  "update:segment": [dimension: FilterDimension];
  "update:query": [query: string];
  "load-more": [];
  /** 抽屉里那份名录的视口行，原样透传（PSU-2.128）。 */
  "visible-change": [ids: readonly string[] | null];
  apply: [filters: SearchFilters];
  close: [];
}>();

/** 打开时装载一次；之后抽屉里的点选只动这一份，不惊动查询。 */
const draft = ref<SearchFilters>(props.filters);
watch(
  // 关掉再开也要重新装载：上一次没点「查看结果」的草稿不作数，不能留到这次。
  [() => props.open, () => props.filters],
  ([, filters]) => {
    draft.value = filters;
  },
);

// 声明了 aria-modal 就要有配套的键盘行为：Esc 关闭、Tab 圈在抽屉里、焦点
// 进出有着落——与别的对话框同一个 useModalFocus，不另写一套。
const { container, initial, onKeydown } = useModalFocus(
  toRef(props, "open"),
  () => emit("close"),
);
void container;
void initial;

const segments = computed<FilterDimension[]>(() => {
  // 与范围栏同一条：类型恰好是「文件」时文件类别那一段才存在（后端契约）。
  const all: FilterDimension[] = ["types"];
  if (props.capabilities.fileClassScope && fileClassesAllowed(draft.value.types)) {
    all.push("fileClasses");
  }
  all.push("conversationIds");
  if (props.capabilities.senderScope) all.push("senderIds");
  return all;
});

const options = computed(() =>
  props.segment === "types" ? TYPE_OPTIONS : FILE_CLASS_OPTIONS,
);
const isDirectory = computed(
  () => props.segment === "conversationIds" || props.segment === "senderIds",
);
const picked = computed<string[]>(() => [...draft.value[props.segment]]);
const atLimit = computed(
  () => picked.value.length >= props.capabilities.maxSelections,
);
const limitNotice = computed(() =>
  atLimit.value
    ? `最多同时选 ${String(props.capabilities.maxSelections)} 个，先摘掉一个再选。`
    : null,
);

const toggle = (dimension: FilterDimension, value: string): void => {
  draft.value = toggleFilterValue(
    draft.value,
    dimension,
    value,
    props.capabilities.maxSelections,
  );
  // 文件类别那一段站不住脚时（摘掉了「文件」，或又叠了别的类型），跳回类型段。
  if (!segments.value.includes(props.segment)) emit("update:segment", "types");
};

/** 「查看结果（3）」——按钮上写着提交的是几个条件，草稿与已生效才分得清。 */
const applyLabel = computed(() => {
  const count = filterCount(draft.value);
  return count === 0 ? "查看结果" : `查看结果（${String(count)}）`;
});

const submit = (): void => {
  emit("apply", draft.value);
  emit("close");
};
</script>

<template>
  <div
    v-if="open"
    class="search-filter-sheet-backdrop"
    @click.self="emit('close')"
  >
    <section
      ref="container"
      class="search-filter-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="筛选"
      @keydown="onKeydown"
    >
      <header class="search-filter-sheet-head">
        <h2>筛选</h2>
        <button
          ref="initial"
          class="text-action"
          type="button"
          @click="emit('close')"
        >
          关闭
        </button>
      </header>

      <nav
        class="search-filter-segments"
        aria-label="筛选维度"
      >
        <button
          v-for="dimension in segments"
          :key="dimension"
          class="search-filter-segment"
          :class="{ 'is-active': dimension === segment }"
          type="button"
          :aria-pressed="dimension === segment"
          @click="emit('update:segment', dimension)"
        >
          {{ DIMENSION_LABELS[dimension] }}
          <span
            v-if="draft[dimension].length > 0"
            class="search-filter-segment-count"
          >{{ draft[dimension].length }}</span>
        </button>
      </nav>

      <div class="search-filter-sheet-body">
        <SearchDirectory
          v-if="isDirectory"
          :key="segment"
          :title="segment === 'conversationIds' ? '会话名录' : '发送人名录'"
          :placeholder="segment === 'conversationIds' ? '搜索会话' : '搜索发送人'"
          :query="query"
          :entries="entries"
          :selected-ids="picked"
          :loading="loading"
          :fetching-more="fetchingMore"
          :has-more="hasMore"
          :limit-notice="limitNotice"
          @toggle="toggle(segment, $event)"
          @update:query="emit('update:query', $event)"
          @load-more="emit('load-more')"
          @visible-change="emit('visible-change', $event)"
        />
        <div
          v-else
          class="search-filter-grid"
        >
          <button
            v-for="item in options"
            :key="item.value"
            class="search-filter-option"
            :class="{ 'is-active': picked.includes(item.value) }"
            type="button"
            role="checkbox"
            :aria-checked="picked.includes(item.value)"
            @click="toggle(segment, item.value)"
          >
            <svg
              width="18"
              height="18"
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              stroke-width="1.7"
              stroke-linecap="round"
              stroke-linejoin="round"
              aria-hidden="true"
            >
              <path :d="item.path" />
            </svg>
            <span>{{ item.label }}</span>
          </button>
        </div>
      </div>

      <footer class="search-filter-sheet-foot">
        <button
          class="button quiet"
          type="button"
          @click="draft = EMPTY_FILTERS"
        >
          重置
        </button>
        <button
          class="button primary search-filter-apply"
          type="button"
          @click="submit"
        >
          {{ applyLabel }}
        </button>
      </footer>
    </section>
  </div>
</template>

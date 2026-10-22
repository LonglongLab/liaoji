<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import AvatarBadge from "@/components/AvatarBadge.vue";
import { useVisibleIds } from "@/composables/useVisibleIds";
import type { DirectoryEntry } from "@/lib/searchScopes";

/**
 * 发送人／聊天名录（PSU-2.66；PSU-2.75 起可多选）。
 *
 * 它取代的是原来那个下拉框——真实归档有 612 个会话，而下拉里只装得下第一页的
 * 100 个，剩下 512 个在界面上等于不存在。所以这里**必须真的翻页**，并且如实
 * 印出「已加载多少」。
 *
 * 多选之后多了一条：**已选的钉在最上面**。名录会跟着输入框翻动，选过的那几个
 * 很容易被筛没；看不见自己选了什么，就没法确认这一屏结果是按什么筛出来的。
 */
const props = defineProps<{
  title: string;
  placeholder: string;
  /** 当前生效的名录过滤词，作输入框的初值——框里的词必须等于列表的筛。 */
  query?: string | undefined;
  entries: DirectoryEntry[];
  /** 已选中的标识；顺序不影响筛选，只影响置顶区的排列。 */
  selectedIds: string[];
  loading: boolean;
  fetchingMore: boolean;
  hasMore: boolean;
  errorText?: string | null;
  /** 到了上限还想再选时说的那一句；`null` = 还没到上限。 */
  limitNotice?: string | null;
  disabled?: boolean;
  allSelected?: boolean;
  selectionLayout?: "pinned" | "inline";
  selectedEntries?: DirectoryEntry[];
}>();

const emit = defineEmits<{
  toggle: [id: string];
  "update:query": [query: string];
  "load-more": [];
  /**
   * 现在真的在视口里（含上下各一屏缓冲）的那几行；`null` = 这一端没有
   * `IntersectionObserver`，按全部算。与会话列表同一个事件、同一个 composable
   * （PSU-2.128）：远程搜索页据此只取这几行的头像。本机端的头像是稳定 URL，
   * 不用听这一条。
   */
  "visible-change": [ids: readonly string[] | null];
  "search-focus": [focused: boolean];
}>();

const visibleRows = useVisibleIds();
watch(visibleRows.ids, (ids) => { emit("visible-change", ids); }, { immediate: true });
// 卸载时补一声「什么都看不见了」：H5 抽屉一关名录就整个卸掉，不说的话，父页面
// 会一直替已经不在屏幕上的那几行留着头像。
onBeforeUnmount(() => { globalThis.window.clearTimeout(searchTimer); emit("visible-change", []); });

// 从生效的过滤词装载初值：抽屉关了再开、实例重挂时，框里的词才还等于列表的
// 筛——只装载、不立刻回发，列表本来就是按这个词筛出来的。
const draft = ref(props.query ?? "");
const inputElement = ref<HTMLInputElement | null>(null);
const focused = ref(false);
let composing = false;
let searchTimer: number | undefined;
let lastRequested = props.query ?? "";
const requestSearch = (): void => {
  const value = draft.value.trim();
  if (value === lastRequested) return;
  lastRequested = value;
  emit("update:query", value);
};
const onInput = (event: Event): void => {
  draft.value = (event.target as HTMLInputElement).value;
  globalThis.window.clearTimeout(searchTimer);
  if (composing || (event instanceof InputEvent && event.isComposing)) return;
  searchTimer = globalThis.window.setTimeout(requestSearch, 300);
};
const onCompositionEnd = (event: CompositionEvent): void => {
  composing = false;
  onInput(event);
};
const onCompositionStart = (): void => { composing = true; globalThis.window.clearTimeout(searchTimer); };
const setFocus = (value: boolean): void => { focused.value = value; emit("search-focus", value); };
const finishSearch = (): void => {
  globalThis.window.clearTimeout(searchTimer);
  requestSearch();
  inputElement.value?.blur();
};
const onEnter = (event: KeyboardEvent): void => {
  if (event.isComposing || composing) return;
  event.preventDefault();
  finishSearch();
};
const keepSearchFocus = (event: PointerEvent): void => {
  // Do not blur/reflow on pointerdown. Commit selection on click, so a scroll
  // gesture cannot select an item and an IME blur cannot remove its click target.
  if (focused.value && event.isPrimary && event.button === 0
      && event.target instanceof Element && event.target.closest("button")) event.preventDefault();
};
const toggle = (id: string): void => {
  if (props.disabled || props.allSelected) return;
  emit("toggle", id);
  finishSearch();
};
// 常驻实例（桌面中列是 CSS 隐藏不重挂）也得跟上：别的实例改了共享词、或换名录
// 清了词，这里的框要同步——但自己敲字经防抖回流的同值不动，免得打断输入。
watch(
  () => props.query,
  (value) => {
    const next = value ?? "";
    if (next !== lastRequested && !composing) {
      globalThis.window.clearTimeout(searchTimer);
      lastRequested = next;
      draft.value = next;
    }
  },
);

const selected = computed(() => new Set(props.selectedIds));

/**
 * 已选的置顶，其余按名录自己的顺序。
 *
 * 置顶那几个用的是名录里那一行本身（有名字、有头像）；名录里没有它——比如它在
 * 还没翻到的那一页，或者被输入框筛掉了——就退回一行只有标识的占位，**不编名字**。
 */
const pinned = computed<DirectoryEntry[]>(() =>
  props.selectionLayout === "inline" ? [] :
  props.selectedIds.map(
    (id) =>
      props.entries.find((entry) => entry.id === id) ?? props.selectedEntries?.find(entry => entry.id === id) ?? {
        id,
        name: id,
        named: false,
      },
  ),
);
const rest = computed(() => props.selectionLayout === "inline" ? props.entries
  : props.entries.filter((entry) => !selected.value.has(entry.id)));
</script>

<template>
  <div
    class="search-directory"
    :aria-label="title"
    :class="{ 'is-search-focused': focused }"
    @pointerdown="keepSearchFocus"
  >
    <div class="search-directory-search">
      <span
        class="search-directory-search-icon"
        aria-hidden="true"
      >⌕</span>
      <input
        ref="inputElement"
        :value="draft"
        type="search"
        data-feedback-mask="search-query"
        :aria-label="placeholder"
        :placeholder="placeholder"
        :disabled="disabled"
        enterkeyhint="done"
        autocomplete="off"
        @input="onInput"
        @compositionstart="onCompositionStart"
        @compositionend="onCompositionEnd"
        @focus="setFocus(true)"
        @blur="setFocus(false)"
        @keydown.enter="onEnter"
      >
      <button
        v-if="draft !== ''"
        class="search-directory-clear"
        type="button"
        aria-label="清空检索"
        :disabled="disabled"
        @click="draft = ''; requestSearch(); inputElement?.focus()"
      >
        ×
      </button>
      <button
        v-if="focused"
        class="search-directory-done"
        type="button"
        aria-label="完成输入"
        @click="finishSearch"
      >
        完成
      </button>
    </div>
    <slot name="toolbar" />
    <div class="search-directory-list">
      <p
        v-if="errorText != null"
        class="notice-warn"
        role="alert"
      >
        {{ errorText }}
      </p>
      <template v-else>
        <!-- 已选区。名录在翻、在筛，而这几行不动。 -->
        <template v-if="pinned.length > 0">
          <div class="search-directory-selected-head">
            <span>已选 {{ pinned.length }} 个</span>
          </div>
          <button
            v-for="entry in pinned"
            :key="`picked-${entry.id}`"
            :ref="(el) => { visibleRows.observe(el as Element | null, entry.id); }"
            class="search-directory-item is-active"
            :class="{ 'is-unnamed': entry.named === false }"
            type="button"
            role="checkbox"
            aria-checked="true"
            :aria-label="entry.name"
            :disabled="disabled || allSelected"
            @click="toggle(entry.id)"
          >
            <span
              class="search-directory-check is-checked"
              aria-hidden="true"
            />
            <AvatarBadge
              :name="entry.name"
              :src="entry.avatarRef ?? null"
              :alt="`${entry.name}的头像`"
              size="sm"
            />
            <span
              data-feedback-mask="conversation-name"
              class="search-directory-name"
            >{{ entry.name }}</span>
          </button>
          <p
            v-if="limitNotice != null"
            class="search-directory-hint"
            role="status"
          >
            {{ limitNotice }}
          </p>
          <hr class="search-directory-rule">
        </template>

        <p
          v-if="loading"
          class="search-directory-hint"
          role="status"
        >
          正在加载……
        </p>
        <p
          v-else-if="entries.length === 0"
          class="search-directory-hint"
          role="status"
        >
          名录里没有匹配的名字
        </p>
        <template v-else>
          <button
            v-for="entry in rest"
            :key="entry.id"
            :ref="(el) => { visibleRows.observe(el as Element | null, entry.id); }"
            class="search-directory-item"
            :class="{ 'is-unnamed': entry.named === false, 'is-active': allSelected || selected.has(entry.id) }"
            type="button"
            role="checkbox"
            :aria-checked="allSelected || selected.has(entry.id)"
            :aria-label="entry.name"
            :disabled="disabled || allSelected"
            @click="toggle(entry.id)"
          >
            <span
              class="search-directory-check"
              :class="{ 'is-checked': allSelected || selected.has(entry.id) }"
              aria-hidden="true"
            />
            <AvatarBadge
              :name="entry.name"
              :src="entry.avatarRef ?? null"
              :alt="`${entry.name}的头像`"
              size="sm"
            />
            <span
              data-feedback-mask="conversation-name"
              class="search-directory-name"
            >{{ entry.name }}</span>
          </button>
        </template>
        <!-- 如实说到第几个了。612 个会话装不进一个下拉，也不该假装已经全在这里。 -->
        <button
          v-if="hasMore"
          class="button quiet search-directory-more"
          type="button"
          :disabled="fetchingMore || disabled"
          @click="emit('load-more')"
        >
          {{ fetchingMore ? "正在加载……" : `已加载 ${String(entries.length)} 个，加载更多` }}
        </button>
        <p
          v-else
          class="search-directory-hint"
        >
          共 {{ entries.length }} 个
        </p>
      </template>
    </div>
  </div>
</template>

<style scoped>
.search-directory-search { display: flex; align-items: center; gap: 8px; margin: 8px 12px; padding: 4px 10px; border: 1px solid transparent; border-radius: var(--radius-btn); background: var(--surface-sunken); }
.search-directory-search:focus-within { border-color: var(--rule); background: var(--surface); }
.search-directory-search input { min-width: 0; width: 100%; min-height: 40px; padding: 4px 0; border: 0; border-radius: 0; background: transparent; font: inherit; font-size: 16px; outline: none; }
.search-directory-search input::-webkit-search-cancel-button,.search-directory-search input::-webkit-search-decoration { appearance: none; -webkit-appearance: none; }
.search-directory-search-icon { color: var(--ink-faint); font-size: 22px; }
.search-directory-clear,.search-directory-done { min-width: 44px; min-height: 44px; padding: 0 4px; border: 0; background: none; color: var(--link); font: inherit; font-size: 13px; cursor: pointer; }
.search-directory-clear { color: var(--ink-faint); font-size: 22px; }
.search-directory-item:disabled { cursor: default; }
</style>

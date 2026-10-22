<script setup lang="ts">
import { computed, ref } from "vue";

import type { SearchSort } from "@/api/archiveApi";
import { SORT_LABELS } from "@/lib/searchScopes";

/**
 * 排序控件（PSU-2.66）。
 *
 * 微信那个菜单有四项，我们只有两项——按大小排在真实归档上实测每翻一页都要把
 * 整个候选集物化再排（最小的「文件」范围 6,287 行也要 315–1,302 ms，且与翻多深
 * 无关），契约枚举里因此就没有那两个值。菜单里不摆一个点不动的选项。
 */
const props = defineProps<{
  sort: SearchSort;
  modes: readonly SearchSort[];
}>();

const emit = defineEmits<{
  "update:sort": [sort: SearchSort];
}>();

const open = ref(false);
const label = computed(() => SORT_LABELS[props.sort]);
/** 只有一种排序时（远端），控件退化成一行说明，不给一个点开只有一项的菜单。 */
const choosable = computed(() => props.modes.length > 1);

const choose = (value: SearchSort): void => {
  open.value = false;
  emit("update:sort", value);
};
</script>

<template>
  <div class="search-sort">
    <span
      v-if="!choosable"
      class="search-sort-fixed"
    >{{ label }}</span>
    <template v-else>
      <button
        class="search-sort-trigger"
        type="button"
        aria-haspopup="listbox"
        :aria-expanded="open"
        aria-label="结果排序"
        @click="open = !open"
      >
        <span>{{ label }}</span>
        <svg
          width="11"
          height="11"
          viewBox="0 0 24 24"
          fill="none"
          stroke="currentColor"
          stroke-width="2.4"
          stroke-linecap="round"
          stroke-linejoin="round"
          aria-hidden="true"
        >
          <path d="M7 10l5 5 5-5" />
        </svg>
      </button>
      <template v-if="open">
        <div
          class="search-sort-scrim"
          @click="open = false"
        />
        <ul
          class="search-sort-menu"
          role="listbox"
        >
          <li
            v-for="mode in modes"
            :key="mode"
            role="option"
            :aria-selected="mode === sort"
          >
            <button
              class="search-sort-option"
              :class="{ 'is-active': mode === sort }"
              type="button"
              @click="choose(mode)"
            >
              {{ SORT_LABELS[mode] }}
            </button>
          </li>
        </ul>
      </template>
    </template>
  </div>
</template>

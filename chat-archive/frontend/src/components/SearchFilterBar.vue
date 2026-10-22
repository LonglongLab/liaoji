<script setup lang="ts">
import { computed } from "vue";

import {
  DIMENSION_LABELS,
  FILTER_DIMENSIONS,
  fileClassesAllowed,
  filterChips,
  filterCount,
  type FilterDimension,
  type FilterNames,
  type SearchFilters,
} from "@/lib/searchFilters";
import type { SearchCapabilities } from "@/lib/searchScopes";

/**
 * 筛选条（PSU-2.75）：当前筛的是什么，一眼看得见，也就地摘得掉。
 *
 * 手机上它顶替了原来那半屏名录——名录挪进了抽屉，结果区拿回整屏。一个都没选时
 * 它显示四个入口 chip，那既是提示也是入口；选了之后每个值一枚 chip，超过两个
 * 就并成「3 个会话」（二十个名字排一行既看不完也点不准）。
 */
const props = defineProps<{
  filters: SearchFilters;
  names: FilterNames;
  capabilities: SearchCapabilities;
}>();

const emit = defineEmits<{
  open: [dimension: FilterDimension | null];
  remove: [dimension: FilterDimension, value: string | null];
  clear: [];
}>();

/** 这一端答不出的维度整条不出现——不是灰着（`capabilities.ts` 的教条）。 */
const dimensions = computed<FilterDimension[]>(() =>
  FILTER_DIMENSIONS.filter((dimension) => {
    if (dimension === "senderIds") return props.capabilities.senderScope;
    if (dimension === "fileClasses") {
      // 与范围栏同一条：类型恰好是「文件」时这一维才存在（后端契约的镜像）。
      return props.capabilities.fileClassScope && fileClassesAllowed(props.filters.types);
    }
    return true;
  }),
);
const chips = computed(() => filterChips(props.filters, props.names));
const count = computed(() => filterCount(props.filters));
</script>

<template>
  <div
    class="search-filter-bar"
    role="group"
    aria-label="筛选条件"
  >
    <button
      class="search-filter-funnel"
      type="button"
      aria-label="筛选"
      @click="emit('open', null)"
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
        <path d="M4 6h16M7 12h10M10 18h4" />
      </svg>
      <span
        v-if="count > 0"
        class="search-filter-count"
      >{{ count }}</span>
    </button>

    <div class="search-filter-chips">
      <template v-if="chips.length === 0">
        <!-- 一个都没选时给入口：空着的筛选条否则只是一条灰线。 -->
        <button
          v-for="dimension in dimensions"
          :key="dimension"
          class="search-filter-chip is-empty"
          type="button"
          @click="emit('open', dimension)"
        >
          {{ DIMENSION_LABELS[dimension] }}
        </button>
      </template>
      <template v-else>
        <span
          v-for="chip in chips"
          :key="`${chip.dimension}:${chip.value ?? '*'}`"
          class="search-filter-chip"
        >
          <button
            data-feedback-mask="conversation-name"
            class="search-filter-chip-label"
            type="button"
            @click="emit('open', chip.dimension)"
          >
            {{ chip.label }}
          </button>
          <button
            data-feedback-mask="conversation-name"
            class="search-filter-chip-remove"
            type="button"
            :aria-label="`取消筛选 ${chip.label}`"
            @click="emit('remove', chip.dimension, chip.value)"
          >
            ×
          </button>
        </span>
      </template>
    </div>

    <button
      v-if="count > 0"
      class="text-action search-filter-clear"
      type="button"
      @click="emit('clear')"
    >
      清空
    </button>
  </div>
</template>

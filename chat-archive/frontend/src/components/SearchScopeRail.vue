<script setup lang="ts">
import { computed } from "vue";

import {
  FILE_CLASS_OPTIONS,
  TYPE_OPTIONS,
  fileClassesAllowed,
  type FilterDimension,
  type SearchFilters,
} from "@/lib/searchFilters";
import type { SearchCapabilities } from "@/lib/searchScopes";

/**
 * 类型栏——微信文件管理左边那一列（PSU-2.66；PSU-2.75 起是多选）。
 *
 * 从前它是「范围」：一次只能选一项，选了「聊天」就没法再叠「图片」。现在它是
 * 消息类型这一维的选择器，复选语义（`aria-checked`），会话与发送人挪去名录栏。
 * 「文件」选中时在下面展开文件类别那一层：微信的一级就是文件类型，我们的一级
 * 是消息类型，所以它只能是第二层。
 */
const props = defineProps<{
  filters: SearchFilters;
  capabilities: SearchCapabilities;
  /** H5 抽屉里字号与留白都大一档。 */
  compact?: boolean;
}>();

const emit = defineEmits<{
  toggle: [dimension: FilterDimension, value: string];
}>();

// 「恰好只选了文件」才展开第二层——镜像后端契约：`file_class` 只在类型恰好为
// {file} 时被接受，又叠上一个类型的那一刻，这一层连同它的选择一起收起。
const showsFileClasses = computed(
  () => props.capabilities.fileClassScope && fileClassesAllowed(props.filters.types),
);
const isPicked = (dimension: FilterDimension, value: string): boolean =>
  (props.filters[dimension] as readonly string[]).includes(value);
</script>

<template>
  <nav
    class="search-scope-rail"
    :class="{ 'is-compact': compact }"
    aria-label="消息类型"
  >
    <h2 class="search-scope-heading">
      消息类型
    </h2>
    <button
      v-for="item in TYPE_OPTIONS"
      :key="item.value"
      class="search-scope-item"
      :class="{ 'is-active': isPicked('types', item.value) }"
      type="button"
      role="checkbox"
      :aria-checked="isPicked('types', item.value)"
      @click="emit('toggle', 'types', item.value)"
    >
      <svg
        width="17"
        height="17"
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

    <template v-if="showsFileClasses">
      <h2 class="search-scope-heading">
        文件类型
      </h2>
      <button
        v-for="item in FILE_CLASS_OPTIONS"
        :key="item.value"
        class="search-scope-item is-nested"
        :class="{ 'is-active': isPicked('fileClasses', item.value) }"
        type="button"
        role="checkbox"
        :aria-checked="isPicked('fileClasses', item.value)"
        @click="emit('toggle', 'fileClasses', item.value)"
      >
        <svg
          width="16"
          height="16"
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
    </template>
  </nav>
</template>

<script setup lang="ts">
import { computed } from "vue";
import AppSelect from "@/components/ui/AppSelect.vue";
import type { SearchSort } from "@/api/archiveApi";
import { SORT_LABELS } from "@/lib/searchScopes";

const props = defineProps<{ sort: SearchSort; modes: readonly SearchSort[] }>();
const emit = defineEmits<{ "update:sort": [sort: SearchSort] }>();
const options = computed(() => props.modes.map(value => ({ value, label: SORT_LABELS[value] })));
const selected = computed({
  get: () => props.sort,
  set: (value: SearchSort) => { if (props.modes.includes(value)) emit("update:sort", value); },
});
</script>

<template>
  <div class="search-sort">
    <span
      v-if="modes.length < 2"
      class="search-sort-fixed"
    >{{ SORT_LABELS[sort] }}</span>
    <AppSelect
      v-else
      v-model="selected"
      class="search-sort-trigger"
      :options="options"
      :filter="false"
      aria-label="结果排序"
    />
  </div>
</template>

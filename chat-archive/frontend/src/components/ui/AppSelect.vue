<script setup lang="ts" generic="T extends string | number">
import Select from "primevue/select";

defineOptions({ inheritAttrs: false });
withDefaults(defineProps<{
  options: readonly { label: string; value: T }[];
  filter?: boolean;
  placeholder?: string;
  sensitive?: boolean;
}>(), { filter: true, placeholder: "请选择", sensitive: false });
const model = defineModel<T>({ required: true });
</script>

<template>
  <Select
    v-bind="$attrs"
    v-model="model"
    class="app-select"
    :options="[...options]"
    option-label="label"
    option-value="value"
    :filter="filter"
    filter-placeholder="搜索选项"
    :placeholder="placeholder"
    append-to="body"
    :scroll-height="'240px'"
  >
    <template #option="{ option }">
      <span
        v-if="sensitive"
        data-feedback-mask="conversation-name"
      >{{ option.label }}</span>
      <span v-else>{{ option.label }}</span>
    </template>
  </Select>
</template>

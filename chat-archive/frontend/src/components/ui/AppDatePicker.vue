<script setup lang="ts">
import { computed } from "vue";
import DatePicker from "primevue/datepicker";

defineOptions({ inheritAttrs: false });
const props = defineProps<{ min?: string; max?: string }>();
const model = defineModel<string>({ required: true });
const parse = (value: string | undefined): Date | undefined => {
  if (!value || !/^\d{4}-\d{2}-\d{2}$/.test(value)) return undefined;
  const [year, month, day] = value.split("-").map(Number);
  if (year === undefined || month === undefined || day === undefined) return undefined;
  const date = new Date(year, month - 1, day);
  return date.getFullYear() === year && date.getMonth() === month - 1 && date.getDate() === day ? date : undefined;
};
const selected = computed(() => parse(model.value) ?? null);
const update = (value: unknown): void => {
  if (value === null || value === undefined) { model.value = ""; return; }
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) return;
  model.value = `${String(value.getFullYear()).padStart(4, "0")}-${String(value.getMonth() + 1).padStart(2, "0")}-${String(value.getDate()).padStart(2, "0")}`;
};
</script>

<template>
  <DatePicker
    v-bind="$attrs"
    class="app-date-picker"
    :model-value="selected"
    :min-date="parse(props.min)"
    :max-date="parse(props.max)"
    date-format="yy-mm-dd"
    :manual-input="false"
    show-icon
    show-button-bar
    append-to="body"
    @update:model-value="update"
  />
</template>

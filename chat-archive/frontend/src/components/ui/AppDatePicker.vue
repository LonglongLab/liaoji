<script setup lang="ts">
import { computed } from "vue";
import DatePicker from "primevue/datepicker";
import { formatDateInput, parseDateInput } from "@/utils/time";

defineOptions({ inheritAttrs: false });
const props = defineProps<{ min?: string; max?: string }>();
const model = defineModel<string>({ required: true });
const parse = (value: string | undefined): Date | undefined => parseDateInput(value) ?? undefined;
const selected = computed(() => parse(model.value) ?? null);
const update = (value: unknown): void => {
  if (value === null || value === undefined) { model.value = ""; return; }
  if (!(value instanceof Date) || !Number.isFinite(value.getTime())) return;
  model.value = formatDateInput(value);
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

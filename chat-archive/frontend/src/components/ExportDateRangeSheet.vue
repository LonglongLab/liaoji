<script setup lang="ts">
import { computed, ref, toRef, watch, nextTick } from "vue";
import { Calendar, type CalendarInstance } from "vant/es/calendar";
import { DatePicker } from "vant/es/date-picker";
import type { PickerConfirmEventParams } from "vant/es/picker";
import { useModalFocus } from "@/composables/useModalFocus";
import AppAction from "@/components/ui/AppAction.vue";
import { formatDateInput as dateInput, parseDateInput } from "@/utils/time";
import "vant/es/style/base.css";
import "vant/es/icon/index.css";
import "vant/es/loading/index.css";
import "vant/es/button/index.css";
import "vant/es/picker/index.css";
import "vant/es/calendar/index.css";

const props = defineProps<{ open: boolean; from: string; to: string; conversationName?: string | undefined }>();
const emit = defineEmits<{ close: []; apply: [from: string, to: string] }>();
const monthPickerOpen = ref(false);
const monthValues = ref<string[]>([]);
const calendar = ref<CalendarInstance | null>(null);
const selectedDates = ref<Date[]>([]);
const { container, initial, onKeydown } = useModalFocus(toRef(props,"open"), () => {
  if(monthPickerOpen.value)monthPickerOpen.value=false;
  else emit("close");
});
void container;void initial;
const monthLabel = (date: Date | undefined): string => {
  const value=date??new Date();
  return `${String(value.getFullYear())} 年 ${String(value.getMonth()+1)} 月`;
};
const openMonthPicker = (date: Date | undefined): void => {
  const value=date??new Date();
  monthValues.value=[String(value.getFullYear()),String(value.getMonth()+1).padStart(2,"0")];
  monthPickerOpen.value=true;
};
const jumpToMonth = async (event: PickerConfirmEventParams): Promise<void> => {
  const year=Number(event.selectedValues[0]),month=Number(event.selectedValues[1]);
  if(!Number.isInteger(year)||!Number.isInteger(month)||month<1||month>12)return;
  monthPickerOpen.value=false;
  await nextTick();
  calendar.value?.scrollToDate(new Date(year,month-1,1));
};
const defaultDate = computed(() => {
  const from = parseDateInput(props.from), to = parseDateInput(props.to);
  return from !== null && to !== null && from <= to ? [from,to] : null;
});
watch(() => props.open, () => {
  monthPickerOpen.value = false;
  selectedDates.value = defaultDate.value ?? [];
}, { immediate: true });
const dateHint = computed(() => {
  const [from, to] = selectedDates.value;
  if (from === undefined) return "先选开始日";
  return to === undefined ? "再选结束日，可以是同一天。" : "包含开始日和结束日当天。";
});
const minDate = computed(() => {
  const old = parseDateInput(props.from);
  return old !== null && old.getFullYear() < 1970 ? old : new Date(1970,0,1);
});
const maxDate = computed(() => {
  const today = new Date();
  const previous = parseDateInput(props.to);
  return previous !== null && previous > today ? previous : today;
});
const confirm = (dates: Date[]): void => {
  const from = dates[0], to = dates[1];
  if (from === undefined || to === undefined || from > to) return;
  emit("apply", dateInput(from), dateInput(to));
  emit("close");
};
</script>

<template>
  <div
    v-if="open"
    class="export-sheet-backdrop"
    @click.self="emit('close')"
  >
    <section
      ref="container"
      class="export-sheet export-calendar-sheet"
      role="dialog"
      aria-modal="true"
      aria-label="自选日期"
      @keydown="onKeydown"
    >
      <header class="export-sheet-head">
        <h2>自选日期</h2><button
          ref="initial"
          type="button"
          class="export-icon-button"
          aria-label="关闭日期选择"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
      <div class="export-calendar-range">
        <strong
          v-if="conversationName !== undefined"
          data-feedback-mask="conversation-name"
        >{{ conversationName }}</strong>
        <dl>
          <div>
            <dt>开始日</dt>
            <dd>{{ selectedDates[0] ? dateInput(selectedDates[0]) : '未选择' }}</dd>
          </div>
          <div>
            <dt>结束日</dt>
            <dd>{{ selectedDates[1] ? dateInput(selectedDates[1]) : '未选择' }}</dd>
          </div>
        </dl>
        <p
          class="export-note"
          role="status"
        >
          {{ dateHint }}
        </p>
        <AppAction
          v-if="conversationName !== undefined"
          @click="emit('apply', '', ''); emit('close')"
        >
          保存这个聊天的全部时间
        </AppAction>
      </div>
      <div class="export-calendar-body">
        <DatePicker
          v-if="monthPickerOpen"
          v-model="monthValues"
          title="跳到年月"
          :columns-type="['year','month']"
          :min-date="minDate"
          :max-date="maxDate"
          @confirm="jumpToMonth"
          @cancel="monthPickerOpen = false"
        />
        <Calendar
          v-show="!monthPickerOpen"
          ref="calendar"
          class="export-date-calendar"
          :show="open"
          color="var(--accent-solid)"
          :poppable="false"
          :show-title="false"
          title="选择日期区间"
          type="range"
          switch-mode="year-month"
          :allow-same-day="true"
          :min-date="minDate"
          :max-date="maxDate"
          :default-date="defaultDate"
          :show-mark="false"
          confirm-text="使用这段时间"
          confirm-disabled-text="使用这段时间"
          :close-on-click-overlay="true"
          :safe-area-inset-bottom="true"
          @confirm="confirm"
          @select="selectedDates = $event"
          @update:show="!$event && emit('close')"
        >
          <template #subtitle="{ date }">
            <button
              type="button"
              class="export-month-jump"
              aria-label="跳到年月"
              @click.stop="openMonthPicker(date)"
            >
              {{ monthLabel(date) }}
            </button>
          </template>
          <template #prev-month="{ disabled }">
            <button
              type="button"
              aria-label="上一月"
              :disabled="disabled"
            >
              ‹
            </button>
          </template>
          <template #next-month="{ disabled }">
            <button
              type="button"
              aria-label="下一月"
              :disabled="disabled"
            >
              ›
            </button>
          </template>
          <template #prev-year="{ disabled }">
            <button
              type="button"
              aria-label="上一年"
              :disabled="disabled"
            >
              «
            </button>
          </template>
          <template #next-year="{ disabled }">
            <button
              type="button"
              aria-label="下一年"
              :disabled="disabled"
            >
              »
            </button>
          </template>
        </Calendar>
      </div>
    </section>
  </div>
</template>

<style>
.export-date-calendar {
  --van-calendar-background: var(--surface);
  --van-calendar-selected-day-background: var(--accent-solid);
  --van-calendar-range-edge-background: var(--accent-solid);
  --van-calendar-range-edge-color: var(--surface);
  --van-button-primary-background: var(--accent-solid);
  --van-button-primary-border-color: var(--accent-solid);
  --van-calendar-selected-day-color: var(--surface);
  --van-calendar-range-middle-color: var(--accent-ink);
  --van-calendar-range-middle-background-opacity: .08;
  --van-calendar-day-height: 56px;
  --van-calendar-header-title-height: 48px;
  --van-calendar-confirm-button-height: 48px;
  --van-popup-background: var(--surface);
  --van-text-color: var(--ink);
  --van-text-color-2: var(--ink-soft);
  --van-text-color-3: var(--ink-faint);
  --van-border-color: var(--rule);
  --van-primary-color: var(--accent-solid);
  --van-font-family: var(--font-body);
}
.export-date-calendar .van-calendar__header-title { font-family: var(--font-display); }
.export-date-calendar .van-calendar__confirm { border-radius: var(--radius-btn); }
.export-date-calendar .van-calendar__month-selector button,
.export-date-calendar .van-calendar__header-subtitle,
.export-date-calendar .van-calendar__header-action,
.export-date-calendar .van-popup__close-icon { min-width: 44px; min-height: 44px; }
.export-date-calendar .van-calendar__header-action button { min-width: 44px; min-height: 44px; border: 0; background: transparent; color: inherit; font: inherit; font-size: 22px; }
.export-date-calendar .van-icon-cross::before { content: "×"; font-family: var(--font-body); }
.export-date-calendar .van-calendar__header { box-shadow: none; border-bottom: 1px solid var(--rule); }
.export-calendar-sheet { height: min(650px,85dvh); }
.export-calendar-range { padding: 8px 16px; border-bottom: 1px solid var(--rule); }
.export-calendar-range dl { display: grid; grid-template-columns: repeat(2,minmax(0,1fr)); gap: 8px; }
.export-calendar-range dt { color: var(--ink-soft); font-size: 12px; }
.export-calendar-range dd { font-weight: 700; }
.export-calendar-range .export-note { margin-top: 4px; }
.export-calendar-body { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; }
.export-calendar-body > .van-calendar { flex: 1; min-height: 0; }
.export-month-jump { min-height: 44px; padding: 0 12px; border: 0; background: transparent; color: var(--ink); font: inherit; cursor: pointer; }
.export-calendar-body .van-picker { --van-picker-background: var(--surface); --van-text-color: var(--ink); --van-picker-mask-color: transparent; --van-picker-confirm-action-color: var(--accent-ink); }
.export-calendar-body .van-picker__confirm,.export-calendar-body .van-picker__cancel { min-height: 44px; min-width: 60px; }
</style>

<script setup lang="ts">
import { computed, ref, toRef, watch, nextTick } from "vue";
import { Calendar, type CalendarInstance } from "vant/es/calendar";
import { DatePicker } from "vant/es/date-picker";
import type { PickerConfirmEventParams } from "vant/es/picker";
import { useModalFocus } from "@/composables/useModalFocus";
import "vant/es/style/base.css";
import "vant/es/icon/index.css";
import "vant/es/loading/index.css";
import "vant/es/button/index.css";
import "vant/es/overlay/index.css";
import "vant/es/popup/index.css";
import "vant/es/toast/index.css";
import "vant/es/sticky/index.css";
import "vant/es/swipe/index.css";
import "vant/es/swipe-item/index.css";
import "vant/es/tabs/index.css";
import "vant/es/tab/index.css";
import "vant/es/picker/index.css";
import "vant/es/picker-group/index.css";
import "vant/es/calendar/index.css";

const props = defineProps<{ open: boolean; from: string; to: string }>();
const emit = defineEmits<{ close: []; apply: [from: string, to: string] }>();
const monthPickerOpen = ref(false);
const monthValues = ref<string[]>([]);
const calendar = ref<CalendarInstance | null>(null);
const { container, initial, onKeydown } = useModalFocus(toRef(props,"open"), () => {
  if(monthPickerOpen.value)monthPickerOpen.value=false;
  else emit("close");
});
void container;void initial;
watch(()=>props.open,()=>{monthPickerOpen.value=false;});
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
const parse = (value: string): Date | null => {
  if (!/^\d{4}-\d{2}-\d{2}$/.test(value)) return null;
  const [y,m,d] = value.split("-").map(Number);
  if (y === undefined || m === undefined || d === undefined) return null;
  const date = new Date(y,m-1,d);
  return date.getFullYear() === y && date.getMonth() === m-1 && date.getDate() === d ? date : null;
};
const dateInput = (date: Date): string => `${String(date.getFullYear())}-${String(date.getMonth()+1).padStart(2,"0")}-${String(date.getDate()).padStart(2,"0")}`;
const defaultDate = computed(() => {
  const from = parse(props.from), to = parse(props.to);
  return from !== null && to !== null && from <= to ? [from,to] : null;
});
const minDate = computed(() => {
  const old = parse(props.from);
  return old !== null && old.getFullYear() < 1970 ? old : new Date(1970,0,1);
});
const maxDate = computed(() => {
  const today = new Date();
  const previous = parse(props.to);
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
      aria-label="选择日期区间"
      @keydown="onKeydown"
    >
      <header class="export-sheet-head">
        <h2>选择日期区间</h2><button
          ref="initial"
          type="button"
          class="export-icon-button"
          aria-label="关闭日期选择"
          @click="emit('close')"
        >
          ×
        </button>
      </header>
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
          confirm-text="确认日期"
          confirm-disabled-text="请选择开始日和结束日"
          :close-on-click-overlay="true"
          :safe-area-inset-bottom="true"
          @confirm="confirm"
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
.export-calendar-body { display: flex; flex-direction: column; flex: 1; min-height: 0; overflow: hidden; }
.export-calendar-body > .van-calendar { flex: 1; min-height: 0; }
.export-month-jump { min-height: 44px; padding: 0 12px; border: 0; background: transparent; color: var(--ink); font: inherit; cursor: pointer; }
.export-calendar-body .van-picker { --van-picker-background: var(--surface); --van-text-color: var(--ink); --van-picker-mask-color: transparent; --van-picker-confirm-action-color: var(--accent-ink); }
.export-calendar-body .van-picker__confirm,.export-calendar-body .van-picker__cancel { min-height: 44px; min-width: 60px; }
</style>

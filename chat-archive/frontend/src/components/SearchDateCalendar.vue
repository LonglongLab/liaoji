<script setup lang="ts">
import AppSelect from "@/components/ui/AppSelect.vue";
import { computed, ref, watch } from "vue";

const props = defineProps<{
  open: boolean;
  /** 已选中的发送日期，YYYY-MM-DD；空串表示没有按日期筛选。 */
  selected: string;
  /** 这个月里真的有消息的日子，其余日子点不动。 */
  availableDays: string[];
  loading?: boolean;
}>();

const emit = defineEmits<{
  close: [];
  apply: [date: string];
  "view-month": [year: number, month: number];
}>();

const WEEKDAYS = ["一", "二", "三", "四", "五", "六", "日"];

const parseDate = (value: string): { year: number; month: number; day: number } | null => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return null;
  return { year: Number(match[1]), month: Number(match[2]), day: Number(match[3]) };
};

const dateKey = (year: number, month: number, day: number): string =>
  `${String(year)}-${String(month).padStart(2, "0")}-${String(day).padStart(2, "0")}`;

const today = new Date();
const viewYear = ref(today.getFullYear());
const viewMonth = ref(today.getMonth() + 1);
const picked = ref(props.selected);

const syncView = (): void => {
  const parsed = parseDate(props.selected);
  picked.value = props.selected;
  viewYear.value = parsed?.year ?? new Date().getFullYear();
  viewMonth.value = parsed?.month ?? new Date().getMonth() + 1;
};

watch(
  () => props.open,
  (isOpen) => {
    if (isOpen) syncView();
  },
  { immediate: true },
);

watch(
  [viewYear, viewMonth, () => props.open],
  ([year, month, isOpen]) => {
    if (isOpen) emit("view-month", year, month);
  },
  { immediate: true },
);

const years = computed(() => {
  const current = new Date().getFullYear();
  const earliest = Math.min(current - 12, viewYear.value);
  const list: number[] = [];
  for (let year = current; year >= earliest; year -= 1) list.push(year);
  return list;
});

const months = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12];

interface DayCell {
  key: string;
  day: number | null;
  available: boolean;
  picked: boolean;
}

const cells = computed<DayCell[]>(() => {
  const available = new Set(props.availableDays);
  const first = new Date(viewYear.value, viewMonth.value - 1, 1);
  const leading = (first.getDay() + 6) % 7;
  const daysInMonth = new Date(viewYear.value, viewMonth.value, 0).getDate();
  const list: DayCell[] = [];
  for (let index = 0; index < leading; index += 1) {
    list.push({ key: `blank-${String(index)}`, day: null, available: false, picked: false });
  }
  for (let day = 1; day <= daysInMonth; day += 1) {
    const key = dateKey(viewYear.value, viewMonth.value, day);
    list.push({ key, day, available: available.has(key), picked: picked.value === key });
  }
  return list;
});

const pick = (cell: DayCell): void => {
  if (cell.day === null || !cell.available) return;
  picked.value = picked.value === cell.key ? "" : cell.key;
};

const confirm = (): void => {
  emit("apply", picked.value);
};
</script>

<template>
  <section
    class="date-calendar"
    role="dialog"
    aria-modal="true"
    aria-label="选择发送日期"
  >
    <header class="date-calendar-head">
      <label>
        <AppSelect
          v-model="viewYear"
          :options="years.map(value => ({ value, label: `${String(value)}年` }))"
          aria-label="年份"
        />
      </label>
      <label>
        <AppSelect
          v-model="viewMonth"
          :options="months.map(value => ({ value, label: `${String(value)}月` }))"
          aria-label="月份"
        />
      </label>
    </header>

    <p class="date-calendar-hint">
      {{ loading ? "正在读取这个月的记录……" : "选择发送日期，灰色的日子没有归档记录。" }}
    </p>

    <div
      class="date-calendar-grid"
      role="grid"
    >
      <span
        v-for="weekday in WEEKDAYS"
        :key="weekday"
        class="date-calendar-weekday"
      >{{ weekday }}</span>
      <template
        v-for="cell in cells"
        :key="cell.key"
      >
        <span
          v-if="cell.day === null"
          class="date-calendar-blank"
        />
        <button
          v-else
          class="date-calendar-day"
          type="button"
          :class="{ 'is-picked': cell.picked, 'is-quiet': !cell.available }"
          :disabled="!cell.available"
          :aria-pressed="cell.picked"
          :aria-label="cell.available ? `${cell.key}，有归档记录` : `${cell.key}，没有归档记录`"
          @click="pick(cell)"
        >
          {{ cell.day }}
        </button>
      </template>
    </div>

    <footer class="date-calendar-foot">
      <button
        class="button quiet"
        type="button"
        @click="emit('close')"
      >
        取消
      </button>
      <button
        class="button"
        type="button"
        @click="confirm"
      >
        确定
      </button>
    </footer>
  </section>
</template>

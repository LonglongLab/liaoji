<script setup lang="ts">
import { computed } from "vue";
const props = defineProps<{ items: { label: string; count: number }[]; label: string }>();
const emit = defineEmits<{ select: [label: string] }>();
const max = computed(() => Math.max(0, ...props.items.map((item) => item.count)));
</script>
<template>
  <p
    v-if="max === 0"
    class="analysis-empty"
  >
    所选范围内没有归档消息。
  </p>
  <div v-else>
    <p class="chart-scale">
      最高 {{ max.toLocaleString("zh-CN") }} 条
    </p>
    <div
      class="analysis-bars"
      :aria-label="label"
    >
      <button
        v-for="item in items"
        :key="item.label"
        type="button"
        class="analysis-bar"
        :class="{ peak: item.count === max }"
        :style="{ height: `${String(item.count / max * 100)}%` }"
        :disabled="item.count === 0"
        :aria-label="`${item.label}，${String(item.count)} 条消息`"
        :title="`${item.label}：${item.count.toLocaleString('zh-CN')} 条`"
        @click="emit('select', item.label)"
      />
    </div>
    <div class="analysis-axis">
      <span>{{ items[0]?.label }}</span><span>{{ items[Math.floor(items.length / 2)]?.label }}</span><span>{{ items[items.length - 1]?.label }}</span>
    </div>
  </div>
</template>
<style scoped>
.chart-scale { font-size: 12px; color: var(--ink-faint); text-align: right; margin: 16px 0 8px; }
.analysis-bars { display: flex; align-items: end; gap: 4px; height: 160px; border-bottom: 1px solid var(--rule); }
.analysis-bar { flex: 1; min-width: 0; padding: 0; border: 0; border-radius: 3px 3px 0 0; background: #b7d3ab; cursor: pointer; }
.analysis-bar.peak, .analysis-bar:hover, .analysis-bar:focus-visible { background: var(--accent-solid); }
.analysis-axis { display: flex; justify-content: space-between; gap: 8px; font-size: 11px; color: var(--ink-faint); margin-top: 8px; }
.analysis-empty { padding: 64px 8px; color: var(--ink-faint); text-align: center; }
@media (max-width: 700px) { .analysis-bars { gap: 2px; } }
</style>

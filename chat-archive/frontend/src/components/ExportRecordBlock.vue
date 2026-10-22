<script setup lang="ts">
import {
  mergedForwardItemSummary,
  mergedForwardTitle,
  type AppCard,
} from "@/lib/mergedForward";

/**
 * 导出文档里的「聊天记录」卡片（PSU-2.168）。
 *
 * App 里那张卡是**点开**看的，而导出的文件里点不开——把内容折在一张点不动的卡
 * 后面，等于把它丢了。用户在决策页上选的是 hybrid：HTML 里可折叠（保住点开的
 * 手感），PDF 与图片里就地展开。两者的区别只有 `open` 这一个属性，所以是同一个
 * 组件，不是两份。
 *
 * 嵌套的记录**递归**成嵌套的 `<details>`：App 里点进去看的那一层，在文件里必须
 * 就在原地，否则又是一个点不动的入口。
 */
const props = withDefaults(
  defineProps<{
    card: AppCard;
    /** 静态产物（PDF / 图片）里全部摊开；HTML 里收着。 */
    expanded?: boolean;
    /** 顶层是 0；每深一层缩进一档，读者才看得出哪几条属于哪一层。 */
    depth?: number;
  }>(),
  { expanded: false, depth: 0 },
);

const title = (): string => mergedForwardTitle(props.card);
</script>

<template>
  <details
    class="export-record"
    :open="expanded"
    :style="{ marginLeft: `${depth * 10}px` }"
  >
    <summary data-feedback-mask="message-body">
      {{ title() }} · {{ card.item_total }} 条
    </summary>
    <div class="export-record-items">
      <template
        v-for="(item, index) in card.items"
        :key="index"
      >
        <!-- 嵌套的记录自己展开成一层；其余条目就是一行摘要。 -->
        <ExportRecordBlock
          v-if="item.nested !== null"
          :card="item.nested"
          :expanded="expanded"
          :depth="depth + 1"
        />
        <p
          v-else
          data-feedback-mask="message-body"
          class="export-record-line"
        >
          {{ mergedForwardItemSummary(item) }}
        </p>
      </template>
      <p
        v-if="card.items.length < card.item_total"
        class="export-record-more"
      >
        <!-- 归档里只存了前若干条时如实说，不让读者以为这就是全部。 -->
        档案里只留下了前 {{ card.items.length }} 条，原记录共 {{ card.item_total }} 条
      </p>
      <p
        v-else-if="card.items.length === 0"
        class="export-record-more"
      >
        档案里没有留下这份记录的条目
      </p>
    </div>
  </details>
</template>

<style scoped>
.export-record {
  margin: 4px 0;
  padding: 8px 10px;
  border: 1px solid var(--rule-soft);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
  font-size: 13px;
}

.export-record > summary {
  cursor: pointer;
  color: var(--ink);
  font-weight: 600;
}

.export-record-items {
  margin-top: 6px;
  padding-left: 9px;
  border-left: 2px solid var(--accent-solid);
}

.export-record-line {
  margin: 0 0 3px;
  color: var(--ink-soft);
  overflow-wrap: anywhere;
}

.export-record-more {
  margin: 4px 0 0;
  color: var(--ink-faint);
  font-size: 12px;
}
</style>

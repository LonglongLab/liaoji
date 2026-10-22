<script setup lang="ts">
import { computed } from "vue";

import { fileVisualOf } from "@/lib/fileKinds";

/**
 * 纸张造型的类型图标。底部印的是文件自己的扩展名——认不出的类型也照实印，
 * 「DWG」比一个问号多给用户信息，也不用我们去猜它是什么。
 */
const props = withDefaults(
  defineProps<{
    fileName: string | null;
    size?: "sm" | "md" | "lg";
  }>(),
  { size: "md" },
);

const visual = computed(() => fileVisualOf(props.fileName));
/** 四个字符的扩展名要再小一号才装得下。 */
const markLength = computed(() => `is-len-${String(Math.min(visual.value.mark.length, 4))}`);
</script>

<template>
  <span
    class="file-icon"
    :class="[`is-${visual.kind}`, `is-${size}`, markLength]"
    aria-hidden="true"
  >
    <span class="file-icon-fold" />
    <span class="file-icon-mark">{{ visual.mark }}</span>
  </span>
</template>

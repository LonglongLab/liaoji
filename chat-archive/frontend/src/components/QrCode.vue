<script setup lang="ts">
import { computed } from "vue";

import { qrPathData, QR_MODULE_COUNT_UNAVAILABLE, qrModuleCount } from "@/lib/qr";

const props = defineProps<{ value: string; label: string }>();

const modules = computed(() => qrModuleCount(props.value));
const path = computed(() => qrPathData(props.value));
const failed = computed(() => modules.value === QR_MODULE_COUNT_UNAVAILABLE);
</script>

<template>
  <!--
    画成一个 SVG path，而不是一堆 <rect>：一个 41×41 的码有上千个模块，
    上千个元素在手机上明显更慢，而路径的 d 属性只是一串字符串。
  -->
  <svg
    v-if="!failed"
    class="qr-code"
    :viewBox="`0 0 ${modules} ${modules}`"
    role="img"
    :aria-label="label"
    shape-rendering="crispEdges"
  >
    <rect
      :width="modules"
      :height="modules"
      fill="#ffffff"
    />
    <path
      :d="path"
      fill="#000000"
    />
  </svg>
  <p
    v-else
    class="action-note"
    role="status"
  >
    这台设备画不出二维码。用下面的密钥手工录入，效果一样。
  </p>
</template>

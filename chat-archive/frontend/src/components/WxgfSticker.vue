<script setup lang="ts">
/**
 * wxgf 动图表情：解码后在画布上逐帧播放（PSU-2.146）。
 *
 * 没有 WebCodecs HEVC 的浏览器只解得出第一帧（WASM 兜底对部分动图偏色，宁可静态
 * 也不给错颜色），这时如实说一句「此浏览器不支持动图」，不假装它就是静态表情。
 */
import { onBeforeUnmount, onMounted, ref } from "vue";

import { playWxgfSticker, type StickerPlayback } from "@/lib/wxgf/player";

const props = defineProps<{
  blob: Blob;
  alt: string;
}>();

const emit = defineEmits<{
  loaded: [];
  failed: [];
}>();

const canvas = ref<HTMLCanvasElement | null>(null);
const stillOnly = ref(false);
const frameCount = ref(0);
/**
 * 画成了没有，写在画布上（`data-sticker-state`）。
 *
 * 导出（PSU-2.168）要等表情真画上去才读像素，没画成的要如实记成「没带上」；
 * 屏外那份文档里它只能从 DOM 上读到这件事（`lib/exportDocument/render.ts`）。
 */
const state = ref<"loading" | "ready" | "failed">("loading");
let playback: StickerPlayback | null = null;
let disposed = false;

onMounted(async () => {
  const target = canvas.value;
  if (target === null) return;
  try {
    const started = await playWxgfSticker(target, props.blob);
    if (disposed) {
      started.stop();
      return;
    }
    playback = started;
    frameCount.value = started.frameCount;
    stillOnly.value = !started.complete;
    state.value = "ready";
    emit("loaded");
  } catch {
    if (disposed) return;
    state.value = "failed";
    emit("failed");
  }
});

onBeforeUnmount(() => {
  disposed = true;
  playback?.stop();
});
</script>

<template>
  <span class="wxgf-sticker">
    <canvas
      ref="canvas"
      class="attachment-emoji"
      role="img"
      :aria-label="alt"
      :data-frames="frameCount"
      :data-sticker-state="state"
    />
    <small
      v-if="stillOnly"
      class="wxgf-sticker-note"
    >此浏览器不支持动图</small>
  </span>
</template>

<style scoped>
.wxgf-sticker {
  display: inline-flex;
  flex-direction: column;
  gap: 2px;
}

.wxgf-sticker-note {
  color: var(--ink-faint);
  font-size: 11px;
}
</style>

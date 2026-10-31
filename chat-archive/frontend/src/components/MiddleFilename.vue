<script setup lang="ts">
import { computed } from "vue";
import { highlightParts, type HighlightPart } from "@/lib/highlight";

const props = defineProps<{ name: string; query?: string | undefined }>();
const parts = computed(() => {
  const chars = Array.from(props.name);
  const extension = /(\.tar\.(?:gz|bz2|xz|zst)|\.[^.]+)$/iu.exec(props.name);
  const suffixLength = extension !== null && extension.index > 0 ? Array.from(extension[0]).length : 0;
  const tailLength = Math.min(chars.length, Math.max(8, suffixLength));
  return [chars.slice(0, chars.length - tailLength).join(""), chars.slice(-tailLength).join("")];
});
const highlighted = computed(() => {
  const start: HighlightPart[] = [], end: HighlightPart[] = [];
  const boundary = parts.value[0]?.length ?? 0;
  let offset = 0;
  for (const part of highlightParts(props.name, props.query)) {
    const cut = Math.max(0, Math.min(part.text.length, boundary - offset));
    if (cut > 0) start.push({ ...part, text: part.text.slice(0, cut) });
    if (cut < part.text.length) end.push({ ...part, text: part.text.slice(cut) });
    offset += part.text.length;
  }
  return [start, end];
});
</script>

<template>
  <span
    class="middle-filename"
    :title="name"
    :aria-label="name"
  >
    <span
      class="middle-filename-start"
    ><template
      v-for="(part, index) in highlighted[0]"
      :key="index"
    ><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span><span
      class="middle-filename-end"
    ><template
      v-for="(part, index) in highlighted[1]"
      :key="index"
    ><mark v-if="part.matched">{{ part.text }}</mark><template v-else>{{ part.text }}</template></template></span>
  </span>
</template>

<style scoped>
.middle-filename { display: flex; min-width: 0; max-width: 100%; }
.middle-filename-start { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
.middle-filename-end { flex: none; max-width: 100%; overflow-wrap: anywhere; white-space: normal; }
</style>

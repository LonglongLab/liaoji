<script setup lang="ts">
import { computed, onBeforeUnmount, ref, shallowRef, watch } from "vue";

import type { AttachmentSummary } from "@/api/archiveApi";
import WxgfSticker from "@/components/WxgfSticker.vue";
import { isWxgfAnimation } from "@/lib/wxgf/toBlob";

const props = withDefaults(defineProps<{
  attachment: AttachmentSummary;
  load: (attachmentId: string) => Promise<Blob>;
  resolve?: ((attachmentId: string) => Promise<AttachmentSummary>) | undefined;
  fetchAvailable?: boolean;
}>(), { resolve: undefined, fetchAvailable: false });
const emit = defineEmits<{ open: [attachmentId: string]; "enable-sticker-fetch": [] }>();
const resolved = shallowRef<AttachmentSummary | null>(null);
const media = computed(() => resolved.value ?? props.attachment);
const blob = shallowRef<Blob | null>(null);
const url = ref<string | null>(null);
const busy = ref(false);
const failed = ref(false);
const blocked = computed(() => props.resolve !== undefined && !props.fetchAvailable && !media.value.content_available);
let generation = 0;
let disposed = false;
const revoke = (): void => {
  if (url.value !== null) URL.revokeObjectURL(url.value);
  url.value = null;
};

const acquire = async (retry = false): Promise<void> => {
  if (busy.value || blocked.value) return;
  const current = generation;
  const id = props.attachment.id;
  busy.value = true;
  failed.value = false;
  try {
    let attachment = media.value;
    if (!attachment.content_available) {
      if (props.resolve === undefined || !props.fetchAvailable) return;
      if (!retry && attachment.failure_code !== null && attachment.failure_code !== "EMOJI_CDN_DISABLED") return;
      attachment = await props.resolve(id);
      if (disposed || current !== generation) return;
      resolved.value = attachment;
      if (!attachment.content_available) { failed.value = true; return; }
    }
    const content = await props.load(id);
    if (disposed || current !== generation) return;
    revoke();
    blob.value = content;
    if (!isWxgfAnimation(content)) url.value = URL.createObjectURL(content);
  } catch {
    if (!disposed && current === generation) failed.value = true;
  } finally {
    if (current === generation) busy.value = false;
  }
};

watch(() => [props.attachment.id, props.attachment.content_available, props.fetchAvailable] as const, () => {
  generation++;
  busy.value = false;
  resolved.value = null;
  blob.value = null;
  failed.value = false;
  revoke();
  void acquire();
}, { immediate: true });
onBeforeUnmount(() => { disposed = true; generation++; revoke(); });
</script>

<template>
  <div class="forward-sticker">
    <button
      v-if="blob !== null && !failed"
      class="merged-forward-media-button is-sticker"
      type="button"
      aria-label="打开表情"
      @click="emit('open', media.id)"
    >
      <WxgfSticker
        v-if="isWxgfAnimation(blob)"
        :blob="blob"
        alt="表情"
        @failed="failed = true"
      />
      <img
        v-else
        class="forward-sticker-image"
        :src="url ?? undefined"
        alt="表情"
        @error="failed = true"
      >
    </button>
    <div
      v-else
      class="forward-sticker-status"
      role="status"
    >
      <span v-if="busy">正在加载表情…</span>
      <template v-else-if="blocked">
        <span>联网获取表情未启用</span>
        <button
          type="button"
          @click="emit('enable-sticker-fetch')"
        >
          启用表情获取
        </button>
      </template>
      <template v-else>
        <span>{{ failed ? '表情暂时无法显示' : '表情内容未保存' }}</span>
        <button
          v-if="resolve !== undefined && fetchAvailable"
          type="button"
          @click="acquire(true)"
        >
          重新获取
        </button>
      </template>
    </div>
  </div>
</template>

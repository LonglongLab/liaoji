<script setup lang="ts">
import { onBeforeUnmount, ref, watch } from "vue";

/**
 * The inline picture for one media item inside a merged forward. It loads the
 * attachment's bytes into an <img> and owns that object URL's whole lifecycle,
 * so the recursive MergedForwardItems tree does not have to. Deliberately not a
 * preview: clicking the surrounding button opens the shared MediaViewer. The
 * `load` function is injected (the view's loadThumbnail, which already falls
 * back to the full artifact when there is no separate thumbnail), and
 * createObjectUrl/revokeObjectUrl are injected too so happy-dom tests -- where
 * they are not guaranteed to exist -- can supply their own.
 */
const props = withDefaults(
  defineProps<{
    load: (attachmentId: string) => Promise<Blob>;
    attachmentId: string;
    alt: string;
    createObjectUrl?: (blob: Blob) => string;
    revokeObjectUrl?: (url: string) => void;
  }>(),
  {
    createObjectUrl: (blob: Blob) => URL.createObjectURL(blob),
    revokeObjectUrl: (url: string) => {
      URL.revokeObjectURL(url);
    },
  },
);

const url = ref<string | null>(null);
const failed = ref(false);
let current: string | null = null;

const revoke = (): void => {
  if (current !== null) {
    props.revokeObjectUrl(current);
    current = null;
  }
};

const load = async (attachmentId: string): Promise<void> => {
  failed.value = false;
  try {
    const blob = await props.load(attachmentId);
    // The id may have changed while we awaited; a stale result must not win.
    if (attachmentId !== props.attachmentId) return;
    revoke();
    current = props.createObjectUrl(blob);
    url.value = current;
  } catch {
    if (attachmentId === props.attachmentId) failed.value = true;
  }
};

watch(
  () => props.attachmentId,
  (attachmentId) => {
    url.value = null;
    void load(attachmentId);
  },
  { immediate: true },
);

onBeforeUnmount(revoke);
</script>

<template>
  <img
    v-if="url !== null"
    class="forward-media-thumb"
    :src="url"
    :alt="alt"
    loading="lazy"
    @error="failed = true"
  >
  <span
    v-else-if="failed"
    class="forward-media-thumb-fallback"
    role="img"
    :aria-label="alt"
  >{{ alt }}</span>
  <span
    v-else
    class="forward-media-thumb-loading"
    role="status"
    aria-label="正在加载"
  />
</template>

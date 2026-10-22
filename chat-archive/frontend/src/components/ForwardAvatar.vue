<script setup lang="ts">
import { computed, inject, onScopeDispose, ref, watch } from "vue";

import { archiveApiKey } from "@/api/injection";
import { useVisibleIds } from "@/composables/useVisibleIds";
import { capabilityState } from "@/lib/capabilities";
import { safeForwardAvatar } from "@/lib/mergedForward";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import { useWorkspace } from "@/lib/workspaceContext";
import { useCapabilityStore } from "@/stores/capabilities";

const props = defineProps<{
  url?: string | null;
  localRef?: string | null;
  blobId?: string | null;
  sender?: string | null;
}>();
const workspace = useWorkspace();
const api = inject(archiveApiKey, null);
const capabilities = workspace === "remote" && api !== null ? useCapabilityStore() : null;
const store = workspace === "remote" && api !== null
  ? sharedAvatarStoreVia(id => api.getRemoteAttachmentUrl(id)) : null;
const visible = useVisibleIds();
const element = ref<HTMLElement | null>(null);
watch(element, el => { visible.observe(el, "avatar"); }, { flush: "post" });
const cached = ref<string | null>(null);
const cacheFailed = ref(false);
const imageFailed = ref(false);
const localFailed = ref(false);
let release: (() => void) | null = null;
const source = computed(() => safeForwardAvatar(props.url));
const local = computed(() => source.value !== null && workspace === "local" && typeof props.localRef === "string"
  && /^\/api\/v1\/avatars\/[\w-]+\?account_id=[\w%.-]+$/.test(props.localRef)
  ? props.localRef : null);
const key = computed(() => source.value !== null && store !== null && capabilities !== null
  && capabilityState(capabilities.deployment, capabilities.states, "attachment-view") === "available"
  && typeof props.blobId === "string" && /^[0-9a-f]{64}$/.test(props.blobId)
  ? props.blobId : null);
watch(() => [props.url, props.localRef, props.blobId], () => {
  imageFailed.value = false; localFailed.value = false;
}, { flush: "sync" });
watch([key, visible.ids], ([id, ids]) => {
  release?.(); release = null;
  cached.value = null; cacheFailed.value = false;
  if (id === null || store === null || (ids !== null && !ids.includes("avatar"))) return;
  release = store.subscribe(id, value => {
    cached.value = value;
    cacheFailed.value = store.failed(id);
  });
}, { immediate: true, flush: "sync" });
onScopeDispose(() => { release?.(); });
const src = computed(() => {
  if (imageFailed.value) return null;
  if (local.value !== null && !localFailed.value) return local.value;
  if (cached.value !== null && !localFailed.value) return cached.value;
  // While the shared cache is loading, do not start a second CDN request.
  if (key.value !== null && !cacheFailed.value && !localFailed.value) return null;
  return source.value;
});
const failed = (): void => {
  if (src.value !== source.value && source.value !== null) localFailed.value = true;
  else imageFailed.value = true;
};
</script>

<template>
  <span
    ref="element"
    class="forward-avatar"
  >
    <img
      v-if="src"
      class="merged-forward-avatar"
      :src="src"
      alt="发送者头像"
      loading="lazy"
      referrerpolicy="no-referrer"
      @error="failed"
    >
    <span
      v-else
      class="merged-forward-avatar is-fallback"
      aria-hidden="true"
    >{{ sender?.slice(0, 1) ?? '?' }}</span>
  </span>
</template>

<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { Minus, Square, Copy, X } from "@lucide/vue";

interface NativeMessages {
  postMessage: (message: object) => void;
  addEventListener: (name: "message", callback: (event: MessageEvent<unknown>) => void) => void;
  removeEventListener: (name: "message", callback: (event: MessageEvent<unknown>) => void) => void;
}
const emit = defineEmits<{ ready: [value: boolean] }>();
const available = ref(false);
const maximized = ref(false);
const maximizeLabel = computed(() => maximized.value ? "还原窗口" : "最大化");
const edges = ["left", "right", "top", "bottom", "top-left", "top-right", "bottom-left", "bottom-right"];
let native: NativeMessages | undefined;
let timer: ReturnType<typeof globalThis.setInterval> | undefined;
let attempts = 0;
const send = (action: string, edge?: string): void => {
  native?.postMessage({ type: "chat-archive-window", action, ...(edge === undefined ? {} : { edge }) });
};
const toggleMaximize = (): void => { if (available.value) send("toggle-maximize"); };
const received = (event: MessageEvent<unknown>): void => {
  const value = event.data;
  if (typeof value !== "object" || value === null || !("type" in value) || value.type !== "chat-archive-window-state"
    || !("maximized" in value) || typeof value.maximized !== "boolean") return;
  available.value = true; maximized.value = value.maximized;
  globalThis.clearInterval(timer); timer = undefined; emit("ready", true);
};
onMounted(() => {
  const host = globalThis as typeof globalThis & { chrome?: { webview?: NativeMessages } };
  native = host.chrome?.webview;
  if (native === undefined || typeof native.postMessage !== "function" || typeof native.addEventListener !== "function") return;
  native.addEventListener("message", received);
  const handshake = (): void => {
    send("state"); attempts++;
    if (attempts >= 20) { globalThis.clearInterval(timer); timer = undefined; }
  };
  handshake();
  if (!available.value) timer = globalThis.setInterval(handshake, 250);
});
onBeforeUnmount(() => { globalThis.clearInterval(timer); native?.removeEventListener("message", received); });
defineExpose({ toggleMaximize });
</script>

<template>
  <div
    v-if="available"
    class="native-window-controls"
    role="group"
    aria-label="窗口控制"
  >
    <button
      type="button"
      aria-label="最小化"
      title="最小化"
      @click="send('minimize')"
    >
      <Minus
        :size="16"
        aria-hidden="true"
      />
    </button>
    <button
      type="button"
      :aria-label="maximizeLabel"
      :title="maximizeLabel"
      @click="toggleMaximize"
    >
      <component
        :is="maximized ? Copy : Square"
        :size="14"
        aria-hidden="true"
      />
    </button>
    <button
      type="button"
      class="native-window-close"
      aria-label="关闭窗口"
      title="关闭窗口"
      @click="send('close')"
    >
      <X
        :size="18"
        aria-hidden="true"
      />
    </button>
  </div>
  <Teleport
    v-if="available && !maximized"
    to="body"
  >
    <div
      v-for="edge in edges"
      :key="edge"
      class="native-resize-edge"
      :class="`native-resize-${edge}`"
      aria-hidden="true"
      @pointerdown.left.prevent="send('resize', edge)"
    />
  </Teleport>
</template>

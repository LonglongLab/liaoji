<script setup lang="ts">
import { inject, nextTick, onBeforeUnmount, onMounted, ref, useId, watch } from "vue";
import { routerKey } from "vue-router";
import { LOGIN_PATH, UNLOCK_PATH } from "@/lib/workspaceRouting";

const props = defineProps<{
  label: string;
  busy: boolean;
  fallbackFocus?: HTMLElement | null;
}>();
const emit = defineEmits<{ close: [] }>();
const router = inject(routerKey, null);
const dialog = ref<HTMLDialogElement | null>(null);
const content = ref<HTMLElement | null>(null);
const statusId = `task-sheet-${useId()}`;
const blocked = ref(false);
let active = true;
let previous: HTMLElement | null = null;
let removeGuard: (() => void) | undefined;
let backdropStart = false;
let securityExit = false;

const requestClose = (): void => {
  if (!active) return;
  if (props.busy) { blocked.value = true; return; }
  active = false;
  emit("close");
};

const outside = (event: MouseEvent | PointerEvent): boolean => {
  const root = dialog.value;
  if (root === null || event.target !== root) return false;
  const rect = root.getBoundingClientRect();
  return event.clientX < rect.left || event.clientX > rect.right || event.clientY < rect.top || event.clientY > rect.bottom;
};
const startPointer = (event: PointerEvent): void => { backdropStart = outside(event); };
const clickBackdrop = (event: MouseEvent): void => {
  if (backdropStart && outside(event)) requestClose();
  backdropStart = false;
};
const beforeUnload = (event: BeforeUnloadEvent): void => {
  if (!active || !props.busy) return;
  event.preventDefault();
};
const trapTab = (event: KeyboardEvent): void => {
  if (event.key !== "Tab") return;
  const root = dialog.value;
  if (root === null) return;
  const items = [...root.querySelectorAll<HTMLElement>('button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled),textarea:not(:disabled),[tabindex="0"]')]
    .filter(item => !item.closest("[hidden],[inert]") && globalThis.getComputedStyle(item).display !== "none" && globalThis.getComputedStyle(item).visibility !== "hidden");
  const first = items[0], last = items[items.length - 1];
  if (first === undefined || last === undefined) { event.preventDefault(); content.value?.focus(); return; }
  const focused = globalThis.document.activeElement;
  if (focused === content.value || !root.contains(focused) || (event.shiftKey ? focused === first : focused === last)) {
    event.preventDefault();
    (event.shiftKey ? last : first).focus();
  }
};
watch(() => props.busy, busy => { if (!busy) blocked.value = false; });

onMounted(() => {
  previous = globalThis.document.activeElement instanceof HTMLElement ? globalThis.document.activeElement : null;
  // Native modal isolation covers the whole application, including teleported controls.
  dialog.value?.showModal();
  (dialog.value?.querySelector<HTMLElement>("button:not(:disabled),a[href],input:not(:disabled),select:not(:disabled)") ?? content.value)?.focus();
  removeGuard = router?.beforeEach(to => {
    if (to.path === LOGIN_PATH || to.path === UNLOCK_PATH) {
      securityExit = true;
      active = false;
      dialog.value?.close();
      return true;
    }
    if (!active) return true;
    requestClose();
    return false;
  });
  globalThis.addEventListener("beforeunload", beforeUnload);
});

onBeforeUnmount(() => {
  active = false;
  removeGuard?.();
  globalThis.removeEventListener("beforeunload", beforeUnload);
  dialog.value?.close();
  if (securityExit) return;
  const trigger = previous;
  const fallback = props.fallbackFocus;
  void nextTick(() => {
    const target = trigger?.isConnected && !trigger.matches(":disabled,[inert]") && trigger !== globalThis.document.body ? trigger : fallback;
    if (target?.isConnected) target.focus({ preventScroll: true });
  });
});
</script>

<template>
  <dialog
    ref="dialog"
    class="transcript-task-sheet"
    :aria-label="label"
    aria-modal="true"
    :aria-busy="busy"
    :aria-describedby="busy || blocked ? statusId : undefined"
    @cancel.prevent="requestClose"
    @pointerdown.stop="startPointer"
    @click.stop="clickBackdrop"
    @keydown.stop="trapTab"
  >
    <div
      ref="content"
      tabindex="-1"
    >
      <slot :close="requestClose" />
    </div>
    <p
      :id="statusId"
      class="transcript-task-status"
      role="status"
    >
      {{ busy || blocked ? "正在生成，请等待完成后再关闭。" : "" }}
    </p>
  </dialog>
</template>

<style scoped>
.transcript-task-sheet { width: min(680px, calc(100% - 32px)); max-height: 85dvh; margin: auto; padding: 0; overflow-y: auto; overscroll-behavior: contain; border: 1px solid var(--rule); border-radius: var(--radius-card); background: var(--surface); color: var(--ink); font: inherit; box-shadow: var(--shadow-pop); }
.transcript-task-sheet::backdrop { background: var(--scrim); }
.transcript-task-sheet :deep(.export-sheet), .transcript-task-sheet :deep(.share-sheet) { max-height: none; overflow: visible; border-top: 0; }
.transcript-task-status { margin: 0; padding: 0 20px 16px; color: var(--ink-soft); font-size: 13px; }
.transcript-task-status:empty { display: none; }
@media (max-width: 47.99rem) {
  .transcript-task-sheet { width: 100%; max-width: none; margin: auto 0 0; padding-bottom: env(safe-area-inset-bottom); border-right: 0; border-bottom: 0; border-left: 0; border-radius: var(--radius-card) var(--radius-card) 0 0; }
}
</style>

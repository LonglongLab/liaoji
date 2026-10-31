import { getCurrentInstance, onBeforeUnmount, ref, watch, type Ref } from "vue";
import type { Router } from "vue-router";

/** No draft values enter this guard: it only knows whether edits exist. */
export const useUnsavedChanges = (dirty: Ref<boolean>, busy: Ref<boolean>) => {
  // Panels are also mounted standalone; $router is present only in a router-enabled host.
  const host = getCurrentInstance()?.appContext.config.globalProperties as { $router?: Router } | undefined;
  const router = host?.$router;
  const open = ref(false);
  const blocked = ref(false);
  let pending: ((allow: boolean) => void) | null = null;
  const finish = (allow: boolean): void => {
    const resolve = pending;
    pending = null;
    open.value = false;
    resolve?.(allow);
  };
  const ask = (): boolean | Promise<boolean> => {
    if (busy.value) { blocked.value = true; return false; }
    if (!dirty.value) return true;
    // Rapid repeated navigation replaces the pending destination without stacking dialogs.
    finish(false);
    open.value = true;
    return new Promise<boolean>(resolve => { pending = resolve; });
  };
  const removeGuard = router?.beforeEach((to, from) => {
    // Security-driven sign-in/lock transitions must never wait on an editable draft.
    if (["/login", "/unlock"].includes(to.path)) { finish(false); return true; }
    if (to.fullPath === from.fullPath) return true;
    return ask();
  });
  const beforeUnload = (event: BeforeUnloadEvent): void => {
    if (!dirty.value && !busy.value) return;
    event.preventDefault();
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- Required for legacy WebView beforeunload prompts.
    event.returnValue = "";
  };
  globalThis.addEventListener("beforeunload", beforeUnload);
  watch(busy, value => { if (!value) blocked.value = false; });
  onBeforeUnmount(() => {
    finish(false);
    removeGuard?.();
    globalThis.removeEventListener("beforeunload", beforeUnload);
  });
  return {
    open, blocked,
    keepEditing: (): void => finish(false),
    discard: (): void => { if (!busy.value) finish(true); },
    discardThen: async (action: () => void): Promise<void> => { if (await ask()) action(); },
  };
};

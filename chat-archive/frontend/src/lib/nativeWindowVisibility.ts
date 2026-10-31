import { focusManager } from "@tanstack/vue-query";

/** Native WebViews do not all report minimize/restore as document visibility. */
export const installNativeWindowVisibility = (): (() => void) => {
  const state = globalThis as typeof globalThis & { __chatArchiveWindowVisible?: boolean };
  if (typeof state.__chatArchiveWindowVisible === "boolean") focusManager.setFocused(state.__chatArchiveWindowVisible);
  const changed = (event: Event): void => {
    if (!(event instanceof CustomEvent) || typeof event.detail !== "boolean") return;
    state.__chatArchiveWindowVisible = event.detail;
    focusManager.setFocused(event.detail);
  };
  globalThis.addEventListener("chat-archive:window-visibility", changed);
  return () => {
    globalThis.removeEventListener("chat-archive:window-visibility", changed);
    focusManager.setFocused(undefined);
  };
};

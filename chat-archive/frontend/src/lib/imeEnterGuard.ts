/** Capture before form defaults and component Enter handlers, including teleported dialogs. */
export const installImeEnterGuard = (root: Document): (() => void) => {
  const states = new WeakMap<Element, { composing: boolean; endedAt: number }>();
  const inputOf = (event: Event): Element | null => event.target instanceof Element
    ? event.target.closest('input, textarea, [contenteditable="true"]') : null;
  const start = (event: Event): void => {
    const input = inputOf(event);
    if (input !== null) states.set(input, { composing: true, endedAt: -Infinity });
  };
  const end = (event: Event): void => {
    const input = inputOf(event);
    if (input !== null) states.set(input, { composing: false, endedAt: performance.now() });
  };
  const guard = (event: KeyboardEvent): void => {
    // eslint-disable-next-line @typescript-eslint/no-deprecated -- Legacy IMEs report composition Enter as 229.
    const legacyCode = event.keyCode;
    if (event.key !== "Enter" && event.code !== "Enter" && event.code !== "NumpadEnter" && legacyCode !== 13) return;
    const input = inputOf(event);
    if (input === null) return;
    const state = states.get(input);
    if (event.isComposing || legacyCode === 229 || state?.composing
      || performance.now() - (state?.endedAt ?? -Infinity) < 500) {
      event.preventDefault();
      event.stopImmediatePropagation();
    }
  };
  root.addEventListener("compositionstart", start, true);
  root.addEventListener("compositionend", end, true);
  root.addEventListener("keydown", guard, true);
  root.addEventListener("keyup", guard, true);
  return () => {
    root.removeEventListener("compositionstart", start, true);
    root.removeEventListener("compositionend", end, true);
    root.removeEventListener("keydown", guard, true);
    root.removeEventListener("keyup", guard, true);
  };
};

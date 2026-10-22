import { ref, watch, nextTick, type Ref } from "vue";

const FOCUSABLE = [
  "a[href]",
  "button:not([disabled])",
  'input:not([disabled]):not([type="hidden"])',
  "select:not([disabled])",
  "textarea:not([disabled])",
  '[tabindex]:not([tabindex="-1"])',
].join(",");

export interface ModalFocus {
  container: Ref<HTMLElement | null>;
  initial: Ref<Pick<HTMLElement, "focus"> | null>;
  onKeydown: (event: KeyboardEvent) => void;
}

export const useModalFocus = (open: Ref<boolean>, close: () => void): ModalFocus => {
  const container = ref<HTMLElement | null>(null);
  const initial = ref<Pick<HTMLElement, "focus"> | null>(null);
  let previous: HTMLElement | null = null;

  watch(
    open,
    async (isOpen) => {
      if (isOpen) {
        previous =
          document.activeElement instanceof HTMLElement ? document.activeElement : null;
        await nextTick();
        initial.value?.focus();
      } else {
        previous?.focus();
        previous = null;
      }
    },
    { immediate: true },
  );

  const onKeydown = (event: KeyboardEvent): void => {
    if (event.key === "Escape") {
      event.preventDefault();
      close();
      return;
    }
    if (event.key !== "Tab") return;
    const root = container.value;
    if (root === null) return;
    const items = [...root.querySelectorAll(FOCUSABLE)].filter(
      (item): item is HTMLElement => item instanceof HTMLElement,
    );
    if (items.length === 0) return;
    const first = items[0];
    const last = items[items.length - 1];
    if (first === undefined || last === undefined) return;
    const active = document.activeElement;
    if (event.shiftKey && active === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && active === last) {
      event.preventDefault();
      first.focus();
    }
  };

  return { container, initial, onKeydown };
};

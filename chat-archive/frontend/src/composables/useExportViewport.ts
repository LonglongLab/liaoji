import { computed, onMounted, onUnmounted, ref, type CSSProperties } from "vue";

/** Layout follows the visible viewport; dvh alone does not track every mobile IME. */
export const useExportViewport = () => {
  const metrics = ref<{ height: number; top: number } | null>(null);
  const update = (): void => {
    const viewport = window.visualViewport ?? null;
    metrics.value = window.innerWidth <= 900 && (viewport === null || Math.abs(viewport.scale-1) < .05)
      ? { height: viewport?.height ?? window.innerHeight, top: viewport?.offsetTop ?? 0 } : null;
  };
  onMounted(() => {
    update();
    window.addEventListener("resize",update);
    window.visualViewport?.addEventListener("resize",update);
    window.visualViewport?.addEventListener("scroll",update);
  });
  onUnmounted(() => {
    window.removeEventListener("resize",update);
    window.visualViewport?.removeEventListener("resize",update);
    window.visualViewport?.removeEventListener("scroll",update);
  });
  const style = computed<CSSProperties>(() => metrics.value === null ? {} : {
    "--export-visible-height": `${String(metrics.value.height)}px`, "--export-visible-top": `${String(metrics.value.top)}px`,
  });
  return { style };
};

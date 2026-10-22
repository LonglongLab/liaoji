import { ref, watch, type Ref } from "vue";

export const useDebouncedTrimmed = (source: Ref<string>, delayMs = 300): Ref<string> => {
  const output = ref("");
  let timer: ReturnType<typeof setTimeout> | undefined;
  watch(source, (value) => {
    clearTimeout(timer);
    timer = setTimeout(() => {
      output.value = value.trim();
    }, delayMs);
  });
  return output;
};

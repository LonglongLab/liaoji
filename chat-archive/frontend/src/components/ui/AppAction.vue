<script setup lang="ts">
import Button from "primevue/button";
import { computed, ref, watch } from "vue";
import { RouterLink, type RouteLocationRaw } from "vue-router";
import ButtonProgress from "./ButtonProgress.vue";

const props = withDefaults(defineProps<{
  to?: RouteLocationRaw | null;
  href?: string | null;
  variant?: "primary" | "secondary" | "quiet" | "inline";
  disabled?: boolean;
  loading?: boolean;
  type?: "button" | "submit" | "reset";
}>(), { to: null, href: null, variant: "quiet", disabled: false, loading: false, type: "button" });

const isLink = computed(() => props.to !== null || props.href !== null);
const blocked = computed(() => props.disabled || props.loading);
const control = ref<{ $el: unknown } | null>(null);
defineExpose({ focus: (options?: FocusOptions): void => {
  const element = control.value?.$el;
  if (element instanceof HTMLElement) element.focus(options);
} });
const guard = (event: MouseEvent): void => {
  if (!blocked.value) return;
  event.preventDefault();
  event.stopImmediatePropagation();
};
watch(
  [(): unknown => control.value?.$el, blocked],
  ([element, isBlocked], _prev, onCleanup) => {
    if (!isBlocked || !(element instanceof HTMLElement)) return;
    element.addEventListener("click", guard, { capture: true });
    onCleanup(() => element.removeEventListener("click", guard, { capture: true }));
  },
  { flush: "sync", immediate: true },
);
</script>

<template>
  <Button
    ref="control"
    :as="to !== null && !blocked ? RouterLink : isLink ? 'a' : 'BUTTON'"
    v-bind="isLink && blocked ? { role: 'link' } : to !== null ? { to } : href !== null ? { href } : {}"
    :type="isLink ? undefined : type"
    :disabled="isLink ? undefined : blocked"
    :aria-disabled="blocked ? true : undefined"
    :aria-busy="loading ? true : undefined"
    :tabindex="isLink && blocked ? -1 : undefined"
    class="app-action"
    :class="`app-action--${variant}`"
    unstyled
  >
    <ButtonProgress
      v-if="loading"
      :busy="true"
    >
      <slot />
    </ButtonProgress>
    <slot v-else />
  </Button>
</template>

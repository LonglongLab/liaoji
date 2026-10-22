<script setup lang="ts">
import { computed, ref, watch } from "vue";

const props = withDefaults(
  defineProps<{
    name: string;
    src?: string | null | undefined;
    size?: "sm" | "md" | "lg";
    alt?: string;
  }>(),
  {
    src: null,
    size: "md",
    alt: "头像",
  },
);

const imageFailed = ref(false);

const safeSrc = computed(() => {
  const value = props.src?.trim() ?? "";
  if (
    value === "" ||
    (!value.startsWith("/api/") && !value.startsWith("data:image/") && !value.startsWith("blob:"))
  ) {
    return null;
  }
  return value;
});

const initial = computed(() => Array.from(props.name.trim())[0] ?? "?");

watch(safeSrc, () => {
  imageFailed.value = false;
});
</script>

<template>
  <span
    class="avatar-badge"
    data-feedback-mask="avatar"
    :class="`is-${size}`"
    :aria-label="alt"
  >
    <img
      v-if="safeSrc !== null && !imageFailed"
      :src="safeSrc"
      :alt="alt"
      @error="imageFailed = true"
    >
    <span
      v-else
      aria-hidden="true"
    >
      {{ initial }}
    </span>
  </span>
</template>

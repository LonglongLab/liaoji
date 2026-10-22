<script setup lang="ts">
import { computed, ref, watch } from "vue";

import type { MessageSummary } from "@/api/archiveApi";

type AppCard = NonNullable<MessageSummary["app_card"]>;

/**
 * Legacy link-like cards share title/des/url; type 51 also carries finderFeed
 * source media references. Keep the kind visible without playback status copy.
 */
type LinkishKind = "link" | "miniprogram" | "channel";

const KIND_LABELS = {
  link: "链接",
  miniprogram: "小程序",
  channel: "视频号",
} as const;

const props = withDefaults(defineProps<{
  card: AppCard;
  fallbackUrl?: string | null;
}>(), { fallbackUrl: null });

const kind = computed<LinkishKind>(() => {
  const value = props.card.kind;
  return value === "miniprogram" || value === "channel" ? value : "link";
});
const kindLabel = computed(() => KIND_LABELS[kind.value]);
/** 网页链接不必自称「链接」——页脚已经画着链条图标；小程序和视频号要说。 */
const kindPrefix = computed(() => (kind.value === "link" ? null : kindLabel.value));

const normalizeHttpUrl = (value: string | null | undefined): string | null => {
  if (typeof value !== "string" || value.trim() === "") return null;
  try {
    const parsed = new URL(value);
    return parsed.protocol === "http:" || parsed.protocol === "https:"
      ? parsed.href
      : null;
  } catch {
    return null;
  }
};

const safeUrl = computed(() =>
  normalizeHttpUrl(props.card.url) ?? normalizeHttpUrl(props.fallbackUrl),
);

const title = computed(() => props.card.title.trim() || kindLabel.value);
const subtitle = computed(() => {
  const value = props.card.subtitle?.trim() ?? "";
  // 不少卡片的 des 与 title 一字不差；把同一句话印两遍不是信息，是噪音。
  return value === "" || value === title.value ? null : value;
});
const host = computed(() => {
  if (safeUrl.value === null) return null;
  try {
    return new URL(safeUrl.value).hostname;
  } catch {
    return null;
  }
});
const source = computed(() => {
  const value = props.card.source_name?.trim() ?? "";
  return value === "" ? host.value ?? kindLabel.value : value;
});
const accessibleLabel = computed(() =>
  safeUrl.value === null ? `链接预览：${title.value}` : `打开链接：${title.value}`,
);
const coverFailed = ref(false);
const avatarFailed = ref(false);
const coverUrl = computed(() => kind.value === "channel" ? normalizeHttpUrl(props.card.cover_url) : null);
const avatarUrl = computed(() => kind.value === "channel" ? normalizeHttpUrl(props.card.avatar_url) : null);
watch(coverUrl, () => { coverFailed.value = false; });
watch(avatarUrl, () => { avatarFailed.value = false; });
const channelMedia = computed(() => kind.value === "channel" &&
  (coverUrl.value !== null || props.card.duration_seconds != null));
const aspectRatio = computed(() => {
  const width = props.card.media_width;
  const height = props.card.media_height;
  return typeof width === "number" && typeof height === "number" && width > 0 && height > 0
    ? `${String(width)} / ${String(height)}` : "9 / 16";
});
const duration = computed(() => {
  const seconds = props.card.duration_seconds;
  if (typeof seconds !== "number" || !Number.isInteger(seconds) || seconds <= 0) return null;
  return `${String(Math.floor(seconds / 60)).padStart(2, "0")}:${String(seconds % 60).padStart(2, "0")}`;
});
</script>

<template>
  <component
    :is="safeUrl === null ? 'div' : 'a'"
    data-feedback-mask="message-body"
    class="link-preview-card"
    :class="[`is-${kind}`, { 'is-static': safeUrl === null, 'has-channel-media': channelMedia }]"
    :href="safeUrl ?? undefined"
    :target="safeUrl === null ? undefined : '_blank'"
    :rel="safeUrl === null ? undefined : 'noreferrer'"
    :aria-label="accessibleLabel"
    :title="kind === 'channel' ? undefined : safeUrl ?? undefined"
  >
    <span
      v-if="coverUrl !== null && !coverFailed"
      class="channel-cover-frame"
      :style="{ aspectRatio }"
    >
      <img
        class="channel-cover"
        :src="coverUrl"
        alt="视频号封面"
        loading="lazy"
        referrerpolicy="no-referrer"
        @error="coverFailed = true"
      >
      <span
        v-if="safeUrl !== null"
        class="channel-play"
        aria-hidden="true"
      >▶</span>
      <span
        v-if="duration !== null"
        class="channel-duration"
      >{{ duration }}</span>
    </span>
    <span class="link-preview-copy">
      <strong class="link-preview-title">{{ title }}</strong>
      <span
        v-if="subtitle !== null"
        class="link-preview-subtitle"
      >
        {{ subtitle }}
      </span>
    </span>
    <span class="link-preview-source">
      <img
        v-if="avatarUrl !== null && !avatarFailed"
        class="channel-avatar"
        :src="avatarUrl"
        alt=""
        loading="lazy"
        referrerpolicy="no-referrer"
        @error="avatarFailed = true"
      >
      <svg
        v-if="kind === 'miniprogram'"
        class="link-preview-mark"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <rect
          x="3"
          y="3"
          width="7.5"
          height="7.5"
          rx="1.6"
        />
        <rect
          x="13.5"
          y="3"
          width="7.5"
          height="7.5"
          rx="1.6"
        />
        <rect
          x="3"
          y="13.5"
          width="7.5"
          height="7.5"
          rx="1.6"
        />
        <rect
          x="13.5"
          y="13.5"
          width="7.5"
          height="7.5"
          rx="1.6"
        />
      </svg>
      <svg
        v-else-if="kind === 'channel'"
        class="link-preview-mark"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <rect
          x="2.5"
          y="5"
          width="19"
          height="14"
          rx="3"
        />
        <path d="M10 9.4v5.2l4.6-2.6z" />
      </svg>
      <svg
        v-else
        class="link-preview-mark"
        width="14"
        height="14"
        viewBox="0 0 24 24"
        fill="none"
        stroke="currentColor"
        stroke-width="1.8"
        stroke-linecap="round"
        stroke-linejoin="round"
        aria-hidden="true"
      >
        <path d="M10 13a5 5 0 0 0 7.07.07l2-2a5 5 0 0 0-7.07-7.07l-1.15 1.15" />
        <path d="M14 11a5 5 0 0 0-7.07-.07l-2 2A5 5 0 0 0 7 20l1.15-1.15" />
      </svg>
      <span
        v-if="kindPrefix !== null"
        class="link-preview-kind"
      >{{ kindPrefix }}</span>
      <span class="link-preview-origin">{{ source }}</span>
      <span
        v-if="duration !== null && (coverUrl === null || coverFailed)"
        class="channel-duration"
      >{{ duration }}</span>
    </span>
  </component>
</template>

<style scoped>
.has-channel-media { width: 248px; max-width: 100%; overflow: hidden; padding: 0; gap: 0; }
.has-channel-media .link-preview-copy { padding: 12px 14px; }
.has-channel-media .link-preview-source { padding: 10px 14px; }
.channel-cover-frame { display: block; position: relative; width: 100%; max-height: 440px; background: var(--surface, #eee9de); }
.channel-cover { display: block; width: 100%; height: 100%; object-fit: contain; }
.channel-avatar { width: 22px; height: 22px; border-radius: 50%; object-fit: cover; flex-shrink: 0; }
.channel-duration { font-size: 12px; margin-left: auto; }
.channel-cover-frame .channel-duration { position: absolute; right: 9px; bottom: 9px; background: #343a37; color: white; padding: 0 6px; border-radius: 4px; }
.channel-play { position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%); color: white; font-size: 25px; border: 1px solid white; border-radius: 50%; width: 48px; height: 48px; display: grid; place-items: center; background: #0004; }
</style>

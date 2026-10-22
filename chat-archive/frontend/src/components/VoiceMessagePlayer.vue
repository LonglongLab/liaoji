<script setup lang="ts">
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import { formatVoiceDuration } from "@/lib/recordPreview";

const props = defineProps<{
  loadAudio: () => Promise<Blob>;
  label: string;
  durationMs?: number | null | undefined;
}>();

const audioRef = ref<HTMLAudioElement | null>(null);
const sourceUrl = ref<string | null>(null);
const loading = ref(false);
const playing = ref(false);
const duration = ref<number | null>(
  props.durationMs !== null && props.durationMs !== undefined && props.durationMs >= 0
    ? props.durationMs / 1000
    : null,
);
const errorText = ref<string | null>(null);

const actionLabel = computed(() => {
  if (loading.value) return `正在加载${props.label}`;
  if (errorText.value !== null) return `重试播放${props.label}`;
  return `${playing.value ? "暂停" : "播放"}${props.label}`;
});

const ensureSource = async (): Promise<void> => {
  if (sourceUrl.value !== null) return;
  const blob = await props.loadAudio();
  sourceUrl.value = URL.createObjectURL(blob);
  await nextTick();
};

const togglePlayback = async (): Promise<void> => {
  const audio = audioRef.value;
  if (audio === null || loading.value) return;
  errorText.value = null;
  if (playing.value) {
    audio.pause();
    return;
  }
  loading.value = true;
  try {
    await ensureSource();
    await audio.play();
  } catch {
    playing.value = false;
    errorText.value = "暂时无法播放语音，请重试。";
  } finally {
    loading.value = false;
  }
};

const syncDuration = (event: Event): void => {
  const audio = event.currentTarget;
  if (audio instanceof HTMLAudioElement && Number.isFinite(audio.duration)) {
    duration.value = audio.duration;
  }
};

watch(
  () => props.durationMs,
  (value) => {
    if (duration.value === null && value !== null && value !== undefined && value >= 0) {
      duration.value = value / 1000;
    }
  },
);

onBeforeUnmount(() => {
  if (sourceUrl.value !== null) URL.revokeObjectURL(sourceUrl.value);
});
</script>

<template>
  <div
    class="voice-message-player"
    :class="{ 'is-playing': playing, 'is-loading': loading, 'has-error': errorText !== null }"
  >
    <button
      data-feedback-mask="file-name"
      class="voice-toggle"
      type="button"
      :aria-label="actionLabel"
      :aria-busy="loading"
      @click="togglePlayback"
    >
      <!-- 取一条语音要先从微信的媒体库里解出来，真机上常有几秒。从前这段等待
           在屏幕上没有任何痕迹——图标还是「▶」，波形还是静的——于是点下去像是
           没点着，人会反复点。转成转圈，等待才是看得见的。 -->
      <span
        v-if="loading"
        class="voice-play-icon voice-spinner"
        aria-hidden="true"
      />
      <span
        v-else
        class="voice-play-icon"
        aria-hidden="true"
      >{{ playing ? "Ⅱ" : "▶" }}</span>
      <span
        class="voice-wave"
        aria-hidden="true"
      >
        <i
          v-for="index in 10"
          :key="index"
        />
      </span>
      <span class="voice-duration">{{ formatVoiceDuration(duration) }}</span>
    </button>
    <audio
      ref="audioRef"
      class="voice-native"
      :src="sourceUrl ?? undefined"
      preload="metadata"
      playsinline
      tabindex="-1"
      aria-hidden="true"
      @loadedmetadata="syncDuration"
      @durationchange="syncDuration"
      @play="playing = true"
      @pause="playing = false"
      @ended="playing = false"
      @error="errorText = '暂时无法播放语音，请重试。'; playing = false"
    />
    <p
      v-if="errorText !== null"
      class="voice-error"
      role="alert"
    >
      {{ errorText }}
    </p>
  </div>
</template>

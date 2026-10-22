<script setup lang="ts">
import { computed } from "vue";

import type { BundleKind, BundlePlan } from "@/lib/exportDocument/bundle";
import type { ExportFormat } from "@/lib/exportDocument/format";
import { formatBytes } from "@/utils/format";

/**
 * 选导出成什么（PSU-2.169；PSU-2.179 起「HTML」换成「网页（压缩包）」）。
 *
 * 压缩包那一项会先把**按类的个数与大小**说在前面（原型 `zip-export.html` 第 5 块）：
 * 附件动辄几十 MB，点下去之前就该知道。大小是估的——原图与文件用归档里记的字节数，
 * 缩略图与 mp3 按经验值——所以一律写「约」；有不知道大小的就写「至少」。
 * 长图那一项仍然不报大小：它要渲染完才知道，报一个不敢担保的数不如不报。
 */
const props = defineProps<{
  count: number;
  /** 这批里能带上的图片张数。 */
  imageCount: number;
  conversationName: string;
  format: ExportFormat;
  useOriginals: boolean;
  /** 压缩包的清单；格式不是压缩包时不用。 */
  plan: BundlePlan | null;
  includeVideos: boolean;
  /** 本机语音转成 mp3；远端原样是 wav（PSU-2.178 的 profile 差异）。 */
  voiceAsMp3: boolean;
  /** 导出过程中的那句进度，比如「正在取附件 3/9」。 */
  progress: string | null;
  busy: boolean;
}>();

const emit = defineEmits<{
  "update:format": [format: ExportFormat];
  "update:use-originals": [value: boolean];
  "update:include-videos": [value: boolean];
  confirm: [];
  close: [];
}>();

/** 超过这么大加一句黄色提醒，但不拦（用户在原型上没有反对这条）。 */
const LARGE_BUNDLE_BYTES = 200 * 1024 * 1024;

const originalsNote = computed(() =>
  props.imageCount === 0
    ? "这批里没有图片"
    : `这批里有 ${String(props.imageCount)} 张图`,
);

const confirmLabel = computed(() => (props.format === "bundle" ? "导出压缩包" : "存为图片"));

interface ContentRow {
  kind: BundleKind;
  label: string;
  size: string;
}

const UNIT: Record<BundleKind, [string, string]> = {
  image: ["张", "图片"],
  audio: ["条", "语音"],
  video: ["段", "视频"],
  file: ["个", "文件"],
};

const rows = computed<ContentRow[]>(() => {
  const plan = props.plan;
  if (plan === null) return [];
  return (["image", "audio", "video", "file"] as const)
    .filter((kind) => plan.counts[kind] > 0)
    .map((kind) => {
      const [unit, word] = UNIT[kind];
      const suffix = kind === "audio" && props.voiceAsMp3 ? "（转成 mp3）" : "";
      const size =
        kind === "video" && !props.includeVideos
          ? "只留封面"
          : sizeWords(plan.bytes[kind], plan.unknownByKind[kind], plan.included[kind]);
      return { kind, label: `${word} ${String(plan.counts[kind])} ${unit}${suffix}`, size };
    });
});

/**
 * 一类的大小怎么说：全都知道＝「约 X」；有几条不知道＝「至少 X」；
 * 一条都不知道＝「大小未知」（远端的文件常常如此）——绝不写成「约 0 B」。
 */
const sizeWords = (bytes: number, unknown: number, included: number): string => {
  if (included === 0) return "都拿不到了";
  if (unknown >= included) return "大小未知";
  return unknown > 0 ? `至少 ${formatBytes(bytes)}` : `约 ${formatBytes(bytes)}`;
};

const totalLabel = computed(() => {
  const plan = props.plan;
  if (plan === null) return "";
  const size = formatBytes(plan.totalBytes);
  return plan.unknownSizes > 0 ? `至少 ${size}` : `约 ${size}`;
});

const unavailableNote = computed(() => {
  const plan = props.plan;
  if (plan === null) return null;
  const parts = (["image", "audio", "video", "file"] as const)
    .filter((kind) => plan.unavailable[kind] > 0)
    .map((kind) => `${String(plan.unavailable[kind])} ${UNIT[kind][0]}${UNIT[kind][1]}`);
  return parts.length === 0
    ? null
    : `${parts.join("、")}本机和微信里都已经没有了，网页里会标成「没能带上」。`;
});
</script>

<template>
  <section
    class="export-sheet"
    role="dialog"
    aria-label="选择导出格式"
  >
    <header class="export-sheet-head">
      <strong>导出 {{ count }} 条</strong>
      <span data-feedback-mask="conversation-name">{{ conversationName }}</span>
      <button
        type="button"
        class="export-sheet-close"
        aria-label="取消导出"
        @click="emit('close')"
      >
        ✕
      </button>
    </header>

    <div class="export-sheet-formats">
      <button
        type="button"
        class="export-format"
        :class="{ 'is-on': format === 'bundle' }"
        :aria-pressed="format === 'bundle'"
        @click="emit('update:format', 'bundle')"
      >
        <span class="export-format-name">网页（压缩包）</span>
        <small>解压后双击网页就能看。图片能放大，语音、视频能播，文件能打开，断网也行</small>
      </button>
      <button
        type="button"
        class="export-format"
        :class="{ 'is-on': format === 'image' }"
        :aria-pressed="format === 'image'"
        @click="emit('update:format', 'image')"
      >
        <span class="export-format-name">图片（长图）</span>
        <small>
          发给人看；太长的话会按消息之间的空隙切成多张，打包成一个 zip。
          嵌套的聊天记录就地展开，语音、视频只留样子
        </small>
      </button>
    </div>

    <div
      v-if="format === 'bundle' && plan !== null && rows.length > 0"
      class="export-contents"
      aria-label="压缩包里会有什么"
    >
      <div
        v-for="row in rows"
        :key="row.kind"
        class="export-contents-row"
      >
        <span class="export-contents-label">{{ row.label }}</span>
        <span class="export-contents-size">{{ row.size }}</span>
        <button
          v-if="row.kind === 'video'"
          type="button"
          role="switch"
          class="export-switch"
          :class="{ 'is-on': includeVideos }"
          :aria-checked="includeVideos"
          aria-label="带上视频"
          @click="emit('update:include-videos', !includeVideos)"
        />
      </div>
      <div class="export-contents-row is-total">
        <span class="export-contents-label">预计大小</span>
        <span class="export-contents-size">{{ totalLabel }}</span>
      </div>
    </div>

    <div class="export-sheet-toggle">
      <span class="export-sheet-toggle-label">
        用原图
        <small>{{ originalsNote }}</small>
      </span>
      <button
        type="button"
        role="switch"
        class="export-switch"
        :class="{ 'is-on': useOriginals }"
        :aria-checked="useOriginals"
        :disabled="imageCount === 0"
        aria-label="用原图"
        @click="emit('update:use-originals', !useOriginals)"
      />
    </div>
    <p class="export-sheet-note">
      缺省用微信自带的缩略图，够看也够发。勾了原图就原样带上原图，文件会明显变大。
    </p>

    <template v-if="format === 'bundle' && plan !== null">
      <p
        v-if="plan.refetchCount > 0"
        class="export-sheet-warn"
      >
        有 {{ plan.refetchCount }} 个附件本机已经清掉了。导出时会先去微信的文件里重新取，要多花一点时间。
      </p>
      <p
        v-if="unavailableNote !== null"
        class="export-sheet-warn"
      >
        {{ unavailableNote }}
      </p>
      <p
        v-if="plan.totalBytes > LARGE_BUNDLE_BYTES"
        class="export-sheet-warn"
      >
        压缩包比较大（{{ totalLabel }}），打包要一会儿；不需要视频的话可以把「视频」关掉。
      </p>
    </template>

    <button
      type="button"
      class="export-sheet-go"
      :disabled="busy"
      @click="emit('confirm')"
    >
      {{ busy ? (progress ?? "正在生成…") : confirmLabel }}
    </button>
  </section>
</template>

<style scoped>
/* 与分享面板同一个取法：底色贴满，内容收在中间 560px 一列。 */
.export-sheet {
  padding: 14px max(14px, calc((100% - 560px) / 2)) 16px;
  border-top: 1px solid var(--rule-soft);
  background: var(--surface);
}

.export-sheet-head {
  position: relative;
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin-bottom: 10px;
  padding-right: 28px;
}

.export-sheet-head strong {
  font-size: 15px;
}

.export-sheet-head span {
  overflow: hidden;
  color: var(--ink-soft);
  font-size: 12.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.export-sheet-close {
  position: absolute;
  top: -2px;
  right: 0;
  padding: 2px 6px;
  border: 0;
  border-radius: 5px;
  background: none;
  color: var(--ink-faint);
  font: inherit;
  cursor: pointer;
}

.export-sheet-formats {
  display: grid;
  gap: 8px;
}

.export-format {
  display: block;
  padding: 10px 12px;
  border: 1.5px solid var(--rule-soft);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
  color: var(--ink);
  font: inherit;
  text-align: left;
  cursor: pointer;
}

.export-format.is-on {
  border-color: var(--accent-solid);
  background: var(--safe-bg);
}

.export-format-name {
  display: block;
  font-size: 14px;
  font-weight: 600;
}

.export-format small {
  color: var(--ink-soft);
}

.export-sheet-toggle {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  margin-top: 10px;
  padding: 9px 12px;
  border: 1px solid var(--rule-soft);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
}

.export-sheet-toggle-label {
  font-size: 13.5px;
}

.export-sheet-toggle-label small {
  display: block;
  color: var(--ink-soft);
}

.export-switch {
  position: relative;
  width: 42px;
  height: 24px;
  flex: none;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: var(--track);
  cursor: pointer;
  transition: background 0.15s;
}

.export-switch::after {
  position: absolute;
  top: 2px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  box-shadow: 0 1px 2px rgb(0 0 0 / 0.2);
  content: "";
  transition: transform 0.15s;
}

.export-switch.is-on {
  border-color: var(--accent-solid);
  background: var(--accent-solid);
}

.export-switch.is-on::after {
  transform: translateX(18px);
}

.export-switch:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.export-sheet-note {
  margin: 8px 0 0;
  color: var(--ink-faint);
  font-size: 12.5px;
}

.export-sheet-go {
  width: 100%;
  margin-top: 12px;
  padding: 12px;
  border: 0;
  border-radius: var(--radius-btn);
  background: var(--accent-solid);
  color: #fff;
  font: inherit;
  font-size: 15px;
  font-weight: 600;
  cursor: pointer;
}

.export-sheet-go:disabled {
  cursor: progress;
  opacity: 0.6;
}

.export-contents {
  margin-top: 10px;
  border: 1px solid var(--rule-soft);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
}

.export-contents-row {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 7px 12px;
  border-top: 1px solid var(--rule-soft);
  font-size: 13px;
}

.export-contents-row:first-child {
  border-top: 0;
}

.export-contents-label {
  flex: 1;
  color: var(--ink);
}

.export-contents-size {
  color: var(--ink-soft);
  white-space: nowrap;
}

.export-contents-row.is-total {
  background: var(--surface-sunken);
  font-weight: 600;
}

.export-contents-row.is-total .export-contents-size {
  color: var(--ink);
}

.export-sheet-warn {
  margin: 8px 0 0;
  padding: 7px 10px;
  border: 1px solid var(--warn-border);
  border-radius: var(--radius-btn);
  background: var(--warn-bg);
  color: var(--warn);
  font-size: 12.5px;
}
</style>

<script setup lang="ts">
import { computed, ref } from "vue";
import { RouterLink } from "vue-router";

import type { BundleKind, BundlePlan } from "@/lib/exportDocument/bundle";
import { expiryWords } from "@/lib/share/format";
import type { ShareExpiry } from "@/lib/share/runShare";
import { SHARE_MESSAGE_LIMIT } from "@/lib/share/snapshot";
import { copyText } from "@/utils/clipboard";
import { formatBytes } from "@/utils/format";

/**
 * 分享面板（PSU-2.182，原型 `designs/online-share/index.html` 第 2 块，已点定）。
 *
 * - 只要链接、不要提取码，所以「只要链接就能看」那条黄条**常驻**；
 * - 失效时间缺省 7 天，可改 1 天 / 30 天 / 永久；选永久多一条「要你自己记得删」；
 * - 「隐藏名字」缺省关；附件一行按类说个数与大小；「用原图」缺省关。
 *
 * 本机没配对远端时是 not_configured：面板照样打开，说明为什么、给配置入口。
 */
const props = defineProps<{
  count: number;
  freshnessNote?: string | null;
  conversationName: string;
  state: "available" | "not_configured";
  plan: BundlePlan | null;
  imageCount: number;
  expiresInDays: ShareExpiry;
  hideNames: boolean;
  useOriginals: boolean;
  busy: boolean;
  progress: string | null;
  error: string | null;
  /** 生成完成：链接与失效时刻。 */
  result: { link: string; expiresAt: number | null } | null;
  /** 远程网页没有「接入远程档案」那一页，配置入口只在本机给。 */
  configurePath: string | null;
}>();

const emit = defineEmits<{
  "update:expires-in-days": [value: ShareExpiry];
  "update:hide-names": [value: boolean];
  "update:use-originals": [value: boolean];
  confirm: [];
  close: [];
}>();

const EXPIRY_CHOICES: readonly { value: ShareExpiry; label: string }[] = [
  { value: 1, label: "1 天" },
  { value: 7, label: "7 天" },
  { value: 30, label: "30 天" },
  { value: null, label: "永久" },
];

const UNIT: Record<BundleKind, [string, string]> = {
  image: ["张", "图片"],
  audio: ["条", "语音"],
  video: ["段", "视频"],
  file: ["个", "文件"],
};

const tooMany = computed(() => props.count > SHARE_MESSAGE_LIMIT);

const attachmentWords = computed(() => {
  const plan = props.plan;
  if (plan === null) return null;
  const parts = (["image", "audio", "video", "file"] as const)
    .filter((kind) => plan.included[kind] > 0)
    .map((kind) => `${UNIT[kind][1]} ${String(plan.included[kind])}`);
  if (parts.length === 0) return null;
  const size = formatBytes(plan.totalBytes);
  return `${parts.join(" · ")}，${plan.unknownSizes > 0 ? "至少" : "约"} ${size}`;
});

const copied = ref<"yes" | "no" | null>(null);
const copyLink = async (): Promise<void> => {
  if (props.result === null) return;
  copied.value = (await copyText(props.result.link)) ? "yes" : "no";
};
</script>

<template>
  <section
    class="share-sheet"
    role="region"
    aria-label="分享选中的消息"
    :aria-busy="busy"
    tabindex="0"
  >
    <header class="share-sheet-head">
      <strong>{{ result !== null ? "链接已生成" : `分享 ${count} 条` }}</strong>
      <span data-feedback-mask="conversation-name">{{ conversationName }}</span>
      <button
        type="button"
        class="share-sheet-close"
        aria-label="关闭分享"
        :disabled="busy"
        @click="emit('close')"
      >
        ✕
      </button>
    </header>

    <div
      v-if="state === 'not_configured'"
      class="share-unconfigured"
    >
      <p>分享页要放在你的远程归档上，别人才打得开。这台电脑还没有接入远程归档。</p>
      <RouterLink
        v-if="configurePath !== null"
        class="share-sheet-go"
        :to="configurePath"
        @click="emit('close')"
      >
        去配置
      </RouterLink>
    </div>

    <template v-else-if="result !== null">
      <div class="share-result">
        <input
          data-feedback-mask="share-link"
          class="share-link"
          :value="result.link"
          readonly
          aria-label="分享链接"
          @focus="($event.target as HTMLInputElement).select()"
        >
        <button
          data-feedback-action="复制链接"
          type="button"
          class="share-sheet-go"
          @click="copyLink"
        >
          {{ copied === "yes" ? "已复制" : "复制链接" }}
        </button>
      </div>
      <p
        v-if="copied === 'no'"
        class="share-sheet-warn"
      >
        没能复制，请选中上面的链接手动复制。
      </p>
      <p class="share-sheet-note">
        {{ `${expiryWords(result.expiresAt)}${hideNames ? "，别人的名字已隐藏" : ""}。随时可以在「我的分享」里删掉。` }}
      </p>
    </template>

    <template v-else>
      <div
        class="share-row"
        role="radiogroup"
        aria-label="多久之后失效"
      >
        <span class="share-row-label">多久之后失效</span>
        <div class="share-choices">
          <button
            v-for="choice in EXPIRY_CHOICES"
            :key="String(choice.value)"
            type="button"
            role="radio"
            class="share-choice"
            :class="{ 'is-on': expiresInDays === choice.value }"
            :aria-checked="expiresInDays === choice.value"
            :disabled="busy"
            @click="emit('update:expires-in-days', choice.value)"
          >
            {{ choice.label }}
          </button>
        </div>
      </div>
      <p
        v-if="expiresInDays === null"
        class="share-sheet-warn"
      >
        永久分享要你自己记得删。它会一直出现在「我的分享」里，打开次数照常记。
      </p>

      <p
        v-if="freshnessNote"
        class="share-sheet-note"
        role="status"
      >
        {{ freshnessNote }}。已固定所选记录，新消息不会自动加入。
      </p>
      <div class="share-row">
        <span class="share-row-label">
          隐藏名字
          <small>别人显示成「成员 A / B」，自己是「我」</small>
        </span>
        <button
          type="button"
          role="switch"
          class="share-switch"
          :class="{ 'is-on': hideNames }"
          :aria-checked="hideNames"
          :disabled="busy"
          aria-label="隐藏名字"
          @click="emit('update:hide-names', !hideNames)"
        />
      </div>

      <div
        v-if="attachmentWords !== null"
        class="share-row"
      >
        <span class="share-row-label">
          附件
          <small>{{ attachmentWords }}，都带上</small>
        </span>
      </div>

      <div class="share-row">
        <span class="share-row-label">
          用原图
          <small>{{ imageCount === 0 ? "这批里没有图片" : "缺省用缩略图" }}</small>
        </span>
        <button
          type="button"
          role="switch"
          class="share-switch"
          :class="{ 'is-on': useOriginals }"
          :aria-checked="useOriginals"
          :disabled="busy || imageCount === 0"
          aria-label="用原图"
          @click="emit('update:use-originals', !useOriginals)"
        />
      </div>

      <p class="share-sheet-warn share-link-only">
        只要链接就能看：链接被转给谁，谁就能看到。
      </p>
      <p
        v-if="tooMany"
        class="share-sheet-warn"
      >
        一次最多分享 {{ SHARE_MESSAGE_LIMIT }} 条，现在选了 {{ count }} 条。
      </p>
      <p
        v-if="error !== null"
        class="share-sheet-error"
        role="alert"
      >
        {{ error }}
      </p>

      <button
        data-feedback-action="生成链接"
        type="button"
        class="share-sheet-go"
        :disabled="busy || tooMany"
        :aria-busy="busy"
        @click="emit('confirm')"
      >
        {{ busy ? (progress ?? "正在生成…") : "生成链接" }}
      </button>
    </template>
  </section>
</template>

<style scoped>
/* 底色贴满转录宽，内容收在中间一列：PC 上转录有一千多像素宽，整行铺开时
   开关离标签一屏远、按钮成了一条横幅。窄屏时 max() 退回 14px 的边距。 */
.share-sheet {
  min-height: 0;
  max-height: min(480px, 60dvh);
  flex-shrink: 1;
  overflow-y: auto;
  overscroll-behavior-y: contain;
  padding: 16px max(16px, calc((100% - 560px) / 2));
  border-top: 1px solid var(--rule-soft);
  background: var(--surface);
}

.share-sheet-head {
  position: relative;
  display: grid;
  gap: 2px;
  margin-bottom: 8px;
  padding-right: 44px;
}

.share-sheet-head strong {
  font-size: 16px;
}

.share-sheet-head span {
  overflow: hidden;
  color: var(--ink-soft);
  font-size: 12.5px;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.share-sheet-close {
  position: absolute;
  top: -2px;
  right: 0;
  width: 44px;
  height: 44px;
  padding: 0;
  border: 0;
  border-radius: 5px;
  background: none;
  color: var(--ink-faint);
  font: inherit;
  cursor: pointer;
}

.share-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  min-height: 52px;
  padding: 8px 0;
  border-bottom: 1px solid var(--rule-soft);
  flex-wrap: wrap;
}

.share-row-label {
  min-width: 0;
  flex: 1;
  overflow-wrap: anywhere;
  font-size: 13.5px;
}

.share-row-label small {
  display: block;
  color: var(--ink-soft);
}

.share-choices {
  display: flex;
  gap: 4px;
  flex-wrap: wrap;
}

.share-choice {
  min-height: 44px;
  padding: 8px 12px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: var(--surface);
  color: var(--ink);
  font: inherit;
  font-size: 12.5px;
  cursor: pointer;
}

.share-choice.is-on {
  border-color: var(--accent-solid);
  background: var(--accent-solid);
  color: #fff;
}

.share-switch {
  position: relative;
  width: 42px;
  height: 44px;
  flex: none;
  border: 1px solid var(--rule);
  border-radius: var(--radius-pill);
  background: transparent;
  border-color: transparent;
  cursor: pointer;
  transition: background 0.15s;
}

.share-switch::before {
  position: absolute;
  top: 9px;
  right: 0;
  left: 0;
  height: 24px;
  border-radius: var(--radius-pill);
  background: var(--track);
  content: "";
}

.share-switch::after {
  position: absolute;
  top: 12px;
  left: 2px;
  width: 18px;
  height: 18px;
  border-radius: 50%;
  background: #fff;
  content: "";
  transition: transform 0.15s;
}

.share-switch.is-on {
  border-color: transparent;
}

.share-switch.is-on::before {
  background: var(--accent-solid);
}

.share-switch.is-on::after {
  transform: translateX(18px);
}

.share-switch:disabled,
.share-choice:disabled {
  cursor: not-allowed;
  opacity: 0.45;
}

.share-sheet-note {
  margin: 8px 0 0;
  color: var(--ink-soft);
  font-size: 12.5px;
}

.share-sheet-warn {
  margin: 8px 0 0;
  padding: 7px 10px;
  border: 1px solid var(--warn-border);
  border-radius: var(--radius-btn);
  background: var(--warn-bg);
  color: var(--warn);
  font-size: 12.5px;
}

.share-sheet-error {
  margin: 8px 0 0;
  color: var(--danger, var(--warn));
  font-size: 12.5px;
}

.share-sheet-go {
  display: block;
  width: 100%;
  max-width: 200px;
  min-height: 44px;
  margin-left: auto;
  margin-top: 12px;
  padding: 10px 16px;
  border: 0;
  border-radius: var(--radius-btn);
  background: var(--accent-solid);
  color: #fff;
  font: inherit;
  font-size: 15px;
  font-weight: 600;
  text-align: center;
  text-decoration: none;
  cursor: pointer;
}

.share-sheet-go:disabled {
  cursor: progress;
  opacity: 0.6;
}

.share-unconfigured p {
  margin: 0;
  color: var(--ink-soft);
  font-size: 13.5px;
}

.share-result {
  display: flex;
  gap: 8px;
  align-items: stretch;
}

.share-result .share-sheet-go {
  width: auto;
  margin-top: 0;
  padding: 8px 14px;
  font-size: 14px;
  white-space: nowrap;
}

.share-link {
  flex: 1;
  min-width: 0;
  padding: 8px 10px;
  border: 1px solid var(--rule);
  border-radius: var(--radius-btn);
  background: var(--surface-pop);
  color: var(--ink);
  font: inherit;
  font-size: 13px;
}

@media (max-width: 600px) {
  .share-sheet-go {
    max-width: none;
  }

  .share-choices {
    width: 100%;
  }

  .share-choice {
    flex: 1;
  }
}

@media (prefers-reduced-motion: reduce) {
  .share-switch,
  .share-switch::after {
    transition: none;
  }
}
</style>

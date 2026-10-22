<script setup lang="ts">
/**
 * 多选态的底部操作条（PSU-2.168）。
 *
 * 形状照 PC 版微信：图标在上、文字在下、居中一排，退出的 ✕ 单独靠右，不可用的
 * 直接灰掉（微信里没选到图片时「保存」就是灰的）。**顶栏不参与**——微信多选时
 * 顶栏原样不动，所以「已选 N 条」跟着「导出」按钮走，不挤进标题。
 *
 * 微信那一排到我们这儿的取舍（原型 `designs/merge-export/index.html`）：
 * 「逐条转发」「收藏」没有对应；「合并转发」就是这里的「导出」；
 * **「删除」不能有**——这是只读归档，给这个口本身就是错的。
 */
withDefaults(
  defineProps<{
    count: number;
    /** 选中的消息里有没有能存出来的附件；没有就让「保存媒体」灰着。 */
    hasMedia: boolean;
    /** 正在生成文件：按钮全灰，避免点两下导出两份。 */
    busy?: boolean;
    /**
     * 「分享」出不出现（PSU-2.182）。由能力清单派生：这一端没有分享这件事就不出现；
     * 本机没配对远端时照样出现，点开再说明（「暂时未配置」不藏）。
     */
    canShare?: boolean;
  }>(),
  { busy: false, canShare: false },
);

const emit = defineEmits<{
  export: [];
  share: [];
  copy: [];
  "save-media": [];
  range: [];
  exit: [];
}>();
</script>

<template>
  <div
    class="selection-dock"
    role="toolbar"
    aria-label="已选消息的操作"
  >
    <!-- 快捷键就近说一句（交互第二版）：拉框、Shift 连选都是看不见的手势。 -->
    <p
      class="selection-keys"
      aria-hidden="true"
    >
      <kbd>Shift</kbd> 连选 · 拉框多选 · <kbd>Esc</kbd> 退出
    </p>
    <button
      data-feedback-action="导出所选"
      type="button"
      class="selection-action is-primary"
      :disabled="count === 0 || busy"
      @click="emit('export')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M12 3v11" /><path d="M8.5 10.5 12 14l3.5-3.5" />
        <path d="M4 15v3.5A1.5 1.5 0 0 0 5.5 20h13a1.5 1.5 0 0 0 1.5-1.5V15" />
      </svg>
      <span>{{ count === 0 ? "导出" : `导出 ${count} 条` }}</span>
    </button>

    <button
      v-if="canShare"
      data-feedback-action="分享所选"
      type="button"
      class="selection-action"
      :disabled="count === 0 || busy"
      @click="emit('share')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M10 14a4 4 0 0 0 5.66 0l3-3a4 4 0 0 0-5.66-5.66l-1 1" />
        <path d="M14 10a4 4 0 0 0-5.66 0l-3 3a4 4 0 0 0 5.66 5.66l1-1" />
      </svg>
      <span>分享</span>
    </button>

    <button
      data-feedback-action="复制所选"
      type="button"
      class="selection-action"
      :disabled="count === 0 || busy"
      @click="emit('copy')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="9"
          y="9"
          width="11"
          height="11"
          rx="2"
        /><path d="M5 15V6a2 2 0 0 1 2-2h8" />
      </svg>
      <span>复制</span>
    </button>

    <!-- 2.171 之前恒灰：它有明确的禁用态语义（这批里没有可保存的媒体），
         不是一个点了没反应的假入口。 -->
    <button
      data-feedback-action="保存媒体"
      type="button"
      class="selection-action"
      :disabled="!hasMedia || busy"
      @click="emit('save-media')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <rect
          x="3"
          y="5"
          width="18"
          height="14"
          rx="2"
        /><circle
          cx="8.5"
          cy="10"
          r="1.4"
        /><path d="m5 17 4.5-4.5L13 16l2.5-2.5L19 17" />
      </svg>
      <span>保存媒体</span>
    </button>

    <span
      class="selection-divider"
      aria-hidden="true"
    />

    <!-- 微信没有这一项，是我们加的：归档常要整段导出，一条条点不现实。 -->
    <button
      data-feedback-action="选到这里"
      type="button"
      class="selection-action"
      :disabled="count < 2 || busy"
      @click="emit('range')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M4 7h16" /><path d="M4 17h16" /><path d="M12 9.5v5" />
        <path d="m10 11.5 2-2 2 2" /><path d="m10 12.5 2 2 2-2" />
      </svg>
      <span>选到这里</span>
    </button>

    <button
      data-feedback-action="退出多选"
      type="button"
      class="selection-close"
      aria-label="退出多选"
      @click="emit('exit')"
    >
      <svg
        viewBox="0 0 24 24"
        aria-hidden="true"
      >
        <path d="M6 6l12 12M18 6 6 18" />
      </svg>
    </button>
  </div>
</template>

<style scoped>
.selection-dock {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 4px;
  padding: 8px 52px;
  border-top: 1px solid var(--rule-soft);
  background: var(--surface-pop);
}

.selection-keys {
  position: absolute;
  top: 50%;
  left: 14px;
  margin: 0;
  color: var(--ink-faint);
  font-size: 11.5px;
  transform: translateY(-50%);
}

.selection-keys kbd {
  padding: 0 4px;
  border: 1px solid var(--rule);
  border-bottom-width: 2px;
  border-radius: 4px;
  background: var(--surface);
  font: 11px ui-monospace, Consolas, monospace;
}

/* 窄的时候按钮已经挤满，提示让位。 */
@media (width < 1100px) {
  .selection-keys {
    display: none;
  }
}

.selection-action {
  display: flex;
  min-width: 62px;
  flex-direction: column;
  align-items: center;
  gap: 4px;
  padding: 6px 6px 4px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--ink-soft);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
}

.selection-action:hover:not(:disabled) {
  background: var(--surface-sunken);
}

.selection-action.is-primary {
  color: var(--accent-solid);
  font-weight: 600;
}

.selection-action:disabled {
  cursor: not-allowed;
  opacity: 0.38;
}

.selection-action svg,
.selection-close svg {
  width: 21px;
  height: 21px;
  fill: none;
  stroke: currentColor;
  stroke-linecap: round;
  stroke-linejoin: round;
  stroke-width: 1.6;
}

.selection-divider {
  width: 1px;
  height: 26px;
  margin: 0 4px;
  background: var(--rule-soft);
}

.selection-close {
  position: absolute;
  top: 50%;
  right: 12px;
  padding: 7px;
  border: 0;
  border-radius: 6px;
  background: none;
  color: var(--ink-faint);
  cursor: pointer;
  transform: translateY(-50%);
}

.selection-close svg {
  width: 17px;
  height: 17px;
}

.selection-close:hover {
  background: var(--surface-sunken);
}
</style>

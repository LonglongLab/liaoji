/**
 * 从 `.vue` 模板里抽「界面自己说的话」。
 *
 * 三个调用方，**必须共用这一份实现**：
 *
 * - `src/uiCopy.test.ts` —— 界面文案的句长守卫；
 * - `vite.config.ts` 的 `feedback-ui-copy` 插件 —— 用 `collectUiCopy` 做成虚拟模块
 *   `virtual:feedback-ui-copy`，经 widget 宿主选项 `uiCopy` 交给平台反馈 widget；
 * - `src/lib/feedbackUnmaskRegistry.test.ts` 等登记表守卫 —— 只借 `vueFiles` 这个遍历。
 *
 * ## 文案表为什么归宿主交
 *
 * 划线是「**配置平台下发、产品知识宿主交**」。脱敏策略（含平台键 `staticCopy`）
 * 是配置，由平台按 `projectId` 下发，宿主传它会抛 `WIDGET_PLATFORM_CONFIG_INJECTED`；
 * 而「这个产品的界面上写死了哪些字」是**产品知识**——只有本仓的模板里才有，平台
 * 没有任何办法自己知道。所以 widget 0.1.6 起开了宿主选项 `uiCopy`（用户 2026-09-29
 * 拍板：录屏默认「结构档」，整个文本节点 trim 后精确命中这张表的原样显示，其余照遮）。
 *
 * 白名单要是另写一套遍历，它迟早会比句长守卫多认出一些东西，而「多认出来的那些」
 * 正是会被原样录进回放的文本——所以只有这一份。
 *
 * 放在 `.mjs` 而不是 `.ts`：`vite.config.ts` 属于 tsconfig.node 那一档、测试属于
 * tsconfig.app 那一档，一个 `.ts` 模块要同时被两档 include 才不报错。类型由同目录
 * 的 `uiCopy.d.mts` 提供。
 */

import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";

/** `<code>`/`<pre>` 里是命令与路径，`<script>`/`<style>` 不是给人读的话。 */
const SKIP_TAGS = new Set(["code", "pre", "script", "style"]);

/** 递归列出目录下所有 `.vue`。 */
export function vueFiles(dir) {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const full = join(dir, entry.name);
    if (entry.isDirectory()) return vueFiles(full);
    return entry.name.endsWith(".vue") ? [full] : [];
  });
}

/** 走一遍字符：标签里的东西（含属性值里的 > 和引号）与注释一律不算文案。 */
export function visibleText(template) {
  const out = [];
  let buf = "";
  let index = 0;
  let skipDepth = 0;
  const flush = () => {
    if (skipDepth === 0 && buf.trim() !== "") out.push(buf);
    buf = "";
  };
  while (index < template.length) {
    const char = template.charAt(index);
    if (char !== "<") {
      if (skipDepth === 0) buf += char;
      index += 1;
      continue;
    }
    flush();
    // 注释整段跳过：里面常有 <rect> 这种示例标签，按标签解析会把注释正文漏成文案
    if (template.startsWith("<!--", index)) {
      const end = template.indexOf("-->", index + 4);
      index = end === -1 ? template.length : end + 3;
      continue;
    }
    const tag = /^<(\/?)([a-zA-Z][\w-]*)/.exec(template.slice(index));
    index += 1;
    let quote = null;
    while (index < template.length) {
      const inner = template.charAt(index);
      if (quote !== null) {
        if (inner === quote) quote = null;
      } else if (inner === '"' || inner === "'") {
        quote = inner;
      } else if (inner === ">") {
        index += 1;
        break;
      }
      index += 1;
    }
    if (tag !== null && SKIP_TAGS.has((tag[2] ?? "").toLowerCase())) {
      if (tag[1] === "/") skipDepth = Math.max(0, skipDepth - 1);
      else skipDepth += 1;
    }
  }
  flush();
  return out;
}

/** 中文等效宽度：全角算 1，半角算 0.5。 */
export function cjkWidth(text) {
  const wide = text.match(/[⺀-￿]/g)?.length ?? 0;
  return wide + (text.length - wide) * 0.5;
}

/** 一份 `.vue` 的 `<template>` 源码；没有模板段时返回 null。 */
function templateOf(file) {
  const src = readFileSync(file, "utf8");
  return /<template>([\s\S]*?)\n<\/template>/.exec(src)?.[1] ?? null;
}

/** 插值里写死的中文字符串字面量——它们是真会显示出来的界面文案。 */
function interpolationLiterals(expression) {
  const literals = [];
  for (const match of expression.matchAll(/['"`]([^'"`]*[一-鿿][^'"`]*)['"`]/g)) {
    const literal = match[1];
    if (literal !== undefined) literals.push(literal);
  }
  return literals;
}

/** 一份 `.vue` 里超过 `limit` 个中文等效字符的句子。 */
export function longSentences(file, limit) {
  const template = templateOf(file);
  if (template === null) return [];
  // 插值本身不算文案，但里面的中文字符串字面量是真会显示出来的，单独收进来。
  const literals = [];
  const stripped = template.replace(/\{\{([\s\S]*?)\}\}/g, (_all, expr) => {
    literals.push(...interpolationLiterals(expr));
    return " ⟪值⟫ ";
  });
  return [...visibleText(stripped), ...literals]
    .filter((chunk) => /[一-鿿]/.test(chunk))
    // HTML 把换行折叠成一个空格，先照它的规则归一再断句
    .flatMap((chunk) => chunk.replace(/\s*\n\s*/g, " ").trim().split(/(?<=[。！？；])/))
    .map((sentence) => sentence.trim())
    .filter((sentence) => sentence !== "")
    .map((text) => ({ text, width: cjkWidth(text) }))
    .filter((sentence) => sentence.width > limit);
}

/**
 * 录屏文本放行白名单：模板里**写死**的那些文本。
 *
 * 收两类，两类都是源码里的字面量，与用户数据无关：
 *
 * 1. **不含插值的文本节点**——`<button>开始录制</button>` 这种。带插值的节点
 *    （`保留 {{ n }} 条`）在运行时是一个拼好的整串，白名单里放它的碎片没有用，
 *    更不能拿碎片去做前缀匹配放行；那种值请用 `data-feedback-unmask` 显式标。
 * 2. **插值里的中文字符串字面量**——`{{ ok ? "已完成" : "未完成" }}` 这种，
 *    整个节点的值就是其中一个字面量，精确匹配得上。
 *
 * 返回的字符串都已 `trim`、换行折叠成一个空格（HTML 的渲染口径），与 widget
 * 「整个文本节点 trim 后精确命中」同一口径。按字典序排好并去重，构建产物才是
 * 确定性的。长度、字符类别等护栏在 widget 侧做，这里不重复。
 */
export function collectUiCopy(dir) {
  const copy = new Set();
  for (const file of vueFiles(dir)) {
    const template = templateOf(file);
    if (template === null) continue;
    for (const match of template.matchAll(/\{\{([\s\S]*?)\}\}/g)) {
      for (const literal of interpolationLiterals(match[1] ?? "")) {
        const trimmed = literal.replace(/\s*\n\s*/g, " ").trim();
        if (trimmed !== "") copy.add(trimmed);
      }
    }
    // 带插值的节点整段丢掉：先把 `{{...}}` 换成一个绝不会出现在真实文案里的
    // 哨兵，再把含哨兵的块剔除。直接删插值会把 `保留 {{n}} 条` 拼成
    // 「保留 条」——一个页面上根本不存在、却会被当成白名单条目的字符串。
    const sentinel = "\u0000";
    const stripped = template.replace(/\{\{[\s\S]*?\}\}/g, sentinel);
    for (const chunk of visibleText(stripped)) {
      if (chunk.includes(sentinel)) continue;
      const trimmed = chunk.replace(/\s*\n\s*/g, " ").trim();
      if (trimmed !== "") copy.add(trimmed);
    }
  }
  return [...copy].sort();
}

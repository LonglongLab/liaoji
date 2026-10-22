/**
 * 把一份**已经渲染好的**转录片段收成一个自包含的 HTML 文件（PSU-2.168）。
 *
 * 这里全是纯函数，理由是：
 * happy-dom 上没有真 canvas、也跑不了真渲染，能断言的东西必须全在「渲染之前」
 * 和「渲染之后的那份 DOM 上」。真正要挂节点、等字节的那一段在 `render.ts`。
 */

import { createLimiter, EXPORT_CONCURRENCY } from "./limit";
import { VIEWER_SCRIPT, VIEWER_STYLE } from "./viewerScript";

/** 只能进 HTML 文本位的转义；属性位另外走 `escapeAttribute`。 */
const escapeHtml = (value: string): string =>
  value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");

/**
 * 把页面上所有样式表的规则抄成一段文本。
 *
 * 抄的是 `cssRules` 而不是去抓 `<link>` 的字节：scoped 组件的 `data-v-*` 规则
 * 就在同一批表里，一起抄走，产物才和 App 里长得一样。
 *
 * 跨域的表读 `cssRules` 会抛 SecurityError。**吞掉继续**：少一张表的产物是
 * 少了些样式，而抛出去的话是整个导出失败——后者明显更糟。
 */
export const collectStyleText = (sheets: Iterable<CSSStyleSheet>): string => {
  const chunks: string[] = [];
  for (const sheet of sheets) {
    try {
      for (const rule of sheet.cssRules) chunks.push(rule.cssText);
    } catch {
      continue;
    }
  }
  return chunks.join("\n");
};

export interface InlineReport {
  inlined: number;
  failed: number;
}

/**
 * 把图片的字节搬进文档。
 *
 * 两种 src 都要搬，理由不同但结果一样是死链接：`blob:` 在页面关掉那一刻就失效，
 * 本机头像那种 `http://127.0.0.1:8000/...` 则是换台机器打开就没有。
 * 已经是 `data:` 的原样放过。
 *
 * 取不到的那些**不能把原 src 留着**：存盘之后那是个碎图标，没有任何解释。
 * 摘掉 src 并打上 `data-export-missing`，由调用方按份数如实告诉用户。
 */
/**
 * 每张图画出来有多宽，由 `stampRenderedWidths` 在克隆之前印在元素上。
 * 克隆保留属性，所以这里读得到；用完摘掉，不带进产物。
 */
export const RENDERED_WIDTH_ATTRIBUTE = "data-export-width";

export const inlineImages = async (
  root: HTMLElement,
  toDataUrl: (src: string, renderedWidth: number) => Promise<string>,
): Promise<InlineReport> => {
  const images = [...root.querySelectorAll("img")];
  let inlined = 0;
  let failed = 0;
  /**
   * 同一个地址只取一次（真机实测）。
   *
   * 637 条消息的导出里有 771 张图，**去重之后只有 97 张**——绝大多数是同几个人
   * 的头像，每条消息各画一次。不缓存的话就是 674 次多余的取字节。
   *
   * 缓存的是 Promise 不是结果：771 张几乎同时开始，等结果再存的话，
   * 它们会在第一个完成之前全部错过缓存。
   */
  const pending = new Map<string, Promise<string>>();
  /**
   * 同时最多编几张。每一张都要整份读进来、解一张位图再编码，
   * 上百张同时开工，内存就是上百份（评审 P2）。闸门套在**去重之后**：
   * 同一张图的第二处引用直接等第一处的 Promise，不占名额。
   */
  const limit = createLimiter(EXPORT_CONCURRENCY);
  /**
   * 缓存的键是「地址 + 目标宽度」。同一张图画在两种尺寸上（头像与大图）
   * 要各编一份，否则小的那份会被大的那份覆盖，或者反过来把大图缩糊。
   *
   * ---
   *
   * **这里只省取字节的次数，不省产物里的字节**：同一张图出现 637 次，产物里
   * 就是 637 份一模一样的 base64。真机上这部分约占文件的 22%（重编码之前是 58%）。
   *
   * 想再省下来，只能换 `<img>` 找字节的方式——CSS `content:` 或 SVG `<use>`——
   * 而那会让产物在一部分浏览器里显示不出图。**用户 2026-09-22 明确选了不换**：
   * 这是一份要发给别人的存档，「到哪儿都打得开」比省 1.5 MB 重要。
   * 不要再把这条当成待办捡起来。
   */
  const dataUrlFor = (src: string, width: number): Promise<string> => {
    const key = `${String(width)}|${src}`;
    const existing = pending.get(key);
    if (existing !== undefined) return existing;
    const started = limit(() => toDataUrl(src, width));
    pending.set(key, started);
    return started;
  };
  await Promise.all(
    images.map(async (image) => {
      const width = Number(image.getAttribute(RENDERED_WIDTH_ATTRIBUTE) ?? Number.NaN);
      image.removeAttribute(RENDERED_WIDTH_ATTRIBUTE);
      const src = image.getAttribute("src") ?? "";
      // 压缩包里指向 `附件/` 的图（PSU-2.179）字节在包里，不再嵌进网页。
      if (src === "" || src.startsWith("data:") || image.hasAttribute("data-bundle-src")) return;
      try {
        image.setAttribute("src", await dataUrlFor(src, width));
        inlined += 1;
      } catch {
        image.removeAttribute("src");
        image.setAttribute("data-export-missing", "");
        failed += 1;
      }
    }),
  );
  return { inlined, failed };
};

export interface StandaloneHtmlOptions {
  /** 会话名，进 `<title>` 与页首。 */
  title: string;
  /** `collectStyleText` 抄下来的那一段。 */
  styleText: string;
  /** 导出文档根节点的 `innerHTML`。 */
  bodyHtml: string;
  messageCount: number;
  generatedAt: Date;
  /**
   * 压缩包里的网页（PSU-2.179）：带上查看脚本、它的样式与「请先解压」黄条，
   * `probe` 是那个探针文件的相对地址。不给就是不带脚本的单文件（长图等仍用它）。
   */
  bundle?: { probe: string } | undefined;
}

/**
 * 产物自己的那一点样式。只做三件事：
 * 给页面一个和 App 一致的底色、把离屏定位的痕迹清掉、**强制打印背景**。
 *
 * 第三条是给 PSU-2.170 的 `window.print()` 回退路径留的：打印对话框默认
 * 不印背景，气泡底色会全白。Edge 的 `--print-to-pdf` 实测会印，加这一行不影响它。
 */
const ARTIFACT_STYLE = `
:root { color-scheme: light; }
html, body {
  margin: 0; padding: 0;
  background: var(--paper, #fcf5eb);
  -webkit-print-color-adjust: exact;
  print-color-adjust: exact;
}
.export-doc { padding: 20px 16px 28px; }
.export-doc-head {
  margin: 0 auto 16px; padding-bottom: 10px;
  border-bottom: 1px solid var(--rule-soft, #efe8d8);
}
.export-doc-head h1 { margin: 0 0 2px; font-size: 18px; }
.export-doc-head p { margin: 0; font-size: 12px; color: var(--ink-soft, #5b5346); }
img[data-export-missing] {
  display: inline-block; min-width: 96px; min-height: 64px;
  border: 1px dashed var(--rule, #ddd3bf); border-radius: 6px;
}
@page { margin: 14mm; }
`;

export const buildStandaloneHtml = (options: StandaloneHtmlOptions): string => {
  const title = escapeHtml(options.title.trim() === "" ? "聊天记录" : options.title);
  const stamp = escapeHtml(formatStamp(options.generatedAt));
  return `<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>${title}</title>
<style>${options.styleText}</style>
<style>${ARTIFACT_STYLE}</style>
${options.bundle === undefined ? "" : `<style>${VIEWER_STYLE}</style>\n`}</head>
<body${options.bundle === undefined ? "" : ` data-bundle-probe="${escapeHtml(options.bundle.probe)}"`}>
${options.bundle === undefined ? "" : `<p class="bundle-unzipped">请先把压缩包整个解压，再打开这个网页；否则图片、语音、视频和文件都找不到。</p>\n`}<div class="export-doc">
<header class="export-doc-head">
<h1>${title}</h1>
<p>共 ${String(options.messageCount)} 条 · 导出于 ${stamp}</p>
</header>
${options.bodyHtml}
</div>
${options.bundle === undefined ? "" : `<script>${VIEWER_SCRIPT}</script>\n`}</body>
</html>
`;
};

const pad = (value: number): string => String(value).padStart(2, "0");

const formatStamp = (at: Date): string =>
  `${String(at.getFullYear())}-${pad(at.getMonth() + 1)}-${pad(at.getDate())} ` +
  `${pad(at.getHours())}:${pad(at.getMinutes())}`;

/** Windows 不收的那几个字符；会话名里出现 `/` 的群不少见。 */
const UNSAFE_FILENAME = /[/\\:*?"<>|]/g;

export const exportFileBaseName = (conversationName: string, at: Date): string => {
  const cleaned = conversationName.replace(UNSAFE_FILENAME, "").trim();
  const name = cleaned === "" ? "聊天记录" : cleaned;
  const day = `${String(at.getFullYear())}-${pad(at.getMonth() + 1)}-${pad(at.getDate())}`;
  return `${name}-${day}`;
};

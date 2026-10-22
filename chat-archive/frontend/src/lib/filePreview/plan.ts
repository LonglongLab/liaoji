import type { FileVisual } from "@/lib/fileKinds";
import { nativePdfViewerUsable } from "@/lib/filePreview/pdfSupport";

/**
 * 本机能渲染成哪一屏。`none` 表示这台电脑上没有可用的解析器——
 * 界面照实说不支持，不用占位图冒充预览。
 *
 * PDF 有两条路，选哪条在这里定死，`parse` 和界面只是照着执行：
 *   pdf-native  浏览器自己的阅读器（`<iframe>`），选字、搜索、打印都在。
 *   pdf-canvas  本机 pdf.js 逐页画到画布上——手机上没有内置阅读器，只能这样。
 */
export type PreviewMode =
  | "text"
  | "markdown"
  | "pdf-native"
  | "pdf-canvas"
  | "doc"
  | "sheet"
  | "slides"
  | "archive"
  | "none";

/**
 * 缩放语义：
 *   reflow  文字类 —— 内容整体放大但宽度自适应，只上下滚，不出横向滚动条。
 *   layout  版式类 —— 页面等比撑开，超出部分靠滚动看。
 *   none    我们不提供缩放。交给内置阅读器的那种 PDF 属于这一类：
 *           缩放归那个阅读器管，我们再套一层只会打架。
 */
export type ZoomSemantics = "reflow" | "layout" | "none";

export type PreviewPlan = {
  mode: PreviewMode;
  /** 这次转换会丢什么。空串表示没有损失，界面就不显示这一条。 */
  fidelity: string;
  /** 渲染不了时的理由。只有 `mode === "none"` 时非空。 */
  reason: string;
  zoom: ZoomSemantics;
};

const LOCAL_PLANS: Partial<Record<FileVisual["kind"], Omit<PreviewPlan, "reason">>> = {
  text: { mode: "text", fidelity: "", zoom: "reflow" },
  code: { mode: "text", fidelity: "", zoom: "reflow" },
  markdown: {
    mode: "markdown",
    fidelity:
      "按 GFM 渲染。外链图片不会加载，内联 HTML 的脚本与样式会被去掉，任务列表的勾选框不呈现；想看原样可以切到源码视图。",
    zoom: "reflow",
  },
  word: {
    mode: "doc",
    fidelity: "在本机转换成网页呈现，版式与 Word 里可能有出入。",
    zoom: "reflow",
  },
  excel: {
    mode: "sheet",
    fidelity: "按工作表读取单元格，公式显示为计算结果。",
    zoom: "reflow",
  },
  ppt: {
    mode: "slides",
    fidelity: "本机只能取出幻灯片里的文字，版式、图片和动画不会呈现。",
    zoom: "layout",
  },
  archive: {
    mode: "archive",
    fidelity: "只读取包内目录，不解压任何文件。",
    zoom: "reflow",
  },
};

/** 老的二进制 Office 格式与本机解析器无关，说清楚是格式本身的问题。 */
const LEGACY_OFFICE = new Set(["doc", "xls", "ppt"]);

/**
 * 本机画出来的那份 PDF 丢了什么。版式是一比一的（pdf.js 就是 Firefox 内置阅读器
 * 的那一个），丢的是**画成图像之后没有的那些东西**：文字、链接、表单。
 */
const PDF_CANVAS_FIDELITY =
  "这台设备的浏览器没有 PDF 阅读器，改由本机逐页画成图像：版式与原文一致，" +
  "但文字不能选中、搜索或复制，注释按原样画出、链接和表单点不了。";

/**
 * `nativePdf` 默认现问一次这台浏览器；测试里显式传值，好把两条路都盯住。
 */
export const previewPlanOf = (
  visual: FileVisual,
  nativePdf: boolean = nativePdfViewerUsable(),
): PreviewPlan => {
  if (visual.kind === "pdf") {
    return nativePdf
      ? { mode: "pdf-native", fidelity: "", reason: "", zoom: "none" }
      : { mode: "pdf-canvas", fidelity: PDF_CANVAS_FIDELITY, reason: "", zoom: "layout" };
  }
  if (LEGACY_OFFICE.has(visual.extension)) {
    return {
      mode: "none",
      fidelity: "",
      reason: `本机解析器只认 docx / xlsx / pptx，.${visual.extension} 这种老格式解不开。`,
      zoom: "none",
    };
  }
  const hit = LOCAL_PLANS[visual.kind];
  if (hit !== undefined) return { ...hit, reason: "" };
  return {
    mode: "none",
    fidelity: "",
    reason: `本机没有可用的 .${visual.extension === "" ? "未知格式" : visual.extension} 预览器。`,
    zoom: "none",
  };
};

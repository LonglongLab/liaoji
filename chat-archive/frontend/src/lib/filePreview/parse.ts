import type { PreviewMode } from "@/lib/filePreview/plan";
import type { LocalPdf } from "@/lib/filePreview/pdfRender";
import { formatFileSize } from "@/lib/fileKinds";

/**
 * 本机解析器：拿到附件的 blob，在这台电脑的浏览器里解析成可渲染的结构，
 * 文件不出本进程。三个解析库都在这里按需 import，不进主 bundle。
 *
 * 所有 `meta` 都是真解析出来的数字。读不全的时候（超大文本、超大表、
 * 条目过多的压缩包）必须在 meta 里说明只渲染了多少，不能让截断看起来像全部。
 */

const TEXT_LINE_LIMIT = 4000;
const SHEET_ROW_LIMIT = 300;
const SHEET_COLUMN_LIMIT = 40;
const ARCHIVE_ENTRY_LIMIT = 500;

export type SheetData = {
  name: string;
  columns: string[];
  rows: string[][];
  /** 工作表真实的行列数，用来说明「只渲染了前 N 行」。 */
  totalRows: number;
  totalColumns: number;
};

export type SlideData = { title: string; bullets: string[] };

export type ArchiveEntry = {
  name: string;
  /** 目录条目没有大小，返回 null 而不是 0。 */
  size: string | null;
  depth: number;
  directory: boolean;
};

export type PreviewDocument =
  | { mode: "text"; meta: string; lines: string[] }
  /** `lines` 是原始 md 源码，界面上「渲染 / 源码」两个视图共用同一次解析。 */
  | { mode: "markdown"; meta: string; html: string; lines: string[] }
  | { mode: "pdf-native"; meta: string; url: string }
  /** 本机 pdf.js 打开的那份文档，界面按页向它要位图。 */
  | { mode: "pdf-canvas"; meta: string; pdf: LocalPdf }
  | { mode: "doc"; meta: string; html: string }
  | { mode: "sheet"; meta: string; sheets: SheetData[] }
  | { mode: "slides"; meta: string; slides: SlideData[] }
  | { mode: "archive"; meta: string; entries: ArchiveEntry[] };

/** 解析失败时抛这个：`message` 是可以直接显示给用户的话。 */
export class PreviewParseError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "PreviewParseError";
  }
}

type DecodedText = { text: string; encoding: string; hasBom: boolean };

/**
 * 归档里的文本常见两种编码：微信桌面导出的多是 UTF-8，老文件是 GBK。
 * 先按 UTF-8 严格解，解不动再试 GBK；两个都不行就说不出来，不输出乱码。
 */
export const decodeText = (buffer: ArrayBuffer): DecodedText => {
  const bytes = new Uint8Array(buffer);
  const hasBom = bytes[0] === 0xef && bytes[1] === 0xbb && bytes[2] === 0xbf;
  const body = hasBom ? bytes.subarray(3) : bytes;
  for (const encoding of ["utf-8", "gbk"]) {
    try {
      const text = new TextDecoder(encoding, { fatal: true }).decode(body);
      return { text, encoding: encoding.toUpperCase(), hasBom };
    } catch {
      continue;
    }
  }
  throw new PreviewParseError("这个文件不是 UTF-8 也不是 GBK，本机没法把它当文本读。");
};

const splitLines = (text: string): string[] => text.replaceAll("\r\n", "\n").split("\n");

const parseTextDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const decoded = decodeText(await blob.arrayBuffer());
  const allLines = splitLines(decoded.text);
  const lines = allLines.slice(0, TEXT_LINE_LIMIT);
  const bomText = decoded.hasBom ? "含 BOM" : "无 BOM";
  const countText =
    allLines.length > lines.length
      ? `${String(allLines.length)} 行（只渲染前 ${String(TEXT_LINE_LIMIT)} 行）`
      : `${String(allLines.length)} 行`;
  return { mode: "text", meta: `${decoded.encoding} · ${bomText} · ${countText}`, lines };
};

const parseNativePdfDocument = (blob: Blob): PreviewDocument => ({
  mode: "pdf-native",
  meta: "由浏览器内置阅读器渲染",
  url: URL.createObjectURL(blob.type === "" ? blob.slice(0, blob.size, "application/pdf") : blob),
});

/**
 * 浏览器没有内置阅读器时走这里。pdf.js 只在这一刻才被下载，
 * 打不开时把它的异常翻译成一句能直接给用户看的话。
 */
const parseCanvasPdfDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const { openLocalPdf, pdfOpenMessage } = await import("@/lib/filePreview/pdfRender");
  try {
    const pdf = await openLocalPdf(await blob.arrayBuffer());
    return {
      mode: "pdf-canvas",
      meta: `${String(pdf.pageCount)} 页 · 本机逐页渲染`,
      pdf,
    };
  } catch (error) {
    throw new PreviewParseError(pdfOpenMessage(error));
  }
};

const DOC_ALLOWED_TAGS = new Set([
  "P", "BR", "STRONG", "B", "EM", "I", "U", "S", "SUP", "SUB", "A", "UL", "OL", "LI",
  "H1", "H2", "H3", "H4", "H5", "H6", "BLOCKQUOTE", "PRE", "CODE",
  "TABLE", "THEAD", "TBODY", "TR", "TD", "TH", "IMG", "HR", "SPAN", "DIV",
]);

/** 这几个标签里装的是代码不是文字，整个丢掉，不能把内容留下来。 */
const DOC_DROPPED_TAGS = new Set(["SCRIPT", "STYLE"]);

const isSafeHref = (value: string): boolean => {
  const trimmed = value.trim().toLowerCase();
  return (
    trimmed.startsWith("http://") ||
    trimmed.startsWith("https://") ||
    trimmed.startsWith("#") ||
    trimmed.startsWith("/")
  );
};

/**
 * 图片只认内嵌的 data:image。docx 里的图片本来就是打包在文件内部的，
 * 一个指向外网的 src 意味着打开预览就会向那个地址发一次请求——
 * 这和「解析全在本机、不联网」的承诺冲突，直接丢掉。
 */
const isSafeImageSrc = (value: string): boolean =>
  value.trim().toLowerCase().startsWith("data:image/");

/**
 * mammoth 输出的 HTML 要经 `v-html` 挂上去，所以这里按白名单过一遍：
 * 一份 docx 是别人发过来的东西，它里面的链接、图片地址不能默认可信。
 */
export const sanitizeDocHtml = (html: string): string => {
  const parsed = new DOMParser().parseFromString(`<body>${html}</body>`, "text/html");

  // walk 和 sanitizeElement 互相递归：一个不认识的标签被脱壳后，冒出来的
  // 子节点本身也可能是危险标签，必须对它们重新跑一遍同样的检查——不能因为
  // 它们没被 walk 那次的子节点快照记录到，就绕过了消毒。
  function walk(node: Element): void {
    for (const child of [...node.children]) {
      sanitizeElement(child);
    }
  }

  function sanitizeElement(child: Element): void {
    if (DOC_DROPPED_TAGS.has(child.tagName)) {
      child.remove();
      return;
    }
    if (!DOC_ALLOWED_TAGS.has(child.tagName)) {
      // 认不出的标签只脱掉外壳，里面的文字是文档正文，留着。
      const promoted = [...child.childNodes];
      child.replaceWith(...promoted);
      for (const node of promoted) {
        if (node instanceof Element) sanitizeElement(node);
      }
      return;
    }
    let dropped = false;
    for (const attribute of [...child.attributes]) {
      const name = attribute.name.toLowerCase();
      if (name.startsWith("on") || name === "style") {
        child.removeAttribute(attribute.name);
      } else if (name === "href" && !isSafeHref(attribute.value)) {
        child.removeAttribute(attribute.name);
      } else if (name === "src" && !isSafeImageSrc(attribute.value)) {
        child.remove();
        dropped = true;
        break;
      }
    }
    if (!dropped) walk(child);
  }

  walk(parsed.body);
  return parsed.body.innerHTML;
};

const parseDocDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const mammoth = await import("mammoth");
  const arrayBuffer = await blob.arrayBuffer();
  // mammoth 的浏览器入口读 `arrayBuffer`，Node 入口读 `buffer`，两条最后都走同一个
  // openArrayBuffer。两个都给，浏览器与 Vitest 里就是同一条代码路径。
  const result = await mammoth.convertToHtml({
    arrayBuffer,
    buffer: arrayBuffer,
  } as unknown as { arrayBuffer: ArrayBuffer });
  const html = sanitizeDocHtml(result.value).trim();
  if (html === "") throw new PreviewParseError("这个 docx 里没有可以提取的正文。");
  return { mode: "doc", meta: "docx · 本机转换为网页呈现", html };
};

/**
 * md 里可以直接写裸 HTML，所以 marked 的产物和 docx 一样是**别人写的 HTML**，
 * 必须过同一道白名单才能交给 `v-html`。源码行一并留着，界面上的
 * 「渲染 / 源码」两个视图共用这一次解析，不重新读一遍 blob。
 */
const parseMarkdownDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const decoded = decodeText(await blob.arrayBuffer());
  const allLines = splitLines(decoded.text);
  const lines = allLines.slice(0, TEXT_LINE_LIMIT);
  const { marked } = await import("marked");
  const html = sanitizeDocHtml(await marked.parse(lines.join("\n"), { gfm: true })).trim();
  if (html === "") throw new PreviewParseError("这个 Markdown 文件里没有可以渲染的内容。");
  const countText =
    allLines.length > lines.length
      ? `${String(allLines.length)} 行（只渲染前 ${String(TEXT_LINE_LIMIT)} 行）`
      : `${String(allLines.length)} 行`;
  return {
    mode: "markdown",
    meta: `${decoded.encoding} · Markdown · ${countText}`,
    html,
    lines,
  };
};

const sheetCellText = (value: unknown): string => {
  if (value == null) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (value instanceof Date) return value.toISOString().slice(0, 10);
  return "";
};

const parseSheetDocument = async (blob: Blob, isCsv: boolean): Promise<PreviewDocument> => {
  const XLSX = await import("xlsx");
  const buffer = await blob.arrayBuffer();
  // CSV 先按我们自己的编码探测解成字符串，否则 GBK 的老表会整片乱码。
  const workbook = isCsv
    ? XLSX.read(decodeText(buffer).text, { type: "string" })
    : XLSX.read(new Uint8Array(buffer), { type: "array" });

  const sheets: SheetData[] = [];
  let truncated = false;
  for (const name of workbook.SheetNames) {
    const worksheet = workbook.Sheets[name];
    if (worksheet === undefined) continue;
    const grid = XLSX.utils.sheet_to_json<unknown[]>(worksheet, {
      header: 1,
      raw: false,
      defval: "",
      blankrows: false,
    });
    const totalRows = grid.length;
    const totalColumns = grid.reduce((widest, row) => Math.max(widest, row.length), 0);
    if (totalRows > SHEET_ROW_LIMIT || totalColumns > SHEET_COLUMN_LIMIT) truncated = true;
    const body = grid
      .slice(0, SHEET_ROW_LIMIT)
      .map((row) => row.slice(0, SHEET_COLUMN_LIMIT).map(sheetCellText));
    const [header, ...rest] = body;
    sheets.push({
      name,
      columns: header ?? [],
      rows: rest,
      totalRows,
      totalColumns,
    });
  }
  if (sheets.length === 0) throw new PreviewParseError("这个表格里没有可以读取的工作表。");

  const first = sheets[0];
  const shape =
    first === undefined
      ? ""
      : ` · 当前表 ${String(first.totalRows)} 行 × ${String(first.totalColumns)} 列`;
  const note = truncated ? `（只渲染前 ${String(SHEET_ROW_LIMIT)} 行）` : "";
  return {
    mode: "sheet",
    meta: `${String(sheets.length)} 个工作表${shape}${note}`,
    sheets,
  };
};

/** pptx 就是个 zip：`ppt/slides/slideN.xml` 里的 `<a:t>` 是所有可见文字。 */
const slideOrder = (path: string): number => {
  const match = /slide(\d+)\.xml$/.exec(path);
  return match?.[1] === undefined ? 0 : Number.parseInt(match[1], 10);
};

const slideTexts = (xml: string): string[] => {
  const texts: string[] = [];
  const pattern = /<a:t(?:\s[^>]*)?>([\s\S]*?)<\/a:t>/g;
  let match = pattern.exec(xml);
  while (match !== null) {
    const raw = match[1] ?? "";
    const text = raw
      .replaceAll("&lt;", "<")
      .replaceAll("&gt;", ">")
      .replaceAll("&quot;", '"')
      .replaceAll("&apos;", "'")
      .replaceAll("&amp;", "&")
      .trim();
    if (text !== "") texts.push(text);
    match = pattern.exec(xml);
  }
  return texts;
};

const parseSlidesDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const { default: JSZip } = await import("jszip");
  const zip = await JSZip.loadAsync(blob);
  const paths = Object.keys(zip.files)
    .filter((path) => /^ppt\/slides\/slide\d+\.xml$/.test(path))
    .sort((left, right) => slideOrder(left) - slideOrder(right));
  if (paths.length === 0) throw new PreviewParseError("这个 pptx 里没有找到幻灯片。");

  const slides: SlideData[] = [];
  for (const path of paths) {
    const file = zip.file(path);
    if (file === null) continue;
    const [title = "", ...bullets] = slideTexts(await file.async("string"));
    slides.push({ title, bullets });
  }
  return {
    mode: "slides",
    meta: `${String(slides.length)} 张幻灯片 · 本机只提取文字`,
    slides,
  };
};

const parseArchiveDocument = async (blob: Blob): Promise<PreviewDocument> => {
  const { default: JSZip } = await import("jszip");
  const zip = await JSZip.loadAsync(blob);
  const all = Object.values(zip.files);
  const entries: ArchiveEntry[] = [];
  let unpackedBytes = 0;
  for (const file of all) {
    const trimmed = file.name.replace(/\/$/, "");
    const segments = trimmed.split("/");
    // JSZip 把未压缩大小放在内部字段上，类型里没有；读不到就不显示大小。
    const size = (file as { _data?: { uncompressedSize?: number } })._data?.uncompressedSize;
    if (!file.dir && typeof size === "number") unpackedBytes += size;
    if (entries.length < ARCHIVE_ENTRY_LIMIT) {
      entries.push({
        name: segments[segments.length - 1] ?? trimmed,
        size: file.dir ? null : formatFileSize(size),
        depth: segments.length - 1,
        directory: file.dir,
      });
    }
  }
  if (all.length === 0) throw new PreviewParseError("这个压缩包是空的。");
  const unpacked = formatFileSize(unpackedBytes);
  const note = all.length > entries.length ? `（只列出前 ${String(ARCHIVE_ENTRY_LIMIT)} 条）` : "";
  return {
    mode: "archive",
    meta: `${String(all.length)} 个条目${unpacked === null ? "" : ` · 解压后约 ${unpacked}`}${note}`,
    entries,
  };
};

export const loadPreviewDocument = async (
  blob: Blob,
  mode: PreviewMode,
  extension: string,
): Promise<PreviewDocument> => {
  switch (mode) {
    case "text":
      return parseTextDocument(blob);
    case "markdown":
      return parseMarkdownDocument(blob);
    case "pdf-native":
      return parseNativePdfDocument(blob);
    case "pdf-canvas":
      return parseCanvasPdfDocument(blob);
    case "doc":
      return parseDocDocument(blob);
    case "sheet":
      return parseSheetDocument(blob, extension === "csv");
    case "slides":
      return parseSlidesDocument(blob);
    case "archive":
      return parseArchiveDocument(blob);
    case "none":
      throw new PreviewParseError("本机没有可用的解析器。");
  }
};

/**
 * 两条 PDF 路各有各的东西要还：内置阅读器那份占着一个 blob URL，
 * 本机渲染那份占着一个 worker 和整份文档的缓存。
 */
export const releasePreviewDocument = (document: PreviewDocument | null): void => {
  if (document?.mode === "pdf-native") URL.revokeObjectURL(document.url);
  if (document?.mode === "pdf-canvas") document.pdf.destroy();
};

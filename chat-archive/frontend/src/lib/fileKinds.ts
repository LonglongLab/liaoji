export type FileKind =
  | "word"
  | "excel"
  | "ppt"
  | "pdf"
  | "archive"
  | "image"
  | "audio"
  | "video"
  | "text"
  | "markdown"
  | "code"
  | "other";

const EXTENSION_KINDS: Record<string, FileKind> = {
  doc: "word",
  docx: "word",
  rtf: "word",
  odt: "word",
  xls: "excel",
  xlsx: "excel",
  xlsm: "excel",
  csv: "excel",
  ppt: "ppt",
  pptx: "ppt",
  pdf: "pdf",
  zip: "archive",
  rar: "archive",
  "7z": "archive",
  tar: "archive",
  gz: "archive",
  png: "image",
  jpg: "image",
  jpeg: "image",
  gif: "image",
  webp: "image",
  bmp: "image",
  svg: "image",
  mp3: "audio",
  wav: "audio",
  m4a: "audio",
  flac: "audio",
  amr: "audio",
  mp4: "video",
  mov: "video",
  avi: "video",
  mkv: "video",
  txt: "text",
  md: "markdown",
  markdown: "markdown",
  json: "text",
  log: "text",
  xml: "text",
  yaml: "text",
  yml: "text",
  ini: "text",
  js: "code",
  ts: "code",
  py: "code",
  java: "code",
  sql: "code",
  sh: "code",
  html: "code",
  css: "code",
};

export const FILE_KIND_LABELS: Record<FileKind, string> = {
  word: "Word 文档",
  excel: "Excel 表格",
  ppt: "PPT 演示",
  pdf: "PDF 文档",
  archive: "压缩包",
  image: "图片文件",
  audio: "音频文件",
  video: "视频文件",
  text: "文本文件",
  markdown: "Markdown 文档",
  code: "代码文件",
  other: "文件",
};

export type FileVisual = {
  /** 小写扩展名；没有扩展名时是空串。 */
  extension: string;
  kind: FileKind;
  /**
   * 图标上印的标记。用文件自己的扩展名，认不出的类型也照实印出来——
   * 「DWG」比一个问号多给用户信息，而且不用我们去猜它是什么。
   */
  mark: string;
  label: string;
};

export const fileExtension = (fileName: string | null | undefined): string => {
  const name = (fileName ?? "").trim();
  const dot = name.lastIndexOf(".");
  if (dot <= 0 || dot === name.length - 1) return "";
  return name.slice(dot + 1).toLowerCase();
};

export const fileKindOf = (fileName: string | null | undefined): FileKind =>
  EXTENSION_KINDS[fileExtension(fileName)] ?? "other";

export const fileVisualOf = (fileName: string | null | undefined): FileVisual => {
  const extension = fileExtension(fileName);
  const kind = EXTENSION_KINDS[extension] ?? "other";
  return {
    extension,
    kind,
    mark: extension === "" ? "文件" : extension.toUpperCase().slice(0, 4),
    label: FILE_KIND_LABELS[kind],
  };
};

/** 归档里的字节数按微信的口径显示；没有大小就返回 null，不编一个。 */
export const formatFileSize = (bytes: number | null | undefined): string | null => {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  if (bytes < 1024) return `${String(bytes)} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let value = bytes / 1024;
  let unitIndex = 0;
  while (value >= 1024 && unitIndex < units.length - 1) {
    value /= 1024;
    unitIndex += 1;
  }
  const rounded = value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
  return `${String(rounded)} ${units[unitIndex] ?? "KB"}`;
};

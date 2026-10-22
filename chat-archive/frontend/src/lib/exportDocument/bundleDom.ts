import type { BundleKind } from "./bundle";

/**
 * 把屏外那份导出文档的**克隆**改写成压缩包里的网页（PSU-2.179）。
 *
 * 渲染器在每个附件区块上标了 `data-attachment-id` / `data-attachment-kind`，
 * 这里按它找到那一块，换成指向 `附件/` 的相对地址；配套的交互由
 * `viewerScript.ts` 那段脚本接手。只改克隆，不碰用户正在看的那棵树。
 */

export interface BundleRewrite {
  /** 附件 id → 包内路径（已定扩展名）。 */
  paths: ReadonlyMap<string, string>;
  /** 取不到的附件 id。 */
  missing: ReadonlySet<string>;
  /** 「视频」开关关掉、只留封面的视频 id。 */
  excludedVideos: ReadonlySet<string>;
  /** 附件 id → 时长毫秒（语音、视频上显示）。 */
  durations: ReadonlyMap<string, number | null>;
}

/** 路径逐段编码：文件名里的 `#`、`?`、空格都不能让地址断掉，`/` 要留着。 */
export const relativeUrl = (path: string): string =>
  path.split("/").map(encodeURIComponent).join("/");

const clock = (ms: number | null | undefined): string | null => {
  if (ms === null || ms === undefined || ms <= 0) return null;
  const seconds = Math.round(ms / 1000);
  return `${String(Math.floor(seconds / 60))}:${String(seconds % 60).padStart(2, "0")}`;
};

const MISSING_WORDS: Record<BundleKind, string> = {
  image: "这张图片本机没有，没能带上",
  audio: "这条语音本机没有，没能带上",
  video: "这段视频本机没有，没能带上",
  file: "这个文件本机没有，没能带上",
};

const element = (owner: Document, tag: string, className: string, text?: string): HTMLElement => {
  const node = owner.createElement(tag);
  node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
};

const missingNote = (owner: Document, kind: BundleKind, text = MISSING_WORDS[kind]) =>
  element(owner, "div", "bundle-missing", text);

const rewriteImage = (section: HTMLElement, path: string | undefined, missing: boolean) => {
  let image = section.querySelector<HTMLImageElement>("img.attachment-image");
  if (path !== undefined && image === null) {
    // 屏外文档里这张图没来得及放进去（等图超时）：字节已经在包里，自己补一个 <img>，
    // 不能让它在网页里凭空消失。
    image = section.ownerDocument.createElement("img");
    image.className = "attachment-image";
    const shell = section.querySelector(".attachment-media-button");
    if (shell === null) section.prepend(image);
    else shell.replaceChildren(image);
  }
  if (path !== undefined && image !== null) {
    const url = relativeUrl(path);
    image.setAttribute("src", url);
    image.setAttribute("data-bundle-src", url);
    image.removeAttribute("loading");
    image.removeAttribute("srcset");
    return;
  }
  if (missing || path === undefined) {
    const media = section.querySelector(".attachment-media-button") ?? image;
    const note = missingNote(section.ownerDocument, "image");
    if (media === null) section.prepend(note);
    else media.replaceWith(note);
  }
};

const rewriteVideo = (
  section: HTMLElement,
  id: string,
  path: string | undefined,
  input: BundleRewrite,
) => {
  const owner = section.ownerDocument;
  const media = section.querySelector(".attachment-media-button");
  let replacement: HTMLElement;
  if (path !== undefined) {
    replacement = element(owner, "div", "bundle-video", "▶");
    replacement.setAttribute("data-bundle-video", relativeUrl(path));
    replacement.setAttribute("role", "button");
    const duration = clock(input.durations.get(id));
    if (duration !== null) replacement.append(element(owner, "span", "bundle-video-duration", duration));
  } else if (input.excludedVideos.has(id)) {
    replacement = missingNote(owner, "video", "视频没有放进压缩包（导出时关掉了「视频」）");
  } else {
    replacement = missingNote(owner, "video");
  }
  if (media === null) section.prepend(replacement);
  else media.replaceWith(replacement);
};

const rewriteVoice = (section: HTMLElement, id: string, path: string | undefined, input: BundleRewrite) => {
  const owner = section.ownerDocument;
  const player = section.querySelector(".voice-message-player");
  let replacement: HTMLElement;
  if (path !== undefined) {
    replacement = element(owner, "div", "bundle-voice");
    replacement.setAttribute("data-bundle-audio", relativeUrl(path));
    replacement.setAttribute("role", "button");
    replacement.append(
      element(owner, "span", "bundle-voice-icon", "▶"),
      element(owner, "span", "bundle-voice-wave"),
      element(owner, "span", "bundle-voice-time", clock(input.durations.get(id)) ?? "语音"),
    );
  } else {
    replacement = missingNote(owner, "audio");
  }
  if (player === null) section.prepend(replacement);
  else player.replaceWith(replacement);
};

const rewriteFile = (section: HTMLElement, path: string | undefined, missing: boolean) => {
  const owner = section.ownerDocument;
  const card = section.querySelector(".file-card");
  if (path !== undefined && card !== null) {
    const link = owner.createElement("a");
    link.className = "bundle-file";
    link.setAttribute("href", relativeUrl(path));
    link.setAttribute("target", "_blank");
    card.replaceWith(link);
    link.append(card);
    return;
  }
  if (missing) section.append(missingNote(owner, "file"));
};

/**
 * App 里才有意义的东西：预览失败的「重试」与原因、文件卡上的「预览 | 下载」按钮。
 * 附件已经进了包，网页里留着它们只会误导（截图上看得见「暂时无法打开附件，请重试」）。
 */
const APP_ONLY = [".attachment-media-meta", ".attachment-reason", ".file-card-actions"].join(",");

export const rewriteForBundle = (root: HTMLElement, input: BundleRewrite): void => {
  for (const section of root.querySelectorAll<HTMLElement>("[data-attachment-id]")) {
    const id = section.dataset.attachmentId ?? "";
    const kind = section.dataset.attachmentKind;
    const path = input.paths.get(id);
    const missing = input.missing.has(id);
    if (path !== undefined) {
      for (const leftover of section.querySelectorAll(APP_ONLY)) leftover.remove();
    }
    if (kind === "image") rewriteImage(section, path, missing);
    else if (kind === "video") rewriteVideo(section, id, path, input);
    else if (kind === "audio") rewriteVoice(section, id, path, input);
    else if (kind === "file") rewriteFile(section, path, missing);
  }
};

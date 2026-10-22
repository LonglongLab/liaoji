import {
  buildStandaloneHtml,
  collectStyleText,
  inlineImages,
  RENDERED_WIDTH_ATTRIBUTE,
  type InlineReport,
} from "./document";
import { isAnimatedImage, planShrink, targetWidthFor } from "./shrink";

/**
 * 把屏外渲染好的那份转录收成一个文件（PSU-2.168）。
 *
 * 为什么不是「把界面截下来」：转录是 `@tanstack/vue-virtual` 虚拟滚动，DOM 里
 * 只有可视那十几行，截不到屏外的消息。选中的 80 条必须另外完整渲染一份，那一份由 `ExportDocument.vue`
 * 渲染（用的是 App 自己那套 `MessageTypeRenderer`，样式才不会分叉），
 * 本文件只负责把那份 DOM 收成字节。
 */

/**
 * 整个摘掉的：产物里没有 JS，这些连字节都没有，留着就是假入口。
 *
 * **`<button>` 不在其列**，这是 e2e 当场纠正的一条：图片气泡的 `<img>` 就包在
 * `<button class="attachment-media-button">` 里面，连壳删掉等于把图片删了
 * ——导出来是一份没有图的文档。按钮改成「脱壳」处理（见下）。
 */
const DROP_IN_ARTIFACT = ["audio", "[data-export-drop]"].join(",");

/**
 * 按钮脱壳：换成同样 class 的 `<div>`，样子一模一样，但不再是个控件。
 *
 * 静态文件里按钮点了不会有任何反应。既不能删（图片在里面），也不该留着一个
 * 会变手型、能聚焦的控件假装可点。
 */
const unwrapButtons = (root: HTMLElement): void => {
  const owner = root.ownerDocument;
  for (const button of [...root.querySelectorAll("button")]) {
    const shell = owner.createElement("div");
    const className = button.getAttribute("class");
    const style = button.getAttribute("style");
    if (className !== null) shell.setAttribute("class", className);
    if (style !== null) shell.setAttribute("style", style);
    shell.append(...button.childNodes);
    button.replaceWith(shell);
  }
};

/**
 * 视频在文件里放不了，但它的位置要留着：摘掉 `src` 与控件，剩下一个静止的框。
 * 整条删掉会让那个气泡变成空的，读者只会以为归档丢了东西。
 */
const stillVideo = (root: HTMLElement): void => {
  for (const video of root.querySelectorAll("video")) {
    video.removeAttribute("src");
    video.removeAttribute("controls");
    video.removeAttribute("autoplay");
  }
};

/**
 * 画布上的那一帧读成 PNG（评审 P1）。
 *
 * 动图表情（`WxgfSticker`）画在 `<canvas>` 上。克隆与 `outerHTML` 都不带画布像素，
 * `inlineImages` 又只认 `<img>`——不换的话，导出来是一块空白，而且不算进「没带上」。
 *
 * 带不走动画：原始字节是 wxgf（HEVC），`<img>` 放不了，只能存眼下这一帧。
 * 没画成的（`data-sticker-state` 不是 ready）、零尺寸的、读像素被拒的，一律给 null，
 * 由调用方记成「没带上」——空白图冒充表情比如实说少了一张糟。
 */
export const canvasFrameDataUrl = (canvas: HTMLCanvasElement): string | null => {
  const state = canvas.dataset.stickerState;
  if (state !== undefined && state !== "ready") return null;
  if (canvas.width === 0 || canvas.height === 0) return null;
  try {
    const url = canvas.toDataURL("image/png");
    return url.startsWith("data:image/") ? url : null;
  } catch {
    return null;
  }
};

/**
 * 画不成的动图表情有几块（长图那一路用）。
 *
 * 长图由 `html-to-image` 自己把画布读成图，画成了的照常带上；画不成的是一块白，
 * 在那一路上没人数它——这里数出来，由调用方并进「没带上」。
 */
export const undrawnStickerCount = (root: HTMLElement): number =>
  root.querySelectorAll('canvas[data-sticker-state]:not([data-sticker-state="ready"])').length;

/** 画布上只属于「活的画布」的属性，换成静图之后没有意义。 */
const CANVAS_ONLY_ATTRIBUTES = new Set(["role", "aria-label", "data-frames", "data-sticker-state"]);

/** 克隆里的每一块画布换成一张同样 class 的 `<img>`；`frames` 与画布按文档顺序一一对应。 */
const canvasesToImages = (
  root: HTMLElement,
  frames: readonly (string | null)[],
): { inlined: number; failed: number } => {
  const owner = root.ownerDocument;
  let inlined = 0;
  let failed = 0;
  for (const [index, canvas] of [...root.querySelectorAll("canvas")].entries()) {
    const still = owner.createElement("img");
    for (const attribute of canvas.attributes) {
      if (!CANVAS_ONLY_ATTRIBUTES.has(attribute.name)) {
        still.setAttribute(attribute.name, attribute.value);
      }
    }
    still.setAttribute("alt", canvas.getAttribute("aria-label") ?? "");
    const frame = frames[index] ?? null;
    if (frame === null) {
      still.setAttribute("data-export-missing", "");
      failed += 1;
    } else {
      still.setAttribute("src", frame);
      inlined += 1;
    }
    canvas.replaceWith(still);
  }
  return { inlined, failed };
};

export interface SerializeOptions {
  /** `ExportDocument.vue` 的根节点，**必须已经挂在文档里**（否则计算样式全是默认值）。 */
  node: HTMLElement;
  title: string;
  messageCount: number;
  styleSheets: Iterable<CSSStyleSheet>;
  toDataUrl: (src: string, renderedWidth: number) => Promise<string>;
  /**
   * 把一块画布上**眼下画着的那一帧**读成 `data:`；读不到给 null（记为没带上）。
   * 缺省是 `canvasFrameDataUrl`；注入点只为测试——happy-dom 没有真 canvas。
   */
  canvasToDataUrl?: ((canvas: HTMLCanvasElement) => string | null) | undefined;
  generatedAt: Date;
  /** 有几张图没带上；调用方据此如实告诉用户，而不是默默少几张。 */
  onReport?: ((report: InlineReport) => void) | undefined;
  /**
   * 压缩包（PSU-2.179）：在嵌图之前改写克隆（附件改成 `附件/` 下的相对地址），
   * 并让产物带上查看脚本。不给就是原来的单文件行为。
   */
  bundle?: { rewrite: (clone: HTMLElement) => void; probe: string } | undefined;
}

export const serializeExportDocument = async (
  options: SerializeOptions,
): Promise<string> => {
  // **改克隆，不改原件。** 原节点还挂在页面上（屏外，但确实在文档里），
  // 动它等于在导出过程中改用户正在看的那棵树；出问题也只会毁掉克隆。
  // 先量再克隆：克隆没有布局，之后就再也问不出「这张图画多宽」了。
  stampRenderedWidths(options.node);
  // 画布的像素也只能在克隆之前读：克隆出来的 <canvas> 是空白的。
  const frames = [...options.node.querySelectorAll("canvas")].map(
    options.canvasToDataUrl ?? canvasFrameDataUrl,
  );
  const clone = options.node.cloneNode(true) as HTMLElement;
  // 先换画布再摘东西：两边的 <canvas> 靠文档顺序一一对上，先摘的话顺序就错了位。
  const stills = canvasesToImages(clone, frames);
  for (const dead of clone.querySelectorAll(DROP_IN_ARTIFACT)) dead.remove();
  unwrapButtons(clone);
  stillVideo(clone);
  options.bundle?.rewrite(clone);

  const images = await inlineImages(clone, options.toDataUrl);
  options.onReport?.({
    inlined: images.inlined + stills.inlined,
    failed: images.failed + stills.failed,
  });

  return buildStandaloneHtml({
    title: options.title,
    styleText: collectStyleText(options.styleSheets),
    bodyHtml: clone.outerHTML,
    messageCount: options.messageCount,
    generatedAt: options.generatedAt,
    bundle: options.bundle === undefined ? undefined : { probe: options.bundle.probe },
  });
};

/**
 * 页面上所有样式表。住在这里而不是组件里：`.vue` 上开着 ESLint 的 `no-undef`
 * 且没有配 browser globals，浏览器 API 只能待在模块里（`utils/clipboard.ts`
 * 的文件头写着同一条）。
 */
export const documentStyleSheets = (): StyleSheetList => globalThis.document.styleSheets;

/** 产物的字节。同样是为了把 `Blob` 挡在 `.vue` 外面。 */
export const htmlArtifactBlob = (html: string): Blob =>
  new globalThis.Blob([html], { type: "text/html;charset=utf-8" });

/**
 * 把一个 `blob:` 或同源 http 的图片地址换成 `data:`。
 *
 * 两种都要换：`blob:` 在页面关掉那一刻失效，本机头像那种
 * `http://127.0.0.1:8000/...` 则是换台机器打开就没有。
 */
const blobToDataUrl = (blob: Blob): Promise<string> =>
  new Promise<string>((resolve, reject) => {
    const reader = new globalThis.FileReader();
    reader.addEventListener("load", () => {
      // `readAsDataURL` 给的一定是 string，但类型上还有 ArrayBuffer 与 null。
      // 不收窄就会在别的分支上悄悄印出 "[object ArrayBuffer]" 当图片地址。
      if (typeof reader.result === "string") resolve(reader.result);
      else reject(new Error("unexpected reader result"));
    });
    reader.addEventListener("error", () => {
      reject(new Error("could not read the image bytes"));
    });
    reader.readAsDataURL(blob);
  });

/** 按目标宽度重画一遍，编成 WebP。透明度保得住，头像与表情才不会出黑底。 */
const reencode = async (blob: Blob, width: number): Promise<string> => {
  const bitmap = await globalThis.createImageBitmap(blob);
  try {
    const height = Math.max(1, Math.round((bitmap.height * width) / bitmap.width));
    const canvas = new globalThis.OffscreenCanvas(width, height);
    const context = canvas.getContext("2d");
    if (context === null) throw new Error("no 2d context");
    context.drawImage(bitmap, 0, 0, width, height);
    return await blobToDataUrl(
      await canvas.convertToBlob({ type: "image/webp", quality: 0.82 }),
    );
  } finally {
    bitmap.close();
  }
};

const SNIFF_BYTES = 64 * 1024;

export interface BlobSrcToDataUrlOptions {
  /**
   * 用户勾了「用原图」：字节**原样**带走，不缩也不重编（评审 P2）。
   * 面板上说的是「文件会明显变大」，照旧缩到两倍显示宽的话那句话就不成立。
   */
  keepOriginal?: boolean | undefined;
}

/**
 * 把一个 `blob:` 或同源 http 的图片地址换成 `data:`，顺手按显示尺寸缩小。
 *
 * 两种地址都要换：`blob:` 在页面关掉那一刻失效，本机头像那种
 * `http://127.0.0.1:8000/...` 则是换台机器打开就没有。
 *
 * 缩小是真机量出来要做的：附件图平均 59 KB（缩略图缺失时回退了原图），
 * 而气泡里只画 280px 宽；头像 132×132 画在 34px 上。细则见 `shrink.ts`。
 */
export const blobSrcToDataUrl = async (
  src: string,
  renderedWidth: number,
  options: BlobSrcToDataUrlOptions = {},
): Promise<string> => {
  const response = await globalThis.fetch(src);
  if (!response.ok) throw new Error(`fetch failed: ${String(response.status)}`);
  const blob = await response.blob();
  const original = await blobToDataUrl(blob);
  if (options.keepOriginal === true) return original;
  // 认动图看字节头：远端一律答 octet-stream，看 mime 的话远端的 GIF 会被编成静图。
  // 只读头上 64 KB：APNG 的 acTL、WebP 的 VP8X/ANIM 都在图像数据之前。
  const head = new Uint8Array(await blob.slice(0, SNIFF_BYTES).arrayBuffer());
  if (isAnimatedImage(head)) return original;
  let bitmapWidth: number;
  try {
    const probe = await globalThis.createImageBitmap(blob);
    bitmapWidth = probe.width;
    probe.close();
  } catch {
    // 解不开就原样带走——认不得的格式不该让整张图丢掉。
    return original;
  }
  const plan = planShrink({ width: bitmapWidth, animated: false }, targetWidthFor(renderedWidth));
  if (plan.skip) return original;
  try {
    const shrunk = await reencode(blob, plan.width);
    // 重编码反而更大就不要——小图上常常如此。
    return shrunk.length < original.length ? shrunk : original;
  } catch {
    return original;
  }
};

/**
 * 在克隆之前，把每张图**画出来的宽度**印在元素上。
 *
 * 克隆没有布局，量不到尺寸；而缩到多小只有这一刻知道。属性会被
 * `inlineImages` 读走并摘掉，不进产物。
 */
export const stampRenderedWidths = (root: HTMLElement): void => {
  for (const image of root.querySelectorAll("img")) {
    const width = Math.round(image.getBoundingClientRect().width);
    image.setAttribute(RENDERED_WIDTH_ATTRIBUTE, String(width));
  }
};

export interface WaitForImagesOptions {
  timeoutMs: number;
}

export interface WaitForExportMediaOptions {
  /**
   * 每条消息（按 id）应当放出几张图，由 `expectedInlineMedia` 算出。
   * 按消息分账：数全文档 `<img>` 的话，头像也算一张，头像先到就「数够了」。
   */
  expected: ReadonlyMap<string, number>;
  timeoutMs: number;
  /** 注入点，只为测试：默认按 `setTimeout` 轮询。 */
  sleep?: ((ms: number) => Promise<void>) | undefined;
  now?: (() => number) | undefined;
}

/**
 * 气泡里的图放好了没有：`<img>` 在就算（解码另由 `waitForImages` 等），
 * 动图表情的画布要等它**画成了或确定画不成**——没画成的画布是一块白。
 */
const PLACED_MEDIA = [
  ".attachment-list img",
  '.attachment-list canvas[data-sticker-state="ready"]',
  '.attachment-list canvas[data-sticker-state="failed"]',
].join(",");

/** 还差几张没放进来。0 = 全到了。 */
const unplacedCount = (root: HTMLElement, expected: ReadonlyMap<string, number>): number => {
  const rows = new Map<string, HTMLElement>();
  for (const row of root.querySelectorAll<HTMLElement>("[data-message-id]")) {
    const id = row.dataset.messageId;
    if (id !== undefined) rows.set(id, row);
  }
  let short = 0;
  for (const [messageId, count] of expected) {
    const placed = rows.get(messageId)?.querySelectorAll(PLACED_MEDIA).length ?? 0;
    short += Math.max(0, count - placed);
  }
  return short;
};

const POLL_MS = 25;

/**
 * 屏外那份文档里的 `loading="lazy"` 必须摘掉（真机实测）。
 *
 * 容器在 `left: -10000px`，永远不会进视口，于是浏览器**根本不加载**这些图，
 * `complete` 一直是 false——等待只能等到超时。产物其实没问题（`inlineImages`
 * 随后是直接 fetch 地址的），但 17 条消息的导出因此白白花掉 8.6 秒里的 8 秒。
 */
const unlazy = (root: HTMLElement): void => {
  for (const image of root.querySelectorAll("img")) image.loading = "eager";
};

const defaultSleep = (ms: number): Promise<void> =>
  new Promise((resolve) => globalThis.setTimeout(resolve, ms));

/**
 * 等这份屏外文档**把图片画出来**，再等它们解码完（PSU-2.168）。
 *
 * 只等 `waitForImages` 是不够的，这一条是 e2e 当场抓到的：`MessageTypeRenderer`
 * 要等字节到手才把 `<img>` 放进 DOM，而字节是在 `onMounted` 之后的微任务里到的。
 * 序列化那一刻 DOM 里**一个 `<img>` 都还没有**，`waitForImages` 看见零张待办、
 * 立刻返回，于是导出来是一份没有图的文档。
 *
 * 数够了就往下走；数不够也不硬等——超时之后照常出文件。
 *
 * **返回到最后也没放进来的张数**：那几张根本不在 DOM 里，`inlineImages` 看不见、
 * 也就不会计进「没带上」。调用方要把这个数一并告诉用户（评审 P1）。
 */
export const waitForExportMedia = async (
  root: HTMLElement,
  options: WaitForExportMediaOptions,
): Promise<number> => {
  unlazy(root);
  const sleep = options.sleep ?? defaultSleep;
  const now = options.now ?? (() => Date.now());
  const deadline = now() + options.timeoutMs;
  while (unplacedCount(root, options.expected) > 0 && now() < deadline) {
    await sleep(POLL_MS);
    // 附件图是字节到手之后才插进来的：每一轮都要把新来的也摘掉 lazy（2026-09-28）。
    // 只在开头摘一次的话，晚到的那几张在屏外永远不加载，每次导出都白等满超时。
    unlazy(root);
  }
  unlazy(root);
  await waitForImages(root, { timeoutMs: Math.max(0, deadline - now()) });
  return unplacedCount(root, options.expected);
};

/**
 * 等这批图片都画完（或都失败）。
 *
 * 字节是**挂之前**就解好的（`ExportDocument` 收的是一份已经解好的 Map），
 * 所以这里等的只是浏览器解码那一下，正常是几十毫秒。
 *
 * 超时**不报错**：一张永远不 load 的图不该把整个导出卡死。少的那张由
 * `inlineImages` 打上 `data-export-missing` 并计入报告，用户看得见。
 */
export const waitForImages = async (
  root: HTMLElement,
  options: WaitForImagesOptions,
): Promise<void> => {
  const pending = [...root.querySelectorAll("img")].filter((image) => !image.complete);
  if (pending.length === 0) return;

  await new Promise<void>((resolve) => {
    let settled = 0;
    let done = false;
    const finish = (): void => {
      if (done) return;
      done = true;
      clearTimeout(timer);
      resolve();
    };
    const timer = setTimeout(finish, options.timeoutMs);
    const onSettle = (): void => {
      settled += 1;
      if (settled >= pending.length) finish();
    };
    for (const image of pending) {
      image.addEventListener("load", onSettle, { once: true });
      image.addEventListener("error", onSettle, { once: true });
    }
  });
};

import type * as PdfjsLegacy from "pdfjs-dist/legacy/build/pdf.mjs";
import type { RenderTask } from "pdfjs-dist";

/**
 * 本机 PDF 渲染引擎。**唯一碰 pdf.js 的地方**——它是动态 import 进来的，
 * 不进主 bundle，只有真的打开一份 PDF 才会下载（和 mammoth / xlsx / jszip 同一条路）。
 *
 * 只有在浏览器自己没有内置阅读器时才走这里，判据见 `pdfSupport.ts`。
 */

/**
 * 用 `legacy/` 构建而不是 `build/`：它带 core-js 垫片，大 50 KB，换来老一点的
 * 手机浏览器也能跑——`Promise.withResolvers` 是 Chrome 119 / Safari 17.4 才有的，
 * 而 worker 那个文件是原样拷进产物的，Vite 不会替它降级语法。
 */
type PdfjsEngine = typeof PdfjsLegacy;

const loadEngine = async (): Promise<PdfjsEngine> => {
  const [pdfjs, worker] = await Promise.all([
    import("pdfjs-dist/legacy/build/pdf.mjs"),
    import("pdfjs-dist/legacy/build/pdf.worker.min.mjs?url"),
  ]);
  pdfjs.GlobalWorkerOptions.workerSrc = worker.default;
  return pdfjs;
};

/** 引擎只装一次：同一次会话里连开几份 PDF，worker 也只起一个。 */
let engine: Promise<PdfjsEngine> | null = null;

const engineOnce = (): Promise<PdfjsEngine> => {
  engine ??= loadEngine();
  return engine;
};

/**
 * pdf.js 运行时会按需去取的东西，**全部指向我们自己的站点**（挂载见 vite.config.ts）：
 * 「解析全在本机、文件不出这台机器」那句话在 PDF 上也得成立，不能改成
 * 「顺手向 CDN 报一声这份文档用了哪些中文字体」。
 */
const assetUrl = (directory: string): string => `${import.meta.env.BASE_URL}pdfjs/${directory}/`;

/** 设备像素比封顶。3x 的手机上按 3 倍画，一页 A4 就逼近画布上限了，肉眼分辨不出。 */
const MAX_PIXEL_RATIO = 2;

/**
 * 画布像素总数封顶。iOS Safari 超过上限不会报错，而是**整页变白**——
 * 与其让人对着白屏猜，不如少一点分辨率。
 */
const MAX_CANVAS_PIXELS = 12_000_000;

export type LocalPdf = {
  pageCount: number;
  /**
   * 把某一页按给定的 CSS 宽度画到画布上。位图按设备像素放大，画布的 CSS 尺寸
   * 由调用方管——这里只负责让它清楚。
   */
  renderPage: (pageNumber: number, canvas: HTMLCanvasElement, cssWidth: number) => Promise<void>;
  destroy: () => void;
};

/** pdf.js 用异常的 `name` 区分原因，取消不是错误。 */
const errorName = (error: unknown): string =>
  error instanceof Error ? error.name : "";

const isCancelled = (error: unknown): boolean =>
  errorName(error) === "RenderingCancelledException";

/** 打不开时给用户的话。说得出原因就说原因，说不出就只说打不开，不猜。 */
export const pdfOpenMessage = (error: unknown): string => {
  switch (errorName(error)) {
    case "PasswordException":
      return "这份 PDF 设了打开密码，本机没有密码就解不开。";
    case "InvalidPDFException":
      return "这个文件的内容不是一份能读的 PDF。";
    default:
      return "本机没能打开这份 PDF。";
  }
};

const pixelRatio = (): number => {
  const ratio = globalThis.devicePixelRatio;
  if (!Number.isFinite(ratio) || ratio <= 0) return 1;
  return Math.min(ratio, MAX_PIXEL_RATIO);
};

export const openLocalPdf = async (data: ArrayBuffer): Promise<LocalPdf> => {
  const pdfjs = await engineOnce();
  // 关掉整份文档要走 loadingTask：它才管得着 worker，`document` 只管自己的缓存。
  const loading = pdfjs.getDocument({
    data,
    cMapUrl: assetUrl("cmaps"),
    cMapPacked: true,
    standardFontDataUrl: assetUrl("standard_fonts"),
    wasmUrl: assetUrl("wasm"),
    iccUrl: assetUrl("iccs"),
  });
  const document = await loading.promise;

  /** 正在画的那个任务。新的一次开始前要把它停下来。 */
  let active: RenderTask | null = null;

  /**
   * 渲染排成一条队。**只在函数开头取消旧任务是不够的**：`getPage` 是异步的，
   * 两次几乎同时的调用会双双越过那一句走到 `page.render()`，pdf.js 直接抛
   * 「同一张画布上有两个 render」——连点两下翻页就能撞上，而且先到的那次会赢，
   * 屏幕停在上一页。所以排队，并且只让最后一次真的画。
   */
  let queue: Promise<unknown> = Promise.resolve();
  let latest = 0;

  const draw = async (
    mine: number,
    pageNumber: number,
    canvas: HTMLCanvasElement,
    cssWidth: number,
  ): Promise<void> => {
    // 排队期间又来了更新的请求，这一次画出来也是立刻被盖掉，白花一次解码。
    if (mine !== latest) return;
    const page = await document.getPage(pageNumber);
    if (mine !== latest) return;
    const unscaled = page.getViewport({ scale: 1 });
    let scale = (cssWidth * pixelRatio()) / unscaled.width;
    const pixels = unscaled.width * unscaled.height * scale * scale;
    if (pixels > MAX_CANVAS_PIXELS) scale *= Math.sqrt(MAX_CANVAS_PIXELS / pixels);
    const viewport = page.getViewport({ scale });
    canvas.width = Math.max(1, Math.round(viewport.width));
    canvas.height = Math.max(1, Math.round(viewport.height));
    const task = page.render({ canvas, viewport });
    active = task;
    try {
      await task.promise;
    } catch (error) {
      // 被后一次渲染取消掉的那一次不是失败，界面上不该冒出错误。
      if (!isCancelled(error)) throw error;
    } finally {
      if (active === task) active = null;
    }
  };

  const renderPage = (
    pageNumber: number,
    canvas: HTMLCanvasElement,
    cssWidth: number,
  ): Promise<void> => {
    latest += 1;
    const mine = latest;
    // 手上那个先停下来，队伍才走得动——不然新的一页要等旧的画完。
    active?.cancel();
    const started = (): Promise<void> => draw(mine, pageNumber, canvas, cssWidth);
    // 上一次的成败与这一次无关，两条分支都往下走。
    const result = queue.then(started, started);
    queue = result.catch(() => undefined);
    return result;
  };

  return {
    pageCount: document.numPages,
    renderPage,
    destroy: (): void => {
      active?.cancel();
      active = null;
      void loading.destroy();
    },
  };
};

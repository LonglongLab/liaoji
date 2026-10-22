import { toBlob } from "html-to-image";
import JSZip from "jszip";

import { sliceLabel, type Slice } from "./slice";

/**
 * 把屏外那份导出文档逐片光栅化成图（PSU-2.169）。
 *
 * 裁剪交给 `html-to-image` 自己：给它 `width`/`height` 和一个作用在克隆根上的
 * `transform`，它就只画那一扇窗。比自己搭一个 `overflow: hidden` 的宿主稳——
 * 少一层要与真实布局保持一致的东西。
 */

export interface SliceRenderOptions {
  width: number;
  height: number;
  style: { transform: string; transformOrigin: string };
}

/**
 * 渲染函数注入点，缺省是 `html-to-image` 的 `toBlob`。
 *
 * 存在的唯一理由是测试：happy-dom 上没有真 canvas，也没有 `foreignObject`
 * 光栅化，真渲染在那里跑不了。
 */
export type SliceRender = (
  node: HTMLElement,
  options: SliceRenderOptions,
) => Promise<Blob>;

export interface RenderSlicesOptions {
  /** `ExportDocument.vue` 的根节点，必须已经挂在文档里（屏外也算）。 */
  node: HTMLElement;
  slices: readonly Slice[];
  /** 文档宽度，也是每张图的宽度。 */
  width: number;
  /** 页脚左边那句，比如「和「项目群」的聊天记录」。 */
  caption: string;
  render: SliceRender;
}

const FOOTER_CLASS = "export-slice-footer";
/**
 * 每张图底下那条页脚的高度（评审 2026-09-23）。
 *
 * 片号从前是压在这一片右下角的一个角标，离下缘 48 像素——正好压在这一片最后
 * 一条消息上，自己发的（靠右）被盖得最狠。现在它住在内容**下面**单独一条里，
 * 所以切的时候要把这条的高度让出来：`MAX_SLICE_HEIGHT - SLICE_FOOTER_HEIGHT`。
 */
export const SLICE_FOOTER_HEIGHT = 28;
/**
 * 最后一片在页脚上方补的留白，与行间距（`.export-doc-row` 的 margin-bottom）同值。
 * 中间那几片断在下一行的 `top`，天然带着这段间距；最后一片断在最后一行的下缘，
 * 不补的话最后一个气泡就贴着页脚。
 */
const LAST_SLICE_GAP = 10;

export const renderSlices = async (options: RenderSlicesOptions): Promise<Blob[]> => {
  const blobs: Blob[] = [];
  const owner = options.node.ownerDocument;
  // 页脚是绝对定位的，得有个定位上下文；文档根本身平时是 static。
  const previousPosition = options.node.style.position;
  options.node.style.position = "relative";
  try {
    for (const [index, slice] of options.slices.entries()) {
      const label = sliceLabel(index, options.slices.length);
      const footer = label === null ? null : owner.createElement("div");
      const gap = index === options.slices.length - 1 ? LAST_SLICE_GAP : 0;
      if (footer !== null) {
        footer.className = FOOTER_CLASS;
        const caption = owner.createElement("span");
        caption.textContent = options.caption;
        const number = owner.createElement("span");
        number.textContent = label;
        footer.append(caption, number);
        // 按**文档坐标**摆在这一片的下缘**之外**：图多画出这一条高，条是不透明的，
        // 盖住的是下一片开头那一截——那一截在这张图上本来就不该出现。
        footer.style.top = `${String(slice.end + gap)}px`;
        options.node.append(footer);
      }
      try {
        blobs.push(
          await options.render(options.node, {
            width: options.width,
            height:
              slice.end - slice.start + (footer === null ? 0 : gap + SLICE_FOOTER_HEIGHT),
            style: {
              transform: `translateY(${String(-slice.start)}px)`,
              transformOrigin: "top left",
            },
          }),
        );
      } finally {
        footer?.remove();
      }
    }
  } finally {
    options.node.style.position = previousPosition;
  }
  return blobs;
};

/**
 * 每张图按 2 倍像素出：发出去之后是要被人放大看字的。
 *
 * 画布上限 16384 像素是按**输出像素**算的，所以 CSS 高度的上限要除以它。
 * 取 8000 而不是 8192，留一点余量给取整。
 */
export const SLICE_PIXEL_RATIO = 2;
export const MAX_SLICE_HEIGHT = 8000;

/**
 * 图的底色。取的是应用的纸色——不给的话 `html-to-image` 会留透明底，
 * 存成 PNG 发出去，在深色聊天背景上看就是一片黑。
 *
 * 和其它浏览器 API 一样住在 .ts 里：`.vue` 上开着 `no-undef` 且没配 browser globals。
 */
export const exportPaperColor = (): string => {
  const paper = globalThis
    .getComputedStyle(globalThis.document.documentElement)
    .getPropertyValue("--paper")
    .trim();
  return paper === "" ? "#ffffff" : paper;
};

export const defaultSliceRender =
  (backgroundColor: string): SliceRender =>
  async (node, options) => {
    const blob = await toBlob(node, {
      width: options.width,
      height: options.height,
      style: options.style,
      backgroundColor,
      pixelRatio: SLICE_PIXEL_RATIO,
      cacheBust: false,
      // 本应用不用 webfont（全仓没有 @font-face），关掉它就少一整轮对外请求。
      skipFonts: true,
    });
    if (blob === null) throw new Error("could not rasterise this slice");
    return blob;
  };

/**
 * 量出每条消息在文档里的位置。
 *
 * 量每一行自己的位置而不是把高度累加：行与行之间有外边距，累加出来
 * 的位置会一路偏移，切点也跟着偏。
 *
 * 这一段在 happy-dom 上测不了（那里所有尺寸都是 0），由 e2e 守着；
 * 所以它只做「读四个数」这一件事，判断全在 `planSlices` 里。
 */
export const measureRows = (node: HTMLElement): { top: number; height: number }[] => {
  // 不用 `offsetTop`：它四舍五入成整数，而行的真实位置是小数，入上去的那半个像素
  // 会让切点落进下一行里——下一行的头像顶上一像素就出现在这一片的页脚上面
  // （评审 2026-09-23 真图上看见的）。量小数、切点向下取整，只会多带一点留白。
  const origin = node.getBoundingClientRect().top;
  return [...node.querySelectorAll<HTMLElement>(".export-doc-row")].map((row) => {
    const box = row.getBoundingClientRect();
    const top = Math.floor(box.top - origin);
    return { top, height: Math.ceil(box.bottom - origin) - top };
  });
};

export interface PackedImages {
  blob: Blob;
  fileName: string;
}

/**
 * 一张就直接给一张；多张才打包。
 *
 * 只有一片却塞进 zip，等于让人为了一张图多解压一次。
 */
export const packSlices = async (
  blobs: readonly Blob[],
  baseName: string,
): Promise<PackedImages> => {
  const only = blobs[0];
  if (only === undefined) throw new Error("nothing to pack");
  if (blobs.length === 1) return { blob: only, fileName: `${baseName}.png` };
  const zip = new JSZip();
  for (const [index, blob] of blobs.entries()) {
    zip.file(`${baseName}-${String(index + 1)}of${String(blobs.length)}.png`, blob);
  }
  return {
    blob: await zip.generateAsync({ type: "blob" }),
    fileName: `${baseName}.zip`,
  };
};

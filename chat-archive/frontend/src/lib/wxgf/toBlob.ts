/**
 * 附件加载器出口的那一道：wxgf 在这里变成 `<img>` 认得的东西（PSU-2.146）。
 *
 * 桌面端从 2.146 起缓存与上传都保留微信的原始 wxgf 字节，远端还有 2.146 之前传的
 * PNG——两种并存，一律**按字节头**认，不看 mime（远端对象存储只会答 octet-stream）。
 *
 * 本机（`ConversationsView`/`SearchView`）与远端（`remoteReadModel`）的加载器都接
 * 同一个函数，两端不各写一份（workspaceParity 约定）。
 */

import { decodeParsedWxgf, type DecodeOptions } from "./decode";
import { isWxgf, parseWxgf, WXGF_MIME_TYPE } from "./parse";

/** 解不出来的 wxgf。加载器据此退到缩略图（它多半是 JPEG）。 */
export const WXGF_UNDECODABLE_CODE = "WXGF_UNDECODABLE";

export class WxgfUndecodableError extends Error {
  readonly code = WXGF_UNDECODABLE_CODE;

  constructor(cause: unknown) {
    super("这张图是微信的 HEVC 编码，这个浏览器解不出来", { cause });
    this.name = "WxgfUndecodableError";
  }
}

export const isWxgfBlob = async (blob: Blob): Promise<boolean> =>
  isWxgf(new Uint8Array(await blob.slice(0, 4).arrayBuffer()));

const canvasToPng = (canvas: HTMLCanvasElement): Promise<Blob> =>
  new Promise((resolve, reject) => {
    canvas.toBlob((png) => {
      if (png === null) reject(new Error("画布导不出 PNG"));
      else resolve(png);
    }, "image/png");
  });

/** 第一帧存成 PNG：静态图就是它本身，表情是开头那一帧。下载也走这里。 */
export const wxgfFirstFramePng = async (blob: Blob, options: DecodeOptions = {}): Promise<Blob> => {
  try {
    const parsed = parseWxgf(new Uint8Array(await blob.arrayBuffer()));
    const decoded = await decodeParsedWxgf(parsed, { ...options, allFrames: false });
    const [first] = decoded.frames;
    if (first === undefined) throw new Error("没有解出画面");
    return await canvasToPng(first);
  } catch (error: unknown) {
    throw new WxgfUndecodableError(error);
  }
};

/**
 * 加载器出口：不是 wxgf 原样返回；静态 wxgf 解成 PNG；**动图原样返回、类型标成
 * `image/x-wxgf`**，由表情组件（`WxgfSticker`）逐帧播放——在这里压成一张 PNG，
 * 就又回到了 2.146 之前「动图只剩一帧」的老路。
 */
export const displayableImage = async (blob: Blob): Promise<Blob> => {
  if (!(await isWxgfBlob(blob))) return blob;
  let animated: boolean;
  try {
    animated = parseWxgf(new Uint8Array(await blob.arrayBuffer())).color.units.length > 1;
  } catch (error: unknown) {
    throw new WxgfUndecodableError(error);
  }
  if (animated) {
    return blob.type === WXGF_MIME_TYPE ? blob : new Blob([blob], { type: WXGF_MIME_TYPE });
  }
  return wxgfFirstFramePng(blob);
};

/** 这个 blob 是要逐帧播放的 wxgf 动图（`displayableImage` 的约定）。 */
export const isWxgfAnimation = (blob: Blob): boolean => blob.type === WXGF_MIME_TYPE;

const codeOf = (error: unknown): unknown =>
  typeof error === "object" && error !== null ? (error as { code?: unknown }).code : undefined;

export const isWxgfUndecodable = (error: unknown): boolean =>
  codeOf(error) === WXGF_UNDECODABLE_CODE;

/**
 * 原图解不出来时退到缩略图——这个浏览器没有 HEVC、hevc.js 也没加载起来时，
 * 与其一张图都不给，不如给那张 KB 级的 JPEG 缩略图。缩略图也拿不到就报原图那个错。
 */
export const withThumbnailWhenUndecodable = <Id>(
  loadOriginal: (id: Id) => Promise<Blob>,
  loadThumbnail: (id: Id) => Promise<Blob>,
): ((id: Id) => Promise<Blob>) => {
  return async (id: Id): Promise<Blob> => {
    try {
      return await loadOriginal(id);
    } catch (error: unknown) {
      if (!isWxgfUndecodable(error)) throw error;
      try {
        return await loadThumbnail(id);
      } catch {
        throw error;
      }
    }
  };
};

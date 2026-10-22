/**
 * 把 wxgf 解成画布（PSU-2.146）。先用浏览器自带的 WebCodecs，没有或失败再用
 * hevc.js（`wasm.ts`）。
 *
 * spike 里踩过、这里都照着修了的四个坑：
 *
 * 1. 遮罩流与彩色流**各自一个解码器**（`parse.ts` 已按 VPS 切开）。
 * 2. SPS 没写矩阵（2 = unspecified）+ full range 的图，Chrome 按 BT.709 解会偏色
 *    （均差 7～12）→ 把 YUV 复制出来重新包成 `VideoFrame`，标 BT.601（`smpte170m`）
 *    与码流里的 `fullRange`。
 * 3. WebView2（MediaFoundation 后端）把 `displayWidth/Height` 报成 1280×720、
 *    `visibleRect` 截到 1280 宽，画面数据其实完整 → 尺寸与裁切一律取 SPS 的
 *    conformance window，`copyTo({ rect })`。
 * 4. 解码器的帧池只有十几帧，输出帧**立刻**转画布并 `close()`，否则 13 帧后卡住。
 *
 * **遮罩流不解**，画面一律按不透明显示——计划第 5 条原想按 alpha 合成，实测做不到：
 *
 * - 遮罩是 HEVC RExt 的 4:0:0 单色流（`hev1.4.10…`）。Chrome 153 的
 *   `isConfigSupported` 答支持，真解时报 `EncodingError`；
 * - hevc.js 把它当 4:2:0 读（报出 `chromaWidth` = 宽/2），合成的半透明遮罩右下角
 *   整块解错（均差 2.3、单点差 60）；PyAV 解同一条流逐点正确。
 *
 * 真机的 3 个遮罩全是 255（不透明），2.146 之前的 PNG 也丢掉了遮罩，所以按不透明
 * 显示与从前一致；而硬去解它，要在六成图片上白白付一次失败的 WebCodecs、加载
 * 262 KB 的 WASM、再解一张几百万像素的灰度图，只为了扔掉结果——或者在真半透明
 * 的图上打出错的洞。遮罩字节原样留在存档里，有能解它的解码器时再接上。
 */

import { parseWxgf, type AccessUnit, type ParsedWxgf, type SpsInfo, type WxgfStream } from "./parse";
import { decodeWithWasm, type WasmFrame } from "./wasm";

export const DECODE_TIMEOUT_MS = 8000;

/**
 * WASM 兜底从这里调，而不是直接调 `decodeWithWasm`：用例要换掉它，而这个模块
 * 早在 `src/test/setup.ts` 的导入链里就被求值过，模块级 mock 够不着。
 */
export const wasmBackend = { decode: decodeWithWasm };

export interface DecodedWxgf {
  width: number;
  height: number;
  frames: HTMLCanvasElement[];
  /** false = 动图只解出了第一帧（没有 WebCodecs HEVC 的浏览器）。 */
  complete: boolean;
  decoder: "webcodecs" | "wasm";
}

export interface DecodeOptions {
  /** 表情要全部帧；图片只要一帧。 */
  allFrames?: boolean;
  timeoutMs?: number;
}

const canvasOf = (width: number, height: number): HTMLCanvasElement => {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  return canvas;
};

const context2d = (canvas: HTMLCanvasElement): CanvasRenderingContext2D => {
  const context = canvas.getContext("2d", { willReadFrequently: true });
  if (context === null) throw new Error("画布不可用");
  return context;
};

export const webCodecsAvailable = (): boolean =>
  typeof globalThis.VideoDecoder === "function" &&
  typeof globalThis.EncodedVideoChunk === "function" &&
  typeof globalThis.VideoFrame === "function";

/** 码流没写矩阵时按 BT.601 读——JPEG 转来的图的惯例，也是 PyAV 参考图的读法。 */
export const colorSpaceFor = (sps: SpsInfo): VideoColorSpaceInit =>
  sps.matrix === 2
    ? { matrix: "smpte170m", primaries: "bt709", transfer: "iec61966-2-1", fullRange: sps.fullRange }
    : {
        matrix: sps.matrix === 1 ? "bt709" : "smpte170m",
        primaries: "bt709",
        transfer: "bt709",
        fullRange: sps.fullRange,
      };

/**
 * 用 WebCodecs 解几个访问单元，每出一帧就交给 `convert` 并立刻 `close()`。
 * 解出来的帧数不够（超时、报错、浏览器说不支持）一律抛，交给调用方换兜底。
 */
export const decodeWithWebCodecs = async <T>(
  stream: WxgfStream,
  units: readonly AccessUnit[],
  convert: (frame: VideoFrame) => Promise<T>,
  timeoutMs = DECODE_TIMEOUT_MS,
): Promise<T[]> => {
  if (!webCodecsAvailable()) throw new Error("这个浏览器没有 WebCodecs");
  const config: VideoDecoderConfig = { codec: stream.codec, optimizeForLatency: true };
  const support = await VideoDecoder.isConfigSupported(config);
  if (support.supported !== true) throw new Error(`这个浏览器解不了 ${stream.codec}`);
  const outputs: Promise<T>[] = [];
  // 放在对象里：闭包里的赋值 TS 的控制流分析看不见。
  const failure: { error: Error | null } = { error: null };
  const decoder = new VideoDecoder({
    output: (frame) => {
      outputs.push(
        convert(frame).finally(() => {
          frame.close();
        }),
      );
    },
    error: (error) => {
      failure.error = error;
    },
  });
  let timer: ReturnType<typeof setTimeout> | undefined;
  try {
    decoder.configure(config);
    units.forEach((unit, index) => {
      decoder.decode(
        new EncodedVideoChunk({
          type: index === 0 || unit.key ? "key" : "delta",
          timestamp: index * 100_000,
          data: unit.data,
        }),
      );
    });
    const timeout = new Promise<never>((_, reject) => {
      timer = setTimeout(() => {
        reject(new Error(`解码超时：${String(outputs.length)}/${String(units.length)} 帧`));
      }, timeoutMs);
    });
    await Promise.race([decoder.flush(), timeout]);
  } finally {
    clearTimeout(timer);
    if (decoder.state !== "closed") decoder.close();
  }
  // 解码器可能先报错、flush 却照常返回。
  if (failure.error !== null) throw failure.error;
  const results = await Promise.all(outputs);
  if (results.length < units.length) {
    throw new Error(`只解出 ${String(results.length)}/${String(units.length)} 帧`);
  }
  return results;
};

/** 一帧彩色画面 → 按 SPS 裁切、按码流色彩标记重新解释后的画布。 */
export const colorFrameToCanvas = async (
  frame: VideoFrame,
  sps: SpsInfo,
): Promise<HTMLCanvasElement> => {
  const { crop } = sps;
  const canvas = canvasOf(crop.width, crop.height);
  const context = context2d(canvas);
  if (frame.format === null) {
    // 硬件帧读不出平面：只能按原样画，裁切仍取 SPS 的。
    context.drawImage(frame, crop.x, crop.y, crop.width, crop.height, 0, 0, crop.width, crop.height);
    return canvas;
  }
  const rect = { x: crop.x, y: crop.y, width: crop.width, height: crop.height };
  const buffer = new Uint8Array(frame.allocationSize({ rect }));
  const layout = await frame.copyTo(buffer, { rect });
  const fixed = new VideoFrame(buffer, {
    format: frame.format,
    codedWidth: rect.width,
    codedHeight: rect.height,
    timestamp: frame.timestamp,
    layout,
    colorSpace: colorSpaceFor(sps),
  });
  try {
    context.drawImage(fixed, 0, 0);
  } finally {
    fixed.close();
  }
  return canvas;
};

/** hevc.js 的帧可能带着编码补齐的边；按 SPS 裁回真正的画面。 */
const croppedPlaneReader = (frame: WasmFrame, sps: SpsInfo) => {
  const cropped = frame.width === sps.crop.width && frame.height === sps.crop.height;
  const offsetX = cropped ? 0 : sps.crop.x;
  const offsetY = cropped ? 0 : sps.crop.y;
  const shift = Math.max(0, frame.bitDepth - 8);
  const scaleX = frame.width / Math.max(1, frame.chromaWidth);
  const scaleY = frame.height / Math.max(1, frame.chromaHeight);
  return {
    luma: (x: number, y: number): number =>
      (frame.y[(y + offsetY) * frame.width + x + offsetX] ?? 0) >> shift,
    chroma: (plane: Uint16Array, x: number, y: number): number =>
      (plane[
        Math.floor((y + offsetY) / scaleY) * frame.chromaWidth + Math.floor((x + offsetX) / scaleX)
      ] ?? 0) >> shift,
  };
};

export const wasmFrameToCanvas = (frame: WasmFrame, sps: SpsInfo): HTMLCanvasElement => {
  const { width, height } = sps.crop;
  const bt709 = sps.matrix === 1;
  const [kr, kb] = bt709 ? [0.2126, 0.0722] : [0.299, 0.114];
  const kg = 1 - kr - kb;
  const lumaScale = sps.fullRange ? 1 : 255 / 219;
  const chromaScale = sps.fullRange ? 1 : 255 / 224;
  const lumaOffset = sps.fullRange ? 0 : 16;
  const rv = 2 * (1 - kr) * chromaScale;
  const bu = 2 * (1 - kb) * chromaScale;
  const gu = ((-2 * kb * (1 - kb)) / kg) * chromaScale;
  const gv = ((-2 * kr * (1 - kr)) / kg) * chromaScale;
  const read = croppedPlaneReader(frame, sps);
  const canvas = canvasOf(width, height);
  const context = context2d(canvas);
  const image = context.createImageData(width, height);
  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const luma = (read.luma(x, y) - lumaOffset) * lumaScale;
      const u = read.chroma(frame.cb, x, y) - 128;
      const v = read.chroma(frame.cr, x, y) - 128;
      const offset = (y * width + x) * 4;
      image.data[offset] = luma + rv * v;
      image.data[offset + 1] = luma + gu * u + gv * v;
      image.data[offset + 2] = luma + bu * u;
      image.data[offset + 3] = 255;
    }
  }
  context.putImageData(image, 0, 0);
  return canvas;
};

const decodeColor = async (
  color: WxgfStream,
  allFrames: boolean,
  timeoutMs: number,
): Promise<Pick<DecodedWxgf, "frames" | "complete" | "decoder">> => {
  const units = allFrames ? color.units : color.units.slice(0, 1);
  try {
    const frames = await decodeWithWebCodecs(
      color,
      units,
      (frame) => colorFrameToCanvas(frame, color.sps),
      timeoutMs,
    );
    return { frames, complete: true, decoder: "webcodecs" };
  } catch {
    // 兜底只要第一帧：hevc.js 对部分动图系统性偏色，宁可静态也不给错颜色。
    const [first] = await wasmBackend.decode(
      color.units.length > 1 ? (color.units[0]?.data ?? color.bytes) : color.bytes,
    );
    if (first === undefined) throw new Error("hevc.js 没有解出画面");
    return {
      frames: [wasmFrameToCanvas(first, color.sps)],
      complete: units.length <= 1,
      decoder: "wasm",
    };
  }
};

export const decodeParsedWxgf = async (
  parsed: ParsedWxgf,
  { allFrames = false, timeoutMs = DECODE_TIMEOUT_MS }: DecodeOptions = {},
): Promise<DecodedWxgf> => {
  // `parsed.mask` 故意不用，见文件头。
  const color = await decodeColor(parsed.color, allFrames, timeoutMs);
  return { width: parsed.color.sps.crop.width, height: parsed.color.sps.crop.height, ...color };
};

export const decodeWxgf = (data: Uint8Array, options: DecodeOptions = {}): Promise<DecodedWxgf> =>
  decodeParsedWxgf(parseWxgf(data), options);

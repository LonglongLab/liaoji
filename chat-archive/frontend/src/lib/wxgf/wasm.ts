/**
 * HEVC 的 WASM 兜底：浏览器没有 WebCodecs HEVC（Firefox、没装 HEVC 扩展的 Edge）
 * 时用它（PSU-2.146）。
 *
 * 解码器是 hevc.js（`@hevcjs/core` 1.4.5，MIT，https://github.com/lid-labs/hevc.js）。
 * 那份 Emscripten 胶水不是 ES 模块，而且 `.wasm` 有 262 KB，所以两样都放在
 * `public/vendor/hevcjs/` 下、**用到才**用 `<script>` 标签加载，不进主包。
 * 下面的 `WasmHevcDecoder` 按 MIT 从它的 `src/decoder.ts` 抽出一次性解码的那一半，
 * 流式 feed/drain 与 MSE 那些用不上的没有搬。
 *
 * 只用来解**静态图与表情的第一帧**：spike 里它对部分动图系统性偏色（均差约 30），
 * 宁可给静态也不给错颜色。
 */

const VENDOR_BASE = `${import.meta.env.BASE_URL}vendor/hevcjs/`;

/** hevc.js 解出来的一帧：8 位也存成 16 位样本，按 `bitDepth` 右移回来。 */
export interface WasmFrame {
  y: Uint16Array;
  cb: Uint16Array;
  cr: Uint16Array;
  width: number;
  height: number;
  chromaWidth: number;
  chromaHeight: number;
  bitDepth: number;
  poc: number;
}

interface EmscriptenModule {
  HEAPU8: Uint8Array;
  HEAPU16: Uint16Array;
  _malloc: (size: number) => number;
  _free: (pointer: number) => void;
  getValue: (pointer: number, type: "*" | "i32") => number;
  cwrap: (name: string, returns: string | null, args: string[]) => (...args: number[]) => number;
}

type ModuleFactory = (options: { locateFile?: (path: string) => string }) => Promise<EmscriptenModule>;

declare global {
  // Emscripten 胶水脚本在全局上留下的工厂函数。
  var HEVCDecoderModule: ModuleFactory | undefined;
}

let factory: Promise<ModuleFactory> | null = null;

const loadFactory = (): Promise<ModuleFactory> => {
  factory ??= new Promise<ModuleFactory>((resolve, reject) => {
    const existing = globalThis.HEVCDecoderModule;
    if (existing !== undefined) {
      resolve(existing);
      return;
    }
    const script = document.createElement("script");
    script.src = `${VENDOR_BASE}hevc-decode.js`;
    script.async = true;
    script.onload = () => {
      const loaded = globalThis.HEVCDecoderModule;
      if (loaded === undefined) reject(new Error("hevc.js 加载了但没有导出解码器"));
      else resolve(loaded);
    };
    script.onerror = () => {
      reject(new Error("hevc.js 加载失败"));
    };
    document.head.append(script);
  }).catch((error: unknown) => {
    // 失败不记住：网络恢复之后下一张图还能再试。
    factory = null;
    throw error;
  });
  return factory;
};

const copyPlane = (
  module: EmscriptenModule,
  pointer: number,
  width: number,
  height: number,
  stride: number,
): Uint16Array => {
  const out = new Uint16Array(width * height);
  const base = pointer >> 1;
  for (let row = 0; row < height; row += 1) {
    out.set(module.HEAPU16.subarray(base + row * stride, base + row * stride + width), row * width);
  }
  return out;
};

class WasmHevcDecoder {
  private readonly module: EmscriptenModule;
  private readonly api: {
    create: () => number;
    destroy: (decoder: number) => number;
    decode: (decoder: number, pointer: number, length: number) => number;
    frameCount: (decoder: number) => number;
    frame: (decoder: number, index: number, pointer: number) => number;
  };
  private handle: number;

  constructor(module: EmscriptenModule) {
    this.module = module;
    this.api = {
      create: module.cwrap("hevc_decoder_create", "number", []),
      destroy: module.cwrap("hevc_decoder_destroy", null, ["number"]),
      decode: module.cwrap("hevc_decoder_decode", "number", ["number", "number", "number"]),
      frameCount: module.cwrap("hevc_decoder_get_frame_count", "number", ["number"]),
      frame: module.cwrap("hevc_decoder_get_frame", "number", ["number", "number", "number"]),
    };
    this.handle = this.api.create();
    if (this.handle === 0) throw new Error("hevc.js 建不出解码器");
  }

  /** 一次解完整条码流；按显示顺序（POC）返回。 */
  decode(stream: Uint8Array): WasmFrame[] {
    const m = this.module;
    const input = m._malloc(stream.length);
    try {
      m.HEAPU8.set(stream, input);
      const status = this.api.decode(this.handle, input, stream.length);
      if (status !== 0) throw new Error(`hevc.js 解码失败（${String(status)}）`);
      const frames: WasmFrame[] = [];
      const count = this.api.frameCount(this.handle);
      const slot = m._malloc(48);
      try {
        for (let index = 0; index < count; index += 1) {
          if (this.api.frame(this.handle, index, slot) !== 0) continue;
          const width = m.getValue(slot + 12, "i32");
          const height = m.getValue(slot + 16, "i32");
          const strideY = m.getValue(slot + 20, "i32");
          const strideC = m.getValue(slot + 24, "i32");
          const chromaWidth = m.getValue(slot + 28, "i32");
          const chromaHeight = m.getValue(slot + 32, "i32");
          frames.push({
            y: copyPlane(m, m.getValue(slot, "*"), width, height, strideY),
            cb: copyPlane(m, m.getValue(slot + 4, "*"), chromaWidth, chromaHeight, strideC),
            cr: copyPlane(m, m.getValue(slot + 8, "*"), chromaWidth, chromaHeight, strideC),
            width,
            height,
            chromaWidth,
            chromaHeight,
            bitDepth: m.getValue(slot + 36, "i32"),
            poc: m.getValue(slot + 40, "i32"),
          });
        }
      } finally {
        m._free(slot);
      }
      return frames.sort((a, b) => a.poc - b.poc);
    } finally {
      m._free(input);
    }
  }

  destroy(): void {
    if (this.handle !== 0) {
      this.api.destroy(this.handle);
      this.handle = 0;
    }
  }
}

let instance: Promise<EmscriptenModule> | null = null;

const loadModule = (): Promise<EmscriptenModule> => {
  instance ??= loadFactory()
    .then((create) => create({ locateFile: () => `${VENDOR_BASE}hevc-decode.wasm` }))
    .catch((error: unknown) => {
      instance = null;
      throw error;
    });
  return instance;
};

/**
 * 用 hevc.js 解一整条码流。模块只实例化一次；解码器每次新建一个——一次性
 * decode 会累计状态，上一张图的帧会混进下一张。
 */
export const decodeWithWasm = async (stream: Uint8Array): Promise<WasmFrame[]> => {
  const decoder = new WasmHevcDecoder(await loadModule());
  try {
    return decoder.decode(stream);
  } finally {
    decoder.destroy();
  }
};

/**
 * 微信 wxgf 容器 → 能交给解码器的 HEVC 码流（PSU-2.146）。纯函数，不碰浏览器。
 *
 * 格式来自 `chatlog` 的 `wxgf.go` 与 15 个真机样本的交叉印证：
 *
 * 1. 头长 = `data[4]`；之后每一段码流前面是 **4 字节大端长度**，段内是 Annex-B。
 *    段与段之间、头与第一段之间可能夹着别的字段（表情的帧表），所以按「起始码前
 *    4 字节恰好是一个装得下的长度」来认段，不按固定偏移。
 * 2. 图片两段 = **灰度遮罩流 + 彩色流**，各带自己的 VPS/SPS/PPS——两条码流，
 *    不能喂同一个解码器，按 VPS 切开。
 * 3. 表情 = 一条连续码流（首段带参数集 + I 帧，后面是 P 帧），被切成几段。
 */

export const WXGF_MAGIC = [0x77, 0x78, 0x67, 0x66] as const; // "wxgf"

/** 本应用给 wxgf 起的类型名。远端一律答 octet-stream，认格式永远看字节头。 */
export const WXGF_MIME_TYPE = "image/x-wxgf";

export const isWxgf = (head: Uint8Array): boolean =>
  WXGF_MAGIC.every((byte, index) => head[index] === byte);

const NAL_VPS = 32;
const NAL_SPS = 33;

const nalType = (nal: Uint8Array): number => ((nal[0] ?? 0) >> 1) & 0x3f;

const concat = (parts: readonly Uint8Array[]): Uint8Array => {
  const out = new Uint8Array(parts.reduce((total, part) => total + part.length, 0));
  let offset = 0;
  for (const part of parts) {
    out.set(part, offset);
    offset += part.length;
  }
  return out;
};

const startCodeAt = (data: Uint8Array, index: number): boolean =>
  data[index] === 0 && data[index + 1] === 0 && data[index + 2] === 0 && data[index + 3] === 1;

/** 容器里的每一段码流（带起始码的 Annex-B），按出现顺序。 */
export const wxgfPartitions = (data: Uint8Array): Uint8Array[] => {
  const out: Uint8Array[] = [];
  let offset = data[4] ?? data.length;
  while (offset < data.length - 4) {
    let found = -1;
    for (let index = offset; index < data.length - 3; index += 1) {
      if (startCodeAt(data, index)) {
        found = index;
        break;
      }
    }
    if (found < 0) break;
    if (found >= 4) {
      const length =
        (((data[found - 4] ?? 0) << 24) |
          ((data[found - 3] ?? 0) << 16) |
          ((data[found - 2] ?? 0) << 8) |
          (data[found - 1] ?? 0)) >>>
        0;
      if (length > 0 && found + length <= data.length) {
        out.push(data.subarray(found, found + length));
        offset = found + length;
        continue;
      }
    }
    offset = found + 1;
  }
  return out;
};

/** Annex-B 码流里的 NAL 单元（不含起始码）。 */
export const nalUnits = (stream: Uint8Array): Uint8Array[] => {
  const starts: number[] = [];
  for (let index = 0; index + 2 < stream.length; index += 1) {
    if (stream[index] === 0 && stream[index + 1] === 0 && stream[index + 2] === 1) {
      starts.push(index + 3);
      index += 2;
    }
  }
  return starts.map((start, position) => {
    let end = position + 1 < starts.length ? (starts[position + 1] ?? 0) - 3 : stream.length;
    // 下一个起始码若是 4 字节形式，它前导的那个 0 不属于这一个 NAL。
    while (end > start && stream[end - 1] === 0) end -= 1;
    return stream.subarray(start, end);
  });
};

/** 按 VPS 把所有段切成独立码流：图片是 [遮罩, 彩色]，表情是 [整条]。 */
export const wxgfStreams = (data: Uint8Array): Uint8Array[] => {
  const groups: Uint8Array[][] = [];
  for (const partition of wxgfPartitions(data)) {
    const first = nalUnits(partition)[0];
    const opensStream = first !== undefined && nalType(first) === NAL_VPS;
    const current = groups[groups.length - 1];
    if (opensStream || current === undefined) groups.push([partition]);
    else current.push(partition);
  }
  return groups.map(concat);
};

export interface AccessUnit {
  /** 4 字节起始码 + NAL，逐个拼起来：一帧的全部数据。 */
  data: Uint8Array;
  key: boolean;
}

/** 一帧一个访问单元：新帧从「第一个 slice」或一组参数集开始。 */
export const accessUnits = (stream: Uint8Array): AccessUnit[] => {
  const groups: Uint8Array[][] = [];
  let current: Uint8Array[] = [];
  let hasPicture = false;
  for (const nal of nalUnits(stream)) {
    const type = nalType(nal);
    const picture = type < 32;
    const firstSlice = picture && ((nal[2] ?? 0) & 0x80) !== 0;
    const opens = hasPicture && ((picture && firstSlice) || (type >= 32 && type <= 35) || type === 39);
    if (opens) {
      groups.push(current);
      current = [];
      hasPicture = false;
    }
    current.push(nal);
    if (picture) hasPicture = true;
  }
  if (current.length > 0) groups.push(current);
  return groups.map((nals) => ({
    data: concat(nals.flatMap((nal) => [new Uint8Array([0, 0, 0, 1]), nal])),
    // IRAP：BLA/IDR/CRA 与保留的 22/23。
    key: nals.some((nal) => nalType(nal) >= 16 && nalType(nal) <= 23),
  }));
};

/** 去掉防竞争字节（00 00 03 → 00 00），并跳过 2 字节 NAL 头。 */
const rbsp = (nal: Uint8Array): Uint8Array => {
  const out: number[] = [];
  let zeros = 0;
  for (let index = 2; index < nal.length; index += 1) {
    const byte = nal[index] ?? 0;
    if (zeros >= 2 && byte === 3) {
      zeros = 0;
      continue;
    }
    out.push(byte);
    zeros = byte === 0 ? zeros + 1 : 0;
  }
  return new Uint8Array(out);
};

class BitReader {
  private position = 0;
  private readonly bytes: Uint8Array;

  constructor(bytes: Uint8Array) {
    this.bytes = bytes;
  }

  bits(count: number): number {
    let value = 0;
    for (let index = 0; index < count; index += 1) {
      const byte = this.bytes[this.position >> 3];
      if (byte === undefined) throw new RangeError("SPS 比声明的短");
      value = value * 2 + ((byte >> (7 - (this.position & 7))) & 1);
      this.position += 1;
    }
    return value;
  }

  flag(): boolean {
    return this.bits(1) === 1;
  }

  ue(): number {
    let zeros = 0;
    while (this.bits(1) === 0) {
      zeros += 1;
      if (zeros > 31) throw new RangeError("SPS 里的 Exp-Golomb 码不合法");
    }
    return zeros === 0 ? 0 : 2 ** zeros - 1 + this.bits(zeros);
  }

  se(): number {
    const code = this.ue();
    return code % 2 === 1 ? (code + 1) / 2 : -(code / 2);
  }
}

export interface CropRect {
  x: number;
  y: number;
  width: number;
  height: number;
}

export interface SpsInfo {
  /** 0 = 4:0:0（灰度，遮罩流就是它），1 = 4:2:0。 */
  chromaFormat: number;
  bitDepth: number;
  /**
   * 真正的画面：SPS 的 conformance window。尺寸与裁切一律取它，不信解码器报的
   * 帧元数据——WebView2 会把 `displayWidth/visibleRect` 报成 1280×720。
   */
  crop: CropRect;
  fullRange: boolean;
  /** H.273 matrix_coefficients。2 = 没写（微信的图大多如此）。 */
  matrix: number;
}

const skipProfileTierLevel = (reader: BitReader, maxSubLayersMinus1: number): void => {
  reader.bits(8); // profile_space, tier, profile_idc
  reader.bits(32); // compatibility flags
  reader.bits(48); // constraint flags
  reader.bits(8); // level_idc
  const subProfile: boolean[] = [];
  const subLevel: boolean[] = [];
  for (let index = 0; index < maxSubLayersMinus1; index += 1) {
    subProfile.push(reader.flag());
    subLevel.push(reader.flag());
  }
  if (maxSubLayersMinus1 > 0) for (let index = maxSubLayersMinus1; index < 8; index += 1) reader.bits(2);
  for (let index = 0; index < maxSubLayersMinus1; index += 1) {
    if (subProfile[index] === true) {
      reader.bits(8);
      reader.bits(32);
      reader.bits(48);
    }
    if (subLevel[index] === true) reader.bits(8);
  }
};

const skipScalingListData = (reader: BitReader): void => {
  for (let size = 0; size < 4; size += 1) {
    for (let matrix = 0; matrix < 6; matrix += size === 3 ? 3 : 1) {
      if (!reader.flag()) {
        reader.ue();
      } else {
        const coefficients = Math.min(64, 1 << (4 + (size << 1)));
        if (size > 1) reader.se();
        for (let index = 0; index < coefficients; index += 1) reader.se();
      }
    }
  }
};

const skipShortTermRefPicSets = (reader: BitReader): void => {
  const count = reader.ue();
  const deltas: number[] = [];
  for (let index = 0; index < count; index += 1) {
    const predicted = index > 0 && reader.flag();
    if (predicted) {
      reader.bits(1);
      reader.ue();
      let used = 0;
      for (let ref = 0; ref <= (deltas[index - 1] ?? 0); ref += 1) {
        const usedByCurrent = reader.flag();
        if (usedByCurrent || reader.flag()) used += 1;
      }
      deltas.push(used);
    } else {
      const negative = reader.ue();
      const positive = reader.ue();
      for (let ref = 0; ref < negative + positive; ref += 1) {
        reader.ue();
        reader.bits(1);
      }
      deltas.push(negative + positive);
    }
  }
};

/** 只读到 VUI 的色彩标记为止：够拿到色度格式、裁切、range 与矩阵。 */
export const parseSps = (nal: Uint8Array): SpsInfo => {
  const reader = new BitReader(rbsp(nal));
  reader.bits(4); // sps_video_parameter_set_id
  const maxSubLayersMinus1 = reader.bits(3);
  reader.bits(1);
  skipProfileTierLevel(reader, maxSubLayersMinus1);
  reader.ue(); // sps_seq_parameter_set_id
  const chromaFormat = reader.ue();
  if (chromaFormat === 3) reader.bits(1);
  const width = reader.ue();
  const height = reader.ue();
  let left = 0;
  let right = 0;
  let top = 0;
  let bottom = 0;
  if (reader.flag()) {
    left = reader.ue();
    right = reader.ue();
    top = reader.ue();
    bottom = reader.ue();
  }
  const unitX = chromaFormat === 1 || chromaFormat === 2 ? 2 : 1;
  const unitY = chromaFormat === 1 ? 2 : 1;
  const crop = {
    x: left * unitX,
    y: top * unitY,
    width: width - (left + right) * unitX,
    height: height - (top + bottom) * unitY,
  };
  const bitDepth = reader.ue() + 8;
  reader.ue(); // bit_depth_chroma_minus8
  const log2MaxPoc = reader.ue() + 4;
  const orderingInfo = reader.flag();
  for (let index = orderingInfo ? 0 : maxSubLayersMinus1; index <= maxSubLayersMinus1; index += 1) {
    reader.ue();
    reader.ue();
    reader.ue();
  }
  for (let index = 0; index < 6; index += 1) reader.ue();
  if (reader.flag() && reader.flag()) skipScalingListData(reader);
  reader.bits(1); // amp_enabled_flag
  reader.bits(1); // sample_adaptive_offset_enabled_flag
  if (reader.flag()) {
    reader.bits(4);
    reader.bits(4);
    reader.ue();
    reader.ue();
    reader.bits(1);
  }
  skipShortTermRefPicSets(reader);
  if (reader.flag()) {
    const count = reader.ue();
    for (let index = 0; index < count; index += 1) {
      reader.bits(log2MaxPoc);
      reader.bits(1);
    }
  }
  reader.bits(1); // sps_temporal_mvp_enabled_flag
  reader.bits(1); // strong_intra_smoothing_enabled_flag
  const info: SpsInfo = { chromaFormat, bitDepth, crop, fullRange: false, matrix: 2 };
  if (reader.flag()) {
    // vui_parameters：只到 colour_description 为止。
    if (reader.flag()) {
      const aspect = reader.bits(8);
      if (aspect === 255) {
        reader.bits(16);
        reader.bits(16);
      }
    }
    if (reader.flag()) reader.bits(1);
    if (reader.flag()) {
      reader.bits(3);
      info.fullRange = reader.flag();
      if (reader.flag()) {
        reader.bits(8);
        reader.bits(8);
        info.matrix = reader.bits(8);
      }
    }
  }
  return info;
};

const hex = (value: number): string => value.toString(16).toUpperCase();

/** WebCodecs 要的 codec 串：`hev1.<空间><档次>.<兼容位倒序>.<层><级别>.<约束>`。 */
export const codecString = (sps: Uint8Array): string => {
  const raw = rbsp(sps);
  const byte = (index: number): number => raw[index] ?? 0;
  const space = (byte(1) >> 6) & 3;
  const tier = (byte(1) >> 5) & 1;
  const profile = byte(1) & 0x1f;
  const compatibility = ((byte(2) << 24) | (byte(3) << 16) | (byte(4) << 8) | byte(5)) >>> 0;
  let reversed = 0;
  for (let bit = 0; bit < 32; bit += 1) reversed = (reversed << 1) | ((compatibility >>> bit) & 1);
  const constraints = [byte(6), byte(7), byte(8), byte(9), byte(10), byte(11)];
  while (constraints.length > 1 && constraints[constraints.length - 1] === 0) constraints.pop();
  return [
    "hev1",
    `${["", "A", "B", "C"][space] ?? ""}${String(profile)}`,
    hex(reversed >>> 0),
    `${tier === 1 ? "H" : "L"}${String(byte(12))}`,
    ...constraints.map(hex),
  ].join(".");
};

export interface WxgfStream {
  codec: string;
  sps: SpsInfo;
  units: AccessUnit[];
  /** 整条 Annex-B：WASM 兜底一次吃下。 */
  bytes: Uint8Array;
}

const describe = (stream: Uint8Array): WxgfStream => {
  const sps = nalUnits(stream).find((nal) => nalType(nal) === NAL_SPS);
  if (sps === undefined) throw new Error("wxgf 码流里没有 SPS");
  return { codec: codecString(sps), sps: parseSps(sps), units: accessUnits(stream), bytes: stream };
};

export interface ParsedWxgf {
  /** 彩色画面。表情是多帧动画，图片是一帧。 */
  color: WxgfStream;
  /** 灰度遮罩（alpha）。只有图片有，而且大多数是全不透明。 */
  mask: WxgfStream | null;
}

export const parseWxgf = (data: Uint8Array): ParsedWxgf => {
  if (!isWxgf(data)) throw new Error("不是 wxgf 容器");
  const streams = wxgfStreams(data);
  const color = streams[streams.length - 1];
  if (color === undefined) throw new Error("wxgf 容器里没有码流");
  const mask = streams.length > 1 ? streams[0] : undefined;
  return { color: describe(color), mask: mask === undefined ? null : describe(mask) };
};

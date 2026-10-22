/**
 * 按**画出来的尺寸**重新编码导出文档里的图片（PSU-2.168）。
 *
 * 真机实测的账：637 条消息的导出 11.18 MB，其中图片占 10.19 MB——
 * 附件图 134 张、平均 **59 KB**，因为缩略图缺失时回退到了原图，而气泡里只画
 * 280px 宽；头像 637 处只有 2 张、各 4 KB，却是 132×132 画在 34px 上。
 *
 * 所以「重复」不是主因，**每张图本身过大**才是。按显示尺寸重编码同时治两头：
 * 单张小了，重复的那些份也跟着小了，而且不用动一个字节的 markup、没有任何
 * 兼容性代价（换 `<img>` 的引用方式才有）。
 */

/** 量不到尺寸时的兜底上限，也是任何一张图的上限。 */
const MAX_WIDTH = 1024;

/**
 * 这张图该编到多宽：画出来那么宽的两倍（高分屏上才不糊），封顶 1024。
 *
 * 量不到（`0` / `NaN`——元素没参与布局）时**不能当成 0**：那会把图片缩没。
 * 退回上限，宁可大一点。
 */
export const targetWidthFor = (renderedWidth: number): number => {
  if (!Number.isFinite(renderedWidth) || renderedWidth <= 0) return MAX_WIDTH;
  return Math.min(MAX_WIDTH, Math.round(renderedWidth * 2));
};

export type ShrinkPlan = { skip: true } | { skip: false; width: number };

/**
 * 重编成 WebP，尺寸取「画出来的两倍」与原尺寸中的小者。
 *
 * **像素没超标也照编**：真机上 134 张附件图里只有 4 张像素超标，可它们平均 58 KB
 * ——大的是字节不是尺寸（JPEG/PNG 本身就比 WebP 费）。只在需要缩小时才动手，
 * 等于把这一大半省下的字节放过。编完更大就退回原件（`reencode` 的调用方守着）。
 */
export const planShrink = (
  source: { width: number; animated: boolean },
  targetWidth: number,
): ShrinkPlan => {
  // 动图过一遍 canvas 只剩第一帧。真机那份导出里 40 张 gif 全是动画表情，
  // 省下的字节远不值这个代价。认动图看的是字节头（`isAnimatedImage`），不是 mime。
  if (source.animated) return { skip: true };
  if (!Number.isFinite(targetWidth) || targetWidth < 1) return { skip: true };
  if (!Number.isFinite(source.width) || source.width < 1) return { skip: true };
  // 绝不放大：放大只会更糊更大。
  return { skip: false, width: Math.min(source.width, targetWidth) };
};

/** 字节里 `offset` 处是不是这几个 ASCII 字符（格式签名、块类型都是 ASCII）。 */
const startsWith = (bytes: Uint8Array, offset: number, text: string): boolean => {
  for (let index = 0; index < text.length; index += 1) {
    if (bytes[offset + index] !== text.charCodeAt(index)) return false;
  }
  return true;
};

const PNG_SIGNATURE = [0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a] as const;

const readU32be = (bytes: Uint8Array, offset: number): number =>
  (((bytes[offset] ?? 0) << 24) >>> 0) +
  ((bytes[offset + 1] ?? 0) << 16) +
  ((bytes[offset + 2] ?? 0) << 8) +
  (bytes[offset + 3] ?? 0);

const readU32le = (bytes: Uint8Array, offset: number): number =>
  (bytes[offset] ?? 0) +
  ((bytes[offset + 1] ?? 0) << 8) +
  ((bytes[offset + 2] ?? 0) << 16) +
  (((bytes[offset + 3] ?? 0) << 24) >>> 0);

/** APNG：第一个 IDAT 之前有 `acTL` 块（规范要求它在 IDAT 之前）。 */
const isApng = (bytes: Uint8Array): boolean => {
  if (!PNG_SIGNATURE.every((byte, index) => bytes[index] === byte)) return false;
  let offset = PNG_SIGNATURE.length;
  while (offset + 8 <= bytes.length) {
    if (startsWith(bytes, offset + 4, "acTL")) return true;
    if (startsWith(bytes, offset + 4, "IDAT")) return false;
    // 长度 + 类型 + 数据 + CRC
    offset += 12 + readU32be(bytes, offset);
  }
  return false;
};

/** 动态 WebP：`VP8X` 的动画位（0x02），或者干脆出现了 `ANIM` 块。 */
const isAnimatedWebp = (bytes: Uint8Array): boolean => {
  if (!startsWith(bytes, 0, "RIFF") || !startsWith(bytes, 8, "WEBP")) return false;
  let offset = 12;
  while (offset + 8 <= bytes.length) {
    if (startsWith(bytes, offset, "VP8X") && ((bytes[offset + 8] ?? 0) & 0x02) !== 0) return true;
    if (startsWith(bytes, offset, "ANIM")) return true;
    const size = readU32le(bytes, offset + 4);
    offset += 8 + size + (size % 2);
  }
  return false;
};

/**
 * 这份字节是不是（可能是）动图——**只看字节头，不看 mime**（评审 P2）。
 *
 * 远端的附件一律答 `application/octet-stream`（`wxgf/parse.ts` 的注释写着），
 * 按 mime 认的话，从远端导出的 GIF 会被编成一张静止的 WebP。
 *
 * GIF 不去数帧：数帧要把整个文件走一遍，而单帧 GIF 重编能省的那点字节不值这个险。
 */
export const isAnimatedImage = (bytes: Uint8Array): boolean =>
  startsWith(bytes, 0, "GIF8") || isApng(bytes) || isAnimatedWebp(bytes);

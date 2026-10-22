/**
 * 把一段文本编成二维码的模块矩阵，再压成一条 SVG path。
 *
 * 只服务两步验证绑定：内容是 `otpauth://` URI，短、纯 ASCII、一次性。
 * 纠错级别取 M（约 15% 冗余）——屏幕上扫，不需要 H 那种为印刷破损准备的余量，
 * 而 H 会把码变密、在小屏上更难扫。
 *
 * 编码本身用 `qrcode-generator`（MIT、零依赖、自带类型）。自己实现 QR 编码是
 * 四百来行 Galois 域运算，为一处界面不值得。
 */
import qrcode from "qrcode-generator";

/** 编不出来时的模块数；调用方据此如实退回手工录入。 */
export const QR_MODULE_COUNT_UNAVAILABLE = 0;

const built = (value: string): ReturnType<typeof qrcode> | null => {
  try {
    // typeNumber 0 = 自动挑最小能装下的版本。
    const code = qrcode(0, "M");
    code.addData(value);
    code.make();
    return code;
  } catch {
    // 超长输入会让所有版本都装不下。那不是崩溃的理由——手工录入那条路还在。
    return null;
  }
};

export const qrModuleCount = (value: string): number => {
  const code = built(value);
  return code === null ? QR_MODULE_COUNT_UNAVAILABLE : code.getModuleCount();
};

/**
 * 每个黑模块一个 1×1 的方块，拼成一条 path。
 *
 * 同一行里连续的黑模块合成一个矩形：`otpauth://` 这种长度的码里，
 * 合并之后 path 的长度大约只有逐格写的一半。
 */
export const qrPathData = (value: string): string => {
  const code = built(value);
  if (code === null) return "";
  const size = code.getModuleCount();
  const parts: string[] = [];
  for (let row = 0; row < size; row += 1) {
    let runStart = -1;
    for (let column = 0; column <= size; column += 1) {
      const dark = column < size && code.isDark(row, column);
      if (dark && runStart < 0) {
        runStart = column;
      } else if (!dark && runStart >= 0) {
        const width = column - runStart;
        parts.push(
          `M${String(runStart)} ${String(row)}h${String(width)}v1h-${String(width)}z`,
        );
        runStart = -1;
      }
    }
  }
  return parts.join("");
};

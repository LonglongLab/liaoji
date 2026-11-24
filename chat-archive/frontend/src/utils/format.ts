const BYTE_UNITS = ["B", "KB", "MB", "GB", "TB"] as const;

export const formatBytes = (value: number | null): string => {
  if (value === null) return "无法从当前来源读取";
  if (value === 0) return "0 B";
  const exponent = Math.min(
    Math.floor(Math.log(value) / Math.log(1024)),
    BYTE_UNITS.length - 1,
  );
  const scaled = value / 1024 ** exponent;
  const digits = exponent === 0 || scaled >= 100 ? 0 : 1;
  return `${scaled.toFixed(digits)} ${BYTE_UNITS[exponent] ?? "TB"}`;
};

/** 二进制 MiB / GiB 口径（用于备份体积展示）；缺省值返回 null。 */
export const formatBinaryMiB = (value: number | null | undefined): string | null => {
  if (value == null || !Number.isFinite(value) || value < 0) return null;
  const mib = value / (1024 * 1024);
  return mib >= 1024 ? `${(mib / 1024).toFixed(2)} GiB` : `${mib.toFixed(1)} MiB`;
};

export interface FormatByteOptions {
  minUnit?: "B" | "KB" | "MB";
  maxUnit?: "MB" | "TB";
  locale?: string;
  trimTrailingZero?: boolean;
}

const formatScaledByteUnit = (
  value: number,
  unit: string,
  options: FormatByteOptions,
): string => {
  if (options.trimTrailingZero) {
    const rounded = value >= 100 ? Math.round(value) : Math.round(value * 10) / 10;
    return `${String(rounded)} ${unit}`;
  }
  if (options.locale) {
    const precision = value < 10 && !Number.isInteger(value) ? 1 : 0;
    return `${new Intl.NumberFormat(options.locale, { maximumFractionDigits: precision }).format(value)} ${unit}`;
  }
  return `${value.toFixed(1)} ${unit}`;
};

/** 通用紧凑字节格式化：缺省或非法值返回 null，按场景控制最小/最大单位及小数规则。 */
export const formatByteSize = (
  bytes: number | null | undefined,
  options: FormatByteOptions = {},
): string | null => {
  if (bytes == null || !Number.isFinite(bytes) || bytes < 0) return null;
  const minUnit = options.minUnit ?? "B";
  const maxUnit = options.maxUnit ?? "TB";
  if (minUnit === "B" && bytes < 1024) {
    const num = options.locale ? new Intl.NumberFormat(options.locale).format(bytes) : String(bytes);
    return `${num} B`;
  }
  const units = maxUnit === "MB" ? (["KB", "MB"] as const) : (["KB", "MB", "GB", "TB"] as const);
  let value = minUnit === "MB" ? bytes / (1024 * 1024) : bytes / 1024;
  let index = minUnit === "MB" ? 1 : 0;
  while (value >= 1024 && index < units.length - 1) {
    value /= 1024;
    index += 1;
  }
  return formatScaledByteUnit(value, units[index] ?? units[units.length - 1] ?? "MB", options);
};

export const formatTimestamp = (value: number | null): string => {
  if (value === null) return "尚未完成";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value * 1000));
};

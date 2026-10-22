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

export const formatTimestamp = (value: number | null): string => {
  if (value === null) return "尚未完成";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value * 1000));
};

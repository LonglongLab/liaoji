const FORMULA_PREFIXES = new Set(["=", "+", "-", "@"]);

const escapeCell = (value: string | number): string => {
  let text = String(value);
  const first = text.charAt(0);
  if (typeof value === "string" && FORMULA_PREFIXES.has(first)) {
    text = `'${text}`;
  }
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
};

export const toCsv = (
  headers: string[],
  rows: ReadonlyArray<ReadonlyArray<string | number>>,
): string =>
  `${[headers, ...rows].map((row) => row.map(escapeCell).join(",")).join("\r\n")}\r\n`;

export const downloadCsv = (fileName: string, csv: string): void => {
  const blob = new Blob(["﻿", csv], { type: "text/csv;charset=utf-8" });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.click();
  URL.revokeObjectURL(url);
};

export type CellFormatMap = {
  readonly customFormats: ReadonlyMap<number, string>;
  readonly cellXfNumFmtIds: readonly number[];
  readonly date1904: boolean;
};

const BUILTIN_FORMATS: Readonly<Record<number, string>> = {
  0: "General",
  1: "0",
  2: "0.00",
  3: "#,##0",
  4: "#,##0.00",
  5: "$#,##0;($#,##0)",
  6: "$#,##0;[Red]($#,##0)",
  7: "$#,##0.00;($#,##0.00)",
  8: "$#,##0.00;[Red]($#,##0.00)",
  9: "0%",
  10: "0.00%",
  11: "0.00E+00",
  12: "# ?/?",
  13: "# ??/??",
  14: "yyyy-mm-dd",
  15: "d-mmm-yy",
  16: "d-mmm",
  17: "mmm-yy",
  18: "h:mm AM/PM",
  19: "h:mm:ss AM/PM",
  20: "h:mm",
  21: "h:mm:ss",
  22: "yyyy-mm-dd hh:mm",
  27: 'yyyy"年"m"月"',
  28: 'm"月"d"日"',
  29: 'm"月"d"日"',
  30: "yyyy/m/d",
  31: 'yyyy"年"m"月"d"日"',
  32: 'h"时"mm"分"',
  33: 'h"时"mm"分"ss"秒"',
  34: 'yyyy"年"m"月"',
  35: 'm"月"d"日"',
  36: 'yyyy"年"m"月"',
  37: "#,##0;(#,##0)",
  38: "#,##0;[Red](#,##0)",
  39: "#,##0.00;(#,##0.00)",
  40: "#,##0.00;[Red](#,##0.00)",
  45: "mm:ss",
  46: "[h]:mm:ss",
  47: "mm:ss.0",
  48: "##0.0E+0",
  49: "@",
  50: 'yyyy"年"m"月"',
  51: 'm"月"d"日"',
  52: 'yyyy"年"m"月"',
  53: 'm"月"d"日"',
  54: 'm"月"d"日"',
  55: 'yyyy"年"m"月"',
  56: 'm"月"d"日"',
  57: 'yyyy"年"m"月"',
  58: 'm"月"d"日"',
};

const MAX_FORMAT_LENGTH = 512;
const DAY_MS = 86_400_000;
const EPOCH_1900_MS = Date.UTC(1899, 11, 30);
const EPOCH_1900_PRE_LEAP_MS = Date.UTC(1899, 11, 31);
const EPOCH_1904_MS = Date.UTC(1904, 0, 1);

const SHORT_MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"] as const;
const LONG_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October", "November", "December"] as const;
const SHORT_WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"] as const;
const LONG_WEEKDAYS = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday"] as const;

const splitSections = (pattern: string): string[] => {
  const sections: string[] = [];
  let current = "";
  let inQuotes = false;
  let inBrackets = false;
  for (let i = 0; i < pattern.length; i += 1) {
    const ch = pattern[i] ?? "";
    if (inQuotes) {
      current += ch;
      if (ch === '"') inQuotes = false;
      continue;
    }
    if (ch === "\\" && i + 1 < pattern.length) {
      current += ch + (pattern[i + 1] ?? "");
      i += 1;
      continue;
    }
    if (ch === '"') {
      inQuotes = true;
    } else if (ch === "[") {
      inBrackets = true;
    } else if (ch === "]" && inBrackets) {
      inBrackets = false;
    } else if (ch === ";" && !inBrackets) {
      sections.push(current);
      current = "";
      continue;
    }
    current += ch;
  }
  sections.push(current);
  return sections;
};

const pickSection = (
  sections: readonly string[],
  value: number,
): { section: string; implicitMinus: boolean } => {
  if (sections.length === 1) {
    return { section: sections[0] ?? "General", implicitMinus: value < 0 };
  }
  if (sections.length === 2) {
    return {
      section: (value < 0 ? sections[1] : sections[0]) ?? "General",
      implicitMinus: false,
    };
  }
  if (value > 0) return { section: sections[0] ?? "General", implicitMinus: false };
  if (value < 0) return { section: sections[1] ?? "General", implicitMinus: false };
  return { section: sections[2] ?? sections[0] ?? "General", implicitMinus: false };
};

type Token =
  | { kind: "literal"; text: string }
  | { kind: "pattern"; text: string };

type TokenizedSection = {
  tokens: Token[];
  hasElapsed: boolean;
  hasAmPm: boolean;
};

const parseBracketInside = (inside: string): { token: Token | null; elapsed: boolean } => {
  if (inside.startsWith("$")) {
    const dash = inside.indexOf("-", 1);
    const currency = (dash === -1 ? inside.slice(1) : inside.slice(1, dash)).trim();
    return {
      token: currency === "" ? null : { kind: "literal", text: currency },
      elapsed: false,
    };
  }
  const lower = inside.toLowerCase();
  if (/^(?:h+|m+|s+)$/.test(lower)) {
    return { token: { kind: "pattern", text: `[${lower}]` }, elapsed: true };
  }
  return { token: null, elapsed: false };
};

const tokenizeSection = (section: string): TokenizedSection => {
  const tokens: Token[] = [];
  let buffer = "";
  let hasElapsed = false;
  let hasAmPm = false;

  const flushPattern = (): void => {
    if (buffer !== "") {
      tokens.push({ kind: "pattern", text: buffer });
      buffer = "";
    }
  };

  for (let i = 0; i < section.length; i += 1) {
    const ch = section[i] ?? "";
    if (ch === '"') {
      flushPattern();
      const close = section.indexOf('"', i + 1);
      const lit = close === -1 ? section.slice(i + 1) : section.slice(i + 1, close);
      if (lit !== "") tokens.push({ kind: "literal", text: lit });
      i = close === -1 ? section.length : close;
    } else if (ch === "\\") {
      flushPattern();
      if (i + 1 < section.length) {
        tokens.push({ kind: "literal", text: section[i + 1] ?? "" });
        i += 1;
      }
    } else if (ch === "_" || ch === "*") {
      flushPattern();
      if (ch === "_" && i + 1 < section.length) tokens.push({ kind: "literal", text: " " });
      i += 1;
    } else if (ch === "[") {
      const close = section.indexOf("]", i + 1);
      if (close === -1) {
        buffer += ch;
      } else {
        flushPattern();
        const parsed = parseBracketInside(section.slice(i + 1, close));
        if (parsed.token !== null) tokens.push(parsed.token);
        if (parsed.elapsed) hasElapsed = true;
        i = close;
      }
    } else if (section.slice(i, i + 5).toLowerCase() === "am/pm") {
      flushPattern();
      hasAmPm = true;
      tokens.push({ kind: "pattern", text: "AM/PM" });
      i += 4;
    } else if (section.slice(i, i + 3).toLowerCase() === "a/p") {
      flushPattern();
      hasAmPm = true;
      tokens.push({ kind: "pattern", text: "A/P" });
      i += 2;
    } else {
      buffer += ch;
    }
  }
  flushPattern();
  return { tokens, hasElapsed, hasAmPm };
};

const isDateSection = (tokens: readonly Token[]): boolean => {
  for (const token of tokens) {
    if (token.kind !== "pattern") continue;
    if (token.text === "AM/PM" || token.text === "A/P" || token.text.startsWith("[")) {
      return true;
    }
    const withoutFracSec = token.text.replace(/s\.0+/gi, "s");
    const stripped = withoutFracSec.replace(/[^0#?eEyYmMdDhHsS]/g, "");
    if (stripped.length > 0 && !/[0#?]/.test(stripped) && /[yYmMdDhHsS]/.test(stripped)) {
      return true;
    }
  }
  return false;
};

const pad2 = (value: number): string => String(value).padStart(2, "0");

type DecodedExcelDate = {
  year: number;
  month: number;
  day: number;
  weekday: number;
  hours: number;
  minutes: number;
  seconds: number;
  fracSeconds: number;
  totalDays: number;
};

const decodeExcelSerial = (
  serial: number,
  date1904: boolean,
  fracPrecision = 0,
): DecodedExcelDate | null => {
  if (!Number.isFinite(serial) || serial < 0 || serial > 2_958_466) {
    return null;
  }
  const precision = Math.max(0, Math.min(3, Math.trunc(fracPrecision)));
  const scale = 10 ** precision;
  let wholeDays = Math.floor(serial);
  let totalScaled = Math.round((serial - wholeDays) * 86_400 * scale);
  if (totalScaled >= 86_400 * scale) {
    totalScaled -= 86_400 * scale;
    wholeDays += 1;
  }
  const totalSeconds = Math.floor(totalScaled / scale);
  const fracSeconds = totalScaled % scale;
  const hours = Math.floor(totalSeconds / 3600);
  const minutes = Math.floor((totalSeconds % 3600) / 60);
  const seconds = totalSeconds % 60;

  if (!date1904 && wholeDays === 0) {
    return {
      year: 1899,
      month: 12,
      day: 31,
      weekday: 0,
      hours,
      minutes,
      seconds,
      fracSeconds,
      totalDays: 0,
    };
  }
  if (!date1904 && wholeDays === 60) {
    return {
      year: 1900,
      month: 2,
      day: 29,
      weekday: 3,
      hours,
      minutes,
      seconds,
      fracSeconds,
      totalDays: 60,
    };
  }
  const ms = date1904
    ? EPOCH_1904_MS + wholeDays * DAY_MS
    : wholeDays < 60
      ? EPOCH_1900_PRE_LEAP_MS + (wholeDays - 1) * DAY_MS
      : EPOCH_1900_MS + wholeDays * DAY_MS;

  const dt = new Date(ms);
  return {
    year: dt.getUTCFullYear(),
    month: dt.getUTCMonth() + 1,
    day: dt.getUTCDate(),
    weekday: dt.getUTCDay(),
    hours,
    minutes,
    seconds,
    fracSeconds,
    totalDays: wholeDays,
  };
};

type DateSegment = { kind: "lit"; text: string } | { kind: "dir"; char: string; len: number };

const pushPatternDateSegments = (text: string, segments: DateSegment[]): void => {
  let idx = 0;
  while (idx < text.length) {
    const lower = (text[idx] ?? "").toLowerCase();
    const prevSeg = segments.at(-1);
    if ("ymdhs".includes(lower)) {
      let end = idx + 1;
      while (end < text.length && (text[end] ?? "").toLowerCase() === lower) end += 1;
      segments.push({ kind: "dir", char: lower, len: end - idx });
      idx = end;
      if (lower === "s") {
        const fracMatch = /^\.(0+)/.exec(text.slice(idx));
        if (fracMatch?.[1] !== undefined) {
          segments.push({ kind: "dir", char: "frac", len: Math.min(fracMatch[1].length, 3) });
          idx += fracMatch[0].length;
        }
      }
    } else if (idx === 0 && prevSeg?.kind === "dir" && prevSeg.char === "[s]") {
      const fracMatch = /^\.(0+)/.exec(text);
      if (fracMatch?.[1] !== undefined) {
        segments.push({ kind: "dir", char: "frac", len: Math.min(fracMatch[1].length, 3) });
        idx += fracMatch[0].length;
        continue;
      }
      segments.push({ kind: "lit", text: text[idx] ?? "" });
      idx += 1;
    } else {
      segments.push({ kind: "lit", text: text[idx] ?? "" });
      idx += 1;
    }
  }
};

const splitDateSegments = (tokens: readonly Token[]): DateSegment[] => {
  const segments: DateSegment[] = [];
  for (const token of tokens) {
    if (token.kind === "literal") {
      segments.push({ kind: "lit", text: token.text });
      continue;
    }
    const text = token.text;
    if (text === "AM/PM" || text === "A/P") {
      segments.push({ kind: "dir", char: text, len: text.length });
      continue;
    }
    if (text.startsWith("[")) {
      const inner = text.slice(1, -1);
      segments.push({ kind: "dir", char: `[${inner[0] ?? ""}]`, len: inner.length });
      continue;
    }
    pushPatternDateSegments(text, segments);
  }
  return segments;
};

const resolveMinuteMonthIndices = (segments: readonly DateSegment[]): Set<number> => {
  const dirs: Array<{ index: number; char: string; len: number }> = [];
  segments.forEach((seg, index) => {
    if (seg.kind === "dir") dirs.push({ index, char: seg.char, len: seg.len });
  });
  const result = new Set<number>();
  for (let i = 0; i < dirs.length; i += 1) {
    const cur = dirs[i];
    if (cur === undefined || cur.char !== "m" || cur.len > 2) continue;
    const prev = dirs[i - 1]?.char;
    const next = dirs[i + 1]?.char;
    if (prev === "h" || prev === "[h]" || next === "s" || next === "[s]") {
      result.add(cur.index);
    }
  }
  return result;
};

const renderMonth = (month: number, len: number, asMinute: boolean, minutes: number): string => {
  if (asMinute) return len >= 2 ? pad2(minutes) : String(minutes);
  if (len === 1) return String(month);
  if (len === 2) return pad2(month);
  if (len === 3) return SHORT_MONTHS[month - 1] ?? String(month);
  if (len === 4) return LONG_MONTHS[month - 1] ?? String(month);
  return (SHORT_MONTHS[month - 1] ?? "M").slice(0, 1);
};

const renderDay = (day: number, weekday: number, len: number): string => {
  if (len === 1) return String(day);
  if (len === 2) return pad2(day);
  if (len === 3) return SHORT_WEEKDAYS[weekday] ?? String(day);
  return LONG_WEEKDAYS[weekday] ?? String(day);
};

const renderElapsedOrClock = (
  char: string,
  len: number,
  decoded: DecodedExcelDate,
  hasAmPm: boolean,
): string => {
  const totalHours = decoded.totalDays * 24 + decoded.hours;
  const elapsed: Readonly<Record<string, string>> = {
    "[h]": String(totalHours).padStart(len, "0"),
    "[m]": String(totalHours * 60 + decoded.minutes).padStart(len, "0"),
    "[s]": String((totalHours * 60 + decoded.minutes) * 60 + decoded.seconds).padStart(len, "0"),
  };
  const matched = elapsed[char];
  if (matched !== undefined) return matched;
  if (char === "h") {
    const val = hasAmPm ? (decoded.hours % 12 === 0 ? 12 : decoded.hours % 12) : decoded.hours;
    return len >= 2 ? pad2(val) : String(val);
  }
  const isPm = decoded.hours >= 12;
  if (char === "AM/PM") return isPm ? "PM" : "AM";
  if (char === "A/P") return isPm ? "P" : "A";
  return "";
};

const renderDateDirective = (
  char: string,
  len: number,
  asMinute: boolean,
  decoded: DecodedExcelDate,
  hasAmPm: boolean,
): string => {
  if (char === "y") {
    return len <= 2 ? pad2(decoded.year % 100) : String(decoded.year).padStart(4, "0");
  }
  if (char === "m") return renderMonth(decoded.month, len, asMinute, decoded.minutes);
  if (char === "d") return renderDay(decoded.day, decoded.weekday, len);
  if (char === "s") return len >= 2 ? pad2(decoded.seconds) : String(decoded.seconds);
  if (char === "frac") return "." + String(decoded.fracSeconds).padStart(len, "0");
  return renderElapsedOrClock(char, len, decoded, hasAmPm);
};

const formatDateTimeSection = (
  serial: number,
  tokens: readonly Token[],
  options: { hasAmPm: boolean; date1904: boolean },
): string | null => {
  const segments = splitDateSegments(tokens);
  const fracPrecision = segments.reduce(
    (max, seg) => (seg.kind === "dir" && seg.char === "frac" ? Math.max(max, seg.len) : max),
    0,
  );
  const decoded = decodeExcelSerial(serial, options.date1904, fracPrecision);
  if (decoded === null) return null;
  const minuteIndices = resolveMinuteMonthIndices(segments);
  return segments
    .map((seg, idx) =>
      seg.kind === "lit"
        ? seg.text
        : renderDateDirective(seg.char, seg.len, minuteIndices.has(idx), decoded, options.hasAmPm),
    )
    .join("");
};

type ClassifiedNumeric = {
  prefix: string;
  suffix: string;
  corePattern: string;
  percentCount: number;
};

const classifyNumericTokens = (tokens: readonly Token[]): ClassifiedNumeric => {
  const prefixParts: string[] = [];
  const suffixParts: string[] = [];
  const coreParts: string[] = [];
  let seenNum = false;
  let percentCount = 0;

  for (const token of tokens) {
    if (token.kind === "literal") {
      (seenNum ? suffixParts : prefixParts).push(token.text);
      continue;
    }
    for (const ch of token.text) {
      if (ch === "%") {
        percentCount += 1;
        seenNum = true;
        suffixParts.push("%");
      } else if (/[0#?.]/.test(ch)) {
        seenNum = true;
        coreParts.push(ch);
      } else if (seenNum && (ch === "," || ch === "E" || ch === "e")) {
        coreParts.push(ch);
      } else if (
        seenNum &&
        (ch === "+" || ch === "-") &&
        (coreParts[coreParts.length - 1] === "E" || coreParts[coreParts.length - 1] === "e")
      ) {
        coreParts.push(ch);
      } else {
        (seenNum ? suffixParts : prefixParts).push(ch);
      }
    }
  }
  return {
    prefix: prefixParts.join(""),
    suffix: suffixParts.join(""),
    corePattern: coreParts.join(""),
    percentCount,
  };
};

const formatScientific = (
  value: number,
  parts: ClassifiedNumeric,
  implicitMinus: boolean,
): string | null => {
  const match = /^([0#?.,]+)([Ee])([+-]?[0#]+)(.*)$/.exec(parts.corePattern);
  if (match === null) return null;
  const [, mantissaPattern = "0.00", expMarker = "E", expPattern = "+00", tail = ""] = match;
  const dot = mantissaPattern.indexOf(".");
  const decimals = dot === -1 ? 0 : mantissaPattern.length - dot - 1;
  const [mPart = "0", rawExp = "+0"] = Math.abs(value)
    .toExponential(Math.min(decimals, 20))
    .split("e");
  const expNum = Number.parseInt(rawExp, 10);
  const expWidth = expPattern.replace(/[^0#]/g, "").length;
  const expSign = expNum < 0 ? "-" : expPattern.includes("+") ? "+" : "";
  const expDigits = String(Math.abs(expNum)).padStart(expWidth, "0");
  const sign = implicitMinus && value < 0 ? "-" : "";
  return `${parts.prefix}${sign}${mPart}${expMarker}${expSign}${expDigits}${tail}${parts.suffix}`;
};

const formatIntegerPart = (raw: string, rawInt: string, maxFracDigits: number): string => {
  const useGrouping = rawInt.includes(",");
  const minIntDigits = (rawInt.match(/0/g) ?? []).length;
  let intStr = raw;
  if (minIntDigits === 0 && intStr === "0" && maxFracDigits > 0 && !rawInt.includes("0")) {
    intStr = rawInt.includes("#") ? "" : "0";
  } else if (intStr.length < minIntDigits) {
    intStr = intStr.padStart(minIntDigits, "0");
  }
  return useGrouping && intStr.length > 3
    ? intStr.replace(/\B(?=(\d{3})+(?!\d))/g, ",")
    : intStr;
};

const formatFixedNumber = (
  absValue: number,
  corePattern: string,
  percentCount: number,
): string => {
  let pattern = corePattern;
  let trailingCommas = 0;
  while (pattern.endsWith(",")) {
    trailingCommas += 1;
    pattern = pattern.slice(0, -1);
  }
  const scaled = (absValue * 100 ** percentCount) / 1000 ** trailingCommas;
  const dot = pattern.indexOf(".");
  const rawInt = dot === -1 ? pattern : pattern.slice(0, dot);
  const rawFrac = dot === -1 ? "" : pattern.slice(dot + 1);
  const maxFracDigits = Math.min((rawFrac.match(/[0#?]/g) ?? []).length, 20);
  const minFracDigits = (rawFrac.match(/0/g) ?? []).length;

  const [rawIntOut = "0", initialFrac = ""] = Number(scaled.toPrecision(15))
    .toFixed(maxFracDigits)
    .split(".");
  const intStr = formatIntegerPart(rawIntOut, rawInt, maxFracDigits);
  let fracStr = initialFrac;
  while (fracStr.length > minFracDigits && fracStr.endsWith("0")) {
    fracStr = fracStr.slice(0, -1);
  }
  return fracStr.length > 0 ? `${intStr || "0"}.${fracStr}` : intStr || "0";
};

const formatNumericSection = (
  value: number,
  tokens: readonly Token[],
  implicitMinus: boolean,
): string => {
  const parts = classifyNumericTokens(tokens);
  if (parts.corePattern === "" || !/[0#?]/.test(parts.corePattern)) {
    const text = `${parts.prefix}${parts.suffix}`;
    return text !== "" ? text : String(value);
  }
  const scientific = formatScientific(value, parts, implicitMinus);
  if (scientific !== null) return scientific;
  const sign = implicitMinus && value < 0 ? "-" : "";
  const body = formatFixedNumber(Math.abs(value), parts.corePattern, parts.percentCount);
  return `${sign}${parts.prefix}${body}${parts.suffix}`;
};

export const formatExcelNumber = (
  rawValue: string,
  styleIndex: string | null,
  styles: CellFormatMap,
): string => {
  const num = Number(rawValue);
  if (!Number.isFinite(num)) return rawValue;

  const xfIndex = styleIndex !== null ? Number.parseInt(styleIndex, 10) : 0;
  const numFmtId =
    Number.isFinite(xfIndex) && xfIndex >= 0
      ? (styles.cellXfNumFmtIds[xfIndex] ?? 0)
      : 0;
  const pattern =
    styles.customFormats.get(numFmtId) ?? BUILTIN_FORMATS[numFmtId] ?? "General";

  if (pattern === "" || pattern === "General" || pattern === "@" || pattern.length > MAX_FORMAT_LENGTH) {
    return rawValue;
  }

  const { section, implicitMinus } = pickSection(splitSections(pattern), num);
  if (section === "" || section === "General" || section === "@") {
    return rawValue;
  }

  const parsed = tokenizeSection(section);
  if (isDateSection(parsed.tokens)) {
    const formattedDate = formatDateTimeSection(num, parsed.tokens, {
      hasAmPm: parsed.hasAmPm,
      date1904: styles.date1904,
    });
    if (formattedDate !== null) return formattedDate;
  }

  if (!parsed.hasElapsed) {
    return formatNumericSection(num, parsed.tokens, implicitMinus);
  }
  return rawValue;
};

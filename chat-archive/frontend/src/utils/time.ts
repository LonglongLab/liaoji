const MINUTE = 60;
const HOUR = 3_600;
const DAY = 86_400;

export const formatDateLabel = (unixSeconds: number): string => {
  const date = new Date(unixSeconds * 1000);
  return `${String(date.getFullYear())}年${String(date.getMonth() + 1)}月${String(date.getDate())}日`;
};

export const formatDateTimeSeparator = (unixSeconds: number): string =>
  `${formatDateLabel(unixSeconds)} ${formatTimeOfDay(unixSeconds)}`;

const startOfLocalDay = (date: Date): Date =>
  new Date(date.getFullYear(), date.getMonth(), date.getDate());

const WEEKDAY_NAMES = "日一二三四五六";

/** 差几个「日历日」，不是差几个 86400 秒：跨日才换说法，几点发的不影响。 */
const localDaysAgo = (target: Date, now: Date): number =>
  Math.round(
    (startOfLocalDay(now).getTime() - startOfLocalDay(target).getTime()) / (DAY * 1_000),
  );

const weekdayLabel = (target: Date): string =>
  `星期${WEEKDAY_NAMES[target.getDay()] ?? ""}`;

const monthDayLabel = (target: Date): string =>
  `${String(target.getMonth() + 1)}月${String(target.getDate())}日`;

/**
 * 微信的日期口径：越近说得越口语，越远说得越具体——今天不提日期，昨天点名昨天，
 * 一周内报星期，本年报月日，跨年补年份。
 *
 * 星期只用到「6 天前」为止：7 天前和今天是同一个星期名，说「星期三」反而更不清楚。
 * 时刻始终跟在后面，因为分隔要回答的是「这条什么时候发的」，只给日期等于没说。
 */
export const formatWechatTimestamp = (
  unixSeconds: number,
  nowMs: number = Date.now(),
): string => {
  const target = new Date(unixSeconds * 1000);
  const now = new Date(nowMs);
  const timeOfDay = formatTimeOfDay(unixSeconds);
  const daysAgo = localDaysAgo(target, now);
  if (daysAgo === 0) return timeOfDay;
  if (daysAgo === 1) return `昨天 ${timeOfDay}`;
  if (daysAgo > 1 && daysAgo < 7) return `${weekdayLabel(target)} ${timeOfDay}`;
  if (target.getFullYear() === now.getFullYear()) {
    return `${monthDayLabel(target)} ${timeOfDay}`;
  }
  return `${formatDateLabel(unixSeconds)} ${timeOfDay}`;
};

/** 同一套日期口径，只是列表一行放不下时刻，跨日之后就只留日期。 */
export const formatConversationListTime = (
  unixSeconds: number | null,
  nowMs: number = Date.now(),
): string => {
  if (unixSeconds === null) return "";
  const target = new Date(unixSeconds * 1000);
  const now = new Date(nowMs);
  const daysAgo = localDaysAgo(target, now);
  if (daysAgo === 0) return formatTimeOfDay(unixSeconds);
  if (daysAgo === 1) return "昨天";
  if (daysAgo > 1 && daysAgo < 7) return weekdayLabel(target);
  if (target.getFullYear() === now.getFullYear()) return monthDayLabel(target);
  return formatDateLabel(unixSeconds);
};

/**
 * 查找结果里的时间跟微信一样用绝对时间：当天只给时刻，本年补月日，跨年补年份。
 * 「3 天前」在翻档案时没有定位价值，这里不再用相对时间。
 */
export const formatSearchHitTime = (
  unixSeconds: number,
  nowMs: number = Date.now(),
): string => {
  if (isSameLocalDay(unixSeconds, nowMs / 1000)) return formatTimeOfDay(unixSeconds);
  const target = new Date(unixSeconds * 1000);
  const timeOfDay = formatTimeOfDay(unixSeconds);
  if (target.getFullYear() === new Date(nowMs).getFullYear()) {
    return `${String(target.getMonth() + 1)}月${String(target.getDate())}日 ${timeOfDay}`;
  }
  return `${formatDateLabel(unixSeconds)} ${timeOfDay}`;
};

export const formatRelativeTime = (
  unixSeconds: number | null,
  nowMs: number = Date.now(),
): string => {
  if (unixSeconds === null) {
    return "尚无消息";
  }
  const delta = Math.floor(nowMs / 1000) - unixSeconds;
  if (delta < MINUTE) return "刚刚";
  if (delta < HOUR) return `${String(Math.floor(delta / MINUTE))} 分钟前`;
  if (delta < DAY) return `${String(Math.floor(delta / HOUR))} 小时前`;
  if (delta < 7 * DAY) return `${String(Math.floor(delta / DAY))} 天前`;
  return formatDateLabel(unixSeconds);
};

export const formatTimeOfDay = (unixSeconds: number): string => {
  const date = new Date(unixSeconds * 1000);
  const pad = (value: number): string => String(value).padStart(2, "0");
  return `${pad(date.getHours())}:${pad(date.getMinutes())}`;
};

// Transfers are the one place seconds carry meaning: two of them a few seconds
// apart are two different payments, and the client itself prints them.
export const formatDateTimeWithSeconds = (unixSeconds: number): string => {
  const date = new Date(unixSeconds * 1000);
  const pad = (value: number): string => String(value).padStart(2, "0");
  const clock = `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
  return `${formatDateLabel(unixSeconds)} ${clock}`;
};

const parseDateInput = (value: string): Date | null => {
  const match = /^(\d{4})-(\d{2})-(\d{2})$/.exec(value);
  if (match === null) return null;
  const [, year, month, day] = match;
  return new Date(Number(year), Number(month) - 1, Number(day));
};

export const dateInputToUnixStart = (value: string): number | null => {
  const date = parseDateInput(value);
  return date === null ? null : date.getTime() / 1000;
};

export const dateInputToUnixEnd = (value: string): number | null => {
  const date = parseDateInput(value);
  if (date === null) return null;
  date.setHours(23, 59, 59, 0);
  return date.getTime() / 1000;
};

export const isSameLocalDay = (a: number, b: number): boolean => {
  const left = new Date(a * 1000);
  const right = new Date(b * 1000);
  return (
    left.getFullYear() === right.getFullYear() &&
    left.getMonth() === right.getMonth() &&
    left.getDate() === right.getDate()
  );
};

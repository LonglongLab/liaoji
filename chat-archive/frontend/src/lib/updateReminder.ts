import type { UpdateStatus } from "@/api/archiveApi";

// 旧键：「以后再说」曾经是永久的——同一版本一旦点过就再也不弹。PSU-2.222 改成
// 24 小时暂缓；读到旧记录就删掉，让被永久压住的那一版恢复一次提醒。
const LEGACY_DISMISSED_VERSION_KEY = "chat-archive:update-reminder:dismissed-version";
const POSTPONED_KEY = "chat-archive:update-reminder:postponed";
export const UPDATE_REMINDER_POSTPONE_MS = 24 * 60 * 60 * 1000;
const BUBBLE_DISMISSED_VERSION_KEY = "chat-archive:update-reminder:bubble-dismissed-version";

const read = (key: string): string | null => {
  try {
    return globalThis.localStorage.getItem(key);
  } catch {
    return null;
  }
};

const write = (key: string, version: string): void => {
  try {
    globalThis.localStorage.setItem(key, version);
  } catch {
    // 无痕模式 / WebView 存储损坏时仍可更新，只是下次会再提醒一次。
  }
};

const remove = (key: string): void => {
  try {
    globalThis.localStorage.removeItem(key);
  } catch {
    // 同上：删不掉只会多提醒一次。
  }
};

export const readPostponed = (): { version: string; until: number } | null => {
  const raw = read(POSTPONED_KEY);
  if (raw === null) return null;
  try {
    const parsed: unknown = JSON.parse(raw);
    if (typeof parsed !== "object" || parsed === null) return null;
    const { version, until } = parsed as { version?: unknown; until?: unknown };
    if (typeof version !== "string" || typeof until !== "number" || !Number.isFinite(until)) {
      return null;
    }
    return { version, until };
  } catch {
    return null;
  }
};

export const stagedUpdateVersion = (status: UpdateStatus | undefined): string | null => {
  if (
    status === undefined ||
    !status.pending_restart ||
    status.active_version === null ||
    status.active_version === status.current_version
  ) {
    return null;
  }
  return status.active_version;
};

export const offeredUpdateVersion = (status: UpdateStatus | undefined): string | null => {
  if (status === undefined || stagedUpdateVersion(status) !== null) return null;
  const offered = status.available_version;
  return offered === null || offered === status.current_version ? null : offered;
};

export const reminderVersion = (status: UpdateStatus | undefined): string | null => {
  const version = stagedUpdateVersion(status) ?? offeredUpdateVersion(status);
  if (version === null || status === undefined || !status.configured) return null;
  // 设置页也会把「回到上一版」写成 pending_restart。回退不属于新版本提醒。
  const candidate = version.split(/[.+-]/).map((part) => Number.parseInt(part, 10) || 0);
  const current = status.current_version.split(/[.+-]/).map((part) => Number.parseInt(part, 10) || 0);
  for (let index = 0; index < Math.max(candidate.length, current.length); index += 1) {
    const difference = (candidate[index] ?? 0) - (current[index] ?? 0);
    if (difference !== 0) return difference > 0 ? version : null;
  }
  return null;
};

/** 这一版的弹窗此刻是否处在「以后再说」的 24 小时里。 */
export const updateReminderWasDismissed = (version: string, now: number = Date.now()): boolean => {
  // 旧的永久记录不再算数：删掉，这一次照常弹。
  remove(LEGACY_DISMISSED_VERSION_KEY);
  const postponed = readPostponed();
  if (postponed === null || postponed.version !== version) return false;
  // 系统时钟往回拨过时 `until` 会远在未来；超过一个暂缓周期的一律当已到期。
  const remaining = postponed.until - now;
  return remaining > 0 && remaining <= UPDATE_REMINDER_POSTPONE_MS;
};

export const updateBubbleWasDismissed = (version: string): boolean =>
  read(BUBBLE_DISMISSED_VERSION_KEY) === version;

/** 「以后再说」：24 小时内这一版不再弹窗；气泡与设置入口的小红点不受影响。 */
export const dismissUpdateReminder = (version: string, now: number = Date.now()): void => {
  remove(LEGACY_DISMISSED_VERSION_KEY);
  write(POSTPONED_KEY, JSON.stringify({ version, until: now + UPDATE_REMINDER_POSTPONE_MS }));
};

export const dismissUpdateBubble = (version: string): void => {
  write(BUBBLE_DISMISSED_VERSION_KEY, version);
};

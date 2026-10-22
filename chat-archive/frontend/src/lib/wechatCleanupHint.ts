/**
 * 浮窗的第二道校验（PSU-2.188）：用户在微信里实际选的范围，是否全部落在远端已同步到的
 * 那一天之内。只认这一件事——已同步才变绿、提示去点「清理」。
 *
 * 「全部时间」「三个月内」**永远是红**：两者都包含「此刻」，而此刻之后进来的消息
 * 不可能已经同步；自定义时间看结束日期与 `syncedUntil`。读不到就是灰，读到之前不会变绿。
 */

import type { WechatCleanupObserved } from "@/api/archiveApi";

export type HintTone = "red" | "green" | "gray";

export interface HintVerdict {
  readonly tone: HintTone;
  readonly code:
    | "unknown"
    | "all_time"
    | "recent_3m"
    | "custom_no_dates"
    | "custom_unsynced"
    | "custom_ok"
    | "editing"
    | "expired"
    | "wrong_session"
    | "session_unknown"
    | "wrong_type"
    | "type_unknown"
    | "custom_ok_cuts_kept";
  /** 第一天还没同步的日期（红色时说「从这天起还没同步」）。 */
  readonly firstUnsynced?: string;
  readonly end?: string;
  /** 比建议日期多清了几天（`custom_ok_cuts_kept`）。 */
  readonly extraDays?: number;
}

export interface HintContext {
  /** 从最早起连续全部已同步的最后一天。 */
  readonly syncedUntil: string;
  /** 建议结束日期（按「保留最近」算、且不晚于 syncedUntil）。 */
  readonly suggestedEnd: string;
  readonly now?: number;
}

/**
 * 驱动停在半路时，告诉用户卡在哪、剩下的怎么手动点（原型第 3 块）。
 * `reason` 是后端的稳定代号；不认识的一律按「没能自动走完」说。
 */
export function driveStopMessage(reason: string | null): string {
  switch (reason) {
    case "cancelled":
      return "本次操作已取消。";
    case "login_timeout":
      return "等待登录已超时，请登录后重新开始。";
    case "wechat_not_found":
      return "没找到可操作的微信窗口。请打开微信主窗口并登录后重试，不要只留在托盘。";
    case "menu_not_found":
    case "settings_not_found":
      return "没能打开微信的设置。请在微信左下角「≡ → 设置」手动打开。";
    case "storage_button_not_found":
    case "overview_not_found":
      return "没找到「存储空间」的「管理」。请在设置的「账号与存储」里手动点它。";
    case "chat_data_button_not_found":
      return "等了一会儿没看到「聊天数据」旁的「管理」按钮。请在存储空间页手动点「聊天数据 → 管理」。";
    case "manage_page_not_found":
      return "没打开「管理微信聊天数据」页。请手动点「聊天数据 → 管理」。";
    case "search_icon_not_found":
      return "没找到清理页的搜索。请在列表里自己找到这个会话并勾上。";
    case "not_found":
      return "在微信的清理页里没搜到这个会话。请自己找到它并勾上。";
    case "duplicate":
      return "搜到了同名的几个会话，分不出是哪个。请你自己勾。";
    case "tick_unverified":
    case "selection_unverified":
      return "点了勾选，但没看到它勾上。请确认一下这个会话有没有勾上。";
    case "not_foreground":
      return "微信被别的窗口挡住了，聊记停了手。请把微信切到前面，自己完成剩下几步。";
    default:
      return "聊记没能自动走完。请在微信里手动打开清理页，找到这个会话并勾上。";
  }
}

const KEEP_MONTHS_KEY = "chat-archive:wechat-cleanup-keep-months";

/** 「保留最近」记住上次的选择（本机设置，localStorage）；没选过是 3 个月。 */
export function loadKeepMonths(): 1 | 3 | 6 | 12 {
  try {
    const value = Number(localStorage.getItem(KEEP_MONTHS_KEY));
    return value === 1 || value === 6 || value === 12 ? value : 3;
  } catch {
    return 3;
  }
}

export function saveKeepMonths(value: 1 | 3 | 6 | 12): void {
  try {
    localStorage.setItem(KEEP_MONTHS_KEY, String(value));
  } catch {
    // 存不下就下次还是 3 个月，不影响这一次。
  }
}

/**
 * 每隔 `ms` 跑一次，返回停止函数。在 .ts 里而不是组件内联：`.vue` 上 ESLint 的
 * `no-undef` 没配 browser globals（与 `lib/clock.ts` 同一条理由）。
 */
export const every = (onTick: () => void, ms: number): (() => void) => {
  const handle = setInterval(onTick, ms);
  return () => {
    clearInterval(handle);
  };
};

export function addDays(day: string, days: number): string {
  const [year, month, date] = day.split("-").map(Number) as [number, number, number];
  const moved = new Date(year, month - 1, date + days);
  const mm = String(moved.getMonth() + 1).padStart(2, "0");
  const dd = String(moved.getDate()).padStart(2, "0");
  return `${String(moved.getFullYear())}-${mm}-${dd}`;
}

function daysBetween(from: string, to: string): number {
  const [fy, fm, fd] = from.split("-").map(Number) as [number, number, number];
  const [ty, tm, td] = to.split("-").map(Number) as [number, number, number];
  return Math.round((Date.UTC(ty, tm - 1, td) - Date.UTC(fy, fm - 1, fd)) / 86_400_000);
}

export function judgeRange(
  observed: WechatCleanupObserved | null,
  context: HintContext,
): HintVerdict {
  const firstUnsynced = addDays(context.syncedUntil, 1);
  if (observed === null || observed.range === "unknown") {
    return { tone: "gray", code: "unknown" };
  }
  if (context.now !== undefined && (observed.seen_at === null || context.now - observed.seen_at > 120)) {
    return { tone: "gray", code: "expired" };
  }
  if (observed.range === "all") {
    return { tone: "red", code: "all_time", firstUnsynced };
  }
  if (observed.range === "recent3m") {
    return { tone: "red", code: "recent_3m", firstUnsynced };
  }
  if (observed.end === null) {
    return { tone: "gray", code: "custom_no_dates" };
  }
  // 日期是 YYYY-MM-DD，字典序就是时间序。
  if (observed.end > context.syncedUntil) {
    return { tone: "red", code: "custom_unsynced", firstUnsynced, end: observed.end };
  }
  if (observed.selection_ok === false) return { tone: "red", code: "wrong_session" };
  if (observed.type_ok === false) return { tone: "red", code: "wrong_type" };
  if (observed.applied !== true) return { tone: "gray", code: "editing" };
  if (observed.selection_ok !== true) return { tone: "gray", code: "session_unknown" };
  if (observed.type_ok !== true) return { tone: "gray", code: "type_unknown" };
  if (observed.end > context.suggestedEnd) {
    return {
      tone: "green",
      code: "custom_ok_cuts_kept",
      end: observed.end,
      extraDays: daysBetween(context.suggestedEnd, observed.end),
    };
  }
  return { tone: "green", code: "custom_ok", end: observed.end };
}

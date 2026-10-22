import type { ArchiveApi, BrowserClientInfo } from "@/api/archiveApi";

interface ClientHints {
  platform: string;
  mobile: boolean;
  brands: ReadonlyArray<{ brand: string; version: string }>;
  getHighEntropyValues?: (hints: string[]) => Promise<{ model?: string }>;
}

export interface BrowserNavigator {
  userAgent: string;
  userAgentData?: ClientHints;
}

const label = (value: unknown, limit: number): string | null => {
  if (typeof value !== "string" || value.length > limit || Array.from(value).some(character => {
    const code = character.codePointAt(0) ?? 0;
    return code < 32 || code === 127 || (code >= 0xd800 && code <= 0xdfff);
  })) return null;
  return value.trim() || null;
};

const browserFrom = (ua: string, hints?: ClientHints): string | null => {
  const names: Readonly<Record<string, string>> = {
    "Microsoft Edge": "Edge", "Opera": "Opera", "Google Chrome": "Chrome", "Firefox": "Firefox", "Chromium": "Chromium",
  };
  for (const brand of Object.keys(names)) {
    if (hints?.brands.some((entry) => entry.brand === brand)) return names[brand] ?? null;
  }
  if (/Edg[A-Za-z]*\//.test(ua)) return "Edge";
  if (/OPR\//.test(ua)) return "Opera";
  if (/FxiOS|Firefox\//.test(ua)) return "Firefox";
  if (/CriOS|Chrome\//.test(ua)) return "Chrome";
  if (/Safari\//.test(ua)) return "Safari";
  return null;
};

/** Do not persist the UA, frozen UA platform versions, or a fingerprint. */
export const readBrowserClientInfo = async (
  nav: BrowserNavigator = globalThis.navigator, timeoutMs = 500,
): Promise<BrowserClientInfo> => {
  const ua = nav.userAgent.slice(0, 1024);
  const hints = nav.userAgentData;
  const platform = label(hints?.platform, 32) ??
    (/Android/.test(ua) ? "Android" : /iPhone|iPad|iPod/.test(ua) ? "iOS" :
      /Windows/.test(ua) ? "Windows" : /CrOS/.test(ua) ? "Chrome OS" : /Macintosh/.test(ua) ? "macOS" : /Linux/.test(ua) ? "Linux" : null);
  const device_type: BrowserClientInfo["device_type"] = /iPad|Tablet/.test(ua) ||
    (platform === "Android" && hints?.mobile === false) ? "tablet" :
    hints?.mobile === true || /iPhone|iPod|Mobile/.test(ua) ? "mobile" :
    platform === null ? "unknown" : "desktop";
  let model: string | null = null;
  if (hints?.getHighEntropyValues !== undefined) {
    let timer: ReturnType<typeof setTimeout> | undefined;
    try {
      const result = await Promise.race([
        hints.getHighEntropyValues(["model"]),
        new Promise<{ model?: string }>((resolve) => { timer = setTimeout(() => resolve({}), timeoutMs); }),
      ]);
      model = label(result.model, 80);
    } catch { /* Withheld hints are normal; retain the platform/browser fallback. */ }
    finally { clearTimeout(timer); }
  } else if (platform === "Android") {
    model = label(ua.match(/Android[^;)]*;\s*(?:[a-z]{2}[-_][A-Z]{2};\s*)?([^;)]+?)(?:\s+Build\/[^;)]*)?\)/)?.[1], 80);
  }
  if (model !== null && /^(K|unknown|generic)$/i.test(model)) model = null;
  return { model, platform, browser: browserFrom(ua, hints), device_type };
};

/** Optional enrichment must neither fail nor hold up signing in on an older server. */
export const recordBrowserClientInfo = async (api: ArchiveApi): Promise<boolean> => {
  try {
    const info = await readBrowserClientInfo();
    if (info.platform === null && info.browser === null && info.model === null) return false;
    await api.recordBrowserClientInfo(info);
    return true;
  } catch { return false; }
};

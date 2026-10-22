/**
 * 本机应用锁（PSU-2.221，前身 PSU-2.213 §14.12）界面共用的小件：对话框模式、
 * 登记时的设备名、本机解锁方式的叫法。
 *
 * 应用锁只对这台设备生效：每台设备各自开关、各自录入自己的指纹 / 面容 / Windows Hello，
 * 设置页只显示本机这一行，不列其他设备。设备名不让用户手填，登记时按这台浏览器的平台与
 * 浏览器自动起名（「Windows · Edge」），只用于服务端审计与排错。
 */
import type { BrowserClientInfo } from "@/api/archiveApi";

/** 对话框做哪件事。`reenroll` = 指纹换了 / 换了 Windows Hello：口令确认后替换本机这一枚。 */
export type AppLockMode = "enable" | "reenroll" | "disable";

const FALLBACK = "这台设备";

export const passkeyLabelFor = (info: BrowserClientInfo): string => {
  const device = info.model ?? info.platform;
  const parts = [device, info.browser].filter((part): part is string => part !== null && part !== "");
  return parts.length > 0 ? parts.join(" · ") : FALLBACK;
};

export interface LocalUnlockMethod {
  /** 设置页那一行「本机 · …」后半截。 */
  readonly name: string;
  /** 锁定页主按钮。 */
  readonly unlockLabel: string;
}

/**
 * 本机的系统验证按平台叫什么。只看 UA 的粗分类、同步可得（锁定页一打开就要画按钮），
 * 猜错也只是叫法不同：真正弹什么框由操作系统决定。
 */
export const localUnlockMethod = (userAgent: string = globalThis.navigator.userAgent): LocalUnlockMethod => {
  if (/Android|iPhone|iPad|iPod|Mobile/.test(userAgent)) {
    return { name: "指纹或面容", unlockLabel: "用指纹或面容解锁" };
  }
  if (/Windows/.test(userAgent)) return { name: "Windows Hello", unlockLabel: "用 Windows Hello 解锁" };
  if (/Macintosh/.test(userAgent)) return { name: "触控 ID", unlockLabel: "用触控 ID 解锁" };
  return { name: "系统验证", unlockLabel: "用系统验证解锁" };
};

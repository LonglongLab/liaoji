/**
 * 锁定时清掉浏览器内存里的私人数据（PSU-2.213）。
 *
 * 日常解锁锁上之后，服务端对私人接口一律答 423；但已经拉到浏览器里的会话列表、
 * 消息还在查询缓存里。锁定页挡住了界面，挡不住「返回」键恢复出来的旧页面——
 * 所以锁定的那一刻把它们清掉。
 *
 * 头像的持久缓存（Cache Storage）**故意不清**（用户 2026-10-17 决定）：头像不按
 * 私人内容处理，每次锁定都清会让解锁后整页头像重新下载一遍。只关掉内存里那份
 * Object URL，锁定页上不留头像；解锁后从持久缓存直接取回，不走网络。退出登录
 * 仍按原规则把两份都清掉（见 `stores/remoteAuth.ts`）。
 */
import type { QueryClient } from "@tanstack/vue-query";

import { closeSharedAvatarStore } from "./remoteAvatarStore";

let queryClient: QueryClient | null = null;

/** `main.ts` 创建查询客户端后登记一次；测试里可以登记替身。 */
export const registerPrivateQueryClient = (client: QueryClient | null): void => {
  queryClient = client;
};

export const purgePrivateCaches = (): Promise<void> => {
  queryClient?.clear();
  closeSharedAvatarStore();
  return Promise.resolve();
};

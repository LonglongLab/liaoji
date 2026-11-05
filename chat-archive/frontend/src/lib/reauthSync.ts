/**
 * 页面这一侧对「锁定 / 解锁」的反应，集中在一处（PSU-2.213）。
 *
 * 三个入口，同一套处理：
 *
 * - 本页某个私人接口答 423：清掉已拉到的私人数据、去锁定页，并广播给其他标签页。
 * - 其他标签页广播「变了」：重新问一次服务端，按答复去锁定页或离开锁定页。
 * - 页面从往返缓存（bfcache）恢复：同上，重新问一次。
 *
 * 公开分享页一律不动：它不属于任何登录用户，分享者自己锁没锁与它无关。
 */
import type { Router } from "vue-router";
import { watch } from "vue";

import type { ArchiveApi } from "@/api/archiveApi";
import { useCapabilityStore } from "@/stores/capabilities";
import { useReauthStore } from "@/stores/reauth";

import { purgePrivateCaches } from "./privateCaches";
import {
  announceReauth,
  browserReauthChannel,
  type ReauthChannel,
  setReauthChannel,
  signalOf,
} from "./reauthChannel";
import { isShareWorkspace, UNLOCK_PATH, unlockLocation, unlockNextPath } from "./workspaceRouting";

export interface ReauthSyncOptions {
  readonly router: Router;
  readonly api: ArchiveApi;
  /** 缺省用浏览器的 BroadcastChannel；测试注入替身，传 null 表示不广播。 */
  readonly channel?: ReauthChannel | null;
}

export interface ReauthSync {
  /** 本页拿到 423。 */
  readonly onReauthRequired: () => void;
  /** 重新问一次端状态，按答复去锁定页或离开锁定页。 */
  readonly recheck: () => Promise<void>;
  readonly dispose: () => void;
}

export const installReauthSync = (options: ReauthSyncOptions): ReauthSync => {
  const { router, api } = options;
  const channel = options.channel === undefined ? browserReauthChannel() : options.channel;
  const reauth = useReauthStore();
  let expiryTimer: ReturnType<typeof globalThis.setTimeout> | undefined;
  let disposed = false;

  const onShare = (): boolean => isShareWorkspace(router.currentRoute.value.meta.workspace);

  const showLock = (): void => {
    void purgePrivateCaches();
    const current = router.currentRoute.value;
    if (onShare() || current.path === UNLOCK_PATH) return;
    void router.replace(unlockLocation(current.fullPath));
  };

  const scheduleExpiry = (): void => {
    globalThis.clearTimeout(expiryTimer);
    const until = reauth.state?.unlocked_until;
    if (disposed || until === null || until === undefined || reauth.state?.locked) return;
    expiryTimer = globalThis.setTimeout(() => {
      onReauthRequired();
      void recheck();
    }, Math.max(1000, until * 1000 - Date.now()));
  };

  const refreshState = async (): Promise<boolean> => {
    const capabilities = useCapabilityStore();
    if (capabilities.deployment !== "remote" || capabilities.states["browser-session"] !== "available") return true;
    const ok = await reauth.refresh(api);
    if (disposed || onShare()) return false;
    if (!ok || reauth.state?.locked) {
      capabilities.authState = "reauth_required";
      showLock();
      return false;
    }
    scheduleExpiry();
    return true;
  };

  const recheck = async (): Promise<void> => {
    if (onShare()) return;
    const capabilities = useCapabilityStore();
    capabilities.loaded = false;
    await capabilities.ensureLoaded(api);
    // 等答复的这段时间里人可能已经走到分享页了。
    if (disposed || onShare()) return;
    if (!capabilities.$state.loaded) {
      // A stale authenticated value is not proof after a failed recheck.
      capabilities.authState = "reauth_required";
      showLock();
      return;
    }
    if (capabilities.authState === "reauth_required") {
      showLock();
      return;
    }
    // 退出登录、服务连不上等其余情况交给下一次导航与 401 处理，这里不抢答。
    if (capabilities.authState !== "authenticated") return;
    const current = router.currentRoute.value;
    if (!(await refreshState())) return;
    if (current.path === UNLOCK_PATH) {
      // 别的标签页刚解锁（或关掉了日常解锁）：这一页不用再验，回到原来要去的地方。
      await router.replace(unlockNextPath(current.query.next));
      return;
    }
    // 设置页开着的话，「日常解锁」卡上的状态跟着更新。
  };

  const onReauthRequired = (): void => {
    const capabilities = useCapabilityStore();
    capabilities.loaded = false;
    capabilities.authState = "reauth_required";
    showLock();
    // 同一浏览器的标签页共用一条登录会话与解锁授权：这里过期了，别处也一样。
    announceReauth("locked");
  };

  channel?.listen((data) => {
    if (signalOf(data) === null) return;
    void recheck();
  });
  setReauthChannel(channel);
  const stopWatching = watch(() => reauth.state?.unlocked_until, scheduleExpiry);
  const removeAfterEach = router.afterEach((to) => {
    if (to.meta.workspace === "remote" && useCapabilityStore().authState === "authenticated") {
      void refreshState();
    }
  });
  const onVisible = (): void => {
    if (document.visibilityState !== "visible" || onShare()) return;
    const until = reauth.state?.unlocked_until;
    if (until !== null && until !== undefined && until * 1000 <= Date.now()) onReauthRequired();
    void recheck();
  };
  document.addEventListener("visibilitychange", onVisible);

  return {
    onReauthRequired,
    recheck,
    dispose: () => {
      disposed = true;
      globalThis.clearTimeout(expiryTimer);
      stopWatching();
      removeAfterEach();
      document.removeEventListener("visibilitychange", onVisible);
      setReauthChannel(null);
      channel?.close();
    },
  };
};

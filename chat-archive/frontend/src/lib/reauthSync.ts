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

  const onShare = (): boolean => isShareWorkspace(router.currentRoute.value.meta.workspace);

  const showLock = (): void => {
    void purgePrivateCaches();
    const current = router.currentRoute.value;
    if (onShare() || current.path === UNLOCK_PATH) return;
    void router.replace(unlockLocation(current.fullPath));
  };

  const recheck = async (): Promise<void> => {
    if (onShare()) return;
    const capabilities = useCapabilityStore();
    capabilities.loaded = false;
    await capabilities.ensureLoaded(api);
    // 等答复的这段时间里人可能已经走到分享页了。
    if (onShare()) return;
    if (capabilities.authState === "reauth_required") {
      showLock();
      return;
    }
    // 退出登录、服务连不上等其余情况交给下一次导航与 401 处理，这里不抢答。
    if (capabilities.authState !== "authenticated") return;
    const current = router.currentRoute.value;
    if (current.path === UNLOCK_PATH) {
      // 别的标签页刚解锁（或关掉了日常解锁）：这一页不用再验，回到原来要去的地方。
      await router.replace(unlockNextPath(current.query.next));
      return;
    }
    // 设置页开着的话，「日常解锁」卡上的状态跟着更新。
    const reauth = useReauthStore();
    if (reauth.state !== null) await reauth.refresh(api);
  };

  const onReauthRequired = (): void => {
    useCapabilityStore().loaded = false;
    showLock();
    // 同一浏览器的标签页共用一条登录会话与解锁授权：这里过期了，别处也一样。
    announceReauth("locked");
  };

  channel?.listen((data) => {
    if (signalOf(data) === null) return;
    void recheck();
  });
  setReauthChannel(channel);

  return {
    onReauthRequired,
    recheck,
    dispose: () => {
      setReauthChannel(null);
      channel?.close();
    },
  };
};

/**
 * 整个应用共用的那一份头像 store（PSU-2.122）。
 *
 * 此前每个视图各建一份：工作台一份、搜索页一份、`AppShell` 一份，而 `AppShell`
 * 是**每个视图各渲染一个**。于是底部 Tab 从「会话」切到「搜索」再切回来，
 * 三份缓存一起被 `onScopeDispose` 关掉，整页头像重下一遍——用户看到的
 * 「每次回到列表都在重新加载」就是这个。
 *
 * 现在只有这一份，寿命是**整个标签页**，不是某个组件。内存由两道闸兜底：
 * store 自己的闲置队列（`maxIdle`）与持久层的条数上限。
 *
 * 为什么不按归档/账号分键：`blob_id` 是账号加盐的内容标识，跨账号跨归档不会
 * 撞，也就不存在"拿到上一个账号的图"这回事。分键只会让来回切账号各重下一遍。
 */

import type { RemoteAttachmentUrl } from "@/api/archiveApi";

import { AvatarObjectUrlStore } from "./avatarObjectUrls";
import { persistentAvatarCache } from "./avatarPersistentCache";
import { fetchRemoteAttachmentBytes } from "./remoteReadModel";

/**
 * 闲置（无人订阅）最多留几个 objectURL。
 *
 * 按实测中位 4.6 KB 算，256 张 ≈ 1.2 MB 常驻。这是"滚出去再滚回来不重下"的
 * 那段余量：一页 100 条会话滚两屏也还在里面。
 */
const SHARED_MAX_IDLE = 256;

let shared: AvatarObjectUrlStore | null = null;

/**
 * 那一份 store。第一个调用方交进来的 `load` 说了算——`api` 在应用里是单例，
 * 后面的调用方交进来的是同一条路。
 */
export const sharedAvatarStore = (
  load: (blobId: string) => Promise<Blob>,
): AvatarObjectUrlStore => {
  shared ??= new AvatarObjectUrlStore({
    load,
    persistent: persistentAvatarCache(),
    maxIdle: SHARED_MAX_IDLE,
  });
  return shared;
};

/**
 * 三个用头像的地方（工作台、搜索页、顶栏账号）都这么取：交一条「怎么问签名」
 * 进来，字节怎么取、存哪儿、什么时候回收都在这一层里。
 *
 * **第二个调用方交进来的 `resolveUrl` 不生效**——store 只建一次。`api` 在应用
 * 里是单例，三处交的本来就是同一条路；写在这里是为了它哪天不再是的时候，
 * 读到这行的人知道去哪儿看。
 */
export const sharedAvatarStoreVia = (
  resolveUrl: (blobId: string) => Promise<RemoteAttachmentUrl>,
): AvatarObjectUrlStore =>
  sharedAvatarStore((blobId) => fetchRemoteAttachmentBytes(resolveUrl, blobId));

/**
 * 关掉并忘掉。退出登录时调——连同 `PersistentBlobCache.clear()`：头像是别人
 * 的脸，不该在退出之后还留在这台机器上。
 */
export const closeSharedAvatarStore = (): void => {
  shared?.close();
  shared = null;
};

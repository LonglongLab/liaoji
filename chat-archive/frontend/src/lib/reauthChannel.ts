/**
 * 同一浏览器各标签页之间的日常解锁广播（PSU-2.213 §4.1）。
 *
 * 授权只认服务端：广播里不带任何状态，只说「我这边变了，你们各自去问一次」。
 * 收到的标签页重新拉 capabilities，按服务端答复决定去不去锁定页。所以伪造一条
 * 广播最多让别的标签页多问一次，不能借它解锁。
 *
 * 这个模块不碰 store，也不碰路由：store 只管喊一声，接收端在 `reauthSync.ts`。
 */

export type ReauthSignal = "locked" | "changed";

/** 只是 `BroadcastChannel` 用得到的那一小块，测试里换成内存替身。 */
export interface ReauthChannel {
  readonly post: (message: unknown) => void;
  readonly listen: (handler: (data: unknown) => void) => void;
  readonly close: () => void;
}

export const REAUTH_CHANNEL_NAME = "chat-archive-reauth-v1";
const MESSAGE_KIND = "chat-archive-reauth";

/** 浏览器自带的那一条；不支持 BroadcastChannel 时为 null，各标签页退回各自遇到 423 再处理。 */
export const browserReauthChannel = (): ReauthChannel | null => {
  if (typeof BroadcastChannel === "undefined") return null;
  const channel = new BroadcastChannel(REAUTH_CHANNEL_NAME);
  return {
    post: (message) => {
      channel.postMessage(message);
    },
    listen: (handler) => {
      channel.onmessage = (event: MessageEvent) => {
        const data: unknown = event.data;
        handler(data);
      };
    },
    close: () => {
      channel.close();
    },
  };
};

let shared: ReauthChannel | null = null;

/** `reauthSync` 装好通道后登记一次；测试里可以登记替身或清掉。 */
export const setReauthChannel = (channel: ReauthChannel | null): void => {
  shared = channel;
};

/** 告诉其他标签页：本页的解锁状态刚变过。BroadcastChannel 不会回送给发送方自己。 */
export const announceReauth = (signal: ReauthSignal): void => {
  try {
    shared?.post({ kind: MESSAGE_KIND, signal });
  } catch {
    // 广播只为让别的标签页早点刷新界面；发不出去不影响任何授权。
  }
};

/** 认出一条本模块发的广播；别的东西一律当没收到。 */
export const signalOf = (data: unknown): ReauthSignal | null => {
  if (typeof data !== "object" || data === null) return null;
  const { kind, signal } = data as { kind?: unknown; signal?: unknown };
  if (kind !== MESSAGE_KIND) return null;
  return signal === "locked" || signal === "changed" ? signal : null;
};

/**
 * 远程头像的 objectURL 管理层（PSU-2.47，PSU-2.122 加了持久层、并发闸门与重试）。
 *
 * 为什么要单独一层，而不是在组件里 `URL.createObjectURL(await load(id))`：
 *
 * - `AvatarBadge` 只吃一个字符串 `src`，而远端的头像是**字节**——取回来要经
 *   预签名 URL，与附件同一条路（`remoteAttachmentLoader`）。中间必须有人把
 *   `Promise<Blob>` 变成一个 `blob:` 字符串。
 * - 每个 objectURL 都是一份**必须显式回收的**引用。虚拟滚动下一行头像会随着
 *   滚动反复挂载卸载，按行创建就等于按滚动距离泄漏内存。
 * - 所以计数按 **blob_id** 而不是按组件：同一张图在列表行、气泡、成员名单里
 *   出现十次，字节只取一次、objectURL 只有一个，最后一个订阅者走了才回收。
 *
 * 引用归零**不立即回收**：虚拟滚动把同一行滚出去再滚回来是常事，立刻 revoke
 * 就等于每次回滚都重新下载一次。归零的进闲置队列，队列超长才按最久未用回收。
 *
 * `URL.createObjectURL` / `revokeObjectURL` 从构造参数进来：测试环境
 * （happy-dom）不保证有它们，而且「有没有真的 revoke 掉」正是这一层要钉住的
 * 行为——用真的实现就只能靠内存看不见的副作用去猜。
 *
 * ## PSU-2.122 加的三件事
 *
 * - **持久层**（`persistent`）：命中就不发网络请求。头像的字节不可变，缓存
 *   不需要失效策略，见 `avatarPersistentCache.ts`。
 * - **并发闸门**（`maxConcurrent`）：一页 100 条会话此前是一次打出 200 个请求
 *   （每张头像两次往返），手机上必然有几个超时。排队不会让总时间变长——带宽
 *   和服务端的处理能力都是固定的——但会让**先取的先出现**，而不是一起卡住。
 * - **失败会再试**（`retryDelaysMs` / `failureCooldownMs`）：此前失败一次就
 *   永久空白，而空白与「这个人没有头像」在界面上一模一样，于是"有时有有时
 *   没有"。现在退避重试两次，仍失败则进冷却期，冷却过后重新订阅会再试一次。
 */

import { nullBlobCache, type PersistentBlobCache } from "./avatarPersistentCache";

export interface AvatarObjectUrlStoreOptions {
  /** 取这个 blob 的字节。与附件同一条路，走预签名 URL 直取对象存储。 */
  load: (blobId: string) => Promise<Blob>;
  /** 闲置（无人订阅）的 objectURL 最多留几个。 */
  maxIdle?: number;
  /** 跨会话的字节缓存。缺省不持久化——那是纯内存的老行为。 */
  persistent?: PersistentBlobCache;
  /** 同时最多几个取回在飞。 */
  maxConcurrent?: number;
  /** 取回失败后的退避重试间隔；空数组 = 不重试。 */
  retryDelaysMs?: readonly number[];
  /** 重试都用完之后，这个 blob 多久之内不再试。 */
  failureCooldownMs?: number;
  createObjectUrl?: (blob: Blob) => string;
  revokeObjectUrl?: (url: string) => void;
  sleep?: (ms: number) => Promise<void>;
  now?: () => number;
}

type Listener = (url: string | null) => void;

interface Entry {
  url: string | null;
  refs: number;
  listeners: Set<Listener>;
  /**
   * 取回失败后的冷却截止时刻（毫秒时间戳）。0 = 没失败过。
   *
   * 此前这里是一个 `failed: boolean`，注释写着「失败不重试：重试也变不出这
   * 一端没有的字节」。那句话对**这一端真的没有**的情形是对的，对超时与限流
   * 是错的——而界面上这两种长得一模一样。所以改成有期限的。
   */
  retryAfter: number;
  started: boolean;
}

const DEFAULT_MAX_IDLE = 64;
const DEFAULT_MAX_CONCURRENT = 6;
const DEFAULT_RETRY_DELAYS = [400, 1600] as const;
const DEFAULT_FAILURE_COOLDOWN_MS = 30_000;

export class AvatarObjectUrlStore {
  private readonly entries = new Map<string, Entry>();
  /** 闲置队列，最久未用在前。同一个 id 只出现一次。 */
  private readonly idle: string[] = [];
  private readonly load: (blobId: string) => Promise<Blob>;
  private readonly maxIdle: number;
  private readonly persistent: PersistentBlobCache;
  private readonly maxConcurrent: number;
  private readonly retryDelays: readonly number[];
  private readonly failureCooldownMs: number;
  private readonly create: (blob: Blob) => string;
  private readonly revoke: (url: string) => void;
  private readonly sleep: (ms: number) => Promise<void>;
  private readonly now: () => number;
  /** 正在飞的取回数，与等着腾位置的那一队。 */
  private inFlight = 0;
  private readonly waiting: (() => void)[] = [];
  private closed = false;

  constructor(options: AvatarObjectUrlStoreOptions) {
    this.load = options.load;
    this.maxIdle = options.maxIdle ?? DEFAULT_MAX_IDLE;
    this.persistent = options.persistent ?? nullBlobCache;
    this.maxConcurrent = options.maxConcurrent ?? DEFAULT_MAX_CONCURRENT;
    this.retryDelays = options.retryDelaysMs ?? DEFAULT_RETRY_DELAYS;
    this.failureCooldownMs = options.failureCooldownMs ?? DEFAULT_FAILURE_COOLDOWN_MS;
    this.create = options.createObjectUrl ?? ((blob) => URL.createObjectURL(blob));
    this.revoke = options.revokeObjectUrl ?? ((url) => { URL.revokeObjectURL(url); });
    this.sleep =
      options.sleep ?? ((ms) => new Promise<void>((resolve) => setTimeout(resolve, ms)));
    this.now = options.now ?? (() => Date.now());
  }

  /**
   * 订阅一个 blob 的 objectURL。返回退订函数。
   *
   * 立刻同步回调一次当前值（可能是 null，表示「还没有」），之后就绪时再回调
   * 一次。**null 是「还没有」或「取不到」，不是「没有头像」**——没有头像的那
   * 条路根本不会走到这里，因为调用方拿不到 blob_id。
   */
  subscribe(blobId: string, notify: Listener): () => void {
    if (this.closed || blobId === "") {
      notify(null);
      return () => undefined;
    }
    const entry = this.entries.get(blobId) ?? {
      url: null,
      refs: 0,
      listeners: new Set<Listener>(),
      retryAfter: 0,
      started: false,
    };
    this.entries.set(blobId, entry);
    entry.refs += 1;
    entry.listeners.add(notify);
    const index = this.idle.indexOf(blobId);
    if (index !== -1) this.idle.splice(index, 1);
    const shouldLoad = !entry.started && entry.url === null && this.now() >= entry.retryAfter;
    if (shouldLoad) {
      entry.started = true;
    }
    notify(entry.url);
    if (shouldLoad) void this.fetch(blobId, entry);
    return () => { this.release(blobId, notify); };
  }

  /** 这个 id 现在的 objectURL，没有就是 null。给不订阅的读法用（测试、快照）。 */
  urlOf(blobId: string): string | null {
    return this.entries.get(blobId)?.url ?? null;
  }

  /** A final failed load lets a forward fall back to its original source URL. */
  failed(blobId: string): boolean {
    const entry = this.entries.get(blobId);
    return entry !== undefined && !entry.started && entry.url === null && entry.retryAfter > 0;
  }

  /** 还留着几个 objectURL —— 泄漏与否只能靠这个数字看出来。 */
  get liveCount(): number {
    let total = 0;
    for (const entry of this.entries.values()) if (entry.url !== null) total += 1;
    return total;
  }

  /**
   * 全部回收。
   *
   * PSU-2.122 之前换账号也调这个，理由写的是「上一个账号的 id 在这个账号下取
   * 不到东西」。那句话只对了一半：`blob_id` 是账号加盐的，所以**同一个 id 的
   * 字节永远是同一份**，留着不会拿错图，只是占内存——而内存有闲置队列兜底。
   * 于是换账号不再清，来回切两次账号不必重下两遍。真正要清的只剩退出登录
   * （见 `PersistentBlobCache.clear`）与整个 store 关掉。
   */
  clear(): void {
    for (const [, entry] of this.entries) {
      if (entry.url !== null) this.revoke(entry.url);
      entry.url = null;
      for (const listener of entry.listeners) listener(null);
    }
    this.entries.clear();
    this.idle.length = 0;
  }

  /** 之后的订阅一律直接答 null。整个应用退出这一端时调。 */
  close(): void {
    this.closed = true;
    this.clear();
  }

  /**
   * 取一个槽位再干活。
   *
   * 闸门在**网络那一段**，不在持久层读那一段：本地读没有并发代价，排它只会
   * 让本来立刻能出现的图白等。
   */
  private async withSlot<T>(task: () => Promise<T>): Promise<T> {
    if (this.inFlight >= this.maxConcurrent) {
      await new Promise<void>((resolve) => this.waiting.push(resolve));
    }
    this.inFlight += 1;
    try {
      return await task();
    } finally {
      this.inFlight -= 1;
      this.waiting.shift()?.();
    }
  }

  /** 取回期间这条记录还是不是原来那条（换账号、关掉都会让它不是）。 */
  private stale(blobId: string, entry: Entry): boolean {
    return this.closed || this.entries.get(blobId) !== entry;
  }

  private publish(blobId: string, entry: Entry, blob: Blob): void {
    if (this.stale(blobId, entry)) return;
    entry.url = this.create(blob);
    for (const listener of entry.listeners) listener(entry.url);
  }

  private async fetch(blobId: string, entry: Entry): Promise<void> {
    const cached = await this.persistent.get(blobId);
    if (this.stale(blobId, entry)) return;
    if (cached !== null) {
      this.publish(blobId, entry, cached);
      return;
    }
    const blob = await this.withSlot(() => this.download(blobId));
    if (this.stale(blobId, entry)) return;
    if (blob === null) {
      // 重试都用完了。进冷却期而不是永久失败：下一次有人订阅它（换页回来、
      // 重新滚到这一行）冷却已过的话就再试一次。
      entry.started = false;
      entry.retryAfter = this.now() + this.failureCooldownMs;
      for (const listener of entry.listeners) listener(null);
      return;
    }
    void this.persistent.put(blobId, blob);
    this.publish(blobId, entry, blob);
  }

  /** 退避重试。全都失败才答 null——调用方据此进冷却期。 */
  private async download(blobId: string): Promise<Blob | null> {
    for (let attempt = 0; ; attempt += 1) {
      try {
        return await this.load(blobId);
      } catch {
        if (attempt >= this.retryDelays.length || this.closed) return null;
        await this.sleep(this.retryDelays[attempt] ?? 0);
        // 睡这一觉期间 store 可能被关掉了，但这里不再判一次：拿回来的字节交给
        // `fetch()`，那边的 `stale()` 认得"已经关了"，而多判一次 TS 会把它
        // narrow 成恒假（关掉是别处改的）。
      }
    }
  }

  private release(blobId: string, notify: Listener): void {
    const entry = this.entries.get(blobId);
    if (entry === undefined) return;
    entry.listeners.delete(notify);
    entry.refs -= 1;
    if (entry.refs > 0) return;
    entry.refs = 0;
    // 引用归零：进闲置队列，不立刻回收——滚出去再滚回来是常事。
    if (!this.idle.includes(blobId)) this.idle.push(blobId);
    while (this.idle.length > this.maxIdle) {
      const evicted = this.idle.shift();
      if (evicted === undefined) break;
      const stale = this.entries.get(evicted);
      if (stale === undefined) continue;
      if (stale.url !== null) this.revoke(stale.url);
      this.entries.delete(evicted);
    }
  }
}

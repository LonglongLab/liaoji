/**
 * 头像字节的持久层（PSU-2.122）。
 *
 * 为什么头像**可以**长期缓存，而附件不行：`blob_id` 是账号加盐的内容标识，
 * 同一个 id 的字节永远是同一份——所以缓存不需要失效策略，只需要一个上限。
 *
 * 为什么必须自己存，而不是交给浏览器的 HTTP 缓存：远端的头像走预签名 URL
 * （`generate_presigned_url`），SigV4 的 `X-Amz-Date` 每次都不同，URL 每次
 * 都是新的——浏览器因此认为每次都是一张新图，一次都命中不了。本机端那条路
 * （`/api/v1/avatars/{id}` + `Cache-Control: private, max-age=86400`）没有这
 * 个问题，这一层就是远程端补上的那一份。
 *
 * 取舍写在这里：
 *
 * - **键是合成的同源路径**（`/__avatar/<blob_id>`），从来不会真的被请求。
 *   Cache Storage 只认 http(s) 的请求键，而真正的签名 URL 又每次不同，
 *   拿它当键等于从不命中。
 * - **淘汰按插入顺序**（`keys()` 规范上就是插入序，重复 `put` 会移到队尾），
 *   不额外存时间戳：多存一份元数据就多一份对不上的可能。
 * - **任何一步失败都退化成"没有缓存"**，不向上抛：配额满、隐私模式、存储被
 *   清空都属常态，头像少一张缓存不该让一个页面报错。
 */

/** Cache Storage 里用得到的那几件事。真的 `CacheStorage` 结构上满足它。 */
interface CacheLike {
  match: (request: string) => Promise<Response | undefined>;
  put: (request: string, response: Response) => Promise<void>;
  keys: () => Promise<readonly { readonly url: string }[]>;
  delete: (request: string) => Promise<boolean>;
}

interface CacheStorageLike {
  open: (name: string) => Promise<CacheLike>;
  delete: (name: string) => Promise<boolean>;
}

export interface PersistentBlobCache {
  /** 这个 blob 的字节，没存过就是 null。 */
  get: (blobId: string) => Promise<Blob | null>;
  /** 存下这个 blob 的字节。失败静默。 */
  put: (blobId: string, blob: Blob) => Promise<void>;
  /** 整份删掉。退出登录时调——头像是别人的脸，不该留在这台机器上。 */
  clear: () => Promise<void>;
}

const CACHE_NAME = "chat-archive-avatars-v1";
const KEY_PREFIX = "/__avatar/";
/** 最多存多少张。按实测中位 4.6 KB / p90 7.8 KB 算，2000 张 ≈ 10~16 MB。 */
const MAX_ENTRIES = 2000;
/** 每写这么多张扫一次超量，不是每次都扫：`keys()` 要把整份键列出来。 */
const SWEEP_INTERVAL = 100;

/** 什么都不存的那一份。没有 Cache Storage 时用它，调用方不必到处判空。 */
export const nullBlobCache: PersistentBlobCache = {
  get: () => Promise.resolve(null),
  put: () => Promise.resolve(),
  clear: () => Promise.resolve(),
};

const keyOf = (blobId: string): string => `${KEY_PREFIX}${encodeURIComponent(blobId)}`;

export const createPersistentBlobCache = (
  storage: CacheStorageLike,
  options: { maxEntries?: number; sweepEvery?: number } = {},
): PersistentBlobCache => {
  const maxEntries = options.maxEntries ?? MAX_ENTRIES;
  const sweepEvery = options.sweepEvery ?? SWEEP_INTERVAL;
  let writesSinceSweep = 0;
  let opening: Promise<CacheLike | null> | null = null;

  const cache = (): Promise<CacheLike | null> => {
    opening ??= storage.open(CACHE_NAME).catch(() => null);
    return opening;
  };

  const sweep = async (open: CacheLike): Promise<void> => {
    const keys = await open.keys();
    // `keys()` 是插入序，队首最旧。重复 `put` 会先删后加，所以再写一次的
    // 那张会自动挪到队尾——这就是"最久未写"的淘汰序，不用额外记时间。
    for (const request of keys.slice(0, Math.max(0, keys.length - maxEntries))) {
      await open.delete(request.url);
    }
  };

  return {
    async get(blobId) {
      try {
        const open = await cache();
        if (open === null) return null;
        const hit = await open.match(keyOf(blobId));
        return hit === undefined ? null : await hit.blob();
      } catch {
        return null;
      }
    },
    async put(blobId, blob) {
      try {
        const open = await cache();
        if (open === null) return;
        await open.put(
          keyOf(blobId),
          new Response(blob, {
            headers: { "content-type": blob.type === "" ? "application/octet-stream" : blob.type },
          }),
        );
        writesSinceSweep += 1;
        if (writesSinceSweep >= sweepEvery) {
          writesSinceSweep = 0;
          await sweep(open);
        }
      } catch {
        // 配额满、隐私模式、存储被清——都只是"这张没缓存成"。
      }
    },
    async clear() {
      try {
        opening = null;
        writesSinceSweep = 0;
        await storage.delete(CACHE_NAME);
      } catch {
        // 删不掉也不该让退出登录失败。
      }
    },
  };
};

const cacheStorage = (): CacheStorageLike | null => {
  // 非安全上下文（http 的局域网地址）里根本没有 `caches`；桌面端在
  // 127.0.0.1 上跑，那是安全上下文，有。
  const candidate = (globalThis as { caches?: CacheStorageLike }).caches;
  return candidate ?? null;
};

let shared: PersistentBlobCache | null = null;

/** 进程里唯一那一份。没有 Cache Storage 时返回 `nullBlobCache`。 */
export const persistentAvatarCache = (): PersistentBlobCache => {
  if (shared !== null) return shared;
  const storage = cacheStorage();
  shared = storage === null ? nullBlobCache : createPersistentBlobCache(storage);
  return shared;
};

/** 只给测试用：忘掉上面那份单例。 */
export const resetPersistentAvatarCacheForTests = (): void => {
  shared = null;
};

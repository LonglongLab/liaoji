/** Optional cancellation belongs to a reader, never to the attachment identity. */
export interface MediaRequestOptions { signal?: AbortSignal }
export type MediaLoader = (id: string, options?: MediaRequestOptions) => Promise<Blob>;

export const isMediaAbort = (error: unknown): boolean =>
  typeof error === "object" && error !== null && "name" in error && error.name === "AbortError";
export const isMediaCancellation = (error: unknown): boolean =>
  typeof error === "object" && error !== null && "name" in error &&
  (error.name === "AbortError" || error.name === "TimeoutError");
const cancelled = (): DOMException => new DOMException("Media reader left", "AbortError");
const abortReason = (signal?: AbortSignal): Error => signal?.reason instanceof Error ? signal.reason : cancelled();

/** Leave HTTP/1 connection headroom for messages. A slot includes the response body. */
export const createMediaRequestBudget = (
  { limit = 2, timeoutMs = 20_000, maxQueued = 128 }:
  { limit?: number; timeoutMs?: number; maxQueued?: number } = {},
) => {
  interface Pending { start: () => void; background: boolean }
  const queue: Pending[] = [];
  let active = 0;
  const pump = (): void => {
    while (active < limit && queue.length > 0) queue.shift()?.start();
  };
  const run = <T>(
    task: (signal: AbortSignal) => Promise<T>,
    { signal, background = true }: MediaRequestOptions & { background?: boolean } = {},
  ): Promise<T> => new Promise((resolve, reject) => {
    if (signal?.aborted || (active >= limit && queue.length >= maxQueued)) {
      reject(abortReason(signal)); return;
    }
    const controller = new AbortController();
    let started = false;
    const abort = (): void => {
      controller.abort(abortReason(signal));
      if (!started) {
        const index = queue.indexOf(pending);
        if (index >= 0) queue.splice(index, 1);
        signal?.removeEventListener("abort", abort);
        reject(abortReason(controller.signal));
      }
    };
    const pending: Pending = { background, start: () => {
      started = true; active++;
      const timer = background ? globalThis.setTimeout(() => {
        controller.abort(new DOMException("Media preview timed out", "TimeoutError"));
      }, timeoutMs) : undefined;
      // Real fetch aborts body consumption too. Do not release a slot early
      // when an injected transport ignores cancellation and is still running.
      void Promise.resolve().then(() => {
        controller.signal.throwIfAborted();
        return task(controller.signal);
      }).then(resolve, reject).finally(() => {
        globalThis.clearTimeout(timer);
        signal?.removeEventListener("abort", abort);
        active--; pump();
      });
    } };
    signal?.addEventListener("abort", abort, { once: true });
    const beforeBackground = background ? -1 : queue.findIndex(item => item.background);
    if (beforeBackground < 0) queue.push(pending); else queue.splice(beforeBackground, 0, pending);
    pump();
  });
  return { run };
};

export const mediaRequestBudget = createMediaRequestBudget();

interface CacheEntry {
  controller: AbortController;
  promise: Promise<Blob>;
  readers: Set<symbol>;
  retained: boolean;
  settled: boolean;
}

/** View/account-scoped cache; only the final departing subscriber aborts fetch. */
export class MediaBlobCache {
  private readonly entries = new Map<string, CacheEntry>();
  private readonly pending = new Set<CacheEntry>();

  load(key: string, task: (signal: AbortSignal) => Promise<Blob>, { signal }: MediaRequestOptions = {}): Promise<Blob> {
    if (signal?.aborted) return Promise.reject(abortReason(signal));
    let entry = this.entries.get(key);
    if (entry === undefined) {
      const controller = new AbortController();
      const created: CacheEntry = {
        controller, readers: new Set(), retained: false, settled: false,
        promise: Promise.resolve().then(() => {
          controller.signal.throwIfAborted();
          return task(controller.signal);
        }),
      };
      created.promise = created.promise.then(blob => {
        created.settled = true;
        this.pending.delete(created);
        return blob;
      }, (error: unknown) => {
        created.settled = true;
        this.pending.delete(created);
        if (this.entries.get(key) === created) this.entries.delete(key);
        throw error;
      });
      this.entries.set(key, created); this.pending.add(created); entry = created;
    }
    const shared = entry;
    if (signal === undefined) {
      shared.retained = true;
      return shared.promise;
    }
    const reader = Symbol(); shared.readers.add(reader);
    return new Promise((resolve, reject) => {
      const cleanup = (): void => {
        signal.removeEventListener("abort", abort); shared.readers.delete(reader);
      };
      const abort = (): void => {
        cleanup(); reject(abortReason(signal));
        if (!shared.settled && !shared.retained && shared.readers.size === 0) {
          if (this.entries.get(key) === shared) this.entries.delete(key);
          shared.controller.abort(cancelled());
        }
      };
      signal.addEventListener("abort", abort, { once: true });
      void shared.promise.finally(cleanup).then(resolve, reject);
    });
  }

  delete(key: string): void { this.entries.delete(key); }
  clear(): void {
    for (const entry of this.pending) entry.controller.abort(cancelled());
    this.pending.clear();
    this.entries.clear();
  }
}

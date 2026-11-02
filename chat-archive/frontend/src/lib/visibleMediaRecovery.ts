import type { AttachmentSummary } from "@/api/archiveApi";
import { mediaRequestBudget, type MediaRequestOptions } from "./mediaRequests";

type Resolver = (id: string, options?: MediaRequestOptions) => Promise<AttachmentSummary>;
interface Request {
  id: string;
  interested: (() => boolean)[];
  finish: (value: AttachmentSummary | null) => void;
  fail: (error: unknown) => void;
  promise: Promise<AttachmentSummary | null>;
  controller: AbortController;
  cleanups: (() => void)[];
  force: boolean;
}
interface State {
  active: number;
  queue: Request[];
  pending: Map<string, Request>;
  attempted: Map<string, number>;
}
// The resolver is bound to one account by the owning view. No persisted IDs,
// timers or network polling; only visible rows can enter the bounded queue.
const states = new WeakMap<Resolver, State>();
export const recoverVisibleMedia = (
  resolve: Resolver, id: string, interested: () => boolean, force = false,
  onCooldown?: (remainingMs: number) => void,
  options: MediaRequestOptions = {},
): Promise<AttachmentSummary | null> => {
  if (options.signal?.aborted) return Promise.resolve(null);
  let state = states.get(resolve);
  if (state === undefined) {
    state = { active: 0, queue: [], pending: new Map(), attempted: new Map() };
    states.set(resolve, state);
  }
  const existing = state.pending.get(id);
  if (existing !== undefined && !existing.controller.signal.aborted) {
    trackInterest(existing, state, interested, options.signal);
    return existing.promise;
  }
  const last = state.attempted.get(id);
  if (!force && last !== undefined && Date.now() - last < 5_000) {
    onCooldown?.(5_000 - (Date.now() - last));
    return Promise.resolve(null);
  }
  if (state.queue.length >= 128) {
    return Promise.resolve(null);
  }
  const current = state;
  const pump = (): void => {
    while (current.active < 2 && current.queue.length > 0) {
      const request = current.queue.shift();
      if (request === undefined) break;
      if (!request.interested.some(check => check())) {
        if (current.pending.get(request.id) === request) current.pending.delete(request.id);
        request.cleanups.forEach(cleanup => cleanup());
        request.finish(null);
        continue;
      }
      current.active++;
      current.attempted.delete(request.id);
      current.attempted.set(request.id, Date.now());
      while (current.attempted.size > 512) {
        const oldest = current.attempted.keys().next();
        if (oldest.done) break;
        current.attempted.delete(oldest.value);
      }
      const release = (): void => {
        if (current.pending.get(request.id) === request) current.pending.delete(request.id);
        request.cleanups.forEach(cleanup => cleanup());
        current.active--;
        pump();
      };
      void mediaRequestBudget.run(signal => resolve(request.id, { signal }), {
        signal: request.controller.signal, background: !request.force,
      }).then(
        value => { release(); request.finish(value); },
        (error: unknown) => { release(); request.fail(error); },
      );
    }
  };
  let finish!: Request["finish"];
  let fail!: Request["fail"];
  const promise = new Promise<AttachmentSummary | null>((accept, reject) => { finish = accept; fail = reject; });
  const request: Request = { id, interested: [], finish, fail, promise,
    controller: new AbortController(), cleanups: [], force };
  trackInterest(request, current, interested, options.signal);
  current.pending.set(id, request);
  current.queue.push(request);
  void Promise.resolve().then(pump);
  return promise;
};

const trackInterest = (request: Request, state: State, interested: () => boolean, signal?: AbortSignal): void => {
  request.interested.push(() => signal?.aborted !== true && interested());
  if (signal === undefined) return;
  const abort = (): void => {
    if (request.interested.some(check => check())) return;
    request.controller.abort();
    const queued = state.queue.indexOf(request);
    if (queued >= 0) {
      state.queue.splice(queued, 1);
      if (state.pending.get(request.id) === request) state.pending.delete(request.id);
      request.cleanups.forEach(cleanup => cleanup()); request.finish(null);
    }
  };
  signal.addEventListener("abort", abort, { once: true });
  request.cleanups.push(() => signal.removeEventListener("abort", abort));
};

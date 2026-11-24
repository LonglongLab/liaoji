import { reactive } from "vue";

export const REMOTE_ORIGINAL_PRESENCE_LIMIT = 128;

export interface RemoteOriginalPresence {
  originalAvailable: (blobId: string) => boolean | undefined;
  recordOriginalPresence: (blobId: string, available: boolean | null) => void;
}

/**
 * 原图在不在（PSU-2.43）。答案随缩略图那次请求一起到，所以这张表是逐渐填满的。
 * 只存明确的 true/false；上限 128 条按先入先出淘汰，避免长列表滚动时无界增长。
 */
export const useRemoteOriginalPresence = (
  limit = REMOTE_ORIGINAL_PRESENCE_LIMIT,
): RemoteOriginalPresence => {
  const originalPresence = reactive<Record<string, boolean>>({});
  const insertionOrder: string[] = [];

  const originalAvailable = (blobId: string): boolean | undefined => originalPresence[blobId];

  const recordOriginalPresence = (blobId: string, available: boolean | null): void => {
    if (available === null) return;
    if (originalPresence[blobId] === undefined) {
      while (insertionOrder.length >= limit) {
        const oldest = insertionOrder.shift();
        if (oldest === undefined) break;
        Reflect.deleteProperty(originalPresence, oldest);
      }
      insertionOrder.push(blobId);
    }
    originalPresence[blobId] = available;
  };

  return { originalAvailable, recordOriginalPresence };
};

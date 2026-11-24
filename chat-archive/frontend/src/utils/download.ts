/** 浏览器下载锚点辅助函数（唯一一份，放在 .ts 中供各组件/模块复用）。 */
export interface UrlDownloadOptions {
  rel?: string;
  appendToBody?: boolean;
}

export interface BlobDownloadOptions extends UrlDownloadOptions {
  /** 默认 0（下一轮事件循环立即撤销），长包下载可设更长超时。 */
  revokeAfterMs?: number;
}

export const triggerUrlDownload = (
  url: string,
  fileName: string,
  options: UrlDownloadOptions = {},
): void => {
  const anchor = globalThis.document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  if (options.rel !== undefined) {
    anchor.rel = options.rel;
  }
  if (options.appendToBody === true) {
    globalThis.document.body.append(anchor);
  }
  anchor.click();
  if (options.appendToBody === true) {
    anchor.remove();
  }
};

export const downloadBlob = (
  blob: Blob,
  fileName: string,
  options: BlobDownloadOptions = {},
): (() => void) => {
  const url = URL.createObjectURL(blob);
  triggerUrlDownload(url, fileName, options);
  let revoked = false;
  let timer: ReturnType<typeof globalThis.setTimeout> | undefined;
  const revoke = (): void => {
    if (revoked) return;
    revoked = true;
    if (timer !== undefined) {
      globalThis.clearTimeout(timer);
      timer = undefined;
    }
    URL.revokeObjectURL(url);
  };
  const delay = options.revokeAfterMs ?? 0;
  if (delay < 0) {
    revoke();
  } else {
    timer = globalThis.setTimeout(revoke, delay);
  }
  return revoke;
};

/**
 * 一份字节的 SHA-256，小写十六进制（PSU-2.184）。
 *
 * 分享时把「要带上的那份字节」的摘要告诉远端：远端归档里有同样内容的，直接在对象存储里
 * 复制，这边就不必再传。放在 .ts 里：`.vue` 上没有配浏览器全局（见 `utils/clipboard.ts`）。
 * `crypto.subtle` 只在安全上下文里有——本机是 127.0.0.1、远程网页是 https，都算。
 */
export const sha256Hex = async (blob: Blob): Promise<string> => {
  const digest = await globalThis.crypto.subtle.digest("SHA-256", await blob.arrayBuffer());
  return Array.from(new Uint8Array(digest), (byte) => byte.toString(16).padStart(2, "0")).join("");
};

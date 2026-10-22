/**
 * 把已经取到本机的附件字节存成文件。走一个临时的 blob URL，
 * 点完就撤销——不留住内容，也不需要服务端再参与一次。
 */
export const downloadBlob = (blob: Blob, fileName: string): void => {
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = "noopener";
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  // 立刻撤销会让部分浏览器来不及开始下载，等这一轮事件循环结束。
  globalThis.setTimeout(() => { URL.revokeObjectURL(url); }, 0);
};

import { downloadBlob as triggerBlobDownload } from "@/utils/download";

/**
 * 把已经取到本机的附件字节存成文件。走一个临时的 blob URL，
 * 点完就撤销——不留住内容，也不需要服务端再参与一次。
 */
export const downloadBlob = (blob: Blob, fileName: string): void => {
  triggerBlobDownload(blob, fileName, { rel: "noopener", appendToBody: true });
};

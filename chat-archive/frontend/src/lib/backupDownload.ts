/**
 * 把备份的各段拉到本机（PSU-2.141）。
 *
 * 浏览器 API 住在 .ts 里而不是 .vue 里：ESLint 对 .vue 开了 no-undef 且没有配
 * browser globals，组件里直接碰 `document` 会红。
 *
 * **它给的是 3-2-1 的第三份，但那一步是手动的。** 这里不做自动化、不做断点续传、
 * 也不校验摘要——摘要在清单里，核不核由拿到文件的人决定。把它做得像"已经有异地
 * 第二份了"是撒谎：真正自动的那一版还不存在。
 */
import type { BackupDownload } from "@/api/archiveApi";

/** 一段备份在本机存成什么名字。`<备份号>-<原库名>.gz`，排序即时间序。 */
export const backupPartFileName = (backupId: string, partName: string): string =>
  `${backupId}-${partName}.gz`;

/**
 * 逐段触发下载。
 *
 * 一次点击开多个下载会被浏览器拦成"多文件下载"提示——这是浏览器的既定行为，
 * 用户点一次"允许"即可；分开做成多个按钮只会让人点五次。
 */
export const downloadBackupParts = (
  download: BackupDownload,
  click: (url: string, fileName: string) => void = defaultClick,
): number => {
  for (const part of download.parts) {
    click(part.url, backupPartFileName(download.backup_id, part.name));
  }
  return download.parts.length;
};

const defaultClick = (url: string, fileName: string): void => {
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = fileName;
  anchor.rel = "noreferrer";
  anchor.click();
};

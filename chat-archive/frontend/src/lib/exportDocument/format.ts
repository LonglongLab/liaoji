/**
 * 导出成什么（PSU-2.169）。
 *
 * 住在模块里而不是组件里：`<script setup>` 不能 `export`，把类型写在那里，
 * 调用方 import 到的是 `any`，于是所有用到它的地方静默失去类型
 * （ESLint 的 `no-unsafe-assignment` 会把这一片全点亮，那是唯一的提示）。
 */
/**
 * `bundle` = 网页（压缩包）：网页 + `附件/` 目录（PSU-2.179）。它取代了 2.168 的
 * 单文件 HTML——单文件放不下语音、视频、文件，留着就是一个打开才发现缺东西的选项。
 */
export type ExportFormat = "bundle" | "image";

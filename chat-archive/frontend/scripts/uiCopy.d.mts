/** `scripts/uiCopy.mjs` 的类型声明。实现是 JS，因为它要被 tsconfig 的两档同时引用。 */

/** 递归列出目录下所有 `.vue`。 */
export function vueFiles(dir: string): string[];

/** 走一遍字符：标签里的东西与注释一律不算文案。 */
export function visibleText(template: string): string[];

/** 中文等效宽度：全角算 1，半角算 0.5。 */
export function cjkWidth(text: string): number;

/** 一份 `.vue` 里超过 `limit` 个中文等效字符的句子。 */
export function longSentences(file: string, limit: number): { text: string; width: number }[];

/** 录屏文本放行白名单：模板里写死的那些文本，已 trim、去重、按字典序排好。 */
export function collectUiCopy(dir: string): string[];

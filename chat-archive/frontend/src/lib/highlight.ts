/**
 * 关键词高亮的唯一一份实现。
 *
 * 在 PSU-2.66 之前这段算法有两份几乎逐行相同的副本（`SearchHitCard` 的
 * `highlightedSummary` 与 `SearchFileRow` 的 `nameParts`），全局搜索页要用时
 * 差点成为第三份。口径沿用那两份原有的行为，一个字没改：
 *
 * - 空关键词 = 整段一片、不高亮；
 * - 折叠大小写按 `toLocaleLowerCase("zh-CN")`，因此英文大小写不敏感；
 * - 同一段里的多处命中全部标出，不是只标第一处。
 *
 * **按原文长度切片**（`needle.length`）而不是按折叠后的长度：折叠只用来定位，
 * 显示的永远是原文那几个字符。
 */
export interface HighlightPart {
  text: string;
  matched: boolean;
}

export const highlightParts = (
  text: string,
  query: string | null | undefined,
): HighlightPart[] => {
  const whole: HighlightPart[] = [{ text, matched: false }];
  const needle = (query ?? "").trim();
  if (needle === "") return whole;

  const haystack = text.toLocaleLowerCase("zh-CN");
  const target = needle.toLocaleLowerCase("zh-CN");
  const parts: HighlightPart[] = [];
  let cursor = 0;
  while (cursor < text.length) {
    const matchAt = haystack.indexOf(target, cursor);
    if (matchAt < 0) {
      parts.push({ text: text.slice(cursor), matched: false });
      break;
    }
    if (matchAt > cursor) parts.push({ text: text.slice(cursor, matchAt), matched: false });
    const end = matchAt + needle.length;
    parts.push({ text: text.slice(matchAt, end), matched: true });
    cursor = end;
  }
  return parts.length === 0 ? whole : parts;
};

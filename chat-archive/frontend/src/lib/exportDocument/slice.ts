/**
 * 把一份很长的导出文档切成几张图（PSU-2.169）。
 *
 * 为什么必须切：浏览器画布单边上限 16384 像素，按每条约 96px 估，150～300 条就
 * 到顶。超了**不是报错，是得到一张空白图**——正是「少做是静默的」那一类失败，
 * 所以要主动挡。用户在决策页上选的是「超长自动切成多张，不设条数上限」。
 *
 * 纯函数，不碰 DOM：切法是这件事里唯一需要仔细的逻辑，拎出来才测得动。
 */

/** 一行在文档里的位置。`top` 要用元素自己的位置，不是前面高度之和——行间有间距。 */
export interface SliceRow {
  top: number;
  height: number;
}

export interface Slice {
  start: number;
  end: number;
}

export const planSlices = (
  rows: readonly SliceRow[],
  maxHeight: number,
): Slice[] => {
  if (rows.length === 0) return [];
  const bottom = (row: SliceRow): number => row.top + row.height;
  const total = Math.max(...rows.map(bottom));
  // 上限不成话时退回「不切」：宁可给一张超大的，也不能在这里空转。
  if (!Number.isFinite(maxHeight) || maxHeight <= 0) return [{ start: 0, end: total }];

  const slices: Slice[] = [];
  let start = 0;
  // 眼下这一片里有没有已经装进去的消息。没有的话不能切：切出来的是一张
  // 只有页首留白、一条消息都没有的图（第一条自己就超高、而它上面有点留白时就是这样）。
  let holding = false;
  for (const row of rows) {
    if (holding && bottom(row) - start > maxHeight && row.top > start) {
      // 这一行装不下了，断在它的 `top`——也就是上一行下边界再往下、间距之后的位置。
      // 中间那点间距因此跟着**上一片**走：图的底部多一点留白，好过下一张顶上缺一截。
      slices.push({ start, end: row.top });
      start = row.top;
    }
    holding = true;
  }
  // **切点只取自某一行的 `top`**，所以不会把一条消息拦腰截断。代价是：单独一条
  // 比上限还高时，它独占的那一片**也比上限高**——这里不切它。那一片交给
  // `html-to-image` 时会超出画布上限，它会把整张图等比缩小到画得下为止
  // （字会变小，但不会是空白图）。要真正不超限，只能在一条消息内部按像素切，
  // 那会把一行字切成两半，暂不做。
  if (start < total) slices.push({ start, end: total });
  return slices;
};

/** 每张图页脚上的片号；只有一片时不标——那只会让人找另外两张。 */
export const sliceLabel = (index: number, count: number): string | null =>
  count <= 1 ? null : `第 ${String(index + 1)} / ${String(count)} 张`;

import { isCoarsePointer, isNarrowScreen } from "@/lib/gestures";

/**
 * 这台浏览器自己能不能把 PDF 在 `<iframe>` 里打开。
 *
 * 能就交给它——内置阅读器有选字、搜索、打印，我们自己画的一定不如它。
 * 不能就得本机渲染，否则用户看到的是一片空白。
 *
 * 判据有两层，缺一不可：
 *
 * 1. `navigator.pdfViewerEnabled` 是标准属性。**Android Chrome 上是 `false`**——
 *    那儿根本没有内置阅读器，iframe 里的 PDF 要么空白要么变成一次下载。
 * 2. iOS 的 WebKit 报 `true`，可它在 iframe 里只画第一页、翻不动。信那个标志
 *    会让人以为这份 PDF 只有一页，比空白更糟。所以**手指操作的那一档一律不走
 *    内置阅读器**，交给本机渲染。
 *
 * 判错的方向是安全的：多走一次本机渲染，而本机渲染在哪儿都能跑。
 */
export const nativePdfViewerUsable = (): boolean => {
  if (isCoarsePointer() || isNarrowScreen()) return false;
  // 类型里它是必有的 boolean，那是标准的说法，不是这台机器的事实：老浏览器上
  // 根本没有这个属性。按「问不出来就是没有」处理，所以这里要的是可选的那个形状。
  const { pdfViewerEnabled } = globalThis.navigator as { pdfViewerEnabled?: boolean };
  return pdfViewerEnabled === true;
};

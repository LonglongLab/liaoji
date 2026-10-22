/**
 * 手势层要问环境的那几个问题。放在 .ts 里是因为 .vue 上开着 no-undef，
 * 浏览器全局只能在模块里碰。
 */

/** 和 main.css 里全屏化预览的断点保持同一个数，改一处要改两处的东西不留。 */
const NARROW_QUERY = "(max-width: 767px)";
const COARSE_QUERY = "(pointer: coarse)";

const mediaMatches = (query: string): boolean => {
  // happy-dom 与老浏览器对 pointer 之类的媒体特性未必认，问不出来就当没有。
  try {
    return globalThis.matchMedia(query).matches;
  } catch {
    return false;
  }
};

/** 手机那一档版式：全屏预览、缩略图横排、打开就按宽度适配。 */
export const isNarrowScreen = (): boolean => mediaMatches(NARROW_QUERY);

/** 主要输入是手指而不是鼠标——提示文案要说的手势不一样。 */
export const isCoarsePointer = (): boolean => mediaMatches(COARSE_QUERY);

const INTERACTIVE = "button, a[href], input, select, textarea, [role='button']";

/**
 * 落在控件上的那一下归控件。手势层（双拍缩放、边缘拦截）都要先问这一句：
 * 双拍翻页按钮不该顺手把画面放大，而 `touchstart` 上一旦 preventDefault，
 * 浏览器就不再补发 click——手机上关闭按钮正好贴着屏幕右边缘。
 */
export const isInteractiveTarget = (target: EventTarget | null): boolean =>
  target instanceof Element && target.closest(INTERACTIVE) !== null;

/** 手机上的「双击」是原地两拍，浏览器不一定补发 dblclick，只能自己数。 */
const DOUBLE_TAP_MS = 320;

/**
 * 每拍一下问一次「这是第二拍吗」。判定成立后立刻清零，
 * 连拍三下只算一次，不会第三下又翻回去。
 */
export const createDoubleTapDetector = (): (() => boolean) => {
  let lastAt = Number.NEGATIVE_INFINITY;
  return (): boolean => {
    const at = globalThis.performance.now();
    if (at - lastAt < DOUBLE_TAP_MS) {
      lastAt = Number.NEGATIVE_INFINITY;
      return true;
    }
    lastAt = at;
    return false;
  };
};

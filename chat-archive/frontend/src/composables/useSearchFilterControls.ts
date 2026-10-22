import { computed, ref, watch, type ComputedRef, type Ref } from "vue";

import type { HistoryLayer } from "@/composables/useHistoryLayer";
import {
  clearDimension,
  filterChips,
  FILTER_DIMENSIONS,
  removeFilterValue,
  toggleFilterValue,
  type FilterDimension,
  type FilterNames,
  type SearchFilters,
} from "@/lib/searchFilters";
import type { SearchCapabilities } from "@/lib/searchScopes";

/**
 * 搜索页的筛选胶水：chip 的增删、标题的归纳、H5 抽屉的开合与停段。
 *
 * 本机与远端两个搜索页**挂同一份**（PSU-2.66 那条「挂同一套壳」的延续）：
 * 这几段在两个视图里曾经逐字相同，各留一份迟早各自演化。端的差异只经
 * `capabilities`（上限等能力）与可选的 `onDimensionShown` 传进来，
 * 这里没有「这是哪一端」的判断。
 */
export const useSearchFilterControls = (options: {
  /** 视图持有的那一份筛选条件；抽屉的 `@apply`、账号切换时的清空仍由视图直写。 */
  filters: Ref<SearchFilters>;
  capabilities: SearchCapabilities;
  /** 会话/发送人标识 → 名字。认不出的 chip 照实印标识，不编名字。 */
  names: ComputedRef<FilterNames>;
  /**
   * 抽屉翻到某一段时（包括打开时的落段）通知调用方。本机页用它把桌面名录
   * 拨到同一维——抽屉与名录看的是**同一份**「哪一维」，两处各记一份正是
   * 两个状态慢慢分家的开始。远端页没有名录切换，不传即可。
   */
  onDimensionShown?: (dimension: FilterDimension) => void;
  /**
   * 抽屉那一格浮层（`?filters=<维>`，PSU-2.120）。视图给的是同一份
   * `useHistoryLayer`——开一层压一条历史，手机上的系统「返回」才只关抽屉，
   * 而不是把整个搜索页弹掉（条件、滚动位置一起没）。
   *
   * 可选：不给就只剩内存状态，开合照旧，只是没有那条历史可退。
   */
  sheetLayer?: HistoryLayer;
}) => {
  const { filters, capabilities, names } = options;

  const toggleFilter = (dimension: FilterDimension, value: string): void => {
    filters.value = toggleFilterValue(
      filters.value,
      dimension,
      value,
      capabilities.maxSelections,
    );
  };
  const removeFilter = (dimension: FilterDimension, value: string | null): void => {
    filters.value =
      value === null
        ? clearDimension(filters.value, dimension)
        : removeFilterValue(filters.value, dimension, value);
  };

  /**
   * 标题就是当前筛的是什么。只选了一个值时就是那个值的名字（和微信一样）；
   * 选了几个就报个数——具体是哪几个，筛选条上一枚一枚写着。
   */
  const title = computed(() => {
    const chips = filterChips(filters.value, names.value);
    if (chips.length === 0) return "全部";
    if (chips.length === 1) return chips[0]?.label ?? "全部";
    return `已选 ${String(chips.length)} 项`;
  });

  /**
   * H5 那个筛选抽屉。停在哪一段就是这一格的值：翻段只是换内容，不该各压一条
   * 历史（`set` 而不是 `open`）。
   *
   * 层自己开、同时告诉那一格压一条历史，路由不再点名它时就收掉——与灯箱、
   * 文件预览同一套约定（PSU-2.120）。
   */
  const sheetLayer = options.sheetLayer;
  const asDimension = (value: string | null): FilterDimension | null =>
    FILTER_DIMENSIONS.find((dimension) => dimension === value) ?? null;
  const initialSegment = asDimension(sheetLayer?.value ?? null);
  const sheetOpen = ref(initialSegment !== null);
  const sheetDimension = ref<FilterDimension>(initialSegment ?? "types");
  const sheetSegment = computed<FilterDimension>({
    get: () => sheetDimension.value,
    set: (value) => {
      sheetDimension.value = value;
      sheetLayer?.set(value);
      options.onDimensionShown?.(value);
    },
  });
  const openSheet = (dimension: FilterDimension | null): void => {
    // 从 chip 点开时抽屉要重新落到那一维，且调用方要收到通知。
    //
    // 不走 `sheetSegment` 那个 setter：它会 `replace` 一次，紧接着这里再 `push`
    // 一次，两次导航抢同一格——后落地的那次（带着旧的「类型」）会把刚选中的
    // 那一维顶掉。开一层只该有一次导航，段就写在那一次里。
    const segment = dimension ?? "types";
    sheetDimension.value = segment;
    options.onDimensionShown?.(segment);
    sheetOpen.value = true;
    sheetLayer?.open(segment);
  };
  const closeSheet = (): void => {
    sheetOpen.value = false;
    sheetLayer?.close();
  };
  /**
   * 系统「返回」走的就是这一步：那一格没了，抽屉跟着收。
   *
   * **只管收、不管开**：开合与落段的发起方是这里，地址栏是它的影子。反过来让
   * 地址栏去改段，一次慢半拍的导航就能把刚选的那一段顶回旧值（前进键因此不会
   * 把抽屉再翻出来——那一层本来也不值得还原）。
   */
  watch(
    () => [sheetLayer?.value ?? null, sheetLayer?.pending ?? false] as const,
    ([value, pending]) => {
      if (!pending && value === null) sheetOpen.value = false;
    },
  );

  return { toggleFilter, removeFilter, title, sheetOpen, sheetSegment, openSheet, closeSheet };
};

import type { FileClass, MessageType } from "@/api/archiveApi";

import {
  FILE_CLASSES,
  TYPE_FILTERS,
  fileClassLabel,
  typeFilterLabel,
  typeFilterMessageTypes,
  type TypeFilterKey,
} from "@/lib/searchScopes";

/**
 * 全局搜索的筛选条件（PSU-2.75）。
 *
 * 取代原来那个单值 `scope`：从「一次只能选一个维度的一个值」变成
 * **维度之间 AND、维度之内 OR**——两个群里的图片，是一次能问出来的问题。
 * 后端那一半在 PSU-2.74 就位（按值扇出再归并），这里是它的界面形状。
 *
 * 类型这一维存的是**界面上的那一项**（`TypeFilterKey`），不是消息类型本身：
 * 「图片与视频」在界面上是一项，在契约里是两个类型。发查询时才展开。
 */
export interface SearchFilters {
  readonly types: readonly TypeFilterKey[];
  readonly fileClasses: readonly FileClass[];
  readonly conversationIds: readonly string[];
  readonly senderIds: readonly string[];
}

export const EMPTY_FILTERS: SearchFilters = {
  types: [],
  fileClasses: [],
  conversationIds: [],
  senderIds: [],
};

export type FilterDimension = keyof SearchFilters;

/** 界面上的次序，也是筛选条上 chip 的次序。 */
export const FILTER_DIMENSIONS: readonly FilterDimension[] = [
  "types",
  "fileClasses",
  "conversationIds",
  "senderIds",
];

export const DIMENSION_LABELS: Record<FilterDimension, string> = {
  types: "类型",
  fileClasses: "文件类型",
  conversationIds: "会话",
  senderIds: "发送人",
};

export const filterCount = (filters: SearchFilters): number =>
  filters.types.length +
  filters.fileClasses.length +
  filters.conversationIds.length +
  filters.senderIds.length;

export const filtersAreEmpty = (filters: SearchFilters): boolean =>
  filterCount(filters) === 0;

export const filterValues = (
  filters: SearchFilters,
  dimension: FilterDimension,
): readonly string[] => filters[dimension];

/**
 * 文件类别这一维只在类型**恰好是「文件」一项**时成立——这是后端契约的镜像：
 * `file_class` 只在 message_types 恰好为 {file} 时被接受，摘掉「文件」或再叠
 * 任何一个类型都是当场 QUERY_INVALID，整个结果区变成一条错误横幅。
 */
export const fileClassesAllowed = (types: readonly TypeFilterKey[]): boolean =>
  types.length === 1 && types[0] === "file";

const withDimension = (
  filters: SearchFilters,
  dimension: FilterDimension,
  values: readonly string[],
): SearchFilters => {
  const next: SearchFilters = { ...filters, [dimension]: values };
  // 类型不再恰好是「文件」时（摘掉了它，或又叠了别的类型），类别跟着清掉：
  // 留着它发出去是 `type=file&type=text&file_class=doc`，后端当场 QUERY_INVALID。
  return fileClassesAllowed(next.types) ? next : { ...next, fileClasses: [] };
};

/**
 * 点一下某个值：没选就加上，选了就摘掉。
 *
 * 超过上限时**原样返回**——调用方据此说一句为什么，而不是悄悄少加一个。
 * 上限是端的能力（本机 20 / 远端 5），差异只经能力对象传进来。
 */
export const toggleFilterValue = (
  filters: SearchFilters,
  dimension: FilterDimension,
  value: string,
  maxSelections: number,
): SearchFilters => {
  // 各维度的元素类型不同（类型键 / 文件类别 / 标识串），在这一层一律当字符串
  // 看：不这么写，`includes` 会把参数收窄成几个维度共有的那两个字面量。
  const current: readonly string[] = filters[dimension];
  if (current.includes(value)) {
    return withDimension(
      filters,
      dimension,
      current.filter((item) => item !== value),
    );
  }
  // 上限只卡名录那两维：远端的多选是把会话／发送人按值扇出成 N 次跨境请求
  // 换来的（PSU-2.74），上限管的是那份扇出预算；类型与文件类别走内联参数、
  // 选项本来就比后端任何上限少，卡它们只会把第 6 个类型点成没反馈的哑按钮。
  const fansOut = dimension === "conversationIds" || dimension === "senderIds";
  if (fansOut && current.length >= maxSelections) return filters;
  return withDimension(filters, dimension, [...current, value]);
};

export const removeFilterValue = (
  filters: SearchFilters,
  dimension: FilterDimension,
  value: string,
): SearchFilters => {
  const current: readonly string[] = filters[dimension];
  return withDimension(
    filters,
    dimension,
    current.filter((item) => item !== value),
  );
};

export const clearDimension = (
  filters: SearchFilters,
  dimension: FilterDimension,
): SearchFilters => withDimension(filters, dimension, []);

/** 发给后端的消息类型：界面上的每一项展开成它涵盖的类型；一个都没选就是不筛。 */
export const filterMessageTypes = (filters: SearchFilters): MessageType[] | null => {
  if (filters.types.length === 0) return null;
  const types = new Set<MessageType>();
  for (const key of filters.types) {
    for (const type of typeFilterMessageTypes(key)) types.add(type);
  }
  return [...types];
};

/**
 * 筛选条上的一枚 chip。
 *
 * 一个维度选到三个以上就并成一枚「3 个会话」：二十个名字排成一行，读者既看不完
 * 也点不准，而摘掉其中某一个的入口在抽屉里本来就有。
 */
export interface FilterChip {
  readonly dimension: FilterDimension;
  /** `null` = 这枚 chip 代表整个维度，摘掉就是清空它。 */
  readonly value: string | null;
  readonly label: string;
}

const SUMMARY_THRESHOLD = 3;

export interface FilterNames {
  readonly conversations: ReadonlyMap<string, string>;
  readonly senders: ReadonlyMap<string, string>;
}

const valueLabel = (
  dimension: FilterDimension,
  value: string,
  names: FilterNames,
): string => {
  switch (dimension) {
    // 值本身在这一层只是字符串——它从哪一维来，就用哪一维的表去认。
    case "types":
      return typeFilterLabel(value as TypeFilterKey);
    case "fileClasses":
      return fileClassLabel(value as FileClass);
    case "conversationIds":
      // 名字还没送到的照实印标识——不编一个名字。
      return names.conversations.get(value) ?? value;
    case "senderIds":
      return names.senders.get(value) ?? value;
  }
};

export const filterChips = (
  filters: SearchFilters,
  names: FilterNames,
): FilterChip[] =>
  FILTER_DIMENSIONS.flatMap<FilterChip>((dimension) => {
      const values: readonly string[] = filters[dimension];
    if (values.length === 0) return [];
    if (values.length >= SUMMARY_THRESHOLD) {
      return [
        {
          dimension,
          value: null,
          label: `${String(values.length)} 个${DIMENSION_LABELS[dimension]}`,
        },
      ];
    }
    return values.map((value) => ({
      dimension,
      value,
      label: valueLabel(dimension, value, names),
    }));
  });

/** 抽屉里那两个「按格子选」的维度各有哪些可选项。 */
export interface FilterOption {
  readonly value: string;
  readonly label: string;
  readonly path: string;
}

export const TYPE_OPTIONS: readonly FilterOption[] = TYPE_FILTERS.map((item) => ({
  value: item.key,
  label: item.label,
  path: item.path,
}));

export const FILE_CLASS_OPTIONS: readonly FilterOption[] = FILE_CLASSES.map((item) => ({
  value: item.key,
  label: item.label,
  path: item.path,
}));

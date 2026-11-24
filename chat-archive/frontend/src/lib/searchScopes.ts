import {
  AlignLeft,
  AppWindow,
  CircleAlert,
  File,
  FileArchive,
  FileText,
  Image,
  LayoutGrid,
  Link2,
  Mic,
  Music,
  Presentation,
  Smile,
  Table,
} from "@lucide/vue";
import type { Component } from "vue";

import type { FileClass, MessageType, SearchSort } from "@/api/archiveApi";

/**
 * 全局搜索里「消息类型」那一维的一项（PSU-2.66 起是范围，PSU-2.75 起可多选）。
 *
 * 它是**界面上的一项**，不是契约里的消息类型：「图片与视频」在这里是一项，
 * 发查询时展开成两个类型。条件之间的组合规则住在 `searchFilters.ts`。
 */
export type TypeFilterKey =
  | "text"
  | "media"
  | "audio"
  | "file"
  | "link"
  | "emoji"
  | "app";

export interface SearchCapabilities {
  /** 排序控件给哪几项。只有一项时控件仍然显示，但点开只有那一项。 */
  readonly sortModes: readonly SearchSort[];
  /** 有没有发送人名录可点。 */
  readonly senderScope: boolean;
  /** 选中「文件」后展不展开文件类别那一层。 */
  readonly fileClassScope: boolean;
  /**
   * 会话／发送人这两维一次最多选几个（PSU-2.75）。
   *
   * 只管名录那两维：远端比本机小，因为它们的多选是本机代理按值扇出成 N 次
   * **跨境**请求换来的（PSU-2.74），每多一个值就多一次往返。类型走内联参数、
   * 从不扇出，不占这份预算。这是如实的能力差异，不是偷工。
   */
  readonly maxSelections: number;
}

export const LOCAL_SEARCH_CAPABILITIES: SearchCapabilities = {
  sortModes: ["newest", "oldest"],
  senderScope: true,
  fileClassScope: true,
  maxSelections: 20,
};

/**
 * 远端能答出的那一部分。三项都不是「暂时没做」，是这一端确实没有那份事实：
 *
 * - 排序：远端搜索代理到远端服务器的 `/query/search`，给它排序要改那个端点并升
 *   记录模型版本；不升就是「本机以为点了按最旧，老远端照旧按最新答」。
 * - 发送人名录：远端没有账号级的联系人端点，只有「按会话列群成员」。
 * - 文件类别：靠扩展名判定，而远端 `attachment_refs` 只有 blob 标识，没有名字。
 *
 * 按 `capabilities.ts` 的教条，本端没有的能力**界面上不出现**，而不是灰着。
 */
export const REMOTE_SEARCH_CAPABILITIES: SearchCapabilities = {
  sortModes: ["newest"],
  senderScope: false,
  fileClassScope: false,
  maxSelections: 5,
};

export interface TypeFilterItem {
  readonly key: TypeFilterKey;
  readonly label: string;
  readonly icon: Component;
}

/** 「消息类型」这一维的全部选项，界面上的次序就是这里的次序。 */
export const TYPE_FILTERS: readonly TypeFilterItem[] = [
  { key: "text", label: "文本", icon: AlignLeft },
  { key: "media", label: "图片与视频", icon: Image },
  { key: "audio", label: "语音", icon: Mic },
  { key: "file", label: "文件", icon: File },
  { key: "link", label: "链接", icon: Link2 },
  { key: "emoji", label: "表情", icon: Smile },
  { key: "app", label: "应用消息", icon: LayoutGrid },
];

/**
 * 消息类型的线性图标——与范围栏用的是同一批图标组件，不是抄一份。
 *
 * 结果行上不是文件的那些消息（文本、语音、链接、表情、应用消息）此前一律落到
 * `FileIcon` 的兜底标记「文件」上：一条纯文本消息旁边印着一张文件纸，读起来
 * 像它带了个附件。
 */
const TYPE_FILTER_ICONS = new Map<TypeFilterKey, Component>(
  TYPE_FILTERS.map((item) => [item.key, item.icon] as const),
);

const TYPE_TO_FILTER: Partial<Record<MessageType, TypeFilterKey>> = {
  text: "text",
  image: "media",
  video: "media",
  audio: "audio",
  file: "file",
  link: "link",
  // 引用命中展示的就是被引的那段文本，行首与文本同一枚。
  quote: "text",
  emoji: "emoji",
  app: "app",
};

const TYPE_FILTER_LABELS = new Map<TypeFilterKey, string>(
  TYPE_FILTERS.map((item) => [item.key, item.label] as const),
);

export const typeFilterLabel = (key: TypeFilterKey): string =>
  TYPE_FILTER_LABELS.get(key) ?? "";

/** 界面上的一项展开成契约里的消息类型：「图片与视频」是两个。 */
export const typeFilterMessageTypes = (key: TypeFilterKey): MessageType[] => {
  switch (key) {
    case "media":
      return ["image", "video"];
    default:
      return [key];
  }
};

export const FILE_CLASSES: readonly { key: FileClass; label: string; icon: Component }[] = [
  { key: "doc", label: "文稿", icon: FileText },
  { key: "sheet", label: "表格", icon: Table },
  { key: "slide", label: "幻灯片", icon: Presentation },
  { key: "archive", label: "压缩文件", icon: FileArchive },
  { key: "audio", label: "音频", icon: Music },
  { key: "app", label: "应用程序", icon: AppWindow },
  { key: "other", label: "其他", icon: CircleAlert },
];

export const fileClassLabel = (value: FileClass): string =>
  FILE_CLASSES.find((item) => item.key === value)?.label ?? "";

/** 认不出的类型行首给「其他」那枚圆圈——图标集里现成的、最不多话的一枚。 */
const UNKNOWN_TYPE_ICON: Component | null =
  FILE_CLASSES.find((item) => item.key === "other")?.icon ?? null;

/**
 * 可检索的每一种消息类型都要答出一个图标：行首那一格是行的一部分，缺了它
 * 整行左移、与邻行错不齐。`quote` 与 `unknown` 都真实出现在结果里（远端缺
 * 类型时一律默认成 unknown）；但不回到从前那张兜底文件纸——纯文本行挂文件纸
 * 读起来像带了附件，那是特意删掉的。只有不可检索的 `system` 允许答 null。
 */
export const messageTypeIcon = (type: MessageType): Component | null => {
  if (type === "unknown") return UNKNOWN_TYPE_ICON;
  const key = TYPE_TO_FILTER[type];
  return key === undefined ? null : (TYPE_FILTER_ICONS.get(key) ?? null);
};

/** 名录里的一行。住在 lib 而不是组件里：从 `.vue` 导出的类型别处解析不到。 */
export interface DirectoryEntry {
  id: string;
  name: string;
  avatarRef?: string | null;
  /** false = 归档里没有这个人的名字，`name` 就是那串 uid。界面据此弱化。 */
  named?: boolean;
}

export const SORT_LABELS: Record<SearchSort, string> = {
  newest: "按最新时间",
  oldest: "按最旧时间",
};

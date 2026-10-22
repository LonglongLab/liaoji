/**
 * 端能力清单——前端唯一一份「这一端有什么」的事实来源。
 *
 * 端类型由后端 `GET /api/v1/capabilities` 下发，前端不自行推断
 * （`docs/local-remote-boundary.md` 第 5 节）。本文件只做两件事：
 *
 * 1. 记下每项能力**归属哪个端**——照抄该文第 3 节的功能归属表，`boundary`
 *    字段写着它出自表里的哪一行，改表时对得上；
 * 2. 记下每项能力**对应哪个路由与菜单位**，让导航与路由守卫都从这里派生。
 *
 * 于是「不可用」的两种情况在一处分开：归属表说本端没有 → `unsupported`，
 * 界面与路由都不出现；属于本端但后端说还没配 → `not_configured`，出现、
 * 标明未配置、由目的地页面给出配置入口。把后者也藏起来会让用户以为软件没这
 * 功能，违反本项目的诚实展示原则。
 *
 * 新增一项能力＝契约 `CapabilityId` 加一个名字 + 本表加一行 + 后端认领一行。
 * 组件不用改：菜单、路由守卫、工作区切换都从本表派生。
 */
import type { CapabilityId, CapabilityState, Deployment } from "@/api/archiveApi";

/** 路由所属的工作区，与 `router.ts` 里每条路由的 `meta.workspace` 同名。 */
export type Workspace = "local" | "remote" | "public";

/** 底部导航的图标键，与 `AppShell.vue` 里那组 svg 一一对应。 */
export type NavIcon = "overview" | "conversations" | "search" | "export" | "shares" | "stats";

interface NavPlacement {
  readonly label: string;
  readonly icon: NavIcon;
  /** 同一工作区内的排位，小的在前。 */
  readonly order: number;
}

/**
 * 顶栏上的两个固定位置。
 *
 * 与 `nav` 分开是因为它们不是同一种入口：底部导航是工作区内的主目的地，
 * 顶栏是「关于这个工作区本身」的两处。写在清单里而不是写死在组件里——
 * 远程端也有自己的设置屏，写死 `local-settings` 就会漏掉它。
 */
export type TopbarSlot = "settings" | "health";

interface CapabilitySurface {
  readonly workspace: Workspace;
  /** `router.ts` 里的路由名。 */
  readonly route: string;
  readonly path: string;
  /** 不进底部导航的（设置、同步健康在顶栏；引导页没有入口）为 null。 */
  readonly nav: NavPlacement | null;
  /** 顶栏位置；不在顶栏为 null。 */
  readonly topbar: TopbarSlot | null;
}

export interface CapabilityDefinition {
  readonly id: CapabilityId;
  /** `docs/local-remote-boundary.md` 第 3 节归属表里的原文，照抄不改写。 */
  readonly boundary: string;
  readonly deployments: readonly Deployment[];
  /** 没有界面的后台能力（解密、密钥保管、预签名签发……）为 null。 */
  readonly surface: CapabilitySurface | null;
}

/** 后端下发的能力状态；缺席即「这一端没说它有」。 */
export type CapabilityStates = Partial<Record<CapabilityId, CapabilityState>>;

export interface NavItem {
  readonly id: CapabilityId;
  readonly icon: NavIcon;
  readonly label: string;
  readonly to: string;
  readonly state: CapabilityState;
}

const LOCAL_ONLY: readonly Deployment[] = ["local"];
const REMOTE_ONLY: readonly Deployment[] = ["remote"];
const BOTH_ENDS: readonly Deployment[] = ["local", "remote"];

const headless = (
  id: CapabilityId,
  boundary: string,
  deployments: readonly Deployment[],
): CapabilityDefinition => ({ id, boundary, deployments, surface: null });

/**
 * 全表。键是 `CapabilityId`（由契约生成），所以少写一项编译就不过——
 * 契约加了名字而这里忘了归属，不会悄悄变成一个谁也不认领的能力。
 */
export const CAPABILITY_MANIFEST: Readonly<Record<CapabilityId, CapabilityDefinition>> = {
  // ——— 仅本地端 ———
  "wechat-decryption": headless("wechat-decryption", "微信数据库解密", LOCAL_ONLY),
  "source-scan": headless("source-scan", "源扫描、微信路径探测", LOCAL_ONLY),
  "windows-keystore": headless("windows-keystore", "Windows keystore / DPAPI", LOCAL_ONLY),
  onboarding: {
    id: "onboarding",
    boundary: "首次引导 Onboarding",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "public",
      route: "start",
      path: "/start",
      nav: null,
      topbar: null,
    },
  },
  "local-overview": {
    id: "local-overview",
    boundary: "本地浏览与本地检索",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "overview",
      path: "/overview",
      nav: { label: "总览", icon: "overview", order: 1 },
      topbar: null,
    },
  },
  "local-conversations": {
    id: "local-conversations",
    boundary: "本地浏览与本地检索",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "conversations",
      path: "/conversations",
      nav: { label: "会话", icon: "conversations", order: 2 },
      topbar: null,
    },
  },
  "local-search": {
    id: "local-search",
    boundary: "本地浏览与本地检索",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "search",
      path: "/search",
      nav: { label: "搜索", icon: "search", order: 3 },
      topbar: null,
    },
  },
  "local-stats": {
    id: "local-stats",
    boundary: "本地浏览与本地检索",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "stats",
      path: "/stats",
      nav: { label: "统计", icon: "stats", order: 5 },
      topbar: null,
    },
  },
  "local-export": {
    id: "local-export",
    boundary: "导出、媒体缓存、安全卸载",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "export",
      path: "/export",
      nav: { label: "导出", icon: "export", order: 4 },
      topbar: null,
    },
  },
  "remote-stats": {
    id: "remote-stats", boundary: "自托管远程统计与导出（PSU-2.197）", deployments: REMOTE_ONLY,
    surface: { workspace: "remote", route: "remote-stats", path: "/remote/stats",
      nav: { label: "统计", icon: "stats", order: 4 }, topbar: null },
  },
  "remote-export": {
    id: "remote-export", boundary: "自托管远程统计与导出（PSU-2.197）", deployments: REMOTE_ONLY,
    surface: { workspace: "remote", route: "remote-export", path: "/remote/export",
      nav: { label: "导出", icon: "export", order: 3 }, topbar: null },
  },
  "local-settings": {
    id: "local-settings",
    // 表外条目：设置页本身不是归属表的一行，它是那一行里三项本地能力
    // （媒体缓存、安全卸载）与密钥删除的入口，故随它们归本地端。
    boundary: "导出、媒体缓存、安全卸载（设置页是其入口）",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "settings",
      path: "/settings",
      nav: null,
      topbar: "settings",
    },
  },
  "media-cache": headless("media-cache", "导出、媒体缓存、安全卸载", LOCAL_ONLY),
  "safe-unload": headless("safe-unload", "导出、媒体缓存、安全卸载", LOCAL_ONLY),
  "incremental-sync": headless("incremental-sync", "增量同步、附件直传", LOCAL_ONLY),
  // 无 surface：提交反馈不是一个目的地，是别的屏上的一个按钮，靠
  // `capabilityState(...) === "available"` 做 `v-if`。远程端**永远不可用**
  // （那一端的页面出不去网，也不该替这台机器的用户发反馈），所以它按归属表
  // 直接判 unsupported，入口不出现——不是「暂时未配置」。
  feedback: headless("feedback", "提交反馈（外部平台代理）", LOCAL_ONLY),
  // 无 surface：它是会话信息抽屉里的一块，靠 `capabilityState(...)` 决定出不出现。
  // 删的是这台电脑上的微信，远程端**永远不可用**，那一块不出现（PSU-2.186）。
  "wechat-cleanup": headless("wechat-cleanup", "在微信中清理已备份的记录", LOCAL_ONLY),

  // ——— 两端共有 ———
  "sync-health": {
    id: "sync-health",
    boundary: "同步健康状态（只读）",
    deployments: BOTH_ENDS,
    surface: {
      workspace: "local",
      route: "sync-health",
      path: "/sync-health",
      nav: null,
      topbar: "health",
    },
  },
  "archive-status": headless("archive-status", "归档状态展示", BOTH_ENDS),
  "object-storage-config": headless("object-storage-config", "对象存储配置", BOTH_ENDS),
  "attachment-view": headless("attachment-view", "附件查看（签名 URL）", BOTH_ENDS),
  "remote-conversations": {
    id: "remote-conversations",
    boundary: "查看远程聊天记录",
    deployments: REMOTE_ONLY,
    surface: {
      workspace: "remote",
      route: "remote-conversations",
      path: "/remote/conversations",
      nav: { label: "会话", icon: "conversations", order: 1 },
      topbar: null,
    },
  },
  "remote-search": {
    id: "remote-search",
    boundary: "搜索远程聊天记录",
    deployments: REMOTE_ONLY,
    surface: {
      workspace: "remote",
      route: "remote-search",
      path: "/remote/search",
      nav: { label: "搜索", icon: "search", order: 2 },
      topbar: null,
    },
  },

  // ——— 仅远程端 ———
  // 登录与向导那两屏**不在这张表里**：它们归认证轴管（`workspaceRouting.ts`
  // 的 `authRedirect`），而未认证的远程端按契约不下发 capabilities——把登录页
  // 挂到能力上，它自己就会被判成 unsupported，变成一扇进不去的门。
  "password-login": headless("password-login", "密码登录、TOTP", REMOTE_ONLY),
  totp: headless("totp", "密码登录、TOTP", REMOTE_ONLY),
  "browser-session": {
    id: "browser-session",
    boundary: "浏览器会话管理",
    deployments: REMOTE_ONLY,
    // 远程端的设置页：会话列表、退出所有设备、两步验证开关、设备撤销。
    // 另外三项远程能力的入口都在这一屏里，所以它们自己不再各占一个路由。
    surface: {
      workspace: "remote",
      route: "remote-settings",
      path: "/remote/settings",
      nav: null,
      topbar: "settings",
    },
  },
  "device-revocation": headless("device-revocation", "设备列表与撤销", REMOTE_ONLY),
  "presigned-url": headless("presigned-url", "预签名 URL 签发", REMOTE_ONLY),
  "object-storage-credentials": headless(
    "object-storage-credentials",
    "对象存储凭据加密保管",
    REMOTE_ONLY,
  ),
  // 没有自己的路由：它住在远程设置页里的一块面板。
  //
  // **被备份的是远程归档**（本机归档按设计是可回收的近期工作集，它的灾备是
  // 可迁移归档包 FR-RECOVERY-006，不是这套定时全量），**但配置它是本机端的事**
  // ——填凭据是已配对设备做的，浏览器会话只看得到一条只读的近况。所以两端
  // 都认领它，和 `object-storage-config` 同一条形状。
  "backup-management": headless("backup-management", "远程归档定期备份", REMOTE_ONLY),
  "pairing-code": headless("pairing-code", "设备列表与撤销", BOTH_ENDS),
  "remote-backup": headless("remote-backup", "远程归档定期备份", BOTH_ENDS),

  // ——— 在线分享（PSU-2.182）———
  // 分享页挂在远端（本机只听回环，别人打不开），发起与管理两端都有。
  // `share` 是多选底栏上那个「分享」按钮：没有自己的目的地，只管出不出现。
  // 本机没配对远端时是 not_configured——按钮照样在，点开说明并给配置入口。
  share: headless("share", "在线分享：发起与管理", BOTH_ENDS),
  // 「我的分享」拆成两项，因为一个入口只属于一个工作区：本机放在本地工作区
  // 「导出」下面（原型第 4 块），远程网页只有远程工作区，放在那里的导航里。
  // 两者是同一个页面组件，打的是同一组 `/api/v1/remote/shares*`。
  "local-shares": {
    id: "local-shares",
    boundary: "在线分享：发起与管理",
    deployments: LOCAL_ONLY,
    surface: {
      workspace: "local",
      route: "shares",
      path: "/shares",
      nav: { label: "我的分享", icon: "shares", order: 4.5 },
      topbar: null,
    },
  },
  "remote-shares": {
    id: "remote-shares",
    boundary: "在线分享：发起与管理",
    deployments: REMOTE_ONLY,
    surface: {
      workspace: "remote",
      route: "remote-shares",
      path: "/remote/shares",
      nav: { label: "我的分享", icon: "shares", order: 5 },
      topbar: null,
    },
  },
};

const definitions = (): readonly CapabilityDefinition[] => Object.values(CAPABILITY_MANIFEST);

/**
 * 一项能力在本端的实际状态。
 *
 * 归属先判：归属表说本端没有，下发的状态说什么都是 `unsupported`。属于本端时才
 * 看后端怎么说；后端没提这一项，按「这一端没说它有」处理，而不是默认可用——
 * 未认证的远程端就不带 `capabilities`，那时整张菜单本就该是空的。
 *
 * `deployment` 为 null 是「端还没答复」（清单没拿到，多半是服务连不上）。那时
 * 不抢答：入口照常给，点进去由页面自己如实报连不上。把菜单清空会让一个抖动的
 * 接口变成一条走不出去的死路，而且那也不算「本端没有」。
 */
export const capabilityState = (
  deployment: Deployment | null,
  states: CapabilityStates,
  id: CapabilityId,
): CapabilityState => {
  if (deployment === null) return "available";
  if (!CAPABILITY_MANIFEST[id].deployments.includes(deployment)) return "unsupported";
  return states[id] ?? "unsupported";
};

/**
 * 这项能力的入口该不该出现在当前工作区里。
 *
 * 两个条件缺一不可：本端得有它（不是 `unsupported`），而且它的入口本就属于
 * 这个工作区。后一条是工作区自己的事，与端无关——本机应用逛远程工作区时，
 * 顶栏不该还挂着「设置」和「同步健康」，那两处是本地工作区的陈设。
 */
export const showsInWorkspace = (
  deployment: Deployment | null,
  states: CapabilityStates,
  workspace: Workspace,
  id: CapabilityId,
): boolean => {
  const surface = CAPABILITY_MANIFEST[id].surface;
  if (surface === null || surface.workspace !== workspace) return false;
  return capabilityState(deployment, states, id) !== "unsupported";
};

/** 某工作区里本端看得见的导航项，按清单排位。 */
export const visibleNavItems = (
  deployment: Deployment | null,
  states: CapabilityStates,
  workspace: Workspace,
): NavItem[] =>
  definitions()
    .filter(
      (entry) =>
        entry.surface?.nav != null &&
        showsInWorkspace(deployment, states, workspace, entry.id),
    )
    .sort((left, right) => (left.surface?.nav?.order ?? 0) - (right.surface?.nav?.order ?? 0))
    .map((entry) => {
      const surface = entry.surface as CapabilitySurface;
      const nav = surface.nav as NavPlacement;
      return {
        id: entry.id,
        icon: nav.icon,
        label: nav.label,
        to: surface.path,
        state: capabilityState(deployment, states, entry.id),
      };
    });

/**
 * 本端能不能进这条路由。
 *
 * 没有能力认领的路由一律放行——前端不重复实现后端的安全边界，遗漏一处最坏是
 * 调到不存在的接口得 404（`docs/local-remote-boundary.md` 第 5 节）。
 * `not_configured` 也放行：页面打得开，缺什么由页面自己说。
 */
export const canEnter = (
  routeName: string,
  deployment: Deployment,
  states: CapabilityStates,
): boolean => {
  const owner = definitions().find((entry) => entry.surface?.route === routeName);
  if (owner === undefined) return true;
  return capabilityState(deployment, states, owner.id) !== "unsupported";
};

/**
 * 被守卫拦下时该去哪儿：本端第一个看得见的目的地，先本端自己的工作区。
 *
 * 一个可见目的地都没有时返回 null——未认证的远程端就是这样。那一端由认证轴
 * 先一步送去登录页（`workspaceRouting.ts` 的 `authRedirect`），走不到这里。
 */
export const fallbackPath = (
  deployment: Deployment,
  states: CapabilityStates,
): string | null => {
  const order: Workspace[] =
    deployment === "remote" ? ["remote", "local", "public"] : ["local", "remote", "public"];
  for (const workspace of order) {
    const first = visibleNavItems(deployment, states, workspace)[0];
    if (first !== undefined) return first.to;
  }
  return null;
};

/**
 * 顶栏某个位置上该显示哪个入口；没有就 null。
 *
 * 与 `visibleNavItems` 同源同规则，只是位置不同。组件里因此不必再写
 * 「设置＝/settings」这种字面量——远程端的设置屏是另一条路由，写死就会漏。
 */
export const topbarEntry = (
  deployment: Deployment | null,
  states: CapabilityStates,
  workspace: Workspace,
  slot: TopbarSlot,
): { to: string; state: CapabilityState } | null => {
  const owner = definitions().find(
    (entry) =>
      entry.surface?.topbar === slot &&
      showsInWorkspace(deployment, states, workspace, entry.id),
  );
  if (owner?.surface === undefined || owner.surface === null) return null;
  return {
    to: owner.surface.path,
    state: capabilityState(deployment, states, owner.id),
  };
};

/** 顶栏的工作区切换：只在另一边真有可见目的地时才给入口。 */
export const workspaceSwitch = (
  deployment: Deployment | null,
  states: CapabilityStates,
  workspace: Workspace,
): { to: string; label: string } | null => {
  if (deployment === "local") return null;
  const target: Workspace = workspace === "remote" ? "local" : "remote";
  const first = visibleNavItems(deployment, states, target)[0];
  if (first === undefined) return null;
  return { to: first.to, label: target === "remote" ? "远程档案" : "返回本机" };
};

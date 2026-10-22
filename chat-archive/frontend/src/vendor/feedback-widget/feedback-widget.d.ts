/**
 * `feedback-widget.js`（平台包通道产物）的类型声明。
 *
 * 上游是 JSDoc 注释的 JS，没有 `.d.ts`——这份是本仓新写的，**只描述被本仓调用到的
 * 那部分导出**。逐字节拷贝的只有 `feedback-widget.js` 一个文件，这份不在 hash 表里。
 *
 * 形状的权威来源是上游的 `packages/feedback-widget/src/`：
 * `options.js`（注入面）、`submit.js`（载荷）、`index.js`（返回值）。改这份之前先读那三个。
 */
/** 一个附件：截图、打码后的图，或录屏事件（`application/json`）。 */
export interface FeedbackWidgetAttachment {
  readonly name: string;
  /** `image/*`、`video/*` 或 `application/json`（录屏事件走的就是后者）。 */
  readonly type: string;
  /** 调用方声明的字节数。本仓后端不信它，转发前用解码后量到的真值覆盖。 */
  readonly size: number;
  /** base64 data URL。 */
  readonly dataUrl: string;
}

/**
 * widget 交给宿主 `submit` 的那份载荷。
 *
 * **没有 `title`**（`SCN-FWB-122` (3)）：标题由平台生成（入库占位 + AI 回写），
 * 连这个键都不出现，免得哪天有人顺手填上它而让占位被跳过。
 *
 * 可选键是**真的会缺席**，不是恒为空——`contact` 空串时不出现，
 * `context` 在宿主没接 `provideContext` 时不出现（`EXC-FWB-027` (3)：
 * 「没有上下文」与「没接钩子」必须分得开，两者的修法不同）。
 */
export interface FeedbackWidgetPayload {
  readonly projectId: string;
  /** 用户没选时是 `null`，而且这个键**始终在场**（`SCN-FWB-137` (2)）。 */
  readonly submittedType: "bug" | "improvement" | "question" | null;
  readonly description: string;
  readonly attachments: readonly FeedbackWidgetAttachment[];
  readonly contact?: string;
  /** 已脱敏的页面地址（`SCN-FWB-135`）。宿主**不要再洗第二遍**。 */
  readonly pageUrl?: string;
  readonly userAgent?: string;
  /** 宿主 `provideContext` 的返回值，放在 `host` 这一格（`EXC-FWB-031`）。 */
  readonly context?: { readonly host: Record<string, unknown> };
}

/** 宿主的提交通道要答出来的那份回执。 */
export interface FeedbackWidgetReceipt {
  /** 提交者自己回看这条反馈的链接；必须是 http(s)，否则 widget 按「拿不到」处理。 */
  readonly ownerUrl?: string;
  readonly issueKey?: string;
  readonly expiresAt?: string;
}

/**
 * 宿主注入面：**两项必需 + 若干钩子**，其余一律由平台按 `projectId` 下发。
 *
 * 传 `privacy` / `limits` / `copy` / `staticCopy` 这类平台侧配置会**当场抛**
 * `WIDGET_PLATFORM_CONFIG_INJECTED`（不是忽略）。
 */
export interface MountPlatformFeedbackWidgetOptions {
  /** 平台登记过的项目 id。本仓是 `proj_wxdump`。 */
  projectId: string;
  /** 提交通道。本仓走自己后端的同源代理——CSP 是 `connect-src 'self'`。 */
  submit: (
    payload: FeedbackWidgetPayload,
    options: { signal: AbortSignal | null },
  ) => Promise<FeedbackWidgetReceipt | null | undefined>;
  /** `auto`（默认，跟随系统）/ `light` / `dark`。 */
  colorScheme?: "auto" | "light" | "dark";
  /**
   * 提交那一刻调**一次**，交出只有宿主才知道的领域事实（本仓：应用版本号等）。
   * 返回值必须能过 `JSON.stringify`；抛异常按「没给」处理，不阻断提交。
   */
  provideContext?: () => Record<string, unknown> | null;
  /**
   * 提交失败时把**宿主自己后端**那套错误码翻成一句人话（`SCN-FWB-147`）。
   *
   * 返回 `string` = 显示它；返回 `null` = 「我也说不清」，回落平台的通用文案。
   * **非函数按「没传」处理、不抛**；钩子自己抛异常时 widget 也按「没给」处理并留痕。
   *
   * ⚠️ 实现里**不得回显 `error.message`**：那段文本可能带着服务端返回的原文，而
   * 平台的 `SCN-FWB-056` 明写「只给模式名、绝不回显命中的原文」。屏幕上只该出现
   * 宿主显式翻译过的那一句。本仓的实现见 `src/lib/platformFeedback.ts`。
   */
  describeSubmitError?: (error: unknown) => string | null;
  /**
   * 实例**自毁完成之后**调一次。调用方**必须接它**并清掉自己存的句柄
   * （`SCN-FWB-145` (4)）。不接的症状：反馈入口关一次就再也点不开，
   * 或者同时弹出两个对话框——两种都不报错、控制台干净。
   */
  onDestroyed?: () => void;
  /**
   * 平台配置端点。widget 会去拉 `GET <configUrl>?projectId=…`。
   *
   * 本仓传的是 `/api/v1/feedback/widget-config`——后端那条同源代理（2026-09-23）。
   * **必须是同源相对路径**：打包形态 SPA 的 CSP 是 `connect-src 'self'`，绝对地址
   * 会被浏览器挡下，而被挡下的 fetch 只是 reject，widget 安静地退回内置保守值。
   *
   * 不传（或拉不到）时走内置保守值，按 `SCN-FWB-130` (3) 那仍然是允许的路径；
   * 但它**不该是日常路径**——内置值里没有 `proj_wxdump` 的 `rejectPatterns`，
   * 于是页面地址里的 `wxid_…` 会被原样发出去并被平台的脱敏门禁 400 拒掉。
   */
  configUrl?: string | null;
  /**
   * 回执页补联系方式的**前缀**（`SCN-FWB-136`）。widget 拼
   * `PUT <contactUrl>/<encodeURIComponent(issueKey)>/contact`，带
   * `Authorization: Bearer <capability>`（取自回执 `ownerUrl` 的 hash）。
   *
   * 包通道默认 `null` = 回执页**不画**输入框。本仓传 `/api/v1/feedback/issues`
   * ——后端那条同源代理。响应体里 `error === 'FEEDBACK_PRIVACY_REJECTED'` 时界面
   * 说「这个联系方式没法保存」（不给重试），其余失败一律「可以重试」。
   */
  contactUrl?: string | null;
  /**
   * `configUrl` / `contactUrl` 两条请求用的 `fetch`（上游 `embed/mount.js`）。
   * 不传就是 `globalThis.fetch`。本仓传 `ArchiveApi.sameOriginFetch`，为的是带上
   * 本机的 `X-Archive-Token`。
   */
  fetchImpl?: typeof globalThis.fetch | null;
  /**
   * 静态界面文案表（widget 0.1.6 起）。录屏默认「结构档」：整个文本节点 trim 后
   * **精确命中**这张表的原样显示，其余照遮。
   *
   * 这是**产品知识**，归宿主交（`staticCopy` 仍是平台键，传它照旧抛
   * `WIDGET_PLATFORM_CONFIG_INJECTED`）。本仓传的是构建期从 `.vue` 模板抽出来的那张
   * （`virtual:feedback-ui-copy`）加能力表里的导航名，见 `src/lib/platformFeedback.ts`。
   *
   * widget 侧护栏：逐条 trim、去重、丢空串、丢少于 2 个字符的、丢不含任何字母/汉字的
   * （纯数字/标点）、单条超过 200 字的丢弃，最多收 20000 条。
   *
   * ⚠️ 表里只能放源码里写死的字面量。混进一条用户数据，它就会原样录进回放。
   */
  uiCopy?: readonly string[];
  /**
   * 返回当前路由**模板**（`/s/:token` 这种，不是 `/s/abc123`），widget 0.1.6 起。
   *
   * 只收以 `/` 开头、仅含 `/`、字母数字、`:`、`-`、`_`、`*`、长度 ≤ 120 的字符串，
   * 否则丢弃。本仓取 vue-router `matched` 最后一条记录的 `path` 并去掉自定义正则段；
   * 归一不了就返回空串，**绝不回退成真实路径**（真实路径里带会话 id 与分享令牌）。
   */
  routeTemplate?: () => string;
}

export interface MountedFeedbackWidget {
  readonly version: string;
  readonly host: HTMLElement;
  readonly shadow: ShadowRoot;
  readonly config: { readonly source: "built-in" | "platform"; [key: string]: unknown };
  /** **幂等**。在 `finally` 里兜底再调一次不会产生第二次 `onDestroyed` 通知。 */
  destroy: () => void;
}

/**
 * 挂载并**立即打开**提交对话框。所以宿主是「点入口时挂载、关闭时自毁」，
 * 而不是「常驻一个隐藏实例」。
 */
export function mountPlatformFeedbackWidget(
  options: MountPlatformFeedbackWidgetOptions,
): Promise<MountedFeedbackWidget>;

/** widget 版本号的单点。`SCN-FWB-118` (2) 要求它在运行时可核对。 */
export const FEEDBACK_WIDGET_VERSION: string;
/** 挂载后版本号落在宿主元素的这个属性上（devtools 里一眼可见）。 */
export const WIDGET_VERSION_ATTRIBUTE: string;
export const BUILT_IN_WIDGET_CONFIG: Readonly<Record<string, unknown>>;
export function resolveWidgetConfig(remote: unknown): Readonly<Record<string, unknown>>;

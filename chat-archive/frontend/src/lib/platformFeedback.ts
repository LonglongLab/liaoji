/**
 * 反馈入口的接线：把平台统一下发的 widget 挂起来（`SCN-FWB-118`、`SCN-FWB-130`）。
 *
 * 本仓**不再自绘提交对话框**。用户 2026-09-20 拍板：「提交反馈这套 UI 与交互由平台
 * 统一提供，接入方只负责挂入口、不自己实现」，理由是「交互一调，其他项目还要再重新
 * 调一遍」。所以这个文件只剩几件事——挂入口、给提交通道、给只有本仓才知道的领域事实、
 * 把只有本仓认得的错误码翻成人话。
 *
 * ## 宿主只给这几样，别的一律不给
 *
 * 划线判据是 `EXC-FWB-027` 改写过的那句「**谁才可能知道**」，不是项数。
 *
 * | 给 | 为什么只能宿主给 |
 * | --- | --- |
 * | `projectId` | 本产品在平台上的身份 |
 * | `submit` | 打包形态 CSP 是 `connect-src 'self'`，页面连不到平台，必须走自己后端的同源代理 |
 * | `provideContext` | 应用版本号是**应用内存里的领域对象**，不在 DOM 里，没有任何选择器选得出来 |
 * | `describeSubmitError` | 失败时抛出来的是**本仓后端**那套错误码，widget 连见都没见过（`SCN-FWB-147`） |
 * | `contactUrl` | 回执页补联系方式那一跳也得走同源代理（`SCN-FWB-136`），代理的路径只有本仓知道 |
 * | `fetchImpl` | widget 自己发的那两条同源请求要带本机的 `X-Archive-Token`，令牌只有本仓的 API 封装拿着 |
 * | `onDestroyed` | 「我的句柄存在哪」只有调用方知道（`SCN-FWB-145` (4)） |
 * | `uiCopy` | 界面上**写死**了哪些字是产品知识，只在本仓的模板里（构建期抽取，见下） |
 * | `routeTemplate` | 当前页面是哪一种页面（`/s/:token`，不带真实值）只有本仓的路由表知道 |
 *
 * 脱敏策略、内容区选择器、三个上限数、全部文案**一律由平台按 `projectId` 下发**。
 * 传进去会当场抛 `WIDGET_PLATFORM_CONFIG_INJECTED`（不是忽略——忽略等于让接入方以为
 * 自己配上了，而线上跑的是另一套值）。
 *
 * ## 录屏的静态文案表：`uiCopy`
 *
 * 用户 2026-09-29 拍板：录屏默认「结构档」——整个文本节点 trim 后**精确命中**静态文案表
 * 的原样显示，数据区照遮。那张表由本仓在**构建期**从 `.vue` 模板抽出来
 * （`scripts/uiCopy.mjs` 的 `collectUiCopy`，经 `vite.config.ts` 做成虚拟模块
 * `virtual:feedback-ui-copy`），随安装包进产物、离线可用，再经 widget 0.1.6 的宿主选项
 * `uiCopy` 交出去。
 *
 * 为什么归宿主交、而不是像 `staticCopy` 那样由平台下发：划线是「**配置平台下发、产品
 * 知识宿主交**」。脱敏策略是配置；「这个产品的界面上写死了哪些字」是产品知识——平台没有
 * 任何办法自己知道。`staticCopy` 仍是平台键，宿主照旧不许传。
 *
 * 模板抽取收不到**脚本里**的界面文字（侧栏各项的名字写在 `capabilities.ts` 的能力表里，
 * 模板里只是 `{{ item.label }}`），所以这里把能力表里的导航名一并补进去——同样是源码里的
 * 字面量，与用户数据无关。
 *
 * 长度、字符类别、条数上限这些护栏在 widget 侧做，这里不重复。
 *
 * 关键控件上另有动作标记（「点击：导出」），登记与纪律见 `feedbackActionRegistry.ts`。
 */

import templateUiCopy from "virtual:feedback-ui-copy";
import type { Router } from "vue-router";

import type { ArchiveApi, FeedbackAttachment, FeedbackSubmission } from "@/api/archiveApi";
import { ArchiveApiError } from "@/api/archiveApi";
import { CAPABILITY_MANIFEST } from "@/lib/capabilities";
import type {
  FeedbackWidgetPayload,
  FeedbackWidgetReceipt,
  MountedFeedbackWidget,
} from "@/vendor/feedback-widget/feedback-widget.js";

/** 本产品在平台上的项目标识。后端注入到上游载荷里的是同一个值（`feedback.py`）。 */
export const FEEDBACK_PROJECT_ID = "proj_wxdump";

/**
 * 平台配置的取数口：本仓后端的**同源代理**（契约 `/api/v1/feedback/widget-config`）。
 *
 * ## 必须是相对路径
 *
 * 打包形态 SPA 的 CSP 是 `connect-src 'self'`（`backend/src/chat_archive/spa.py`）。
 * 写一个指向平台的绝对地址，那条 fetch 会被浏览器当场挡下——而**被挡下的 fetch
 * 只是 reject**，`loadWidgetConfig` 把任何异常都吞掉并退回内置保守值，界面看起来
 * 完全正常。也就是说这个错误的全部症状，是脱敏规则悄悄换成了另一套。
 *
 * ## widget 会在后面拼一个 `?projectId=…`，而后端不看它
 *
 * 平台 `embed/platform.js` 的 `createPlatformConfigFetcher` 写死了这个查询参数。
 * 本仓后端**故意不读**它：页面能指定项目标识，就等于页面上任何一段脚本都能拉到
 * 别的项目的脱敏规则（那份规则里是那个项目禁发什么的完整正则）。项目标识是那台
 * 机器的配置，由后端注入——与提交那条端点逐字同一条纪律。
 */
export const FEEDBACK_WIDGET_CONFIG_URL = "/api/v1/feedback/widget-config";

/**
 * 回执页补联系方式的同源代理**前缀**（`SCN-FWB-136`，契约
 * `PUT /api/v1/feedback/issues/{issue_key}/contact`）。
 *
 * widget 在后面拼 `/<encodeURIComponent(issueKey)>/contact`，带
 * `Authorization: Bearer <capability>`（两样都取自提交回执 `ownerUrl` 的 hash）；
 * 后端把它原样转到平台。**不传这一项，回执页就一个输入框都不画**——包通道的
 * 默认是「没有补写通道」，那是平台 2026-09-22 拍板 (3) 的刻意设计，不是 bug。
 *
 * 必须是相对路径，理由与 `FEEDBACK_WIDGET_CONFIG_URL` 逐字相同（CSP
 * `connect-src 'self'`：绝对地址会被挡下，而被挡下只是一次「没存上」）。
 */
export const FEEDBACK_CONTACT_URL = "/api/v1/feedback/issues";

/**
 * 把 widget 的载荷翻成本仓 `POST /api/v1/feedback` 的请求体。
 *
 * 两边不是同一份形状，这个函数就是那条缝。逐条说清每个字段凭什么这么翻：
 *
 * - **没有 `title`**。`SCN-FWB-122` (3)：标题由平台生成（入库占位 + AI 回写），
 *   「提交载荷里连 `title` 这个键都不出现」。所以这里**不编一个**（比如拿描述的
 *   前 N 个字）——编出来的标题会让平台那边的占位被跳过，而那一步没有任何症状：
 *   反馈照样入库，只是标题永远停在用户随手写的第一行。本仓契约因此把 `title`
 *   从 required 里拿掉了。
 * - **`submittedType: null` → `"unclear"`**。平台服务端的 `normalizeSubmittedType(null)`
 *   落的就是这个既有哨兵，所以这次映射**无损**——不是本层替用户做主，是同一件事在
 *   两处的同一个答案。本仓契约的枚举里 `unclear` 本来就在。
 * - **可选键真的缺席**。`contact` / `pageUrl` / `userAgent` / `context` 在 widget
 *   那边不给时是「键不在」而不是空值（`EXC-FWB-027` (3)：「没有上下文」与「没接钩子」
 *   必须分得开，两者修法不同）。这里用条件展开保住这一点，**不要**改成 `?? ""`
 *   ——那会把「没填联系方式」和「填了空串」抹成同一件事，而且恒发空串会让上游的
 *   占位逻辑拿到一个合法但没意义的值。
 * - **`pageUrl` 不再洗第二遍**。它在 widget 那边挂载时就洗过了（`SCN-FWB-135` (5)
 *   要求清单上显示的那个字符串与载荷里那个字符串逐字相等），这里再洗一遍就会让
 *   两个字符串分叉，而分叉没有症状。
 */
export const toFeedbackSubmission = (payload: FeedbackWidgetPayload): FeedbackSubmission => ({
  description: payload.description,
  submitted_type: payload.submittedType ?? "unclear",
  source_type: "manual",
  attachments: payload.attachments.map(
    (item): FeedbackAttachment => ({
      name: item.name,
      type: item.type,
      // 后端**不信**这个数：转发前用解码后量到的真值覆盖掉（上游会拿两者对账）。
      size: item.size,
      data_url: item.dataUrl,
    }),
  ),
  ...(payload.contact === undefined ? {} : { contact: payload.contact }),
  ...(payload.pageUrl === undefined ? {} : { page_url: payload.pageUrl }),
  ...(payload.userAgent === undefined ? {} : { user_agent: payload.userAgent }),
  ...(payload.context === undefined ? {} : { context: payload.context }),
});

/**
 * 「没网」这一类错误，**翻成 widget 认得的形状**。
 *
 * widget 的 `isOfflineError` 认三样：宿主显式标的 `error.code === 'OFFLINE'`、
 * `navigator.onLine === false`、以及 `fetch` 断网时抛的 `TypeError`。
 *
 * 后两样在本仓的形态下**都不成立**：提交打的是 `127.0.0.1` 上自己的后端，断网时
 * 那一跳照样成功，失败发生在**后端去连平台**那一跳，回到前端时是一个规规矩矩的
 * HTTP 错误响应（`FEEDBACK_UPSTREAM_UNREACHABLE`），既不是 `TypeError`，在「拔网线
 * 但系统还没更新 onLine」时也读不到 `navigator.onLine === false`。
 *
 * 不翻的症状：断网提交时用户看到的是「提交失败了，可以点『重试』再来一次」，
 * 而正确的那句是「看起来现在没有网络。内容还在这儿，联网后点『重试』即可」——
 * 前者会让人以为是内容有问题，然后反复重试。
 *
 * 用 `code` 这个显式标记而不是伪造一个 `TypeError`：那是 widget 文档里明写留给
 * 宿主的那条缝（「宿主显式标的 `code`」），伪造异常类型是在赌它的实现细节。
 */
const OFFLINE_UPSTREAM_CODES = new Set(["FEEDBACK_UPSTREAM_UNREACHABLE"]);

export class FeedbackOfflineError extends Error {
  readonly code = "OFFLINE";
  /** 原来那条错误**留着**：失败分类是为了选对文案，不是为了把真因擦掉。 */
  override readonly cause: ArchiveApiError;

  constructor(cause: ArchiveApiError) {
    super(cause.message);
    this.name = "FeedbackOfflineError";
    this.cause = cause;
  }
}

/**
 * 提交通道：本仓后端的**同源代理**。
 *
 * 不从页面直连平台，两个理由各自都成立——打包形态 SPA 的 CSP 是 `connect-src 'self'`
 * （`backend/src/chat_archive/spa.py`），页面根本连不到第三方域名；而本仓的惯例本来
 * 也是外网请求由后端出（更新检查、远程登录都如此）。`project_id` 由后端注入，界面
 * 无从指定也不该指定。
 *
 * 失败**照原样往外抛**（除了上面那一类翻译）：widget 那边失败不清空任何东西，
 * 对话框不关、正文与附件一字不丢、给「重试」（`SCN-FWB-130` (4)）。这里自己吞掉
 * 错误再返回一个假回执，会让用户看到「提交成功」而东西根本没发出去。
 */
export const submitViaBackendProxy = async (
  api: ArchiveApi,
  payload: FeedbackWidgetPayload,
): Promise<FeedbackWidgetReceipt> => {
  try {
    const receipt = await api.submitFeedback(toFeedbackSubmission(payload));
    // 本仓契约回 `{id, owner_url}`，widget 要 `{ownerUrl, issueKey?}`。
    return { ownerUrl: receipt.owner_url, issueKey: receipt.id };
  } catch (error) {
    if (error instanceof ArchiveApiError && OFFLINE_UPSTREAM_CODES.has(error.code)) {
      throw new FeedbackOfflineError(error);
    }
    throw error;
  }
};

/**
 * 把本仓后端那套错误码翻成**一句可照做的话**（widget 的 `describeSubmitError`，
 * `SCN-FWB-147`）。
 *
 * ## 为什么这一条非接不可
 *
 * widget 只知道「`submit` 抛了」，于是它只能说「提交失败了，可以点『重试』再来一次」。
 * 2026-09-22 的真打包形态走查实测：从带会话参数的页面提交，页面地址里的 `wxid_…`
 * 命中平台的脱敏门禁 → 400、零写入 → 界面说「可以点重试」→ **而重试永远不会成功**。
 * 那句话在最要紧的那种失败上是假的，用户只能反复重试。
 *
 * 本仓接入平台 widget **之前**，它自己那个对话框是说得出这句话的（后端早就把
 * `upstream.patterns` 透传上来了，见 `feedback.py` 的 `FeedbackUpstreamRejection`）。
 * 接入之后这条信息没了——不是后端丢了它，是界面这一侧没人去读。这个钩子把它接回来。
 *
 * ## 三条硬规矩
 *
 * 1. **绝不回显 `error.message`**。那段文本可能带着上游返回的原文，而平台的
 *    `SCN-FWB-056` 明写「响应体与日志只给模式名、绝不回显命中的原文」。这里只用
 *    `code` 与 `rejectedPatterns` 两样**结构化**输入拼句子——`rejectedPatterns` 是
 *    模式的**名字**（`wxid`、`phone`），后端已按字符集白名单过滤过，不含用户内容。
 *    ⚠️ 不要「顺手」加一句 `?? error.message` 兜底：它看起来像在多给用户一点信息，
 *    实际是把本该被洗掉的那些字抄到用户屏幕上。
 * 2. **说不清就返回 `null`**，让 widget 回落到平台的通用文案。自己编一句「未知错误」
 *    只会比通用文案更没用，而且把两处文案分了叉。
 * 3. **永不抛**。widget 那边会 try/catch 并按「没给」处理，但一个会崩的翻译函数意味着
 *    错误区里本该有的那句话在**某些**失败上静默消失——最难查的那一类。
 *
 * ## 为什么不直接把后端那句 `message` 显示出来
 *
 * 除了规矩 1，还有一条实质理由：后端那句是「请删掉**这些内容**后再提交」，而走查里
 * 命中的东西根本不在正文里——它在**页面地址**上，用户翻遍输入框也找不到可删的东西。
 * 只有界面这一侧知道那串 `wxid_` 是从哪儿来的，所以这句话必须在这里写。
 */
export const describePlatformFeedbackError = (error: unknown): string | null => {
  if (!(error instanceof ArchiveApiError)) return null;

  if (error.code === "FEEDBACK_UPSTREAM_REJECTED") {
    // 模式名照 `feedback.py` 的白名单（`[A-Za-z0-9_.-]`）原样列出：它是用户唯一
    // 能拿去核对的线索，去掉它这句话就退化成「反正被拒了」。
    const patterns =
      error.rejectedPatterns.length > 0 ? `（${error.rejectedPatterns.join("、")}）` : "";
    return (
      `反馈平台在内容里检测到疑似隐私信息${patterns}，这条没有收下。` +
      "这串标识多半来自当前页面的地址——换到不带会话或搜索参数的页面（比如「统计」）" +
      "重新打开反馈，或先把正文与附件里的这些内容删掉，再点「重试」。"
    );
  }

  if (error.code === "FEEDBACK_PAYLOAD_TOO_LARGE") {
    return "这条反馈连同附件太大了，平台收不下。删掉一两个附件（录屏通常是最大的那个）再点「重试」。";
  }

  // 其余一律「我也说不清」。`FEEDBACK_UPSTREAM_UNREACHABLE` 也走这里：它已经在
  // `submitViaBackendProxy` 里被标成 `OFFLINE`，widget 自己有更合适的那句话
  // （`SCN-FWB-130` (4)），这里再插一句只会把两处文案分叉。
  return null;
};

/**
 * 只有本仓才知道的领域事实，在**提交那一刻**交出来一次。
 *
 * 只放版本号这一类。**不放路由 params 与 query**——会话 id 就是 wxid / chatroom
 * 标识，搜索词就是用户搜的那句话；把它们塞进 `context` 等于绕过所有文本脱敏另开
 * 一路，而 `context` 是后端原样转发、不解释也不扫描的那一段。页面地址那一项已经
 * 由 widget 自己采集并脱敏（`SCN-FWB-135`），这里不重复。
 *
 * 返回值必须能过 `JSON.stringify`：这里返回的是三个字面量，天然安全。随手把某个
 * store 交出来就会带循环引用，而那会让**每一次**提交都失败。
 */
export const providePlatformFeedbackContext = (): Record<string, unknown> => ({
  app_version: __APP_VERSION__,
  contract_version: __CONTRACT_VERSION__,
});

/**
 * 交给 widget 的静态文案表（宿主选项 `uiCopy`）：模板里抽出来的 + 能力表里的导航名。
 *
 * 两份都是源码里的字面量。有序去重只为让产物确定；护栏（长度、字符类别、条数）在
 * widget 侧。
 */
export const platformFeedbackUiCopy = (): string[] => {
  const navLabels = Object.values(CAPABILITY_MANIFEST).flatMap((definition) =>
    definition.surface?.nav ? [definition.surface.nav.label] : [],
  );
  return [...new Set([...templateUiCopy, ...navLabels])].sort();
};

/** widget 只收这一种形状：`/` 开头，只含 `/`、字母数字、`:`、`-`、`_`、`*`，≤ 120 字。 */
const ROUTE_TEMPLATE_SHAPE = /^\/[A-Za-z0-9/:_*-]*$/;
const ROUTE_TEMPLATE_MAX_LENGTH = 120;

/**
 * 把 vue-router 路由记录的 `path` 归一成 widget 收的路由模板。
 *
 * - 去掉参数后面的自定义正则段：`:id(\d+)` → `:id`、`:pathMatch(.*)*` → `:pathMatch*`
 *   （括号按嵌套深度整段去掉，不认括号里是什么）；
 * - 去掉 `?`、`+` 这两个 widget 字符集外的修饰符，`*` 留着（widget 收它）；
 * - 归一之后仍不合格就返回空串——**绝不回退成真实路径**：真实路径里的会话 id 就是
 *   wxid / chatroom 标识，分享页的 token 就是访问令牌。
 */
export const toRouteTemplate = (path: string): string => {
  let depth = 0;
  let out = "";
  for (const char of path) {
    if (char === "(") {
      depth += 1;
      continue;
    }
    if (char === ")") {
      depth = Math.max(0, depth - 1);
      continue;
    }
    if (depth === 0) out += char;
  }
  // 括号没配平说明这不是一个我们看得懂的路径，整条不要。
  if (depth !== 0) return "";
  const template = out.replace(/[?+]/g, "");
  if (template.length > ROUTE_TEMPLATE_MAX_LENGTH) return "";
  return ROUTE_TEMPLATE_SHAPE.test(template) ? template : "";
};

/**
 * 当前路由的模板：`matched` 最后一条路由**记录**的 `path`（`/s/:token`），不是
 * `currentRoute.path`（`/s/abc123…`）。没有 router、或当前没匹配到记录时返回空串，
 * widget 会丢掉它——空串是「不知道」，不是一个值。
 */
export const currentRouteTemplate = (router: Router | null): string => {
  const matched = router?.currentRoute.value.matched ?? [];
  const last = matched[matched.length - 1];
  return last === undefined ? "" : toRouteTemplate(last.path);
};

/**
 * 当前挂着的那个实例。**全模块唯一的可变状态**，两条路径都必须把它维护对：
 *
 * - 关闭之后不清 ⇒ 反馈入口**关一次就再也点不开**（`SCN-FWB-145` 那条 P0，
 *   2026-09-21 在甘特图上实证过）；
 * - 根本不存 ⇒ 点两下**同时弹出两个对话框**。
 *
 * 两种都不报错、控制台干净，所以它们只能靠这一个变量被维护对。
 */
let mounted: MountedFeedbackWidget | null = null;

/**
 * 挂载是异步的（要先拉配置、再动态 import 那份产物），所以在它落地之前**还有一段
 * `mounted` 仍是 null 的时间窗**。不挡这一段的话，手快点两下就是两个对话框——而
 * `mounted !== null` 这道闸在那一刻恰好是开的。
 */
let opening = false;

/** 测试用：把模块级句柄清干净。产品代码不要调。 */
export const resetPlatformFeedbackForTests = (): void => {
  mounted?.destroy();
  mounted = null;
  opening = false;
};

/** 现在开着吗（测试与自查用）。 */
export const isPlatformFeedbackOpen = (): boolean => mounted !== null;

/**
 * 那份 146 KB 的产物按需取。
 *
 * 动态 `import()` 让 vite 把它切成一个**同源**的独立 chunk：首屏不付这笔字节，
 * 而点开反馈的那一刻从 `'self'` 取——`script-src 'self'` 下合法，离线下也照常
 * （它就在本机的 `dist/` 里，不过网络）。
 *
 * 抽成可替换的函数只为测试：真产物在 happy-dom 里跑不起来（Shadow DOM + rrweb），
 * 而交互判据本来就不该写在 jsdom/happy-dom 里——那一层会对着坏实现点头。
 */
type MountFn = (options: {
  projectId: string;
  configUrl: string;
  submit: (
    payload: FeedbackWidgetPayload,
    options: { signal: AbortSignal | null },
  ) => Promise<FeedbackWidgetReceipt | null | undefined>;
  provideContext: () => Record<string, unknown>;
  describeSubmitError: (error: unknown) => string | null;
  onDestroyed: () => void;
  contactUrl: string;
  fetchImpl?: typeof globalThis.fetch;
  uiCopy: string[];
  routeTemplate: () => string;
}) => Promise<MountedFeedbackWidget>;

let loadMount: () => Promise<MountFn> = async () =>
  (await import("@/vendor/feedback-widget/feedback-widget.js")).mountPlatformFeedbackWidget;

/** 测试用：换掉产物的加载方式。 */
export const setPlatformFeedbackMountForTests = (next: (() => Promise<MountFn>) | null): void => {
  loadMount =
    next ??
    (async () => (await import("@/vendor/feedback-widget/feedback-widget.js")).mountPlatformFeedbackWidget);
};

/**
 * 打开反馈对话框。已经开着（或正在打开）时**什么都不做**。
 *
 * widget 挂载即打开、关闭即自毁，所以这里没有「常驻一个隐藏实例」这回事：
 * 一次点击 = 一次挂载，一次关闭 = 一次销毁 + 一次 `onDestroyed`。
 *
 * **配 `configUrl`**（2026-09-23 起）：指向本仓后端那条同源代理。没有它时 widget
 * 走内置保守兜底值，而那套兜底值里**没有** `proj_wxdump` 的 `rejectPatterns`——
 * 于是页面地址里的 `wxid_…` 原样发出、被平台的脱敏门禁 400 拒掉，用户反复重试也
 * 不会成功（2026-09-22 真机走查实测的那条 P0）。
 *
 * 代理本身拿不到配置时（离线、或者缓存还没暖起来）仍然回落到内置兜底值——按
 * `SCN-FWB-130` (3) 那是这条通道上允许的路径，而且代理**保证不会因此让这个 await
 * 变慢**：它只答缓存、刷新在后台（`backend/src/chat_archive/feedback.py` 的
 * `FeedbackWidgetConfigService`）。这一条不是实现细节而是硬约束——平台的
 * `loadWidgetConfig` 既没有超时也没有 AbortSignal，而下面这行 `await mount(...)`
 * 就挡在对话框渲染之前。
 */
export const openPlatformFeedback = async (
  api: ArchiveApi,
  // 由调用方注入而不是 import `@/router`：那边 import 了全部视图，视图又经 AppShell
  // import 回这里，直接 import 就是一个环。
  router: Router | null = null,
): Promise<void> => {
  if (mounted !== null || opening) return;
  opening = true;
  try {
    const mount = await loadMount();
    mounted = await mount({
      projectId: FEEDBACK_PROJECT_ID,
      // ← 不接这一行，平台按 `projectId` 下发的 `rejectPatterns` 就拿不到，于是
      //   「用这个项目自己的禁发表在发之前洗掉页面地址」整套逻辑空转——从会话页
      //   或搜索页提交的反馈被平台 100% 拒收（2026-09-22 走查实测）。
      configUrl: FEEDBACK_WIDGET_CONFIG_URL,
      // 回执页补联系方式（`SCN-FWB-136`）。不接这一行，回执页照常、只是不问联系方式。
      contactUrl: FEEDBACK_CONTACT_URL,
      // ← 上面两条请求是 widget **自己**发的，不经过 `api`。不接这一行，本机一旦配了
      //   `X-Archive-Token`，配置取数就静默 401 → 退回内置兜底值 → 页面地址里的
      //   `wxid_…` 不再被洗掉 → 提交 100% 被平台拒收；补写联系方式恒 401。两件事都
      //   没有任何报错（见 `ArchiveApi.sameOriginFetch`）。
      ...(api.sameOriginFetch === undefined ? {} : { fetchImpl: api.sameOriginFetch }),
      submit: (payload) => submitViaBackendProxy(api, payload),
      provideContext: providePlatformFeedbackContext,
      // 不接这一行，被平台脱敏门禁拒收时界面只会说「可以点重试」——而重试
      // 永远不会成功（2026-09-22 走查实测）。
      describeSubmitError: describePlatformFeedbackError,
      // 录屏「结构档」的放行来源：构建期抽出来的静态文案表（见文件头）。不接这一行，
      // 回放整屏都是「字字字」——不报错，只会让人以为脱敏坏了。
      uiCopy: platformFeedbackUiCopy(),
      // 每次调用现取：对话框开着时用户可能换了页面。只给模板，不给真实路径。
      routeTemplate: () => currentRouteTemplate(router),
      onDestroyed: () => {
        // ← 不接这一行，入口就只能点一次。`destroy()` 是幂等的，所以这声通知
        //   只会来一次，不会把下一个实例的句柄误清掉。
        mounted = null;
      },
    });
  } finally {
    opening = false;
  }
};

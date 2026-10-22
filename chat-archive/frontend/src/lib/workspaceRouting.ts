/**
 * Workspace routing policy shared by the root guard.
 *
 * Remote read-only routes are independent of local onboarding: a brand-new
 * device with no local archive must still reach `/remote/*`. Local workspace
 * routes stay guarded behind a completed onboarding, and the public start
 * screen forwards to the overview once the local archive is ready.
 *
 * `guardRedirect` 把这条 onboarding 判定和端能力判定合成**一条**，由
 * `router.beforeEach` 独家执行。此前重定向只挂在 `App.vue` 的
 * `watch(() => store.step)` 上——只有 step 变化时才判，直接改地址栏进得去。
 */
import type { AuthState, Deployment } from "@/api/archiveApi";

import { canEnter, type CapabilityStates, fallbackPath } from "./capabilities";

export type WorkspaceRouteKind = "local" | "remote" | "public";

/** The onboarding step at which the local workspace is fully available. */
const LOCAL_READY_STEP = "overview";

/**
 * Decide where a navigation should be redirected, or `null` to allow it.
 *
 * @param onboardingStep current local onboarding step
 * @param targetPath path the router is navigating to
 * @param kind workspace the target route belongs to
 */
export const workspaceRedirect = (
  onboardingStep: string,
  targetPath: string,
  kind: WorkspaceRouteKind,
): string | null => {
  // Remote reads never depend on local onboarding progress.
  if (kind === "remote") return null;

  const localReady = onboardingStep === LOCAL_READY_STEP;

  if (kind === "public") {
    // Once the local archive is ready the start screen forwards to overview.
    if (localReady && (targetPath === "/start" || targetPath === "/")) {
      return "/overview";
    }
    return null;
  }

  // Local workspace routes require a completed onboarding.
  return localReady ? null : "/start";
};

/** 远程端的首次向导。本地端没有这一屏。 */
export const SETUP_PATH = "/setup";
/** 远程端的登录页。本地端没有这一屏。 */
export const LOGIN_PATH = "/login";
/** 远程端日常解锁的锁定页（PSU-2.213）。本地端没有这一屏。 */
export const UNLOCK_PATH = "/unlock";
/** 解锁后没有可回去的页面时，落到会话列表。 */
const UNLOCK_DEFAULT_NEXT = "/remote/conversations";

/**
 * 去锁定页时带上原本要去的远程页面，解锁后回到那里，而不是一律回会话列表。
 * 只带站内 `/remote/` 路径；其余（登录页、向导页、本地路由）不带。
 */
export const unlockLocation = (fullPath: string): string =>
  fullPath.startsWith("/remote/")
    ? `${UNLOCK_PATH}?next=${encodeURIComponent(fullPath)}`
    : UNLOCK_PATH;

/** 锁定页 `next` 参数的取值。不是站内远程路径（被篡改、缺失）就回会话列表。 */
export const unlockNextPath = (value: unknown): string =>
  typeof value === "string" && value.startsWith("/remote/") ? value : UNLOCK_DEFAULT_NEXT;

/** 公开分享页（含说明页）。它们不属于任何登录用户，锁定与否都不能把它们带走。 */
export const isShareWorkspace = (value: unknown): boolean =>
  value === "share" || value === "hint";

/**
 * 认证这一轴，与能力那一轴分开。
 *
 * 两者是**正交**的：能力说的是「这一端结构上有没有这项功能」，认证说的是
 * 「现在这个人能不能用」。把登录页挂到能力清单上会立刻自相矛盾——未认证的
 * 远程端按契约不下发 capabilities，于是登录页自己会被判成 unsupported，
 * 变成一扇进不去的门。
 *
 * 返回 `allow` 放行，`go` 表示应该去 `path`，`elsewhere` 表示这一屏在当前状态下
 * 没有意义、该回到本端自己的地盘（去哪儿由调用方按能力清单决定）。
 */
export type AuthGateDecision =
  | { readonly kind: "allow" }
  | { readonly kind: "go"; readonly path: string }
  | { readonly kind: "elsewhere" };

const ALLOW: AuthGateDecision = { kind: "allow" };

export const authRedirect = (
  authState: AuthState | null,
  targetPath: string,
): AuthGateDecision => {
  // 端还没答复时不抢答：服务连不上不该把用户关在登录页外面。
  if (authState === null) return ALLOW;
  if (authState === "uninitialized") {
    return targetPath === SETUP_PATH ? ALLOW : { kind: "go", path: SETUP_PATH };
  }
  if (authState === "anonymous") {
    return targetPath === LOGIN_PATH ? ALLOW : { kind: "go", path: LOGIN_PATH };
  }
  // 已登录但日常解锁未通过：会话还在，私人接口一律 423，哪儿都只能先去锁定页。
  if (authState === "reauth_required") {
    return targetPath === UNLOCK_PATH ? ALLOW : { kind: "go", path: UNLOCK_PATH };
  }
  // 已登录，或本地端（恒 not_required）：这三屏都不该再出现。
  return targetPath === LOGIN_PATH || targetPath === SETUP_PATH || targetPath === UNLOCK_PATH
    ? { kind: "elsewhere" }
    : ALLOW;
};

export interface GuardInput {
  /** 本地 onboarding 当前步骤。 */
  readonly onboardingStep: string;
  /** 后端下发的端类型；尚未答复时为 null，此时不做能力判定。 */
  readonly deployment: Deployment | null;
  /** 后端下发的认证状态；尚未答复时为 null。 */
  readonly authState: AuthState | null;
  readonly states: CapabilityStates;
  readonly routeName: string;
  readonly targetPath: string;
  readonly workspace: WorkspaceRouteKind;
}

/**
 * 一次导航该被重定向到哪儿，`null` 表示放行。
 *
 * 三关，顺序不能换：
 *
 * 1. **认证**。没登录就哪儿也去不了，能力清单本来也是空的。
 * 2. **端能力**。本端结构上就没有这项功能时，任何入口都不该生效，改地址栏也一样。
 *    拦下之后送去本端第一个看得见的目的地。
 * 3. **本地 onboarding**。
 */
export const guardRedirect = (input: GuardInput): string | null => {
  const { deployment, states, routeName, targetPath } = input;
  // PSU-2.212: old local bookmarks explain the browser hand-off; never render cloud history locally.
  if (deployment === "local" && (input.workspace === "remote" || targetPath === LOGIN_PATH || targetPath === SETUP_PATH || targetPath === UNLOCK_PATH)) return "/remote-web";
  const fallback = (): string | null =>
    deployment === null ? null : fallbackPath(deployment, states);

  const auth = authRedirect(input.authState, targetPath);
  if (auth.kind === "elsewhere") {
    // 已登录还停在登录页/向导页：回本端第一个看得见的目的地。一个都没有
    // （本地端 onboarding 还没跑完）就交给下面那两关，它们会送去 /start。
    const target = fallback();
    if (target !== null && target !== targetPath) return target;
  } else if (auth.kind === "go") {
    return auth.path;
  }

  if (deployment !== null && !canEnter(routeName, deployment, states)) {
    const target = fallback();
    return target !== null && target !== targetPath ? target : null;
  }
  return workspaceRedirect(input.onboardingStep, targetPath, input.workspace);
};

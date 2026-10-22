import { watch } from "vue";
import {
  createRouter,
  createWebHistory,
  START_LOCATION,
  type RouteLocationNormalized,
  type Router,
} from "vue-router";

import type { ArchiveApi } from "@/api/archiveApi";
import { capabilityState } from "@/lib/capabilities";
import {
  guardRedirect,
  isShareWorkspace,
  UNLOCK_PATH,
  unlockLocation,
  type WorkspaceRouteKind,
} from "@/lib/workspaceRouting";
import { useCapabilityStore } from "@/stores/capabilities";
import { useOnboardingStore } from "@/stores/onboarding";

import { routes } from "@workspace-routes";

export const router = createRouter({
  history: createWebHistory(),
  routes: [
    { path: "/", redirect: __APP_TARGET__ === "remote" ? "/remote/conversations" : "/start" },
    ...routes,
    ...(__APP_TARGET__ === "local" ? [
      { path: "/remote/:pathMatch(.*)*", redirect: "/remote-web" },
      { path: "/login", redirect: "/remote-web" },
      { path: "/setup", redirect: "/remote-web" },
      { path: "/unlock", redirect: "/remote-web" },
    ] : []),
    { path: "/:pathMatch(.*)*", redirect: __APP_TARGET__ === "remote" ? "/remote/conversations" : "/start" },
  ],
});

const isShareRoute = (to: RouteLocationNormalized): boolean =>
  isShareWorkspace(to.meta.workspace);

const routeKind = (value: unknown): WorkspaceRouteKind =>
  value === "local" || value === "remote" ? value : "public";

/**
 * 唯一的导航守卫：端能力判定与 onboarding 判定都在这里，改地址栏也绕不过。
 *
 * 判定本身只有一处（`guardRedirect`），触发它的有两个时机：
 *
 * - 每次导航——包括直接改地址栏。此前重定向只挂在 `App.vue` 的
 *   `watch(() => store.step)` 上，只在 step 变化时判，改地址栏进得去。
 * - onboarding 步骤变化——引导跑完时地址栏并没有动，得有人把当前路由重判一遍。
 *   这一条只是「重新问一次」，答案仍由 `guardRedirect` 给。
 *
 * 导航前会先把端问清楚（`GET /api/v1/capabilities`），再按需恢复本地 onboarding。
 * 两件事都只做一次；服务连不上时端类型留 null，守卫放行，由页面自己如实报连不上。
 */
export const installWorkspaceGuard = (target: Router, api: ArchiveApi): void => {
  const decide = (to: RouteLocationNormalized): string | null => {
    const capabilities = useCapabilityStore();
    const onboarding = useOnboardingStore();
    const redirect = guardRedirect({
      onboardingStep: onboarding.step,
      deployment: capabilities.deployment,
      authState: capabilities.authState,
      states: capabilities.states,
      routeName: typeof to.name === "string" ? to.name : "",
      targetPath: to.path,
      workspace: routeKind(to.meta.workspace),
    });
    if (redirect === null || redirect === to.path) return null;
    // 去锁定页时记住原本要去哪儿（PSU-2.213），解锁后回到那里。
    return redirect === UNLOCK_PATH ? unlockLocation(to.fullPath) : redirect;
  };

  target.beforeEach(async (to) => {
    // 打开分享链接的人不是这台服务器的用户：问能力清单只会拿到 anonymous，
    // 然后被送去登录页。这一类路由不进任何一关。
    if (isShareRoute(to)) return true;
    const capabilities = useCapabilityStore();
    const onboarding = useOnboardingStore();
    await capabilities.ensureLoaded(api);
    // 首次引导是「仅本地端」的能力。远程端不该去敲 /api/v1/bootstrap——
    // 那不是它的接口，问了也只会白拿一个错。端还没答复时按本地端处理。
    const onboardingIsHere =
      capabilityState(capabilities.deployment, capabilities.states, "onboarding") !==
      "unsupported";
    if (onboardingIsHere && onboarding.step === "loading") {
      await onboarding.restore(api);
    }
    return decide(to) ?? true;
  });

  watch(
    () => useOnboardingStore().step,
    () => {
      // 首次导航还没落地时 `currentRoute` 仍是 START_LOCATION（路径 "/"）。
      // 那时重判等于拿一个假的当前位置去判，会把正在进行的那次导航顶掉——
      // 首次恢复本就发生在 `beforeEach` 里，它自己会拿到结果。
      const current = target.currentRoute.value;
      if (current === START_LOCATION || isShareRoute(current)) return;
      const next = decide(current);
      if (next !== null) void target.replace(next);
    },
  );
};

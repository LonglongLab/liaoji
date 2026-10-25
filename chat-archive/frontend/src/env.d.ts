/// <reference types="vite/client" />

declare module "*.vue" {
  import type { DefineComponent } from "vue";

  const component: DefineComponent<Record<string, never>, Record<string, never>, unknown>;
  export default component;
}

/**
 * 反馈录屏的静态界面文案表，由 `vite.config.ts` 的 `feedback-ui-copy` 插件在构建期
 * 从 `.vue` 模板里抽出来（`scripts/uiCopy.mjs` 的 `collectUiCopy`），经平台 widget
 * 的宿主选项 `uiCopy` 交出去。
 *
 * 是虚拟模块而不是仓库里的一份 json：落成文件就会有「文件与模板不同步」这种
 * 状态，而不同步的一半后果是新加的界面文案在回放里变成「字字字」——不报错，
 * 只会让人以为脱敏坏了。
 */
declare module "virtual:feedback-ui-copy" {
  const copy: string[];
  export default copy;
}

declare const __APP_VERSION__: string;
declare const __CONTRACT_VERSION__: string;
declare const __CHATLOG_KEEPER__: {
  name: string;
  version: string;
  commit: string;
  license: string;
  retrieved_on: string;
  upstream_url: string;
};
declare const __CHATLOG_KEEPER_LICENSE__: string;
declare const __FRONTEND_DEPENDENCIES__: Array<{ name: string; version: string }>;
declare const __REFERENCE_PROJECTS__: Array<{
  name: string;
  upstream_url: string | null;
  role: string;
  license: string;
  source_path: string | null;
  commit?: string;
  version?: string;
  retrieved_on?: string;
}>;

declare const __APP_TARGET__: "local" | "remote" | "universal";
declare module "@workspace-routes" {
  import type { RouteRecordRaw } from "vue-router";
  export const routes: RouteRecordRaw[];
}

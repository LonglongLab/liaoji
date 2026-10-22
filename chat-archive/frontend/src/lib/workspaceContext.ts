import { inject, type InjectionKey } from "vue";

import type { Workspace } from "@/lib/capabilities";

/**
 * 「这一棵组件树画的是哪一端的档案」（PSU-2.99 批 C）。
 *
 * 两端共用同一批组件（`MessageTypeRenderer` / `MediaViewer` /
 * `MergedForwardItems`），而**极少数几个词**在两端指的不是同一件事。给远程端
 * 另拉一套组件是这一轮问题的根（合同 §12.7.3），逐层往下传一个 prop 又会把
 * 一件页面级的事实塞进每一条消息的接口——所以走 provide / inject。
 *
 * 由**视图**提供，不由 `AppShell` 提供：视图自己的测试常把 AppShell 桩掉，
 * 而这句事实不该因为一个桩就消失。
 */
export const workspaceKey: InjectionKey<Workspace> = Symbol("workspace");

/** 没人 provide 就是本机端——那是这些组件本来的家。 */
export const useWorkspace = (): Workspace => inject(workspaceKey, "local");

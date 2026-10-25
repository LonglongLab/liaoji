import { defineStore } from "pinia";

import {
  type ArchiveApi,
  ArchiveApiError,
  type AuthState,
  type Deployment,
} from "@/api/archiveApi";
import type { CapabilityStates } from "@/lib/capabilities";

type CapabilityReader = Pick<ArchiveApi, "getCapabilities">;

interface CapabilityStoreState {
  /**
   * 后端说的端类型。答复到手前是 null，路由守卫见到 null 就不做能力判定——
   * 服务连不上时不该拿一个猜测把用户关在门外，页面自己会如实报连不上。
   */
  deployment: Deployment | null;
  authState: AuthState | null;
  states: CapabilityStates;
  loaded: boolean;
  error: string | null;
}

// 同样不提「本地」：这条消息出现的时刻，恰恰是**端类型还不知道**的时刻——
// 失败的正是那一次能力清单请求。手机上远程端断网时它照样会显示。
const messageFrom = (error: unknown): string =>
  error instanceof ArchiveApiError
    ? error.message
    : "归档服务暂时没有响应，请检查网络或确认服务正在运行后重试。";

export const useCapabilityStore = defineStore("capabilities", {
  state: (): CapabilityStoreState => ({
    deployment: null,
    authState: null,
    states: {},
    loaded: false,
    error: null,
  }),
  actions: {
    async refresh(api: CapabilityReader): Promise<void> {
      this.loaded = false;
      this.error = null;
      await this.load(api);
    },
    async load(api: CapabilityReader): Promise<void> {
      try {
        const payload = await api.getCapabilities();
        this.deployment = payload.deployment;
        this.authState = payload.auth_state;
        // 未认证的远程端不带 capabilities。缺席就是空表，不是「全都有」。
        this.states = Object.fromEntries(
          (payload.capabilities ?? []).map((entry) => [entry.id, entry.state]),
        );
        this.loaded = true;
        this.error = null;
      } catch (error: unknown) {
        this.error = messageFrom(error);
      }
    },
    /**
     * 取一次能力清单。已经拿到就不再问；失败过则下一次导航会再试一次，
     * 不会因为一次瞬时故障永远停在「端类型未知」。
     */
    async ensureLoaded(api: CapabilityReader): Promise<void> {
      if (this.loaded) return;
      await this.load(api);
    },
  },
});

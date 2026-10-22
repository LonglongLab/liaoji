import { defineStore } from "pinia";

import {
  type ArchiveApi,
  ArchiveApiError,
  type TotpEnrollment,
} from "@/api/archiveApi";
import { persistentAvatarCache } from "@/lib/avatarPersistentCache";
import { closeSharedAvatarStore } from "@/lib/remoteAvatarStore";
import { recordBrowserClientInfo } from "@/lib/browserClientInfo";
import { useCapabilityStore } from "@/stores/capabilities";

/**
 * 首次向导的步骤。
 *
 * `token` 与 `password` 一起提交（服务端也是一次调用），拆成两步只是为了在
 * 手机上一屏一件事；真正的分界在 `totp`——那一步发生在**已经登录之后**，
 * 跳过它也已经算部署完成了。
 */
export type SetupStep = "credentials" | "totp" | "recovery";

export interface AuthFailure {
  readonly code: string;
  readonly message: string;
  /** 退避剩余秒数，来自 `Retry-After`；不在退避中为 null。 */
  readonly retryAfterSeconds: number | null;
}

interface RemoteAuthState {
  step: SetupStep;
  busy: boolean;
  failure: AuthFailure | null;
  enrollment: TotpEnrollment | null;
  recoveryCodes: string[];
}

const failureFrom = (error: unknown): AuthFailure => {
  if (error instanceof ArchiveApiError) {
    return {
      code: error.code,
      message: error.message,
      retryAfterSeconds: error.retryAfterSeconds,
    };
  }
  return {
    code: "SERVICE_UNAVAILABLE",
    message: "远程归档服务暂时没有响应，请稍后重试。",
    retryAfterSeconds: null,
  };
};

export const useRemoteAuthStore = defineStore("remoteAuth", {
  state: (): RemoteAuthState => ({
    step: "credentials",
    busy: false,
    failure: null,
    enrollment: null,
    recoveryCodes: [],
  }),
  actions: {
    reset(): void {
      this.step = "credentials";
      this.busy = false;
      this.failure = null;
      this.enrollment = null;
      this.recoveryCodes = [];
    },

    clearFailure(): void {
      this.failure = null;
    },

    /**
     * 登录/向导成功之后必须重新取一次能力清单：认证状态变了，能力明细也才刚
     * 出现。不重取的话路由守卫还拿着 `anonymous`，会把人原地弹回登录页。
     *
     * 返回服务端**认不认**这条新会话——那是分开的一问：接口回了 200，
     * 浏览器却可能压根没把 Cookie 存下来。
     */
    async _refreshCapabilities(api: ArchiveApi): Promise<boolean> {
      const capabilities = useCapabilityStore();
      capabilities.loaded = false;
      await capabilities.load(api);
      // 开了日常解锁（PSU-2.213）时，刚登录的会话是 `reauth_required`：会话已被认下，
      // 只是还要解锁。不能把它误报成「Cookie 没存下」。
      return (
        capabilities.authState === "authenticated" ||
        capabilities.authState === "reauth_required"
      );
    },

    /**
     * 会话 Cookie 带 `Secure`，明文 HTTP 下浏览器会直接丢掉它。
     *
     * 那时接口回 200、前端以为登录成功，下一次请求却仍然未认证——守卫把人弹回
     * 登录页，界面上一句解释都没有。这是整条链路里最难自查的一种失败，所以在
     * 这里把它说破，而不是留给人对着一个「点了没反应」的登录按钮。
     */
    _cookieRejected(): void {
      this.failure = {
        code: "REMOTE_SESSION_NOT_STORED",
        message:
          "服务器接受了登录，但浏览器没有保存会话。会话 Cookie 带 Secure，" +
          "只在 HTTPS 下才会被保存——请通过 https:// 访问本站再试。",
        retryAfterSeconds: null,
      };
    },

    async completeSetup(
      api: ArchiveApi,
      bootstrapToken: string,
      password: string,
    ): Promise<boolean> {
      if (this.busy) return false;
      this.busy = true;
      this.failure = null;
      try {
        await api.completeBrowserSetup(bootstrapToken.trim(), password);
        if (!(await this._refreshCapabilities(api))) {
          this._cookieRejected();
          return false;
        }
        this.step = "totp";
        void recordBrowserClientInfo(api);
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },

    async signIn(
      api: ArchiveApi,
      password: string,
      secondFactor: string,
    ): Promise<boolean> {
      if (this.busy) return false;
      this.busy = true;
      this.failure = null;
      try {
        await api.browserSignIn(password, secondFactor);
        if (!(await this._refreshCapabilities(api))) {
          this._cookieRejected();
          return false;
        }
        void recordBrowserClientInfo(api);
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },

    async signOut(api: ArchiveApi): Promise<void> {
      if (this.busy) return;
      this.busy = true;
      try {
        await api.browserSignOut();
      } catch {
        // 登出失败也要把本地状态清干净：会话可能本来就已经在服务端失效了，
        // 把人留在一个「看起来还登着」的界面上更糟。
      } finally {
        this.busy = false;
        this.reset();
        // 头像是别人的脸，不该在退出之后还留在这台机器上（PSU-2.122）。
        // 内存里那一份与 Cache Storage 里那一份都清，顺序无所谓：两份都只是缓存。
        closeSharedAvatarStore();
        void persistentAvatarCache().clear();
        await this._refreshCapabilities(api);
      }
    },

    /** 绑定要口令：只凭一条会话就能绑上别人的验证器，会把机主锁在门外。 */
    async beginTotpEnrollment(api: ArchiveApi, password: string): Promise<boolean> {
      if (this.busy) return false;
      this.busy = true;
      this.failure = null;
      try {
        this.enrollment = await api.beginTotpEnrollment(password);
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },

    async enableTotp(api: ArchiveApi, code: string): Promise<boolean> {
      if (this.busy) return false;
      this.busy = true;
      this.failure = null;
      try {
        const enabled = await api.enableTotp(code.trim());
        this.recoveryCodes = [...enabled.recovery_codes];
        this.enrollment = null;
        this.step = "recovery";
        await this._refreshCapabilities(api);
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },

    /** 明确跳过两步验证：不算失败，向导照样是完成的。 */
    skipTotp(): void {
      this.enrollment = null;
      this.step = "recovery";
      this.recoveryCodes = [];
    },
  },
});

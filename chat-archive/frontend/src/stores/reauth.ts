import { defineStore } from "pinia";

import {
  type ArchiveApi,
  ArchiveApiError,
  type PasskeySummary,
  type ReauthState,
} from "@/api/archiveApi";
import { purgePrivateCaches } from "@/lib/privateCaches";
import { announceReauth } from "@/lib/reauthChannel";
import { WebAuthnError, createCredential, getAssertion } from "@/lib/webauthn";
import { useCapabilityStore } from "@/stores/capabilities";

/**
 * PSU-2.221 本机应用锁（前身 PSU-2.213 账户级日常解锁）。
 *
 * 像手机的应用锁：只对这台设备生效，默认关闭。开启后，这台设备每 12 小时用本机的
 * 指纹 / 面容 / Windows Hello 解锁一次；验证不了时用登录口令解锁。其他设备各自开关，
 * 互不影响。改这套设置本身（关锁、重新录入）要求 5 分钟内刚验证过一次——那一次验证
 * 由这里按需调起，界面只管点按钮。
 */
export interface ReauthFailure {
  readonly code: string;
  readonly message: string;
  readonly retryAfterSeconds: number | null;
}

interface ReauthStoreState {
  state: ReauthState | null;
  /** 本机登记的通行密钥（服务端只返回本机这台设备的）。 */
  passkeys: PasskeySummary[];
  passkeysLoaded: boolean;
  busy: boolean;
  failure: ReauthFailure | null;
}

/** 本地时钟与服务端可能差几秒：快到期的 fresh 窗口当作已过期，宁可多验一次。 */
const FRESH_MARGIN_MS = 15_000;

const failureFrom = (error: unknown): ReauthFailure => {
  if (error instanceof WebAuthnError) {
    return { code: `WEBAUTHN_${error.kind.toUpperCase()}`, message: error.message, retryAfterSeconds: null };
  }
  if (error instanceof ArchiveApiError) {
    return { code: error.code, message: error.message, retryAfterSeconds: error.retryAfterSeconds };
  }
  return {
    code: "SERVICE_UNAVAILABLE",
    message: "远程归档服务暂时没有响应，请稍后重试。",
    retryAfterSeconds: null,
  };
};

const isFreshRequired = (error: unknown): boolean =>
  error instanceof ArchiveApiError && error.code === "REMOTE_REAUTH_FRESH_REQUIRED";

const isNotFound = (error: unknown): boolean => error instanceof ArchiveApiError && error.code === "NOT_FOUND";

export const useReauthStore = defineStore("reauth", {
  state: (): ReauthStoreState => ({
    state: null,
    passkeys: [],
    passkeysLoaded: false,
    busy: false,
    failure: null,
  }),
  getters: {
    supported: (store): boolean => store.state?.supported ?? false,
    enabled: (store): boolean => store.state?.enabled ?? false,
  },
  actions: {
    clearFailure(): void {
      this.failure = null;
    },

    async _reloadCapabilities(api: ArchiveApi): Promise<void> {
      const capabilities = useCapabilityStore();
      capabilities.loaded = false;
      await capabilities.load(api);
    },

    /** 用 `fn` 包住一次操作：忙碌标记、失败归档。返回是否成功。 */
    async _run(fn: () => Promise<void>): Promise<boolean> {
      if (this.busy) return false;
      this.busy = true;
      this.failure = null;
      try {
        await fn();
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      } finally {
        this.busy = false;
      }
    },

    async refresh(api: ArchiveApi): Promise<boolean> {
      try {
        this.state = await api.getReauthState();
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      }
    },

    async loadPasskeys(api: ArchiveApi): Promise<boolean> {
      try {
        this.passkeys = [...(await api.listPasskeys()).items];
        this.passkeysLoaded = true;
        return true;
      } catch (error: unknown) {
        this.failure = failureFrom(error);
        return false;
      }
    },

    /** 解锁成功后的共同收尾：能力重载，同一浏览器的其他标签页也离开锁定页。 */
    async _afterUnlock(api: ArchiveApi): Promise<void> {
      await this._reloadCapabilities(api);
      announceReauth("changed");
    },

    /** 锁定页的「用 Windows Hello 解锁」。只能由用户点击触发。 */
    async unlock(api: ArchiveApi): Promise<boolean> {
      return this._run(async () => {
        const challenge = await api.createReauthChallenge("unlock");
        const credential = await getAssertion(challenge.options);
        this.state = await api.verifyReauth(challenge.challenge_id, credential);
        await this._afterUnlock(api);
      });
    },

    /** 锁定页的「使用密码解锁」：指纹验证不了、浏览器不支持时的退路。应用锁保持开启。 */
    async unlockWithPassword(api: ArchiveApi, password: string): Promise<boolean> {
      return this._run(async () => {
        this.state = await api.unlockReauthWithPassword(password);
        await this._afterUnlock(api);
      });
    },

    /** 立即锁定本机：服务端撤销解锁授权，前端清掉已拉到的私人数据。 */
    async lock(api: ArchiveApi): Promise<boolean> {
      return this._run(async () => {
        this.state = await api.lockReauth();
        this.passkeys = [];
        this.passkeysLoaded = false;
        await purgePrivateCaches();
        await this._reloadCapabilities(api);
        announceReauth("locked");
      });
    },

    /** 关掉「旧版应用锁已关闭」提示。失败也先在本页收起，下次打开设置页服务端会再说。 */
    async dismissMigrationNotice(api: ArchiveApi): Promise<void> {
      if (this.state !== null) this.state = { ...this.state, migration_notice: false };
      try {
        await api.dismissReauthNotice();
      } catch {
        // 提示没关掉不影响任何功能。
      }
    },

    /** 管理操作前的系统验证；5 分钟内已验证过就不再弹。 */
    async _ensureFresh(api: ArchiveApi, force = false): Promise<void> {
      if (!force && this.freshNow()) return;
      const challenge = await api.createReauthChallenge("manage");
      const credential = await getAssertion(challenge.options);
      this.state = await api.verifyReauth(challenge.challenge_id, credential);
    },

    /** 先确认新鲜，再做 `fn`；服务端仍说不新鲜（时钟偏差）就强制重验一次。 */
    async _withFresh(api: ArchiveApi, fn: () => Promise<void>): Promise<void> {
      await this._ensureFresh(api);
      try {
        await fn();
      } catch (error: unknown) {
        if (!isFreshRequired(error)) throw error;
        await this._ensureFresh(api, true);
        await fn();
      }
    },

    /** 5 分钟新鲜窗口此刻是否还开着（与 `_ensureFresh` 同一口径）。界面据此决定要不要画「等系统验证」。 */
    freshNow(): boolean {
      const freshUntil = this.state?.fresh_until ?? null;
      return freshUntil !== null && freshUntil * 1000 - FRESH_MARGIN_MS > Date.now();
    },

    /**
     * 在本机登记一枚通行密钥，然后刷新状态与列表。口令总要；登记本身是一次系统验证，
     * 服务端记作新鲜验证，所以「登记 + 开启」只弹一次系统框。
     */
    async _register(api: ArchiveApi, password: string, label: string): Promise<void> {
      const challenge = await api.beginPasskeyRegistration(password);
      const credential = await createCredential(challenge.options);
      await api.finishPasskeyRegistration(challenge.challenge_id, credential, label);
      this.state = await api.getReauthState();
      this.passkeys = [...(await api.listPasskeys()).items];
      this.passkeysLoaded = true;
    },

    /**
     * 开启本机应用锁：口令 → 本机系统验证（登记）→ 开启。登记成功、开启失败时重试会跳过
     * 登记（本机已有一枚），不会再弹一次创建框。
     */
    async enable(api: ArchiveApi, password: string, label: string): Promise<boolean> {
      return this._run(async () => {
        if (this.passkeys.length === 0) await this._register(api, password, label);
        await this._withFresh(api, async () => {
          this.state = await api.putReauthPolicy(true, this.state?.policy_version ?? 0);
        });
        await this._reloadCapabilities(api);
        // 本机旧授权全部失效：开着的其他标签页也回到锁定判断。
        announceReauth("changed");
      });
    },

    /**
     * 重新录入（指纹换了 / 换了 Windows Hello）：口令确认后登记新的一枚，再删掉本机旧的。
     * 系统验证器按本机 user handle 覆盖自己那枚，服务端这边由这里清掉旧记录。
     */
    async reenroll(api: ArchiveApi, password: string, label: string): Promise<boolean> {
      return this._run(async () => {
        const previous = this.passkeysLoaded
          ? this.passkeys.map((passkey) => passkey.id)
          : (await api.listPasskeys()).items.map((passkey) => passkey.id);
        await this._register(api, password, label);
        for (const passkeyId of previous) {
          try {
            await api.deletePasskey(passkeyId);
          } catch (error: unknown) {
            if (!isNotFound(error)) throw error;
          }
        }
        this.passkeys = [...(await api.listPasskeys()).items];
        this.passkeysLoaded = true;
      });
    },

    _afterDisable(): void {
      // 关闭已原子清除本机登记；同步清空缓存，下次开启重新录入。
      this.passkeys = [];
      this.passkeysLoaded = true;
    },

    /** 关闭本机应用锁：5 分钟内验证过就直接生效，否则先做一次本机系统验证。 */
    async disable(api: ArchiveApi): Promise<boolean> {
      return this._run(async () => {
        await this._withFresh(api, async () => {
          this.state = await api.putReauthPolicy(false, this.state?.policy_version ?? 0);
        });
        this._afterDisable();
        await this._reloadCapabilities(api);
        announceReauth("changed");
      });
    },

    /** 关闭时系统验证不了：改用登录口令确认。口令通过同样打开新鲜窗口。 */
    async disableWithPassword(api: ArchiveApi, password: string): Promise<boolean> {
      return this._run(async () => {
        this.state = await api.unlockReauthWithPassword(password);
        this.state = await api.putReauthPolicy(false, this.state.policy_version);
        this._afterDisable();
        await this._reloadCapabilities(api);
        announceReauth("changed");
      });
    },
  },
});

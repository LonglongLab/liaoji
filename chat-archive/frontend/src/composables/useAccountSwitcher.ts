import type { QueryClient } from "@tanstack/vue-query";
import { computed, type ComputedRef, type Ref, watch } from "vue";
import type { RouteLocationNormalizedLoaded, Router } from "vue-router";

import type { ArchiveApi } from "@/api/archiveApi";
import {
  useRemoteAccountScope,
  useRemoteArchiveStatusQuery,
} from "@/composables/remoteArchiveQueries";
import { useLocalAccountSwitch } from "@/composables/useLocalAccountSwitch";
import { useRemoteAvatars } from "@/composables/useRemoteAvatars";
import { capabilityState, type Workspace } from "@/lib/capabilities";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import { useCapabilityStore } from "@/stores/capabilities";
import { useOnboardingStore } from "@/stores/onboarding";
import { useRemoteArchiveUiStore } from "@/stores/remoteArchiveUi";

/** 两端切换面板共用的一行；差别全部折进适配器，模板只有一份。 */
export interface SwitcherEntry {
  readonly id: string;
  readonly label: string;
  readonly subLabel: string | null;
  readonly avatarRef: string | null;
  readonly selected: boolean;
  /** 钉住的默认账号（PSU-2.119）：每次打开都从它开始。 */
  readonly isDefault: boolean;
}

export interface UseAccountSwitcherOptions {
  readonly enabled: boolean;
  readonly workspace: Workspace;
  readonly accountName: Ref<string | undefined>;
  readonly route: RouteLocationNormalizedLoaded | null;
  readonly router: Router | null;
  readonly api: ArchiveApi | null;
  readonly queryClient: QueryClient | null;
  readonly onAfterSwitch: () => void;
  readonly onBeforeAdd: () => void;
}

export interface AccountSwitcher {
  readonly entries: ComputedRef<SwitcherEntry[]>;
  readonly available: ComputedRef<boolean>;
  readonly workspaceAccountId: ComputedRef<string | null>;
  readonly accountLabel: ComputedRef<string | undefined>;
  readonly accountAvatar: ComputedRef<string | null>;
  readonly busy: ComputedRef<boolean>;
  readonly pinnedAccountId: ComputedRef<string | null>;
  readonly pinnedAccountLost: ComputedRef<boolean>;
  readonly pinnedEntry: ComputedRef<SwitcherEntry | null>;
  readonly pinningAvailable: ComputedRef<boolean>;
  readonly errorMessage: ComputedRef<string | null>;
  readonly togglePin: (entryId: string) => void;
  readonly selectEntry: (entryId: string) => void;
  readonly clearError: () => void;
  readonly addAccount: () => void;
}

interface SwitcherBackendAdapter {
  readonly entries: ComputedRef<SwitcherEntry[]>;
  readonly available: ComputedRef<boolean>;
  readonly workspaceAccountId: ComputedRef<string | null>;
  readonly accountLabel: ComputedRef<string | undefined>;
  readonly accountAvatar: ComputedRef<string | null>;
  readonly busy: ComputedRef<boolean>;
  readonly pinnedAccountId: ComputedRef<string | null>;
  readonly pinnedAccountLost: ComputedRef<boolean>;
  readonly errorMessage: ComputedRef<string | null>;
  readonly togglePin: (entryId: string) => void;
  readonly selectEntry: (entryId: string) => Promise<void>;
  readonly clearError: () => void;
  readonly addAccount: () => void;
}

const createInertAdapter = (accountName: Ref<string | undefined>): SwitcherBackendAdapter => ({
  entries: computed(() => []),
  available: computed(() => false),
  workspaceAccountId: computed(() => null),
  accountLabel: computed(() => accountName.value),
  accountAvatar: computed(() => null),
  busy: computed(() => false),
  pinnedAccountId: computed(() => null),
  pinnedAccountLost: computed(() => false),
  errorMessage: computed(() => null),
  togglePin: () => undefined,
  selectEntry: () => Promise.resolve(),
  clearError: () => undefined,
  addAccount: () => undefined,
});

const createLocalAdapter = (options: UseAccountSwitcherOptions): SwitcherBackendAdapter => {
  const onboarding = useOnboardingStore();
  const switchLocalAccount = useLocalAccountSwitch();

  const entries = computed<SwitcherEntry[]>(() =>
    onboarding.accounts.map((account) => ({
      id: account.id,
      label: account.display_name,
      subLabel: account.source_label ?? null,
      avatarRef: account.avatar_ref,
      selected: account.id === onboarding.selectedSourceId,
      isDefault: account.id === onboarding.defaultAccountId,
    })),
  );

  return {
    entries,
    available: computed(() => true),
    workspaceAccountId: computed(() => onboarding.selectedSourceId),
    accountLabel: computed(() => onboarding.selectedAccount?.display_name ?? options.accountName.value),
    accountAvatar: computed(() => onboarding.selectedAccount?.avatar_ref ?? null),
    busy: computed(() => onboarding.accountSwitchBusy),
    pinnedAccountId: computed(() => onboarding.defaultAccountId),
    pinnedAccountLost: computed(() => onboarding.pinnedAccountLost),
    errorMessage: computed(() => onboarding.accountSwitchError?.message ?? null),
    togglePin: (entryId: string): void => {
      const next = onboarding.defaultAccountId === entryId ? null : entryId;
      onboarding.pinDefaultAccount(next);
    },
    selectEntry: async (entryId: string): Promise<void> => {
      if (onboarding.accountSwitchBusy) return;
      if (!(await switchLocalAccount(entryId))) return;
      options.onAfterSwitch();
    },
    clearError: (): void => {
      onboarding.clearAccountSwitchError();
    },
    addAccount: (): void => {
      if (options.router === null) return;
      options.onBeforeAdd();
      onboarding.startAdditionalArchive();
      void options.router.push("/start");
    },
  };
};

const createRemoteAdapter = (options: UseAccountSwitcherOptions): SwitcherBackendAdapter => {
  const api = options.api;
  if (api === null) return createInertAdapter(options.accountName);

  const onboarding = useOnboardingStore();
  const remoteUi = useRemoteArchiveUiStore();
  const capabilities = useCapabilityStore();
  const statusQuery = useRemoteArchiveStatusQuery(api);
  const archiveId = computed(() => statusQuery.data.value?.archive_id ?? null);
  const configured = computed(() => statusQuery.data.value?.configured === true);
  const routeAccountId = computed(() => {
    const value: unknown = options.route?.query.account;
    if (typeof value === "string") return value === "" ? null : value;
    return null;
  });
  const remoteScope = useRemoteAccountScope(api, archiveId, configured, routeAccountId);

  const remoteAttachmentsAvailable = computed(
    () =>
      capabilityState(capabilities.deployment, capabilities.states, "attachment-view") ===
      "available",
  );
  const remoteAvatars = useRemoteAvatars(
    sharedAvatarStoreVia((blobId) => api.getRemoteAttachmentUrl(blobId)),
  );
  const remoteAvatarSrc = (blobId: string | null | undefined): string | null =>
    !remoteAttachmentsAvailable.value ? null : remoteAvatars.srcFor(blobId);

  watch(
    () => ({
      blobIds: remoteScope.accounts.value.map((item) => item.avatar_blob_id),
      available: remoteAttachmentsAvailable.value,
    }),
    ({ blobIds, available }) => {
      remoteAvatars.retain(available ? blobIds : []);
    },
    { immediate: true, deep: true },
  );

  // 名字按归档上报 → 同 id 本地档案 → 不透明标识排列；只读远端自身知道的部分。
  const entries = computed<SwitcherEntry[]>(() =>
    remoteScope.accounts.value.map((item) => {
      const local = onboarding.accounts.find(
        (account) => account.id === item.source_account_id,
      );
      return {
        id: item.source_account_id,
        label: item.display_name ?? local?.display_name ?? item.source_account_id,
        subLabel: `${item.record_count.toLocaleString("zh-CN")} 条消息`,
        avatarRef: remoteAvatarSrc(item.avatar_blob_id) ?? local?.avatar_ref ?? null,
        selected: item.source_account_id === remoteScope.selectedAccountId.value,
        isDefault: item.source_account_id === remoteScope.pinnedAccountId.value,
      };
    }),
  );

  const selectedEntry = computed(
    () => entries.value.find((entry) => entry.selected) ?? null,
  );

  return {
    entries,
    available: computed(() => entries.value.length > 0),
    workspaceAccountId: computed(() => remoteScope.selectedAccountId.value),
    accountLabel: computed(() => selectedEntry.value?.label ?? options.accountName.value),
    accountAvatar: computed(() => selectedEntry.value?.avatarRef ?? null),
    busy: computed(() => false),
    pinnedAccountId: computed(() => remoteScope.pinnedAccountId.value),
    pinnedAccountLost: computed(() => remoteScope.pinnedAccountLost.value),
    errorMessage: computed(() => null),
    togglePin: (entryId: string): void => {
      const next = remoteScope.pinnedAccountId.value === entryId ? null : entryId;
      // 远程端账号为现算值，设默认前先锁定当前视图正在看的账号。
      remoteUi.holdAccount(remoteScope.selectedAccountId.value);
      remoteUi.pinDefaultAccount(next);
    },
    selectEntry: async (entryId: string): Promise<void> => {
      const previousAccountId = remoteScope.selectedAccountId.value;
      if (previousAccountId === entryId) {
        options.onAfterSwitch();
        return;
      }
      remoteUi.selectAccount(entryId);
      if (previousAccountId !== null && options.queryClient !== null) {
        const belongsToPreviousAccount = (queryKey: readonly unknown[]): boolean =>
          queryKey.includes(previousAccountId);
        await options.queryClient.cancelQueries({
          predicate: (query) => belongsToPreviousAccount(query.queryKey),
        });
        options.queryClient.removeQueries({
          predicate: (query) => belongsToPreviousAccount(query.queryKey),
        });
      }
      options.onAfterSwitch();
    },
    clearError: () => undefined,
    addAccount: () => undefined,
  };
};

export const useAccountSwitcher = (options: UseAccountSwitcherOptions): AccountSwitcher => {
  const adapter = !options.enabled
    ? createInertAdapter(options.accountName)
    : options.workspace === "remote"
      ? createRemoteAdapter(options)
      : createLocalAdapter(options);

  const pinnedEntry = computed(
    () => adapter.entries.value.find((entry) => entry.isDefault) ?? null,
  );
  const pinningAvailable = computed(() => adapter.entries.value.length > 1);

  return {
    entries: adapter.entries,
    available: adapter.available,
    workspaceAccountId: adapter.workspaceAccountId,
    accountLabel: adapter.accountLabel,
    accountAvatar: adapter.accountAvatar,
    busy: adapter.busy,
    pinnedAccountId: adapter.pinnedAccountId,
    pinnedAccountLost: adapter.pinnedAccountLost,
    pinnedEntry,
    pinningAvailable,
    errorMessage: adapter.errorMessage,
    togglePin: adapter.togglePin,
    selectEntry: (entryId: string): void => {
      void adapter.selectEntry(entryId);
    },
    clearError: adapter.clearError,
    addAccount: adapter.addAccount,
  };
};

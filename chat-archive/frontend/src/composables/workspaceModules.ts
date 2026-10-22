/** Account and data adapters for the shared statistics/export pages. */
import { useQuery } from "@tanstack/vue-query";
import { computed, inject, ref, shallowRef, watch, type Ref } from "vue";
import { routeLocationKey } from "vue-router";
import { useArchiveApi } from "@/api/injection";
import { useOnboardingStore } from "@/stores/onboarding";
import { useConversationsQuery } from "@/composables/conversationQueries";
import { useRemoteAccountScope, useRemoteConversationsQuery } from "@/composables/remoteArchiveQueries";
import { remoteConversationToRender, remoteAttachmentLoader, remoteThumbnailLoader, remoteAttachmentWithThumbnailFallback, connectRemoteAppCard } from "@/lib/remoteReadModel";
import type { MessageSummary } from "@/api/archiveApi";
import type { RenderConversation } from "@/lib/transcript";
import { useRemoteAvatars } from "@/composables/useRemoteAvatars";
import { sharedAvatarStoreVia } from "@/lib/remoteAvatarStore";
import { useCapabilityStore } from "@/stores/capabilities";
import { capabilityState } from "@/lib/capabilities";

export const useWorkspaceModuleScope = (mode: Ref<"local" | "remote">) => {
  const api = useArchiveApi();
  const local = useOnboardingStore();
  const status = useQuery({ queryKey: ["remote", "status"],
    enabled: computed(() => mode.value === "remote"), queryFn: () => api.getRemoteArchiveStatus() });
  const archiveId = computed(() => status.data.value?.archive_id ?? null);
  const remoteEnabled = computed(() => mode.value === "remote" && status.data.value?.configured === true);
  const route = inject(routeLocationKey, null);
  const preferred = computed(() => {
    const value: unknown = route?.query.account;
    return mode.value === "remote" && typeof value === "string" && value !== "" ? value : null;
  });
  const remote = useRemoteAccountScope(api, archiveId, remoteEnabled, preferred);
  const accountId = computed(() => mode.value === "local" ? local.selectedSourceId : remote.selectedAccountId.value);
  const accountName = computed(() => mode.value === "local" ? local.selectedAccount?.display_name
    : remote.accounts.value.find(item => item.source_account_id === accountId.value)?.display_name ?? undefined);
  const identity = computed(() => `${mode.value}:${mode.value === "remote" ? archiveId.value ?? "" : ""}:${accountId.value ?? ""}`);
  const remoteThumbnail = remoteThumbnailLoader(id => api.getRemoteAttachmentThumbnailUrl(id));
  const remoteOriginal = remoteAttachmentLoader(id => api.getRemoteAttachmentUrl(id));
  const remoteAttachment = remoteAttachmentWithThumbnailFallback(remoteOriginal, remoteThumbnail, () => undefined);
  const loadAttachment = (id: string): Promise<Blob> => mode.value === "remote"
    ? remoteAttachment(id) : api.getAttachmentContent(id, accountId.value ?? "");
  const loadThumbnail = (id: string): Promise<Blob> => mode.value === "remote"
    ? remoteThumbnail(id) : api.getAttachmentThumbnail(id, accountId.value ?? "");
  const canRetry = computed(() => mode.value === "remote" && (status.isError.value || remote.error?.value != null));
  const accountLoading = computed(() => mode.value === "remote" && (status.isFetching.value || (remoteEnabled.value && !remote.settled.value)));
  const accountHint = computed(() => mode.value === "local" ? "请先选择一个已导入的账号。"
    : accountLoading.value ? "正在读取归档账号…" : canRetry.value ? "暂时无法读取归档账号，请重新加载。"
    : !remoteEnabled.value ? "远程归档尚未配置，请先配对一台电脑。" : "远程还没有可用账号，请从已配对的电脑同步记录。");
  const accountGuide = computed(() => accountLoading.value || canRetry.value ? null
    : mode.value === "remote" ? "/remote/pairing" : "/start");
  const retryAccounts = async (): Promise<void> => { await status.refetch(); await remote.reload?.(); };
  const normalizePreview = (items: MessageSummary[]): MessageSummary[] => mode.value === "local" ? items
    : items.map(item => ({...item, app_card: connectRemoteAppCard(item.app_card, item.attachments.map(attachment => attachment.id))}));
  const canOpenFolder = computed(() => mode.value === "local");
  return { api, mode, accountId, accountName, archiveId, remoteEnabled, identity, loadAttachment, loadThumbnail,
    accountHint, accountGuide, canRetry, retryAccounts, canOpenFolder, normalizePreview };
};

export const useExportConversations = (scope: ReturnType<typeof useWorkspaceModuleScope>, search: Ref<string>) => {
  const local = useConversationsQuery(scope.api,
    computed(() => scope.mode.value === "local" ? scope.accountId.value : null), search);
  const remote = useRemoteConversationsQuery(scope.api, scope.archiveId, computed(() => scope.remoteEnabled.value && scope.accountId.value !== null), scope.accountId, search);
  const current = computed(() => scope.mode.value === "local" ? local : remote);
  const visibleIds = ref<readonly string[] | null>([]);
  const rememberedRemote = shallowRef(new Map<string, string | null>());
  const rememberedLocal = shallowRef(new Map<string, string | null>());
  const avatars = useRemoteAvatars(sharedAvatarStoreVia(id => scope.api.getRemoteAttachmentUrl(id)));
  const capabilities = useCapabilityStore();
  const canLoadAvatars = computed(() => scope.mode.value === "remote" && scope.remoteEnabled.value
    && capabilityState(capabilities.deployment, capabilities.states, "attachment-view") === "available");
  const remoteItems = computed(() => remote.data.value?.pages.flatMap(page => page.items) ?? []);
  const localItems = computed(() => local.data.value?.pages.flatMap(page => page.items) ?? []);
  watch(scope.identity, () => {
    rememberedRemote.value = new Map(); rememberedLocal.value = new Map(); visibleIds.value = []; avatars.retain([]);
  }, { flush: "sync" });
  watch(remoteItems, items => {
    const next = new Map(rememberedRemote.value);
    for (const item of items) next.set(item.conversation_id,item.avatar_blob_id ?? null);
    rememberedRemote.value = next;
  }, { immediate: true });
  watch(localItems, items => {
    const next = new Map(rememberedLocal.value);
    for (const item of items) next.set(item.id,item.avatar_ref ?? null);
    rememberedLocal.value = next;
  }, { immediate: true });
  watch([visibleIds, rememberedRemote, canLoadAvatars], () => {
    const ids = visibleIds.value;
    avatars.retain(!canLoadAvatars.value ? [] : ids === null
      ? remoteItems.value.map(item => item.avatar_blob_id)
      : ids.map(id => rememberedRemote.value.get(id)));
  }, { immediate: true });
  const avatarFor = (id: string): string | null => scope.mode.value === "local"
    ? rememberedLocal.value.get(id) ?? null
    : canLoadAvatars.value ? avatars.srcFor(rememberedRemote.value.get(id)) : null;
  const conversations = computed<RenderConversation[]>(() => scope.mode.value === "local"
    ? localItems.value
    : remoteItems.value.map(item => remoteConversationToRender(item, {}, blobId => canLoadAvatars.value ? avatars.srcFor(blobId) : null)));
  return { conversations,
    avatarFor, setVisibleIds: (ids: readonly string[] | null) => { visibleIds.value = ids; },
    isLoading: computed(() => current.value.isLoading.value),
    hasNextPage: computed(() => current.value.hasNextPage.value),
    isFetchingNextPage: computed(() => current.value.isFetchingNextPage.value),
    error: computed(() => current.value.error.value),
    fetchNextPage: () => current.value.fetchNextPage(), refetch: () => current.value.refetch() };
};

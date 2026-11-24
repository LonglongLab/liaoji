<script setup lang="ts">
import { ArrowRight, ChevronRight, Ellipsis, Link2, Plug, Plus, ShieldCheck, SlidersHorizontal, UserRound, X } from "@lucide/vue";
import { computed, nextTick, onBeforeUnmount, ref, watch } from "vue";

import type { AgentAccessGrant, AgentAccessStatus, AgentChatScope } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import AgentConnectionGuide from "@/components/AgentConnectionGuide.vue";
import SearchDirectory from "@/components/SearchDirectory.vue";
import SettingsConfirm from "@/components/SettingsConfirm.vue";
import AppAction from "@/components/ui/AppAction.vue";
import AppSelect from "@/components/ui/AppSelect.vue";
import { type AgentClient, agentClientConfiguration, agentClients, agentTestPrompt } from "@/lib/agentConnection";
import { startClock } from "@/lib/clock";
import { conversationDirectoryEntry } from "@/lib/conversationDirectory";
import type { DirectoryEntry } from "@/lib/searchScopes";
import { copyText as copyToClipboard } from "@/utils/clipboard";

type AccessPanel = "tools" | "scope" | "config" | "settings" | "grant" | "binding";
const MAX_SELECTED_CHATS = 500;

const props = defineProps<{
  local: boolean;
  configured: boolean;
  workspaceAccountId?: string | null | undefined;
  openAccountSwitcher?: (() => void) | undefined;
  consentId?: string | undefined;
  additionTicket?: string | undefined;
}>();
const api = useArchiveApi();
const status = ref<AgentAccessStatus>();
const requester = ref("");
const additions = ref<{ id: string; name: string }[]>([]);
const reviewing = ref(false);
const accounts = ref<{ id: string; name: string }[]>([]);
const chats = ref<DirectoryEntry[]>([]);
const selected = ref<string[]>([]);
const account = ref("");
const mode = ref<AgentChatScope["mode"]>("selected");
const draft = ref<AgentAccessGrant | "new" | null>(null);
const revoke = ref<AgentAccessGrant | null>(null);
const rotate = ref<{ grantId: string; operationId: string } | null>(null);
const conflict = ref<AgentAccessGrant | null>(null);
const search = ref("");
const cursor = ref<string | null>(null);
const statusText = ref("");
const panel = ref<AccessPanel | null>(null);
const accessDialog = ref<{ focus: () => void }>();
const management = ref<HTMLElement>();
const serviceControl = ref<{ focus: () => void }>();
const settingsControl = ref<{ focus: () => void }>();
const connectControl = ref<{ focus: () => void }>();
const scopeEditor = ref<HTMLHeadingElement>();
const chatDirectory = ref<{ focusSearch: () => void; scrollToStart: () => void }>();
const client = ref<AgentClient>("codex");
const guideGrantId = ref<string>();
const menuGrantId = ref<string>();
const guidedDraft = ref(false);
const discardScope = ref(false);
const configurationCopied = ref(false);
const credentialReplaced = ref(false);
const bindingGrant = ref("");
const scopeNames = ref<Record<string, string>>({});
const scopeNameError = ref(false);
const linkCode = ref("");
const busy = ref(false);
const chatsLoading = ref(false);
const problem = ref("");
const chatProblem = ref(false);
const failed = ref(false);
const consentGrant = ref<AgentAccessGrant>();
const workspaceScoped = computed(() => Boolean(props.workspaceAccountId) && !props.consentId);
const preferredAccount = computed(() => props.workspaceAccountId ?? accounts.value[0]?.id ?? "");
const hasPreferredAccount = computed(() => accounts.value.some(item => item.id === preferredAccount.value));
const grants = computed(() => {
  const items = status.value?.grants ?? [];
  return workspaceScoped.value ? items.filter(grant => grant.account_id === props.workspaceAccountId) : items;
});
const editorAccountName = computed(() => accounts.value.find(item => item.id === account.value)?.name ?? "未选择账号");
const grantAccount = (grant: AgentAccessGrant): string =>
  accounts.value.find(item => item.id === grant.account_id)?.name ?? grant.account_id;
const scopeSummary = (grant: AgentAccessGrant): string =>
  grant.scope?.mode === "all" ? "全部聊天" : `${String(grant.scope?.conversation_ids.length ?? 0)} 个指定聊天`;
const guideGrant = computed(() => grants.value.find(grant => grant.id === guideGrantId.value && grant.scope !== null));
const menuGrant = computed(() => grants.value.find(grant => grant.id === menuGrantId.value));
const guideScopeLabel = computed(() => guideGrant.value && `${grantAccount(guideGrant.value)} · ${scopeSummary(guideGrant.value)}`);
const guideStage = computed(() => panel.value === "tools" || panel.value === "scope" || panel.value === "config" ? panel.value : null);
const serviceName = computed(() => props.local ? "本机 MCP" : "远程 MCP");
const panelTitle = computed(() => {
  switch (panel.value) {
    case "tools": return "连接 AI 工具";
    case "scope":
    case "config": return "连接 " + (client.value === "other" ? "通用 MCP" : agentClients[client.value].name);
    case "settings": return serviceName.value + " 设置";
    case "binding": return "关联远程授权";
    case "grant": return "管理聊天授权";
    default: return "";
  }
});
const canSave = computed(() => account.value !== ""
  && (draft.value !== "new" || accounts.value.some(item => item.id === account.value))
  && (mode.value === "all" || selected.value.length > 0));
let chatLoad = 0;
let pendingSave: { signature: string; operationId: string } | undefined;
let scopeLoad = 0;
let disposed = false;
const viewIsActive = (): boolean => !disposed;
let polling = false;
const scopeKey = (accountId: string, chat: string): string => JSON.stringify([accountId, chat]);
const scopeReady = (grant: AgentAccessGrant): boolean => grant.scope?.mode === "all"
  || (grant.scope?.conversation_ids ?? []).every(id => Boolean(scopeNames.value[scopeKey(grant.account_id, id)]));
const scopeItems = (grant: AgentAccessGrant): { id: string; name: string }[] =>
  (grant.scope?.conversation_ids ?? []).map(id => ({ id, name: scopeNames.value[scopeKey(grant.account_id, id)] ?? "名称加载中…" }));
const selectedChats = computed(() => selected.value.map(id => ({
  id, name: scopeNames.value[scopeKey(account.value, id)] ?? "名称未加载",
})));
const guideScopes = computed(() => grants.value.filter(grant => grant.scope !== null).map(grant => ({
  id: grant.id,
  account: grantAccount(grant),
  description: grant.scope?.mode === "all" ? "全部聊天" : scopeItems(grant).map(chat => chat.name).join("、"),
  ready: scopeReady(grant),
})));
const bindTargets = computed(() => (status.value?.grants ?? []).filter(grant =>
  grant.account_id === account.value && (!grant.sync_state || grant.sync_state === "local")));
const bindTarget = computed(() => bindTargets.value.find(grant => grant.id === bindingGrant.value));
const accountOptions = computed(() => accounts.value.map(item => ({ value: item.id, label: item.name })));
const bindOptions = computed(() => bindTargets.value.map(grant => ({ value: grant.id,
  label: grant.scope?.mode === "all" ? "全部聊天" : scopeItems(grant).map(chat => chat.name).join("、") })));
const canBind = computed(() => Boolean(account.value && linkCode.value.trim())
  && (bindTargets.value.length === 0 || Boolean(bindTarget.value && scopeReady(bindTarget.value))));

async function hydrateScopeNames(grants: AgentAccessGrant[]): Promise<void> {
  const generation = ++scopeLoad;
  scopeNameError.value = false;
  const pending = new Map<string, { accountId: string; chat: string }>();
  for (const grant of grants) for (const chat of grant.scope?.conversation_ids ?? []) {
    const key = scopeKey(grant.account_id, chat);
    if (!scopeNames.value[key]) pending.set(key, { accountId: grant.account_id, chat });
  }
  const entries = [...pending.entries()];
  for (let offset = 0; offset < entries.length && !disposed && generation === scopeLoad; offset += 4) {
    await Promise.all(entries.slice(offset, offset + 4).map(async ([key, item]) => {
      try {
        const detail = props.local ? await api.getConversation(item.chat, item.accountId)
          : await api.getRemoteConversation(item.chat, { sourceAccountId: item.accountId });
        if (!disposed && generation === scopeLoad && detail.display_name) scopeNames.value[key] = detail.display_name;
        else if (!disposed && generation === scopeLoad) scopeNameError.value = true;
      } catch { if (!disposed && generation === scopeLoad) scopeNameError.value = true; }
    }));
  }
}

async function refreshStatus(): Promise<void> {
  const next = await api.getAgentAccess();
  if (disposed) return;
  status.value = next;
  await hydrateScopeNames(next.grants);
}

function chooseBindingAccount(): void {
  bindingGrant.value = bindTargets.value.length === 1 ? (bindTargets.value[0]?.id ?? "") : "";
}

function beginBinding(): void {
  account.value = hasPreferredAccount.value ? preferredAccount.value : accounts.value[0]?.id ?? "";
  chooseBindingAccount();
  changePanel("binding");
}

async function copyText(value: string, label: string): Promise<boolean> {
  if (!viewIsActive()) return false;
  const ok = await copyToClipboard(value);
  if (!viewIsActive()) return false;
  if (ok) {
    statusText.value = "已复制" + label;
    return true;
  }
  problem.value = "复制失败。请允许浏览器使用剪贴板后重试。";
  return false;
}

async function setAccess(enabled: boolean): Promise<void> {
  await run(async () => {
    status.value = await api.setAgentAccess(enabled);
    closePanel();
    statusText.value = enabled ? "MCP 已开启" : "MCP 已关闭";
  });
  if (panel.value === null) (status.value?.enabled ? settingsControl.value : serviceControl.value)?.focus();
}

function changePanel(next: AccessPanel | null): void {
  if (panel.value === "binding" && next !== "binding") linkCode.value = "";
  panel.value = next;
  credentialReplaced.value = false;
  problem.value = "";
  statusText.value = "";
  void nextTick(() => accessDialog.value?.focus());
}

function closePanel(): void {
  const wasGuide = guideStage.value !== null;
  changePanel(null);
  guideGrantId.value = undefined;
  configurationCopied.value = false;
  if (wasGuide) {
    void nextTick(() => {
      if (globalThis.document.activeElement === globalThis.document.body) connectControl.value?.focus();
    });
  }
}

function startConnection(grant?: AgentAccessGrant): void {
  configurationCopied.value = false;
  guideGrantId.value = grant?.id;
  changePanel("tools");
}

function selectTool(tool: AgentClient): void {
  client.value = tool;
  if (!props.local || guideGrant.value) changePanel("config");
  else if (!guideScopes.value.length && hasPreferredAccount.value) edit("new", true);
  else changePanel("scope");
}

function chooseGuideScope(id: string): void {
  guideGrantId.value = id;
  changePanel("config");
}

function backGuide(): void {
  configurationCopied.value = false;
  guideGrantId.value = undefined;
  changePanel(props.local && panel.value === "config" && guideScopes.value.length ? "scope" : "tools");
}

function openGrantMenu(grant: AgentAccessGrant): void {
  menuGrantId.value = grant.id;
  changePanel("grant");
}

async function run(action: () => Promise<void>): Promise<void> {
  if (busy.value) return;
  busy.value = true;
  problem.value = "";
  statusText.value = "";
  try {
    await action();
  } catch {
    problem.value = "操作未完成，请重试。";
  } finally {
    busy.value = false;
  }
}

async function load(): Promise<void> {
  if (!props.configured) return;
  failed.value = false;
  await run(async () => {
    try {
      const next = await api.getAgentAccess();
      if (props.consentId) requester.value = (await api.getAgentConsent(props.consentId)).client_id;
      accounts.value = props.local
        ? (await api.bootstrap()).accounts.map(row => ({ id: row.id, name: row.display_name }))
        : (await api.listRemoteArchiveAccounts()).items.map(row => ({
          id: row.source_account_id, name: row.display_name ?? row.source_account_id,
        }));
      status.value = next;
      await hydrateScopeNames(next.grants);
      if (props.additionTicket) {
        const request = await api.getAgentAdditions(props.additionTicket);
        additions.value = await Promise.all(request.conversation_ids.map(async id => {
          const chat = props.local ? await api.getConversation(id, request.grant.account_id)
            : await api.getRemoteConversation(id, { sourceAccountId: request.grant.account_id });
          return { id, name: chat.display_name ?? id };
        }));
        reviewing.value = true;
      }
    } catch (error) {
      failed.value = true;
      throw error;
    }
  });
}

async function listChats(append = false): Promise<void> {
  const id = ++chatLoad;
  if (account.value === "") {
    chats.value = [];
    cursor.value = null;
    chatsLoading.value = false;
    return;
  }
  chatsLoading.value = true;
  chatProblem.value = false;
  if (!append) cursor.value = null;
  const options = { limit: 40, q: search.value.trim(), ...(append && cursor.value ? { cursor: cursor.value } : {}) };
  try {
    const result = props.local ? await api.listConversations(account.value, options)
      : await api.listRemoteConversations({ ...options, sourceAccountId: account.value });
    if (id !== chatLoad) return;
    const rows = result.items.map(item => {
      const id = "id" in item ? item.id : item.conversation_id;
      return conversationDirectoryEntry({
        id,
        display_name: item.display_name ?? id,
        avatar_ref: props.local && "avatar_ref" in item ? (item.avatar_ref ?? null) : null,
      });
    });
    chats.value = append ? [...chats.value, ...rows] : rows;
    cursor.value = result.next_cursor;
    if (!append) chatDirectory.value?.scrollToStart();
  } catch {
    if (id === chatLoad) chatProblem.value = true;
  } finally {
    if (id === chatLoad) chatsLoading.value = false;
  }
}

function setChatQuery(query: string): void {
  search.value = query;
  void listChats();
}

function resetChatDirectory(): void {
  search.value = "";
  chats.value = [];
  void listChats();
}

function toggleChat(id: string): void {
  if (busy.value) return;
  if (selected.value.includes(id)) {
    selected.value = selected.value.filter(chat => chat !== id);
    return;
  }
  const entry = chats.value.find(chat => chat.id === id);
  if (!entry || selected.value.length >= MAX_SELECTED_CHATS) return;
  scopeNames.value[scopeKey(account.value, id)] = entry.name;
  selected.value = [...selected.value, id];
}

function edit(grant: AgentAccessGrant | "new", fromGuide = false): void {
  changePanel(null);
  pendingSave = undefined;
  guidedDraft.value = fromGuide;
  draft.value = grant;
  account.value = grant === "new" ? preferredAccount.value : grant.account_id;
  selected.value = grant === "new" ? [] : [...(grant.scope?.conversation_ids ?? [])];
  mode.value = grant === "new" ? "selected" : grant.scope?.mode ?? "selected";
  resetChatDirectory();
  void nextTick(() => scopeEditor.value?.focus());
}

function leaveScope(): void {
  const returnToGuide = guidedDraft.value;
  guidedDraft.value = false;
  discardScope.value = false;
  draft.value = null;
  pendingSave = undefined;
  if (returnToGuide) changePanel(guideScopes.value.length ? "scope" : "tools");
  else void nextTick(() => management.value?.focus());
}

function cancelScope(): void {
  const original = draft.value;
  if (original && original !== "new" && original.scope
    && (mode.value !== original.scope.mode || mode.value === "selected"
      && (selected.value.length !== original.scope.conversation_ids.length
        || selected.value.some(id => !original.scope?.conversation_ids.includes(id))))) {
    discardScope.value = true;
  } else leaveScope();
}

function changeAccount(): void {
  pendingSave = undefined;
  mode.value = "selected";
  selected.value = [];
  resetChatDirectory();
}

async function finishConsent(grantId: string | null): Promise<void> {
  if (!props.consentId) return;
  const result = await api.completeAgentConsent(props.consentId, grantId);
  globalThis.location.assign(result.redirect_uri);
}

async function save(): Promise<void> {
  const target = draft.value;
  if (target === null || !canSave.value) return;
  const targetAccount = account.value;
  const scope: AgentChatScope = {
    mode: mode.value, conversation_ids: mode.value === "all" ? [] : [...selected.value],
  };
  const signature = JSON.stringify([targetAccount, scope]);
  if (pendingSave?.signature !== signature) {
    pendingSave = { signature, operationId: globalThis.crypto.randomUUID() };
  }
  const operation = pendingSave.operationId;
  await run(async () => {
    const result = target === "new"
      ? await api.createAgentGrant(targetAccount, scope, operation)
      : await api.updateAgentGrant(target.id, target.version, operation, scope);
    if (draft.value !== target || account.value !== targetAccount) {
      await refreshStatus();
      return;
    }
    pendingSave = undefined;
    draft.value = null;
    if (props.consentId) {
      consentGrant.value = result;
      await finishConsent(result.id);
      return;
    }
    if (guidedDraft.value) {
      if (status.value && !grants.value.some(grant => grant.id === result.id)) {
        status.value = { ...status.value, grants: [...grants.value, result] };
      }
      chooseGuideScope(result.id);
      guidedDraft.value = false;
    } else void nextTick(() => management.value?.focus());
    await refreshStatus();
    if (panel.value !== "config") statusText.value = result.sync_state === "pending" ? "待同步 · 本机查询已暂停" : "已保存";
  });
}

async function revokeGrant(): Promise<void> {
  const target = revoke.value;
  if (!target) return;
  await run(async () => {
    const result = await api.updateAgentGrant(target.id, target.version, globalThis.crypto.randomUUID(), null);
    revoke.value = null;
    closePanel();
    await refreshStatus();
    statusText.value = result.sync_state === "pending" ? "待同步 · 本机查询已暂停" : "已撤销授权";
    await nextTick();
    management.value?.focus();
  });
}

async function copyCredential(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    const issued = await api.issueAgentCredential(grant.id);
    if (panel.value !== "config" || guideGrant.value?.id !== grant.id) return;
    const configuration = agentClientConfiguration(client.value, issued.address, { grantId: grant.id, token: issued.token });
    if (await copyText(configuration, client.value === "claude" ? "接入命令" : "连接配置")) {
      configurationCopied.value = true;
    }
  });
}

function openCredentialReset(grant: AgentAccessGrant): void {
  rotate.value = { grantId: grant.id, operationId: globalThis.crypto.randomUUID() };
}

async function resetConnectionCredential(): Promise<void> {
  const request = rotate.value;
  if (!request) return;
  await run(async () => {
    await api.issueAgentCredential(request.grantId, request.operationId);
    if (rotate.value === request) {
      rotate.value = null;
      credentialReplaced.value = true;
    }
  });
}

async function copyConnectionConfiguration(): Promise<void> {
  const current = status.value;
  if (!current?.enabled) return;
  if (!props.local) {
    await run(async () => {
      if (await copyText(agentClientConfiguration(client.value, current.address), client.value === "claude" ? "接入命令" : "连接配置")) {
        configurationCopied.value = true;
      }
    });
    return;
  }
  const grant = guideGrant.value;
  if (!grant) return;
  await copyCredential(grant);
}

async function copyConnectionAddress(): Promise<void> {
  const address = status.value?.address;
  if (!address) return;
  await run(async () => {
    if (await copyText(address, "地址") && client.value === "other") configurationCopied.value = true;
  });
}

async function copyTestPrompt(): Promise<void> {
  await run(async () => { await copyText(agentTestPrompt, "测试指令"); });
}

async function copyLink(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    const link = await api.offerAgentLink(grant.id, grant.account_id);
    if (await copyText(link.code, "关联码")) {
      closePanel();
      statusText.value = "已复制关联码";
    }
  });
}

async function bind(): Promise<void> {
  await run(async () => {
    if (!canBind.value) return;
    const target = bindTarget.value;
    await api.bindAgentLink(account.value, linkCode.value.trim(), target?.id, target?.version);
    closePanel();
    await refreshStatus();
    statusText.value = "已关联远程授权";
  });
}

async function retry(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    const result = await api.retryAgentLink(grant.id);
    await refreshStatus();
    statusText.value = result.sync_state === "pending" ? "仍待同步" : "已同步";
  });
}

async function useCurrentRange(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    await api.discardAgentLinkEdit(grant.id);
    conflict.value = null;
    await refreshStatus();
    statusText.value = "已采用远端范围";
  });
}

const syncLabels = {
  local: "仅本机",
  shared: "两端共用",
  pending: "待同步 · 本机查询已暂停",
  conflict: "范围已在另一端更新",
  paused: "共享查询已暂停",
};

async function resolveAddition(allow: boolean): Promise<void> {
  const ticket = props.additionTicket;
  if (!ticket) return;
  await run(async () => {
    const result = await api.resolveAgentAdditions(ticket, allow);
    reviewing.value = false;
    await refreshStatus();
    statusText.value = result.sync_state === "pending" ? "待同步"
      : allow ? "已添加聊天" : "未添加聊天";
  });
}

watch(() => [props.configured, props.consentId, props.additionTicket], () => { void load(); }, { immediate: true });
watch(() => props.workspaceAccountId, () => {
  if (props.consentId || props.additionTicket) return;
  closePanel();
  revoke.value = null;
  rotate.value = null;
  conflict.value = null;
  discardScope.value = false;
  if (draft.value === "new" && !busy.value) {
    account.value = preferredAccount.value;
    changeAccount();
  } else if (draft.value !== null) {
    guidedDraft.value = false;
    draft.value = null;
    pendingSave = undefined;
  }
});
watch(draft, value => {
  if (value === null) chatLoad++;
});
watch(() => status.value?.enabled, enabled => {
  if (enabled === false && guideStage.value) closePanel();
});
startClock(() => {
  if (disposed || polling || busy.value || draft.value !== null || panel.value === "binding" || !props.configured
    || !status.value || globalThis.document.visibilityState === "hidden") return;
  polling = true;
  void refreshStatus().catch(() => { /* Keep existing state; the server still enforces the policy. */ })
    .finally(() => { polling = false; });
}, 5000);
onBeforeUnmount(() => {
  disposed = true;
  scopeLoad++;
  chatLoad++;
});
</script>

<template>
  <section
    class="stack"
    aria-label="授权管理"
  >
    <div
      v-if="!consentId && !reviewing && draft === null && !failed && (status || !configured)"
      class="agent-service"
    >
      <span class="agent-service-icon"><Plug
        :size="18"
        aria-hidden="true"
      /></span>
      <div class="agent-service-meta">
        <h2>{{ serviceName }}</h2>
        <span
          class="agent-service-status"
          :class="{ 'is-on': status?.enabled }"
        >{{ !configured ? "待配置" : status?.enabled ? "已开启" : "已关闭" }}</span>
      </div>
      <AppAction
        v-if="!configured"
        to="/remote/settings#devices"
        variant="secondary"
      >
        打开设置
      </AppAction>
      <AppAction
        v-else-if="!status?.enabled"
        ref="serviceControl"
        type="button"
        variant="primary"
        class="button primary"
        :disabled="busy"
        @click="setAccess(true)"
      >
        开启 MCP
      </AppAction>
      <AppAction
        v-if="configured && (status?.enabled || local && accounts.length)"
        ref="settingsControl"
        type="button"
        variant="quiet"
        class="agent-quiet"
        :class="{ 'agent-icon-action': !status?.enabled }"
        :disabled="busy"
        aria-label="设置"
        @click="changePanel('settings')"
      >
        <SlidersHorizontal
          :size="17"
          aria-hidden="true"
        />
        <span v-if="status?.enabled">设置</span>
      </AppAction>
    </div>
    <p
      v-if="problem && draft === null && panel === null && !rotate && !revoke && !conflict"
      class="notice-warn"
      role="alert"
    >
      {{ problem }}
    </p>
    <p
      v-if="statusText && panel === null"
      role="status"
    >
      {{ statusText }}
    </p>
    <AppAction
      v-if="failed"
      type="button"
      variant="secondary"
      class="button"
      :disabled="busy"
      @click="load"
    >
      重新加载
    </AppAction>
    <p
      v-else-if="configured && !status"
      role="status"
    >
      加载中…
    </p>
    <template v-else-if="configured && status">
      <h2 v-if="requester">
        允许 {{ requester }} 查看
      </h2>

      <form
        v-if="reviewing"
        class="stack"
        @submit.prevent="resolveAddition(true)"
      >
        <h2>新增聊天</h2>
        <ul v-if="additions.length">
          <li
            v-for="item in additions"
            :key="item.id"
            data-feedback-mask="conversation-name"
          >
            {{ item.name }}
          </li>
        </ul>
        <p v-else>
          没有待新增的聊天。
        </p>
        <div class="agent-controls">
          <AppAction
            type="button"
            variant="secondary"
            class="button"
            :disabled="busy"
            @click="resolveAddition(false)"
          >
            {{ additions.length ? "暂不加入" : "完成" }}
          </AppAction>
          <AppAction
            v-if="additions.length"
            type="submit"
            variant="primary"
            class="button primary"
            :disabled="busy"
          >
            允许加入
          </AppAction>
        </div>
      </form>
      <form
        v-else-if="draft !== null"
        class="stack agent-scope-editor"
        @submit.prevent="save"
      >
        <h2
          ref="scopeEditor"
          tabindex="-1"
        >
          {{ draft !== "new" ? "调整范围" : guidedDraft || consentId ? "选择可看的聊天" : "新建聊天授权" }}
        </h2>
        <div class="agent-editor-account">
          <span class="agent-grant-icon"><UserRound
            :size="18"
            aria-hidden="true"
          /></span>
          <span>账号</span>
          <AppSelect
            v-if="draft === 'new' && !workspaceScoped && accounts.length > 1"
            v-model="account"
            :options="accountOptions"
            aria-label="账号"
            data-feedback-mask="account-name"
            sensitive
            :disabled="busy"
            @change="changeAccount"
          />
          <template v-else>
            <span
              class="agent-editor-account-name"
              data-feedback-mask="account-name"
            >{{ editorAccountName }}</span>
            <AppAction
              v-if="workspaceScoped && openAccountSwitcher"
              :disabled="busy"
              @click="openAccountSwitcher"
            >
              切换账号
            </AppAction>
          </template>
        </div>
        <fieldset
          class="agent-scope-choices"
          :disabled="busy"
        >
          <legend class="agent-visually-hidden">
            可看的聊天
          </legend>
          <label>
            <input
              v-model="mode"
              type="radio"
              value="selected"
            >
            指定聊天
          </label>
          <label>
            <input
              v-model="mode"
              type="radio"
              value="all"
            >
            全部聊天
          </label>
        </fieldset>
        <template v-if="mode === 'selected'">
          <p class="agent-scope-note">
            包含所选聊天的历史和后续消息。
          </p>
          <SearchDirectory
            :key="account"
            ref="chatDirectory"
            class="agent-chat-directory"
            title="授权聊天选择"
            placeholder="搜索联系人或群聊"
            selection-layout="inline"
            :entries="chats"
            :selected-ids="selected"
            :query="search"
            :loading="chatsLoading && cursor === null && chats.length === 0"
            :refreshing="chatsLoading && cursor === null && chats.length > 0"
            :fetching-more="chatsLoading && cursor !== null"
            :has-more="cursor !== null"
            :disabled="busy"
            :error-text="chatProblem ? '聊天加载失败' : null"
            :max-selected="MAX_SELECTED_CHATS"
            @toggle="toggleChat"
            @update:query="setChatQuery"
            @load-more="listChats(true)"
          >
            <template #error-actions>
              <AppAction
                :disabled="busy"
                @click="listChats(cursor !== null)"
              >
                重试加载聊天
              </AppAction>
            </template>
          </SearchDirectory>
          <p
            v-if="selected.length >= MAX_SELECTED_CHATS"
            class="agent-scope-note"
            role="status"
          >
            最多选择 {{ MAX_SELECTED_CHATS }} 个聊天。
          </p>
          <div class="agent-selected">
            <div class="agent-selected-heading">
              <span role="status">已选 {{ selected.length }}</span>
              <AppAction
                v-if="selected.length"
                type="button"
                variant="quiet"
                class="agent-quiet"
                :disabled="busy"
                @click="selected = []; chatDirectory?.focusSearch()"
              >
                清空
              </AppAction>
            </div>
            <ul
              v-if="selectedChats.length"
              class="agent-selected-chats"
              aria-label="已选聊天"
              data-feedback-mask="conversation-name"
            >
              <li
                v-for="chat in selectedChats"
                :key="chat.id"
              >
                <span>{{ chat.name }}</span>
                <AppAction
                  type="button"
                  variant="quiet"
                  class="agent-quiet agent-icon-action"
                  :aria-label="'移除 ' + chat.name"
                  :disabled="busy"
                  @click="toggleChat(chat.id); chatDirectory?.focusSearch()"
                >
                  <X
                    :size="16"
                    aria-hidden="true"
                  />
                </AppAction>
              </li>
            </ul>
          </div>
        </template>
        <div
          v-else
          class="agent-all-scope"
        >
          <strong>当前账号的全部聊天</strong>
          <p>包含历史、后续消息和未来新增聊天。</p>
        </div>
        <p
          v-if="draft === 'new'"
          class="agent-scope-note"
        >
          内容传给 AI 后无法收回。
        </p>
        <footer class="agent-editor-footer">
          <p
            v-if="problem"
            class="notice-warn"
            role="alert"
          >
            {{ problem }}
          </p>
          <div class="agent-controls">
            <AppAction
              type="button"
              variant="secondary"
              class="button"
              :disabled="busy"
              @click="cancelScope"
            >
              {{ guidedDraft ? "返回" : "取消" }}
            </AppAction>
            <AppAction
              type="submit"
              variant="primary"
              class="button primary"
              :disabled="busy || !canSave"
            >
              {{ busy ? "保存中…" : consentId ? "允许" : guidedDraft ? "保存并继续" : "保存" }}
            </AppAction>
          </div>
        </footer>
      </form>

      <div
        v-else
        class="agent-authorizations"
      >
        <div
          v-if="!consentId"
          class="agent-list-heading"
        >
          <h2
            ref="management"
            tabindex="-1"
          >
            聊天授权 <span v-if="grants.length">{{ grants.length }}</span>
          </h2>
          <div
            v-if="grants.length"
            class="agent-controls agent-heading-actions"
          >
            <AppAction
              v-if="hasPreferredAccount"
              type="button"
              variant="quiet"
              class="agent-quiet"
              :disabled="busy"
              @click="edit('new')"
            >
              <Plus
                :size="17"
                aria-hidden="true"
              />新建授权
            </AppAction>
            <AppAction
              v-if="status.enabled"
              ref="connectControl"
              type="button"
              variant="primary"
              class="button primary agent-labeled"
              :disabled="busy"
              @click="startConnection()"
            >
              连接 AI 工具<ArrowRight
                :size="17"
                aria-hidden="true"
              />
            </AppAction>
          </div>
        </div>
        <div
          v-if="!grants.length && !consentId"
          class="agent-empty"
        >
          <span class="agent-empty-icon"><ShieldCheck
            :size="25"
            aria-hidden="true"
          /></span>
          <h3>{{ local && !accounts.length ? "还没有可授权的聊天" : "还没有聊天授权" }}</h3>
          <p v-if="local && !accounts.length">
            先导入聊天记录。
          </p>
          <p v-else>
            {{ status.enabled ? "为你的 AI 工具选择可以查看的聊天。" : "开启 MCP 后可以连接 AI 工具。" }}
          </p>
          <div class="agent-controls">
            <AppAction
              v-if="local && !accounts.length"
              to="/start"
              variant="secondary"
            >
              导入聊天
            </AppAction>
            <AppAction
              v-else-if="status.enabled"
              ref="connectControl"
              type="button"
              variant="primary"
              class="button primary agent-labeled"
              :disabled="busy"
              @click="startConnection()"
            >
              连接 AI 工具<ArrowRight
                :size="17"
                aria-hidden="true"
              />
            </AppAction>
            <AppAction
              v-if="hasPreferredAccount"
              type="button"
              variant="quiet"
              class="agent-quiet"
              :disabled="busy"
              @click="edit('new')"
            >
              <Plus
                :size="17"
                aria-hidden="true"
              />新建授权
            </AppAction>
          </div>
        </div>
        <article
          v-for="grant in grants"
          :key="grant.id"
          class="agent-grant"
        >
          <header class="agent-grant-heading">
            <span class="agent-grant-icon"><UserRound
              :size="18"
              aria-hidden="true"
            /></span>
            <div class="agent-grant-title">
              <strong data-feedback-mask="account-name">{{ grantAccount(grant) }}</strong>
              <p>{{ scopeSummary(grant) }}{{ grant.sync_state === "shared" ? " · 两端共用" : "" }}</p>
            </div>
            <template v-if="!consentId">
              <AppAction
                type="button"
                variant="quiet"
                class="agent-quiet agent-edit"
                :disabled="busy"
                @click="edit(grant)"
              >
                调整范围
              </AppAction>
              <AppAction
                type="button"
                variant="quiet"
                class="agent-quiet agent-icon-action"
                aria-label="更多授权操作"
                :disabled="busy"
                @click="openGrantMenu(grant)"
              >
                <Ellipsis
                  :size="18"
                  aria-hidden="true"
                />
              </AppAction>
            </template>
          </header>
          <ul
            v-if="grant.scope?.mode === 'selected'"
            class="agent-scope-list"
            aria-label="可看的聊天"
            data-feedback-mask="conversation-name"
          >
            <li
              v-for="chat in scopeItems(grant)"
              :key="chat.id"
            >
              {{ chat.name }}
            </li>
          </ul>
          <AppAction
            v-if="scopeNameError && !scopeReady(grant)"
            type="button"
            variant="quiet"
            class="agent-quiet"
            :disabled="busy"
            @click="run(() => hydrateScopeNames(grants))"
          >
            重试范围
          </AppAction>
          <div
            v-if="grant.sync_state && grant.sync_state !== 'shared' && grant.sync_state !== 'local'"
            class="agent-grant-state"
          >
            <span>{{ syncLabels[grant.sync_state] }}</span>
            <AppAction
              v-if="!consentId && grant.sync_state === 'pending'"
              type="button"
              variant="quiet"
              class="agent-quiet"
              :disabled="busy"
              @click="retry(grant)"
            >
              同步
            </AppAction>
            <AppAction
              v-if="!consentId && grant.sync_state === 'conflict'"
              type="button"
              variant="quiet"
              class="agent-quiet"
              :disabled="busy"
              @click="conflict = grant"
            >
              采用远端范围
            </AppAction>
          </div>
          <AppAction
            v-if="consentId"
            type="button"
            variant="primary"
            class="button primary"
            :disabled="busy || !scopeReady(grant)"
            @click="run(() => finishConsent(grant.id))"
          >
            使用此范围连接
          </AppAction>
        </article>
        <AppAction
          v-if="consentId && accounts.length && !consentGrant"
          type="button"
          variant="secondary"
          class="button"
          :disabled="busy"
          @click="edit('new')"
        >
          新建授权
        </AppAction>
        <AppAction
          v-if="consentGrant"
          type="button"
          variant="secondary"
          class="button"
          :disabled="busy"
          @click="run(() => finishConsent(consentGrant?.id ?? null))"
        >
          完成连接
        </AppAction>
        <AppAction
          v-if="consentId"
          type="button"
          variant="secondary"
          class="button"
          :disabled="busy"
          @click="run(() => finishConsent(null))"
        >
          暂不允许
        </AppAction>
      </div>
      <SettingsConfirm
        v-if="panel !== null"
        ref="accessDialog"
        :title="panelTitle"
        :busy="busy"
        sheet
        @close="closePanel"
      >
        <AgentConnectionGuide
          v-if="guideStage !== null"
          :client="client"
          :stage="guideStage"
          :local="local"
          :busy="busy"
          :address="status.address"
          :has-accounts="hasPreferredAccount"
          :scopes="guideScopes"
          :scope-label="guideScopeLabel"
          :copied="configurationCopied"
          :receipt="statusText"
          @select-tool="selectTool"
          @use-scope="chooseGuideScope"
          @back="backGuide"
          @choose-scope="edit('new', true)"
          @copy-configuration="copyConnectionConfiguration"
          @copy-address="copyConnectionAddress"
          @copy-prompt="copyTestPrompt"
        />
        <AppAction
          v-if="guideStage === 'scope' && scopeNameError"
          type="button"
          variant="quiet"
          class="agent-quiet"
          :disabled="busy"
          @click="run(() => hydrateScopeNames(grants))"
        >
          重试范围
        </AppAction>
        <div
          v-if="panel === 'settings'"
          class="agent-panel-options"
        >
          <button
            v-if="local && accounts.length"
            type="button"
            class="agent-panel-option"
            :disabled="busy"
            @click="beginBinding"
          >
            <span class="agent-option-icon"><Link2
              :size="17"
              aria-hidden="true"
            /></span>
            <strong>关联远程授权</strong>
            <ChevronRight
              class="agent-option-chevron"
              :size="16"
              aria-hidden="true"
            />
          </button>
          <button
            v-if="status.enabled"
            type="button"
            class="agent-panel-option"
            :disabled="busy"
            @click="setAccess(false)"
          >
            <span class="agent-option-icon"><Plug
              :size="17"
              aria-hidden="true"
            /></span>
            <span><strong>关闭 MCP</strong><small>暂停 AI 访问，保留已有授权</small></span>
            <ChevronRight
              class="agent-option-chevron"
              :size="16"
              aria-hidden="true"
            />
          </button>
        </div>
        <div
          v-else-if="panel === 'grant' && menuGrant"
          class="agent-grant-panel"
        >
          <div class="agent-grant-panel-scope">
            <ShieldCheck
              :size="20"
              aria-hidden="true"
            />
            <div>
              <strong data-feedback-mask="account-name">{{ grantAccount(menuGrant) }}</strong>
              <small>{{ menuGrant.scope ? "可查看 " + scopeSummary(menuGrant) : "未授权聊天" }}</small>
            </div>
          </div>
          <p
            v-if="credentialReplaced"
            class="agent-grant-receipt"
            role="status"
          >
            {{ status.enabled ? "已更换连接密钥" : "已更换连接密钥。开启 MCP 后可复制新配置。" }}
          </p>
          <AppAction
            v-if="local && status.enabled && menuGrant.scope"
            variant="primary"
            :disabled="busy || !scopeReady(menuGrant)"
            @click="startConnection(menuGrant)"
          >
            {{ credentialReplaced ? "复制新配置" : "连接 AI 工具" }}
            <ArrowRight
              :size="17"
              aria-hidden="true"
            />
          </AppAction>
          <AppAction
            v-if="!local"
            variant="secondary"
            :disabled="busy"
            @click="copyLink(menuGrant)"
          >
            复制本机关联码
          </AppAction>
          <div class="agent-grant-panel-controls">
            <AppAction
              v-if="local && menuGrant.scope"
              :disabled="busy"
              @click="openCredentialReset(menuGrant)"
            >
              更换连接密钥
            </AppAction>
            <AppAction
              class="danger"
              :disabled="busy"
              @click="revoke = menuGrant"
            >
              撤销 AI 访问
            </AppAction>
          </div>
        </div>
        <form
          v-else-if="panel === 'binding'"
          class="stack agent-binding"
          @submit.prevent="bind"
        >
          <p>在远程档案的聊天授权中复制本机关联码。</p>
          <label>本机账号
            <AppSelect
              v-model="account"
              :options="accountOptions"
              aria-label="本机账号"
              data-feedback-mask="account-name"
              sensitive
              append-to="self"
              :disabled="busy"
              @change="chooseBindingAccount"
            />
          </label>
          <label v-if="bindTargets.length">关联这个本机授权
            <AppSelect
              v-model="bindingGrant"
              :options="bindOptions"
              aria-label="关联这个本机授权"
              data-feedback-mask="conversation-name"
              sensitive
              append-to="self"
              :disabled="busy"
            />
          </label>
          <small v-if="bindTarget">原连接一并关联，范围沿用远端。</small>
          <label>远程关联码 <input
            v-model="linkCode"
            autocomplete="off"
            type="password"
          ></label>
          <div class="agent-controls agent-binding-actions">
            <AppAction
              type="button"
              variant="quiet"
              class="agent-quiet"
              :disabled="busy"
              @click="changePanel('settings')"
            >
              返回
            </AppAction>
            <AppAction
              type="submit"
              variant="primary"
              class="button primary"
              :disabled="busy || !canBind"
            >
              关联
            </AppAction>
          </div>
        </form>
        <p
          v-if="problem && !rotate && !revoke"
          class="notice-warn agent-panel-error"
          role="alert"
        >
          {{ problem }}
        </p>
      </SettingsConfirm>
      <SettingsConfirm
        v-if="discardScope"
        title="放弃这次更改？"
        @close="discardScope = false"
      >
        <template #actions>
          <AppAction
            type="button"
            variant="secondary"
            class="button"
            @click="discardScope = false"
          >
            继续编辑
          </AppAction>
          <AppAction
            type="button"
            variant="primary"
            class="button primary"
            @click="leaveScope"
          >
            放弃更改
          </AppAction>
        </template>
      </SettingsConfirm>
      <SettingsConfirm
        v-if="conflict"
        title="采用远端范围？"
        :busy="busy"
        @close="conflict = null"
      >
        <p>待同步的本机更改将被放弃。</p>
        <p
          v-if="problem"
          class="notice-warn"
          role="alert"
        >
          {{ problem }}
        </p>
        <template #actions>
          <AppAction
            type="button"
            variant="secondary"
            class="button"
            :disabled="busy"
            @click="conflict = null"
          >
            取消
          </AppAction>
          <AppAction
            type="button"
            variant="primary"
            class="button primary"
            :disabled="busy"
            @click="useCurrentRange(conflict)"
          >
            采用远端范围
          </AppAction>
        </template>
      </SettingsConfirm>
      <SettingsConfirm
        v-if="rotate"
        title="更换连接密钥？"
        :busy="busy"
        @close="rotate = null"
      >
        <p>旧配置会立即失效，已连接的工具需要重新配置。</p>
        <p
          v-if="problem"
          class="notice-warn"
          role="alert"
        >
          {{ problem }}
        </p>
        <template #actions>
          <AppAction
            :disabled="busy"
            @click="rotate = null"
          >
            取消
          </AppAction>
          <AppAction
            variant="secondary"
            class="danger"
            :disabled="busy"
            @click="resetConnectionCredential"
          >
            确认更换
          </AppAction>
        </template>
      </SettingsConfirm>
      <SettingsConfirm
        v-if="revoke"
        title="撤销 AI 访问？"
        :busy="busy"
        @close="revoke = null"
      >
        <p>{{ revoke.sync_state && revoke.sync_state !== "local" ? "相关的本机和远端连接将停止访问。" : "使用此授权的 AI 工具将停止访问。" }}已传出的内容无法收回。</p>
        <p
          v-if="problem"
          class="notice-warn"
          role="alert"
        >
          {{ problem }}
        </p>
        <template #actions>
          <AppAction
            :disabled="busy"
            @click="revoke = null"
          >
            取消
          </AppAction>
          <AppAction
            variant="secondary"
            class="danger"
            :disabled="busy"
            @click="revokeGrant"
          >
            确认撤销
          </AppAction>
        </template>
      </SettingsConfirm>
    </template>
  </section>
</template>

<style scoped>
.stack { display: grid; gap: 12px; justify-items: start; min-width: 0; }
.agent-controls, .agent-list-heading { display: flex; gap: 8px; flex-wrap: wrap; align-items: center; min-width: 0; }
.agent-authorizations, .agent-service, .agent-scope-editor { width: min(100%, 48rem); }
.agent-service, .agent-grant, .agent-empty, .agent-scope-editor {
  border: 1px solid var(--rule);
  border-radius: 12px;
  background: var(--surface-pop);
}
.agent-service { display: flex; align-items: center; gap: 10px; padding: 11px 14px; }
.agent-service-meta { display: flex; flex-wrap: wrap; column-gap: 12px; align-items: center; margin-inline-end: auto; min-width: 0; }
.agent-service-meta h2 { font-size: .95rem; margin: 0; white-space: nowrap; }
.agent-service-status { display: inline-flex; align-items: center; gap: 6px; color: var(--ink-soft); font-size: .8rem; white-space: nowrap; }
.agent-service-status::before { content: ""; width: 6px; height: 6px; border-radius: 50%; background: currentColor; }
.agent-service-status.is-on { color: var(--accent-ink); }
.agent-service-icon, .agent-grant-icon, .agent-empty-icon, :deep(.agent-option-icon) {
  display: grid;
  place-items: center;
  flex: none;
  width: 38px;
  height: 38px;
  border-radius: 10px;
  background: var(--surface-sunken);
  color: var(--ink-soft);
}
.agent-service-icon, .agent-empty-icon { background: var(--safe-bg); color: var(--accent-ink); }
.agent-authorizations { display: grid; gap: 10px; }
.agent-service ~ .agent-authorizations { margin-top: 16px; }
.agent-list-heading { justify-content: space-between; margin-bottom: 2px; }
.agent-list-heading h2, .agent-scope-editor h2 { margin: 0; font-size: 1.13rem; }
.agent-list-heading h2 { display: flex; gap: 8px; align-items: center; }
.agent-list-heading h2 span { border-radius: 999px; background: var(--surface-sunken); color: var(--ink-soft); padding: 2px 7px; font-size: .75rem; }
.agent-labeled, :deep(.agent-quiet) { display: inline-flex; align-items: center; justify-content: center; gap: 6px; white-space: nowrap; min-height: 44px; }
.agent-labeled svg, :deep(.agent-quiet svg) { flex: none; }
:deep(.agent-quiet) { border: 1px solid transparent; border-radius: 8px; padding: 6px 10px; background: transparent; color: var(--ink-soft); font: inherit; font-weight: 550; cursor: pointer; }
:deep(.agent-quiet:hover:not(:disabled)) { background: var(--surface-sunken); color: var(--ink); }
.agent-icon-action { width: 44px; padding-inline: 0; flex: none; }
.agent-empty { display: grid; justify-items: center; gap: 4px; text-align: center; padding: 32px 16px 28px; }
.agent-empty-icon { width: 51px; height: 51px; border-radius: 14px; margin-bottom: 10px; }
.agent-empty h3, .agent-empty p, .agent-grant-title p { margin: 0; }
.agent-empty h3 { font-size: 1.1rem; }
.agent-empty p, .agent-grant-title p { color: var(--ink-soft); }
.agent-empty .agent-controls:not(:empty) { justify-content: center; margin-top: 12px; }
.agent-grant { display: grid; gap: 9px; min-width: 0; padding: 12px 14px 14px; }
.agent-grant-heading { display: grid; grid-template-columns: auto minmax(0, 1fr) auto auto; align-items: center; gap: 8px; }
.agent-grant-title { overflow-wrap: anywhere; }
.agent-grant-title p, .agent-grant-state { font-size: .8rem; }
.agent-grant-state { display: flex; justify-content: space-between; align-items: center; gap: 8px; color: var(--ink-soft); }
.agent-scope-list { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; padding: 0; margin: 0 0 0 46px; max-height: 12rem; overflow: auto; }
.agent-scope-list li { max-width: 100%; background: var(--surface-sunken); color: var(--ink-soft); border-radius: 6px; padding: 3px 8px; overflow-wrap: anywhere; font-size: .83rem; }
.agent-grant > .button, .agent-grant > .agent-quiet, .agent-authorizations > .button { justify-self: start; }
.agent-scope-editor { --editor-padding: clamp(14px, 3vw, 24px); padding: var(--editor-padding) var(--editor-padding) 0; justify-items: stretch; }
.agent-editor-account {
  display: flex;
  align-items: center;
  gap: 10px;
  min-width: 0;
  padding: 7px 10px;
  border: 1px solid var(--rule-soft);
  border-radius: 10px;
  background: var(--surface);
}
.agent-editor-account .app-select { min-width: 0; flex: 1; }
.agent-editor-account-name { min-width: 0; flex: 1; overflow-wrap: anywhere; }
.agent-scope-choices { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; border: 0; padding: 0; margin: 0; min-width: 0; }
.agent-visually-hidden { position: absolute; width: 1px; height: 1px; overflow: hidden; clip-path: inset(50%); white-space: nowrap; }
.agent-scope-choices label { display: flex; align-items: center; gap: 9px; min-width: 0; cursor: pointer; }
.agent-scope-choices label { min-height: 50px; border: 1px solid var(--rule); border-radius: 9px; background: var(--surface); padding: 8px 12px; }
.agent-scope-choices label:has(:checked) { border-color: var(--accent-solid); background: var(--safe-bg); color: var(--accent-ink); }
.agent-scope-choices input { width: 18px; height: 18px; margin: 0; flex: none; accent-color: var(--accent-solid); }
.agent-scope-note { margin: 0; color: var(--ink-soft); font-size: .85rem; }
.agent-chat-directory { border: 1px solid var(--rule); border-radius: 10px; }
.agent-chat-directory :deep(.search-directory-list) { flex: none; block-size: clamp(10rem, 34dvh, 16rem); scrollbar-gutter: stable; }
.agent-selected-heading { display: flex; justify-content: space-between; align-items: center; color: var(--ink-soft); font-size: .85rem; }
.agent-selected-chats { list-style: none; margin: 0; padding: 0 0 3px; display: flex; flex-wrap: wrap; gap: 6px; max-height: 7rem; overflow: auto; }
.agent-selected-chats li { display: flex; align-items: center; gap: 4px; max-width: 100%; padding-inline-start: 9px; border-radius: 8px; background: var(--surface-sunken); font-size: .85rem; overflow-wrap: anywhere; }
.agent-all-scope { border: 1px solid var(--safe-border); border-radius: 10px; padding: 14px; background: var(--safe-bg); }
.agent-all-scope p { margin: 4px 0 0; color: var(--ink-soft); }
.agent-editor-footer { display: grid; gap: 8px; margin: 0 calc(-1 * var(--editor-padding)); padding: 12px var(--editor-padding); border-top: 1px solid var(--rule-soft); border-radius: 0 0 12px 12px; background: var(--surface-pop); }
.agent-editor-footer p { margin: 0; }
.agent-editor-footer .agent-controls { justify-content: flex-end; }
.agent-controls input { min-width: 0; max-width: 100%; }
.agent-controls .button { min-height: 44px; }
.agent-panel-options { display: grid; gap: 8px; }
:deep(.agent-panel-option) {
  display: flex;
  align-items: center;
  gap: 10px;
  width: 100%;
  min-width: 0;
  min-height: 49px;
  padding: 7px 10px;
  border: 1px solid var(--rule);
  border-radius: 9px;
  color: var(--ink);
  background: var(--surface-pop);
  font: inherit;
  text-align: start;
  overflow-wrap: anywhere;
  cursor: pointer;
}
:deep(.agent-panel-option:hover:not(:disabled)) { background: var(--surface-sunken); }
:deep(.agent-panel-option small) { display: block; margin-top: 2px; color: var(--ink-soft); }
:deep(.agent-option-icon) { width: 34px; height: 34px; }
:deep(.agent-option-chevron) { flex: none; margin-inline-start: auto; color: var(--ink-faint); }
:deep(.agent-panel-option:disabled), :deep(.agent-quiet:disabled) { opacity: .55; cursor: not-allowed; }
.agent-grant-panel { display: grid; gap: 12px; min-width: 0; }
.agent-grant-panel-scope { display: flex; align-items: center; gap: 10px; min-width: 0; padding: 10px 12px; border-radius: 9px; background: var(--safe-bg); color: var(--accent-ink); overflow-wrap: anywhere; }
.agent-grant-panel-scope svg { flex: none; }
.agent-grant-panel-scope small { display: block; margin-top: 2px; color: var(--ink-soft); }
.agent-grant-receipt { margin: 0; color: var(--accent-ink); }
.agent-grant-panel-controls { display: flex; flex-wrap: wrap; justify-content: space-between; align-items: center; gap: 4px; border-top: 1px solid var(--rule-soft); padding-top: 4px; }
.agent-grant-panel-controls .danger { margin-inline-start: auto; }
.agent-binding { justify-items: stretch; }
.agent-binding label { display: grid; gap: 5px; min-width: 0; }
.agent-binding input { min-width: 0; min-height: 44px; }
.agent-binding-actions { justify-content: space-between; }
.agent-panel-error { margin-top: 12px; }
@media (max-width: 29rem) {
  .agent-heading-actions { width: 100%; justify-content: space-between; }
  .agent-service { padding: 9px 10px; gap: 5px; }
  .agent-service-meta { display: grid; }
  .agent-service:has(.agent-icon-action) .agent-service-icon { display: none; }
  .agent-service .button { padding-inline: 10px; }
  .agent-grant { padding-inline: 10px; }
  .agent-grant-heading { gap: 4px; }
  .agent-edit { padding-inline: 4px; font-size: .82rem; }
  .agent-scope-list { margin-inline-start: 42px; }
  .agent-editor-footer .agent-controls .primary { flex: 1; }
}
</style>

<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch } from "vue";

import type { AgentAccessGrant, AgentAccessStatus, AgentChatScope } from "@/api/archiveApi";
import { useArchiveApi } from "@/api/injection";
import AppSelect from "@/components/ui/AppSelect.vue";

const props = defineProps<{
  local: boolean;
  configured: boolean;
  consentId?: string | undefined;
  additionTicket?: string | undefined;
}>();
const api = useArchiveApi();
const status = ref<AgentAccessStatus>();
const requester = ref("");
const additions = ref<{ id: string; name: string }[]>([]);
const reviewing = ref(false);
const accounts = ref<{ id: string; name: string }[]>([]);
const chats = ref<{ id: string; name: string }[]>([]);
const selected = ref<string[]>([]);
const account = ref("");
const mode = ref<AgentChatScope["mode"]>("selected");
const draft = ref<AgentAccessGrant | "new" | null>(null);
const revoke = ref<AgentAccessGrant | null>(null);
const rotate = ref<AgentAccessGrant | null>(null);
const search = ref("");
const cursor = ref<string | null>(null);
const statusText = ref("");
const configuration = ref("");
const binding = ref(false);
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
const canSave = computed(() => account.value !== "" && (mode.value === "all" || selected.value.length > 0));
let chatLoad = 0;
let pendingSave: { signature: string; operationId: string } | undefined;
let scopeLoad = 0;
let disposed = false;
let polling = false;
const scopeKey = (accountId: string, chat: string): string => JSON.stringify([accountId, chat]);
const scopeReady = (grant: AgentAccessGrant): boolean => grant.scope?.mode === "all"
  || (grant.scope?.conversation_ids ?? []).every(id => Boolean(scopeNames.value[scopeKey(grant.account_id, id)]));
const scopeItems = (grant: AgentAccessGrant): { id: string; name: string }[] =>
  (grant.scope?.conversation_ids ?? []).map(id => ({ id, name: scopeNames.value[scopeKey(grant.account_id, id)] ?? "名称加载中…" }));
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
  binding.value = true;
  account.value = accounts.value[0]?.id ?? "";
  chooseBindingAccount();
}

async function copyAddress(): Promise<void> {
  await run(async () => {
    if (status.value) await globalThis.navigator.clipboard.writeText(status.value.address);
    statusText.value = "已复制地址";
  });
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
      status.value = await api.getAgentAccess();
      if (props.consentId) requester.value = (await api.getAgentConsent(props.consentId)).client_id;
      accounts.value = props.local
        ? (await api.bootstrap()).accounts.map(row => ({ id: row.id, name: row.display_name }))
        : (await api.listRemoteArchiveAccounts()).items.map(row => ({
          id: row.source_account_id, name: row.display_name ?? row.source_account_id,
        }));
      await hydrateScopeNames(status.value.grants);
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
  if (!append) {
    chats.value = [];
    cursor.value = null;
  }
  const options = { limit: 40, q: search.value, ...(cursor.value ? { cursor: cursor.value } : {}) };
  try {
    const result = props.local ? await api.listConversations(account.value, options)
      : await api.listRemoteConversations({ ...options, sourceAccountId: account.value });
    if (id !== chatLoad) return;
    const rows = result.items.map(item => ({
      id: "id" in item ? item.id : item.conversation_id,
      name: item.display_name ?? ("id" in item ? item.id : item.conversation_id),
    }));
    chats.value = append ? [...chats.value, ...rows] : rows;
    cursor.value = result.next_cursor;
  } catch {
    if (id === chatLoad) chatProblem.value = true;
  } finally {
    if (id === chatLoad) chatsLoading.value = false;
  }
}

function edit(grant: AgentAccessGrant | "new"): void {
  pendingSave = undefined;
  configuration.value = "";
  draft.value = grant;
  search.value = "";
  account.value = grant === "new" ? accounts.value[0]?.id ?? "" : grant.account_id;
  selected.value = grant === "new" ? [] : [...(grant.scope?.conversation_ids ?? [])];
  mode.value = grant === "new" ? "selected" : grant.scope?.mode ?? "selected";
  void listChats();
}

function changeAccount(): void {
  selected.value = [];
  search.value = "";
  void listChats();
}

async function finishConsent(grantId: string | null): Promise<void> {
  if (!props.consentId) return;
  const result = await api.completeAgentConsent(props.consentId, grantId);
  globalThis.location.assign(result.redirect_uri);
}

async function save(): Promise<void> {
  const target = draft.value;
  if (target === null || !canSave.value) return;
  const scope: AgentChatScope = {
    mode: mode.value, conversation_ids: mode.value === "all" ? [] : [...selected.value],
  };
  const signature = JSON.stringify([account.value, scope]);
  if (pendingSave?.signature !== signature) {
    pendingSave = { signature, operationId: globalThis.crypto.randomUUID() };
  }
  const operation = pendingSave.operationId;
  await run(async () => {
    const result = target === "new"
      ? await api.createAgentGrant(account.value, scope, operation)
      : await api.updateAgentGrant(target.id, target.version, operation, scope);
    pendingSave = undefined;
    draft.value = null;
    if (props.consentId) {
      consentGrant.value = result;
      await finishConsent(result.id);
      return;
    }
    await refreshStatus();
    statusText.value = result.sync_state === "pending" ? "待同步 · 本机查询已暂停" : "已保存";
  });
}

async function revokeGrant(): Promise<void> {
  const target = revoke.value;
  if (!target) return;
  await run(async () => {
    const result = await api.updateAgentGrant(target.id, target.version, globalThis.crypto.randomUUID(), null);
    revoke.value = null;
    await refreshStatus();
    statusText.value = result.sync_state === "pending" ? "待同步 · 本机查询已暂停" : "已撤销授权";
  });
}

async function copyCredential(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    const credential = await api.issueAgentCredential(grant.id);
    configuration.value = JSON.stringify({
      url: credential.address, headers: { Authorization: `Bearer ${credential.token}` },
    }, null, 2);
    rotate.value = null;
    await globalThis.navigator.clipboard.writeText(configuration.value);
    statusText.value = "已复制连接配置";
    configuration.value = "";
  });
}

async function copyLink(grant: AgentAccessGrant): Promise<void> {
  await run(async () => {
    const link = await api.offerAgentLink(grant.id, grant.account_id);
    await globalThis.navigator.clipboard.writeText(link.code);
    statusText.value = "已复制关联码";
  });
}

async function bind(): Promise<void> {
  await run(async () => {
    if (!canBind.value) return;
    const target = bindTarget.value;
    await api.bindAgentLink(account.value, linkCode.value.trim(), target?.id, target?.version);
    linkCode.value = "";
    binding.value = false;
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
const poll = globalThis.setInterval(() => {
  if (disposed || polling || busy.value || draft.value !== null || binding.value || !props.configured
    || !status.value || globalThis.document.visibilityState === "hidden") return;
  polling = true;
  void refreshStatus().catch(() => { /* Keep existing state; the server still enforces the policy. */ })
    .finally(() => { polling = false; });
}, 5000);
onBeforeUnmount(() => { disposed = true; scopeLoad++; globalThis.clearInterval(poll); });
</script>

<template>
  <section
    class="stack"
    aria-label="授权管理"
  >
    <div
      v-if="!configured"
      role="status"
      class="stack"
    >
      <strong>MCP 待配置</strong>
      <p>请在自托管服务配置 HTTPS 地址和 OAuth 客户端回调。</p>
    </div>
    <p
      v-if="problem"
      class="notice-warn"
      role="alert"
    >
      {{ problem }}
    </p>
    <p
      v-if="statusText"
      role="status"
    >
      {{ statusText }}
    </p>
    <button
      v-if="failed"
      type="button"
      class="button"
      :disabled="busy"
      @click="load"
    >
      重新加载
    </button>
    <p
      v-else-if="configured && !status"
      role="status"
    >
      加载中…
    </p>
    <template v-else-if="status">
      <template v-if="!consentId">
        <div class="agent-controls">
          <span>MCP 连接 · {{ status.enabled ? "已启用" : "已关闭" }}</span>
          <button
            v-if="local"
            type="button"
            class="button"
            :disabled="busy"
            @click="run(async () => { if (status) status = await api.setAgentAccess(!status.enabled); })"
          >
            {{ status.enabled ? "关闭" : "启用本机 MCP" }}
          </button>
        </div>
        <div
          v-if="status.enabled"
          class="agent-controls"
        >
          <code data-feedback-mask="connection-address">{{ status.address }}</code>
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="copyAddress"
          >
            复制地址
          </button>
        </div>
      </template>
      <h2 v-else-if="requester">
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
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="resolveAddition(false)"
          >
            {{ additions.length ? "暂不加入" : "完成" }}
          </button>
          <button
            v-if="additions.length"
            type="submit"
            class="button primary"
            :disabled="busy"
          >
            允许加入
          </button>
        </div>
      </form>
      <form
        v-else-if="draft !== null"
        class="stack"
        @submit.prevent="save"
      >
        <label>账号
          <AppSelect
            v-model="account"
            :options="accountOptions"
            aria-label="账号"
            data-feedback-mask="account-name"
            sensitive
            :disabled="draft !== 'new' || busy"
            @change="changeAccount"
          />
        </label>
        <fieldset :disabled="busy">
          <legend>可看的聊天</legend>
          <label><input
            v-model="mode"
            type="radio"
            value="selected"
          > 指定聊天</label>
          <label><input
            v-model="mode"
            type="radio"
            value="all"
          > 全部聊天</label>
        </fieldset>
        <p>{{ mode === "all" ? "包含历史、后续消息和未来新增聊天。" : "包含所选聊天的历史和后续消息。" }}</p>
        <template v-if="mode === 'selected'">
          <div class="agent-controls">
            <input
              v-model="search"
              type="search"
              aria-label="搜索聊天"
              placeholder="搜索聊天"
              @keydown.enter.prevent="listChats()"
            >
            <button
              type="button"
              class="button"
              @click="listChats()"
            >
              搜索
            </button>
          </div>
          <span role="status">已选 {{ selected.length }}</span>
          <div class="stack agent-chat-list">
            <label
              v-for="chat in chats"
              :key="chat.id"
              data-feedback-mask="conversation-name"
            >
              <input
                v-model="selected"
                type="checkbox"
                :value="chat.id"
                :disabled="busy || selected.length >= 500 && !selected.includes(chat.id)"
              >
              {{ chat.name }}
            </label>
          </div>
          <p
            v-if="chatsLoading"
            role="status"
          >
            加载中…
          </p>
          <button
            v-else-if="chatProblem"
            type="button"
            class="button"
            @click="listChats()"
          >
            重新加载聊天
          </button>
          <p v-else-if="!chats.length">
            暂无聊天
          </p>
          <button
            v-if="cursor"
            type="button"
            class="button"
            :disabled="chatsLoading"
            @click="listChats(true)"
          >
            加载更多
          </button>
        </template>
        <p v-if="draft === 'new'">
          内容传给 AI 后无法收回。
        </p>
        <div class="agent-controls">
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="draft = null"
          >
            取消
          </button>
          <button
            type="submit"
            class="button primary"
            :disabled="busy || !canSave"
          >
            {{ consentId ? "允许" : "保存" }}
          </button>
        </div>
      </form>

      <template v-else>
        <article
          v-for="grant in status.grants"
          :key="grant.id"
          class="stack agent-grant"
        >
          <strong data-feedback-mask="account-name">{{ accounts.find(item => item.id === grant.account_id)?.name ?? grant.account_id }}</strong>
          <span>{{ grant.scope?.mode === "all" ? "全部聊天" : `指定聊天 · ${grant.scope?.conversation_ids.length ?? 0}` }}</span>
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
          <button
            v-if="scopeNameError && !scopeReady(grant)"
            type="button"
            class="button"
            :disabled="busy"
            @click="run(() => hydrateScopeNames(status?.grants ?? []))"
          >
            重试范围
          </button>
          <span v-if="grant.sync_state">{{ syncLabels[grant.sync_state] }}</span>
          <div class="agent-controls">
            <button
              v-if="consentId"
              type="button"
              class="button primary"
              :disabled="busy || !scopeReady(grant)"
              @click="run(() => finishConsent(grant.id))"
            >
              使用此范围连接
            </button>
            <template v-else>
              <button
                type="button"
                class="button"
                :disabled="busy"
                @click="edit(grant)"
              >
                调整范围
              </button>
              <button
                v-if="local && status.enabled"
                type="button"
                class="button"
                :disabled="busy"
                @click="rotate = grant"
              >
                复制连接配置
              </button>
              <button
                v-if="!local"
                type="button"
                class="button"
                :disabled="busy"
                @click="copyLink(grant)"
              >
                复制本机关联码
              </button>
              <button
                v-if="grant.sync_state === 'pending'"
                type="button"
                class="button"
                :disabled="busy"
                @click="retry(grant)"
              >
                同步
              </button>
              <button
                v-if="grant.sync_state === 'conflict'"
                type="button"
                class="button"
                :disabled="busy"
                @click="useCurrentRange(grant)"
              >
                放弃待同步修改，采用远端范围
              </button>
              <button
                type="button"
                class="button"
                :disabled="busy"
                @click="revoke = grant"
              >
                撤销授权
              </button>
            </template>
          </div>
        </article>
        <textarea
          v-if="configuration"
          :value="configuration"
          readonly
          aria-label="连接配置"
        />
        <button
          v-if="accounts.length && !consentGrant"
          type="button"
          class="button"
          :disabled="busy"
          @click="edit('new')"
        >
          新建授权
        </button>
        <button
          v-if="consentGrant"
          type="button"
          class="button"
          :disabled="busy"
          @click="run(() => finishConsent(consentGrant?.id ?? null))"
        >
          完成连接
        </button>
        <button
          v-if="consentId"
          type="button"
          class="button"
          :disabled="busy"
          @click="run(() => finishConsent(null))"
        >
          暂不允许
        </button>
      </template>
      <template v-if="local && !consentId && draft === null">
        <button
          v-if="!binding && accounts.length"
          type="button"
          class="button"
          @click="beginBinding"
        >
          关联远程授权
        </button>
        <form
          v-if="binding"
          class="stack"
          @submit.prevent="bind"
        >
          <label>本机账号
            <AppSelect
              v-model="account"
              :options="accountOptions"
              aria-label="本机账号"
              data-feedback-mask="account-name"
              sensitive
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
              :disabled="busy"
            />
          </label>
          <small v-if="bindTarget">原连接一并关联，范围沿用远端。</small>
          <label>远程关联码 <input
            v-model="linkCode"
            autocomplete="off"
            type="password"
          ></label>
          <div class="agent-controls">
            <button
              type="button"
              class="button"
              @click="binding = false; linkCode = ''"
            >
              取消
            </button>
            <button
              type="submit"
              class="button primary"
              :disabled="busy || !canBind"
            >
              关联
            </button>
          </div>
        </form>
      </template>
      <div
        v-if="rotate"
        class="stack"
        role="alert"
      >
        <p>生成配置后，旧连接凭据将失效。</p>
        <div class="agent-controls">
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="rotate = null"
          >
            取消
          </button>
          <button
            type="button"
            class="button primary"
            :disabled="busy"
            @click="copyCredential(rotate)"
          >
            生成并复制
          </button>
        </div>
      </div>
      <div
        v-if="revoke"
        class="stack"
        role="alert"
      >
        <p>撤销后将停止访问，已传出的内容无法收回。</p>
        <div class="agent-controls">
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="revoke = null"
          >
            取消
          </button>
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="revokeGrant"
          >
            确认撤销
          </button>
        </div>
      </div>
    </template>
  </section>
</template>

<style scoped>
.stack { display: grid; gap: 12px; justify-items: start; min-width: 0; }
.agent-grant { padding-block: 12px; border-block-start: 1px solid var(--border); width: 100%; }
.agent-controls { display: flex; flex-wrap: wrap; gap: 8px; align-items: center; min-width: 0; }
.agent-controls code { overflow-wrap: anywhere; }
.agent-chat-list { max-height: 20rem; overflow: auto; width: 100%; }
.agent-scope-list { margin: 0; padding-inline-start: 20px; max-height: 12rem; overflow: auto; }
fieldset { display: flex; gap: 16px; flex-wrap: wrap; }
textarea { width: 100%; min-height: 100px; }
</style>

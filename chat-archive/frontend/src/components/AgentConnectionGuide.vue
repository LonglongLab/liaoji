<script setup lang="ts">
import { ChevronRight, Plug, ShieldCheck, Terminal } from "@lucide/vue";

import AppAction from "@/components/ui/AppAction.vue";
import { type AgentClient, agentClients, agentSetupNextStep } from "@/lib/agentConnection";

defineProps<{
  stage: "tools" | "scope" | "config";
  client: AgentClient;
  local: boolean;
  busy: boolean;
  address: string;
  hasAccounts: boolean;
  scopes: readonly { id: string; account: string; description: string; ready: boolean }[];
  scopeLabel: string | undefined;
  copied: boolean;
  receipt: string;
}>();
const emit = defineEmits<{
  selectTool: [client: AgentClient];
  useScope: [id: string];
  back: [];
  chooseScope: [];
  copyConfiguration: [];
  copyAddress: [];
  copyPrompt: [];
}>();
const tools = ["codex", "claude", "cursor", "other"] as const;
</script>

<template>
  <div class="guide">
    <div
      v-if="stage === 'tools'"
      class="guide-options"
    >
      <button
        v-for="tool in tools"
        :key="tool"
        type="button"
        class="agent-panel-option"
        :disabled="busy"
        @click="emit('selectTool', tool)"
      >
        <span class="agent-option-icon"><component
          :is="tool === 'other' ? Plug : Terminal"
          :size="17"
          aria-hidden="true"
        /></span>
        <strong>{{ tool === "other" ? "通用 MCP" : agentClients[tool].name }}</strong>
        <ChevronRight
          class="agent-option-chevron"
          :size="16"
          aria-hidden="true"
        />
      </button>
    </div>
    <template v-else-if="stage === 'scope'">
      <h4>选择可看的聊天</h4>
      <div
        v-if="scopes.length"
        class="guide-options"
      >
        <button
          v-for="scope in scopes"
          :key="scope.id"
          type="button"
          class="agent-panel-option"
          :disabled="busy || !scope.ready"
          @click="emit('useScope', scope.id)"
        >
          <span class="agent-option-icon"><ShieldCheck
            :size="17"
            aria-hidden="true"
          /></span>
          <span>
            <strong data-feedback-mask="account-name">{{ scope.account }}</strong>
            <small data-feedback-mask="conversation-name">{{ scope.description }}</small>
          </span>
          <ChevronRight
            class="agent-option-chevron"
            :size="16"
            aria-hidden="true"
          />
        </button>
      </div>
      <p v-if="!hasAccounts && !scopes.length">
        先导入聊天记录，再选择可以授权的聊天。
      </p>
      <div class="guide-actions">
        <button
          type="button"
          class="agent-quiet"
          :disabled="busy"
          @click="emit('back')"
        >
          返回
        </button>
        <button
          v-if="hasAccounts"
          type="button"
          class="button primary"
          :disabled="busy"
          @click="emit('chooseScope')"
        >
          新建授权
        </button>
        <AppAction
          v-else
          to="/start"
          variant="inline"
        >
          导入聊天
        </AppAction>
      </div>
    </template>
    <template v-else>
      <div
        v-if="scopeLabel"
        class="guide-range"
        data-feedback-mask="account-name"
      >
        <ShieldCheck
          :size="18"
          aria-hidden="true"
        />
        <span>{{ scopeLabel }}</span>
      </div>
      <div
        v-if="!local && address"
        class="guide-address"
      >
        <code data-feedback-mask="connection-address">{{ address }}</code>
        <AppAction
          v-if="client !== 'other'"
          variant="quiet"
          :disabled="busy"
          @click="emit('copyAddress')"
        >
          复制地址
        </AppAction>
      </div>
      <p
        v-if="receipt"
        class="guide-receipt"
        role="status"
      >
        {{ receipt }}
      </p>
      <p
        v-if="copied || !local"
        class="guide-next-step"
      >
        {{ copied ? agentSetupNextStep[client][local ? "local" : "remote"] : "在工具中配置后登录聊记。" }}
      </p>
      <div class="guide-actions">
        <div class="guide-support">
          <AppAction
            variant="quiet"
            :disabled="busy"
            @click="emit('back')"
          >
            返回
          </AppAction>
          <AppAction
            v-if="agentClients[client].docs"
            :href="agentClients[client].docs"
            variant="quiet"
            target="_blank"
            rel="noopener noreferrer"
          >
            官方指南
          </AppAction>
        </div>
        <div class="guide-copy-actions">
          <AppAction
            v-if="copied"
            variant="quiet"
            :disabled="busy"
            @click="emit('copyPrompt')"
          >
            复制测试指令
          </AppAction>
          <AppAction
            variant="primary"
            :disabled="busy || !address || local && !scopeLabel"
            @click="!local && client === 'other' ? emit('copyAddress') : emit('copyConfiguration')"
          >
            {{ !local && client === "other" ? "复制地址" : client === "claude" ? "复制接入命令" : client === "other" ? "复制连接配置" : "复制配置" }}
          </AppAction>
        </div>
      </div>
    </template>
  </div>
</template>

<style scoped>
.guide, .guide-options { display: grid; gap: 8px; }
.guide h4, .guide p { margin: 0; }
.guide-range, .guide-address { display: flex; align-items: center; gap: 8px; padding: 10px; border-radius: 8px; min-width: 0; }
.guide-range { background: var(--safe-bg); color: var(--accent-ink); overflow-wrap: anywhere; }
.guide-range svg { flex: none; }
.guide-address { flex-wrap: wrap; background: var(--surface-sunken); }
.guide-address code { margin-inline-end: auto; overflow-wrap: anywhere; min-width: 0; }
.guide .guide-receipt { color: var(--accent-ink); }
.guide .guide-next-step { color: var(--ink-soft); font-size: .875rem; overflow-wrap: anywhere; }
.guide-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 8px; margin-top: 8px; }
.guide-actions > :first-child { margin-inline-end: auto; }
.guide-support, .guide-copy-actions { display: flex; flex-wrap: wrap; align-items: center; gap: 4px; }
.guide-copy-actions { margin-inline-start: auto; gap: 8px; }
</style>

<script setup lang="ts">
import { Plug } from "@lucide/vue";
import { ref } from "vue";

import AppAction from "@/components/ui/AppAction.vue";
import { type AgentClient, agentClients } from "@/lib/agentConnection";

defineProps<{
  local: boolean;
  available: boolean;
  enabled: boolean;
  open: boolean;
  busy: boolean;
  address?: string | undefined;
  hasAccounts: boolean;
  scopeLabel?: string | undefined;
  receipt: string;
}>();
const client = defineModel<AgentClient>("client", { required: true });
const heading = ref<HTMLHeadingElement>();
defineExpose({ focus: () => heading.value?.focus() });

const emit = defineEmits<{
  connect: [];
  close: [];
  setEnabled: [enabled: boolean];
  chooseScope: [];
  copyConfiguration: [];
  copyAddress: [];
  copyPrompt: [];
}>();
function select(tool: AgentClient): void {
  client.value = tool;
  emit("connect");
}
</script>

<template>
  <section
    class="connection-card"
    aria-label="连接 AI 工具"
  >
    <header class="connection-heading">
      <span class="connection-icon"><Plug
        :size="21"
        aria-hidden="true"
      /></span>
      <div class="connection-title">
        <h2
          ref="heading"
          tabindex="-1"
        >
          {{ local ? "本机 MCP" : "远程 MCP" }}
        </h2>
        <span
          class="connection-state"
          :class="{ ready: enabled }"
        >{{ !available ? "待配置" : enabled ? "已开启" : "已关闭" }}</span>
      </div>
      <button
        v-if="available"
        type="button"
        class="button"
        :class="{ primary: !enabled }"
        :disabled="busy"
        @click="emit('setEnabled', !enabled)"
      >
        {{ enabled ? "关闭 MCP" : "开启 MCP" }}
      </button>
    </header>
    <p v-if="!available">
      请先完成远程档案设置。
    </p>
    <p v-else-if="!enabled">
      开启后，AI 工具可以在授权范围内查阅聊天和文件。
    </p>
    <template v-else>
      <div class="connection-actions">
        <button
          type="button"
          class="button primary"
          :disabled="busy"
          @click="select('codex')"
        >
          连接 Codex
        </button>
        <button
          type="button"
          class="button"
          :disabled="busy"
          @click="select('claude')"
        >
          连接 Claude Code
        </button>
        <details>
          <summary>其他工具</summary>
          <div class="connection-actions">
            <button
              type="button"
              class="button"
              :disabled="busy"
              @click="select('cursor')"
            >
              连接 Cursor
            </button>
            <button
              type="button"
              class="button"
              :disabled="busy"
              @click="select('other')"
            >
              通用 MCP
            </button>
          </div>
        </details>
      </div>
      <div
        v-if="open"
        class="connection-guide"
      >
        <div class="connection-actions">
          <h3>连接 {{ agentClients[client].name }}</h3>
          <button
            type="button"
            class="button"
            :disabled="busy"
            @click="emit('close')"
          >
            收起
          </button>
        </div>
        <template v-if="local && !scopeLabel">
          <p v-if="!hasAccounts">
            先导入聊天记录，再选择可以授权的聊天。
          </p>
          <div
            v-else
            class="connection-actions"
          >
            <button
              type="button"
              class="button primary"
              :disabled="busy"
              @click="emit('chooseScope')"
            >
              选择可看的聊天
            </button>
          </div>
        </template>
        <template v-else>
          <p
            v-if="scopeLabel"
            data-feedback-mask="account-name"
          >
            {{ scopeLabel }}
          </p>
          <div
            v-if="!local && address"
            class="connection-actions"
          >
            <code data-feedback-mask="connection-address">{{ address }}</code>
            <button
              type="button"
              class="button"
              :disabled="busy"
              @click="emit('copyAddress')"
            >
              {{ receipt === "已复制地址" ? "已复制地址" : "复制地址" }}
            </button>
          </div>
          <p v-if="client === 'codex'">
            打开 Codex 的个人 config.toml。入口在「设置 → MCP 服务器」。粘贴配置；已有同名项就替换。
          </p>
          <p v-else-if="client === 'claude'">
            复制命令，在终端粘贴运行。重新打开 Claude Code 后，在 /mcp 找到聊记。
          </p>
          <p v-else-if="client === 'cursor'">
            打开 Cursor 的「设置 → Tools &amp; MCP」，添加聊记到个人 mcp.json。
          </p>
          <p v-else>
            在 AI 工具里添加 Streamable HTTP 服务器。
          </p>
          <p v-if="local">
            连接时保持聊记运行。配置含访问凭据，请只用于自己的工具。
          </p>
          <p v-else>
            首次连接时，在 AI 工具里登录，回到聊记确认它可以查看的聊天。
          </p>
          <div class="connection-actions">
            <button
              v-if="local || client !== 'other'"
              type="button"
              class="button primary"
              :disabled="busy || !address"
              @click="emit('copyConfiguration')"
            >
              {{ receipt === "已复制连接配置" ? "已复制" : client === "claude" ? "复制接入命令" : "复制配置" }}
            </button>
            <button
              type="button"
              class="button"
              :disabled="busy"
              @click="emit('copyPrompt')"
            >
              复制测试指令
            </button>
            <AppAction
              v-if="agentClients[client].docs"
              :href="agentClients[client].docs"
              variant="inline"
              target="_blank"
              rel="noopener noreferrer"
            >
              官方指南
            </AppAction>
          </div>
        </template>
      </div>
    </template>
    <slot />
  </section>
</template>

<style scoped>
.connection-card { display: grid; gap: 16px; width: min(100%, 48rem); min-width: 0; padding: clamp(14px, 3vw, 24px); border: 1px solid var(--rule); border-radius: 16px; background: var(--surface-pop); }
.connection-heading, .connection-actions { display: flex; align-items: center; flex-wrap: wrap; gap: 10px; min-width: 0; }
.connection-title { flex: 1 1 8rem; min-width: 0; }
.connection-title h2 { margin: 0 0 4px; font-size: 1.15rem; }
.connection-icon { display: grid; place-items: center; width: 42px; height: 42px; border-radius: 11px; color: var(--accent-ink); background: var(--safe-bg); flex: none; }
.connection-card p { margin: 0; color: var(--ink-soft); }
.connection-state { padding: 4px 9px; border-radius: 999px; background: var(--surface-sunken); color: var(--ink-soft); font-size: 0.8rem; }
.connection-state.ready { background: var(--safe-bg); color: var(--accent-ink); }
.connection-guide { display: grid; gap: 12px; min-width: 0; padding-top: 14px; border-top: 1px solid var(--rule-soft); }
.connection-guide h3 { margin: 0; font-size: 1rem; }
.connection-card .button, .connection-card summary { min-height: 44px; }
.connection-card summary { align-content: center; cursor: pointer; }
.connection-card code { overflow-wrap: anywhere; max-width: 100%; }
</style>

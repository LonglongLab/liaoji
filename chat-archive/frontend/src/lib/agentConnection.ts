export const agentClients = {
  codex: { name: "Codex", docs: "https://learn.chatgpt.com/docs/extend/mcp?surface=cli" },
  claude: { name: "Claude Code", docs: "https://code.claude.com/docs/en/mcp" },
  cursor: { name: "Cursor", docs: "https://cursor.com/docs/mcp" },
  other: { name: "其他工具", docs: null },
} as const;

export type AgentClient = keyof typeof agentClients;

const CODEX_CALLBACK_PORT = 43119;
const CODEX_CALLBACK = "http://127.0.0.1:" + String(CODEX_CALLBACK_PORT) + "/callback";
const CLAUDE_CALLBACK_PORT = 43120;
const REMOTE_SERVER_NAME = "liaoji_remote";

export const agentSetupNextStep = {
  codex: {
    local: "粘贴到 ~/.codex/config.toml，有同名项就替换，再重开 Codex。",
    remote: `粘贴到 ~/.codex/config.toml，再运行 codex mcp login ${REMOTE_SERVER_NAME}。`,
  },
  claude: {
    local: "在终端执行接入命令。更新已有配置时，先移除同名连接。",
    remote: "在终端执行接入命令，再在 Claude Code 中用 /mcp 登录聊记。",
  },
  cursor: {
    local: "合并到 ~/.cursor/mcp.json 的 mcpServers，有同名项就替换。",
    remote: "合并到 ~/.cursor/mcp.json 的 mcpServers，再在 Cursor 的 MCP 设置中登录聊记。",
  },
  other: {
    local: "在客户端添加 Streamable HTTP 连接，填入配置里的地址和请求头。",
    remote: "在客户端添加 Streamable HTTP 地址，再按提示登录聊记。",
  },
} satisfies Record<AgentClient, { local: string; remote: string }>;

export const agentTestPrompt = "请调用聊记 MCP 的 liaoji_get_access_context，告诉我连接是否可用以及当前授权范围。请勿读取聊天正文。";

export function agentClientConfiguration(
  client: AgentClient,
  address: string,
  credential?: { grantId: string; token: string },
): string {
  if (credential && !/^[\w-]+$/.test(credential.grantId)) {
    throw new Error("Invalid MCP grant ID for connection configuration");
  }
  const token = credential?.token;
  const headers = token === undefined ? undefined : { Authorization: "Bearer " + token };
  const name = credential ? "liaoji_" + credential.grantId : REMOTE_SERVER_NAME;
  if (client === "claude") {
    if (!/^https?:\/\/(?:[a-z\d.-]+|\[[a-f\d:.]+\])(?::\d+)?\/mcp$/i.test(address)
      || (token !== undefined && !/^[\w-]+$/.test(token))) {
      throw new Error("Invalid MCP connection configuration");
    }
    if (headers) {
      return "claude mcp add --transport http --scope user " + name + " " + JSON.stringify(address)
        + " --header " + JSON.stringify("Authorization: " + headers.Authorization);
    }
    return "claude mcp add --transport http --scope user --client-id claude-code --callback-port "
      + String(CLAUDE_CALLBACK_PORT) + " " + name + " " + JSON.stringify(address);
  }
  if (client === "codex") {
    const table = "mcp_servers." + name;
    const server = ["[" + table + "]", "url = " + JSON.stringify(address)];
    if (headers) {
      server.push("http_headers = { Authorization = " + JSON.stringify(headers.Authorization) + " }");
    } else {
      server.push(
        "oauth_resource = " + JSON.stringify(address), "", "[" + table + ".oauth]", 'client_id = "codex"',
        "callback_url = " + JSON.stringify(CODEX_CALLBACK), "callback_port = " + String(CODEX_CALLBACK_PORT),
      );
    }
    return server.join("\n");
  }
  if (client === "cursor") {
    return JSON.stringify({ mcpServers: { [name]: {
      url: address, ...(headers ? { headers } : { auth: { CLIENT_ID: "cursor" } }),
    } } }, null, 2);
  }
  return headers ? JSON.stringify({ url: address, headers }, null, 2) : address;
}

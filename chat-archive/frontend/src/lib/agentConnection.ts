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

export const agentTestPrompt = "请用聊记 MCP 检查当前授权范围。只告诉我连接是否可用、授权范围是什么，先不要读取聊天正文。";

export function agentClientConfiguration(client: AgentClient, address: string, token?: string): string {
  const headers = token === undefined ? undefined : { Authorization: "Bearer " + token };
  const name = token === undefined ? "liaoji_remote" : "liaoji_local";
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

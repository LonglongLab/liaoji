// 部署向导的纯编排逻辑（FR-REMOTE-003）：I/O 由 scripts/deploy-wizard.mjs 执行。
// 这里只做步骤规划、健康检测比对、MFA 结果解析与输出脱敏，保持可在 workerd 内测试。
// 本模块必须保持零导入：deploy-wizard.mjs 经 Node 原生 type stripping 直接加载它。

export const CONTRACT_VERSION = "0.0.1";

// 与 migrations/ 目录中最新迁移前缀一致；新增迁移时同步递增。
export const SCHEMA_VERSION = "0008";

export interface DeployState {
  loggedIn: boolean;
  d1DatabaseName: string;
  d1Exists: boolean;
  r2BucketName: string;
  r2Exists: boolean;
  archiveCount: number;
}

export interface DeployStep {
  id: string;
  title: string;
  command: string | null;
}

export const planDeploySteps = (state: DeployState): DeployStep[] => {
  if (!state.loggedIn) {
    return [
      {
        id: "login",
        title: "登录 Cloudflare 账号",
        command: "wrangler login",
      },
    ];
  }
  const steps: DeployStep[] = [];
  if (!state.d1Exists) {
    steps.push({
      id: "create-d1",
      title: "创建 D1 数据库",
      command: `wrangler d1 create ${state.d1DatabaseName}`,
    });
  }
  if (!state.r2Exists) {
    steps.push({
      id: "create-r2",
      title: "创建私有 R2 Bucket",
      command: `wrangler r2 bucket create ${state.r2BucketName}`,
    });
  }
  steps.push(
    {
      id: "apply-migrations",
      title: "应用 D1 迁移",
      command: `wrangler d1 migrations apply ${state.d1DatabaseName} --remote`,
    },
    {
      id: "deploy",
      title: "部署 Worker",
      command: "wrangler deploy",
    },
    {
      id: "health-check",
      title: "连接检测（health + schema 版本比对）",
      command: null,
    },
    {
      id: "mfa-check",
      title: "Cloudflare 账号 MFA 检查",
      command: null,
    },
  );
  if (state.archiveCount === 0) {
    steps.push({
      id: "bootstrap-archive",
      title: "创建初始归档并签发首个配对码",
      command: null,
    });
  }
  return steps;
};

export type MfaStatus = "enabled" | "disabled" | "unknown";

export const parseMfaStatus = (payload: unknown): MfaStatus => {
  if (typeof payload !== "object" || payload === null) return "unknown";
  const body = payload as Record<string, unknown>;
  if (body["success"] !== true) return "unknown";
  const result = body["result"];
  if (typeof result !== "object" || result === null) return "unknown";
  const flag = (result as Record<string, unknown>)["two_factor_authentication_enabled"];
  if (flag === true) return "enabled";
  if (flag === false) return "disabled";
  return "unknown";
};

export const renderMfaStatus = (status: MfaStatus): string => {
  switch (status) {
    case "enabled":
      return "MFA 检查：账号已开启多因素认证。";
    case "disabled":
      return "MFA 检查：警告——账号未开启多因素认证，请立即在 Cloudflare 控制台开启。";
    case "unknown":
      return "MFA 检查：无法确认（API 令牌无权限或网络失败）。请手工确认账号已开启 MFA。";
  }
};

export const redactSecrets = (text: string, secrets: readonly string[]): string => {
  let output = text;
  for (const secret of secrets) {
    if (secret.length === 0) continue;
    output = output.replaceAll(secret, "[已脱敏]");
  }
  return output;
};

export interface HealthCheckResult {
  ok: boolean;
  reason: string | null;
}

export const checkHealth = (
  payload: unknown,
  expectedContractVersion: string,
  expectedSchemaVersion: string,
): HealthCheckResult => {
  if (typeof payload !== "object" || payload === null) {
    return { ok: false, reason: "健康检测响应不是合法 JSON 对象" };
  }
  const body = payload as Record<string, unknown>;
  if (body["status"] !== "ok") {
    return { ok: false, reason: "服务状态不是 ok" };
  }
  if (body["contract_version"] !== expectedContractVersion) {
    return { ok: false, reason: "contract_version 与本地契约不一致" };
  }
  if (body["schema_version"] !== expectedSchemaVersion) {
    return { ok: false, reason: "schema 版本与本地迁移不一致，请先应用迁移" };
  }
  return { ok: true, reason: null };
};

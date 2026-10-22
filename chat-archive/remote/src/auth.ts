import type { Env } from "./index";
import { errorResponse } from "./http";
import { constantTimeEqual, parseBearerToken, sha256Hex } from "./security";
import { parseClientInfo } from "./clientInfo";

export interface AuthContext {
  deviceId: string;
  archiveId: string;
}

interface DeviceRow {
  id: string;
  archive_id: string;
  secret_hash: string;
  status: string;
}

const unauthorized = (): Response =>
  errorResponse("UNAUTHORIZED", "设备令牌缺失或无效，请重新配对设备。");

export const authenticate = async (
  env: Env,
  request: Request,
  ctx: ExecutionContext,
): Promise<AuthContext | Response> => {
  const token = parseBearerToken(request.headers.get("Authorization"));
  if (token === null) {
    return unauthorized();
  }
  const device = await env.DB.prepare(
    "SELECT id, archive_id, secret_hash, status FROM devices WHERE id = ?",
  )
    .bind(token.deviceId)
    .first<DeviceRow>();
  if (device === null) {
    return unauthorized();
  }
  const presentedHash = await sha256Hex(token.secret);
  if (!constantTimeEqual(presentedHash, device.secret_hash)) {
    return unauthorized();
  }
  if (device.status !== "active") {
    return unauthorized();
  }
  ctx.waitUntil(
    env.DB.prepare("UPDATE devices SET last_seen_at = ? WHERE id = ?")
      .bind(Math.floor(Date.now() / 1000), device.id)
      .run(),
  );
  const info = parseClientInfo(request.headers.get("X-Chat-Archive-Client"));
  const archiveInPath = new URL(request.url).pathname.match(/\/api\/v1\/archives\/([^/]+)\//)?.[1];
  if (info !== null && archiveInPath === device.archive_id) {
    ctx.waitUntil(env.DB.prepare(
      `INSERT INTO device_client_info (device_id, computer_name, platform, client_name)
       SELECT id, ?, ?, ? FROM devices WHERE id = ? AND archive_id = ? AND status = 'active'
       ON CONFLICT(device_id) DO UPDATE SET computer_name = excluded.computer_name,
         platform = excluded.platform, client_name = excluded.client_name
       WHERE device_client_info.computer_name != excluded.computer_name
          OR device_client_info.platform != excluded.platform
          OR device_client_info.client_name != excluded.client_name`,
    ).bind(info.computer_name, info.platform, info.client_name, device.id, device.archive_id).run());
  }
  return { deviceId: device.id, archiveId: device.archive_id };
};

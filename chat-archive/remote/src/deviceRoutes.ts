import type { AuthContext } from "./auth";
import type { Env } from "./index";
import { errorResponse, jsonResponse } from "./http";
import { constantTimeEqual, sha256Hex } from "./security";

const PAIRING_CODE_TTL_SECONDS = 600;
const PAIRING_MAX_FAILED_ATTEMPTS = 8;
const MAX_DEVICE_NAME_LENGTH = 64;
// 配对码固定为 32 位 id + "." + 64 位 secret（97 字符），128 留出余量。
const MAX_PAIRING_CODE_LENGTH = 128;

const nowSeconds = (): number => Math.floor(Date.now() / 1000);

const randomSecret = (): string => {
  const bytes = crypto.getRandomValues(new Uint8Array(32));
  return [...bytes].map((byte) => byte.toString(16).padStart(2, "0")).join("");
};

const randomOpaqueId = (): string => crypto.randomUUID().replaceAll("-", "");

const notFound = (): Response => errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");

const pairingRejected = (): Response =>
  errorResponse("UNAUTHORIZED", "配对码无效或已过期，请重新获取。");

const requireOwnArchive = (auth: AuthContext, archiveId: string): Response | null =>
  auth.archiveId === archiveId ? null : notFound();

const audit = (
  env: Env,
  archiveId: string,
  event: string,
  actorDeviceId: string | null,
  targetId: string | null,
): D1PreparedStatement =>
  env.DB.prepare(
    `INSERT INTO security_audit (archive_id, event, actor_device_id, target_id, created_at)
     VALUES (?, ?, ?, ?, ?)`,
  ).bind(archiveId, event, actorDeviceId, targetId, nowSeconds());

export const handlePairingCodeCreate = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const pairingCodeId = randomOpaqueId();
  const secret = randomSecret();
  const expiresAt = nowSeconds() + PAIRING_CODE_TTL_SECONDS;
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO pairing_codes
         (id, archive_id, secret_hash, created_by_device_id, status,
          max_uses, used_count, failed_attempts, expires_at, created_at)
       VALUES (?, ?, ?, ?, 'active', 1, 0, 0, ?, ?)`,
    ).bind(pairingCodeId, archiveId, await sha256Hex(secret), auth.deviceId, expiresAt, nowSeconds()),
    audit(env, archiveId, "pairing_code_created", auth.deviceId, pairingCodeId),
  ]);
  return jsonResponse({
    pairing_code: `${pairingCodeId}.${secret}`,
    pairing_code_id: pairingCodeId,
    expires_at: expiresAt,
  });
};

interface PairingCodeRow {
  id: string;
  archive_id: string;
  secret_hash: string;
  status: string;
  max_uses: number;
  used_count: number;
  failed_attempts: number;
  expires_at: number;
}

interface PairRequestBody {
  pairing_code: string;
  device_name: string;
}

const parsePairBody = (payload: unknown): PairRequestBody | null => {
  if (typeof payload !== "object" || payload === null) return null;
  const body = payload as Record<string, unknown>;
  const pairingCode = body["pairing_code"];
  const deviceName = body["device_name"];
  if (
    typeof pairingCode !== "string" ||
    pairingCode.length === 0 ||
    pairingCode.length > MAX_PAIRING_CODE_LENGTH
  ) {
    return null;
  }
  if (
    typeof deviceName !== "string" ||
    deviceName.trim().length === 0 ||
    deviceName.length > MAX_DEVICE_NAME_LENGTH
  ) {
    return null;
  }
  return { pairing_code: pairingCode, device_name: deviceName.trim() };
};

export const handlePair = async (env: Env, request: Request): Promise<Response> => {
  let payload: unknown;
  try {
    payload = await request.json();
  } catch {
    return errorResponse("QUERY_INVALID", "请求体必须是合法 JSON。");
  }
  const body = parsePairBody(payload);
  if (body === null) {
    return errorResponse("QUERY_INVALID", "pairing_code 与 device_name 是必填字段。");
  }
  const separator = body.pairing_code.indexOf(".");
  if (separator <= 0 || separator === body.pairing_code.length - 1) {
    return pairingRejected();
  }
  const pairingCodeId = body.pairing_code.slice(0, separator);
  const secret = body.pairing_code.slice(separator + 1);
  const row = await env.DB.prepare(
    `SELECT id, archive_id, secret_hash, status, max_uses, used_count, failed_attempts, expires_at
     FROM pairing_codes WHERE id = ?`,
  )
    .bind(pairingCodeId)
    .first<PairingCodeRow>();
  if (row === null) {
    return pairingRejected();
  }
  const presentedHash = await sha256Hex(secret);
  const secretMatches = constantTimeEqual(presentedHash, row.secret_hash);
  const usable =
    row.status === "active" &&
    row.expires_at > nowSeconds() &&
    row.used_count < row.max_uses &&
    row.failed_attempts < PAIRING_MAX_FAILED_ATTEMPTS;
  if (!secretMatches || !usable) {
    if (!secretMatches && row.status === "active") {
      await env.DB.prepare(
        `UPDATE pairing_codes
         SET failed_attempts = failed_attempts + 1,
             status = CASE WHEN failed_attempts + 1 >= ? THEN 'revoked' ELSE status END
         WHERE id = ?`,
      )
        .bind(PAIRING_MAX_FAILED_ATTEMPTS, row.id)
        .run();
    }
    return pairingRejected();
  }
  const consumed = await env.DB.prepare(
    `UPDATE pairing_codes
     SET used_count = used_count + 1,
         status = CASE WHEN used_count + 1 >= max_uses THEN 'used' ELSE status END
     WHERE id = ? AND status = 'active' AND used_count = ?`,
  )
    .bind(row.id, row.used_count)
    .run();
  if (consumed.meta.changes !== 1) {
    return pairingRejected();
  }
  const deviceId = `dev_${randomOpaqueId()}`;
  const deviceSecret = randomSecret();
  await env.DB.batch([
    env.DB.prepare(
      `INSERT INTO devices (id, archive_id, display_name, secret_hash, status, created_at, last_seen_at)
       VALUES (?, ?, ?, ?, 'active', ?, NULL)`,
    ).bind(deviceId, row.archive_id, body.device_name, await sha256Hex(deviceSecret), nowSeconds()),
    audit(env, row.archive_id, "device_paired", deviceId, row.id),
  ]);
  return jsonResponse({
    archive_id: row.archive_id,
    device_id: deviceId,
    device_token: `${deviceId}.${deviceSecret}`,
  });
};

export const handleDeviceRevoke = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  deviceId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const target = await env.DB.prepare(
    "SELECT id FROM devices WHERE id = ? AND archive_id = ?",
  )
    .bind(deviceId, archiveId)
    .first<{ id: string }>();
  if (target === null) {
    return notFound();
  }
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE devices SET status = 'revoked', revoked_at = ? WHERE id = ? AND archive_id = ?",
    ).bind(nowSeconds(), deviceId, archiveId),
    audit(env, archiveId, "device_revoked", auth.deviceId, deviceId),
  ]);
  return jsonResponse({ device_id: deviceId, status: "revoked" });
};

export const handleDevicesRevokeAll = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const results = await env.DB.batch([
    env.DB.prepare(
      "UPDATE devices SET status = 'revoked', revoked_at = ? WHERE archive_id = ? AND status = 'active'",
    ).bind(nowSeconds(), archiveId),
    env.DB.prepare(
      "UPDATE pairing_codes SET status = 'revoked' WHERE archive_id = ? AND status = 'active'",
    ).bind(archiveId),
    audit(env, archiveId, "devices_revoked_all", auth.deviceId, null),
  ]);
  const revokedDevices = results[0]?.meta.changes ?? 0;
  return jsonResponse({ revoked_devices: revokedDevices });
};

export const handleTokenRotate = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const deviceSecret = randomSecret();
  await env.DB.batch([
    env.DB.prepare(
      "UPDATE devices SET secret_hash = ? WHERE id = ? AND archive_id = ? AND status = 'active'",
    ).bind(await sha256Hex(deviceSecret), auth.deviceId, archiveId),
    audit(env, archiveId, "token_rotated", auth.deviceId, auth.deviceId),
  ]);
  return jsonResponse({
    device_id: auth.deviceId,
    device_token: `${auth.deviceId}.${deviceSecret}`,
  });
};

import type { AuthContext } from "./auth";
import { CONTRACT_VERSION, SCHEMA_VERSION } from "./deployPlan";
import type { Env } from "./index";
import { errorResponse, jsonResponse } from "./http";
import releaseVersions from "../../backend/src/chat_archive/release_versions.json";

export { CONTRACT_VERSION, SCHEMA_VERSION };

export const handleHealth = (): Response =>
  jsonResponse({
    status: "ok",
    server_version: releaseVersions.server,
    runtime_profile: "workers",
    contract_version: CONTRACT_VERSION,
    schema_version: SCHEMA_VERSION,
    // This runtime remains on the v1 record protocol; never offer v14 media publication.
    record_model_version: 1,
  });

const requireOwnArchive = (auth: AuthContext, archiveId: string): Response | null =>
  auth.archiveId === archiveId
    ? null
    : errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");

interface StatsRow {
  message_count: number;
  conversation_count: number;
  last_server_sequence: number;
}

export const handleArchiveStats = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const row = await env.DB.prepare(
    `SELECT message_count, conversation_count, last_server_sequence
     FROM archive_stats WHERE archive_id = ?`,
  )
    .bind(archiveId)
    .first<StatsRow>();
  return jsonResponse({
    message_count: row?.message_count ?? 0,
    conversation_count: row?.conversation_count ?? 0,
    last_server_sequence: row?.last_server_sequence ?? 0,
  });
};

interface DeviceListRow {
  computer_name: string | null;
  platform: string | null;
  client_name: string | null;
  id: string;
  display_name: string;
  status: string;
  created_at: number;
  last_seen_at: number | null;
  revoked_at: number | null;
  last_local_sequence: number;
}

interface SyncHealthRow {
  last_uploaded_local_sequence: number;
  last_server_sequence: number;
  remote_used_bytes: number;
  remote_blob_count: number;
  pending_attachment_count: number;
  conflict_count: number;
  last_success_at: number | null;
}

export const handleSyncHealth = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const row = await env.DB.prepare(
    `SELECT
       COALESCE((SELECT MAX(last_local_sequence) FROM device_cursors
                 WHERE archive_id = ? AND device_id = ?), 0)
         AS last_uploaded_local_sequence,
       COALESCE((SELECT last_server_sequence FROM archive_stats
                 WHERE archive_id = ?), 0) AS last_server_sequence,
       COALESCE((SELECT SUM(size) FROM blob_manifests
                 WHERE archive_id = ?), 0) AS remote_used_bytes,
       COALESCE((SELECT COUNT(*) FROM blob_manifests
                 WHERE archive_id = ?), 0) AS remote_blob_count,
       COALESCE((SELECT COUNT(*) FROM blob_manifests
                 WHERE archive_id = ? AND state <> 'remote_ready'), 0)
         AS pending_attachment_count,
       (SELECT COUNT(*) FROM record_conflicts WHERE archive_id = ?)
         AS conflict_count,
       (SELECT last_seen_at FROM devices WHERE archive_id = ? AND id = ?)
         AS last_success_at`,
  )
    .bind(
      archiveId,
      auth.deviceId,
      archiveId,
      archiveId,
      archiveId,
      archiveId,
      archiveId,
      archiveId,
      auth.deviceId,
    )
    .first<SyncHealthRow>();
  let objectStorageStatus = "healthy";
  try {
    await env.BLOBS.list({ limit: 1 });
  } catch {
    objectStorageStatus = "degraded";
  }
  return jsonResponse({
    archive_id: archiveId,
    backend_type: "cloudflare_d1_r2",
    device_id: auth.deviceId,
    last_uploaded_local_sequence: row?.last_uploaded_local_sequence ?? 0,
    last_server_sequence: row?.last_server_sequence ?? 0,
    remote_used_bytes: row?.remote_used_bytes ?? 0,
    remote_blob_count: row?.remote_blob_count ?? 0,
    pending_attachment_count: row?.pending_attachment_count ?? 0,
    conflict_count: row?.conflict_count ?? 0,
    // row 为空(设备未知)或 last_success_at 为空(配对后从未同步过)都必须诚实
    // 报告 null——伪造一个"现在"会让前端把"从未同步"显示成"刚同步过"。
    last_success_at: row?.last_success_at ?? null,
    last_reconciled_at: null,
    last_recovery_verified_at: null,
    remote_service_status: "healthy",
    object_storage_status: objectStorageStatus,
  });
};

export const handleArchiveDevices = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
): Promise<Response> => {
  const denied = requireOwnArchive(auth, archiveId);
  if (denied !== null) return denied;
  const rows = await env.DB.prepare(
    `SELECT d.id, d.display_name, d.status, d.created_at, d.last_seen_at, d.revoked_at,
            i.computer_name, i.platform, i.client_name,
            COALESCE((SELECT MAX(c.last_local_sequence) FROM device_cursors c
                      WHERE c.archive_id = d.archive_id AND c.device_id = d.id), 0)
              AS last_local_sequence
     FROM devices d LEFT JOIN device_client_info i ON i.device_id = d.id
     WHERE d.archive_id = ? ORDER BY d.created_at ASC, d.id ASC`,
  )
    .bind(archiveId)
    .all<DeviceListRow>();
  return jsonResponse({ items: rows.results.map(({ computer_name, platform, client_name, ...row }) => ({
    ...row,
    client_info: computer_name === null ? null : { computer_name, platform, client_name },
  })) });
};

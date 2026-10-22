import type { AuthContext } from "./auth";
import type { Env } from "./index";
import { errorResponse, jsonResponse } from "./http";
import {
  normalizeSearchText,
  planCommit,
  validateBatch,
  type CommitPlan,
  type SyncBatchBody,
  type SyncRecordInput,
} from "./syncPlan";

interface BatchReceipt {
  client_batch_id: string;
  server_sequence_start: number;
  server_sequence_end: number;
  inserted_count: number;
  updated_count: number;
  conflict_count: number;
}

interface ReceiptRow {
  client_batch_id: string;
  payload_sha256: string;
  server_sequence_start: number;
  server_sequence_end: number;
  inserted_count: number;
  updated_count: number;
  conflict_count: number;
}

const conflict = (message: string): Response =>
  errorResponse("SYNC_CONFLICT", message);

const invalid = (message: string): Response => errorResponse("QUERY_INVALID", message);

const findReceipt = async (
  env: Env,
  archiveId: string,
  deviceId: string,
  clientBatchId: string,
): Promise<ReceiptRow | null> =>
  env.DB.prepare(
    `SELECT client_batch_id, payload_sha256, server_sequence_start,
            server_sequence_end, inserted_count, updated_count, conflict_count
     FROM sync_batches
     WHERE archive_id = ? AND device_id = ? AND client_batch_id = ?`,
  )
    .bind(archiveId, deviceId, clientBatchId)
    .first<ReceiptRow>();

const receiptPayload = (row: ReceiptRow): BatchReceipt => ({
  client_batch_id: row.client_batch_id,
  server_sequence_start: row.server_sequence_start,
  server_sequence_end: row.server_sequence_end,
  inserted_count: row.inserted_count,
  updated_count: row.updated_count,
  conflict_count: row.conflict_count,
});

const isSyncBatchBody = (value: unknown): value is SyncBatchBody => {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  if (
    typeof body.client_batch_id !== "string" ||
    typeof body.source_account_id !== "string" ||
    body.source_account_id.length === 0 ||
    typeof body.local_sequence_start !== "number" ||
    typeof body.local_sequence_end !== "number" ||
    typeof body.source_instance_id !== "string" ||
    typeof body.schema_version !== "string" ||
    typeof body.payload_sha256 !== "string" ||
    !Array.isArray(body.records)
  ) {
    return false;
  }
  return body.records.every((record: unknown) => {
    if (typeof record !== "object" || record === null) return false;
    const item = record as Record<string, unknown>;
    return (
      typeof item.record_key === "string" &&
      typeof item.conversation_id === "string" &&
      typeof item.created_at === "number" &&
      typeof item.content_text === "string" &&
      typeof item.content_sha256 === "string" &&
      (item.source_instance_id === undefined || typeof item.source_instance_id === "string") &&
      (item.schema_version === undefined || typeof item.schema_version === "string")
    );
  });
};

export const buildCommitStatements = (
  env: Env,
  archiveId: string,
  deviceId: string,
  body: SyncBatchBody,
  observedSequence: number,
  plan: CommitPlan,
): D1PreparedStatement[] => {
  // 守卫：所有写只在归档序号仍等于读阶段观测值时生效；并发写入者已推进则整批
  // no-op（自增放最后、按 observedSequence 守卫，不再借用后写入的 sequenceEnd）。
  // 新归档尚无 archive_sequences 行，用 COALESCE 归零匹配 observedSequence=0。
  const guard =
    "COALESCE((SELECT last_server_sequence FROM archive_sequences WHERE archive_id = ?), 0) = ?";
  const statements: D1PreparedStatement[] = [];
  for (const item of plan.changed) {
    statements.push(
      env.DB.prepare(
         `INSERT INTO archive_records(
             archive_id, record_key, conversation_id, created_at,
             content_text, normalized_content, content_sha256,
             source_instance_id, schema_version, origin_device_id, server_sequence
         )
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
         WHERE ${guard}
         ON CONFLICT(archive_id, record_key) DO UPDATE SET
             conversation_id = excluded.conversation_id,
             created_at = excluded.created_at,
             content_text = excluded.content_text,
             normalized_content = excluded.normalized_content,
             content_sha256 = excluded.content_sha256,
             source_instance_id = excluded.source_instance_id,
             schema_version = excluded.schema_version,
             origin_device_id = excluded.origin_device_id,
             server_sequence = excluded.server_sequence`,
      ).bind(
        archiveId,
        item.record.record_key,
        item.record.conversation_id,
        item.record.created_at,
        item.record.content_text,
        normalizeSearchText(item.record.content_text),
        item.record.content_sha256,
        item.record.source_instance_id ?? body.source_instance_id,
        item.record.schema_version ?? body.schema_version,
        deviceId,
        item.serverSequence,
        archiveId,
        observedSequence,
      ),
    );
  }
  for (const item of plan.conflicts) {
    statements.push(
      env.DB.prepare(
        `INSERT INTO record_conflicts(
             archive_id, server_sequence, record_key,
             canonical_content_sha256, incoming_content_sha256,
             canonical_device_id, incoming_device_id,
             canonical_source_instance_id, incoming_source_instance_id,
             incoming_schema_version, detected_at
         )
         SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
         WHERE ${guard}`,
      ).bind(
        archiveId,
        item.serverSequence,
        item.record_key,
        item.canonical_content_sha256,
        item.incoming_content_sha256,
        item.canonical_device_id,
        item.incoming_device_id,
        item.canonical_source_instance_id,
        item.incoming_source_instance_id,
        item.incoming_schema_version,
        item.detected_at,
        archiveId,
        observedSequence,
      ),
    );
  }
  statements.push(
    env.DB.prepare(
      `INSERT INTO device_cursors(
           archive_id, device_id, source_account_id, last_local_sequence
       )
       SELECT ?, ?, ?, ?
       WHERE ${guard}
       ON CONFLICT(archive_id, device_id, source_account_id) DO UPDATE SET
           last_local_sequence = excluded.last_local_sequence`,
    ).bind(
      archiveId,
      deviceId,
      body.source_account_id,
      body.local_sequence_end,
      archiveId,
      observedSequence,
    ),
    env.DB.prepare(
      `INSERT INTO sync_batches(
           archive_id, device_id, source_account_id, client_batch_id, payload_sha256,
           server_sequence_start, server_sequence_end, inserted_count, updated_count,
           conflict_count
       )
       SELECT ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
       WHERE ${guard}`,
    ).bind(
      archiveId,
      deviceId,
      body.source_account_id,
      body.client_batch_id,
      body.payload_sha256,
      plan.sequenceStart,
      plan.sequenceEnd,
      plan.insertedCount,
      plan.updatedCount,
      plan.conflicts.length,
      archiveId,
      observedSequence,
    ),
    env.DB.prepare(
      `INSERT INTO archive_stats(
           archive_id, message_count, conversation_count, last_server_sequence
       )
       SELECT ?, COUNT(*), COUNT(DISTINCT conversation_id), ?
       FROM archive_records WHERE archive_id = ?
       HAVING ${guard}
       ON CONFLICT(archive_id) DO UPDATE SET
           message_count = excluded.message_count,
           conversation_count = excluded.conversation_count,
           last_server_sequence = excluded.last_server_sequence`,
    ).bind(archiveId, plan.sequenceEnd, archiveId, archiveId, observedSequence),
  );
  // 自增放最后：CAS observedSequence→sequenceEnd；新归档无行时直接 INSERT 建行。
  statements.push(
    env.DB.prepare(
      `INSERT INTO archive_sequences(archive_id, last_server_sequence)
       VALUES (?, ?)
       ON CONFLICT(archive_id) DO UPDATE SET
           last_server_sequence = excluded.last_server_sequence
       WHERE archive_sequences.last_server_sequence = ?`,
    ).bind(archiveId, plan.sequenceEnd, observedSequence),
  );
  return statements;
};

export const handleSyncCommit = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  request: Request,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  let parsed: unknown;
  try {
    parsed = await request.json();
  } catch {
    return invalid("请求体必须是合法 JSON。");
  }
  if (!isSyncBatchBody(parsed)) {
    return invalid("请求体缺少必要字段或类型不正确。");
  }
  const failure = await validateBatch(parsed);
  if (failure !== null) {
    return invalid(failure);
  }

  const prior = await findReceipt(env, archiveId, auth.deviceId, parsed.client_batch_id);
  if (prior !== null) {
    if (prior.payload_sha256 !== parsed.payload_sha256) {
      return conflict("同一批次标识携带了不同的负载。");
    }
    return jsonResponse(receiptPayload(prior));
  }

  const cursorRow = await env.DB.prepare(
    "SELECT last_local_sequence FROM device_cursors " +
      "WHERE archive_id = ? AND device_id = ? AND source_account_id = ?",
  )
    .bind(archiveId, auth.deviceId, parsed.source_account_id)
    .first<{ last_local_sequence: number }>();
  const expectedStart = cursorRow === null ? 1 : cursorRow.last_local_sequence + 1;
  if (parsed.local_sequence_start !== expectedStart) {
    return conflict("设备本地序号窗口不连续，请从上次确认位置继续。");
  }

  const sequenceRow = await env.DB.prepare(
    "SELECT last_server_sequence FROM archive_sequences WHERE archive_id = ?",
  )
    .bind(archiveId)
    .first<{ last_server_sequence: number }>();
  const observedSequence = sequenceRow?.last_server_sequence ?? 0;

  // 服务端归一：来源实例/schema 版本只作为存储与诊断信息；冲突判据始终使用
  // authenticate 得到的 deviceId，绝不信任客户端可声明的来源实例。
  //
  // 逐字段重建而不是展开 `...record`：v1 线格式之外夹带的字段（尤其是
  // `message_type`——`planCommit` 会据它把跨设备分歧静默掉）既没经过
  // `isSyncBatchBody` 校验也不在校验和里，不可信，一律不进计划。
  const effectiveRecords = parsed.records.map(
    (record): SyncRecordInput => ({
      record_key: record.record_key,
      conversation_id: record.conversation_id,
      created_at: record.created_at,
      content_text: record.content_text,
      content_sha256: record.content_sha256,
      source_instance_id: record.source_instance_id ?? parsed.source_instance_id,
      schema_version: record.schema_version ?? parsed.schema_version,
    }),
  );

  const keys = effectiveRecords.map((record) => record.record_key);
  const placeholders = keys.map(() => "?").join(", ");
  const checksumRows = await env.DB.prepare(
    `SELECT record_key, content_sha256, source_instance_id, origin_device_id FROM archive_records
     WHERE archive_id = ? AND record_key IN (${placeholders})`,
  )
    .bind(archiveId, ...keys)
    .all<{
      record_key: string;
      content_sha256: string;
      source_instance_id: string;
      origin_device_id: string | null;
    }>();
  const existing = new Map(
    checksumRows.results.map(
      (row) =>
        [
          row.record_key,
          {
            content_sha256: row.content_sha256,
            origin_device_id: row.origin_device_id,
            source_instance_id: row.source_instance_id,
          },
        ] as const,
    ),
  );

  const plan = planCommit(
    effectiveRecords,
    existing,
    observedSequence,
    auth.deviceId,
    Math.floor(Date.now() / 1000),
  );
  try {
    await env.DB.batch(
      buildCommitStatements(env, archiveId, auth.deviceId, parsed, observedSequence, plan),
    );
  } catch {
    const retry = await findReceipt(env, archiveId, auth.deviceId, parsed.client_batch_id);
    if (retry !== null && retry.payload_sha256 === parsed.payload_sha256) {
      return jsonResponse(receiptPayload(retry));
    }
    return conflict("提交批次时发生冲突，请重试。");
  }
  const committed = await findReceipt(env, archiveId, auth.deviceId, parsed.client_batch_id);
  if (committed === null) {
    return conflict("并发写入者推进了归档序号，请重试本批次。");
  }
  return jsonResponse(receiptPayload(committed));
};

const MAX_RECORD_PAGE = 500;

export const handleSyncRecords = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  url: URL,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  const afterRaw = url.searchParams.get("after_server_sequence") ?? "0";
  const limitRaw = url.searchParams.get("limit") ?? "100";
  const after = Number(afterRaw);
  const limit = Number(limitRaw);
  if (!Number.isSafeInteger(after) || after < 0) {
    return invalid("after_server_sequence 必须是非负整数。");
  }
  if (!Number.isSafeInteger(limit) || limit <= 0 || limit > MAX_RECORD_PAGE) {
    return invalid(`limit 必须在 1 到 ${String(MAX_RECORD_PAGE)} 之间。`);
  }
  const rows = await env.DB.prepare(
    `SELECT record_key, conversation_id, created_at, content_text,
            content_sha256, source_instance_id, schema_version, server_sequence
     FROM archive_records
     WHERE archive_id = ? AND server_sequence > ?
     ORDER BY server_sequence, record_key
     LIMIT ?`,
  )
    .bind(archiveId, after, limit + 1)
    .all<{
      record_key: string;
      conversation_id: string;
      created_at: number;
      content_text: string;
      content_sha256: string;
      source_instance_id: string;
      schema_version: string;
      server_sequence: number;
    }>();
  const hasMore = rows.results.length > limit;
  const items = rows.results.slice(0, limit);
  const nextSequence =
    items.length === 0 ? after : (items[items.length - 1]?.server_sequence ?? after);
  return jsonResponse({
    items,
    next_server_sequence: nextSequence,
    has_more: hasMore,
  });
};

export const handleSyncConflicts = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  url: URL,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  const afterRaw = url.searchParams.get("after_server_sequence") ?? "0";
  const limitRaw = url.searchParams.get("limit") ?? "100";
  const after = Number(afterRaw);
  const limit = Number(limitRaw);
  if (!Number.isSafeInteger(after) || after < 0) {
    return invalid("after_server_sequence 必须是非负整数。");
  }
  if (!Number.isSafeInteger(limit) || limit <= 0 || limit > MAX_RECORD_PAGE) {
    return invalid(`limit 必须在 1 到 ${String(MAX_RECORD_PAGE)} 之间。`);
  }
  const rows = await env.DB.prepare(
     `SELECT server_sequence, record_key, canonical_content_sha256,
             incoming_content_sha256, canonical_device_id, incoming_device_id,
             canonical_source_instance_id, incoming_source_instance_id,
             incoming_schema_version, detected_at
     FROM record_conflicts
     WHERE archive_id = ? AND server_sequence > ?
     ORDER BY server_sequence
     LIMIT ?`,
  )
    .bind(archiveId, after, limit + 1)
    .all<{
      server_sequence: number;
      record_key: string;
      canonical_content_sha256: string;
      incoming_content_sha256: string;
      canonical_device_id: string | null;
      incoming_device_id: string | null;
      canonical_source_instance_id: string | null;
      incoming_source_instance_id: string | null;
      incoming_schema_version: string | null;
      detected_at: number;
    }>();
  const hasMore = rows.results.length > limit;
  const items = rows.results.slice(0, limit);
  const nextSequence =
    items.length === 0 ? after : (items[items.length - 1]?.server_sequence ?? after);
  return jsonResponse({
    items,
    next_server_sequence: nextSequence,
    has_more: hasMore,
  });
};

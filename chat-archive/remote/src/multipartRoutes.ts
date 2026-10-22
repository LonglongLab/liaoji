import type { AuthContext } from "./auth";
import { errorResponse, jsonResponse } from "./http";
import type { Env } from "./index";
import { constantTimeEqual, hexBytes, sha256Bytes, sha256Hex } from "./security";

export const MIN_MULTIPART_PART_BYTES = 5 * 1024 * 1024;
export const MAX_MULTIPART_PART_BYTES = 16 * 1024 * 1024;
export const MAX_MULTIPART_PARTS = 10_000;
const MAX_PLAN_BODY_BYTES = 2 * 1024 * 1024;
const CAPABILITY_TTL_SECONDS = 15 * 60;
const SHA256_HEX = /^[0-9a-f]{64}$/;
const CLIENT_UPLOAD_ID = /^[A-Za-z0-9_-]{1,128}$/;

interface PlannedPart {
  part_number: number;
  size: number;
  sha256: string;
}

interface UploadPlanBody {
  client_upload_id: string;
  part_size: number;
  parts: PlannedPart[];
}

interface ManifestRow {
  size: number;
  sha256: string;
  state: string;
  object_version: string | null;
  verified_at: number | null;
}

interface UploadRow {
  archive_id: string;
  blob_id: string;
  device_id: string;
  client_upload_id: string;
  session_id: string;
  r2_upload_id: string;
  temp_key: string;
  object_key: string;
  total_size: number;
  part_size: number;
  part_count: number;
  sha256: string;
  plan_sha256: string;
  state: string;
  object_version: string | null;
}

interface PartRow {
  part_number: number;
  expected_size: number;
  expected_sha256: string;
  etag: string | null;
  completed_at: number | null;
  capability_hash: string | null;
  capability_expires_at: number | null;
}

interface PartUploadRow extends PartRow {
  r2_upload_id: string;
  temp_key: string;
  state: string;
}

const nowSeconds = (): number => Math.floor(Date.now() / 1000);

const notFound = (): Response =>
  errorResponse("NOT_FOUND", "上传不存在或不属于当前设备。");

const invalid = (message: string): Response => errorResponse("QUERY_INVALID", message);

const conflict = (message: string, retryable = false): Response =>
  errorResponse("BLOB_CONFLICT", message, retryable);

const integrity = (message: string): Response => errorResponse("BLOB_INTEGRITY", message);

const randomOpaqueId = (): string => crypto.randomUUID().replaceAll("-", "");

const readBoundedJson = async (request: Request): Promise<unknown> => {
  if (request.body === null) throw new Error("missing body");
  const reader = request.body.getReader() as ReadableStreamDefaultReader<Uint8Array>;
  const chunks: Uint8Array[] = [];
  let total = 0;
  let result = await reader.read();
  while (!result.done) {
    total += result.value.byteLength;
    if (total > MAX_PLAN_BODY_BYTES) {
      await reader.cancel();
      throw new Error("body too large");
    }
    chunks.push(result.value);
    result = await reader.read();
  }
  const combined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    combined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return JSON.parse(new TextDecoder().decode(combined)) as unknown;
};

const isPlanBody = (value: unknown): value is UploadPlanBody => {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  if (
    typeof body.client_upload_id !== "string" ||
    typeof body.part_size !== "number" ||
    !Array.isArray(body.parts)
  ) {
    return false;
  }
  return body.parts.every((part: unknown) => {
    if (typeof part !== "object" || part === null) return false;
    const item = part as Record<string, unknown>;
    return (
      typeof item.part_number === "number" &&
      typeof item.size === "number" &&
      typeof item.sha256 === "string"
    );
  });
};

const planFailure = (body: UploadPlanBody, manifest: ManifestRow): string | null => {
  if (!CLIENT_UPLOAD_ID.test(body.client_upload_id)) {
    return "client_upload_id 格式非法。";
  }
  if (
    !Number.isSafeInteger(body.part_size) ||
    body.part_size < MIN_MULTIPART_PART_BYTES ||
    body.part_size > MAX_MULTIPART_PART_BYTES
  ) {
    return `part_size 必须在 ${String(MIN_MULTIPART_PART_BYTES)} 到 ${String(MAX_MULTIPART_PART_BYTES)} 字节之间。`;
  }
  if (body.parts.length === 0 || body.parts.length > MAX_MULTIPART_PARTS) {
    return `part 数量必须在 1 到 ${String(MAX_MULTIPART_PARTS)} 之间。`;
  }
  const expectedCount = Math.ceil(manifest.size / body.part_size);
  if (body.parts.length !== expectedCount) return "part 数量与 manifest size 不一致。";
  let total = 0;
  for (let index = 0; index < body.parts.length; index += 1) {
    const part = body.parts[index];
    if (part === undefined) return "part 规划不完整。";
    const isLast = index === body.parts.length - 1;
    const expectedSize = isLast ? manifest.size - body.part_size * index : body.part_size;
    if (
      part.part_number !== index + 1 ||
      !Number.isSafeInteger(part.size) ||
      part.size !== expectedSize ||
      part.size <= 0 ||
      !SHA256_HEX.test(part.sha256)
    ) {
      return "part 编号、大小或 checksum 与固定规划不一致。";
    }
    total += part.size;
  }
  return total === manifest.size ? null : "part 总大小与 manifest size 不一致。";
};

const planChecksum = (body: UploadPlanBody, manifest: ManifestRow): Promise<string> =>
  sha256Hex(
    JSON.stringify({
      client_upload_id: body.client_upload_id,
      part_size: body.part_size,
      parts: body.parts,
      sha256: manifest.sha256,
      size: manifest.size,
    }),
  );

const loadUpload = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  sessionId: string,
): Promise<UploadRow | null> =>
  env.DB.prepare(
    `SELECT archive_id, blob_id, device_id, client_upload_id, session_id,
            r2_upload_id, temp_key, object_key, total_size, part_size,
            part_count, sha256, plan_sha256, state, object_version
     FROM blob_uploads
     WHERE archive_id = ? AND blob_id = ? AND session_id = ? AND device_id = ?`,
  )
    .bind(archiveId, blobId, sessionId, auth.deviceId)
    .first<UploadRow>();

const partRows = async (env: Env, upload: UploadRow): Promise<PartRow[]> => {
  const rows = await env.DB.prepare(
    `SELECT part_number, expected_size, expected_sha256, etag, completed_at,
            capability_hash, capability_expires_at
     FROM blob_upload_parts
     WHERE archive_id = ? AND blob_id = ? AND session_id = ?
     ORDER BY part_number`,
  )
    .bind(upload.archive_id, upload.blob_id, upload.session_id)
    .all<PartRow>();
  return rows.results;
};

const uploadPayload = async (env: Env, upload: UploadRow): Promise<Response> => {
  const parts = await partRows(env, upload);
  const expiresAt = nowSeconds() + CAPABILITY_TTL_SECONDS;
  const statements: D1PreparedStatement[] = [];
  const payloadParts: unknown[] = [];
  for (const part of parts) {
    if (part.etag !== null) {
      payloadParts.push({
        part_number: part.part_number,
        size: part.expected_size,
        sha256: part.expected_sha256,
        state: "completed",
      });
      continue;
    }
    const token = await sha256Hex(`${crypto.randomUUID()}:${crypto.randomUUID()}`);
    statements.push(
      env.DB.prepare(
        `UPDATE blob_upload_parts
         SET capability_hash = ?, capability_expires_at = ?
         WHERE archive_id = ? AND blob_id = ? AND session_id = ? AND part_number = ?`,
      ).bind(
        await sha256Hex(token),
        expiresAt,
        upload.archive_id,
        upload.blob_id,
        upload.session_id,
        part.part_number,
      ),
    );
    payloadParts.push({
      part_number: part.part_number,
      size: part.expected_size,
      sha256: part.expected_sha256,
      state: "pending",
      upload_request: {
        url: `/api/v1/archives/${upload.archive_id}/blobs/${upload.blob_id}/uploads/${upload.session_id}/parts/${String(part.part_number)}`,
        method: "PUT",
        headers: {
          Authorization: `Upload ${token}`,
          "Content-Length": String(part.expected_size),
          "X-Content-SHA256": part.expected_sha256,
        },
        expires_at: expiresAt,
      },
    });
  }
  if (statements.length > 0) await env.DB.batch(statements);
  return jsonResponse({
    blob_id: upload.blob_id,
    client_upload_id: upload.client_upload_id,
    session_id: upload.session_id,
    state: upload.state,
    part_size: upload.part_size,
    total_size: upload.total_size,
    sha256: upload.sha256,
    parts: payloadParts,
  });
};

export const handleMultipartPlan = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  request: Request,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  let parsed: unknown;
  try {
    parsed = await readBoundedJson(request);
  } catch {
    return invalid("请求体必须是大小受限的合法 JSON。");
  }
  if (!isPlanBody(parsed)) return invalid("上传计划字段缺失或类型错误。");
  const manifest = await env.DB.prepare(
    `SELECT size, sha256, state, object_version, verified_at
     FROM blob_manifests WHERE archive_id = ? AND blob_id = ?`,
  )
    .bind(archiveId, blobId)
    .first<ManifestRow>();
  if (manifest === null) return errorResponse("NOT_FOUND", "附件清单不存在。");
  if (manifest.state === "remote_ready") return conflict("已验证附件不可覆盖。");
  const failure = planFailure(parsed, manifest);
  if (failure !== null) return invalid(failure);
  const checksum = await planChecksum(parsed, manifest);
  const prior = await env.DB.prepare(
    `SELECT archive_id, blob_id, device_id, client_upload_id, session_id,
            r2_upload_id, temp_key, object_key, total_size, part_size,
            part_count, sha256, plan_sha256, state, object_version
     FROM blob_uploads
     WHERE archive_id = ? AND device_id = ? AND client_upload_id = ?`,
  )
    .bind(archiveId, auth.deviceId, parsed.client_upload_id)
    .first<UploadRow>();
  if (prior !== null) {
    if (prior.blob_id !== blobId || prior.plan_sha256 !== checksum) {
      return conflict("同一 client_upload_id 携带了不同上传计划。");
    }
    return uploadPayload(env, prior);
  }

  const sessionId = randomOpaqueId();
  const tempKey = `multipart/${sessionId}`;
  const objectKey = `${archiveId}/${blobId}`;
  const multipart = await env.BLOBS.createMultipartUpload(tempKey, {
    customMetadata: { archive_id: archiveId, blob_id: blobId, sha256: manifest.sha256 },
  });
  const now = nowSeconds();
  const statements: D1PreparedStatement[] = [
    env.DB.prepare(
      `INSERT INTO blob_uploads(
           archive_id, blob_id, device_id, client_upload_id, session_id,
           r2_upload_id, temp_key, object_key, total_size, part_size,
           part_count, sha256, plan_sha256, state, created_at, updated_at
       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'planned', ?, ?)`,
    ).bind(
      archiveId,
      blobId,
      auth.deviceId,
      parsed.client_upload_id,
      sessionId,
      multipart.uploadId,
      tempKey,
      objectKey,
      manifest.size,
      parsed.part_size,
      parsed.parts.length,
      manifest.sha256,
      checksum,
      now,
      now,
    ),
    ...parsed.parts.map((part) =>
      env.DB.prepare(
        `INSERT INTO blob_upload_parts(
             archive_id, blob_id, session_id, part_number,
             expected_size, expected_sha256
         ) VALUES (?, ?, ?, ?, ?, ?)`,
      ).bind(archiveId, blobId, sessionId, part.part_number, part.size, part.sha256),
    ),
    env.DB.prepare(
      `UPDATE blob_manifests SET state = 'uploading_parts', updated_at = ?
       WHERE archive_id = ? AND blob_id = ? AND state = 'pending_upload'`,
    ).bind(now, archiveId, blobId),
  ];
  try {
    await env.DB.batch(statements);
  } catch {
    await multipart.abort();
    return conflict("上传计划发生并发冲突，请重试。", true);
  }
  const created = await loadUpload(env, auth, archiveId, blobId, sessionId);
  return created === null ? conflict("上传计划未能持久化。", true) : uploadPayload(env, created);
};

export const handleMultipartStatus = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  sessionId: string,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  const upload = await loadUpload(env, auth, archiveId, blobId, sessionId);
  return upload === null ? notFound() : uploadPayload(env, upload);
};

const uploadUnauthorized = (): Response =>
  errorResponse("UNAUTHORIZED", "上传能力缺失、过期或无效。");

export const handleMultipartPartUpload = async (
  env: Env,
  archiveId: string,
  blobId: string,
  sessionId: string,
  partNumber: number,
  request: Request,
): Promise<Response> => {
  const row = await env.DB.prepare(
    `SELECT p.part_number, p.expected_size, p.expected_sha256, p.etag,
            p.completed_at, p.capability_hash, p.capability_expires_at,
            u.r2_upload_id, u.temp_key, u.state
     FROM blob_upload_parts p
     JOIN blob_uploads u
       ON u.archive_id = p.archive_id AND u.blob_id = p.blob_id
      AND u.session_id = p.session_id
     WHERE p.archive_id = ? AND p.blob_id = ? AND p.session_id = ?
       AND p.part_number = ?`,
  )
    .bind(archiveId, blobId, sessionId, partNumber)
    .first<PartUploadRow>();
  const authorization = request.headers.get("Authorization");
  if (
    row === null ||
    authorization === null ||
    !authorization.startsWith("Upload ") ||
    row.capability_hash === null ||
    row.capability_expires_at === null ||
    row.capability_expires_at < nowSeconds()
  ) {
    return uploadUnauthorized();
  }
  const tokenHash = await sha256Hex(authorization.slice("Upload ".length));
  if (!constantTimeEqual(tokenHash, row.capability_hash)) return uploadUnauthorized();
  if (row.state === "aborted" || row.state === "integrity_failed") {
    return conflict("上传会话已结束。");
  }
  if (row.etag !== null) {
    return jsonResponse({ part_number: row.part_number, etag: row.etag, state: "completed" });
  }
  const length = Number(request.headers.get("Content-Length"));
  if (!Number.isSafeInteger(length) || length !== row.expected_size) {
    return invalid("Content-Length 与计划 part 大小不一致。");
  }
  if (request.headers.get("X-Content-SHA256") !== row.expected_sha256) {
    return integrity("part checksum header 与计划不一致。");
  }
  const body = await request.arrayBuffer();
  if (body.byteLength !== row.expected_size) return invalid("part 实际字节数与计划不一致。");
  if ((await sha256Bytes(body)) !== row.expected_sha256) {
    return integrity("part 内容 checksum 校验失败。");
  }
  let uploaded: R2UploadedPart;
  try {
    uploaded = await env.BLOBS.resumeMultipartUpload(row.temp_key, row.r2_upload_id).uploadPart(
      row.part_number,
      body,
    );
  } catch {
    return conflict("R2 part 上传暂时失败，请刷新状态后重试。", true);
  }
  const now = nowSeconds();
  await env.DB.batch([
    env.DB.prepare(
      `UPDATE blob_upload_parts
       SET etag = ?, completed_at = ?, capability_hash = NULL, capability_expires_at = NULL
       WHERE archive_id = ? AND blob_id = ? AND session_id = ? AND part_number = ?`,
    ).bind(uploaded.etag, now, archiveId, blobId, sessionId, partNumber),
    env.DB.prepare(
      `UPDATE blob_uploads SET state = 'uploading_parts', updated_at = ?
       WHERE archive_id = ? AND blob_id = ? AND session_id = ?`,
    ).bind(now, archiveId, blobId, sessionId),
  ]);
  return jsonResponse({ part_number: uploaded.partNumber, etag: uploaded.etag, state: "completed" });
};

const verifiedManifest = (blobId: string, upload: UploadRow, verifiedAt: number) => ({
  blob_id: blobId,
  size: upload.total_size,
  sha256: upload.sha256,
  state: "remote_ready",
  object_version: upload.object_version,
  verified_at: verifiedAt,
});

export const handleMultipartComplete = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  sessionId: string,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  const upload = await loadUpload(env, auth, archiveId, blobId, sessionId);
  if (upload === null) return notFound();
  const manifest = await env.DB.prepare(
    `SELECT size, sha256, state, object_version, verified_at
     FROM blob_manifests WHERE archive_id = ? AND blob_id = ?`,
  )
    .bind(archiveId, blobId)
    .first<ManifestRow>();
  if (manifest?.state === "remote_ready" && manifest.verified_at !== null) {
    return jsonResponse(verifiedManifest(blobId, { ...upload, object_version: manifest.object_version }, manifest.verified_at));
  }
  if (upload.state === "aborted" || upload.state === "integrity_failed") {
    return conflict("上传会话已结束。");
  }
  const parts = await partRows(env, upload);
  if (parts.length !== upload.part_count || parts.some((part) => part.etag === null)) {
    return conflict("仍有 part 未上传，不能完成附件。", true);
  }

  let temporary = await env.BLOBS.head(upload.temp_key);
  if (temporary === null) {
    try {
      temporary = await env.BLOBS.resumeMultipartUpload(
        upload.temp_key,
        upload.r2_upload_id,
      ).complete(
        parts.map((part) => ({ partNumber: part.part_number, etag: part.etag ?? "" })),
      );
    } catch {
      return conflict("R2 multipart 完成暂时失败，请重试。", true);
    }
  }
  if (temporary.size !== upload.total_size) {
    await env.BLOBS.delete(upload.temp_key);
    await env.DB.prepare(
      `UPDATE blob_uploads SET state = 'integrity_failed', updated_at = ?
       WHERE archive_id = ? AND blob_id = ? AND session_id = ?`,
    )
      .bind(nowSeconds(), archiveId, blobId, sessionId)
      .run();
    return integrity("multipart 完成对象大小与 manifest 不一致。");
  }

  let finalObject = await env.BLOBS.head(upload.object_key);
  if (finalObject === null) {
    const source = await env.BLOBS.get(upload.temp_key);
    if (source === null) return conflict("完成对象暂时不可读取，请重试。", true);
    try {
      finalObject = await env.BLOBS.put(upload.object_key, source.body, {
        onlyIf: { etagDoesNotMatch: "*" },
        sha256: upload.sha256,
        customMetadata: {
          archive_id: archiveId,
          blob_id: blobId,
          archive_sha256: upload.sha256,
        },
      });
    } catch {
      await env.DB.prepare(
        `UPDATE blob_uploads SET state = 'integrity_failed', updated_at = ?
         WHERE archive_id = ? AND blob_id = ? AND session_id = ?`,
      )
        .bind(nowSeconds(), archiveId, blobId, sessionId)
        .run();
      return integrity("R2 完整对象 checksum 校验失败。");
    }
    if (finalObject === null) finalObject = await env.BLOBS.head(upload.object_key);
  }
  const storedChecksum = finalObject?.checksums.sha256;
  if (
    finalObject === null ||
    finalObject.size !== upload.total_size ||
    finalObject.customMetadata?.archive_sha256 !== upload.sha256 ||
    (storedChecksum !== undefined && hexBytes(storedChecksum) !== upload.sha256)
  ) {
    return conflict("最终对象已存在但与当前 manifest 不一致。");
  }

  const verifiedAt = nowSeconds();
  // 先单独跑这条 CAS UPDATE 并检查受影响行数，而不是把它和 blob_uploads 的
  // 无条件 UPDATE 一起丢进 batch() 后完全不看结果：guard 没匹配上(size/sha256
  // 在这期间被并发的 manifest 重新注册改掉了)时，既不能删掉临时对象，也不能
  // 回一个 remote_ready——那会让 D1 记录和 R2 实际对象永久失步且无法重试。
  const manifestUpdate = await env.DB.prepare(
    `UPDATE blob_manifests
     SET state = 'remote_ready', object_version = ?, verified_at = ?, updated_at = ?
     WHERE archive_id = ? AND blob_id = ? AND size = ? AND sha256 = ?`,
  )
    .bind(
      finalObject.version,
      verifiedAt,
      verifiedAt,
      archiveId,
      blobId,
      upload.total_size,
      upload.sha256,
    )
    .run();
  if (manifestUpdate.meta.changes === 0) {
    return conflict("附件清单在完成上传前被并发修改，请刷新后重试。", true);
  }
  await env.DB.prepare(
    `UPDATE blob_uploads
     SET state = 'remote_ready', object_version = ?, updated_at = ?
     WHERE archive_id = ? AND blob_id = ? AND session_id = ?`,
  )
    .bind(finalObject.version, verifiedAt, archiveId, blobId, sessionId)
    .run();
  await env.BLOBS.delete(upload.temp_key);
  return jsonResponse(
    verifiedManifest(blobId, { ...upload, object_version: finalObject.version }, verifiedAt),
  );
};

export const handleMultipartAbort = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  sessionId: string,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  const upload = await loadUpload(env, auth, archiveId, blobId, sessionId);
  if (upload === null) return notFound();
  if (upload.state === "remote_ready") return conflict("已验证附件不能中止或覆盖。");
  try {
    await env.BLOBS.resumeMultipartUpload(upload.temp_key, upload.r2_upload_id).abort();
  } catch {
    // The R2 upload may already be completed; deleting the temporary key remains safe.
  }
  await env.BLOBS.delete(upload.temp_key);
  const now = nowSeconds();
  await env.DB.batch([
    env.DB.prepare(
      `UPDATE blob_uploads SET state = 'aborted', updated_at = ?
       WHERE archive_id = ? AND blob_id = ? AND session_id = ?`,
    ).bind(now, archiveId, blobId, sessionId),
    env.DB.prepare(
      `UPDATE blob_manifests SET state = 'pending_upload', updated_at = ?
       WHERE archive_id = ? AND blob_id = ? AND state <> 'remote_ready'`,
    ).bind(now, archiveId, blobId),
  ]);
  return jsonResponse({ blob_id: blobId, session_id: sessionId, state: "aborted" });
};

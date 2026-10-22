import type { AuthContext } from "./auth";
import type { Env } from "./index";
import { errorResponse, jsonResponse, withSecurityHeaders } from "./http";

// 小附件仍可经 Worker 代理；大附件使用 multipartRoutes 的 R2 binding 状态机。
export const MAX_BLOB_BYTES = 25 * 1024 * 1024;
// 任何上传路径能声明的最大附件。MAX_BLOB_BYTES 只约束单请求代理路径，而
// multipart 的 part 数量是按 manifest size 推出来的：没有这条上限时，天花板
// 就是 MAX_MULTIPART_PARTS × MAX_MULTIPART_PART_BYTES 恰好乘出来的约 156 GiB，
// 而那两个常量只是在照搬 S3 的协议限制。2 GiB 是微信自身最大传输文件的两倍，
// 真实附件不可能触到。
export const MAX_BLOB_TOTAL_BYTES = 2 * 1024 * 1024 * 1024;

const BLOB_STATES = new Set(["pending_upload", "remote_ready"]);
const SHA256_HEX = /^[0-9a-f]{64}$/;

const notFound = (): Response =>
  errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");

const invalid = (message: string): Response => errorResponse("QUERY_INVALID", message);
const conflict = (message: string, retryable = false): Response =>
  errorResponse("BLOB_CONFLICT", message, retryable);

export interface BlobManifestInput {
  size: number;
  sha256: string;
  state: string;
}

const isManifestInput = (value: unknown): value is BlobManifestInput => {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  return (
    typeof body.size === "number" &&
    typeof body.sha256 === "string" &&
    typeof body.state === "string"
  );
};

const manifestFailure = (manifest: BlobManifestInput): string | null => {
  if (!Number.isSafeInteger(manifest.size) || manifest.size < 0) {
    return "size 必须是非负整数。";
  }
  // 放在这里而不是 multipart 计划里：size 只从这一个入口进来，计划的 part
  // 数量是从这里落下的值推出来的。
  if (manifest.size > MAX_BLOB_TOTAL_BYTES) {
    return "附件超过单个附件的大小上限。";
  }
  if (!SHA256_HEX.test(manifest.sha256)) {
    return "sha256 必须是 64 位十六进制摘要。";
  }
  if (!BLOB_STATES.has(manifest.state)) {
    return "state 只能是 pending_upload。";
  }
  return null;
};

const blobKey = (archiveId: string, blobId: string): string => `${archiveId}/${blobId}`;

// 读旧状态、按旧状态决定动作、写入之间不是原子的：两次并发请求都可能在
// prior.state !== "remote_ready" 时通过前置检查。DO UPDATE 的 WHERE 子句把
// "只在仍非 remote_ready 时才覆盖" 这条判断挪进同一条 SQL 语句里做成 CAS，
// 输给并发的一方会观察到 changes === 0，转去重新读一次当前状态而不是盲目
// 覆盖——避免把已验证 blob 的 size/sha256/object_version 静默降级。
export const upsertManifestOnce = async (
  env: Env,
  archiveId: string,
  blobId: string,
  parsed: BlobManifestInput,
): Promise<Response | null> => {
  const prior = await env.DB.prepare(
    `SELECT size, sha256, state, object_version, verified_at
     FROM blob_manifests WHERE archive_id = ? AND blob_id = ?`,
  )
    .bind(archiveId, blobId)
    .first<{
      size: number;
      sha256: string;
      state: string;
      object_version: string | null;
      verified_at: number | null;
    }>();
  if (prior?.state === "remote_ready") {
    if (prior.size !== parsed.size || prior.sha256 !== parsed.sha256) {
      return conflict("已验证附件不可覆盖。", false);
    }
    return jsonResponse({
      blob_id: blobId,
      size: prior.size,
      sha256: prior.sha256,
      state: prior.state,
      object_version: prior.object_version,
      verified_at: prior.verified_at,
    });
  }
  const result = await env.DB.prepare(
    `INSERT INTO blob_manifests(archive_id, blob_id, size, sha256, state, updated_at)
     VALUES (?, ?, ?, ?, ?, ?)
     ON CONFLICT(archive_id, blob_id) DO UPDATE SET
         size = excluded.size,
         sha256 = excluded.sha256,
         state = excluded.state,
         object_version = NULL,
         verified_at = NULL,
         updated_at = excluded.updated_at
     WHERE blob_manifests.state != 'remote_ready'`,
  )
    .bind(archiveId, blobId, parsed.size, parsed.sha256, parsed.state, Math.floor(Date.now() / 1000))
    .run();
  if (result.meta.changes === 0) {
    // 另一个请求在我们读到 prior 之后把这条 manifest 标记成了 remote_ready；
    // 交给调用方重新走一遍读-判断，而不是在这里假装覆盖成功了。
    return null;
  }
  return jsonResponse({
    blob_id: blobId,
    size: parsed.size,
    sha256: parsed.sha256,
    state: parsed.state,
  });
};

export const handleManifestUpsert = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  request: Request,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  let parsed: unknown;
  try {
    parsed = await request.json();
  } catch {
    return invalid("请求体必须是合法 JSON。");
  }
  if (!isManifestInput(parsed)) {
    return invalid("请求体缺少必要字段或类型不正确。");
  }
  const failure = manifestFailure(parsed);
  if (failure !== null) {
    return invalid(failure);
  }
  if (parsed.state !== "pending_upload") {
    return conflict("客户端不能直接把附件标记为 remote_ready。", false);
  }
  const first = await upsertManifestOnce(env, archiveId, blobId, parsed);
  if (first !== null) return first;
  const second = await upsertManifestOnce(env, archiveId, blobId, parsed);
  if (second !== null) return second;
  return conflict("附件状态正在被并发更新，请重试。", true);
};

interface ReconcileBody {
  expected: Array<{ blob_id: string } & BlobManifestInput>;
}

const isReconcileBody = (value: unknown): value is ReconcileBody => {
  if (typeof value !== "object" || value === null) return false;
  const body = value as Record<string, unknown>;
  if (!Array.isArray(body.expected)) return false;
  return body.expected.every((entry: unknown) => {
    if (typeof entry !== "object" || entry === null) return false;
    const item = entry as Record<string, unknown>;
    return typeof item.blob_id === "string" && isManifestInput(item);
  });
};

// PSU-0.7 `reconcile_blob_manifests` 逐条移植：三桶各自排序，重复 blob_id 拒绝。
export const handleManifestReconcile = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  request: Request,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  let parsed: unknown;
  try {
    parsed = await request.json();
  } catch {
    return invalid("请求体必须是合法 JSON。");
  }
  if (!isReconcileBody(parsed)) {
    return invalid("请求体缺少必要字段或类型不正确。");
  }
  const expectedById = new Map(parsed.expected.map((entry) => [entry.blob_id, entry]));
  if (expectedById.size !== parsed.expected.length) {
    return invalid("expected 清单包含重复的 blob_id。");
  }
  const rows = await env.DB.prepare(
    "SELECT blob_id, sha256 FROM blob_manifests WHERE archive_id = ?",
  )
    .bind(archiveId)
    .all<{ blob_id: string; sha256: string }>();
  const stored = new Map(rows.results.map((row) => [row.blob_id, row.sha256]));

  const missing = [...expectedById.keys()].filter((id) => !stored.has(id)).sort();
  const mismatched = [...expectedById.keys()]
    .filter((id) => stored.has(id) && stored.get(id) !== expectedById.get(id)?.sha256)
    .sort();
  const orphaned = [...stored.keys()].filter((id) => !expectedById.has(id)).sort();
  return jsonResponse({
    missing_in_store: missing,
    checksum_mismatches: mismatched,
    orphaned_in_store: orphaned,
  });
};

const toHex = (digest: ArrayBuffer): string =>
  [...new Uint8Array(digest)].map((byte) => byte.toString(16).padStart(2, "0")).join("");

export const handleContentUpload = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
  request: Request,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  const manifest = await env.DB.prepare(
    `SELECT size, sha256, state, object_version, verified_at
     FROM blob_manifests WHERE archive_id = ? AND blob_id = ?`,
  )
    .bind(archiveId, blobId)
    .first<{
      size: number;
      sha256: string;
      state: string;
      object_version: string | null;
      verified_at: number | null;
    }>();
  if (manifest === null) {
    return errorResponse("NOT_FOUND", "附件清单不存在，请先登记 manifest。");
  }
  if (manifest.size > MAX_BLOB_BYTES) {
    return invalid("附件超过代理上传上限；大文件属后续单元的分段上传。");
  }
  if (manifest.state === "remote_ready") {
    const existing = await env.BLOBS.head(blobKey(archiveId, blobId));
    if (
      existing?.size === manifest.size &&
      existing.customMetadata?.archive_sha256 === manifest.sha256
    ) {
      return jsonResponse({
        blob_id: blobId,
        state: "remote_ready",
        object_version: existing.version,
        verified_at: manifest.verified_at,
      });
    }
    return conflict("已验证附件对象缺失或不一致。", true);
  }
  const body = await request.arrayBuffer();
  if (body.byteLength !== manifest.size) {
    return invalid("上传字节数与清单 size 不一致。");
  }
  const digest = toHex(await crypto.subtle.digest("SHA-256", body));
  if (digest !== manifest.sha256) {
    return invalid("上传内容摘要与清单 sha256 不一致。");
  }
  const stored = await env.BLOBS.put(blobKey(archiveId, blobId), body, {
    onlyIf: { etagDoesNotMatch: "*" },
    sha256: manifest.sha256,
    customMetadata: {
      archive_id: archiveId,
      blob_id: blobId,
      archive_sha256: manifest.sha256,
    },
  });
  const object = stored ?? (await env.BLOBS.head(blobKey(archiveId, blobId)));
  if (
    object === null ||
    object.size !== manifest.size ||
    object.customMetadata?.archive_sha256 !== manifest.sha256
  ) {
    return errorResponse("BLOB_INTEGRITY", "远程对象校验失败。", false);
  }
  const verifiedAt = Math.floor(Date.now() / 1000);
  const updated = await env.DB.prepare(
    `UPDATE blob_manifests
     SET state = 'remote_ready', object_version = ?, verified_at = ?, updated_at = ?
     WHERE archive_id = ? AND blob_id = ? AND sha256 = ? AND size = ?`,
  )
    .bind(object.version, verifiedAt, verifiedAt, archiveId, blobId, manifest.sha256, manifest.size)
    .run();
  if (updated.meta.changes === 0) {
    // manifest 在我们读它、上传并校验对象期间被并发改写(sha256/size 已不同)：
    // R2 里的对象是照旧值写的，不能冒充这条新 manifest 已经 remote_ready。
    return conflict("附件清单在上传过程中被并发修改，请刷新后重试。", true);
  }
  return jsonResponse({
    blob_id: blobId,
    state: "remote_ready",
    object_version: object.version,
    verified_at: verifiedAt,
  });
};

export const handleContentDownload = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  blobId: string,
): Promise<Response> => {
  if (auth.archiveId !== archiveId) return notFound();
  const manifest = await env.DB.prepare(
    "SELECT state, sha256 FROM blob_manifests WHERE archive_id = ? AND blob_id = ?",
  )
    .bind(archiveId, blobId)
    .first<{ state: string; sha256: string }>();
  if (manifest?.state !== "remote_ready") {
    return errorResponse("NOT_FOUND", "附件尚未完成远程校验。");
  }
  const object = await env.BLOBS.get(blobKey(archiveId, blobId));
  if (object === null) {
    return errorResponse("NOT_FOUND", "附件内容不存在。");
  }
  return withSecurityHeaders(
    new Response(object.body, {
      status: 200,
      headers: { "Content-Type": "application/octet-stream" },
    }),
  );
};

import type { AuthContext } from "./auth";
import type { Env } from "./index";
import { errorResponse, jsonResponse } from "./http";
import { normalizeSearchText } from "./syncPlan";

// 新设备远程只读查询面：会话浏览 + 中文子串搜索。逐条镜像三后端
// MetadataStore.list_conversations / list_conversation_messages / search 的 SQL 语义。

const MAX_QUERY_PAGE = 500;

const invalid = (message: string): Response => errorResponse("QUERY_INVALID", message);

const notForArchive = (auth: AuthContext, archiveId: string): boolean =>
  auth.archiveId !== archiveId;

interface MessageRow {
  record_key: string;
  conversation_id: string;
  created_at: number;
  content_text: string;
  content_sha256: string;
  server_sequence: number;
}

const MESSAGE_COLUMNS =
  "record_key, conversation_id, created_at, content_text, content_sha256, server_sequence";

const parseLimit = (url: URL): number | null => {
  const limit = Number(url.searchParams.get("limit") ?? "100");
  if (!Number.isSafeInteger(limit) || limit <= 0 || limit > MAX_QUERY_PAGE) return null;
  return limit;
};

export const handleQuerySearch = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  url: URL,
): Promise<Response> => {
  if (notForArchive(auth, archiveId)) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  const limit = parseLimit(url);
  if (limit === null) {
    return invalid(`limit 必须在 1 到 ${String(MAX_QUERY_PAGE)} 之间。`);
  }
  const normalized = normalizeSearchText(url.searchParams.get("q") ?? "").trim();
  if (normalized === "") {
    return jsonResponse({ items: [] });
  }
  const rows = await env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS}
     FROM archive_records
     WHERE archive_id = ? AND instr(normalized_content, ?) > 0
     ORDER BY created_at DESC, record_key
     LIMIT ?`,
  )
    .bind(archiveId, normalized, limit)
    .all<MessageRow>();
  // Frozen v1 has no reply linkage or typed media; do not invent a preview.
  return jsonResponse({ items: rows.results.map((row) => ({ ...row, reply_preview: null })) });
};

export const handleQueryConversations = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  url: URL,
): Promise<Response> => {
  if (notForArchive(auth, archiveId)) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  const limit = parseLimit(url);
  if (limit === null) {
    return invalid(`limit 必须在 1 到 ${String(MAX_QUERY_PAGE)} 之间。`);
  }
  const after = url.searchParams.get("after_conversation_id") ?? "";
  const rows = await env.DB.prepare(
    `SELECT conversation_id, COUNT(*) AS message_count,
            MAX(created_at) AS last_created_at,
            MAX(server_sequence) AS last_server_sequence
     FROM archive_records
     WHERE archive_id = ? AND conversation_id > ?
     GROUP BY conversation_id
     ORDER BY conversation_id
     LIMIT ?`,
  )
    .bind(archiveId, after, limit + 1)
    .all<{
      conversation_id: string;
      message_count: number;
      last_created_at: number;
      last_server_sequence: number;
    }>();
  const hasMore = rows.results.length > limit;
  const items = rows.results.slice(0, limit);
  const nextId = items.length === 0 ? null : (items[items.length - 1]?.conversation_id ?? null);
  return jsonResponse({ items, next_conversation_id: nextId, has_more: hasMore });
};

export const handleQueryConversationMessages = async (
  env: Env,
  auth: AuthContext,
  archiveId: string,
  conversationId: string,
  url: URL,
): Promise<Response> => {
  if (notForArchive(auth, archiveId)) {
    return errorResponse("NOT_FOUND", "归档不存在或不属于当前设备。");
  }
  const limit = parseLimit(url);
  if (limit === null) {
    return invalid(`limit 必须在 1 到 ${String(MAX_QUERY_PAGE)} 之间。`);
  }
  const after = Number(url.searchParams.get("after_server_sequence") ?? "0");
  if (!Number.isSafeInteger(after) || after < 0) {
    return invalid("after_server_sequence 必须是非负整数。");
  }
  const rows = await env.DB.prepare(
    `SELECT ${MESSAGE_COLUMNS}
     FROM archive_records
     WHERE archive_id = ? AND conversation_id = ? AND server_sequence > ?
     ORDER BY server_sequence, record_key
     LIMIT ?`,
  )
    .bind(archiveId, conversationId, after, limit + 1)
    .all<MessageRow>();
  const hasMore = rows.results.length > limit;
  const items = rows.results.slice(0, limit).map((row) => ({ ...row, reply_preview: null }));
  const nextSequence =
    items.length === 0 ? after : (items[items.length - 1]?.server_sequence ?? after);
  return jsonResponse({ items, next_server_sequence: nextSequence, has_more: hasMore });
};

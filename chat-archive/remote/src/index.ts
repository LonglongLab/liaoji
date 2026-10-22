import { authenticate } from "./auth";
import {
  handleContentDownload,
  handleContentUpload,
  handleManifestReconcile,
  handleManifestUpsert,
} from "./blobRoutes";
import {
  handleMultipartAbort,
  handleMultipartComplete,
  handleMultipartPartUpload,
  handleMultipartPlan,
  handleMultipartStatus,
} from "./multipartRoutes";
import {
  handleDeviceRevoke,
  handleDevicesRevokeAll,
  handlePair,
  handlePairingCodeCreate,
  handleTokenRotate,
} from "./deviceRoutes";
import { errorResponse } from "./http";
import {
  handleQueryConversationMessages,
  handleQueryConversations,
  handleQuerySearch,
} from "./queryRoutes";
import {
  handleArchiveDevices,
  handleArchiveStats,
  handleHealth,
  handleSyncHealth,
} from "./routes";
import { handleSyncCommit, handleSyncConflicts, handleSyncRecords } from "./syncRoutes";

export interface Env {
  DB: D1Database;
  BLOBS: R2Bucket;
}

const ARCHIVE_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/(stats|devices|sync-health|sync\/batches|sync\/records|sync\/conflicts|query\/search|query\/conversations)$/;

const METHOD_BY_RESOURCE: Record<string, string> = {
  stats: "GET",
  devices: "GET",
  "sync-health": "GET",
  "sync/batches": "POST",
  "sync/records": "GET",
  "sync/conflicts": "GET",
  "query/search": "GET",
  "query/conversations": "GET",
};

const QUERY_MESSAGES_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/query\/conversations\/([A-Za-z0-9_-]{1,128})\/messages$/;

const PAIRING_CODE_ROUTE = /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/pairing-codes$/;
const DEVICE_REVOKE_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/devices\/([A-Za-z0-9_-]{1,128})\/revoke$/;
const DEVICES_REVOKE_ALL_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/devices\/revoke-all$/;
const TOKEN_ROTATE_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/devices\/self\/rotate$/;

const BLOB_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/([A-Za-z0-9_-]{1,128})\/(manifest|content)$/;

const RECONCILE_ROUTE = /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/reconcile$/;
const MULTIPART_PLAN_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/([A-Za-z0-9_-]{1,128})\/uploads$/;
const MULTIPART_ITEM_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/([A-Za-z0-9_-]{1,128})\/uploads\/([a-f0-9]{32})$/;
const MULTIPART_COMPLETE_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/([A-Za-z0-9_-]{1,128})\/uploads\/([a-f0-9]{32})\/complete$/;
const MULTIPART_PART_ROUTE =
  /^\/api\/v1\/archives\/([A-Za-z0-9_-]{1,128})\/blobs\/([A-Za-z0-9_-]{1,128})\/uploads\/([a-f0-9]{32})\/parts\/([0-9]{1,5})$/;

export default {
  async fetch(request, env, ctx): Promise<Response> {
    const url = new URL(request.url);
    const { pathname } = url;

    if (pathname === "/api/v1/health") {
      return request.method === "GET"
        ? handleHealth()
        : errorResponse("METHOD_NOT_ALLOWED", "此端点只支持 GET。");
    }

    if (pathname === "/api/v1/pair") {
      return request.method === "POST"
        ? handlePair(env, request)
        : errorResponse("METHOD_NOT_ALLOWED", "此端点只支持 POST。");
    }

    const pairingCodeMatch = PAIRING_CODE_ROUTE.exec(pathname);
    if (pairingCodeMatch !== null) {
      const [, archiveId] = pairingCodeMatch;
      if (archiveId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handlePairingCodeCreate(env, auth, archiveId);
    }

    const revokeAllMatch = DEVICES_REVOKE_ALL_ROUTE.exec(pathname);
    if (revokeAllMatch !== null) {
      const [, archiveId] = revokeAllMatch;
      if (archiveId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleDevicesRevokeAll(env, auth, archiveId);
    }

    const rotateMatch = TOKEN_ROTATE_ROUTE.exec(pathname);
    if (rotateMatch !== null) {
      const [, archiveId] = rotateMatch;
      if (archiveId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleTokenRotate(env, auth, archiveId);
    }

    const deviceRevokeMatch = DEVICE_REVOKE_ROUTE.exec(pathname);
    if (deviceRevokeMatch !== null) {
      const [, archiveId, deviceId] = deviceRevokeMatch;
      if (archiveId === undefined || deviceId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleDeviceRevoke(env, auth, archiveId, deviceId);
    }

    const partMatch = MULTIPART_PART_ROUTE.exec(pathname);
    if (partMatch !== null) {
      const [, archiveId, blobId, sessionId, partNumberRaw] = partMatch;
      const partNumber = Number(partNumberRaw);
      if (
        archiveId === undefined ||
        blobId === undefined ||
        sessionId === undefined ||
        !Number.isSafeInteger(partNumber) ||
        partNumber <= 0 ||
        request.method !== "PUT"
      ) {
        return errorResponse("METHOD_NOT_ALLOWED", "该上传端点不支持此方法或参数。");
      }
      return handleMultipartPartUpload(env, archiveId, blobId, sessionId, partNumber, request);
    }

    const completeMatch = MULTIPART_COMPLETE_ROUTE.exec(pathname);
    if (completeMatch !== null) {
      const [, archiveId, blobId, sessionId] = completeMatch;
      if (archiveId === undefined || blobId === undefined || sessionId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该上传端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleMultipartComplete(env, auth, archiveId, blobId, sessionId);
    }

    const planMatch = MULTIPART_PLAN_ROUTE.exec(pathname);
    if (planMatch !== null) {
      const [, archiveId, blobId] = planMatch;
      if (archiveId === undefined || blobId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该上传端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleMultipartPlan(env, auth, archiveId, blobId, request);
    }

    const itemMatch = MULTIPART_ITEM_ROUTE.exec(pathname);
    if (itemMatch !== null) {
      const [, archiveId, blobId, sessionId] = itemMatch;
      if (archiveId === undefined || blobId === undefined || sessionId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "GET" && request.method !== "DELETE") {
        return errorResponse("METHOD_NOT_ALLOWED", "该上传端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return request.method === "GET"
        ? handleMultipartStatus(env, auth, archiveId, blobId, sessionId)
        : handleMultipartAbort(env, auth, archiveId, blobId, sessionId);
    }

    const archiveMatch = ARCHIVE_ROUTE.exec(pathname);
    if (archiveMatch !== null) {
      const [, archiveId, resource] = archiveMatch;
      if (archiveId === undefined || resource === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== METHOD_BY_RESOURCE[resource]) {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) {
        return auth;
      }
      if (resource === "stats") {
        return handleArchiveStats(env, auth, archiveId);
      }
      if (resource === "devices") {
        return handleArchiveDevices(env, auth, archiveId);
      }
      if (resource === "sync-health") {
        return handleSyncHealth(env, auth, archiveId);
      }
      if (resource === "sync/batches") {
        return handleSyncCommit(env, auth, archiveId, request);
      }
      if (resource === "sync/records") {
        return handleSyncRecords(env, auth, archiveId, url);
      }
      if (resource === "sync/conflicts") {
        return handleSyncConflicts(env, auth, archiveId, url);
      }
      if (resource === "query/search") {
        return handleQuerySearch(env, auth, archiveId, url);
      }
      return handleQueryConversations(env, auth, archiveId, url);
    }

    const queryMessagesMatch = QUERY_MESSAGES_ROUTE.exec(pathname);
    if (queryMessagesMatch !== null) {
      const [, archiveId, conversationId] = queryMessagesMatch;
      if (archiveId === undefined || conversationId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "GET") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) return auth;
      return handleQueryConversationMessages(env, auth, archiveId, conversationId, url);
    }

    const blobMatch = BLOB_ROUTE.exec(pathname);
    if (blobMatch !== null) {
      const [, archiveId, blobId, resource] = blobMatch;
      if (archiveId === undefined || blobId === undefined || resource === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      const allowed = resource === "manifest" ? ["PUT"] : ["PUT", "GET"];
      if (!allowed.includes(request.method)) {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) {
        return auth;
      }
      if (resource === "manifest") {
        return handleManifestUpsert(env, auth, archiveId, blobId, request);
      }
      return request.method === "PUT"
        ? handleContentUpload(env, auth, archiveId, blobId, request)
        : handleContentDownload(env, auth, archiveId, blobId);
    }

    const reconcileMatch = RECONCILE_ROUTE.exec(pathname);
    if (reconcileMatch !== null) {
      const [, archiveId] = reconcileMatch;
      if (archiveId === undefined) {
        return errorResponse("NOT_FOUND", "资源不存在。");
      }
      if (request.method !== "POST") {
        return errorResponse("METHOD_NOT_ALLOWED", "该端点不支持此方法。");
      }
      const auth = await authenticate(env, request, ctx);
      if (auth instanceof Response) {
        return auth;
      }
      return handleManifestReconcile(env, auth, archiveId, request);
    }

    return errorResponse("NOT_FOUND", "资源不存在。");
  },
} satisfies ExportedHandler<Env>;

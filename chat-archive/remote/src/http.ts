const SECURITY_HEADERS: ReadonlyArray<[string, string]> = [
  ["Content-Security-Policy", "default-src 'none'; frame-ancestors 'none'"],
  ["X-Content-Type-Options", "nosniff"],
  ["Referrer-Policy", "no-referrer"],
  ["Cache-Control", "no-store"],
];

export const withSecurityHeaders = (response: Response): Response => {
  const secured = new Response(response.body, response);
  for (const [name, value] of SECURITY_HEADERS) {
    secured.headers.set(name, value);
  }
  return secured;
};

export type RemoteErrorCode =
  | "UNAUTHORIZED"
  | "NOT_FOUND"
  | "QUERY_INVALID"
  | "METHOD_NOT_ALLOWED"
  | "SYNC_CONFLICT"
  | "BLOB_CONFLICT"
  | "BLOB_SOURCE_CHANGED"
  | "BLOB_INTEGRITY"
  | "SERVICE_UNAVAILABLE";

const STATUS_BY_CODE: Record<RemoteErrorCode, number> = {
  UNAUTHORIZED: 401,
  NOT_FOUND: 404,
  QUERY_INVALID: 400,
  METHOD_NOT_ALLOWED: 405,
  SYNC_CONFLICT: 409,
  BLOB_CONFLICT: 409,
  BLOB_SOURCE_CHANGED: 409,
  BLOB_INTEGRITY: 422,
  SERVICE_UNAVAILABLE: 503,
};

export const errorResponse = (
  code: RemoteErrorCode,
  message: string,
  retryable = false,
): Response =>
  withSecurityHeaders(
    Response.json(
      { error: { code, message, retryable } },
      { status: STATUS_BY_CODE[code] },
    ),
  );

export const jsonResponse = (payload: unknown, status = 200): Response =>
  withSecurityHeaders(Response.json(payload, { status }));

export const MAX_JSON_BODY_BYTES = 2 * 1024 * 1024;

export class BodyLimitError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "BodyLimitError";
  }
}

export const readBoundedBytes = async (
  request: Request,
  maxBytes: number,
): Promise<ArrayBuffer> => {
  const declared = request.headers.get("Content-Length");
  if (declared !== null) {
    const parsedLength = Number(declared);
    if (!Number.isSafeInteger(parsedLength) || parsedLength < 0 || parsedLength > maxBytes) {
      throw new BodyLimitError("请求体超过允许大小。");
    }
  }
  if (request.body === null) {
    return new ArrayBuffer(0);
  }
  const reader = request.body.getReader() as ReadableStreamDefaultReader<Uint8Array>;
  const chunks: Uint8Array[] = [];
  let total = 0;
  let result = await reader.read();
  while (!result.done) {
    total += result.value.byteLength;
    if (total > maxBytes) {
      await reader.cancel();
      throw new BodyLimitError("请求体超过允许大小。");
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
  return combined.buffer;
};

export const readBoundedJson = async (
  request: Request,
  maxBytes = MAX_JSON_BODY_BYTES,
): Promise<unknown> => {
  const raw = await readBoundedBytes(request, maxBytes);
  if (raw.byteLength === 0) {
    throw new SyntaxError("empty body");
  }
  const text = new TextDecoder("utf-8", { fatal: true, ignoreBOM: false }).decode(raw);
  return JSON.parse(text) as unknown;
};

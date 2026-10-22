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

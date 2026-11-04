/** Slow-search diagnostics contain only bounded numeric timings and a random trace id. */
const headerTimes = new WeakMap<Response, number>();
const serverStages = new Set([
  "server", "dispatch", "encode", "handler", "auth", "account", "schema",
  "schema_upgrade", "index", "browse", "counts", "page", "index_status", "hydrate",
  "records", "avatars", "transcripts", "replies", "upstream_headers", "upstream_body", "upstream_decode",
]);

export const noteSearchHeaders = (response: Response): void => {
  headerTimes.set(response, performance.now());
};

const ms = (value: number): number | null =>
  Number.isFinite(value) && value >= 0 ? Math.round(value * 1000) / 1000 : null;

const parsedServerTiming = (value: string | null): Record<string, number> => {
  const result: Record<string, number> = {};
  for (const metric of (value ?? "").slice(0, 4096).split(",")) {
    const match = /^\s*([a-z_]+);dur=(\d+(?:\.\d+)?)(?:\s|;|$)/.exec(metric);
    if (match?.[1] !== undefined && match[2] !== undefined && serverStages.has(match[1])) {
      const duration = Number(match[2]);
      if (Number.isFinite(duration)) result[match[1]] = duration;
    }
  }
  return result;
};

const report = (kind: "local" | "remote", started: number, response?: Response): void => {
  const finished = performance.now();
  if (finished - started < 2000) return;
  const headersAt = response === undefined ? undefined : headerTimes.get(response);
  const rawTrace = response?.headers.get("X-Search-Trace") ?? "";
  const resources = response?.url
    ? (performance.getEntriesByName(response.url, "resource") as PerformanceResourceTiming[])
      .filter(entry => entry.startTime >= started - 1 && entry.responseEnd <= finished + 1)
    : [];
  // The URL is not a request id: simultaneous identical requests are ambiguous.
  const resource = resources.length === 1 ? resources[0] : undefined;
  console.warn("search_request_timing", {
    kind, total_ms: ms(finished - started), status: response?.status ?? null,
    headers_ms: headersAt === undefined ? null : ms(headersAt - started),
    // Includes download, JSON decoding and any main-thread scheduling delay.
    body_decode_ms: headersAt === undefined ? null : ms(finished - headersAt),
    trace_id: /^[a-f0-9]{12}$/.test(rawTrace) ? rawTrace : null,
    server: parsedServerTiming(response?.headers.get("Server-Timing") ?? null),
    network: resource === undefined ? null : {
      queue_ms: ms(Math.max(0, resource.requestStart - resource.fetchStart
        - (resource.domainLookupEnd - resource.domainLookupStart)
        - (resource.connectEnd - resource.connectStart))),
      dns_ms: ms(resource.domainLookupEnd - resource.domainLookupStart),
      connect_ms: ms(resource.connectEnd - resource.connectStart),
      ttfb_ms: ms(resource.responseStart - resource.requestStart),
      download_ms: ms(resource.responseEnd - resource.responseStart),
      after_download_ms: ms(finished - resource.responseEnd),
      bytes: ms(resource.transferSize),
    },
  });
};

export async function observeSearchRequest<T extends { response: Response }>(
  kind: "local" | "remote", request: () => Promise<T>,
): Promise<T> {
  const started = performance.now();
  let response: Response | undefined;
  try {
    const result = await request();
    response = result.response;
    return result;
  } finally {
    try { report(kind, started, response); } catch { /* Diagnostics cannot replace the result. */ }
  }
}

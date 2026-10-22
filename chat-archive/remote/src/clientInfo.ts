export interface SyncClientInfo {
  computer_name: string;
  platform: string;
  client_name: string;
}

const validText = (value: unknown, limit: number): value is string =>
  typeof value === "string" && value.trim().length > 0 && value.length <= limit &&
  !Array.from(value).some(character => {
    const code = character.codePointAt(0) ?? 0;
    return code < 32 || code === 127 || (code >= 0xd800 && code <= 0xdfff);
  });

export const parseClientInfo = (header: string | null): SyncClientInfo | null => {
  if (header === null || header.length > 2048) return null;
  let body: unknown;
  try { body = JSON.parse(header); } catch { return null; }
  if (body === null || typeof body !== "object" || Array.isArray(body)) return null;
  const fields = body as Record<string, unknown>;
  if (Object.keys(fields).sort().join(",") !== "client_name,computer_name,platform") return null;
  const name = fields["computer_name"], platform = fields["platform"], client = fields["client_name"];
  if (!validText(name, 128) || !validText(platform, 32) || !validText(client, 32)) return null;
  return { computer_name: name.trim(), platform: platform.trim(), client_name: client.trim() };
};

export const constantTimeEqual = (a: string, b: string): boolean => {
  const encoder = new TextEncoder();
  const left = encoder.encode(a);
  const right = encoder.encode(b);
  let diff = left.length ^ right.length;
  const length = Math.max(left.length, right.length);
  for (let index = 0; index < length; index += 1) {
    diff |= (left[index] ?? 0) ^ (right[index] ?? 0);
  }
  return diff === 0;
};

export const sha256Hex = async (input: string): Promise<string> => {
  const digest = await crypto.subtle.digest("SHA-256", new TextEncoder().encode(input));
  return hexBytes(digest);
};

export const sha256Bytes = async (input: ArrayBuffer): Promise<string> => {
  const digest = await crypto.subtle.digest("SHA-256", input);
  return hexBytes(digest);
};

export const hexBytes = (input: ArrayBuffer): string => {
  return [...new Uint8Array(input)]
    .map((byte) => byte.toString(16).padStart(2, "0"))
    .join("");
};

export interface BearerToken {
  deviceId: string;
  secret: string;
}

export const parseBearerToken = (header: string | null): BearerToken | null => {
  if (header === null || !header.startsWith("Bearer ")) {
    return null;
  }
  const token = header.slice("Bearer ".length);
  const separator = token.indexOf(".");
  if (separator <= 0 || separator === token.length - 1) {
    return null;
  }
  return {
    deviceId: token.slice(0, separator),
    secret: token.slice(separator + 1),
  };
};

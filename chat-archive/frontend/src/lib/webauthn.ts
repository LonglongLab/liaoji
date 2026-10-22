/**
 * PSU-2.213 日常解锁的浏览器侧 WebAuthn 薄封装。
 *
 * 服务端下发 WebAuthn Level 3 JSON 形态的 options（二进制字段为 base64url），
 * 浏览器回传 `PublicKeyCredential.toJSON()` 的结果。新浏览器自带
 * `parse*OptionsFromJSON` / `toJSON`，旧一点的（iOS 16、部分 Android WebView）
 * 没有，这里手动做 base64url ↔ ArrayBuffer 的转换。
 *
 * 只在用户点击后调用：系统验证框必须由用户手势触发，否则 Safari 直接拒绝，
 * 也避免一打开页面就弹框打扰人。
 */
import type { WebAuthnCredentialJson } from "../api/archiveApi";

export type WebAuthnFailureKind = "cancelled" | "already_registered" | "unsupported" | "failed";

export class WebAuthnError extends Error {
  readonly kind: WebAuthnFailureKind;

  constructor(kind: WebAuthnFailureKind, message: string) {
    super(message);
    this.name = "WebAuthnError";
    this.kind = kind;
  }
}

const MESSAGES: Record<WebAuthnFailureKind, string> = {
  cancelled: "系统验证已取消或超时，没有完成。",
  already_registered: "这个设备上的通行密钥已经登记过了。",
  unsupported: "当前浏览器或连接不支持通行密钥（需要 HTTPS 且浏览器支持 WebAuthn）。",
  failed: "系统验证没有完成，请重试。",
};

export const webAuthnMessage = (kind: WebAuthnFailureKind): string => MESSAGES[kind];

type JsonRecord = Record<string, unknown>;

interface CredentialContainerLike {
  create(options: CredentialCreationOptions): Promise<Credential | null>;
  get(options: CredentialRequestOptions): Promise<Credential | null>;
}

interface WebAuthnEnvironment {
  credentials?: CredentialContainerLike | undefined;
  PublicKeyCredential?: unknown;
  isSecureContext?: boolean | undefined;
}

const defaultEnvironment = (): WebAuthnEnvironment => ({
  credentials: typeof navigator === "undefined" ? undefined : navigator.credentials,
  PublicKeyCredential:
    typeof window === "undefined"
      ? undefined
      : (window as unknown as { PublicKeyCredential?: unknown }).PublicKeyCredential,
  isSecureContext: typeof window === "undefined" ? false : window.isSecureContext,
});

export const isWebAuthnAvailable = (env: WebAuthnEnvironment = defaultEnvironment()): boolean =>
  env.isSecureContext === true &&
  typeof env.PublicKeyCredential === "function" &&
  env.credentials !== undefined &&
  typeof env.credentials.get === "function" &&
  typeof env.credentials.create === "function";

export const base64UrlToBuffer = (value: string): ArrayBuffer => {
  const padded = value.replace(/-/g, "+").replace(/_/g, "/");
  const normalized = padded + "=".repeat((4 - (padded.length % 4)) % 4);
  const binary = atob(normalized);
  const bytes = new Uint8Array(binary.length);
  for (let index = 0; index < binary.length; index += 1) {
    bytes[index] = binary.charCodeAt(index);
  }
  return bytes.buffer;
};

export const bufferToBase64Url = (value: ArrayBuffer | ArrayBufferView): string => {
  const bytes =
    value instanceof ArrayBuffer
      ? new Uint8Array(value)
      : new Uint8Array(value.buffer, value.byteOffset, value.byteLength);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");
};

const isRecord = (value: unknown): value is JsonRecord =>
  typeof value === "object" && value !== null && !Array.isArray(value);

const decodeDescriptors = (value: unknown): unknown =>
  Array.isArray(value)
    ? (value as unknown[]).map((item): unknown =>
        isRecord(item) && typeof item.id === "string"
          ? { ...item, id: base64UrlToBuffer(item.id) }
          : item,
      )
    : value;

export const decodeRequestOptions = (options: JsonRecord): PublicKeyCredentialRequestOptions => {
  const decoded: JsonRecord = { ...options };
  if (typeof options.challenge === "string") decoded.challenge = base64UrlToBuffer(options.challenge);
  if ("allowCredentials" in options) decoded.allowCredentials = decodeDescriptors(options.allowCredentials);
  return decoded as unknown as PublicKeyCredentialRequestOptions;
};

export const decodeCreationOptions = (options: JsonRecord): PublicKeyCredentialCreationOptions => {
  const decoded: JsonRecord = { ...options };
  if (typeof options.challenge === "string") decoded.challenge = base64UrlToBuffer(options.challenge);
  if (isRecord(options.user) && typeof options.user.id === "string") {
    decoded.user = { ...options.user, id: base64UrlToBuffer(options.user.id) };
  }
  if ("excludeCredentials" in options) {
    decoded.excludeCredentials = decodeDescriptors(options.excludeCredentials);
  }
  return decoded as unknown as PublicKeyCredentialCreationOptions;
};

const optionalBuffer = (value: unknown): string | null =>
  value instanceof ArrayBuffer || ArrayBuffer.isView(value) ? bufferToBase64Url(value) : null;

/** 把浏览器返回的凭据转成服务端要的 JSON；优先用浏览器自带的 `toJSON()`。 */
export const credentialToJson = (credential: unknown): WebAuthnCredentialJson => {
  if (!isRecord(credential)) throw new WebAuthnError("failed", MESSAGES.failed);
  const native = credential.toJSON;
  if (typeof native === "function") {
    const json: unknown = native.call(credential);
    if (isRecord(json)) return json;
  }
  const response = credential.response;
  if (!isRecord(response)) throw new WebAuthnError("failed", MESSAGES.failed);
  const serialized: JsonRecord = {};
  const clientDataJSON = optionalBuffer(response.clientDataJSON);
  if (clientDataJSON !== null) serialized.clientDataJSON = clientDataJSON;
  const attestationObject = optionalBuffer(response.attestationObject);
  if (attestationObject !== null) {
    serialized.attestationObject = attestationObject;
    const getTransports = response.getTransports;
    if (typeof getTransports === "function") {
      const transports: unknown = getTransports.call(response);
      if (Array.isArray(transports)) serialized.transports = transports;
    }
  }
  const authenticatorData = optionalBuffer(response.authenticatorData);
  if (authenticatorData !== null) serialized.authenticatorData = authenticatorData;
  const signature = optionalBuffer(response.signature);
  if (signature !== null) serialized.signature = signature;
  if ("userHandle" in response) serialized.userHandle = optionalBuffer(response.userHandle);
  const rawId = optionalBuffer(credential.rawId);
  const getExtensions = credential.getClientExtensionResults;
  const extensions: unknown =
    typeof getExtensions === "function" ? getExtensions.call(credential) : {};
  const json: JsonRecord = {
    id: typeof credential.id === "string" ? credential.id : rawId,
    rawId,
    type: typeof credential.type === "string" ? credential.type : "public-key",
    response: serialized,
    clientExtensionResults: isRecord(extensions) ? extensions : {},
  };
  if (typeof credential.authenticatorAttachment === "string") {
    json.authenticatorAttachment = credential.authenticatorAttachment;
  }
  return json;
};

export const classifyWebAuthnError = (error: unknown): WebAuthnError => {
  if (error instanceof WebAuthnError) return error;
  const name = isRecord(error) && typeof error.name === "string" ? error.name : "";
  if (name === "NotAllowedError" || name === "AbortError") {
    return new WebAuthnError("cancelled", MESSAGES.cancelled);
  }
  if (name === "InvalidStateError") {
    return new WebAuthnError("already_registered", MESSAGES.already_registered);
  }
  if (name === "NotSupportedError" || name === "SecurityError") {
    return new WebAuthnError("unsupported", MESSAGES.unsupported);
  }
  return new WebAuthnError("failed", MESSAGES.failed);
};

interface PublicKeyCredentialStatics {
  parseRequestOptionsFromJSON?: (options: JsonRecord) => PublicKeyCredentialRequestOptions;
  parseCreationOptionsFromJSON?: (options: JsonRecord) => PublicKeyCredentialCreationOptions;
}

const statics = (env: WebAuthnEnvironment): PublicKeyCredentialStatics =>
  isRecord(env.PublicKeyCredential) || typeof env.PublicKeyCredential === "function"
    ? (env.PublicKeyCredential as PublicKeyCredentialStatics)
    : {};

const requireAvailable = (env: WebAuthnEnvironment): CredentialContainerLike => {
  if (!isWebAuthnAvailable(env) || env.credentials === undefined) {
    throw new WebAuthnError("unsupported", MESSAGES.unsupported);
  }
  return env.credentials;
};

/** 解锁 / 管理确认：调起系统验证框并返回可直接提交给服务端的凭据 JSON。 */
export const getAssertion = async (
  options: JsonRecord,
  env: WebAuthnEnvironment = defaultEnvironment(),
): Promise<WebAuthnCredentialJson> => {
  const container = requireAvailable(env);
  try {
    const parse = statics(env).parseRequestOptionsFromJSON;
    const publicKey =
      typeof parse === "function"
        ? parse.call(env.PublicKeyCredential, options)
        : decodeRequestOptions(options);
    const credential = await container.get({ publicKey });
    if (credential === null) throw new WebAuthnError("cancelled", MESSAGES.cancelled);
    return credentialToJson(credential);
  } catch (error) {
    throw classifyWebAuthnError(error);
  }
};

/** 登记新通行密钥。 */
export const createCredential = async (
  options: JsonRecord,
  env: WebAuthnEnvironment = defaultEnvironment(),
): Promise<WebAuthnCredentialJson> => {
  const container = requireAvailable(env);
  try {
    const parse = statics(env).parseCreationOptionsFromJSON;
    const publicKey =
      typeof parse === "function"
        ? parse.call(env.PublicKeyCredential, options)
        : decodeCreationOptions(options);
    const credential = await container.create({ publicKey });
    if (credential === null) throw new WebAuthnError("cancelled", MESSAGES.cancelled);
    return credentialToJson(credential);
  } catch (error) {
    throw classifyWebAuthnError(error);
  }
};

/** Exposed UI handle; authentication inputs stay inside the field component. */
export interface ServerManagementHandle {
  readonly ready: boolean;
  authorize(): Promise<boolean>;
  requireProof(error: unknown): void;
  clear(): void;
}

export type ServerConfigurationState = "configured" | "not_configured" | "unreadable" | "unknown";

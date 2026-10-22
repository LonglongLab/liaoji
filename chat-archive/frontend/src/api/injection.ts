import { inject, type InjectionKey } from "vue";

import type { ArchiveApi } from "./archiveApi";

export const archiveApiKey: InjectionKey<ArchiveApi> = Symbol("archive-api");

export const useArchiveApi = (): ArchiveApi => {
  const api = inject(archiveApiKey);
  if (api === undefined) {
    throw new Error("Archive API is not configured");
  }
  return api;
};

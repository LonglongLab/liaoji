import { ArchiveApiError } from "@/api/archiveApi";

/** 提取一般 Error 的非空消息，避免在多处视图里重复手写 instanceof 判断。 */
export const errorMessage = (error: unknown, fallback: string): string =>
  error instanceof Error && error.message !== "" ? error.message : fallback;

/** 提取后端领域错误消息；网络异常等非 ArchiveApiError 统一回落到界面兜底文案。 */
export const apiErrorMessage = (error: unknown, fallback: string): string =>
  error instanceof ArchiveApiError && error.message !== "" ? error.message : fallback;

import type JSZip from "jszip";
import type { JSZipObject } from "jszip";

export const MAX_OFFICE_ZIP_ENTRIES = 5000;
export const MAX_OFFICE_UNPACKED_BYTES = 50 * 1024 * 1024;
export const MAX_OFFICE_ENTRY_BYTES = 20 * 1024 * 1024;

export type ZipBudget = {
  remainingBytes: number;
  maxEntryBytes: number;
};

export type ZipGuardLimits = {
  maxEntries?: number;
  maxUnpackedBytes?: number;
  maxEntryBytes?: number;
};

export const createZipBudget = (
  maxUnpackedBytes: number = MAX_OFFICE_UNPACKED_BYTES,
  maxEntryBytes: number = MAX_OFFICE_ENTRY_BYTES,
): ZipBudget => ({
  remainingBytes: maxUnpackedBytes,
  maxEntryBytes,
});

export const loadGuardedZip = async (
  input: Blob | ArrayBuffer,
  limits: ZipGuardLimits = {},
): Promise<{ zip: JSZip; files: JSZipObject[] }> => {
  const { default: JSZipCtor } = await import("jszip");
  const zip = await JSZipCtor.loadAsync(input);
  const files = Object.values(zip.files);
  const maxEntries = limits.maxEntries ?? MAX_OFFICE_ZIP_ENTRIES;
  const maxUnpackedBytes = limits.maxUnpackedBytes ?? MAX_OFFICE_UNPACKED_BYTES;
  const maxEntryBytes = limits.maxEntryBytes ?? MAX_OFFICE_ENTRY_BYTES;
  if (files.length > maxEntries) {
    throw new RangeError("zip archive entry limit exceeded");
  }
  let declaredTotal = 0;
  for (const file of files) {
    if (file.dir) continue;
    const rawSize = (file as { _data?: { uncompressedSize?: number } })._data?.uncompressedSize;
    if (typeof rawSize === "number" && Number.isFinite(rawSize)) {
      if (rawSize > maxEntryBytes) {
        throw new RangeError("zip entry size limit exceeded");
      }
      declaredTotal += rawSize;
      if (declaredTotal > maxUnpackedBytes) {
        throw new RangeError("zip archive unpacked size limit exceeded");
      }
    }
  }
  return { zip, files };
};

export const readZipBytesBounded = async (
  file: JSZipObject,
  budget: ZipBudget,
): Promise<Uint8Array> => {
  const bytes = await file.async("uint8array");
  if (bytes.byteLength > budget.maxEntryBytes || bytes.byteLength > budget.remainingBytes) {
    throw new RangeError("zip entry unpacked byte limit exceeded");
  }
  budget.remainingBytes -= bytes.byteLength;
  return bytes;
};

export const readZipTextBounded = async (
  file: JSZipObject,
  budget: ZipBudget,
): Promise<string> => {
  const bytes = await readZipBytesBounded(file, budget);
  return new TextDecoder("utf-8").decode(bytes);
};

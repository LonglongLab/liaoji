// 恢复演练的纯校验逻辑（FR-RECOVERY-002、SYSTEM_DESIGN §18）：
// 对隔离环境恢复出的 D1 聚合与 R2 对象清单做双向对账。
// I/O 由 scripts/recovery-drill.mjs 执行；本模块保持零导入以便 Node 直接加载。

export interface BatchRange {
  start: number;
  end: number;
}

export interface ManifestEntry {
  blob_id: string;
  size: number;
  sha256: string;
  state: string;
}

export interface ArchiveSnapshot {
  archive_id: string;
  last_server_sequence: number;
  record_count: number;
  max_record_sequence: number;
  stats_message_count: number | null;
  stats_last_server_sequence: number | null;
  batch_ranges: BatchRange[];
  manifests: ManifestEntry[];
}

export interface R2InventoryObject {
  key: string;
  size: number;
  sha256: string | null;
}

export interface RecoveryIssue {
  archive_id: string | null;
  code:
    | "SEQUENCE_GAP"
    | "SEQUENCE_AHEAD"
    | "STATS_MISMATCH"
    | "BLOB_MISSING"
    | "BLOB_SIZE_MISMATCH"
    | "BLOB_CHECKSUM_MISMATCH"
    | "OBJECT_ORPHANED";
  detail: string;
}

export interface RecoveryReport {
  ok: boolean;
  recovery_point: {
    archives: number;
    total_records: number;
    max_server_sequence: number;
  };
  issues: RecoveryIssue[];
}

const MULTIPART_TEMP_PREFIX = "multipart/";

const checkSequences = (archive: ArchiveSnapshot, issues: RecoveryIssue[]): void => {
  const sorted = [...archive.batch_ranges].sort((a, b) => a.start - b.start);
  let expectedStart = 1;
  let tiled = true;
  for (const range of sorted) {
    if (range.start !== expectedStart || range.end < range.start) {
      tiled = false;
      break;
    }
    expectedStart = range.end + 1;
  }
  if (!tiled || expectedStart - 1 !== archive.last_server_sequence) {
    issues.push({
      archive_id: archive.archive_id,
      code: "SEQUENCE_GAP",
      detail: "sync_batches 序号范围未连续覆盖 1..last_server_sequence",
    });
  }
  if (archive.max_record_sequence > archive.last_server_sequence) {
    issues.push({
      archive_id: archive.archive_id,
      code: "SEQUENCE_AHEAD",
      detail: "archive_records 存在超过已提交序号的记录",
    });
  }
};

const checkStats = (archive: ArchiveSnapshot, issues: RecoveryIssue[]): void => {
  if (
    archive.stats_message_count !== archive.record_count ||
    archive.stats_last_server_sequence !== archive.last_server_sequence
  ) {
    issues.push({
      archive_id: archive.archive_id,
      code: "STATS_MISMATCH",
      detail: "archive_stats 与恢复出的记录数或序号不一致",
    });
  }
};

const checkBlobs = (
  archive: ArchiveSnapshot,
  inventoryByKey: Map<string, R2InventoryObject>,
  claimedKeys: Set<string>,
  issues: RecoveryIssue[],
): void => {
  for (const manifest of archive.manifests) {
    if (manifest.state !== "remote_ready") continue;
    const key = `${archive.archive_id}/${manifest.blob_id}`;
    claimedKeys.add(key);
    const object = inventoryByKey.get(key);
    if (object === undefined) {
      issues.push({
        archive_id: archive.archive_id,
        code: "BLOB_MISSING",
        detail: `remote_ready manifest 缺少对应对象：${manifest.blob_id}`,
      });
      continue;
    }
    if (object.size !== manifest.size) {
      issues.push({
        archive_id: archive.archive_id,
        code: "BLOB_SIZE_MISMATCH",
        detail: `对象尺寸与 manifest 不符：${manifest.blob_id}`,
      });
    }
    if (object.sha256 !== null && object.sha256 !== manifest.sha256) {
      issues.push({
        archive_id: archive.archive_id,
        code: "BLOB_CHECKSUM_MISMATCH",
        detail: `对象摘要与 manifest 不符：${manifest.blob_id}`,
      });
    }
  }
};

export const checkRecoveryPoint = (
  archives: ArchiveSnapshot[],
  inventory: R2InventoryObject[],
): RecoveryReport => {
  const issues: RecoveryIssue[] = [];
  const inventoryByKey = new Map(inventory.map((object) => [object.key, object]));
  const claimedKeys = new Set<string>();
  for (const archive of archives) {
    checkSequences(archive, issues);
    checkStats(archive, issues);
    checkBlobs(archive, inventoryByKey, claimedKeys, issues);
  }
  for (const object of inventory) {
    if (object.key.startsWith(MULTIPART_TEMP_PREFIX)) continue;
    if (!claimedKeys.has(object.key)) {
      issues.push({
        archive_id: null,
        code: "OBJECT_ORPHANED",
        detail: `对象存储存在未被任何 remote_ready manifest 认领的对象：${object.key}`,
      });
    }
  }
  return {
    ok: issues.length === 0,
    recovery_point: {
      archives: archives.length,
      total_records: archives.reduce((sum, archive) => sum + archive.record_count, 0),
      max_server_sequence: archives.reduce(
        (max, archive) => Math.max(max, archive.last_server_sequence),
        0,
      ),
    },
    issues,
  };
};

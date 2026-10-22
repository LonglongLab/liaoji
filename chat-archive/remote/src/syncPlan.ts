import { sha256Hex } from "./security";

export interface SyncRecordInput {
  record_key: string;
  conversation_id: string;
  created_at: number;
  content_text: string;
  content_sha256: string;
  source_instance_id?: string;
  schema_version?: string;
  // 只为让 `planCommit` 与 Python `plan_commit` 同一份规则而存在。本 Worker 的
  // v1 线格式没有这个字段、校验和也不覆盖它，所以路由层一律不传（见
  // syncRoutes.ts）；Worker 上它永远缺席，于是按「无类型」照旧记冲突。
  message_type?: string;
}

export interface SyncBatchBody {
  client_batch_id: string;
  // 设备内上传流标识：采集端 outbox 按 source account 各持 1..N 序号，
  // 游标与连续性检查按 (archive, device, source_account_id) 三元作用域。
  source_account_id: string;
  local_sequence_start: number;
  local_sequence_end: number;
  source_instance_id: string;
  schema_version: string;
  payload_sha256: string;
  records: SyncRecordInput[];
}

export const MAX_BATCH_RECORDS = 500;

// 与 Python `SyncBatch.checksum` 字节一致：键名字典序、`,`/`:` 分隔、非 ASCII 不转义。
export const canonicalPayloadChecksum = async (
  records: readonly SyncRecordInput[],
): Promise<string> => {
  const canonical = records.map((record) => ({
    content_sha256: record.content_sha256,
    content_text: record.content_text,
    conversation_id: record.conversation_id,
    created_at: record.created_at,
    record_key: record.record_key,
    ...(record.schema_version === undefined ? {} : { schema_version: record.schema_version }),
    ...(record.source_instance_id === undefined
      ? {}
      : { source_instance_id: record.source_instance_id }),
  }));
  return sha256Hex(JSON.stringify(canonical));
};

// 逐条移植 `ports/metadata.py::validate_batch`；返回 null 表示通过，否则给稳定中文消息。
export const validateBatch = async (body: SyncBatchBody): Promise<string | null> => {
  if (body.client_batch_id === "") {
    return "批次标识不能为空。";
  }
  if (body.source_instance_id === "" || body.schema_version === "") {
    return "来源实例和 schema 版本不能为空。";
  }
  if (body.records.length === 0 || body.records.length > MAX_BATCH_RECORDS) {
    return `记录条数必须在 1 到 ${String(MAX_BATCH_RECORDS)} 之间。`;
  }
  if (body.local_sequence_start <= 0 || body.local_sequence_end < body.local_sequence_start) {
    return "本地序号窗口非法。";
  }
  if ((await canonicalPayloadChecksum(body.records)) !== body.payload_sha256) {
    return "批次校验和与记录内容不匹配。";
  }
  const seen = new Set<string>();
  for (const record of body.records) {
    if (record.record_key === "" || seen.has(record.record_key)) {
      return "record_key 必须非空且在批内唯一。";
    }
    seen.add(record.record_key);
    if (
      record.source_instance_id !== undefined &&
      record.source_instance_id !== body.source_instance_id
    ) {
      return "记录来源实例与批次不一致。";
    }
    if (record.schema_version !== undefined && record.schema_version !== body.schema_version) {
      return "记录 schema 版本与批次不一致。";
    }
    if ((await sha256Hex(record.content_text)) !== record.content_sha256) {
      return "记录校验和与正文不匹配。";
    }
  }
  return null;
};

export interface SequencedRecord {
  record: SyncRecordInput;
  serverSequence: number;
  isInsert: boolean;
}

// 已存记录的身份：服务端记录提交设备用于冲突判定；来源实例只保留为诊断信息。
export interface StoredIdentity {
  content_sha256: string;
  origin_device_id: string | null;
  source_instance_id: string | null;
}

// 跨设备分歧账本项：只含校验和、设备/来源诊断与 schema 版本，绝不含正文。
export interface ConflictRecord {
  record_key: string;
  serverSequence: number;
  canonical_content_sha256: string;
  incoming_content_sha256: string;
  canonical_device_id: string | null;
  incoming_device_id: string;
  canonical_source_instance_id: string | null;
  incoming_source_instance_id: string | null;
  incoming_schema_version: string | null;
  detected_at: number;
}

export interface CommitPlan {
  changed: SequencedRecord[];
  conflicts: ConflictRecord[];
  sequenceStart: number;
  sequenceEnd: number;
  insertedCount: number;
  updatedCount: number;
}

// 逐条镜像 `ports/metadata.py::plan_commit`（insert/noop/version/conflict 四类）。
// 冲突判据必须使用认证得到的 device id；客户端提交的 source_instance_id 只用于诊断。
//
// 跨设备正文不同、且 incoming 声明了非空且不是 "text" 的 message_type 时归为
// noop：非文本消息的正文是解析器渲染的，两台机器版本不同渲染就不同；先到者本来
// 就胜，系统自己已裁决，不记账、也不占序号（占了序号却没有任何行承载会在变更流
// 里留洞）。"text"、空串与缺席（v1 发送方、本 Worker 的线格式）照旧记冲突。
export const planCommit = (
  records: readonly SyncRecordInput[],
  existing: ReadonlyMap<string, StoredIdentity>,
  lastServerSequence: number,
  incomingDeviceId: string,
  detectedAt: number,
): CommitPlan => {
  const changed: SequencedRecord[] = [];
  const conflicts: ConflictRecord[] = [];
  let sequence = lastServerSequence;
  let inserted = 0;
  let updated = 0;
  for (const record of records) {
    const stored = existing.get(record.record_key);
    const incomingSource = record.source_instance_id ?? null;
    if (stored === undefined) {
      sequence += 1;
      inserted += 1;
      changed.push({ record, serverSequence: sequence, isInsert: true });
    } else if (stored.content_sha256 === record.content_sha256) {
      continue;
    } else if (stored.origin_device_id === incomingDeviceId) {
      sequence += 1;
      updated += 1;
      changed.push({ record, serverSequence: sequence, isInsert: false });
    } else if (
      record.message_type !== undefined &&
      record.message_type !== "" &&
      record.message_type !== "text"
    ) {
      continue;
    } else {
      sequence += 1;
      conflicts.push({
        record_key: record.record_key,
        serverSequence: sequence,
        canonical_content_sha256: stored.content_sha256,
        incoming_content_sha256: record.content_sha256,
        canonical_device_id: stored.origin_device_id,
        incoming_device_id: incomingDeviceId,
        canonical_source_instance_id: stored.source_instance_id ?? null,
        incoming_source_instance_id: incomingSource,
        incoming_schema_version: record.schema_version ?? null,
        detected_at: detectedAt,
      });
    }
  }
  const eventCount = changed.length + conflicts.length;
  let sequenceStart = sequence - eventCount + 1;
  if (eventCount === 0) {
    sequence += 1;
    sequenceStart = sequence;
  }
  return {
    changed,
    conflicts,
    sequenceStart,
    sequenceEnd: sequence,
    insertedCount: inserted,
    updatedCount: updated,
  };
};

// 与 Python `normalize_search_text`（NFKC + casefold）对齐；JS 无 casefold，用
// toLowerCase 近似——中文内容一致，个别拉丁折叠特例（如 ß）不同，如实标注。
export const normalizeSearchText = (value: string): string =>
  value.normalize("NFKC").toLowerCase();

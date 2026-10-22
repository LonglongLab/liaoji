-- 与 PSU-0.7 `chat_archive.adapters.d1.metadata` 的 DDL 逐字一致的六张同步表，
-- 加上 1.8a 的 archives/devices 授权表。所有对象显式携带 archive_id。

CREATE TABLE IF NOT EXISTS archive_sequences (
    archive_id TEXT PRIMARY KEY,
    last_server_sequence INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS device_cursors (
    archive_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    last_local_sequence INTEGER NOT NULL,
    PRIMARY KEY (archive_id, device_id)
);

CREATE TABLE IF NOT EXISTS sync_batches (
    archive_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    client_batch_id TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    server_sequence_start INTEGER NOT NULL,
    server_sequence_end INTEGER NOT NULL,
    inserted_count INTEGER NOT NULL,
    updated_count INTEGER NOT NULL,
    PRIMARY KEY (archive_id, device_id, client_batch_id)
);

CREATE TABLE IF NOT EXISTS archive_records (
    archive_id TEXT NOT NULL,
    record_key TEXT NOT NULL,
    conversation_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    content_text TEXT NOT NULL,
    normalized_content TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    server_sequence INTEGER NOT NULL,
    PRIMARY KEY (archive_id, record_key)
);

CREATE INDEX IF NOT EXISTS ix_archive_records_cursor
    ON archive_records(archive_id, server_sequence, record_key);

CREATE TABLE IF NOT EXISTS archive_stats (
    archive_id TEXT PRIMARY KEY,
    message_count INTEGER NOT NULL,
    conversation_count INTEGER NOT NULL,
    last_server_sequence INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS blob_manifests (
    archive_id TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    PRIMARY KEY (archive_id, blob_id)
);

CREATE TABLE IF NOT EXISTS archives (
    id TEXT PRIMARY KEY,
    display_name TEXT NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS devices (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id),
    display_name TEXT NOT NULL,
    secret_hash TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'revoked')),
    created_at INTEGER NOT NULL,
    last_seen_at INTEGER
);

CREATE INDEX IF NOT EXISTS ix_devices_archive ON devices(archive_id);

-- PSU-2.3a：跨设备同账号分歧账本。只存校验和与来源实例，绝不含聊天正文。
CREATE TABLE record_conflicts (
    archive_id TEXT NOT NULL,
    server_sequence INTEGER NOT NULL,
    record_key TEXT NOT NULL,
    canonical_content_sha256 TEXT NOT NULL,
    incoming_content_sha256 TEXT NOT NULL,
    canonical_source_instance_id TEXT,
    incoming_source_instance_id TEXT,
    incoming_schema_version TEXT,
    PRIMARY KEY (archive_id, server_sequence)
);

CREATE INDEX ix_record_conflicts_key
    ON record_conflicts(archive_id, record_key);

ALTER TABLE sync_batches
    ADD COLUMN conflict_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE blob_manifests ADD COLUMN object_version TEXT;
ALTER TABLE blob_manifests ADD COLUMN verified_at INTEGER;
ALTER TABLE blob_manifests ADD COLUMN updated_at INTEGER;

CREATE TABLE IF NOT EXISTS blob_uploads (
    archive_id TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    client_upload_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    r2_upload_id TEXT NOT NULL,
    temp_key TEXT NOT NULL,
    object_key TEXT NOT NULL,
    total_size INTEGER NOT NULL,
    part_size INTEGER NOT NULL,
    part_count INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    plan_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    object_version TEXT,
    created_at INTEGER NOT NULL,
    updated_at INTEGER NOT NULL,
    PRIMARY KEY (archive_id, blob_id, session_id),
    UNIQUE (archive_id, device_id, client_upload_id)
);

CREATE TABLE IF NOT EXISTS blob_upload_parts (
    archive_id TEXT NOT NULL,
    blob_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    part_number INTEGER NOT NULL,
    expected_size INTEGER NOT NULL,
    expected_sha256 TEXT NOT NULL,
    etag TEXT,
    completed_at INTEGER,
    capability_hash TEXT,
    capability_expires_at INTEGER,
    PRIMARY KEY (archive_id, blob_id, session_id, part_number),
    FOREIGN KEY (archive_id, blob_id, session_id)
        REFERENCES blob_uploads(archive_id, blob_id, session_id)
        ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS ix_blob_uploads_device_client
    ON blob_uploads(archive_id, device_id, client_upload_id);

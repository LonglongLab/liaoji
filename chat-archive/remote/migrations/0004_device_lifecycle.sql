-- PSU-1.11 设备生命周期：一次性配对码与不含 Secret 的安全审计。

CREATE TABLE IF NOT EXISTS pairing_codes (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL REFERENCES archives(id),
    secret_hash TEXT NOT NULL,
    created_by_device_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK (status IN ('active', 'used', 'revoked')),
    max_uses INTEGER NOT NULL,
    used_count INTEGER NOT NULL,
    failed_attempts INTEGER NOT NULL,
    expires_at INTEGER NOT NULL,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_pairing_codes_archive ON pairing_codes(archive_id, status);

CREATE TABLE IF NOT EXISTS security_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    archive_id TEXT NOT NULL,
    event TEXT NOT NULL,
    actor_device_id TEXT,
    target_id TEXT,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS ix_security_audit_archive ON security_audit(archive_id, id);

ALTER TABLE devices ADD COLUMN revoked_at INTEGER;

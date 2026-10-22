-- 已发布 0005 的向前兼容修复：冲突判据改用认证设备，并为账本补检测时间。
-- 历史记录无法可靠反推设备/时间，NULL device 与 detected_at=0 明确表示未知。
ALTER TABLE archive_records
    ADD COLUMN origin_device_id TEXT;

ALTER TABLE record_conflicts
    ADD COLUMN canonical_device_id TEXT;

ALTER TABLE record_conflicts
    ADD COLUMN incoming_device_id TEXT;

ALTER TABLE record_conflicts
    ADD COLUMN detected_at INTEGER NOT NULL DEFAULT 0;

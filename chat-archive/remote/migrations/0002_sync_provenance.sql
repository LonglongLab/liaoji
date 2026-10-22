ALTER TABLE archive_records
    ADD COLUMN source_instance_id TEXT NOT NULL DEFAULT 'legacy';

ALTER TABLE archive_records
    ADD COLUMN schema_version TEXT NOT NULL DEFAULT '1';

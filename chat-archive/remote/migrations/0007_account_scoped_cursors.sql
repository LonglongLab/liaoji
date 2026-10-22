-- 设备游标补上账号维度（PSU-2.20 决策：服务端序号模型按上传流作用域）。
--
-- 本地采集端的 outbox 按 source account 各自维护 1..N 序号，一台设备可携带
-- 多条流；游标只按 (archive, device) 作用域时，第二个账号的序号 1 必然被判
-- "不连续"而拒绝——一台设备两个微信号的形态从未能同步。
--
-- 旧的按设备游标行无法在事后归属到某个账号，所以重建时丢弃而不是猜测：
-- 受影响设备从序号 1 重传，批次重放标识与记录 upsert 吸收全部重复，
-- at-least-once 语义不变。
DROP TABLE device_cursors;
CREATE TABLE device_cursors (
    archive_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    source_account_id TEXT NOT NULL,
    last_local_sequence INTEGER NOT NULL,
    PRIMARY KEY (archive_id, device_id, source_account_id)
);

-- 批次记录它属于哪条流；老行留 NULL——它们的流无法证明，编造会污染
-- 恢复演练要查的序号连续性。
ALTER TABLE sync_batches ADD COLUMN source_account_id TEXT;

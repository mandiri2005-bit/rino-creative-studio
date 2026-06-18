-- Replication health. Run on the SUBSCRIBER (target during forward sync; source during failback).
SELECT subname,
       received_lsn,
       latest_end_lsn,
       (latest_end_lsn - received_lsn) AS bytes_behind,
       last_msg_receipt_time
FROM pg_stat_subscription;

-- Per-table initial-copy state: 'd'=copying, 's'/'r'=synced/ready. All must be r before cutover.
SELECT srsubstate, count(*)
FROM pg_subscription_rel
GROUP BY 1 ORDER BY 1;

-- Run this on the PUBLISHER to watch slot-retained WAL (a dead subscriber pins source disk = prod risk):
--   SELECT slot_name, active,
--          pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS retained_wal
--   FROM pg_replication_slots;

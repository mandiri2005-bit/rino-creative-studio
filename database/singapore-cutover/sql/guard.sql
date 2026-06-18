-- GUARD: list every column-owned sequence whose value is BEHIND max(owning column).
-- MUST return ZERO rows before resuming writes. Non-empty result => HARD STOP (a nextval would
-- re-issue an existing PK). Verified on PG16 (flagged lag before setval, 0 rows after).
--
-- ceritaAI note: the entire app schema uses UUID primary keys (gen_random_uuid()); the ONLY
-- sequence is migrations_id_seq, which is not written during normal traffic. So for this schema
-- this guard is a near-noop safety net — keep it anyway, it is the universal correctness check.
SELECT o.seqname AS sequence,
       o.tblname AS owning_table,
       o.colname AS col,
       coalesce(pg_sequence_last_value(o.seqname), 0) AS seq_last_value,
       mx.col_max,
       mx.col_max - coalesce(pg_sequence_last_value(o.seqname), 0) AS lag
FROM (
  SELECT c.oid::regclass AS seqname,
         dep.refobjid::regclass AS tblname,
         a.attname AS colname
  FROM pg_class c
  JOIN pg_depend dep ON dep.objid = c.oid AND dep.deptype IN ('a','i')  -- a=serial default, i=identity
  JOIN pg_attribute a ON a.attrelid = dep.refobjid AND a.attnum = dep.refobjsubid
  WHERE c.relkind = 'S'
) o
CROSS JOIN LATERAL (
  SELECT (xpath('//max/text()',
                query_to_xml(format('SELECT max(%I) AS max FROM %s', o.colname, o.tblname),
                             true, false, ''))::text[])[1]::bigint AS col_max
) mx
WHERE mx.col_max IS NOT NULL
  AND mx.col_max > coalesce(pg_sequence_last_value(o.seqname), 0);

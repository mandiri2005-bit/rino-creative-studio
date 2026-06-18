-- RECONCILE digest. Run on BOTH source and target (run.sh reconcile does both) and DIFF the output.
-- Row counts must match table-for-table, and the money aggregates must match exactly, before
-- you trust the cutover (or the failback, run in reverse).
SELECT n.nspname||'.'||c.relname AS tbl,
       (xpath('//cnt/text()',
              query_to_xml(format('SELECT count(*) AS cnt FROM %I.%I', n.nspname, c.relname),
                           false, true, ''))::text[])[1]::bigint AS rows
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
WHERE c.relkind = 'r' AND n.nspname = 'public'
ORDER BY 1;

-- Money aggregates (the values that must NEVER diverge across a cutover).
SELECT 'credit_ledger SUM(delta)' AS metric, COALESCE(SUM(delta),0)::text AS value FROM credit_ledger
UNION ALL
SELECT 'credit_balances SUM(balance)', COALESCE(SUM(balance),0)::text FROM credit_balances
UNION ALL
SELECT 'subscriptions count', count(*)::text FROM subscriptions;

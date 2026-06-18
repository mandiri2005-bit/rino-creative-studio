-- ADVANCE every column-owned sequence to max(owning column). Run ON TARGET after drain,
-- before resuming writes. Self-executing via \gexec (generates one setval per sequence, runs it).
-- Verified on PG16. setval(seq, max, true) => next nextval returns max+1 (no PK collision).
SELECT format(
  'SELECT setval(pg_get_serial_sequence(%L,%L), (SELECT COALESCE(max(%I),1) FROM %I.%I), true);',
  quote_ident(n.nspname)||'.'||quote_ident(c.relname),  -- table for pg_get_serial_sequence
  a.attname,                                            -- column name
  a.attname, n.nspname, c.relname                       -- max(col) FROM schema.table
)
FROM pg_class c
JOIN pg_namespace n ON n.oid = c.relnamespace
JOIN pg_attribute a ON a.attrelid = c.oid AND a.attnum > 0 AND NOT a.attisdropped
WHERE c.relkind = 'r'
  AND n.nspname = 'public'
  AND pg_get_serial_sequence(quote_ident(n.nspname)||'.'||quote_ident(c.relname), a.attname) IS NOT NULL
\gexec

# Migrations

Applied in filename order by `database/migrate.js` (tracked in the `migrations`
table). Each file is wrapped `BEGIN; … COMMIT;` (the runner strips the outer pair).

## Numbering rule (READ BEFORE ADDING A MIGRATION)

**Migration numbers are allocated against `origin/main` only.** Before creating a
new migration, `git fetch` and use the next free number after the highest on
`origin/main`. Do **not** pick a number based on your feature branch alone.

Why: feature branches drift. `0031` was claimed independently by **two** branches
(`feat/accounting-foundation` → `0031_accounting_foundation.sql` and an early Dodo
draft → `0031_dodo_payments.sql`), which collide on merge. The Dodo work was
renumbered to `0032_payment_events.sql` to resolve it.

If two unmerged branches still end up with the same number, the one merging
**second** must renumber to the next free slot before merging.

> Future option (lower friction): switch to timestamped names
> (`YYYYMMDDHHMMSS_name.sql`) so parallel branches never collide. Not adopted yet
> to keep the existing zero-padded sequence readable.

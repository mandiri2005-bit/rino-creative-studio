# C-03 Schema/RLS Acceptance Pack V4

V4 supersedes V1, V2, and V3 after independent audits reproduced fourteen false acceptances across
the three rounds. A prior runner PASS is not approval. `AUDIT-FINDINGS.md` is required reading and the V4
runner is the only authority.

This immutable pack defines and tests the database safety boundary for the five Narasi continuity
stores. It is intentionally migration-only and offline.

Files:

- `C03-SCHEMA-RLS-CONTRACT.md`: normative schema, security, retention, and rollback requirements.
- `AUDIT-FINDINGS.md`: V1 rejection evidence and V2 root-cause closure requirements.
- `CLAUDE-C03-ORDER.md`: exact implementation authority and stop conditions.
- `SOURCE-BASELINE.json`: pinned backend/source and dirty-worktree evidence.
- `run_c03_acceptance.py`: the only accepted runner; real local PostgreSQL, no partial mode.
- `rollback_c03.sql`: pack-owned, non-destructive rollback rehearsal; never a product migration.
- `SHA256SUMS`: immutable pack manifest.

The runner refuses configured database URLs, starts PostgreSQL with Unix sockets and empty
`listen_addresses`, applies the complete repository migration chain through 0071, applies 0072 twice,
runs adversarial catalog/RLS/data-lifecycle checks, rehearses rollback, and reruns C-01 V2. It removes
its own temporary cluster after the run.

Required product result:

`/tmp/wt-narasi-perf/database/migrations/0072_narasi_continuity_schema.sql`

No migration or live database action is authorized by merely possessing this pack.

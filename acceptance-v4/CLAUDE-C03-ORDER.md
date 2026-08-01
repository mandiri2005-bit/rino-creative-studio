# Claude Final Order: C-03 Schema/RLS Migration Rework 3 (V4)

Execute this as one bounded implementation-and-verification pass. Do not spawn agents, subagents,
workflows, or background jobs. Do not send checkpoints. Read every required file first, implement the
one authorized migration, run the single mandatory runner, report once, and stop.

## Required reading in exact order

1. `README.md`, `AUDIT-FINDINGS.md`, and `C03-SCHEMA-RLS-CONTRACT.md` in this V4 pack.
2. `SOURCE-BASELINE.json`, `run_c03_acceptance.py`, `rollback_c03.sql`, and `SHA256SUMS` in this V4 pack.
3. CLEAN ledger Sections 5.7-6.3, 10-11, 8.37, 8.43, 8.52, 8.55, 8.58, and the new C-03
   authorization section.
4. Backend deploy runbook `/Users/rino/docs/IMPORTANT-wimba-backend-deploy-runbook.md` for context
   only; deployment remains forbidden.
5. Current complete source files in `/tmp/wt-narasi-perf`:
   - `database/migrate.js`
   - migrations `0001`, `0006`, `0011`, `0013`, `0016`, `0017`, `0051`, `0069`, `0070`, `0071`
   - `python/database.py` only to understand the existing 24-hour job cleanup interaction
6. C-01 V2 order, spec, baseline, tests, runner, and manifest.

No summary, memory note, chat transcript, or older rejected pack substitutes for these files.

## Authorized product edit

Edit exactly the existing rejected candidate:

`/tmp/wt-narasi-perf/database/migrations/0072_narasi_continuity_schema.sql`

Do not edit any other product, test, acceptance, ledger, migration, frontend, configuration, or
documentation file. Do not copy `rollback_c03.sql` into the migrations directory. Do not rename or
split the migration.

Implement every normative V1+V2+V3+V4 requirement. Preserve the complete accepted V3 behavior.
The only new implementation work is exact and bounded:

1. in `narasi_c03_guard_contract_insert()`, assign `NEW.updated_at := now()` on every accepted
   INSERT before `RETURN NEW`; and
2. in the `TG_OP = 'INSERT'` branch of `narasi_c03_guard_violation_update()`, assign
   `NEW.updated_at := now()` after lifecycle validation and before `RETURN NEW`.

Do not add literal-date exceptions, weaken caller validation, change UPDATE semantics, add a new
function, or edit any other product path. The inherited generalized outcomes remain mandatory:

1. add INSERT-time violation lifecycle enforcement: open, unresolved, zero attempts, no purge
   timestamp; evidence and purge timestamp can never coexist;
2. freeze attempt count together with state/timestamp after a terminal transition while preserving
   retention-only evidence purge;
3. make referenced-claim retention delete set only `claim_id` null, never `tenant_id`, and preserve
   the violation row;
4. validate audit timestamp component ranges before accepting PostgreSQL's parsed value; and
5. validate leading/trailing whitespace and nonblank content with a real whitespace class for all
   four named recovery metadata fields.

Preserve every previously accepted V1/V2 invariant, first/second application idempotency, RLS,
grants, retention/cascade behavior, and rollback residue checks. Do not weaken a check because an
initial test fails. Do not change the pack or its runner. If baseline or integrity fails before
editing, stop and report. Repair only 0072 and rerun the complete V4 runner from the beginning until
it passes.

## Mandatory command

```bash
LC_ALL=C python3 /Users/rino/docs/IMPORTANT-wimba-narasi-c03-schema-rls-acceptance-v4/run_c03_acceptance.py \
  --backend-worktree /tmp/wt-narasi-perf
```

This command creates and destroys only a disposable local PostgreSQL cluster under the OS temporary
directory. It must not use any `DATABASE_URL`, network endpoint, Docker daemon, Railway, Neon,
Supabase, or provider. The runner must exit 0 and print its exact final PASS line. There is no valid
substitute command and no quick/partial mode.

After it passes, run only these read-only checks:

```bash
cd /tmp/wt-narasi-perf
git diff --check
git status --short
```

## Forbidden actions

- No live, shared, staging, development, or production database access.
- No Railway/provider/network/Redis/object-store/billing call.
- No migration against Neon/Supabase or any configured `DATABASE_URL`.
- No runtime persistence wiring, feature flag, prompt or pipeline change.
- No acceptance-pack, repository-test, CLEAN-ledger, or runbook edit.
- No commit, push, merge, rebase, branch switch, stash, reset, worktree operation, or deployment.
- No agent/workflow delegation, checkpoint report, skipped test, altered expectation, or self-approval.

## Required final report

Report exactly, including the retained V2/V3 and new V4 closure-group totals:

1. pack manifest and source-baseline results;
2. backend HEAD/branch and the sole product path/hash;
3. local PostgreSQL version and proof that no configured/live database was used;
4. pre-C-03 migration count, first and second 0072 application results;
5. catalog/RLS/grant/function/FK/index test totals;
6. tenant A/B and unset-tenant adversarial totals;
7. contract, claim, violation, coverage, recovery, retention, cascade, and rollback totals;
8. inherited C-01 V2 result;
9. before/after status equality and `git diff --check`;
10. explicit zero live/git/remote action and any real residual limitation.

Before reporting, read your finished 0072 from start to finish and confirm both INSERT branches own
`updated_at`, while every V3 closure remains unchanged. A runner PASS does not excuse a known source
gap. Do not mark C-03 achieved. Stop for Codex's independent rerun and approval.

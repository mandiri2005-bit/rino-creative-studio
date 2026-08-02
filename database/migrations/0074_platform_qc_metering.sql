-- =====================================================================
-- 0074_platform_qc_metering.sql
--
-- L2B-METER: platform quality-control attempt metering.
--
-- Records PHYSICAL provider attempts for internal Canon-Lite QC on a DEDICATED table,
-- entirely separate from customer billing. Implements the ratified acceptance matrix
-- `WIMBA_CONT_PROJECT/L2B-METER-IMPLEMENTATION-ACCEPTANCE-MATRIX-001.md`
-- sha256 548fb8a862013a1069beae0d61b51519965f75a55d4804a4fe9641af3f771019 (81 rows).
--
-- WHY A DEDICATED TABLE AND NOT A COLUMN ON usage_logs (D-METER-1b)
--   usage_logs.cost_usd is `numeric NOT NULL DEFAULT 0`, so an attempt whose cost is not yet
--   known has nowhere to live: it would be recorded as ZERO, which is the exact
--   under-recording this package exists to prevent. Relaxing NOT NULL on the live customer
--   billing table to serve QC is not acceptable. A separate table also makes customer
--   isolation STRUCTURAL — v_pl_monthly / v_op_margin / v_cogs_by_provider cannot see these
--   rows at all, so there is no view filter to forget.
--   ==> usage_logs, its views, and database.log_usage are NOT TOUCHED by this migration.
--
-- WHY NUMBER 0074 (D-METER-14, D-METER-20/21)
--   0072 is claimed by the frozen Narasi C-03 pack (anchored at codex/c03-schema-frozen,
--   recorded in database/migrations/RESERVED.md). 0073 is quarantined under
--   database/quarantine/. 0070/0071 are this branch's own applied migrations. The number was
--   allocated against the DEPLOY branch per the corrected rule in migrations/README.md — not
--   against origin/main, which stops at 0035 and would have yielded a slot 40 migrations in
--   the past.
--
-- VERIFIED AGAINST PRODUCTION 2026-08-01/02 (READ-ONLY) BEFORE WRITING
--   migrations table            66 applied, last 0071_deferred_revenue_no_silent_fallback
--   server_version              PostgreSQL 18.4
--   tenants_plan_check          free|starter|plus|pro|ultra|enterprise      (0042:20)
--   gl_accounts                 code PK / name / type CHECK(...,'opex')     (0029:37)
--   neondb_owner                rolsuper=f  rolbypassrls=TRUE  rolcreaterole=TRUE
--   app_user                    rolsuper=f  rolbypassrls=f
--   usage_logs                  46 rows, all is_paid=TRUE; journal_entries 0 rows
--   platform_qc_reaper          does not exist (correct pre-0074 state)
--
--   rolcreaterole=TRUE is why step 7 is executable rather than aspirational.
--   rolbypassrls=TRUE on the owner is why FORCE RLS is NOT described anywhere here as a
--   protection against the owner: the GRANTS are. See step 9.
--
-- THE 0016 TRAP (step 6 is not optional)
--   0016_app_role.sql:31 ALTER DEFAULT PRIVILEGES ... GRANT ... ON TABLES TO app_user, so
--   BOTH new tables below are auto-granted SELECT,INSERT,UPDATE,DELETE the instant they are
--   created, and a later narrower GRANT is additive — only an explicit REVOKE strips it.
--   This hole has already shipped three times (gl_accounts, orphan_reversals,
--   journal_entries). Step 6 revokes; database/checks/app_user_privilege_audit.sql detects a
--   regression.
--
-- FORCE RLS AND SECURITY DEFINER — A CORRECTION TO PLANNING RECORD 003 §3
--   003 §3 said SECURITY DEFINER "bypasses the table's FORCE RLS. That is the point."
--   That is NOT generally true: FORCE ROW LEVEL SECURITY deliberately subjects the table
--   OWNER to its own policies, and a DEFINER function runs as the owner. In production the
--   write path happens to succeed only because neondb_owner carries rolbypassrls=TRUE — an
--   accident of that role, not a property of the design, and it would fail on any cluster
--   whose owner lacks BYPASSRLS (which is exactly how the acceptance harness runs, per
--   matrix §2.2).
--   ==> The platform_qc_usage policy keeps the ordinary tenant predicate and adds a narrow
--       DEFINER-only path: `current_user <> session_user` plus a transaction-local,
--       QC-specific GUC set inside the four usage-writing functions. The functions NEVER
--       touch app.current_tenant_id. An earlier draft did, and changed an unset pooled
--       customer session into the empty string on return, making the next ordinary RLS
--       predicate raise on `''::uuid`.
--       The dedicated GUC may reset to empty after the transaction; no customer policy reads
--       it, and the `current_user <> session_user` guard makes it useless to a direct caller.
--       Correctness no longer depends on the owner's BYPASSRLS bit.
--
-- REVERSAL (this repo is forward-only; database/rollback.js is a dev-only full reset)
--     DROP VIEW IF EXISTS public.v_platform_qc_cost;
--     DROP FUNCTION IF EXISTS public.platform_qc_kill_sync(text);
--     DROP FUNCTION IF EXISTS public.platform_qc_reap();
--     DROP FUNCTION IF EXISTS public.platform_qc_kill_is_armed();
--     DROP FUNCTION IF EXISTS public.platform_qc_arm_kill(text);
--     DROP FUNCTION IF EXISTS public.platform_qc_resolve_cost(uuid,integer,integer,numeric);
--     DROP FUNCTION IF EXISTS public.platform_qc_finish_attempt(uuid,text);
--     DROP FUNCTION IF EXISTS public.platform_qc_begin_attempt(
--         text,uuid,text,text,integer,integer,text,text,text,numeric,numeric,numeric);
--     DROP TRIGGER IF EXISTS platform_qc_kill_no_mutate ON public.platform_qc_kill;
--     DROP TRIGGER IF EXISTS platform_qc_kill_no_truncate ON public.platform_qc_kill;
--     DROP FUNCTION IF EXISTS public.platform_qc_kill_append_only();
--     DROP TABLE IF EXISTS public.platform_qc_kill;
--     DROP TABLE IF EXISTS public.platform_qc_usage;
--     DROP ROLE IF EXISTS platform_qc_reaper;
--     DELETE FROM gl_accounts WHERE code = '6740';
--     DELETE FROM tenants WHERE id = '670711f2-ecc9-5577-9bdf-cc77611d1b4b';
--     ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_plan_check;
--     ALTER TABLE tenants ADD  CONSTRAINT tenants_plan_check
--         CHECK (plan IN ('free','starter','plus','pro','ultra','enterprise'));
--   No customer row is written, altered or deleted, so reversal is complete and lossless.
--
-- DEPLOYMENT ORDER IS ABSOLUTE — SCHEMA FIRST, CODE SECOND (matrix A16, DG-1, DG-2)
--   1. apply this migration
--   2. verify READ-ONLY in production: pg_constraint, pg_get_viewdef,
--      information_schema.role_table_grants, pg_roles.rolbypassrls for platform_qc_reaper
--   3. only then deploy code that writes
--   Neither a schema dump nor the migration chain is evidence: at 12bc71f8
--   database/schema_live_raw.sql is a 0-byte blob and the root dump still declares the old
--   eight-value endpoint set. 0029/0042/'faq' are three live instances of code emitting a
--   value the CHECK did not yet accept, each swallowed by a caller's exception handler.
--   Production verification and deployment are DEPLOY GATES — not authorised by
--   GO L2B-METER IMPLEMENT.
--
-- NOT IN SCOPE: any provider call, measurement, or activation. Ledger §18.1 / D-METER-23 bar
-- every L2B provider call until L2C-BILLING-INTEGRITY completes. This migration is inert
-- until code is deployed under a separate token.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 1. tenants.plan gains 'platform' (D-METER-12)
--
--    Fourth widening of this CHECK family. It MUST be applied before any code emits the
--    value — 0042's own header records what happens otherwise: _setTenantPlan threw and the
--    failure was "silently swallowed → tenants.plan stayed stale".
-- ---------------------------------------------------------------------
ALTER TABLE tenants DROP CONSTRAINT IF EXISTS tenants_plan_check;
ALTER TABLE tenants ADD  CONSTRAINT tenants_plan_check
    CHECK (plan IN ('free','starter','plus','pro','ultra','enterprise','platform'));

COMMENT ON CONSTRAINT tenants_plan_check ON tenants IS
    'Global subscription ladder (0042) plus the internal platform-QC funding source (0074). '
    '''platform'' is NOT a customer plan: it identifies the single QC tenant and must be '
    'excluded from every customer statistic. Widen here BEFORE code emits a new value.';

-- ---------------------------------------------------------------------
-- 2. The platform QC tenant — fixed sentinel, never gen_random_uuid() (D-METER-26, A4)
--
--    670711f2-ecc9-5577-9bdf-cc77611d1b4b is derived reproducibly, not invented:
--      seed   = "wimba.canon-lite.l2b-meter.platform_qc.tenant.v1"
--      sha256 = 670711f2ecc985779bdfcc77611d1b4bbc9c46ecc8480fd60b272d469a5872dd
--             → first 16 bytes, version nibble set to 5, RFC 4122 variant bits set
--    A generated UUID would differ per environment and make the CHECK in step 4 unwritable.
--
--    plan='platform' is load-bearing: database.log_usage derives
--    `_paid = bool(_plan) and _plan != "free"`, and `_paid is False` is precisely the branch
--    that feeds credits.note_free_cost into the GLOBAL freecogs:{date} counter. A platform
--    tenant seeded as 'free' could therefore deny free-tier service to every user on the
--    platform. QC never goes through log_usage at all, but the seed must be safe even if
--    future code touches it.
--
--    is_active=FALSE is defence in depth. No QC write path filters on it (asserted by test).
--    .invalid is reserved by RFC 2606 and can never route.
-- ---------------------------------------------------------------------
-- `tenants` already has FORCE RLS. Even `ON CONFLICT DO NOTHING` evaluates the insert policy
-- before discovering the existing id, so a non-BYPASS owner cannot re-run this migration
-- unless the sentinel context is present. This helper is migration-session-only and is
-- dropped immediately. Its transaction-local tenant GUC cannot reach a pooled application
-- connection and does not enlarge the permanent seven-function surface.
CREATE FUNCTION pg_temp.platform_qc_seed_tenant()
RETURNS void
LANGUAGE sql
SET search_path = ''
AS $seed$
    SELECT pg_catalog.set_config(
        'app.current_tenant_id', '670711f2-ecc9-5577-9bdf-cc77611d1b4b', true);
    INSERT INTO public.tenants (id, name, slug, email, plan, is_active)
        VALUES ('670711f2-ecc9-5577-9bdf-cc77611d1b4b'::uuid,
                'Platform QC (internal, non-customer)',
                '__platform_qc__',
                'platform-qc@wimba.invalid',
                'platform',
                FALSE)
        ON CONFLICT (id) DO NOTHING
$seed$;

SELECT pg_temp.platform_qc_seed_tenant();
DROP FUNCTION pg_temp.platform_qc_seed_tenant();

COMMENT ON TABLE tenants IS
    'One row per organisation or individual customer, plus exactly one internal '
    'platform-QC tenant (plan=''platform'', 0074) that must be excluded from customer counts.';

-- ---------------------------------------------------------------------
-- 3. GL account 6740 — a NEW opex line, seeded BEFORE anything can reference it (D-METER-3)
--
--    Platform QC consumes provider tokens with NO customer revenue attached, so it is
--    neither COGS (5xxx, which v_op_margin pairs against revenue) nor free-tier customer
--    acquisition (6100, a marketing-spend story). Production's chart has a free slot between
--    6730 Beban Sewa Kantor and 6750 Beban Pemasaran & Iklan.
--
--    There is NO revenue leg and therefore no revenue column anywhere in this package.
-- ---------------------------------------------------------------------
INSERT INTO gl_accounts (code, name, type) VALUES
    ('6740','Beban Riset & QC Platform (internal, non-revenue)','opex')
ON CONFLICT (code) DO NOTHING;

-- ---------------------------------------------------------------------
-- 4. platform_qc_usage — one row per PHYSICAL provider attempt, including retries
--
--    attempt_state and cost_state are ORTHOGONAL (D-METER-16b/19c). A failed or timed-out
--    attempt can still have been billed, and its cost can become known later; collapsing
--    them into one enum makes that common case unrepresentable. Every combination is legal,
--    including (timeout, known) and (succeeded, unknown). The one thing forbidden is the
--    original sin: cost_state='unknown' cannot carry a number, so an unknown cost can never
--    be read as zero. A genuine zero is cost_state='known', cost_usd=0 — an assertion.
--
--    Identity is the TUPLE, not a concatenated key (D-METER-16b). Bounds on component length
--    never prevented phase='a:b',unit_index=1 colliding with phase='a',unit_index='b:1' once
--    concatenated, so the concatenation is removed entirely and the database enforces
--    identity directly. There is no string to forge and no separator to inject.
--
--    run_id is load-bearing: the narasi dispatcher enqueues with {"attempts": 2}, so BullMQ
--    genuinely re-runs a job — those are NEW physical calls costing real money. A key built
--    only from job+phase+unit+ordinal would repeat on the retry and silently discard the
--    second run's spend.
--
--    NO FK on job_uuid/tenant_id (D-METER-18): cost evidence is a financial record of money
--    already spent and must survive job purge, tenant cleanup and retention sweeps. CASCADE
--    would destroy it, SET NULL would strip provenance, RESTRICT would make routine deletion
--    fail and push someone toward deleting the evidence. The sentinel CHECK replaces the
--    referential guarantee.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.platform_qc_usage (
    id                          UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id                   UUID NOT NULL,
    cost_center                 TEXT NOT NULL DEFAULT 'platform_qc',

    -- identity
    run_id                      TEXT NOT NULL,
    job_uuid                    UUID NOT NULL,
    job_external_id             TEXT,
    phase                       TEXT NOT NULL,
    unit_index                  INTEGER NOT NULL,
    attempt_ordinal             INTEGER NOT NULL,

    -- lifecycle
    attempt_state               TEXT NOT NULL DEFAULT 'attempted',
    attempt_state_at            TIMESTAMPTZ NOT NULL DEFAULT now(),
    attempt_timeout_s           NUMERIC NOT NULL,
    deadline_at                 TIMESTAMPTZ NOT NULL,

    -- cost
    cost_state                  TEXT NOT NULL DEFAULT 'unknown',
    cost_usd                    NUMERIC(14,6),
    cost_resolved_at            TIMESTAMPTZ,
    provider_reported_cost_usd  NUMERIC(14,6),

    -- rate provenance, frozen onto the row so cost is reconstructible without a rate table
    provider                    TEXT NOT NULL,
    model_upstream              TEXT NOT NULL,
    pricing_version             TEXT NOT NULL,
    rate_in_usd_per_m           NUMERIC(12,6) NOT NULL,
    rate_out_usd_per_m          NUMERIC(12,6) NOT NULL,
    tokens_in                   INTEGER,
    tokens_out                  INTEGER,

    gl_opex_code                TEXT NOT NULL REFERENCES gl_accounts(code),
    attempted_at                TIMESTAMPTZ NOT NULL DEFAULT now(),

    CONSTRAINT platform_qc_attempt_identity
        UNIQUE (run_id, job_uuid, phase, unit_index, attempt_ordinal),

    CONSTRAINT platform_qc_cost_center_check
        CHECK (cost_center = 'platform_qc'),

    -- the sentinel is a CONSTRAINT, not a convention: no FK enforces tenant_id (D-METER-18a)
    CONSTRAINT platform_qc_tenant_sentinel
        CHECK (tenant_id = '670711f2-ecc9-5577-9bdf-cc77611d1b4b'::uuid),

    CONSTRAINT platform_qc_attempt_state_check
        CHECK (attempt_state IN ('attempted','succeeded','failed','timeout')),
    CONSTRAINT platform_qc_cost_state_check
        CHECK (cost_state IN ('unknown','known')),

    -- cost is present IF AND ONLY IF the state says it is known
    CONSTRAINT platform_qc_cost_presence
        CHECK ((cost_state = 'known') = (cost_usd IS NOT NULL)),
    CONSTRAINT platform_qc_cost_resolved_at
        CHECK ((cost_state = 'known') = (cost_resolved_at IS NOT NULL)),

    -- an unbounded timeout produces a deadline in the past (instantly reapable) or years
    -- away (never reapable); both are silent
    CONSTRAINT platform_qc_timeout_bounds
        CHECK (attempt_timeout_s > 0 AND attempt_timeout_s <= 900),

    CONSTRAINT platform_qc_cost_bounds
        CHECK (cost_usd IS NULL OR (cost_usd >= 0 AND cost_usd <= 100)),
    CONSTRAINT platform_qc_provider_cost_bounds
        CHECK (provider_reported_cost_usd IS NULL
               OR (provider_reported_cost_usd >= 0 AND provider_reported_cost_usd <= 100)),
    CONSTRAINT platform_qc_token_bounds
        CHECK (coalesce(tokens_in, 0)  BETWEEN 0 AND 10000000
           AND coalesce(tokens_out, 0) BETWEEN 0 AND 10000000),
    CONSTRAINT platform_qc_rate_bounds
        CHECK (rate_in_usd_per_m  >= 0 AND rate_in_usd_per_m  <= 10000
           AND rate_out_usd_per_m >= 0 AND rate_out_usd_per_m <= 10000),
    CONSTRAINT platform_qc_ordinal_bounds
        CHECK (attempt_ordinal BETWEEN 1 AND 1000 AND unit_index BETWEEN 0 AND 10000),

    -- `phase` is a CATALOG, not an enum (D-METER-22, ledger §18.3.5): a CHECK enumerating
    -- its values would recreate the 0029/0042/faq drift inside the package written to
    -- prevent it. Length bound only; the value set is governed by a versioned source catalog
    -- plus the D-METER-11 parity test.
    CONSTRAINT platform_qc_text_bounds
        CHECK (length(run_id) <= 64
           AND length(phase) <= 64
           AND length(provider) <= 64
           AND length(model_upstream) <= 128
           AND length(pricing_version) <= 64
           AND (job_external_id IS NULL OR length(job_external_id) <= 64)),

    -- One-argument btrim() strips SPACE ONLY — not tab, newline, CR, form feed or vertical
    -- tab. Measured on PostgreSQL 18.4: btrim(chr(11)) = chr(11), not ''. A phase of "\t"
    -- would otherwise pass a naive non-empty check and enter the identity tuple as a
    -- distinct, invisible value. The set is spelled out rather than defaulted.
    -- \x0B is byte-identical to \v here (both give 20090a0d0c0b) and is used because \v is
    -- absent from the documented escape table — documentation-safety, not a defect fix.
    CONSTRAINT platform_qc_identity_nonempty
        CHECK (btrim(run_id,          E' \t\n\r\f\x0B') <> ''
           AND btrim(phase,           E' \t\n\r\f\x0B') <> ''
           AND btrim(provider,        E' \t\n\r\f\x0B') <> ''
           AND btrim(model_upstream,  E' \t\n\r\f\x0B') <> ''
           AND btrim(pricing_version, E' \t\n\r\f\x0B') <> ''
           AND (job_external_id IS NULL
                OR btrim(job_external_id, E' \t\n\r\f\x0B') <> ''))
);

CREATE INDEX IF NOT EXISTS idx_platform_qc_usage_open
    ON public.platform_qc_usage (deadline_at)
    WHERE attempt_state = 'attempted';
CREATE INDEX IF NOT EXISTS idx_platform_qc_usage_job
    ON public.platform_qc_usage (job_uuid, run_id);

COMMENT ON TABLE public.platform_qc_usage IS
    'One row per PHYSICAL provider attempt for internal Canon-Lite QC (L2B-METER, 0074). '
    'Platform-funded opex: there is deliberately no revenue, is_paid or credits column — the '
    'absence is the guarantee. Never joined to customer billing; usage_logs is untouched.';
COMMENT ON COLUMN public.platform_qc_usage.run_id IS
    'Per executor run. Keeps a BullMQ queue-level re-run ({"attempts":2}) distinct from a '
    'sink-level retry of the same physical attempt.';
COMMENT ON COLUMN public.platform_qc_usage.provider_reported_cost_usd IS
    'Evidence for invoice reconciliation ONLY. Never authoritative: cost_usd is always '
    'recomputed from the rates frozen on this row.';

-- ---------------------------------------------------------------------
-- 5. platform_qc_kill — the append-only kill-latch AUTHORITY (D-METER-9a/9b/9d)
--
--    Redis holds only a NEGATIVE cache: presence means armed, and ABSENCE means "consult
--    Postgres" — never "clear". The application credential is shared and therefore holds DEL
--    on every key including the latch, so a design resting on "the app would not DEL" is not
--    a design. Making absence harmless removes the capability question entirely.
--
--    Total order is `id`, never created_at: timestamps tie and can go backwards. Identity
--    values are assigned at insert but become visible at commit, so writers additionally take
--    an advisory transaction lock to serialise arm and clear genuinely.
--
--    Three events. 'clearing' exists because after the operator inserts a clear but BEFORE
--    the Redis key is deleted, the state is cache=armed/authority=cleared — which is exactly
--    the damage signature the reaper's repair is built to fix, so the reaper would re-arm the
--    latch mid-reset. is_armed() treats 'clearing' as still-armed, and the reaper skips
--    repair while it stands. A row survives a dropped operator session; an advisory lock
--    does not.
-- ---------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS public.platform_qc_kill (
    id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event       TEXT NOT NULL CHECK (event IN ('armed','clearing','cleared')),
    reason_code TEXT NOT NULL CHECK (length(reason_code) <= 64
                                     AND btrim(reason_code, E' \t\n\r\f\x0B') <> ''),
    actor       TEXT NOT NULL CHECK (actor IN ('application','operator')),
    created_at  TIMESTAMPTZ NOT NULL DEFAULT now()
);

COMMENT ON TABLE public.platform_qc_kill IS
    'Append-only kill-latch authority for L2B-METER. Newest row by id is the effective state. '
    'reason_code is a BOUNDED CODE, never an exception string.';

-- UPDATE, DELETE and TRUNCATE are forbidden by TRIGGER, not only by grant. The owner can drop
-- the triggers — acceptable, because dropping one is a deliberate visible act whereas an
-- ordinary destructive statement is a typo.
CREATE OR REPLACE FUNCTION public.platform_qc_kill_append_only()
RETURNS TRIGGER LANGUAGE plpgsql SET search_path = '' AS $$
BEGIN
    RAISE EXCEPTION 'platform_qc_kill is append-only (attempted %)', TG_OP
        USING ERRCODE = 'raise_exception';
END;
$$;

DROP TRIGGER IF EXISTS platform_qc_kill_no_mutate ON public.platform_qc_kill;
CREATE TRIGGER platform_qc_kill_no_mutate
    BEFORE UPDATE OR DELETE ON public.platform_qc_kill
    FOR EACH ROW EXECUTE FUNCTION public.platform_qc_kill_append_only();
DROP TRIGGER IF EXISTS platform_qc_kill_no_truncate ON public.platform_qc_kill;
CREATE TRIGGER platform_qc_kill_no_truncate
    BEFORE TRUNCATE ON public.platform_qc_kill
    FOR EACH STATEMENT EXECUTE FUNCTION public.platform_qc_kill_append_only();

-- Seed one initial 'cleared' row so the empty-table case is never reached in practice. The
-- reader ALSO carries a fail-closed COALESCE(...,true) default: both, because a fail-closed
-- default that is never exercised is untested, and a seed someone later deletes would
-- otherwise silently invert the latch.
INSERT INTO public.platform_qc_kill (event, reason_code, actor)
SELECT 'cleared', 'initial_seed', 'operator'
 WHERE NOT EXISTS (SELECT 1 FROM public.platform_qc_kill);

-- ---------------------------------------------------------------------
-- 6. REVOKE the 0016 auto-grant (matrix B1, B5)
--
--    Both tables were granted SELECT,INSERT,UPDATE,DELETE to app_user the instant they were
--    created, above. app_user gets NO table privilege at all — not even SELECT. Reading is
--    owner-only (D-METER-17); writing is via the functions in step 8 and nowhere else.
-- ---------------------------------------------------------------------
REVOKE ALL ON public.platform_qc_usage FROM app_user;
REVOKE ALL ON public.platform_qc_kill  FROM app_user;
REVOKE ALL ON public.platform_qc_usage FROM PUBLIC;
REVOKE ALL ON public.platform_qc_kill  FROM PUBLIC;

-- ---------------------------------------------------------------------
-- 7. The reaper role — created BEFORE any grant can name it (matrix B6, B7, D-METER-24/33)
--
--    NOBYPASSRLS is explicit and asserted by the privilege audit. Production's neondb_owner
--    is NOT a superuser yet holds rolbypassrls=TRUE, so reusing BACKUP_DATABASE_URL for a
--    scheduled unattended job would have handed it a role that bypasses row security on
--    every table in the database in order to perform one bounded UPDATE.
--
--    LOGIN with no password here: the credential is issued out of band. USAGE ON SCHEMA
--    public is REQUIRED — without it the two EXECUTE grants in step 8 are unreachable. It is
--    a requirement, not a leak.
-- ---------------------------------------------------------------------
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'platform_qc_reaper') THEN
        CREATE ROLE platform_qc_reaper LOGIN NOBYPASSRLS;
    ELSIF EXISTS (
        SELECT 1 FROM pg_roles
         WHERE rolname = 'platform_qc_reaper' AND rolbypassrls
    ) THEN
        -- A role without BYPASSRLS may not ALTER that attribute even to its existing FALSE
        -- value. Only issue the change when the catalogue says there is something to remove.
        ALTER ROLE platform_qc_reaper NOBYPASSRLS;
    END IF;
END;
$$;

GRANT USAGE ON SCHEMA public TO platform_qc_reaper;
REVOKE ALL ON ALL TABLES    IN SCHEMA public FROM platform_qc_reaper;
REVOKE ALL ON ALL SEQUENCES IN SCHEMA public FROM platform_qc_reaper;

-- ---------------------------------------------------------------------
-- 8. The seven functions (matrix §3.2, B2, B3)
--
--    All seven: SECURITY DEFINER, SET search_path = '', every user object fully qualified,
--    REVOKE EXECUTE FROM PUBLIC. (pg_catalog remains implicitly searched even with an empty
--    search_path, so built-ins resolve; only user objects need qualifying.)
--
--    app_user               5  begin / finish / resolve_cost / arm_kill / kill_is_armed
--    platform_qc_reaper     2  reap / kill_sync
--    PUBLIC                 0  on all seven
--
--    REVOKE ... FROM PUBLIC is not decoration: SECURITY DEFINER functions are executable by
--    PUBLIC by default, so without it each function is a WIDER hole than the table grant it
--    replaces.
--
--    Every function that writes platform_qc_usage activates the narrow DEFINER-only policy
--    path with a QC-specific transaction-local GUC. app.current_tenant_id is never touched,
--    so a pooled customer connection keeps its exact prior tenant state — see the header.
-- ---------------------------------------------------------------------

-- 8.1 arm_kill — created first because begin/resolve call it on conflict.
CREATE OR REPLACE FUNCTION public.platform_qc_arm_kill(p_reason_code text)
RETURNS TABLE (armed boolean, was_already_armed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_newest text;
BEGIN
    IF p_reason_code IS NULL OR btrim(p_reason_code, E' \t\n\r\f\x0B') = ''
       OR length(p_reason_code) > 64 THEN
        RAISE EXCEPTION 'platform_qc_bounds:reason_code' USING ERRCODE = 'check_violation';
    END IF;

    -- identity values are assigned at insert but visible at commit, so serialise genuinely
    PERFORM pg_advisory_xact_lock(hashtext('platform_qc_kill'));

    SELECT k.event INTO v_newest
      FROM public.platform_qc_kill k ORDER BY k.id DESC LIMIT 1;

    IF v_newest = 'armed' THEN
        -- idempotent: repeated failures must not grow the table without bound. The bounded
        -- qc_meter:write_fail counter records VOLUME; this table records STATE.
        RETURN QUERY SELECT true, true;
        RETURN;
    END IF;

    INSERT INTO public.platform_qc_kill (event, reason_code, actor)
    VALUES ('armed', p_reason_code, 'application');

    RETURN QUERY SELECT true, false;
END;
$$;

-- 8.2 kill_is_armed — the reader. 'clearing' counts as ARMED so L2B stays blocked mid-reset.
--     COALESCE(...,true) is the fail-closed default for an empty table.
CREATE OR REPLACE FUNCTION public.platform_qc_kill_is_armed()
RETURNS boolean
LANGUAGE sql STABLE SECURITY DEFINER SET search_path = '' AS $$
    SELECT COALESCE(
      (SELECT k.event IN ('armed','clearing')
         FROM public.platform_qc_kill k ORDER BY k.id DESC LIMIT 1), true);
$$;

-- 8.3 begin_attempt — conditional insert, then a SEVEN-FIELD provenance comparison.
--     Identity alone is not enough: the same tuple arriving with a different provider,
--     model, pricing_version, rate or timeout is not a replay, it is two different physical
--     calls claiming one identity. Accepting that silently either double-counts or loses
--     spend, so it arms the latch instead.
CREATE OR REPLACE FUNCTION public.platform_qc_begin_attempt(
    p_run_id             text,
    p_job_uuid           uuid,
    p_job_external_id    text,
    p_phase              text,
    p_unit_index         integer,
    p_attempt_ordinal    integer,
    p_provider           text,
    p_model_upstream     text,
    p_pricing_version    text,
    p_rate_in_usd_per_m  numeric,
    p_rate_out_usd_per_m numeric,
    p_attempt_timeout_s  numeric
) RETURNS TABLE (attempt_id uuid, outcome text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_sentinel CONSTANT uuid := '670711f2-ecc9-5577-9bdf-cc77611d1b4b';
    v_ws       CONSTANT text := E' \t\n\r\f\x0B';
    v_grace    numeric;
    v_id       uuid;
    v_row      public.platform_qc_usage%ROWTYPE;
BEGIN
    -- Bounds are validated HERE as well as by the table CHECKs, so the caller receives a
    -- BOUNDED CODE rather than a raw constraint message. log_usage's own
    -- log.error("log_usage: %s", e) renders constraint names and offending values into the
    -- log; this path must never do that.
    IF p_run_id IS NULL OR btrim(p_run_id, v_ws) = '' OR length(p_run_id) > 64
        THEN RAISE EXCEPTION 'platform_qc_bounds:run_id'          USING ERRCODE='check_violation'; END IF;
    IF p_job_uuid IS NULL
        THEN RAISE EXCEPTION 'platform_qc_bounds:job_uuid'        USING ERRCODE='check_violation'; END IF;
    IF p_phase IS NULL OR btrim(p_phase, v_ws) = '' OR length(p_phase) > 64
        THEN RAISE EXCEPTION 'platform_qc_bounds:phase'           USING ERRCODE='check_violation'; END IF;
    IF p_provider IS NULL OR btrim(p_provider, v_ws) = '' OR length(p_provider) > 64
        THEN RAISE EXCEPTION 'platform_qc_bounds:provider'        USING ERRCODE='check_violation'; END IF;
    IF p_model_upstream IS NULL OR btrim(p_model_upstream, v_ws) = '' OR length(p_model_upstream) > 128
        THEN RAISE EXCEPTION 'platform_qc_bounds:model_upstream'  USING ERRCODE='check_violation'; END IF;
    IF p_pricing_version IS NULL OR btrim(p_pricing_version, v_ws) = '' OR length(p_pricing_version) > 64
        THEN RAISE EXCEPTION 'platform_qc_bounds:pricing_version' USING ERRCODE='check_violation'; END IF;
    IF p_job_external_id IS NOT NULL
       AND (btrim(p_job_external_id, v_ws) = '' OR length(p_job_external_id) > 64)
        THEN RAISE EXCEPTION 'platform_qc_bounds:job_external_id' USING ERRCODE='check_violation'; END IF;
    IF p_unit_index IS NULL OR p_unit_index < 0 OR p_unit_index > 10000
        THEN RAISE EXCEPTION 'platform_qc_bounds:unit_index'      USING ERRCODE='check_violation'; END IF;
    IF p_attempt_ordinal IS NULL OR p_attempt_ordinal < 1 OR p_attempt_ordinal > 1000
        THEN RAISE EXCEPTION 'platform_qc_bounds:attempt_ordinal' USING ERRCODE='check_violation'; END IF;
    IF p_attempt_timeout_s IS NULL OR p_attempt_timeout_s <= 0 OR p_attempt_timeout_s > 900
        THEN RAISE EXCEPTION 'platform_qc_bounds:attempt_timeout_s' USING ERRCODE='check_violation'; END IF;
    IF p_rate_in_usd_per_m IS NULL OR p_rate_in_usd_per_m < 0 OR p_rate_in_usd_per_m > 10000
        THEN RAISE EXCEPTION 'platform_qc_bounds:rate_in'         USING ERRCODE='check_violation'; END IF;
    IF p_rate_out_usd_per_m IS NULL OR p_rate_out_usd_per_m < 0 OR p_rate_out_usd_per_m > 10000
        THEN RAISE EXCEPTION 'platform_qc_bounds:rate_out'        USING ERRCODE='check_violation'; END IF;

    PERFORM pg_catalog.set_config('app.platform_qc_definer', 'on', true);

    -- grace is a pure function of THIS attempt's timeout: floored at 30s so a very short
    -- timeout still gets usable slack, capped at 300s so a long one cannot park a row beyond
    -- operator patience. Not a global staleness constant.
    v_grace := LEAST(GREATEST(0.5 * p_attempt_timeout_s, 30), 300);

    INSERT INTO public.platform_qc_usage (
        tenant_id, run_id, job_uuid, job_external_id, phase, unit_index, attempt_ordinal,
        attempt_state, attempt_timeout_s, deadline_at,
        provider, model_upstream, pricing_version,
        rate_in_usd_per_m, rate_out_usd_per_m, gl_opex_code)
    VALUES (
        v_sentinel, p_run_id, p_job_uuid, p_job_external_id, p_phase, p_unit_index,
        p_attempt_ordinal, 'attempted', p_attempt_timeout_s,
        now() + ((p_attempt_timeout_s + v_grace) * INTERVAL '1 second'),
        p_provider, p_model_upstream, p_pricing_version,
        p_rate_in_usd_per_m::numeric(12,6), p_rate_out_usd_per_m::numeric(12,6), '6740')
    ON CONFLICT ON CONSTRAINT platform_qc_attempt_identity DO NOTHING
    RETURNING id INTO v_id;

    IF v_id IS NOT NULL THEN
        RETURN QUERY SELECT v_id, 'inserted'::text;
        RETURN;
    END IF;

    SELECT * INTO v_row FROM public.platform_qc_usage u
     WHERE u.run_id = p_run_id AND u.job_uuid = p_job_uuid AND u.phase = p_phase
       AND u.unit_index = p_unit_index AND u.attempt_ordinal = p_attempt_ordinal;

    -- Rates are cast to the COLUMN's scale before comparison. Without this every legitimate
    -- replay would conflict: a float 0.075 arrives as
    -- 0.074999999999999997224442438437108648940920829772949218750 while the column stores
    -- 0.075000, and IS NOT DISTINCT FROM between them is false. That defect would have armed
    -- the latch and halted L2B on the FIRST replay in production. Sub-scale precision is
    -- normalisation, not different provenance; a difference that survives the cast (0.0751)
    -- is still a genuine conflict.
    IF  v_row.provider           IS NOT DISTINCT FROM p_provider
    AND v_row.model_upstream     IS NOT DISTINCT FROM p_model_upstream
    AND v_row.pricing_version    IS NOT DISTINCT FROM p_pricing_version
    AND v_row.rate_in_usd_per_m  IS NOT DISTINCT FROM p_rate_in_usd_per_m::numeric(12,6)
    AND v_row.rate_out_usd_per_m IS NOT DISTINCT FROM p_rate_out_usd_per_m::numeric(12,6)
    AND v_row.attempt_timeout_s  IS NOT DISTINCT FROM p_attempt_timeout_s
    AND v_row.job_external_id    IS NOT DISTINCT FROM p_job_external_id
    THEN
        RETURN QUERY SELECT v_row.id, 'replay'::text;
    ELSE
        PERFORM public.platform_qc_arm_kill('begin_provenance_conflict');
        RETURN QUERY SELECT v_row.id, 'conflict'::text;
    END IF;
END;
$$;

-- 8.4 finish_attempt — lifecycle CAS ONLY. Separate from cost so a lost race costs a
--     lifecycle label, never a dollar.
CREATE OR REPLACE FUNCTION public.platform_qc_finish_attempt(
    p_attempt_id uuid, p_attempt_state text)
RETURNS TABLE (lifecycle_applied boolean, current_state text)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_n        integer;
    v_state    text;
BEGIN
    IF p_attempt_state IS NULL OR p_attempt_state NOT IN ('succeeded','failed','timeout') THEN
        RAISE EXCEPTION 'platform_qc_bounds:attempt_state' USING ERRCODE = 'check_violation';
    END IF;

    PERFORM pg_catalog.set_config('app.platform_qc_definer', 'on', true);

    UPDATE public.platform_qc_usage
       SET attempt_state = p_attempt_state, attempt_state_at = now()
     WHERE id = p_attempt_id AND attempt_state = 'attempted';
    GET DIAGNOSTICS v_n = ROW_COUNT;

    SELECT u.attempt_state INTO v_state
      FROM public.platform_qc_usage u WHERE u.id = p_attempt_id;

    -- v_state NULL means no such row; the caller logs a bounded late_finish/unknown_attempt
    RETURN QUERY SELECT (v_n = 1), v_state;
END;
$$;

-- 8.5 resolve_cost — cost is COMPUTED from the rates frozen on the row, never asserted by
--     the caller. p_provider_reported_cost_usd is stored as invoice evidence only and has NO
--     DEFAULT: a nullable parameter nobody supplies is a dead path, so every call site must
--     state it (NULL when the provider reports nothing).
CREATE OR REPLACE FUNCTION public.platform_qc_resolve_cost(
    p_attempt_id                 uuid,
    p_tokens_in                  integer,
    p_tokens_out                 integer,
    p_provider_reported_cost_usd numeric)
RETURNS TABLE (outcome text, cost_usd numeric)
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_row      public.platform_qc_usage%ROWTYPE;
    v_cost     numeric(14,6);
BEGIN
    IF p_tokens_in IS NULL OR p_tokens_in < 0 OR p_tokens_in > 10000000
        THEN RAISE EXCEPTION 'platform_qc_bounds:tokens_in'  USING ERRCODE='check_violation'; END IF;
    IF p_tokens_out IS NULL OR p_tokens_out < 0 OR p_tokens_out > 10000000
        THEN RAISE EXCEPTION 'platform_qc_bounds:tokens_out' USING ERRCODE='check_violation'; END IF;
    IF p_provider_reported_cost_usd IS NOT NULL
       AND (p_provider_reported_cost_usd < 0 OR p_provider_reported_cost_usd > 100)
        THEN RAISE EXCEPTION 'platform_qc_bounds:provider_reported_cost' USING ERRCODE='check_violation'; END IF;

    PERFORM pg_catalog.set_config('app.platform_qc_definer', 'on', true);

    SELECT * INTO v_row FROM public.platform_qc_usage u WHERE u.id = p_attempt_id;

    IF NOT FOUND THEN
        -- the caller holds a handle to a row that does not exist
        PERFORM public.platform_qc_arm_kill('resolve_cost_missing');
        RETURN QUERY SELECT 'missing'::text, NULL::numeric;
        RETURN;
    END IF;

    -- round(numeric, 6) rounds half AWAY FROM ZERO. The double precision overload rounds
    -- half-to-even, so a silent type change here would move money.
    v_cost := round((p_tokens_in::numeric  * v_row.rate_in_usd_per_m
                   + p_tokens_out::numeric * v_row.rate_out_usd_per_m) / 1000000, 6);
    IF v_cost < 0 OR v_cost > 100
        THEN RAISE EXCEPTION 'platform_qc_bounds:cost_usd' USING ERRCODE='check_violation'; END IF;

    IF v_row.cost_state = 'unknown' THEN
        UPDATE public.platform_qc_usage
           SET cost_state = 'known', cost_usd = v_cost, cost_resolved_at = now(),
               tokens_in = p_tokens_in, tokens_out = p_tokens_out,
               provider_reported_cost_usd = p_provider_reported_cost_usd::numeric(14,6)
         WHERE id = p_attempt_id AND cost_state = 'unknown';
        IF FOUND THEN
            RETURN QUERY SELECT 'applied'::text, v_cost;
            RETURN;
        END IF;

        -- Another resolver won after our first read. Re-read the committed payload before
        -- classifying the loser: returning `applied` after a zero-row CAS would claim that
        -- this caller's tokens were stored when they were not.
        SELECT * INTO v_row FROM public.platform_qc_usage u WHERE u.id = p_attempt_id;
        IF NOT FOUND THEN
            PERFORM public.platform_qc_arm_kill('resolve_cost_missing');
            RETURN QUERY SELECT 'missing'::text, NULL::numeric;
            RETURN;
        END IF;
    END IF;

    -- Compare the WHOLE resolution payload, not the computed cost alone: (1000,500) and
    -- (2000,250) can compute to the identical 0.000225, so a cost-only comparison would file
    -- a genuine disagreement as a legitimate replay. IS NOT DISTINCT FROM on every field,
    -- because provider_reported_cost_usd is legitimately NULL when the provider reports
    -- nothing and `=` would mis-compare it.
    IF  v_row.tokens_in                  IS NOT DISTINCT FROM p_tokens_in
    AND v_row.tokens_out                 IS NOT DISTINCT FROM p_tokens_out
    AND v_row.cost_usd                   IS NOT DISTINCT FROM v_cost
    AND v_row.provider_reported_cost_usd IS NOT DISTINCT FROM p_provider_reported_cost_usd::numeric(14,6)
    THEN
        RETURN QUERY SELECT 'already_same'::text, v_row.cost_usd;
    ELSE
        PERFORM public.platform_qc_arm_kill('resolve_cost_conflict');
        RETURN QUERY SELECT 'conflict'::text, v_row.cost_usd;
    END IF;
END;
$$;

-- 8.6 reap — the sweep. Takes NO cache parameter at all (D-METER-29c). Coupling it to Redis
--     let a caller-side cache bug early-return before the sweep ran, so stale rows were not
--     reaped AND `reaped = 0` was reported as a fact rather than as no information. A
--     separation cannot be got wrong by a later edit; an ordering comment can.
CREATE OR REPLACE FUNCTION public.platform_qc_reap()
RETURNS integer
LANGUAGE plpgsql SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_n integer;
BEGIN
    PERFORM pg_catalog.set_config('app.platform_qc_definer', 'on', true);

    UPDATE public.platform_qc_usage
       SET attempt_state = 'timeout', attempt_state_at = now()
     WHERE attempt_state = 'attempted' AND deadline_at < now();
    GET DIAGNOSTICS v_n = ROW_COUNT;

    RETURN v_n;
END;
$$;

-- 8.7 kill_sync — authority repair, driven by the cron's Redis read. Tri-valued and TOTAL:
--     NULL / '' / 'ARMED' / 'banana' / '0' are all invalid, and on a kill-latch input
--     "whichever branch the implementation happened to write last" is not a safety property.
--     '0' is deliberately invalid: passing the COUNTER value where the CACHE STATE belongs is
--     a plausible caller mistake and must be rejected, not coerced.
CREATE OR REPLACE FUNCTION public.platform_qc_kill_sync(p_cache_state text)
RETURNS TABLE (authority_repaired boolean, repair_skipped_reason text, effective_armed boolean)
LANGUAGE plpgsql SECURITY DEFINER SET search_path = '' AS $$
DECLARE
    v_newest text;
BEGIN
    IF p_cache_state IS NULL OR p_cache_state NOT IN ('armed','absent','unreadable') THEN
        -- fails closed on the verdict, never touches the authority, and says exactly what was
        -- wrong so a caller bug is not filed as an infrastructure blip
        RETURN QUERY SELECT false, 'invalid_cache_state'::text, true;
        RETURN;
    END IF;

    PERFORM pg_advisory_xact_lock(hashtext('platform_qc_kill'));

    SELECT k.event INTO v_newest
      FROM public.platform_qc_kill k ORDER BY k.id DESC LIMIT 1;
    v_newest := COALESCE(v_newest, 'armed');   -- empty table fails closed, as the reader does

    IF p_cache_state = 'unreadable' THEN
        -- a read that FAILED proves nothing about the cache, so it may never claim a repair
        RETURN QUERY SELECT false, 'cache_unreadable'::text, true;
        RETURN;
    END IF;

    IF p_cache_state = 'absent' THEN
        RETURN QUERY SELECT false, 'cache_absent'::text, (v_newest IN ('armed','clearing'));
        RETURN;
    END IF;

    -- p_cache_state = 'armed'
    IF v_newest = 'clearing' THEN
        -- an operator is mid-reset: cache=armed/authority=cleared is the SIGNATURE of the
        -- damage this repair fixes, so without the 'clearing' label the reaper would re-arm
        -- the latch during the reset. Reported on EVERY run so an abandoned 'clearing' —
        -- which blocks L2B indefinitely, safely but invisibly — surfaces within 5 minutes.
        RETURN QUERY SELECT false, 'clearing'::text, true;
        RETURN;
    END IF;

    IF v_newest = 'armed' THEN
        RETURN QUERY SELECT false, 'not_needed'::text, true;
        RETURN;
    END IF;

    -- authority says cleared while the cache says armed → the authority write was lost
    INSERT INTO public.platform_qc_kill (event, reason_code, actor)
    VALUES ('armed', 'reaper_authority_repair', 'application');
    RETURN QUERY SELECT true, NULL::text, true;
END;
$$;

-- ── Function privileges ──────────────────────────────────────────────
--
-- 🔴 THE 0016 TRAP HAS A SECOND HALF, AND IT APPLIES TO FUNCTIONS.
--    0016_app_role.sql:27  GRANT EXECUTE ON ALL FUNCTIONS IN SCHEMA public TO app_user
--    0016_app_role.sql:36  ALTER DEFAULT PRIVILEGES ... GRANT EXECUTE ON FUNCTIONS TO app_user
--    So EVERY function created here is auto-granted EXECUTE to app_user the instant it is
--    created — including platform_qc_reap() and platform_qc_kill_sync(), which app_user must
--    NEVER be able to call. REVOKE ... FROM PUBLIC does not touch a grant held by app_user.
--    Found by EXECUTING this migration, not by reading it: the first run left app_user
--    holding EXECUTE on all seven.
--    Same for sequences (0016:34): the GENERATED ALWAYS AS IDENTITY sequence on
--    platform_qc_kill is auto-granted USAGE,SELECT.
--    Both REVOKEs below are NARROW BY NAME. `REVOKE ... ON ALL SEQUENCES IN SCHEMA public`
--    would strip app_user's sequence access across the WHOLE schema — far outside this
--    package's allowlist and capable of breaking the live customer path.
REVOKE EXECUTE ON FUNCTION public.platform_qc_reap()          FROM app_user;
REVOKE EXECUTE ON FUNCTION public.platform_qc_kill_sync(text) FROM app_user;
REVOKE ALL ON SEQUENCE public.platform_qc_kill_id_seq FROM app_user;

--    Third instance in this one migration: platform_qc_kill_append_only() is a TRIGGER
--    function, not part of the seven-function API surface — but it is still a function in
--    schema public, so 0016 auto-granted EXECUTE to app_user AND it kept the default PUBLIC
--    grant. Measured on the first run: app_user held SIX EXECUTEs, not five. It must hold
--    ZERO non-owner grants; the trigger invokes it as the table owner regardless.
REVOKE EXECUTE ON FUNCTION public.platform_qc_kill_append_only() FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_kill_append_only() FROM app_user;

REVOKE EXECUTE ON FUNCTION public.platform_qc_begin_attempt(
    text,uuid,text,text,integer,integer,text,text,text,numeric,numeric,numeric) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_finish_attempt(uuid,text)                FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_resolve_cost(uuid,integer,integer,numeric) FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_arm_kill(text)                           FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_kill_is_armed()                          FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_reap()                                   FROM PUBLIC;
REVOKE EXECUTE ON FUNCTION public.platform_qc_kill_sync(text)                          FROM PUBLIC;

GRANT EXECUTE ON FUNCTION public.platform_qc_begin_attempt(
    text,uuid,text,text,integer,integer,text,text,text,numeric,numeric,numeric) TO app_user;
GRANT EXECUTE ON FUNCTION public.platform_qc_finish_attempt(uuid,text)                 TO app_user;
GRANT EXECUTE ON FUNCTION public.platform_qc_resolve_cost(uuid,integer,integer,numeric) TO app_user;
GRANT EXECUTE ON FUNCTION public.platform_qc_arm_kill(text)                            TO app_user;
GRANT EXECUTE ON FUNCTION public.platform_qc_kill_is_armed()                           TO app_user;

GRANT EXECUTE ON FUNCTION public.platform_qc_reap()                     TO platform_qc_reaper;
GRANT EXECUTE ON FUNCTION public.platform_qc_kill_sync(text)            TO platform_qc_reaper;

-- ---------------------------------------------------------------------
-- 9. RLS — defence in depth, NOT the primary control (matrix B7, B8)
--
--    app_user holds no table privilege, so the GRANTS are what protect these rows. RLS is
--    enabled and FORCED so that if a future migration ever grants SELECT, a customer-scoped
--    read still returns nothing. It is explicitly NOT protection against the owner:
--    production's neondb_owner carries rolbypassrls=TRUE.
--
--    FORCE also binds the owner, which is why each platform_qc_usage writer above activates
--    the DEFINER-only policy branch rather than assuming a bypass. app.current_tenant_id is
--    never changed, so pooled customer callers are not changed from unset to empty.
-- ---------------------------------------------------------------------
ALTER TABLE public.platform_qc_usage ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_qc_usage FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON public.platform_qc_usage;
CREATE POLICY tenant_isolation ON public.platform_qc_usage
    USING (
        tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid
        OR (
            current_user <> session_user
            AND current_setting('app.platform_qc_definer', TRUE) = 'on'
        )
    )
    WITH CHECK (
        tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid
        OR (
            current_user <> session_user
            AND current_setting('app.platform_qc_definer', TRUE) = 'on'
        )
    );

ALTER TABLE public.platform_qc_kill ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.platform_qc_kill FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS platform_qc_kill_owner_path ON public.platform_qc_kill;
-- The latch is GLOBAL platform state — there is no tenant column to scope by, so there is no
-- house predicate to apply. An earlier draft used USING (false) WITH CHECK (false) on the
-- reasoning that "the DEFINER functions are unaffected". MEASURED: that is false. FORCE ROW
-- LEVEL SECURITY deliberately binds the table OWNER, and a DEFINER function runs as the
-- owner, so USING(false) would have blocked arm_kill, kill_is_armed and kill_sync — the
-- entire latch — on any cluster whose owner lacks BYPASSRLS. It only appeared to work here
-- because the harness owner mirrors production's neondb_owner and carries rolbypassrls=TRUE.
--
-- The GRANT surface (app_user holds nothing) plus the append-only trigger remain the primary
-- controls. This policy adds a narrow owner path: a DEFINER call is identifiable because
-- current_user is the table owner while session_user is the caller. A direct owner session
-- must explicitly select the platform tenant in its transaction, which is also how the
-- operator performs the ratified reset. Merely setting that GUC as app_user cannot pass the
-- owner check, so a future accidental table grant still does not expose the global latch.
CREATE POLICY platform_qc_kill_owner_path ON public.platform_qc_kill
    USING (
        current_user = pg_catalog.pg_get_userbyid(
            (SELECT c.relowner
               FROM pg_catalog.pg_class c
              WHERE c.oid = 'public.platform_qc_kill'::regclass)
        )
        AND (
            current_user <> session_user
            OR current_setting('app.current_tenant_id', TRUE)::uuid =
               '670711f2-ecc9-5577-9bdf-cc77611d1b4b'::uuid
        )
    )
    WITH CHECK (
        current_user = pg_catalog.pg_get_userbyid(
            (SELECT c.relowner
               FROM pg_catalog.pg_class c
              WHERE c.oid = 'public.platform_qc_kill'::regclass)
        )
        AND (
            current_user <> session_user
            OR current_setting('app.current_tenant_id', TRUE)::uuid =
               '670711f2-ecc9-5577-9bdf-cc77611d1b4b'::uuid
        )
    );

-- ---------------------------------------------------------------------
-- 10. v_platform_qc_cost — owner-only, granted to NO ONE (D-METER-17)
--
--     The view aggregates across ALL QC rows with no tenant predicate. Granting it to
--     app_user would hand the application a global, RLS-free financial aggregate — precisely
--     what FORCE RLS on the base table exists to prevent, reintroduced one level up. 0029
--     already established that global financial views are run as the DB owner.
--
--     attempts_uncosted is deliberately a FIRST-CLASS output, not a footnote: it is the size
--     of the gap between what was spent and what is known.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW public.v_platform_qc_cost AS
SELECT date_trunc('month', u.attempted_at)::date AS month,
       u.provider,
       u.model_upstream,
       u.pricing_version,
       u.cost_state,
       count(*)                                                        AS attempts,
       sum(u.cost_usd) FILTER (WHERE u.cost_state = 'known')           AS cost_usd_known,
       count(*)        FILTER (WHERE u.cost_state <> 'known')          AS attempts_uncosted
  FROM public.platform_qc_usage u
 GROUP BY 1,2,3,4,5;

REVOKE ALL ON public.v_platform_qc_cost FROM PUBLIC;
REVOKE ALL ON public.v_platform_qc_cost FROM app_user;

COMMENT ON VIEW public.v_platform_qc_cost IS
    'Owner-only QC opex aggregate (0074). Deliberately NOT granted to app_user: it is a '
    'global RLS-free financial aggregate. attempts_uncosted is the spend/knowledge gap.';

COMMIT;

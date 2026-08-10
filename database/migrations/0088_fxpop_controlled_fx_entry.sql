-- =====================================================================
-- 0088_fxpop_controlled_fx_entry.sql
--
-- The `fxpop` contract, implemented from the NORMATIVE source:
-- `PLAN-046 §A/S10.G3.3-c`, recorded verbatim under owner `fxpop=OK`
-- (`BALLOT-045-R13-FINAL`). `MATRIX-046 T72` is the ACCEPTANCE for this
-- contract, not its specification — an earlier draft of this migration was
-- built from the T72 row alone and diverged from the normative signatures,
-- the rename, the privilege split and the correction protocol.
--
-- Clause-by-clause mapping is given inline as `G3.3-c(n)`.
--
-- 🔴 NOTHING IS INSERTED HERE. Mechanism only: no rate, no KMK period, no
--    correction. Population happens through `python/ops/fx_rates_entry.py`
--    (`G3.3-c(9)`), under its own authorization.
--
-- 🔴 SCOPE — GATE 1 of four. This ships the FX half only: roles, the three
--    functions, the correction/audit mechanism, the currency-generic rename,
--    and the `credit_lots` FX-reference invariants those depend on. It does
--    NOT ship the definer lot-writer, the posting engine, the Dodo ledger
--    acquisition or the valuation computation of `G3.3-b` — Gates 2-4 — and
--    no object here depends on a Gate 2-4 file.
--
-- 🔴 KNOWN UNRESOLVED DEPENDENCY, DELIBERATELY NOT SIMULATED.
--    `G3.3-c(3)` proves deadlock freedom against an engine that takes its
--    `FOR SHARE` birth locks FIRST, ordered ascending by
--    `(currency_pair, rate_date)`, and never acquires an `fx_rates` lock after
--    it begins writing. THAT ENGINE DOES NOT EXIST YET — it is the Gate 2/3
--    writer. The corrector half of the protocol is implemented here in full
--    (three statements, `FOR UPDATE`, fresh-snapshot finality test), but the
--    ordered-`FOR SHARE` half has no counterparty, so the interaction cannot
--    be executed. It is recorded as an OPEN dependency and the acceptance for
--    it is reported PARTIAL. A hand-rolled `FOR UPDATE` holder standing in for
--    the engine would prove only that this file's own code path runs; it would
--    not exercise the birth path, and reporting that as PASS would be exactly
--    the "test that cannot fail" this workstream already paid for once.
--
-- 🔴 DEPLOY BLOCKER — THIS FILE MAY NOT SHIP ON ITS OWN.
--    Section 5 leaves `credit_lots` carrying BOTH `0086`'s `rate_date` and the
--    normative `fx_rate_date`. They are pinned equal by CHECK
--    (`IS NOT DISTINCT FROM`, so NULL-vs-value is a violation too), which stops
--    them DIVERGING but does not make them ONE column. Two columns for one
--    business value is a defect waiting for a writer to pick the wrong one, and
--    the writer is Gate 2. `0088` is therefore NOT committable and NOT
--    deployable until that unification lands in the same release.
--
-- FORWARD-ONLY. `0031`, `0086`, `0087` are applied in production and are NOT
-- edited; `migrate.js` tracks by filename, so an edit silently skips every
-- environment that already ran them.
-- =====================================================================

BEGIN;

-- ---------------------------------------------------------------------
-- 0. PRECONDITIONS.
--
--    G3.3-c(1): "Migration preflight asserts `server_version_num >= 160000`
--    (that membership syntax is PG16+); failure STOPS the migration, never
--    falls back silently."  `GRANT ... WITH INHERIT FALSE, SET TRUE` parses
--    only on PG16+; on PG15 it is a syntax error at a point where roles and
--    tables already exist, so the check must come FIRST.
-- ---------------------------------------------------------------------
DO $guard$
DECLARE
    v_ver INT := current_setting('server_version_num')::INT;
BEGIN
    IF v_ver < 160000 THEN
        RAISE EXCEPTION
            'ABORT 0088: PostgreSQL >= 16 is required (server_version_num=%). The role gate '
            'depends on GRANT ... WITH INHERIT FALSE, SET TRUE, which does not exist before 16; '
            'without it plain membership inherits and the writer gate is fictional.', v_ver;
    END IF;

    IF to_regclass('public.fx_rates') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0088: fx_rates is absent; apply 0031 first';
    END IF;
    IF to_regclass('public.credit_lots') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0088: credit_lots is absent; apply 0031 first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
        RAISE EXCEPTION 'ABORT 0088: role app_user is absent; apply 0016 first';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_proc WHERE proname = 'l2c_reject_mutation'
                     AND pronamespace = 'public'::regnamespace) THEN
        RAISE EXCEPTION 'ABORT 0088: public.l2c_reject_mutation() is absent; apply 0085 first';
    END IF;

    -- G3.3-c(4) rests on credit_lots being ENABLE + FORCE RLS with a
    -- permissive tenant_isolation policy carrying NO `TO` clause. If that is
    -- not the shape on disk, the cross-tenant reference read is unsound and
    -- this migration must not pretend otherwise.
    IF NOT EXISTS (SELECT 1 FROM pg_class
                    WHERE oid = 'public.credit_lots'::regclass
                      AND relrowsecurity AND relforcerowsecurity) THEN
        RAISE EXCEPTION
            'ABORT 0088: credit_lots must have ENABLE + FORCE ROW LEVEL SECURITY (G3.3-c(4))';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_policy
                    WHERE polrelid = 'public.credit_lots'::regclass
                      AND polname = 'tenant_isolation'
                      AND polpermissive
                      AND polroles = '{0}') THEN
        RAISE EXCEPTION
            'ABORT 0088: credit_lots.tenant_isolation must be PERMISSIVE with no TO clause. '
            'G3.3-c(4) relies on permissive policies combining with OR; a RESTRICTIVE policy '
            'would AND and silently deny the reference read.';
    END IF;

    IF EXISTS (SELECT 1 FROM public.fx_rates) THEN
        RAISE EXCEPTION
            'ABORT 0088: fx_rates already holds row(s). G3.3-c(7) permits the rename precisely '
            'because the table is empty (0031:509-523); a populated table needs a provenance '
            'backfill nobody has authorised.';
    END IF;
    IF EXISTS (SELECT 1 FROM public.credit_lots) THEN
        RAISE EXCEPTION
            'ABORT 0088: credit_lots holds row(s); the NOT NULL valuation columns below assume '
            'the 0086 G0 precondition (0 rows, 0 writers) still holds.';
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. ROLES.  G3.3-c(1) — three roles + acknowledged break-glass.
--
--    fx_rates_owner   NOLOGIN NOBYPASSRLS. Owns fx_rates, fx_rates_corrections,
--                     fx_kmk_periods and all three functions, so the operator
--                     connection is NOT the owner and owner-inherent rights
--                     cannot leak a write path.
--    fx_rates_writer  NOLOGIN. Holds NO TABLE PRIVILEGE AT ALL — only EXECUTE
--                     on the two API functions. This is exact: a SELECT grant
--                     here would hand every operator a standing read of the
--                     valuation surface through a role they can always assume.
--    g3_posting_engine  SELECT only.
--
--    Database-admin/superuser access is an acknowledged, documented
--    break-glass and is explicitly OUTSIDE these guarantees.
-- ---------------------------------------------------------------------
DO $roles$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fx_rates_owner') THEN
        CREATE ROLE fx_rates_owner NOLOGIN NOBYPASSRLS;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'fx_rates_writer') THEN
        CREATE ROLE fx_rates_writer NOLOGIN NOBYPASSRLS;
    END IF;
    -- Created here as a BARE, privilege-less identity only so G3.3-c(1)'s
    -- "g3_posting_engine: SELECT only" is assertable. Gate 2/3 attaches its
    -- actual duties; the CREATE is idempotent so it can.
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'g3_posting_engine') THEN
        CREATE ROLE g3_posting_engine NOLOGIN NOBYPASSRLS;
    END IF;
END
$roles$;

-- An ASSERTION, not an ALTER: changing SUPERUSER or BYPASSRLS requires
-- SUPERUSER, which the production migration role (neondb_owner) is not — an
-- ALTER here would fail in production while passing under a superuser test
-- run, which is the defect class this whole gate exists to eliminate.
-- 🔴 THIS RUNS AGAINST ROLES THAT MAY ALREADY EXIST. The CREATE block above is
--    `IF NOT EXISTS`, so a role left over from an earlier life — created by
--    hand, by a rolled-back attempt, or by another environment — is adopted
--    silently with whatever attributes it already carries. Until this preflight
--    existed, only a test on a FRESH cluster caught that, which is no guard at
--    all for the case that actually matters: applying into a database that has
--    history. NOLOGIN is asserted here for the same reason as NOSUPERUSER and
--    NOBYPASSRLS — these three roles must only ever be ASSUMED.
DO $attrs$
DECLARE r RECORD;
BEGIN
    FOR r IN SELECT rolname, rolsuper, rolbypassrls, rolcanlogin,
                    rolcreaterole, rolcreatedb, rolreplication
               FROM pg_roles
              WHERE rolname IN ('fx_rates_owner','fx_rates_writer','g3_posting_engine')
    LOOP
        IF r.rolsuper THEN
            RAISE EXCEPTION 'ABORT 0088: role % is SUPERUSER; the contract is unenforceable against it', r.rolname;
        END IF;
        IF r.rolbypassrls THEN
            RAISE EXCEPTION 'ABORT 0088: role % has BYPASSRLS; G3.3-c(4) would be vacuous', r.rolname;
        END IF;
        IF r.rolcanlogin THEN
            RAISE EXCEPTION
                'ABORT 0088: role % is LOGIN. G3.3-c(1) ships all three normative roles NOLOGIN so '
                'they can only be ASSUMED — a login-capable owner or writer is a standing, directly '
                'connectable path to the valuation surface. This role pre-existed this migration; '
                'fix it deliberately rather than letting 0088 adopt it.', r.rolname;
        END IF;
        IF r.rolcreaterole OR r.rolcreatedb OR r.rolreplication THEN
            RAISE EXCEPTION
                'ABORT 0088: role % carries CREATEROLE/CREATEDB/REPLICATION; it must hold none of them',
                r.rolname;
        END IF;
    END LOOP;
END
$attrs$;

-- ---------------------------------------------------------------------
-- 2. G3.3-c(7) — CURRENCY-GENERIC SCHEMA, UNCONDITIONALLY.
--
--    `idr_per_usd` -> `idr_per_major_unit` (IDR per ONE major unit), COMMENT
--    replaced. Independent of the owner's `d`: `d=USD` restricts what runtime
--    ACCEPTS, never the physical schema.
--
--    This is a RENAME, not an added column. The table holds zero rows so no
--    backfill is needed, and a second column carrying the same quantity would
--    be exactly the silent-divergence hazard the contract removes.
--
--    READER SWEEP (required by G3.3-c(7), run at implementation): six matches
--    for `idr_per_usd` in the tree, ALL of them comments —
--      0031:445  comment on payments.fx_rate          (different table)
--      0031:512  section comment
--      0031:518  the column definition itself         (renamed here)
--      0031:523  COMMENT ON TABLE fx_rates            (replaced below)
--      0031:574  comment on provider_invoices.accrued_rate  (different table)
--      0031:575  comment on provider_invoices.settled_rate  (different table)
--    ZERO executable readers. The live COGS reader remains the KURS env
--    constant (0031:523), so no production reader breaks. `0031` itself is
--    applied and forward-only: its inline comment text is historical and is
--    not rewritten — the authoritative description is the COMMENT below.
-- ---------------------------------------------------------------------
ALTER TABLE fx_rates RENAME COLUMN idr_per_usd TO idr_per_major_unit;

ALTER TABLE fx_rates
    ADD COLUMN IF NOT EXISTS source_ref TEXT,
    ADD COLUMN IF NOT EXISTS entered_by TEXT;

ALTER TABLE fx_rates
    ALTER COLUMN source_ref SET NOT NULL,
    ALTER COLUMN entered_by SET NOT NULL,
    ALTER COLUMN source     SET NOT NULL;

COMMENT ON TABLE fx_rates IS
  'Published FX rates, one row per (currency_pair, rate_date). Currency-generic: idr_per_major_unit is IDR per ONE major unit of the base currency. Written ONLY through fx_rates_enter()/fx_rates_correct(); the KURS env constant remains the live COGS default.';
COMMENT ON COLUMN fx_rates.idr_per_major_unit IS
  'IDR per ONE major unit of the base currency (6 dp). Renamed from idr_per_usd by 0088 per PLAN-046 G3.3-c(7): the physical schema is currency-generic even though runtime currently accepts USD only. A series quoted per 100 units must be divided by its Nilai BEFORE entry — python/ops/fx_rates_entry.py does that and shows its arithmetic.';
COMMENT ON COLUMN fx_rates.rate_date IS
  'The valuation/grant date (G3.3-b). NOT the publication date: under fallback alpha a Saturday grant resolves to Friday''s publication, which is named by source_ref. G3.3-c(5) — the two never merge.';
COMMENT ON COLUMN fx_rates.source_ref IS
  'Names the UNDERLYING PUBLICATION selected by e3=alpha, and may carry a different date than rate_date. For source=kmk_tax it must name the KMK whose validity interval CONTAINS rate_date.';
COMMENT ON COLUMN fx_rates.entered_by IS
  'session_user of the operator. NOT a parameter and NOT current_user, which under SET ROLE is uniformly fx_rates_writer and useless for audit (G3.3-c(2), lesson -033).';

-- G3.3-c(6) — invariants as CHECKs, not prose.
ALTER TABLE fx_rates
    ADD CONSTRAINT fx_rates_source_domain
        CHECK (source IN ('jisdor','bi_transaction_mid','kmk_tax','manual')),
    ADD CONSTRAINT fx_rates_currency_pair_format
        CHECK (currency_pair ~ '^[A-Z]{3}/IDR$'),
    ADD CONSTRAINT fx_rates_major_unit_positive
        CHECK (idr_per_major_unit > 0),
    ADD CONSTRAINT fx_rates_source_ref_nonblank
        CHECK (length(btrim(source_ref)) > 0),
    ADD CONSTRAINT fx_rates_entered_by_nonblank
        CHECK (length(btrim(entered_by)) > 0);

-- ---------------------------------------------------------------------
-- 3. fx_kmk_periods — KMK validity intervals.
--
--    Supports the `kmk_tax` half of G3.3-c(6)'s source domain and the
--    "KMK effective-period lookup" named in G3.3-c's own Acceptance list.
--    Citing a KMK whose interval does NOT contain rate_date is the specific
--    error this table makes impossible.
-- ---------------------------------------------------------------------
CREATE TABLE fx_kmk_periods (
    kmk_ref    TEXT PRIMARY KEY,
    valid_from DATE NOT NULL,
    valid_to   DATE NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    CONSTRAINT fx_kmk_periods_interval_ordered CHECK (valid_to >= valid_from),
    CONSTRAINT fx_kmk_periods_ref_nonblank     CHECK (length(btrim(kmk_ref)) > 0)
);

-- ---------------------------------------------------------------------
-- 4. fx_rates_corrections.  G3.3-c(3).
--
--    `correction_xid XID8 NOT NULL UNIQUE` — the constraint IS the
--    concurrency/backstop enforcement. XID8, not BIGINT: pg_current_xact_id()
--    returns xid8 and the column must carry it without a lossy cast.
--    pg_current_xact_id() returns the TOP-LEVEL xid, stable across savepoints,
--    which is why a savepoint-rolled-back attempt leaves no guard row and may
--    legitimately be retried.
--
--    A correction may change the rate, the source AND the source_ref, so all
--    three are recorded old-and-new.
-- ---------------------------------------------------------------------
CREATE TABLE fx_rates_corrections (
    id             BIGINT        GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    currency_pair  TEXT          NOT NULL,
    rate_date      DATE          NOT NULL,
    old_rate       NUMERIC(18,6) NOT NULL,
    new_rate       NUMERIC(18,6) NOT NULL,
    old_source     TEXT          NOT NULL,
    new_source     TEXT          NOT NULL,
    old_source_ref TEXT          NOT NULL,
    new_source_ref TEXT          NOT NULL,
    reason         TEXT          NOT NULL,
    corrected_by   TEXT          NOT NULL,
    corrected_at   TIMESTAMPTZ   NOT NULL DEFAULT clock_timestamp(),
    correction_xid XID8          NOT NULL,
    CONSTRAINT fx_rates_corrections_xid_unique UNIQUE (correction_xid),
    CONSTRAINT fx_rates_corrections_reason_nonblank CHECK (length(btrim(reason)) > 0),
    CONSTRAINT fx_rates_corrections_by_nonblank     CHECK (length(btrim(corrected_by)) > 0),
    CONSTRAINT fx_rates_corrections_new_positive    CHECK (new_rate > 0),
    -- A "correction" that changes nothing is not a correction.
    CONSTRAINT fx_rates_corrections_changes_something
        CHECK (new_rate <> old_rate OR new_source <> old_source
               OR new_source_ref <> old_source_ref),
    CONSTRAINT fx_rates_corrections_rate_fk
        FOREIGN KEY (currency_pair, rate_date)
        REFERENCES fx_rates (currency_pair, rate_date) ON DELETE RESTRICT
);
COMMENT ON TABLE fx_rates_corrections IS
  'Append-only record of every accepted fx_rates_correct() call. UNIQUE(correction_xid) is the concurrency enforcement, not a formality (G3.3-c(3)); the RESTRICT FK keeps a corrected rate undeletable.';

-- ---------------------------------------------------------------------
-- 5. credit_lots — the FX reference and its valuation invariants.
--    G3.3-c(6), with the field names of G3.3-b.
--
--    `valued`                  => fx_currency_pair = src_currency || '/IDR'
--                                 AND both FX columns NOT NULL
--    `verified_zero`/`unknown` => both NULL (fabricating a reference must FAIL)
--    `src_currency ~ '^[A-Z]{3}$'` when present
--
--    🔴 NAMING DIVERGENCE, RECORDED NOT PAPERED OVER. `0086` shipped
--    `credit_lots.rate_date`; `G3.3-b`/`G3.3-c` name the FX reference column
--    `fx_rate_date`, and `G3.3-c(3)`'s finality predicate is written against
--    `fx_rate_date` specifically. `0086`'s column has LIVE readers at this
--    commit — `backend/g3_lots.mjs` (4 sites), `0086`'s own `g3_write_lot`,
--    and `tests/node/l2c_realdb_t79_cr29.test.mjs:184` — every one of them on
--    the Gate 2/3 writer surface this gate must not touch. So `fx_rate_date`
--    is ADDED under its normative name and the two are pinned equal by CHECK
--    while both exist. Unifying them belongs to Gate 2, and is an OPEN item.
-- ---------------------------------------------------------------------
ALTER TABLE credit_lots
    ADD COLUMN IF NOT EXISTS valuation_status TEXT NOT NULL DEFAULT 'unknown',
    ADD COLUMN IF NOT EXISTS src_currency     TEXT,
    ADD COLUMN IF NOT EXISTS fx_currency_pair TEXT,
    ADD COLUMN IF NOT EXISTS fx_rate_date     DATE;

ALTER TABLE credit_lots
    ADD CONSTRAINT credit_lots_fx_rate_fk
        FOREIGN KEY (fx_currency_pair, fx_rate_date)
        REFERENCES fx_rates (currency_pair, rate_date) ON DELETE RESTRICT,

    ADD CONSTRAINT credit_lots_valuation_status_domain
        CHECK (valuation_status IN ('valued','verified_zero','unknown')),

    ADD CONSTRAINT credit_lots_src_currency_format
        CHECK (src_currency IS NULL OR src_currency ~ '^[A-Z]{3}$'),

    -- valued => both FX columns present AND the pair derives from the currency
    ADD CONSTRAINT credit_lots_valued_has_fx_reference
        CHECK (valuation_status <> 'valued'
               OR (fx_currency_pair IS NOT NULL AND fx_rate_date IS NOT NULL
                   AND src_currency IS NOT NULL
                   AND fx_currency_pair = src_currency || '/IDR')),

    -- verified_zero / unknown => NO FX reference. This is the clause that makes
    -- fabricating a rate reference on a free or unpriced lot fail closed.
    ADD CONSTRAINT credit_lots_unvalued_has_no_fx_reference
        CHECK (valuation_status = 'valued'
               OR (fx_currency_pair IS NULL AND fx_rate_date IS NULL)),

    -- is_priced (0086) and valuation_status must never disagree.
    ADD CONSTRAINT credit_lots_priced_matches_valuation_status
        CHECK (is_priced = (valuation_status = 'valued')),

    -- While 0086's rate_date and the normative fx_rate_date coexist they denote
    -- the SAME business value and may not diverge — including by NULL.
    --
    -- 🔴 An earlier draft wrote `fx_rate_date IS NULL OR rate_date IS NULL OR
    --    fx_rate_date = rate_date`, which permits exactly the divergence it
    --    claimed to forbid: one column set and the other NULL satisfies it.
    --    IS NOT DISTINCT FROM is the correct predicate — both NULL, or both
    --    equal, and nothing else.
    ADD CONSTRAINT credit_lots_fx_rate_date_agrees_with_0086_rate_date
        CHECK (fx_rate_date IS NOT DISTINCT FROM rate_date);

COMMENT ON COLUMN credit_lots.valuation_status IS
  'valued | verified_zero | unknown (G3.3-b). Free/signup/daily/admin lots are verified_zero with dpp_total_idr=0 and NO FX reference; subscription lots stay unknown under the excluded Delta-4. Default unknown = fail closed.';
COMMENT ON COLUMN credit_lots.fx_rate_date IS
  'FX reference date, normative name per PLAN-046 G3.3-b. Pinned equal to 0086''s rate_date while both exist; unifying them is an OPEN Gate-2 item.';

-- ---------------------------------------------------------------------
-- 6. THE BACKSTOP TRIGGER FUNCTION.  G3.3-c(2).
--
--    Third of the three functions. Itself SECURITY DEFINER owned by
--    fx_rates_owner with a pinned search_path — "without that it runs as the
--    invoker, and on the break-glass path its cross-tenant reference check
--    would be tenant-blind and could pass vacuously."
--
--    Enforces column immutability: currency_pair, rate_date, created_at,
--    entered_by may NEVER change; only idr_per_major_unit, source and
--    source_ref may, and only through fx_rates_correct().
--
--    Its reference check is DEFENCE IN DEPTH and is explicitly NOT the
--    finality claim (G3.3-c(3)); the finality proof is the fresh-snapshot
--    EXISTS test inside fx_rates_correct().
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fx_rates_reject_mutation() RETURNS TRIGGER
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
BEGIN
    IF TG_OP = 'TRUNCATE' THEN
        RAISE EXCEPTION 'fx_rates: TRUNCATE is not permitted on %.%',
            TG_TABLE_SCHEMA, TG_TABLE_NAME USING ERRCODE = 'restrict_violation';
    END IF;

    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'fx_rates: DELETE is not permitted (% %)',
            OLD.currency_pair, OLD.rate_date USING ERRCODE = 'restrict_violation';
    END IF;

    IF NEW.currency_pair IS DISTINCT FROM OLD.currency_pair
       OR NEW.rate_date  IS DISTINCT FROM OLD.rate_date
       OR NEW.created_at IS DISTINCT FROM OLD.created_at
       OR NEW.entered_by IS DISTINCT FROM OLD.entered_by THEN
        RAISE EXCEPTION
            'fx_rates: currency_pair, rate_date, created_at and entered_by are immutable '
            '(attempted on % %)', OLD.currency_pair, OLD.rate_date
            USING ERRCODE = 'restrict_violation';
    END IF;

    -- Defence in depth ONLY. Runs as fx_rates_owner, so the credit_lots
    -- reference policy applies and the read is cross-tenant.
    IF NEW.idr_per_major_unit IS DISTINCT FROM OLD.idr_per_major_unit
       OR NEW.source IS DISTINCT FROM OLD.source
       OR NEW.source_ref IS DISTINCT FROM OLD.source_ref THEN
        IF EXISTS (SELECT 1 FROM public.credit_lots l
                    WHERE l.fx_currency_pair = OLD.currency_pair
                      AND l.fx_rate_date = OLD.rate_date) THEN
            RAISE EXCEPTION
                'fx_rates: % % is referenced by a committed lot and cannot be amended',
                OLD.currency_pair, OLD.rate_date USING ERRCODE = 'FX002';
        END IF;
    END IF;

    RETURN NEW;
END
$fn$;

CREATE TRIGGER fx_rates_no_mutation
    BEFORE UPDATE OR DELETE ON fx_rates
    FOR EACH ROW EXECUTE FUNCTION public.fx_rates_reject_mutation();

CREATE TRIGGER fx_rates_no_truncate
    BEFORE TRUNCATE ON fx_rates
    FOR EACH STATEMENT EXECUTE FUNCTION public.fx_rates_reject_mutation();

CREATE TRIGGER fx_rates_corrections_no_mutation
    BEFORE UPDATE OR DELETE ON fx_rates_corrections
    FOR EACH ROW EXECUTE FUNCTION public.l2c_reject_mutation();
CREATE TRIGGER fx_rates_corrections_no_truncate
    BEFORE TRUNCATE ON fx_rates_corrections
    FOR EACH STATEMENT EXECUTE FUNCTION public.l2c_reject_mutation();

-- ---------------------------------------------------------------------
-- 7. fx_rates_enter — NORMATIVE SIGNATURE, G3.3-c(2).
--
--      fx_rates_enter(p_pair, p_rate_date, p_rate, p_source, p_source_ref)
--
--    FIVE parameters, exactly. `created_at` and `entered_by` are NOT
--    parameters: filled in-body from clock_timestamp() and session_user.
--    Idempotent ON CONFLICT (currency_pair, rate_date) DO NOTHING plus
--    read-back-compare; mismatch => RAISE.
--
--    NORMALISATION IS THE CALLER'S ARITHMETIC, NOT A PARAMETER SET. p_rate is
--    already IDR per ONE major unit. A BI transaction-mid series quoted per
--    100 units must be divided by its Nilai BEFORE the call —
--    python/ops/fx_rates_entry.py performs and prints that arithmetic. An
--    earlier draft added raw jual/beli/nilai columns and a derivation inside
--    this function; that is not the normative signature and is withdrawn.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fx_rates_enter(
    p_pair       TEXT,
    p_rate_date  DATE,
    p_rate       NUMERIC,
    p_source     TEXT,
    p_source_ref TEXT
) RETURNS TABLE (outcome TEXT, effective_rate NUMERIC)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    v_stored NUMERIC(18,6);
BEGIN
    IF p_pair IS NULL OR p_rate_date IS NULL OR p_rate IS NULL
       OR p_source IS NULL OR p_source_ref IS NULL THEN
        RAISE EXCEPTION 'fx_rates_enter: pair, rate_date, rate, source and source_ref are all required'
            USING ERRCODE = 'FX004';
    END IF;

    -- KMK citations must name the interval that CONTAINS rate_date.
    IF p_source = 'kmk_tax' THEN
        IF NOT EXISTS (
            SELECT 1 FROM public.fx_kmk_periods k
             WHERE k.kmk_ref = p_source_ref
               AND p_rate_date BETWEEN k.valid_from AND k.valid_to
        ) THEN
            RAISE EXCEPTION
                'fx_rates_enter: KMK % does not cover rate_date % — a kmk_tax rate must cite the '
                'KMK whose validity interval contains the day it values', p_source_ref, p_rate_date
                USING ERRCODE = 'FX005';
        END IF;
    END IF;

    INSERT INTO public.fx_rates
        (currency_pair, rate_date, idr_per_major_unit, source, source_ref, entered_by, created_at)
    VALUES
        (p_pair, p_rate_date, p_rate, p_source, p_source_ref, session_user, clock_timestamp())
    ON CONFLICT (currency_pair, rate_date) DO NOTHING;

    SELECT f.idr_per_major_unit INTO v_stored
      FROM public.fx_rates f
     WHERE f.currency_pair = p_pair AND f.rate_date = p_rate_date;

    IF v_stored IS DISTINCT FROM p_rate THEN
        RAISE EXCEPTION
            'fx_rates_enter: % % already holds %, refusing to replace it with %. Correcting a '
            'published rate is a separate authorised act, never an overwrite.',
            p_pair, p_rate_date, v_stored, p_rate
            USING ERRCODE = 'FX006';
    END IF;

    RETURN QUERY SELECT 'entered'::TEXT, v_stored;
END
$fn$;

-- ---------------------------------------------------------------------
-- 8. fx_rates_correct — NORMATIVE SIGNATURE AND PROTOCOL, G3.3-c(3).
--
--      fx_rates_correct(p_pair, p_rate_date, p_new_rate,
--                       p_new_source, p_new_source_ref, p_reason)
--
--    SIX parameters: a correction may amend the rate, the source AND the
--    source_ref — those are exactly the three columns the immutability
--    trigger permits to move.
--
--    "Used" = REFERENCED BY A COMMITTED LOT, not credits consumed.
--
--    PROTOCOL, and the order is the whole point:
--      entry) require READ COMMITTED                       -> FX003
--      entry) v_xid := pg_current_xact_id(); guard row?     -> FX001
--             — BEFORE TAKING ANY ROW LOCK
--      (i)    SELECT ... FOR UPDATE on the rate row
--             — blocks behind any in-flight birth's FOR SHARE
--      (ii)   a NEW STATEMENT, on a fresh snapshot, EXISTS over credit_lots
--             — THIS IS THE FINALITY PROOF: any lot that committed while we
--               waited on (i) is visible here                -> FX002
--      (iii)  only then UPDATE + the append-only audit row carrying v_xid
--
--    Under READ COMMITTED each statement takes a new snapshot, which is why
--    (ii) must be its own statement and must follow (i). Collapsing (i) and
--    (ii) into one statement would test a snapshot taken BEFORE the wait and
--    could miss a lot that committed during it.
--
--    NO CALLER-WRITABLE STATE. The GUC sentinel (`fx.correction_call`) is
--    WITHDRAWN IN FULL: custom two-part GUCs are caller-writable, so no
--    security or deadlock claim may rest on one.
--
--    DEADLOCK FREEDOM — one half present, one half OPEN. A corrector holds at
--    most ONE fx_rates row lock per transaction, because the second call fails
--    at the XID guard before locking. That half is implemented and testable
--    here. The other half — the engine takes all its FOR SHARE locks first,
--    ordered ascending by (currency_pair, rate_date), and never acquires an
--    fx_rates lock after it begins writing — belongs to the Gate 2/3 birth
--    path, which does not exist. Single-holder vs ordered-acquirer cannot
--    cycle, but that conclusion is not established until the acquirer exists.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.fx_rates_correct(
    p_pair           TEXT,
    p_rate_date      DATE,
    p_new_rate       NUMERIC,
    p_new_source     TEXT,
    p_new_source_ref TEXT,
    p_reason         TEXT
) RETURNS TABLE (old_rate NUMERIC, new_rate NUMERIC)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $fn$
DECLARE
    v_old_rate       NUMERIC(18,6);
    v_old_source     TEXT;
    v_old_source_ref TEXT;
    v_used           BOOLEAN;
    v_xid            XID8;
BEGIN
    IF current_setting('transaction_isolation') <> 'read committed' THEN
        RAISE EXCEPTION
            'fx_rates_correct: requires READ COMMITTED isolation (session is %) — the finality '
            'proof depends on per-statement snapshots',
            current_setting('transaction_isolation')
            USING ERRCODE = 'FX003';
    END IF;

    IF p_reason IS NULL OR length(btrim(p_reason)) = 0 THEN
        RAISE EXCEPTION 'fx_rates_correct: a reason is required' USING ERRCODE = 'FX004';
    END IF;
    IF p_new_rate IS NULL OR p_new_rate <= 0 THEN
        RAISE EXCEPTION 'fx_rates_correct: the new rate must be > 0 (got %)', p_new_rate
            USING ERRCODE = 'FX004';
    END IF;
    IF p_new_source IS NULL OR p_new_source_ref IS NULL
       OR length(btrim(p_new_source_ref)) = 0 THEN
        RAISE EXCEPTION 'fx_rates_correct: new source and source_ref are required'
            USING ERRCODE = 'FX004';
    END IF;

    -- BEFORE ANY ROW LOCK.
    v_xid := pg_current_xact_id();
    IF EXISTS (SELECT 1 FROM public.fx_rates_corrections c WHERE c.correction_xid = v_xid) THEN
        RAISE EXCEPTION
            'fx_rates_correct: this transaction has already recorded a correction. One correction '
            'per transaction, so each amendment is independently reviewable — and so a corrector '
            'can never hold two fx_rates row locks at once.'
            USING ERRCODE = 'FX001';
    END IF;

    -- (i) lock the rate row; blocks behind any in-flight birth's FOR SHARE.
    SELECT f.idr_per_major_unit, f.source, f.source_ref
      INTO v_old_rate, v_old_source, v_old_source_ref
      FROM public.fx_rates f
     WHERE f.currency_pair = p_pair AND f.rate_date = p_rate_date
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'fx_rates_correct: no rate for % %', p_pair, p_rate_date
            USING ERRCODE = 'FX004';
    END IF;

    IF p_new_rate = v_old_rate AND p_new_source = v_old_source
       AND p_new_source_ref = v_old_source_ref THEN
        RAISE EXCEPTION 'fx_rates_correct: % % already holds those values; nothing to correct',
            p_pair, p_rate_date USING ERRCODE = 'FX004';
    END IF;

    -- (ii) A NEW STATEMENT on a fresh snapshot. THE FINALITY PROOF.
    --      Runs as fx_rates_owner, whose credit_lots policy is unrestricted
    --      SELECT, so it is tenant-blind by construction: an unset or wrong
    --      app.current_tenant_id cannot shrink it and let a used rate move.
    SELECT EXISTS (
        SELECT 1 FROM public.credit_lots l
         WHERE l.fx_currency_pair = p_pair AND l.fx_rate_date = p_rate_date
    ) INTO v_used;

    IF v_used THEN
        RAISE EXCEPTION
            'fx_rates_correct: % % is already referenced by a committed lot and can no longer be '
            'corrected. Amending it would silently re-price recognised revenue; a wrong rate is '
            'handled as an accounting correction, never by retro-pricing.',
            p_pair, p_rate_date
            USING ERRCODE = 'FX002';
    END IF;

    -- (iii) only now.
    UPDATE public.fx_rates
       SET idr_per_major_unit = p_new_rate,
           source             = p_new_source,
           source_ref         = p_new_source_ref
     WHERE currency_pair = p_pair AND rate_date = p_rate_date;

    INSERT INTO public.fx_rates_corrections
        (currency_pair, rate_date, old_rate, new_rate, old_source, new_source,
         old_source_ref, new_source_ref, reason, corrected_by, correction_xid)
    VALUES
        (p_pair, p_rate_date, v_old_rate, p_new_rate, v_old_source, p_new_source,
         v_old_source_ref, p_new_source_ref, p_reason, session_user, v_xid);

    RETURN QUERY SELECT v_old_rate, p_new_rate;
END
$fn$;

-- ---------------------------------------------------------------------
-- 9. THE credit_lots READ POLICY.  G3.3-c(4).
--
--    Executed as the credit_lots owner. Permissive policies combine with OR,
--    so fx_rates_owner sees all tenants while every other role stays
--    tenant-confined and app_user's policy is untouched.
--
--    Grant without policy = no access; policy without grant = no access.
--    BOTH are required, which is why both statements are here.
-- ---------------------------------------------------------------------
DROP POLICY IF EXISTS fx_rates_reference_check ON credit_lots;
CREATE POLICY fx_rates_reference_check
ON public.credit_lots FOR SELECT TO fx_rates_owner USING (true);

GRANT SELECT ON public.credit_lots TO fx_rates_owner;

-- ---------------------------------------------------------------------
-- 10. PRIVILEGES.  G3.3-c(1) and G3.3-c(2).
--
--     Runs BEFORE the ownership transfer: once fx_rates belongs to
--     fx_rates_owner, neondb_owner can no longer GRANT or REVOKE on it (its
--     membership is INHERIT FALSE and is revoked entirely in section 11).
--     Explicit ACL entries survive ALTER ... OWNER TO unchanged.
--
--     🔴 `app_user` AND `PUBLIC` ARE BOTH NAMED EXPLICITLY, on tables AND on
--     functions — the `0016` default-privilege gap (lesson -032). `0016:31/34/36`
--     grant app_user DML on future TABLES, USAGE on future SEQUENCES and
--     EXECUTE on future FUNCTIONS, writing an EXPLICIT `app_user=...` ACL
--     entry that a `REVOKE ... FROM PUBLIC` does not touch. Measured on a real
--     run before this was explicit: `app_user=X/fx_rates_owner` on BOTH API
--     functions — the web-app role could write valuation inputs directly.
-- ---------------------------------------------------------------------
REVOKE ALL ON fx_rates             FROM PUBLIC, app_user, g3_posting_engine, fx_rates_writer;
REVOKE ALL ON fx_rates_corrections FROM PUBLIC, app_user, g3_posting_engine, fx_rates_writer;
REVOKE ALL ON fx_kmk_periods       FROM PUBLIC, app_user, g3_posting_engine, fx_rates_writer;
REVOKE ALL ON SEQUENCE fx_rates_corrections_id_seq
    FROM PUBLIC, app_user, g3_posting_engine, fx_rates_writer;

-- g3_posting_engine: SELECT only.
GRANT SELECT ON fx_rates, fx_kmk_periods, fx_rates_corrections TO g3_posting_engine;
-- app_user keeps the read 0031 gave it, and gains nothing else.
GRANT SELECT ON fx_rates TO app_user;

-- (REFERENCES for the migration role is granted in section 11, AFTER the
--  ownership transfer and while the owner role is still assumable — see there
--  for why it cannot be done here.)

-- 🔴 fx_rates_writer gets EXECUTE AND NOTHING ELSE — no SELECT, on any table.
--    An earlier draft granted it SELECT "so the operator can verify what they
--    entered". That is not the contract: the operator is the privileged
--    db-backup connection and reads under its OWN identity, never through a
--    role every operator can assume.
REVOKE ALL ON FUNCTION public.fx_rates_enter(TEXT,DATE,NUMERIC,TEXT,TEXT)
    FROM PUBLIC, app_user, g3_posting_engine;
REVOKE ALL ON FUNCTION public.fx_rates_correct(TEXT,DATE,NUMERIC,TEXT,TEXT,TEXT)
    FROM PUBLIC, app_user, g3_posting_engine;
REVOKE ALL ON FUNCTION public.fx_rates_reject_mutation()
    FROM PUBLIC, app_user, g3_posting_engine, fx_rates_writer;

GRANT EXECUTE ON FUNCTION public.fx_rates_enter(TEXT,DATE,NUMERIC,TEXT,TEXT)   TO fx_rates_writer;
GRANT EXECUTE ON FUNCTION public.fx_rates_correct(TEXT,DATE,NUMERIC,TEXT,TEXT,TEXT) TO fx_rates_writer;

-- ---------------------------------------------------------------------
-- 10b. THE OPS MEMBERSHIP — G3.3-c(1), and it is PERMANENT.
--
--      "The operator is the established privileged connection (`db-backup` /
--       `BACKUP_DATABASE_URL`) with GRANT fx_rates_writer TO <migration/ops
--       role> WITH INHERIT FALSE, SET TRUE — plain membership inherits by
--       default and would make the gate fictional."
--
--      🔴 WITHOUT THIS GRANT THE OPERATIONAL PATH DOES NOT EXIST. An earlier
--      draft omitted it and let the test invent a synthetic operator instead,
--      so `python/ops/fx_rates_entry.py` "worked" in the fixture while the real
--      `db-backup` connection would have been refused at `SET ROLE`. A fixture
--      that manufactures the privilege under test proves nothing about
--      production.
--
--      WHICH ROLE. Verified, not assumed, exactly as (8) requires for the
--      migration role: production migrations are run THROUGH db-backup
--      (`railway run -s db-backup -- sh -c 'DATABASE_URL="$BACKUP_DATABASE_URL"
--      ... node database/migrate.js'`), so the ops role and the migration role
--      are the SAME connected role, and `current_user` names it at run time.
--      `fxpop.ops_role` overrides it if that ever stops being true.
--
--      PERMANENT, unlike the fx_rates_owner membership of (8)(iii) which is
--      temporary and revoked at (v). The distinction is the whole design:
--      assuming the WRITER is the daily operational act; assuming the OWNER is
--      break-glass.
-- ---------------------------------------------------------------------
DO $ops$
DECLARE
    v_ops_role TEXT := coalesce(
        nullif(current_setting('fxpop.ops_role', true), ''), current_user);
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_ops_role) THEN
        RAISE EXCEPTION
            'ABORT 0088: ops role % does not exist; fxpop.ops_role must name a real role', v_ops_role;
    END IF;

    EXECUTE format('GRANT fx_rates_writer TO %I WITH INHERIT FALSE, SET TRUE', v_ops_role);

    -- Assert the gate is a gate: SET yes, USAGE no. A plain GRANT would make
    -- the writer's EXECUTE ambient for every session that role opens.
    IF pg_has_role(v_ops_role, 'fx_rates_writer', 'USAGE') THEN
        IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = v_ops_role AND rolsuper) THEN
            -- A superuser satisfies EVERY role predicate by definition, so the
            -- SET ROLE gate provides exactly nothing when the migration runs as
            -- one. Refusing here rather than applying is deliberate: a database
            -- migrated by a superuser would report this contract as enforced
            -- while enforcing none of it — the T79 defect, one layer down.
            RAISE EXCEPTION
                'ABORT 0088: the migration is running as SUPERUSER %, which implicitly holds '
                'every role, so the fx_rates_writer SET-ROLE gate would be fictional. Run '
                'migrations as the production-equivalent NON-superuser owner (neondb_owner). '
                'NOTE: tests/node/_realdb.mjs currently provisions and migrates as `postgres` '
                'and will hit this — that harness is Gate 4''s "T79 39/39 through real roles".',
                v_ops_role;
        END IF;
        RAISE EXCEPTION
            'ABORT 0088: ops role % INHERITS fx_rates_writer; the SET ROLE gate would be '
            'fictional and the API would be callable ambiently', v_ops_role;
    END IF;
    IF NOT pg_has_role(v_ops_role, 'fx_rates_writer', 'SET') THEN
        RAISE EXCEPTION
            'ABORT 0088: ops role % cannot SET ROLE fx_rates_writer; the operational entry '
            'path of G3.3-c(9) would not exist', v_ops_role;
    END IF;

    RAISE NOTICE '0088: fx_rates_writer granted to ops role % (INHERIT FALSE, SET TRUE)', v_ops_role;
END
$ops$;

-- ---------------------------------------------------------------------
-- 11. OWNER TRANSFER — the executable sequence of G3.3-c(8), in order.
--
--     (i)   CREATE ROLE                                     [section 1]
--     (ii)  GRANT USAGE permanently + CREATE temporarily
--     (iii) GRANT fx_rates_owner TO <migration role>
--             WITH ADMIN FALSE, INHERIT FALSE, SET TRUE     temporarily
--     (iv)  transfer both tables and all three functions
--     (v)   REVOKE that temporary membership
--     (vi)  REVOKE CREATE ON SCHEMA public
--     (vii) retain only what the owned objects need, plus the explicit
--           GRANT SELECT ON credit_lots (section 9)
--
--     The <migration role> is VERIFIED against the runner's actual connected
--     role — `current_user` — never assumed to be `neondb_owner`.
-- ---------------------------------------------------------------------
GRANT USAGE  ON SCHEMA public TO fx_rates_owner;   -- (ii) permanent
GRANT CREATE ON SCHEMA public TO fx_rates_owner;   -- (ii) temporary

DO $xfer$
DECLARE
    v_migration_role TEXT := current_user;   -- (viii) verified, not assumed
    v_granted        BOOLEAN := FALSE;
BEGIN
    -- (iii) temporary SET capability, only if not already held.
    IF NOT pg_has_role(v_migration_role, 'fx_rates_owner', 'SET') THEN
        EXECUTE format(
            'GRANT fx_rates_owner TO %I WITH ADMIN FALSE, INHERIT FALSE, SET TRUE',
            v_migration_role);
        v_granted := TRUE;
    END IF;

    -- (iv) transfer.
    EXECUTE 'ALTER TABLE public.fx_rates             OWNER TO fx_rates_owner';
    EXECUTE 'ALTER TABLE public.fx_rates_corrections OWNER TO fx_rates_owner';
    EXECUTE 'ALTER TABLE public.fx_kmk_periods       OWNER TO fx_rates_owner';
    EXECUTE 'ALTER FUNCTION public.fx_rates_reject_mutation() OWNER TO fx_rates_owner';
    EXECUTE 'ALTER FUNCTION public.fx_rates_enter(TEXT,DATE,NUMERIC,TEXT,TEXT) OWNER TO fx_rates_owner';
    EXECUTE 'ALTER FUNCTION public.fx_rates_correct(TEXT,DATE,NUMERIC,TEXT,TEXT,TEXT) OWNER TO fx_rates_owner';

    -- (iv-b) REFERENCES for the migration role, and ONLY references.
    --
    -- Once (v) revokes the membership, the migration role holds NOTHING on
    -- fx_rates — the intent — but that also makes `ADD CONSTRAINT ...
    -- REFERENCES fx_rates` impossible in EVERY LATER MIGRATION. `0089` hit
    -- exactly that when it recreated the credit_lots FK.
    --
    -- 🔴 It must be granted HERE, as the NEW owner, and not before the
    -- transfer: `ALTER TABLE ... OWNER TO` REWRITES ACL entries whose grantor
    -- was the old owner, so a self-grant made while neondb_owner still owned
    -- the table is silently reassigned to fx_rates_owner and the migration role
    -- ends up with nothing. Measured, not assumed — that is what the first
    -- attempt did, and `has_table_privilege(...,'REFERENCES')` came back false.
    --
    -- REFERENCES confers no read and no write: it permits pointing a foreign
    -- key at this table and nothing else.
    EXECUTE 'SET LOCAL ROLE fx_rates_owner';
    EXECUTE format('GRANT REFERENCES ON public.fx_rates TO %I', v_migration_role);
    EXECUTE 'RESET ROLE';

    -- (v) REVOKE the temporary membership. Leaving it standing would give the
    --     migration role a permanent, assumable path into the owner identity —
    --     precisely the leak the three-role split exists to prevent.
    IF v_granted THEN
        EXECUTE format('REVOKE fx_rates_owner FROM %I', v_migration_role);
    END IF;
END
$xfer$;

-- (vi)
REVOKE CREATE ON SCHEMA public FROM fx_rates_owner;

COMMIT;

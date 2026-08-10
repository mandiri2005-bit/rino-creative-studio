-- =====================================================================
-- 0089_g3_birth_path_ordered_fx_locks.sql
--
-- GATE 2. Two things, and they belong in one migration because the second
-- cannot be written correctly without the first.
--
--   (1) UNIFY the lot's FX reference date into ONE normative column,
--       `credit_lots.fx_rate_date`. `0086` shipped `rate_date`; `PLAN-046
--       §G3.3-b`'s field table and `§G3.3-c(3)`'s finality predicate both name
--       `fx_rate_date`. `0088` added the normative column beside `0086`'s and
--       pinned the two equal, which stopped them diverging but left two columns
--       for one business value — a defect waiting for a writer to pick the
--       wrong one. This removes the duplication.
--
--   (2) THE BIRTH PATH, with the lock discipline `G3.3-c(3)` proves deadlock
--       freedom against: the engine takes **every** `fx_rates` `FOR SHARE` lock
--       FIRST, in ONE statement ORDERED ascending by `(currency_pair,
--       rate_date)`, BEFORE any write, and **never acquires an `fx_rates` lock
--       again** once writing has begun.
--
--       Paired with the corrector's guarantee — it holds at most ONE `fx_rates`
--       row lock per transaction, because a second `fx_rates_correct()` fails
--       at the XID guard before locking — single-holder versus ordered-acquirer
--       cannot form a cycle. `0088` implemented the corrector half and could
--       not test the interaction because this half did not exist. It does now,
--       and `T72(11)` is tested against it for real.
--
-- 🔴 STILL NOT SHIPPABLE ALONE, AND THE WRITER STAYS OFF.
--    `G3_LOT_WRITER_ENABLED` remains `'0'`; nothing here turns it on. `0088`
--    and `0089` are one unit — `0088`'s DEPLOY BLOCKER is cleared BY this file
--    and by nothing else, so the two ship together or not at all.
--
-- 🔴 SCOPE. This is the LOCK DISCIPLINE and the reference plumbing. The full
--    `G3.3-b` valuation review — supplier-fee anchor, tax owner, sales channel,
--    rounding — is Gate 3. What is implemented here is exactly what the birth
--    path needs to REFERENCE a rate at all: resolve, lock, value, write, and
--    STALL rather than invent when the rate is absent (`e4` = α STALL).
--
-- FORWARD-ONLY WHERE IT MATTERS. `0031`, `0086` and `0087` are applied in
-- production and are NOT edited. `0088` IS edited alongside this file, and that
-- is correct rather than an exception: the never-edit rule exists to protect
-- environments that have already run a migration, and `0088` has run in none —
-- it is unshipped, uncommitted, and ships as one unit with this file. Building
-- this migration surfaced one thing `0088` had to add: after it transfers
-- `fx_rates` away and revokes the temporary membership, the migration role can
-- no longer point a foreign key at that table, so `0088` now grants itself
-- `REFERENCES` (and only `REFERENCES`) before the transfer.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regclass('public.fx_rates') IS NULL
       OR NOT EXISTS (SELECT 1 FROM pg_attribute
                       WHERE attrelid='public.fx_rates'::regclass
                         AND attname='idr_per_major_unit' AND NOT attisdropped) THEN
        RAISE EXCEPTION 'ABORT 0089: apply 0088 first (fx_rates.idr_per_major_unit is absent)';
    END IF;
    IF to_regproc('public.g3_write_lot') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0089: apply 0086 first (g3_write_lot is absent)';
    END IF;
    IF EXISTS (SELECT 1 FROM public.credit_lots) THEN
        RAISE EXCEPTION
            'ABORT 0089: credit_lots holds row(s); the column unification below assumes the '
            '0086 G0 precondition (0 rows, 0 writers) still holds.';
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. UNIFY THE DATE COLUMN.
--
--    Drop 0088's added column, rename 0086's to the normative name, and put
--    the constraints back on the survivor. credit_lots has ZERO rows, so this
--    is a pure schema move with no data to reconcile.
-- ---------------------------------------------------------------------
ALTER TABLE credit_lots
    DROP CONSTRAINT credit_lots_fx_rate_fk,
    DROP CONSTRAINT credit_lots_valued_has_fx_reference,
    DROP CONSTRAINT credit_lots_unvalued_has_no_fx_reference,
    DROP CONSTRAINT credit_lots_fx_rate_date_agrees_with_0086_rate_date,
    DROP COLUMN fx_rate_date;

ALTER TABLE credit_lots RENAME COLUMN rate_date TO fx_rate_date;

ALTER TABLE credit_lots
    ADD CONSTRAINT credit_lots_fx_rate_fk
        FOREIGN KEY (fx_currency_pair, fx_rate_date)
        REFERENCES fx_rates (currency_pair, rate_date) ON DELETE RESTRICT,
    ADD CONSTRAINT credit_lots_valued_has_fx_reference
        CHECK (valuation_status <> 'valued'
               OR (fx_currency_pair IS NOT NULL AND fx_rate_date IS NOT NULL
                   AND src_currency IS NOT NULL
                   AND fx_currency_pair = src_currency || '/IDR')),
    ADD CONSTRAINT credit_lots_unvalued_has_no_fx_reference
        CHECK (valuation_status = 'valued'
               OR (fx_currency_pair IS NULL AND fx_rate_date IS NULL));

-- The CONSIDERATION that produced dpp_total_idr, recorded explicitly.
--
-- Under `sales_channel='dodo_mor'` / `tax_owner='provider'` the customer gross,
-- Dodo's fee and Dodo's tax are PROVIDER RECONCILIATION EVIDENCE — they are not
-- Wimba's DPP. Wimba's transaction price is the Decision-3A Supplier Fee. These
-- two columns name that figure on the lot, so `dpp_total_idr`'s source is a
-- stated fact rather than an inference, and nothing has to reinterpret the
-- legacy meaning of `gross_idr` to find it.
--
-- 🔴 `payment_fees` and `tax` are subtracted ONCE, here, in source minor units.
-- They must NOT be subtracted again downstream through `fee_idr`/`net_idr` —
-- that would deduct the same amounts twice.
ALTER TABLE credit_lots
    ADD COLUMN IF NOT EXISTS consideration_minor    BIGINT,
    ADD COLUMN IF NOT EXISTS consideration_currency TEXT;

ALTER TABLE credit_lots
    ADD CONSTRAINT credit_lots_consideration_paired
        CHECK ((consideration_minor IS NULL) = (consideration_currency IS NULL)),
    ADD CONSTRAINT credit_lots_consideration_currency_format
        CHECK (consideration_currency IS NULL OR consideration_currency ~ '^[A-Z]{3}$'),
    -- valued ⇒ the consideration that priced it is on the row, and it is the
    -- same currency the FX reference converted from.
    ADD CONSTRAINT credit_lots_valued_has_consideration
        CHECK (valuation_status <> 'valued'
               OR (consideration_minor IS NOT NULL
                   AND consideration_currency = src_currency)),
    ADD CONSTRAINT credit_lots_unvalued_has_no_consideration
        CHECK (valuation_status = 'valued' OR consideration_minor IS NULL);

COMMENT ON COLUMN credit_lots.consideration_minor IS
  'The Decision-3A Supplier Fee, in provider minor units: payment - payment_fees - tax, subtracted in minor units before any FX conversion or rounding. THIS is the source of dpp_total_idr under dodo_mor; the customer gross and the provider fee/tax are reconciliation evidence and are deliberately not stored here.';

COMMENT ON COLUMN credit_lots.fx_rate_date IS
  'THE FX reference date — one column, normative name (PLAN-046 G3.3-b). Renamed from 0086''s rate_date by 0089; 0088''s duplicate of the same name was dropped in the same statement. Derived from the same persisted payment_at as acquired_at, never recomputed.';


-- ---------------------------------------------------------------------
-- 2. THE RATE DATE — Asia/Jakarta, never UTC.
--
--    `G3.3-b`: `rate_date := (<the persisted instant> AT TIME ZONE
--    'Asia/Jakarta')::date`, and `D22`=B fixes the convention as **WIB**.
--
--    🔴 An earlier draft used `AT TIME ZONE 'UTC'`. WIB is UTC+7, so every
--    payment between 17:00 and 24:00 UTC falls on the NEXT Jakarta day: the lot
--    would reference the previous day's rate and be valued at a price that was
--    never in force for it. That is a silently wrong number, not a rounding
--    difference, and it is invisible in any test whose fixtures avoid the
--    boundary — which is why one now sits exactly on it.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_wib_rate_date(p_instant TIMESTAMPTZ)
RETURNS DATE
LANGUAGE sql
IMMUTABLE
PARALLEL SAFE
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT (p_instant AT TIME ZONE 'Asia/Jakarta')::date;
$$;
COMMENT ON FUNCTION public.g3_wib_rate_date IS
  'The FX rate date for an instant, in Asia/Jakarta (G3.3-b, D22=B). NEVER UTC: WIB is UTC+7, so 17:00-24:00 UTC belongs to the NEXT Jakarta day and a UTC-derived date would reference a rate that was never in force for the payment.';

-- ---------------------------------------------------------------------
-- 3. RESOLUTION — pure, lock-free, and TOTAL.
--
--    Returns the whole decision for one request: the authoritative instant, the
--    provider terms, the FX reference, and — crucially — whether this request
--    must STALL. Nothing downstream re-derives any of it.
--
--    `D22`=B is **USD ONLY**. Any other paid currency is an UNSUPPORTED
--    CURRENCY and STALLS (`α`); it does not quietly become `<CCY>/IDR` and get
--    divided by 100 the moment somebody enters a matching rate row.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_resolve_request(p_request JSONB)
RETURNS JSONB
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_source   TEXT := p_request->>'source';
    v_acq      TIMESTAMPTZ;
    v_fee      BIGINT;
    v_ccy      TEXT;
    v_channel  TEXT;
    v_owner    TEXT;
    v_pair     TEXT := NULL;
    v_date     DATE := NULL;
    v_stall    TEXT := NULL;
BEGIN
    IF p_request->>'provider' IS NOT NULL AND p_request->>'provider_payment_id' IS NOT NULL THEN
        SELECT g.payment_at, g.supplier_fee_minor, g.supplier_fee_currency,
               g.sales_channel, g.tax_owner
          INTO v_acq, v_fee, v_ccy, v_channel, v_owner
          FROM public.g3_provider_payments g
         WHERE g.provider = p_request->>'provider'
           AND g.provider_payment_id = p_request->>'provider_payment_id';
    ELSE
        v_acq := (p_request->>'attested_at')::TIMESTAMPTZ;
    END IF;

    IF v_source = 'topup' AND v_ccy IS NOT NULL THEN
        IF v_ccy <> 'USD' THEN
            -- D22=B: unsupported currency ⇒ α STALL. NOT an unpriced lot, and
            -- emphatically not a <CCY>/IDR reference that a stray rate row
            -- would silently make spendable.
            v_stall := format('unsupported source currency %s (D22=B is USD only)', v_ccy);
        ELSE
            v_pair := 'USD/IDR';
            v_date := public.g3_wib_rate_date(v_acq);
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'tenant',           p_request->>'tenant',
        'source',           v_source,
        'credits',          p_request->>'credits',
        'ledger_op_id',     p_request->>'ledger_op_id',
        'provenance_kind',  COALESCE(p_request->>'provenance_kind', 'grant'),
        'acquired_at',      v_acq,
        'fee_minor',        v_fee,
        'fee_currency',     v_ccy,
        'sales_channel',    v_channel,
        'tax_owner',        v_owner,
        'fx_pair',          v_pair,
        'fx_date',          v_date,
        'stall',            v_stall);
END
$$;
COMMENT ON FUNCTION public.g3_resolve_request IS
  'Resolves ONE request completely: instant, provider terms, FX reference, stall verdict. The result is MATERIALISED by g3_birth_lots and is the only thing the write phase ever reads — so provider terms cannot change under the engine between resolution and write.';

-- ---------------------------------------------------------------------
-- 4. THE ORDERED LOCK PHASE.
--
--    ONE statement. `ORDER BY currency_pair, rate_date` then `FOR SHARE`.
--    Returns HOW MANY rows it actually locked, and that number is CHECKED by
--    the caller — a lock function whose result is discarded is a lock function
--    that cannot report a missing rate.
--
--    SECURITY DEFINER, owned by fx_rates_owner: after 0088 nobody else may lock
--    a row in fx_rates, not even the engine.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_lock_fx_refs(p_refs JSONB)
RETURNS INT
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_locked INT;
BEGIN
    IF p_refs IS NULL OR jsonb_array_length(p_refs) = 0 THEN
        RETURN 0;
    END IF;

    WITH wanted AS (
        SELECT DISTINCT (r->>'pair')::TEXT AS currency_pair, (r->>'date')::DATE AS rate_date
          FROM jsonb_array_elements(p_refs) r
    ),
    locked AS (
        SELECT f.currency_pair, f.rate_date
          FROM public.fx_rates f
          JOIN wanted w ON w.currency_pair = f.currency_pair AND w.rate_date = f.rate_date
         ORDER BY f.currency_pair, f.rate_date      -- <- the ordering, in ONE statement
           FOR SHARE OF f
    )
    SELECT count(*) INTO v_locked FROM locked;

    RETURN v_locked;
END
$$;
COMMENT ON FUNCTION public.g3_lock_fx_refs IS
  'Takes EVERY fx_rates FOR SHARE lock the caller needs, in ONE statement ordered ascending by (currency_pair, rate_date), before the caller writes anything. Returns the count actually locked so the caller can detect an absent rate BEFORE writing.';

-- ---------------------------------------------------------------------
-- 5. THE RATE READER — no locking clause.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_fx_rate_at(p_pair TEXT, p_date DATE)
RETURNS NUMERIC
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT f.idr_per_major_unit
      FROM public.fx_rates f
     WHERE f.currency_pair = p_pair AND f.rate_date = p_date;
$$;
COMMENT ON FUNCTION public.g3_fx_rate_at IS
  'Reads a rate for the write phase. NO locking clause: the row is already held FOR SHARE by g3_lock_fx_refs, and a second lock here would violate "no fx_rates lock acquisition after writing begins".';

-- ---------------------------------------------------------------------
-- 6. THE WRITER — every input is a PARAMETER.
--
--    🔴 It reads NO provider table. An earlier draft re-read
--    `g3_provider_payments` here, which meant `supplier_fee_currency` could go
--    NULL -> 'USD' between the resolve phase and the write phase: the engine
--    would then use an FX reference that was never in the lock set, and the
--    "all locks first" claim would be false while every test still passed.
--    Passing the resolved vector in is what makes that impossible.
-- ---------------------------------------------------------------------
DROP FUNCTION IF EXISTS public.g3_write_lot(UUID,TEXT,BIGINT,TEXT,TEXT,TEXT,TEXT,TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.g3_release_quarantined_topup(TEXT,TEXT,UUID,BIGINT,TEXT,TEXT,TEXT);
DROP FUNCTION IF EXISTS public.g3_write_lot_locked(UUID,TEXT,BIGINT,TEXT,TEXT,TEXT,TEXT,TIMESTAMPTZ);
DROP FUNCTION IF EXISTS public.g3_fx_ref_for(TEXT,TEXT,TIMESTAMPTZ);

CREATE OR REPLACE FUNCTION public.g3_write_lot_resolved(p_resolved JSONB)
RETURNS TABLE (
    lot_id           UUID,
    acquired_at      TIMESTAMPTZ,
    fx_rate_date     DATE,
    is_priced        BOOLEAN,
    grandfathered    BOOLEAN,
    reason           TEXT,
    valuation_status TEXT
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_tenant   UUID        := (p_resolved->>'tenant')::UUID;
    v_source   TEXT        := p_resolved->>'source';
    v_credits  BIGINT      := (p_resolved->>'credits')::BIGINT;
    v_op       TEXT        := p_resolved->>'ledger_op_id';
    v_prov     TEXT        := p_resolved->>'provenance_kind';
    v_acquired TIMESTAMPTZ := (p_resolved->>'acquired_at')::TIMESTAMPTZ;
    v_fee      BIGINT      := (p_resolved->>'fee_minor')::BIGINT;
    v_ccy      TEXT        := p_resolved->>'fee_currency';
    v_channel  TEXT        := p_resolved->>'sales_channel';
    v_owner    TEXT        := p_resolved->>'tax_owner';
    v_pair     TEXT        := p_resolved->>'fx_pair';
    v_fxdate   DATE        := (p_resolved->>'fx_date')::DATE;
    v_gf       BOOLEAN;
    v_reason   TEXT;
    v_priced   BOOLEAN := false;
    v_unpriced TEXT;
    v_status   TEXT := 'unknown';
    v_dpp      NUMERIC(18,2) := 0;
    v_ppc      NUMERIC(14,4) := 0;
    v_rate     NUMERIC(18,6);
    v_lot      UUID;
    v_existing RECORD;
    v_digest_now TEXT;
BEGIN
    IF v_tenant IS NULL OR v_source IS NULL OR v_op IS NULL THEN
        RAISE EXCEPTION 'g3_write_lot: tenant, source and ledger_op_id are required';
    END IF;
    IF NOT (v_credits > 0) THEN
        RAISE EXCEPTION 'g3_write_lot: credits must be > 0 (got %)', v_credits;
    END IF;
    IF v_acquired IS NULL THEN
        RAISE EXCEPTION
            'g3_write_lot: no authoritative timestamp for op_id=%; failing closed with no lot', v_op;
    END IF;

    v_gf := (v_source = 'topup');
    IF v_gf THEN
        v_reason := 'cr29_temp_workaround';
    END IF;

    IF v_source <> 'topup' THEN
        v_priced := false; v_unpriced := 'not_a_paid_lot';                v_status := 'verified_zero';
    ELSIF v_channel IS NULL OR v_owner IS NULL THEN
        v_priced := false; v_unpriced := 'unknown_channel_or_tax_owner';  v_status := 'unknown';
    ELSIF v_fee IS NULL OR v_ccy IS NULL THEN
        v_priced := false; v_unpriced := 'supplier_fee_not_established';  v_status := 'unknown';
    ELSE
        -- A paid, fully-termed request reaches here ONLY as USD with an FX
        -- reference the caller has already locked; g3_resolve_request stalled
        -- anything else and g3_birth_lots refused to proceed on a missing rate.
        IF v_pair IS NULL OR v_fxdate IS NULL THEN
            RAISE EXCEPTION
                'g3_write_lot: paid request % reached the write phase with no FX reference; '
                'the resolve phase must have stalled it', v_op
                USING ERRCODE = 'FX007';
        END IF;
        v_rate := public.g3_fx_rate_at(v_pair, v_fxdate);
        IF v_rate IS NULL THEN
            RAISE EXCEPTION
                'g3_write_lot: rate % % vanished between the lock phase and the write phase',
                v_pair, v_fxdate
                USING ERRCODE = 'FX007';
        END IF;
        v_priced := true; v_unpriced := NULL; v_status := 'valued';
        -- G3.3-b: minor units -> major, times the rate, ONE terminal rounding.
        -- The full fee/tax/channel review is Gate 3.
        v_dpp := round((v_fee::NUMERIC / 100) * v_rate, 2);
        v_ppc := round(v_dpp / v_credits, 4);
    END IF;

    -- 🔴 THE EVIDENCE MUST NOT HAVE MOVED. The vector froze the lock set; it
    -- does NOT freeze the ledger, because a row lock cannot stop an INSERT. If
    -- an entry arrived after the anchor was derived, the anchor is STALE and
    -- writing it books a figure the provider has since contradicted. An earlier
    -- draft booked 905 while the authoritative ledger had moved to 850 and a
    -- test called that snapshot stability; it is a 55 overstatement.
    IF p_resolved->>'ledger_digest' IS NOT NULL THEN
        v_digest_now := public.g3_ledger_digest(
            p_resolved->>'provider', p_resolved->>'provider_payment_id');
        IF v_digest_now IS DISTINCT FROM (p_resolved->>'ledger_digest') THEN
            RAISE EXCEPTION
                'g3_write_lot: the provider ledger changed between resolution and write for % '
                '(digest % -> %). The anchor is stale; no lot is born. Re-resolve under the fence.',
                p_resolved->>'provider_payment_id', p_resolved->>'ledger_digest', v_digest_now
                USING ERRCODE = 'FX012';
        END IF;
    END IF;

    -- 🔴 A TOP-UP MAY NOT BE WRITTEN FROM A HAND-MADE VECTOR. Checked AFTER the
    --    digest, so "the evidence moved" is reported as such rather than as a
    --    provenance failure — the two have different remedies. Provider ids, a
    --    digest and an anchor that the ledger actually yields are all required,
    --    and the anchor is RE-DERIVED and compared here. Without this the
    --    writer is a public helper that will price a lot from whatever it is
    --    handed — which is how a vector saying "USD, fully termed" could book a
    --    valued lot against a payment with no evidence at all.
    IF v_source = 'topup' AND v_status = 'valued' THEN
        IF p_resolved->>'provider' IS NULL OR p_resolved->>'provider_payment_id' IS NULL
           OR p_resolved->>'ledger_digest' IS NULL THEN
            RAISE EXCEPTION
                'g3_write_lot: a valued top-up needs provider identity and a ledger digest; this '
                'vector was not produced by g3_resolve_request'
                USING ERRCODE = 'FX014';
        END IF;
        DECLARE v_check RECORD;
        BEGIN
            SELECT a.anchor_minor, a.currency INTO v_check
              FROM public.g3_supplier_fee_anchor(
                       p_resolved->>'provider', p_resolved->>'provider_payment_id',
                       (p_resolved->>'settlement_tax_minor')::BIGINT) a;
            IF v_check.anchor_minor IS DISTINCT FROM v_fee
               OR v_check.currency IS DISTINCT FROM v_ccy THEN
                RAISE EXCEPTION
                    'g3_write_lot: vector anchor % %  does not match the ledger anchor % % for %',
                    v_fee, v_ccy, v_check.anchor_minor, v_check.currency,
                    p_resolved->>'provider_payment_id'
                    USING ERRCODE = 'FX014';
            END IF;
        END;
    END IF;

    -- An unpriced lot carries NO FX reference (fxpop §C).
    IF v_status <> 'valued' THEN
        v_pair := NULL; v_fxdate := NULL;
    END IF;

    -- 🔴 INSERT ... ON CONFLICT DO NOTHING RETURNING, then read back and
    --    compare. `SELECT` then `INSERT` is race-prone: two concurrent callers
    --    both see no row and the loser surfaces a raw 23505 instead of the
    --    idempotent replay this contract promises.
    INSERT INTO public.credit_lots (
        tenant_id, source, is_paid, credits_granted, credits_remaining,
        price_per_credit_idr, dpp_total_idr, recognized_idr,
        ledger_op_id, granted_at,
        acquired_at, is_grandfathered, grandfather_reason, provenance_kind,
        is_priced, unpriced_reason,
        valuation_status, src_currency, fx_currency_pair, fx_rate_date,
        consideration_minor, consideration_currency, fx_rate_at_grant
    ) VALUES (
        v_tenant, v_source, (v_source = 'topup'), v_credits, v_credits,
        v_ppc, v_dpp, 0,
        v_op, v_acquired,
        v_acquired, v_gf, v_reason, v_prov,
        v_priced, v_unpriced,
        v_status, CASE WHEN v_pair IS NULL THEN NULL ELSE split_part(v_pair,'/',1) END,
        v_pair, v_fxdate,
        CASE WHEN v_status = 'valued' THEN v_fee ELSE NULL END,
        CASE WHEN v_status = 'valued' THEN v_ccy ELSE NULL END,
        v_rate
    )
    ON CONFLICT (tenant_id, ledger_op_id) WHERE ledger_op_id IS NOT NULL DO NOTHING
    RETURNING id INTO v_lot;

    IF v_lot IS NOT NULL THEN
        RETURN QUERY SELECT v_lot, v_acquired, v_fxdate, v_priced, v_gf, v_reason, v_status;
        RETURN;
    END IF;

    -- Replay. Compare the WHOLE birth-immutable tuple (G3.2), not just the
    -- timestamps: an earlier draft compared acquired_at / grandfather /
    -- provenance / fx_rate_date only, so a replay carrying a DIFFERENT anchor
    -- silently returned the original lot and the divergence was never seen.
    SELECT * INTO v_existing FROM public.credit_lots
     WHERE tenant_id = v_tenant AND ledger_op_id = v_op;

    IF NOT FOUND THEN
        RAISE EXCEPTION
            'g3_write_lot: conflict on op_id=% but no row is visible; the uniqueness arbiter is '
            'not the one this function assumes', v_op;
    END IF;

    IF v_existing.acquired_at            IS DISTINCT FROM v_acquired
    OR v_existing.is_grandfathered       IS DISTINCT FROM v_gf
    OR v_existing.grandfather_reason     IS DISTINCT FROM v_reason
    OR v_existing.provenance_kind        IS DISTINCT FROM v_prov
    OR v_existing.source                 IS DISTINCT FROM v_source
    OR v_existing.credits_granted        IS DISTINCT FROM v_credits
    OR v_existing.valuation_status       IS DISTINCT FROM v_status
    OR v_existing.is_priced              IS DISTINCT FROM v_priced
    OR v_existing.consideration_minor    IS DISTINCT FROM
         (CASE WHEN v_status = 'valued' THEN v_fee ELSE NULL END)
    OR v_existing.consideration_currency IS DISTINCT FROM
         (CASE WHEN v_status = 'valued' THEN v_ccy ELSE NULL END)
    OR v_existing.dpp_total_idr          IS DISTINCT FROM v_dpp
    OR v_existing.price_per_credit_idr   IS DISTINCT FROM v_ppc
    OR v_existing.fx_currency_pair       IS DISTINCT FROM v_pair
    OR v_existing.fx_rate_date           IS DISTINCT FROM v_fxdate
    OR v_existing.fx_rate_at_grant       IS DISTINCT FROM v_rate THEN
        RAISE EXCEPTION
            'g3_write_lot: birth-immutable mismatch on replay for op_id=% — stored '
            '(consideration %, dpp %, ppc %, status %, fx % %) vs replay '
            '(consideration %, dpp %, ppc %, status %, fx % %). A replay that would change a '
            'valuation is a defect, not an idempotent retry.',
            v_op,
            v_existing.consideration_minor, v_existing.dpp_total_idr,
            v_existing.price_per_credit_idr, v_existing.valuation_status,
            v_existing.fx_currency_pair, v_existing.fx_rate_date,
            CASE WHEN v_status = 'valued' THEN v_fee ELSE NULL END, v_dpp, v_ppc, v_status,
            v_pair, v_fxdate
            USING ERRCODE = 'FX013';
    END IF;

    RETURN QUERY SELECT v_existing.id, v_existing.acquired_at, v_existing.fx_rate_date,
                        v_existing.is_priced, v_existing.is_grandfathered,
                        v_existing.grandfather_reason, v_existing.valuation_status;
END
$$;
COMMENT ON FUNCTION public.g3_write_lot_resolved IS
  'Writes ONE lot from an ALREADY-RESOLVED vector. Reads no provider table and takes no fx_rates lock — both are what make "the lock set is complete and stable" true rather than hoped for.';

-- ---------------------------------------------------------------------
-- 7. THE ENGINE. Resolve → materialise → lock → VERIFY → write.
--
--    The verification step is the one that was missing: `g3_lock_fx_refs`
--    returns how many rows it locked, and if that is fewer than the number of
--    DISTINCT references the batch requires, at least one published rate does
--    not exist. `e4` = α **STALL**: the replay cursor halts and NO LOT IS BORN.
--    An earlier draft discarded the count and wrote an `unknown` lot instead —
--    the exact inverse of `T62`, and the kind of fallback that turns a missing
--    input into a silently unpriced balance.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_birth_lots(p_requests JSONB)
RETURNS TABLE (
    lot_id           UUID,
    acquired_at      TIMESTAMPTZ,
    fx_rate_date     DATE,
    is_priced        BOOLEAN,
    grandfathered    BOOLEAN,
    reason           TEXT,
    valuation_status TEXT
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_resolved JSONB := '[]'::JSONB;
    v_refs     JSONB := '[]'::JSONB;
    r          JSONB;
    v_one      JSONB;
    v_required INT;
    v_locked   INT;
BEGIN
    IF p_requests IS NULL OR jsonb_array_length(p_requests) = 0 THEN
        RETURN;
    END IF;

    -- ── PHASE 0: FENCE every payment this batch touches, in a FIXED order,
    --    before a single request is resolved. ──────────────────────────────
    PERFORM public.g3_fence_payments(p_requests);

    -- ── PHASE 1: RESOLVE, and MATERIALISE. No writes. ───────────────────────
    FOR r IN SELECT * FROM jsonb_array_elements(p_requests) LOOP
        v_one := public.g3_resolve_request(r);

        IF v_one->>'stall' IS NOT NULL THEN
            RAISE EXCEPTION 'g3_birth_lots: STALL for op_id=% — %',
                v_one->>'ledger_op_id', v_one->>'stall'
                USING ERRCODE = 'FX008';
        END IF;

        v_resolved := v_resolved || jsonb_build_array(v_one);
        IF v_one->>'fx_pair' IS NOT NULL THEN
            v_refs := v_refs || jsonb_build_array(
                jsonb_build_object('pair', v_one->>'fx_pair', 'date', v_one->>'fx_date'));
        END IF;
    END LOOP;

    -- ── PHASE 2: LOCK all of them, ordered, in ONE statement. ───────────────
    SELECT count(DISTINCT (x->>'pair') || '|' || (x->>'date')) INTO v_required
      FROM jsonb_array_elements(v_refs) x;
    v_required := COALESCE(v_required, 0);

    v_locked := public.g3_lock_fx_refs(v_refs);

    -- ── PHASE 2b: VERIFY the lock set is COMPLETE, before any write. ────────
    IF v_locked <> v_required THEN
        RAISE EXCEPTION
            'g3_birth_lots: STALL — % of % required FX rate(s) exist; no lot is born. '
            'Enter the published rate through python/ops/fx_rates_entry.py and replay.',
            v_locked, v_required
            USING ERRCODE = 'FX007';
    END IF;

    -- ── PHASE 3: WRITE from the MATERIALISED vector only. ───────────────────
    RETURN QUERY
    SELECT w.* FROM jsonb_array_elements(v_resolved) res,
                    LATERAL public.g3_write_lot_resolved(res) w;
END
$$;
COMMENT ON FUNCTION public.g3_birth_lots IS
  'THE BIRTH PATH. Resolve+materialise, lock every reference in ONE ordered statement, VERIFY the lock count equals the required count (α STALL if not — no lot is born), then write from the materialised vector only. The ordered acquirer of G3.3-c(3).';

-- ---------------------------------------------------------------------
-- 8. Single-lot entry point and quarantine->release. Both go through the
--    engine, so neither can lock out of order or skip the STALL.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_write_lot(
    p_tenant              UUID,
    p_source              TEXT,
    p_credits             BIGINT,
    p_ledger_op_id        TEXT,
    p_provenance_kind     TEXT        DEFAULT 'grant',
    p_provider            TEXT        DEFAULT NULL,
    p_provider_payment_id TEXT        DEFAULT NULL,
    p_attested_at         TIMESTAMPTZ DEFAULT NULL
) RETURNS TABLE (
    lot_id           UUID,
    acquired_at      TIMESTAMPTZ,
    fx_rate_date     DATE,
    is_priced        BOOLEAN,
    grandfathered    BOOLEAN,
    reason           TEXT,
    valuation_status TEXT
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $$
BEGIN
    RETURN QUERY SELECT * FROM public.g3_birth_lots(jsonb_build_array(jsonb_build_object(
        'tenant', p_tenant, 'source', p_source, 'credits', p_credits,
        'ledger_op_id', p_ledger_op_id, 'provenance_kind', p_provenance_kind,
        'provider', p_provider, 'provider_payment_id', p_provider_payment_id,
        'attested_at', p_attested_at)));
END
$$;

CREATE OR REPLACE FUNCTION public.g3_release_quarantined_topup(
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_tenant              UUID,
    p_credits             BIGINT,
    p_ledger_op_id        TEXT,
    p_released_by         TEXT,
    p_disposition         TEXT
) RETURNS TABLE (
    lot_id           UUID,
    acquired_at      TIMESTAMPTZ,
    fx_rate_date     DATE,
    is_priced        BOOLEAN,
    grandfathered    BOOLEAN,
    reason           TEXT,
    valuation_status TEXT
)
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE v_q RECORD;
BEGIN
    IF p_released_by IS NULL OR btrim(p_released_by) = ''
    OR p_disposition IS NULL OR btrim(p_disposition) = '' THEN
        RAISE EXCEPTION 'g3_release_quarantined_topup: an attributed disposition is required';
    END IF;

    SELECT * INTO v_q FROM public.g3_topup_quarantine
     WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id
       AND released_at IS NULL
       FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'g3_release_quarantined_topup: no open quarantine for % %',
            p_provider, p_provider_payment_id;
    END IF;

    RETURN QUERY SELECT * FROM public.g3_write_lot(
        p_tenant, 'topup', p_credits, p_ledger_op_id, 'grant',
        p_provider, p_provider_payment_id, NULL);

    UPDATE public.g3_topup_quarantine
       SET released_at = clock_timestamp(), released_by = p_released_by,
           disposition = p_disposition
     WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id;
END
$$;

-- ---------------------------------------------------------------------
-- 9. OWNERSHIP for the fx_rates-touching definers, then privileges.
-- ---------------------------------------------------------------------
DO $xfer$
DECLARE
    v_migration_role TEXT := current_user;
    v_granted        BOOLEAN := FALSE;
BEGIN
    IF NOT pg_has_role(v_migration_role, 'fx_rates_owner', 'SET') THEN
        EXECUTE format(
            'GRANT fx_rates_owner TO %I WITH ADMIN FALSE, INHERIT FALSE, SET TRUE',
            v_migration_role);
        v_granted := TRUE;
    END IF;

    EXECUTE 'GRANT CREATE ON SCHEMA public TO fx_rates_owner';
    EXECUTE 'ALTER FUNCTION public.g3_lock_fx_refs(JSONB) OWNER TO fx_rates_owner';
    EXECUTE 'ALTER FUNCTION public.g3_fx_rate_at(TEXT,DATE) OWNER TO fx_rates_owner';
    EXECUTE 'REVOKE CREATE ON SCHEMA public FROM fx_rates_owner';

    IF v_granted THEN
        EXECUTE format('REVOKE fx_rates_owner FROM %I', v_migration_role);
    END IF;
END
$xfer$;

REVOKE ALL ON FUNCTION public.g3_wib_rate_date(TIMESTAMPTZ)                 FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_resolve_request(JSONB)                     FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_lock_fx_refs(JSONB)                        FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_fx_rate_at(TEXT,DATE)                      FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_write_lot_resolved(JSONB)                  FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_birth_lots(JSONB)                          FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_write_lot(UUID,TEXT,BIGINT,TEXT,TEXT,TEXT,TEXT,TIMESTAMPTZ)
    FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_release_quarantined_topup(TEXT,TEXT,UUID,BIGINT,TEXT,TEXT,TEXT)
    FROM PUBLIC, app_user;

COMMIT;

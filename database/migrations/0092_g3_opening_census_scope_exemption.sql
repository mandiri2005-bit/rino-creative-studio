-- =====================================================================
-- 0092_g3_opening_census_scope_exemption.sql
--
-- GATE 4: put the `opening_census` path back, WITHOUT widening anything.
--
-- 🔴 WHAT BROKE. `0090` added the Gate-3 scope lock — a top-up is admitted only
--    as `dodo_mor` + `provider` — and reads that pair from
--    `g3_provider_payments`. An `opening_census` lot HAS NO PROVIDER PAYMENT: it
--    is an attested historical entry carrying `attested_at` and nothing else, so
--    both columns come back NULL and the lock fires. Every opening-census top-up
--    has stalled `FX008` since `0090`. That is an EXISTING Decision 1/2 path
--    killed by accident, not a channel or currency anyone now wants supported.
--    `T79`'s `H4` and `H5` were asserting it and could not report the regression,
--    because `0088`'s superuser guard had stopped the node suite running at all.
--
-- 🔴 THE FIRST CUT WAS TOO WIDE; THE SECOND WAS TOO LATE. Cut one keyed on the
--    `provenance_kind` LABEL alone, so a request could carry `opening_census`
--    AND a provider identity and walk past the channel/tax lock — a
--    caller-supplied string buying an exemption from a scope rule. Cut two
--    required both identity columns to be NULL for the exemption to apply, which
--    is correct as far as it goes, but it only withheld an exemption: the
--    mislabelled request then fell through to whatever fired first, which for a
--    payment with no ledger was `FX011`, and for one with full terms was nothing
--    at all.
--
--    So the rule is now stated POSITIVELY and FIRST, as its own guard at the top
--    of the resolver: an `opening_census` carrying EITHER half of a provider
--    identity STALLS, before the anchor derivation and before the scope lock,
--    whatever the channel, the tax owner, the ledger or the FX rate say. A test
--    can then hand it a perfectly valid `dodo_mor` + `provider` payment with
--    complete USD ledger evidence and a seeded rate, and the refusal it gets
--    back is attributable to the identity and to nothing else.
--
-- WHAT IS AND IS NOT CHANGED
--   * exempt from the CHANNEL/TAX-OWNER lock only. The currency lock, the
--     ledger-derived anchor and `D22=B` are untouched — a census entry never
--     reaches them, having no provider payment to derive an anchor from;
--   * it stays UNPRICED. Nothing here gives the path a fee, a rate or a
--     valuation. It is admitted, not valued;
--   * it still FAILS CLOSED with no lot when `attested_at` is absent — that
--     check lives in the writer and is deliberately untouched (`H5`);
--   * the writer's grandfather precedence is restored: an `opening_census` lot
--     records `grandfather_reason = 'opening_census'`, not the CR-29 workaround
--     label it was being flattened into. An ordinary top-up still records
--     `cr29_temp_workaround`.
--
-- 🔴 BOTH BODIES BELOW ARE THE ACCEPTED MIGRATIONS' OWN TEXT, MACHINE-EXTRACTED
--    (`g3_resolve_request` from `0090`, `g3_write_lot_resolved` from `0089`),
--    each with EXACTLY ONE delta. `CREATE OR REPLACE` of a plpgsql function has
--    no in-place patch, so a definition has to be restated in full; it was
--    copied programmatically rather than by hand precisely so that "restated in
--    full" cannot quietly become "restated with a difference nobody intended".
--
-- `0089` and `0090` are accepted and are NOT edited. FORWARD-ONLY.
-- `G3_LOT_WRITER_ENABLED` stays `'0'`.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regproc('public.g3_resolve_request') IS NULL
       OR to_regproc('public.g3_write_lot_resolved') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0092: apply 0089 and 0090 first';
    END IF;
END
$guard$;

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
    v_provider TEXT := p_request->>'provider';
    v_ppid     TEXT := p_request->>'provider_payment_id';
    v_acq      TIMESTAMPTZ;
    v_settle_tax BIGINT;
    v_fee      BIGINT := NULL;
    v_ccy      TEXT   := NULL;
    v_channel  TEXT;
    v_owner    TEXT;
    v_pair     TEXT := NULL;
    v_date     DATE := NULL;
    v_stall    TEXT := NULL;
    v_anchor   RECORD;
    v_digest   TEXT := NULL;
BEGIN
    -- 🔴 CENSUS IDENTITY GUARD. FIRST, BEFORE ANY DERIVATION.
    --    An `opening_census` entry is defined by the ABSENCE of a provider
    --    payment. Carrying either half of a provider identity makes it an
    --    ordinary top-up wearing the wrong label, and it STALLS — whatever the
    --    channel, the tax owner, the ledger or the FX rate say.
    --
    --    Placed here on purpose: the anchor derivation below raises FX011 for an
    --    empty ledger, and the scope lock further down raises FX008. If this ran
    --    after either of them, a mislabelled census would report whichever
    --    happened to fire first, and the identity defect — the thing that
    --    actually disqualifies it — would never surface at all.
    IF v_source = 'topup'
       AND (p_request->>'provenance_kind') = 'opening_census'
       AND (v_provider IS NOT NULL OR v_ppid IS NOT NULL) THEN
        v_stall := format(
            'opening_census carries a provider identity (%s / %s); a census entry is defined by '
            'the absence of a provider payment and is not in Gate-3 scope',
            coalesce(v_provider,'<null>'), coalesce(v_ppid,'<null>'));
    END IF;

    IF v_provider IS NOT NULL AND v_ppid IS NOT NULL THEN
        SELECT g.payment_at, g.sales_channel, g.tax_owner, g.provider_tax_minor
          INTO v_acq, v_channel, v_owner, v_settle_tax
          FROM public.g3_provider_payments g
         WHERE g.provider = v_provider AND g.provider_payment_id = v_ppid;

        -- Decision 3A: the anchor comes from the LEDGER, never from
        -- g3_provider_payments.supplier_fee_minor (settlement-derived).
        IF v_source = 'topup' AND v_acq IS NOT NULL AND v_stall IS NULL THEN
            -- Fence FIRST: hold the payment for the rest of this transaction so
            -- no acquisition step can add an entry underneath the derivation.
            PERFORM public.g3_fence_payment(v_provider, v_ppid);
            -- 🔴 NO `ELSE` BRANCH, DELIBERATELY. An earlier draft turned "no
            -- ledger" into NULL terms, which the writer then banked as an
            -- `unknown` lot — indistinguishable from a legitimate T64 outcome
            -- and the exact opposite of Decision 3A's
            -- STALL-AWAITING-PROVIDER-LEDGER. The derivation raises FX011 for
            -- an empty entry set, and that propagates out of the resolver
            -- BEFORE the lock phase and before any write.
            SELECT a.anchor_minor, a.currency INTO v_anchor
              FROM public.g3_supplier_fee_anchor(v_provider, v_ppid, v_settle_tax) a;
            v_fee := v_anchor.anchor_minor;
            v_ccy := v_anchor.currency;
            v_digest := public.g3_ledger_digest(v_provider, v_ppid);
        END IF;
    ELSE
        v_acq := (p_request->>'attested_at')::TIMESTAMPTZ;
    END IF;

    -- Gate 3 scope lock: the ONLY admitted combination is
    -- dodo_mor + provider + USD. Anything else STALLS; direct-sale is a
    -- separate contract and is deliberately NOT generalised here.
    IF v_source = 'topup' AND v_stall IS NULL
       AND NOT ((p_request->>'provenance_kind') = 'opening_census'
                AND v_provider IS NULL AND v_ppid IS NULL)
       AND (v_channel IS DISTINCT FROM 'dodo_mor' OR v_owner IS DISTINCT FROM 'provider') THEN
        v_stall := format(
            'unsupported channel/tax-owner combination (%s / %s); only dodo_mor + provider is in '
            'Gate-3 scope', coalesce(v_channel,'<null>'), coalesce(v_owner,'<null>'));
    END IF;

    IF v_source = 'topup' AND v_ccy IS NOT NULL AND v_stall IS NULL THEN
        IF v_ccy <> 'USD' THEN
            v_stall := format('unsupported source currency %s (D22=B is USD only)', v_ccy);
        ELSE
            v_pair := 'USD/IDR';
            v_date := public.g3_wib_rate_date(v_acq);
        END IF;
    END IF;

    RETURN jsonb_build_object(
        'tenant',           p_request->>'tenant',
        'provider',         v_provider,
        'provider_payment_id', v_ppid,
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
        'ledger_digest',    v_digest,
        'settlement_tax_minor', v_settle_tax,
        'stall',            v_stall);
END
$$;

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
        v_reason := CASE WHEN v_prov = 'opening_census' THEN 'opening_census'
                         ELSE 'cr29_temp_workaround' END;
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

DO $verify$
DECLARE
    v_resolve TEXT;
    v_writer  TEXT;
BEGIN
    SELECT pg_get_functiondef(p.oid) INTO v_resolve FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname='public' AND p.proname='g3_resolve_request';
    SELECT pg_get_functiondef(p.oid) INTO v_writer FROM pg_proc p
      JOIN pg_namespace n ON n.oid = p.pronamespace
     WHERE n.nspname='public' AND p.proname='g3_write_lot_resolved';

    -- The exemption is present AND is gated on the absence of a provider
    -- identity. A copy-forward that kept the label check but dropped the NULL
    -- test would apply perfectly cleanly and re-open exactly the hole the owner
    -- review found.
    IF v_resolve NOT LIKE '%opening_census%' THEN
        RAISE EXCEPTION 'ABORT 0092: the opening_census exemption is not in the installed resolver';
    END IF;
    IF v_resolve NOT LIKE '%v_provider IS NULL AND v_ppid IS NULL%' THEN
        RAISE EXCEPTION
            'ABORT 0092: the census exemption is not gated on a NULL provider identity — the '
            'provenance_kind label alone would bypass the channel/tax scope lock';
    END IF;
    IF v_resolve NOT LIKE '%v_provider IS NOT NULL OR v_ppid IS NOT NULL%' THEN
        RAISE EXCEPTION
            'ABORT 0092: the census IDENTITY GUARD is absent. Without it a mislabelled census '
            'reports whichever stall fires first (FX011 for an empty ledger, FX008 for an '
            'out-of-scope channel) and the identity defect never surfaces.';
    END IF;
    -- ...and nothing else moved.
    IF v_resolve NOT LIKE '%D22=B is USD only%' THEN
        RAISE EXCEPTION 'ABORT 0092: the currency lock did not survive the copy-forward';
    END IF;
    IF v_resolve NOT LIKE '%g3_supplier_fee_anchor%' THEN
        RAISE EXCEPTION 'ABORT 0092: the ledger-derived anchor did not survive the copy-forward';
    END IF;
    IF v_writer NOT LIKE '%cr29_temp_workaround%' THEN
        RAISE EXCEPTION 'ABORT 0092: the ordinary top-up grandfather reason was lost';
    END IF;
    IF v_writer NOT LIKE '%WHEN v_prov = ''opening_census'' THEN ''opening_census''%' THEN
        RAISE EXCEPTION 'ABORT 0092: the opening_census grandfather precedence is not installed';
    END IF;
END
$verify$;

COMMIT;

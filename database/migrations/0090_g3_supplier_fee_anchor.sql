-- =====================================================================
-- 0090_g3_supplier_fee_anchor.sql
--
-- GATE 3, part 1: the DECISION-3A supplier-fee anchor.
--
-- `L2C-CR29-DECISION-3A-SUPPLIER-FEE-ANCHOR-001.md` (`4d92d545d01dbd66…`/117),
-- binding on `MATRIX-046`:
--
--     Supplier Fee = signed payment credit − payment_fees − tax
--
-- taken from the **Dodo Balance Ledger per payment**
-- (`reference_object_id = <payment_id>`), and admitted ONLY when
--   * there is exactly ONE `payment` entry,
--   * ONE settlement currency across the entries,
--   * a `tax` entry whenever `settlement_tax > 0`,
--   * a `payment_fees` entry unless a zero fee is explicitly evidenced.
-- An unknown event type **STALLS**. `settlement_amount` is reconciliation
-- evidence ONLY — never the anchor.
--
-- 🔴 WHY THIS MIGRATION EXISTS. Measured on `pay_0Nl2TQaryBXPhF66u1GEJ`: the
--    ledger carries `payment` 1000 and `payment_fees` 95 with `settlement_tax`
--    0, so the anchor is **905**. `0086` stored **1000**, because
--    `settlement_amount` LOOKED net. That overstates the revenue base by 95 on
--    every fee-bearing transaction — the exact over-recognition Decision 3 §B2
--    exists to prevent, reappearing one layer down. `MATRIX-046` therefore adds
--    a `V46` acceptance assertion: **a lot born at `settlement_amount` while
--    `payment_fees ≠ 0` must FAIL.**
--
--    The enforcement here is structural rather than advisory: the birth path
--    stops reading `g3_provider_payments.supplier_fee_minor` and derives the
--    anchor from the persisted ledger evidence itself, so a stale or
--    settlement-derived value in that column can no longer reach a lot at all.
--
-- 🔴 UNIT AND SCOPE. Amounts are provider MINOR units and the ledger's own
--    settlement currency; `D22`=B still restricts pricing to USD, and `0089`
--    stalls anything else before this code is reached. Refund and dispute are
--    SUBSEQUENT events: they are persisted here as evidence but never alter lot
--    birth. Payout fees and payout FX post at payout and are out of scope.
--
-- 🔴 STILL NOT SHIPPABLE, WRITER STILL OFF. `0088`+`0089`+`0090` are one unit.
--    `G3_LOT_WRITER_ENABLED` stays `'0'`; nothing here turns it on.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regproc('public.g3_resolve_request') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0090: apply 0089 first (g3_resolve_request is absent)';
    END IF;
    IF to_regclass('public.g3_provider_payments') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0090: apply 0086 first';
    END IF;
    IF EXISTS (SELECT 1 FROM public.credit_lots) THEN
        RAISE EXCEPTION 'ABORT 0090: credit_lots holds row(s); this changes how they are valued';
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. THE EVIDENCE. One row per Balance Ledger entry, append-only.
--
--    Persisted rather than derived-on-read because the anchor is an accounting
--    figure: it has to remain reconstructable from what the provider actually
--    said, long after the API response is gone.
-- ---------------------------------------------------------------------
CREATE TABLE g3_provider_balance_ledger (
    id                   BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider             TEXT        NOT NULL,
    provider_payment_id  TEXT        NOT NULL,
    entry_id             TEXT        NOT NULL,
    event_type           TEXT        NOT NULL,
    amount_minor         BIGINT      NOT NULL,
    currency             TEXT        NOT NULL,
    raw_entry            JSONB       NOT NULL,
    ingested_at          TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    ingested_by          TEXT        NOT NULL DEFAULT session_user,
    CONSTRAINT g3_pbl_entry_unique UNIQUE (provider, entry_id),
    CONSTRAINT g3_pbl_currency_format CHECK (currency ~ '^[A-Z]{3}$'),
    CONSTRAINT g3_pbl_ids_nonblank
        CHECK (length(btrim(provider_payment_id)) > 0 AND length(btrim(entry_id)) > 0),
    -- Deliberately NOT an allow-list: an UNKNOWN event type must reach the
    -- derivation and STALL there, loudly. A CHECK would reject it at ingestion
    -- and lose the evidence that something unmodelled happened.
    CONSTRAINT g3_pbl_event_type_nonblank CHECK (length(btrim(event_type)) > 0)
);
CREATE INDEX g3_pbl_by_payment ON g3_provider_balance_ledger (provider, provider_payment_id);

COMMENT ON TABLE g3_provider_balance_ledger IS
  'Dodo Balance Ledger entries per payment (reference_object_id = provider_payment_id). The AUTHORITATIVE basis for the Decision-3A supplier-fee anchor. Append-only; settlement_amount is reconciliation evidence only and is not stored here as an anchor.';
COMMENT ON COLUMN g3_provider_balance_ledger.event_type IS
  'payment | payment_fees | tax | refund | dispute | anything else. NOT constrained to a set on purpose: an unmodelled type must survive ingestion and STALL the derivation, not be silently refused at the door.';

CREATE TRIGGER g3_pbl_no_mutation
    BEFORE UPDATE OR DELETE ON g3_provider_balance_ledger
    FOR EACH ROW EXECUTE FUNCTION public.l2c_reject_mutation();
CREATE TRIGGER g3_pbl_no_truncate
    BEFORE TRUNCATE ON g3_provider_balance_ledger
    FOR EACH STATEMENT EXECUTE FUNCTION public.l2c_reject_mutation();

-- ---------------------------------------------------------------------
-- 1b. THE EVIDENCE FENCE, and the snapshot digest.
--
--    🔴 LOCKING THE EXISTING LEDGER ROWS IS NOT ENOUGH. Row locks cannot stop
--    an INSERT of a NEW entry, and a late `tax` entry is exactly the case that
--    moves the anchor. So the fence is the PARENT ROW — `g3_provider_payments`
--    for that payment — taken `FOR UPDATE` by BOTH the birth path and the
--    ledger acquisition step. One row per payment, so two ingestions of the
--    same payment serialise and an ingestion cannot interleave with a birth.
--
--    The digest is defence in depth for the case the fence is NOT honoured by
--    some future writer: it pins the exact entry set the anchor was derived
--    from, and the write phase refuses if that set has moved. A fence you
--    cannot verify is a convention; a digest makes it checkable.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_ledger_digest(p_provider TEXT, p_ppid TEXT)
RETURNS TEXT
LANGUAGE sql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
    SELECT md5(coalesce(string_agg(
               l.entry_id || '|' || l.event_type || '|' || l.amount_minor::text
                          || '|' || l.currency, E'\n' ORDER BY l.entry_id), ''))
      FROM public.g3_provider_balance_ledger l
     WHERE l.provider = p_provider AND l.provider_payment_id = p_ppid;
$$;
COMMENT ON FUNCTION public.g3_ledger_digest IS
  'Digest of the FULL ledger entry set for one payment. Pins what the anchor was derived from, so a late INSERT between resolve and write is detectable — which row locks alone cannot do.';

CREATE OR REPLACE FUNCTION public.g3_fence_payment(p_provider TEXT, p_ppid TEXT)
RETURNS VOID
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE v_found BOOLEAN;
BEGIN
    SELECT TRUE INTO v_found FROM public.g3_provider_payments g
     WHERE g.provider = p_provider AND g.provider_payment_id = p_ppid
       FOR UPDATE;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'g3_fence_payment: no provider payment aggregate for % — nothing to fence', p_ppid
            USING ERRCODE = 'FX011';
    END IF;
END
$$;
CREATE OR REPLACE FUNCTION public.g3_fence_payments(p_requests JSONB)
RETURNS INT
LANGUAGE plpgsql
VOLATILE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE v_n INT;
BEGIN
    -- ALL of them, in ONE statement, ORDERED — the same discipline the FX locks
    -- use, for the same reason: two engines fencing the same pair of payments in
    -- opposite orders would deadlock. Per-request fencing in request order was
    -- exactly that hazard.
    WITH wanted AS (
        SELECT DISTINCT r->>'provider' AS provider, r->>'provider_payment_id' AS ppid
          FROM jsonb_array_elements(p_requests) r
         WHERE r->>'provider' IS NOT NULL AND r->>'provider_payment_id' IS NOT NULL
    ), locked AS (
        SELECT g.provider FROM public.g3_provider_payments g
          JOIN wanted w ON w.provider = g.provider AND w.ppid = g.provider_payment_id
         ORDER BY g.provider, g.provider_payment_id
           FOR UPDATE OF g
    )
    SELECT count(*) INTO v_n FROM locked;
    RETURN v_n;
END
$$;
COMMENT ON FUNCTION public.g3_fence_payments IS
  'Takes EVERY per-payment fence a batch needs, in ONE statement ordered by (provider, provider_payment_id), before anything is resolved. Fixed order; no new locking mechanism.';

COMMENT ON FUNCTION public.g3_fence_payment IS
  'THE per-payment fence. Both the birth path and the ledger acquisition step take it FOR UPDATE, so an ingestion cannot interleave with a birth. Row locks on the ledger entries themselves would not help: they cannot block an INSERT of a new entry.';

-- ---------------------------------------------------------------------
-- 2. THE DERIVATION.  Decision 3A, verbatim, with its admission rules.
--
--    Returns the anchor in minor units plus its currency, or RAISES. It never
--    returns a "best effort" number: an anchor that cannot be admitted is a
--    STALL, because the alternative is pricing a lot from evidence nobody
--    verified.
--
--    `FX009` — evidence not admissible (cardinality, currency, missing tax or
--              fee entry)
--    `FX010` — unknown event type present
-- ---------------------------------------------------------------------
-- 🔴 NO CALLER-SUPPLIED EVIDENCE. An earlier draft took
--    `p_zero_fee_evidenced BOOLEAN` from the request, so any caller could
--    assert its way past the `payment_fees` requirement — the admission rule
--    became advisory, and a test even enshrined the bypass. "Explicitly
--    evidenced" means the PROVIDER evidenced it, in persisted ledger rows. A
--    zero fee is therefore evidenced the only way a provider can evidence one:
--    a `payment_fees` entry that exists and says 0. If Dodo ever attests a zero
--    fee by some other mechanism, that mechanism must be PERSISTED and admitted
--    here — never asserted by the caller.
CREATE OR REPLACE FUNCTION public.g3_supplier_fee_anchor(
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_settlement_tax_minor BIGINT DEFAULT NULL
) RETURNS TABLE (anchor_minor BIGINT, currency TEXT)
LANGUAGE plpgsql
STABLE
PARALLEL UNSAFE
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_known     CONSTANT TEXT[] := ARRAY['payment','payment_fees','tax','refund','dispute'];
    v_unknown   TEXT;
    v_ccy_count INT;
    v_ccy       TEXT;
    v_n_payment INT;
    v_payment   BIGINT;
    v_fees      BIGINT;
    v_tax       BIGINT;
    v_n_fees    INT;
    v_n_tax     INT;
BEGIN
    -- NO LEDGER AT ALL ⇒ STALL-AWAITING-PROVIDER-LEDGER. Checked first of all.
    -- Absence of evidence is not evidence of simple terms: Decision 3A requires
    -- a stall and ZERO lots, not an `unknown` lot that quietly banks an
    -- unpriced balance and looks like a legitimate T64 outcome.
    IF NOT EXISTS (SELECT 1 FROM public.g3_provider_balance_ledger l
                    WHERE l.provider = p_provider
                      AND l.provider_payment_id = p_provider_payment_id) THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: STALL-AWAITING-PROVIDER-LEDGER — no Balance Ledger evidence '
            'for %. No lot is born.', p_provider_payment_id
            USING ERRCODE = 'FX011';
    END IF;

    -- Unknown event type ⇒ STALL. Checked next: everything below assumes the
    -- entry set is fully modelled, and a type nobody has classified could be a
    -- credit, a debit, or neither.
    SELECT string_agg(DISTINCT l.event_type, ', ') INTO v_unknown
      FROM public.g3_provider_balance_ledger l
     WHERE l.provider = p_provider AND l.provider_payment_id = p_provider_payment_id
       AND NOT (l.event_type = ANY (v_known));
    IF v_unknown IS NOT NULL THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: unknown ledger event type(s) [%] for % — STALL rather than '
            'guess whether they move the anchor', v_unknown, p_provider_payment_id
            USING ERRCODE = 'FX010';
    END IF;

    SELECT count(*) FILTER (WHERE l.event_type = 'payment'),
           count(*) FILTER (WHERE l.event_type = 'payment_fees'),
           count(*) FILTER (WHERE l.event_type = 'tax'),
           count(DISTINCT l.currency),
           min(l.currency),
           coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment'), 0),
           coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment_fees'), 0),
           coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'tax'), 0)
      INTO v_n_payment, v_n_fees, v_n_tax, v_ccy_count, v_ccy, v_payment, v_fees, v_tax
      FROM public.g3_provider_balance_ledger l
     WHERE l.provider = p_provider AND l.provider_payment_id = p_provider_payment_id;

    IF v_n_payment <> 1 THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: expected exactly ONE payment entry for %, found % — STALL',
            p_provider_payment_id, v_n_payment
            USING ERRCODE = 'FX009';
    END IF;
    IF v_ccy_count <> 1 THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: % settlement currencies for % — the anchor is one figure in '
            'one currency, so this STALLS', v_ccy_count, p_provider_payment_id
            USING ERRCODE = 'FX009';
    END IF;
    IF coalesce(p_settlement_tax_minor, 0) <> 0 AND v_n_tax = 0 THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: settlement_tax is % for % but the ledger carries no tax '
            'entry — evidence incomplete, STALL', p_settlement_tax_minor, p_provider_payment_id
            USING ERRCODE = 'FX009';
    END IF;
    IF v_n_fees = 0 THEN
        RAISE EXCEPTION
            'g3_supplier_fee_anchor: no payment_fees entry for %. A MISSING fee entry and a PROVEN '
            'zero fee are different facts, and assuming the first is the second is exactly how 1000 '
            'got stored where 905 belonged. A zero fee is evidenced by a payment_fees entry that '
            'says 0 — by the PROVIDER, never by the caller.', p_provider_payment_id
            USING ERRCODE = 'FX009';
    END IF;

    -- Decision 3A: signed payment credit − payment_fees − tax.
    RETURN QUERY SELECT (v_payment - v_fees - v_tax)::BIGINT, v_ccy;
END
$$;
COMMENT ON FUNCTION public.g3_supplier_fee_anchor IS
  'Decision 3A: Supplier Fee = signed payment credit - payment_fees - tax, from the Balance Ledger, admitted only under the four cardinality/evidence rules. Unknown event type STALLS (FX010); inadmissible evidence STALLS (FX009). It never returns a best-effort figure.';

-- ---------------------------------------------------------------------
-- 3. RECONCILIATION, recorded but never load-bearing.
--
--    `settlement_amount` stays as evidence. This view makes the gap visible —
--    it is where the 1000-vs-905 discrepancy would have shown up in review —
--    without ever letting it price anything.
-- ---------------------------------------------------------------------
CREATE OR REPLACE VIEW v_g3_supplier_fee_reconciliation AS
SELECT p.provider,
       p.provider_payment_id,
       p.supplier_fee_minor                             AS stored_supplier_fee_minor,
       coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment'), 0)      AS ledger_payment_minor,
       coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment_fees'), 0) AS ledger_fees_minor,
       coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'tax'), 0)          AS ledger_tax_minor,
       coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment'), 0)
         - coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment_fees'), 0)
         - coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'tax'), 0)      AS anchor_minor,
       p.supplier_fee_minor
         - (coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment'), 0)
            - coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'payment_fees'), 0)
            - coalesce(sum(l.amount_minor) FILTER (WHERE l.event_type = 'tax'), 0))  AS overstatement_minor
  FROM public.g3_provider_payments p
  LEFT JOIN public.g3_provider_balance_ledger l
    ON l.provider = p.provider AND l.provider_payment_id = p.provider_payment_id
 GROUP BY p.provider, p.provider_payment_id, p.supplier_fee_minor;

COMMENT ON VIEW v_g3_supplier_fee_reconciliation IS
  'settlement-derived stored value vs the Decision-3A ledger anchor. overstatement_minor is 95 for the measured fixture. Evidence only: nothing prices from this view.';

-- ---------------------------------------------------------------------
-- 4. THE BIRTH PATH NOW DERIVES THE ANCHOR — it no longer trusts the column.
--
--    `g3_resolve_request` previously read `g3_provider_payments.
--    supplier_fee_minor`, which `0086` populated from `settlement_amount`.
--    That is the defect Decision 3A names, so the column is no longer an input
--    to valuation at all: the resolver calls the derivation, and a payment with
--    no admissible ledger evidence STALLS instead of being priced.
--
--    Consequence, stated plainly: a lot can no longer be born at
--    `settlement_amount` while `payment_fees ≠ 0`, because that value is not
--    read. `MATRIX-046`'s new `V46` assertion is enforced by construction.
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
    IF v_provider IS NOT NULL AND v_ppid IS NOT NULL THEN
        SELECT g.payment_at, g.sales_channel, g.tax_owner, g.provider_tax_minor
          INTO v_acq, v_channel, v_owner, v_settle_tax
          FROM public.g3_provider_payments g
         WHERE g.provider = v_provider AND g.provider_payment_id = v_ppid;

        -- Decision 3A: the anchor comes from the LEDGER, never from
        -- g3_provider_payments.supplier_fee_minor (settlement-derived).
        IF v_source = 'topup' AND v_acq IS NOT NULL THEN
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

-- ---------------------------------------------------------------------
-- 5. PRIVILEGES + ownership for the definer that reads fx-adjacent evidence.
-- ---------------------------------------------------------------------
REVOKE ALL ON g3_provider_balance_ledger FROM PUBLIC, app_user, fx_rates_writer;
GRANT SELECT ON g3_provider_balance_ledger TO g3_posting_engine;
REVOKE ALL ON SEQUENCE g3_provider_balance_ledger_id_seq FROM PUBLIC, app_user, fx_rates_writer;
REVOKE ALL ON v_g3_supplier_fee_reconciliation FROM PUBLIC, app_user;
GRANT SELECT ON v_g3_supplier_fee_reconciliation TO g3_posting_engine;

REVOKE ALL ON FUNCTION public.g3_supplier_fee_anchor(TEXT,TEXT,BIGINT) FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_resolve_request(JSONB)                        FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_ledger_digest(TEXT,TEXT)                      FROM PUBLIC, app_user;
REVOKE ALL ON FUNCTION public.g3_fence_payment(TEXT,TEXT)                      FROM PUBLIC, app_user;

COMMIT;

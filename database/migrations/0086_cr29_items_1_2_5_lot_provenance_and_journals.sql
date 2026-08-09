-- =====================================================================
-- 0086_cr29_items_1_2_5_lot_provenance_and_journals.sql
--
-- L2C · CR-29 items 1 + 2 + 5, shipped as ONE vertical slice.
--
-- AUTHORISED BY: owner, 2026-08-09 — "Approve Decisions 1 and 2 exactly as
--   recommended. Record both as ANSWERED, then implement Items 1+2+5 as one
--   production-bound slice with full non-vacuous T79."
--
-- DECISION RECORDS (all three answered; no decision blocker remains):
--   D1 + D2  WIMBA_CONT_PROJECT/L2C-CR29-DECISIONS-1-2-ANSWERED-001.md
--   D3       WIMBA_CONT_PROJECT/L2C-CR29-DECISION-3-ANSWERED-001.md   <- AUTHORITATIVE.
--            It REJECTS the unblock request's recommended answer on four points.
--
-- ─────────────────────────────────────────────────────────────────────
-- DEPLOY ORDER
--
--   PREFERRED: **SCHEMA FIRST, THEN SOURCE.** Same direction as 0085, the
--   REVERSE of 0084. Safe here for a reason that was verified, not assumed:
--   every object below is NEW, and the one NOT NULL column added without a
--   default (credit_lots.acquired_at) sits on a table with ZERO writers --
--   `INSERT INTO credit_lots` occurs in no .mjs, .js, .py or .sql file in this
--   repository, and the table holds 0 rows in production. Old code therefore
--   cannot meet a constraint it never touches. Guard G0 below aborts the
--   migration if either fact has changed.
--
--   SOURCE FIRST is **also safe, for this release only**, and only while
--   G3_LOT_WRITER_ENABLED is absent or false: recordTopupLot returns at the flag
--   check BEFORE every database access, and it is the only backend/g3_lots.mjs
--   export wired into a production path. Deployed code with the flag off
--   therefore never touches an object created here, so the schema may land
--   afterwards.
--
--   🔴 NEVER enable G3_LOT_WRITER_ENABLED before migration 0086 has applied AND
--   production schema verification has passed. That flag is the entire reason
--   the source-first order is safe; flipping it early removes the protection
--   retroactively.
--
--   Do NOT generalise either order to the next migration. 0084 was source-first
--   and 0085 schema-first for unrelated reasons; the order is a per-migration
--   argument about what old code can reach, never a house style.
-- ─────────────────────────────────────────────────────────────────────
--
-- WHAT THIS MIGRATION DOES NOT DO -- deliberately, and each for a recorded reason:
--
--   * It does NOT activate the top-up checkout gate. 0085 shipped it INACTIVE in
--     shadow; nothing here changes g3_topup_gate_activation.
--   * It does NOT record checkout intent inside checkout creation (freeze-scope
--     amendment iii, still UNAUTHORISED -- that is CR-29 item 6).
--   * It does NOT enable breakage. Decision 3 A7 disables it: no expiry-driven
--     revenue, no age-based release. Account 4600 stays unused by construction,
--     and E5 is not wired. credit_lots.expires_at keeps its value and drives
--     NOTHING here.
--   * It does NOT re-price, reclassify or backfill any existing credit. CR-29
--     clause 6 forbids it and there is nothing to backfill (0 lots).
--   * It does NOT run the opening census. Per Decision 2 that census cannot run
--     until every censused lot carries an attested date; the class is supported
--     here, no rows are written.
--
-- FREEZE-SCOPE NOTE, stated plainly because it is an amendment:
--   PLAN-045 G3.1-h authorised amendment (i) -- the provider-global receipt
--   aggregate and its write -- and left (ii) the atomic release transaction
--   UNAUTHORISED. MATRIX-045 T79 requires path independence and declares a
--   single-path test VACUOUS. A full non-vacuous T79 is therefore impossible
--   without (ii). The owner's 2026-08-09 instruction to implement items 1+2+5
--   "with full non-vacuous T79" is what authorises (ii); it is implemented here
--   and nowhere wider. (iii) remains untouched.
-- =====================================================================

BEGIN;

-- =====================================================================
-- G0. PRECONDITION GUARDS -- abort rather than corrupt.
-- =====================================================================
DO $guard$
DECLARE
    n_lots BIGINT;
BEGIN
    SELECT count(*) INTO n_lots FROM credit_lots;
    IF n_lots <> 0 THEN
        RAISE EXCEPTION
            'ABORT 0086/G0: credit_lots holds % row(s); acquired_at NOT NULL without a default '
            'would fail or silently need a fabricated value. Decision 2 forbids inferring one. '
            'Resolve the rows first.', n_lots;
    END IF;

    IF to_regclass('public.g3_provider_payments') IS NOT NULL THEN
        RAISE EXCEPTION 'ABORT 0086/G0: g3_provider_payments already exists; refusing to redefine it.';
    END IF;

    -- 0085 must be in place: this slice reads tos_versions and the gate activation log.
    IF to_regclass('public.tos_versions') IS NULL
       OR to_regclass('public.g3_topup_gate_activation') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0086/G0: migration 0085 objects are absent; apply 0085 first.';
    END IF;
END
$guard$;

-- =====================================================================
-- 1. ITEM 1 -- g3_provider_payments: the provider-global payment aggregate
--    and the temporary CR-29 business timestamp.
--
--    IDENTITY BOUNDARY (T79 asserts this separately and non-vacuously):
--      receipt identity = (provider, webhook_id)      -- per DELIVERY
--      payment identity = (provider, provider_payment_id)  -- per PAYMENT
--    This table is keyed by PAYMENT identity. webhook_id must NEVER substitute
--    for provider_payment_id here; it is recorded only as pinning provenance.
-- =====================================================================
CREATE TABLE g3_provider_payments (
    provider              TEXT        NOT NULL,
    provider_payment_id   TEXT        NOT NULL,

    -- The CR-29 temporary business timestamp. NULL until the FIRST
    -- signature-valid payment.succeeded pins it from payload.data.created_at.
    -- Legitimately NULL when the aggregate is born from a refund/dispute that
    -- arrives BEFORE any payment.succeeded -- a correct intermediate state.
    payment_at            TIMESTAMPTZ,

    -- Pinning provenance. Audit only: never a source for payment_at, and never
    -- consulted by any consumer.
    payment_at_pinned_by  TEXT,          -- webhook_id of the pinning delivery
    payment_at_pinned_at  TIMESTAMPTZ,   -- when the pin happened (clock, audit only)

    -- Decision 3 B2: the SUPPLIER FEE is the cash-in anchor -- what Dodo pays
    -- Wimba, net of the customer's tax, Dodo's discount and other deductions.
    -- It is NOT the customer's gross checkout. NULL = not established, which
    -- makes the lot unpriced; it is never inferred from total_amount.
    supplier_fee_minor    BIGINT,
    supplier_fee_currency TEXT,

    -- Decision 3 A6: who owns the customer-facing tax, and through which channel
    -- the sale was made. Both are REQUIRED before a lot can be priced -- the
    -- writer fails closed when either is unknown, rather than defaulting.
    sales_channel         TEXT        CHECK (sales_channel IN ('dodo_mor','wimba_direct')),
    tax_owner             TEXT        CHECK (tax_owner    IN ('provider','wimba')),

    -- Provider-reported tax. Decision 3 A6: for the Dodo MoR channel this is
    -- PROVIDER INFORMATION ONLY and is never a Wimba VAT liability.
    provider_tax_minor    BIGINT,

    created_at            TIMESTAMPTZ NOT NULL DEFAULT now(),

    PRIMARY KEY (provider, provider_payment_id),

    -- A pin is all-or-nothing: the value and both provenance fields move together.
    CONSTRAINT g3_pp_pin_coherent CHECK (
        (payment_at IS NULL     AND payment_at_pinned_by IS NULL     AND payment_at_pinned_at IS NULL)
     OR (payment_at IS NOT NULL AND payment_at_pinned_by IS NOT NULL AND payment_at_pinned_at IS NOT NULL)
    ),
    -- A supplier fee without its currency is not a monetary amount.
    CONSTRAINT g3_pp_fee_coherent CHECK (
        (supplier_fee_minor IS NULL     AND supplier_fee_currency IS NULL)
     OR (supplier_fee_minor IS NOT NULL AND supplier_fee_currency IS NOT NULL)
    ),
    -- Decision 3 A6 is a rule about the PAIR, so it is enforced on the pair:
    -- the Dodo MoR channel always has provider tax ownership; a wimba_direct
    -- sale is always Wimba's own. A row asserting otherwise is a defect.
    CONSTRAINT g3_pp_channel_tax_owner_coherent CHECK (
        sales_channel IS NULL OR tax_owner IS NULL
     OR (sales_channel = 'dodo_mor'     AND tax_owner = 'provider')
     OR (sales_channel = 'wimba_direct' AND tax_owner = 'wimba')
    )
);

COMMENT ON TABLE g3_provider_payments IS
  'CR-29 provider-global payment aggregate, keyed by PAYMENT identity (provider, provider_payment_id) -- never by receipt identity (provider, webhook_id). Carries the temporary business timestamp payment_at and the Decision 3 supplier-fee anchor.';
COMMENT ON COLUMN g3_provider_payments.payment_at IS
  'CR-29 temporary business timestamp: payload.data.created_at from the FIRST signature-valid payment.succeeded. Exactly one NULL->value transition, immutable thereafter (trigger g3_pp_payment_at_immutable_trg). Sole source for BOTH credit_lots.acquired_at AND the FX rate_date -- ordinary grant and quarantine->release read this one value (precondition A). NEVER the envelope timestamp, NEVER the webhook-timestamp header, NEVER now().';
COMMENT ON COLUMN g3_provider_payments.supplier_fee_minor IS
  'Decision 3 A1/B2: what the provider pays Wimba, net of customer tax, provider discount and other deductions. The cash-in anchor and the lot valuation base. NOT the customer gross. NULL => lot is written unpriced; never inferred.';

-- ── payment_at immutability: exactly one transition, NULL -> value ────────────
-- T79 asserts this against BOTH a same-webhook_id redelivery and a DIFFERENT
-- webhook_id for the same provider_payment_id. Neither may overwrite.
CREATE OR REPLACE FUNCTION public.g3_pp_payment_at_immutable() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF OLD.payment_at IS NOT NULL AND NEW.payment_at IS DISTINCT FROM OLD.payment_at THEN
        RAISE EXCEPTION
            'g3_provider_payments.payment_at is immutable once pinned (provider=%, provider_payment_id=%): '
            'attempted % -> %. A divergent redelivery is ALERT-ONLY; it must never overwrite.',
            OLD.provider, OLD.provider_payment_id, OLD.payment_at, NEW.payment_at
            USING ERRCODE = 'raise_exception';
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER g3_pp_payment_at_immutable_trg
    BEFORE UPDATE ON g3_provider_payments
    FOR EACH ROW EXECUTE FUNCTION public.g3_pp_payment_at_immutable();

-- ── Divergence alerts: audit only, never a gate, never a quarantine ───────────
CREATE TABLE g3_payment_at_divergence_alerts (
    id                    BIGINT      GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    provider              TEXT        NOT NULL,
    provider_payment_id   TEXT        NOT NULL,
    pinned_payment_at     TIMESTAMPTZ NOT NULL,
    observed_payment_at   TIMESTAMPTZ NOT NULL,
    observed_webhook_id   TEXT,
    observed_at           TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp()
);

COMMENT ON TABLE g3_payment_at_divergence_alerts IS
  'Append-only audit of redeliveries whose data.created_at differs from the pinned payment_at. ALERT ONLY: it must never overwrite the pin and must never quarantine the payment (MATRIX-045 T79). payload_digest is likewise audit-only and must never gate.';

CREATE INDEX g3_payment_at_divergence_alerts_payment
    ON g3_payment_at_divergence_alerts (provider, provider_payment_id, observed_at DESC);

-- Append-only, reusing 0085's rejector.
CREATE TRIGGER g3_payment_at_divergence_alerts_append_only
    BEFORE UPDATE OR DELETE ON g3_payment_at_divergence_alerts
    FOR EACH ROW EXECUTE FUNCTION public.l2c_reject_mutation();

-- =====================================================================
-- 2. THE CR-29 WORKAROUND WINDOW -- Decision 1's accepted consequence.
--
--    Decision 1 makes grandfather_reason answer "WHY is this lot protected",
--    not "was it born during the workaround": a census or activation-inflight
--    lot born inside the window keeps its more specific reason. The reason
--    column is therefore NOT a complete cohort marker, and cohort membership
--    moves here -- "granted_at inside the window".
--
--    One row. started_at is set now, because this migration is the moment the
--    writer that applies blanket grandfathering ships. That is a POLICY event
--    timestamp and is unrelated to Decision 2's forbidden now() fallback, which
--    governs a LOT's acquired_at only.
-- =====================================================================
CREATE TABLE g3_cr29_workaround_window (
    id          SMALLINT    PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    started_at  TIMESTAMPTZ NOT NULL,
    ended_at    TIMESTAMPTZ,
    note        TEXT,
    CONSTRAINT g3_cr29_window_order CHECK (ended_at IS NULL OR ended_at > started_at)
);

COMMENT ON TABLE g3_cr29_workaround_window IS
  'CR-29 temporary-workaround active window. Cohort membership is granted_at within [started_at, ended_at) -- NOT grandfather_reason, which Decision 1 made a why-marker rather than a cohort marker. Singleton; started_at immutable; ended_at may be set exactly once.';

CREATE OR REPLACE FUNCTION public.g3_cr29_window_guard() RETURNS TRIGGER
LANGUAGE plpgsql AS $$
BEGIN
    IF TG_OP = 'DELETE' THEN
        RAISE EXCEPTION 'g3_cr29_workaround_window is not deletable';
    END IF;
    IF NEW.started_at IS DISTINCT FROM OLD.started_at THEN
        RAISE EXCEPTION 'g3_cr29_workaround_window.started_at is immutable';
    END IF;
    IF OLD.ended_at IS NOT NULL AND NEW.ended_at IS DISTINCT FROM OLD.ended_at THEN
        RAISE EXCEPTION 'g3_cr29_workaround_window.ended_at may be set exactly once (already %)', OLD.ended_at;
    END IF;
    RETURN NEW;
END
$$;

CREATE TRIGGER g3_cr29_window_guard_trg
    BEFORE UPDATE OR DELETE ON g3_cr29_workaround_window
    FOR EACH ROW EXECUTE FUNCTION public.g3_cr29_window_guard();

INSERT INTO g3_cr29_workaround_window (id, started_at, note)
VALUES (1, now(),
        'Opened by migration 0086 -- the moment the blanket-grandfathering lot writer ships. '
        'Decision 1 (L2C-CR29-DECISIONS-1-2-ANSWERED-001.md): cohort = granted_at within this window.');

-- =====================================================================
-- 3. ITEM 2 -- credit_lots provenance columns and the four-class truth table.
--
--    Truth table (Decision 1, APPROVED AS RECOMMENDED):
--      opening census                       true  / 'opening_census'
--      activation inflight                  true  / 'activation_inflight'
--      ordinary grant, workaround ACTIVE    true  / 'cr29_temp_workaround'
--      quarantine release                   INHERITS the payment's class -- never its own
--      ordinary grant, workaround ENDED     false / NULL
--
--    Precedence: opening_census > activation_inflight > cr29_temp_workaround > pre_cutover
--
--    Quarantine release is NOT a class. It is a second delivery path for a lot
--    whose class is already fixed by its payment -- which is exactly what makes
--    precondition A checkable rather than aspirational.
-- =====================================================================
ALTER TABLE credit_lots
    -- Decision 2: NO DEFAULT. A writer that omits it must fail loudly rather
    -- than silently stamping now(). Safe as NOT NULL: 0 rows, 0 writers (G0).
    ADD COLUMN acquired_at        TIMESTAMPTZ NOT NULL,
    ADD COLUMN is_grandfathered   BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN grandfather_reason TEXT,
    ADD COLUMN provenance_kind    TEXT        NOT NULL DEFAULT 'grant',
    -- Fail-closed valuation state, in 0071's spirit: silent wrongness becomes
    -- loud incompleteness. An unpriced lot is NOT a zero-priced lot.
    ADD COLUMN is_priced          BOOLEAN     NOT NULL DEFAULT false,
    ADD COLUMN unpriced_reason    TEXT,
    -- The FX rate_date and acquired_at ALWAYS derive from the same value for a
    -- given lot; they never diverge by path (Decision 2).
    ADD COLUMN rate_date          DATE;

ALTER TABLE credit_lots
    ADD CONSTRAINT credit_lots_grandfather_topup_only
        CHECK (is_grandfathered = false OR source = 'topup'),
    ADD CONSTRAINT credit_lots_grandfather_reason_paired
        CHECK ((is_grandfathered AND grandfather_reason IS NOT NULL)
            OR (NOT is_grandfathered AND grandfather_reason IS NULL)),
    ADD CONSTRAINT credit_lots_grandfather_reason_domain
        CHECK (grandfather_reason IS NULL OR grandfather_reason IN
               ('pre_cutover','opening_census','activation_inflight','cr29_temp_workaround')),
    ADD CONSTRAINT credit_lots_provenance_kind_domain
        CHECK (provenance_kind IN ('grant','opening_census')),
    -- Priced and unpriced are mutually exclusive states, both explicit.
    ADD CONSTRAINT credit_lots_priced_paired
        CHECK ((is_priced AND unpriced_reason IS NULL)
            OR (NOT is_priced AND unpriced_reason IS NOT NULL)),
    -- An unpriced lot may not carry a valuation. This is what stops a
    -- fail-closed lot from quietly recognising revenue later.
    ADD CONSTRAINT credit_lots_unpriced_has_no_value
        CHECK (is_priced OR (dpp_total_idr = 0 AND price_per_credit_idr = 0 AND recognized_idr = 0));

COMMENT ON COLUMN credit_lots.acquired_at IS
  'Decision 2: for provider-paid lots, g3_provider_payments.payment_at -- read, never recomputed, by BOTH the ordinary-grant and quarantine->release paths. For opening_census, the artifact-attested date. For activation_inflight, the positive artifact timestamp. NO fallback exists: not now(), not clock_timestamp(), not insert time, not released_at, not the envelope timestamp, not the webhook-timestamp header.';
COMMENT ON COLUMN credit_lots.grandfather_reason IS
  'WHY this lot is protected, not WHEN it was born. Decision 1 precedence: opening_census > activation_inflight > cr29_temp_workaround > pre_cutover. Permanent (CR-29 clause 6, no reclassification). Cohort membership lives in g3_cr29_workaround_window, NOT here.';
COMMENT ON COLUMN credit_lots.is_priced IS
  'FALSE = fail-closed: the valuation source was absent, invalid or unestablished, so no value was inferred (0071 discipline). An unpriced lot carries zero valuation by CHECK and must be reported as incomplete, never as zero-value.';

-- The G3.5-b relief key: the two-group (grandfathered-first) order becomes an
-- index scan rather than a sort.
CREATE INDEX credit_lots_relief_order
    ON credit_lots (tenant_id, is_grandfathered, granted_at, lot_seq)
    WHERE credits_remaining > 0;

-- =====================================================================
-- 4. ITEM 2 (second path) -- top-up quarantine and its release.
--
--    A checkout created at/after the gate activation instant must carry both
--    tos_version and acceptance_event_id. If either is absent, or does not
--    resolve to a matching accepted tos_acceptances row, the payment is
--    quarantined: no lot is written, no version is guessed, an alert is raised,
--    and THE PAYMENT IS NEVER FORFEITED -- the money and the grant stand; the
--    gap awaits a per-row owner/ops disposition.
--
--    The gate ships INACTIVE (0085), so nothing can be quarantined in
--    production today. The path exists because T79's path-independence
--    assertion is vacuous without it.
-- =====================================================================
CREATE TABLE g3_topup_quarantine (
    id                    UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    provider              TEXT        NOT NULL,
    provider_payment_id   TEXT        NOT NULL,
    tenant_id             UUID,
    reason                TEXT        NOT NULL,
    metadata_as_received  JSONB       NOT NULL DEFAULT '{}'::jsonb,
    quarantined_at        TIMESTAMPTZ NOT NULL DEFAULT clock_timestamp(),
    released_at           TIMESTAMPTZ,
    released_by           TEXT,
    release_disposition   TEXT,
    released_lot_id       UUID        REFERENCES credit_lots(id),
    CONSTRAINT g3_quarantine_release_coherent CHECK (
        (released_at IS NULL     AND released_by IS NULL     AND release_disposition IS NULL)
     OR (released_at IS NOT NULL AND released_by IS NOT NULL AND release_disposition IS NOT NULL)
    ),
    FOREIGN KEY (provider, provider_payment_id)
        REFERENCES g3_provider_payments (provider, provider_payment_id)
);

-- At most one OPEN quarantine per payment.
CREATE UNIQUE INDEX g3_topup_quarantine_one_open
    ON g3_topup_quarantine (provider, provider_payment_id)
    WHERE released_at IS NULL;

COMMENT ON TABLE g3_topup_quarantine IS
  'Post-boundary top-up payments whose ToS metadata was absent or unresolvable. No lot is written and no version is guessed; the payment is NEVER forfeited. Downstream completeness signals must report a quarantined payment as incomplete indefinitely, not silently self-resolve. Release is a manual per-row disposition (g3_release_quarantined_topup).';

-- =====================================================================
-- 5. ITEM 1 (writer) -- pin payment_at.
--
--    Contract, each clause asserted separately by T79:
--      * source is payload.data.created_at from the FIRST signature-valid
--        payment.succeeded, taken BYTE-FOR-BYTE (the caller passes the raw text;
--        this function does the one strict parse and nothing else);
--      * exactly one NULL -> value transition; immutable thereafter;
--      * a redelivery with the SAME value is a no-op;
--      * a redelivery with a DIFFERENT value raises an ALERT and nothing more --
--        never an overwrite, never a quarantine;
--      * absent / null / empty / unparseable => payment_at stays NULL, the row
--        still exists (so a refund-first aggregate is representable), and NO LOT
--        may be written. There is no fallback of any kind.
-- =====================================================================
CREATE OR REPLACE FUNCTION public.g3_pin_payment_at(
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_created_at_raw      TEXT,          -- payload.data.created_at, verbatim
    p_webhook_id          TEXT
) RETURNS TABLE (payment_at TIMESTAMPTZ, pinned BOOLEAN, outcome TEXT)
LANGUAGE plpgsql AS $$
DECLARE
    v_parsed  TIMESTAMPTZ;
    v_current TIMESTAMPTZ;
BEGIN
    IF p_provider IS NULL OR p_provider_payment_id IS NULL
       OR btrim(p_provider) = '' OR btrim(p_provider_payment_id) = '' THEN
        RAISE EXCEPTION 'g3_pin_payment_at: payment identity is required (provider=%, provider_payment_id=%)',
            p_provider, p_provider_payment_id;
    END IF;

    -- ONE strict parse. No coercion ladder, no locale guessing, no fallback.
    IF p_created_at_raw IS NULL OR btrim(p_created_at_raw) = '' THEN
        v_parsed := NULL;
    ELSE
        BEGIN
            v_parsed := p_created_at_raw::timestamptz;
        EXCEPTION WHEN others THEN
            v_parsed := NULL;
        END;
    END IF;

    -- The aggregate always exists after this call, pinned or not: a refund or
    -- dispute may legitimately create it before any payment.succeeded.
    INSERT INTO g3_provider_payments (provider, provider_payment_id)
    VALUES (p_provider, p_provider_payment_id)
    ON CONFLICT (provider, provider_payment_id) DO NOTHING;

    SELECT g.payment_at INTO v_current
      FROM g3_provider_payments g
     WHERE g.provider = p_provider AND g.provider_payment_id = p_provider_payment_id
     FOR UPDATE;

    IF v_parsed IS NULL THEN
        -- FAIL CLOSED. Stays NULL; caller must write no lot.
        RETURN QUERY SELECT v_current, false, 'fail_closed_invalid_created_at'::TEXT;
        RETURN;
    END IF;

    IF v_current IS NULL THEN
        UPDATE g3_provider_payments
           SET payment_at           = v_parsed,
               payment_at_pinned_by = p_webhook_id,
               payment_at_pinned_at = clock_timestamp()
         WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id;
        RETURN QUERY SELECT v_parsed, true, 'pinned'::TEXT;
        RETURN;
    END IF;

    IF v_current = v_parsed THEN
        RETURN QUERY SELECT v_current, false, 'already_pinned_identical'::TEXT;
        RETURN;
    END IF;

    -- Divergent redelivery: ALERT ONLY. No overwrite. No quarantine.
    INSERT INTO g3_payment_at_divergence_alerts
        (provider, provider_payment_id, pinned_payment_at, observed_payment_at, observed_webhook_id)
    VALUES (p_provider, p_provider_payment_id, v_current, v_parsed, p_webhook_id);

    RETURN QUERY SELECT v_current, false, 'divergent_alert_only'::TEXT;
END
$$;

COMMENT ON FUNCTION public.g3_pin_payment_at(TEXT,TEXT,TEXT,TEXT) IS
  'CR-29 payment_at pinning writer. One NULL->value transition from the first signature-valid payment.succeeded''s data.created_at; immutable after. Divergent redelivery is alert-only. Invalid/absent input fails closed to NULL with no fallback (MATRIX-045 T79).';

-- =====================================================================
-- 6. ITEM 1 + 2 -- record the payment's commercial terms.
--
--    Decision 3 A6 fail-closed: sales_channel and tax_owner must both be known
--    and coherent before a lot can be priced. Decision 3 A1/B2: the anchor is
--    the SUPPLIER FEE, never the customer gross.
-- =====================================================================
CREATE OR REPLACE FUNCTION public.g3_record_payment_terms(
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_sales_channel       TEXT,
    p_tax_owner           TEXT,
    p_supplier_fee_minor  BIGINT,
    p_supplier_fee_ccy    TEXT,
    p_provider_tax_minor  BIGINT DEFAULT NULL
) RETURNS VOID
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO g3_provider_payments (provider, provider_payment_id)
    VALUES (p_provider, p_provider_payment_id)
    ON CONFLICT (provider, provider_payment_id) DO NOTHING;

    -- Terms are write-once in the same spirit as the pin: COALESCE keeps the
    -- first established value and a later delivery cannot silently restate it.
    UPDATE g3_provider_payments g
       SET sales_channel         = COALESCE(g.sales_channel, p_sales_channel),
           tax_owner             = COALESCE(g.tax_owner, p_tax_owner),
           supplier_fee_minor    = COALESCE(g.supplier_fee_minor, p_supplier_fee_minor),
           supplier_fee_currency = COALESCE(g.supplier_fee_currency, p_supplier_fee_ccy),
           provider_tax_minor    = COALESCE(g.provider_tax_minor, p_provider_tax_minor)
     WHERE g.provider = p_provider AND g.provider_payment_id = p_provider_payment_id;
END
$$;

-- =====================================================================
-- 7. ITEM 2 -- g3_write_lot: the ONE lot writer, used by BOTH paths.
--
--    Precondition A lives here: the ordinary-grant path and the
--    quarantine->release path both call this function, which READS
--    g3_provider_payments.payment_at. Neither path recomputes it, so
--    acquired_at and rate_date cannot diverge between them. A design in which
--    they differ is a defect, not a variant.
-- =====================================================================
CREATE OR REPLACE FUNCTION public.g3_write_lot(
    p_tenant              UUID,
    p_source              TEXT,
    p_credits             BIGINT,
    p_ledger_op_id        TEXT,
    p_provenance_kind     TEXT     DEFAULT 'grant',
    p_provider            TEXT     DEFAULT NULL,
    p_provider_payment_id TEXT     DEFAULT NULL,
    p_attested_at         TIMESTAMPTZ DEFAULT NULL   -- census / inflight classes only
) RETURNS TABLE (
    lot_id       UUID,
    acquired_at  TIMESTAMPTZ,
    rate_date    DATE,
    is_priced    BOOLEAN,
    grandfathered BOOLEAN,
    reason       TEXT
)
LANGUAGE plpgsql AS $$
DECLARE
    v_acquired    TIMESTAMPTZ;
    v_reason      TEXT;
    v_gf          BOOLEAN;
    v_fee         BIGINT;
    v_ccy         TEXT;
    v_channel     TEXT;
    v_owner       TEXT;
    v_priced      BOOLEAN := false;
    v_unpriced    TEXT;
    v_dpp         NUMERIC(18,2) := 0;
    v_ppc         NUMERIC(14,4) := 0;
    v_inflight    BOOLEAN := false;
    v_win_start   TIMESTAMPTZ;
    v_win_end     TIMESTAMPTZ;
    v_lot         UUID;
    v_existing    RECORD;
BEGIN
    IF p_tenant IS NULL OR p_source IS NULL OR p_ledger_op_id IS NULL THEN
        RAISE EXCEPTION 'g3_write_lot: tenant, source and ledger_op_id are required';
    END IF;
    IF NOT (p_credits > 0) THEN
        RAISE EXCEPTION 'g3_write_lot: credits must be > 0 (got %)', p_credits;
    END IF;

    SELECT started_at, ended_at INTO v_win_start, v_win_end
      FROM g3_cr29_workaround_window WHERE id = 1;

    -- ── Decision 2: acquired_at, three sources, strict order, NO fallback ─────
    IF p_provenance_kind = 'opening_census' THEN
        v_acquired := p_attested_at;                       -- artifact-attested date
    ELSIF p_provider IS NOT NULL AND p_provider_payment_id IS NOT NULL THEN
        SELECT g.payment_at, g.supplier_fee_minor, g.supplier_fee_currency,
               g.sales_channel, g.tax_owner
          INTO v_acquired, v_fee, v_ccy, v_channel, v_owner
          FROM g3_provider_payments g
         WHERE g.provider = p_provider AND g.provider_payment_id = p_provider_payment_id;
    ELSE
        -- Activation-inflight: the timestamp on the positive artifact.
        v_acquired := p_attested_at;
        v_inflight := p_attested_at IS NOT NULL;
    END IF;

    -- FAIL CLOSED. Decision 2: no lot is written when the source is absent.
    IF v_acquired IS NULL THEN
        RAISE EXCEPTION
            'g3_write_lot: fail-closed, no authoritative acquired_at for source=% provenance=% payment=%/%. '
            'Decision 2 forbids every fallback (now, clock_timestamp, insert time, released_at, '
            'envelope timestamp, webhook-timestamp header). No lot written.',
            p_source, p_provenance_kind, p_provider, p_provider_payment_id
            USING ERRCODE = 'raise_exception';
    END IF;

    -- ── Decision 1: the four-class truth table, in precedence order ───────────
    --    opening_census > activation_inflight > cr29_temp_workaround > pre_cutover
    IF p_source <> 'topup' THEN
        -- Non-topup is never grandfathered (CHECK enforces it too).
        v_gf := false; v_reason := NULL;
    ELSIF p_provenance_kind = 'opening_census' THEN
        v_gf := true;  v_reason := 'opening_census';
    ELSIF v_inflight THEN
        v_gf := true;  v_reason := 'activation_inflight';
    ELSIF v_win_start IS NOT NULL AND v_win_end IS NULL THEN
        -- Workaround ACTIVE: blanket grandfathering of ordinary top-up grants.
        -- No acquired_at-vs-cutover comparison is performed while it is active,
        -- which is precisely why the outstanding Dodo answers do not gate this.
        v_gf := true;  v_reason := 'cr29_temp_workaround';
    ELSE
        -- Workaround ENDED: fall through to the cutover comparison.
        SELECT (v_acquired < t.effective_at) INTO v_gf
          FROM tos_versions t WHERE t.is_lifecycle_cutover LIMIT 1;
        v_gf := COALESCE(v_gf, false);
        v_reason := CASE WHEN v_gf THEN 'pre_cutover' ELSE NULL END;
    END IF;

    -- ── Decision 3 B2 + A6: valuation from the SUPPLIER FEE, fail-closed ──────
    IF p_source <> 'topup' THEN
        v_priced := false; v_unpriced := 'not_a_paid_lot';     -- free/grant lots: E3, no revenue
    ELSIF v_channel IS NULL OR v_owner IS NULL THEN
        v_priced := false; v_unpriced := 'unknown_channel_or_tax_owner';
    ELSIF v_fee IS NULL OR v_ccy IS NULL THEN
        v_priced := false; v_unpriced := 'supplier_fee_not_established';
    ELSIF v_ccy <> 'IDR' THEN
        -- Cross-currency valuation needs an FX rate at rate_date; not established
        -- here, and Decision 2 forbids inventing one.
        v_priced := false; v_unpriced := 'supplier_fee_currency_not_idr';
    ELSE
        v_priced := true;  v_unpriced := NULL;
        v_dpp := (v_fee)::NUMERIC(18,2);
        -- Decision 3 A4: carrying value = allocated price / ALL credits issued
        -- in the lot, bonus included. p_credits IS that full issued count.
        v_ppc := ROUND(v_dpp / p_credits, 4);
    END IF;

    -- ── Birth-immutable write, idempotent on (tenant, ledger_op_id) ───────────
    SELECT * INTO v_existing FROM credit_lots
     WHERE tenant_id = p_tenant AND ledger_op_id = p_ledger_op_id;

    IF FOUND THEN
        -- Read back and compare; a replay that would flip a birth field ABORTS
        -- rather than absorbing the difference.
        IF v_existing.acquired_at        IS DISTINCT FROM v_acquired
        OR v_existing.is_grandfathered   IS DISTINCT FROM v_gf
        OR v_existing.grandfather_reason IS DISTINCT FROM v_reason
        OR v_existing.provenance_kind    IS DISTINCT FROM p_provenance_kind THEN
            RAISE EXCEPTION
                'g3_write_lot: birth-immutable mismatch on replay for op_id=% (acquired_at %/%s, '
                'is_grandfathered %/%, reason %/%, provenance %/%)',
                p_ledger_op_id, v_existing.acquired_at, v_acquired,
                v_existing.is_grandfathered, v_gf,
                v_existing.grandfather_reason, v_reason,
                v_existing.provenance_kind, p_provenance_kind;
        END IF;
        RETURN QUERY SELECT v_existing.id, v_existing.acquired_at, v_existing.rate_date,
                            v_existing.is_priced, v_existing.is_grandfathered,
                            v_existing.grandfather_reason;
        RETURN;
    END IF;

    INSERT INTO credit_lots (
        tenant_id, source, is_paid, credits_granted, credits_remaining,
        price_per_credit_idr, dpp_total_idr, recognized_idr,
        ledger_op_id, granted_at,
        acquired_at, is_grandfathered, grandfather_reason, provenance_kind,
        is_priced, unpriced_reason, rate_date
    ) VALUES (
        p_tenant, p_source, (p_source = 'topup'), p_credits, p_credits,
        v_ppc, v_dpp, 0,
        p_ledger_op_id, v_acquired,
        v_acquired, v_gf, v_reason, p_provenance_kind,
        v_priced, v_unpriced, (v_acquired AT TIME ZONE 'UTC')::date
    )
    RETURNING id INTO v_lot;

    RETURN QUERY SELECT v_lot, v_acquired, (v_acquired AT TIME ZONE 'UTC')::date,
                        v_priced, v_gf, v_reason;
END
$$;

COMMENT ON FUNCTION public.g3_write_lot IS
  'The ONE credit-lot writer. Both the ordinary-grant path and the quarantine->release path call it, and it READS g3_provider_payments.payment_at rather than recomputing -- which is what makes precondition A (identical acquired_at and rate_date across paths) structurally true instead of aspirational. Fails closed with no lot when the authoritative timestamp is absent.';

-- =====================================================================
-- 8. ITEM 2 (second path) -- release a quarantined top-up.
--
--    Writes the lot the ordinary path would have written, from the SAME stored
--    payment_at. It does NOT re-read the webhook, does NOT recompute the
--    timestamp, and does NOT get its own provenance class: per Decision 1 the
--    released lot inherits the class its payment already fixed.
-- =====================================================================
CREATE OR REPLACE FUNCTION public.g3_release_quarantined_topup(
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_tenant              UUID,
    p_credits             BIGINT,
    p_ledger_op_id        TEXT,
    p_released_by         TEXT,
    p_disposition         TEXT
) RETURNS TABLE (
    lot_id       UUID,
    acquired_at  TIMESTAMPTZ,
    rate_date    DATE,
    is_priced    BOOLEAN,
    grandfathered BOOLEAN,
    reason       TEXT
)
LANGUAGE plpgsql AS $$
DECLARE
    v_q   RECORD;
    v_out RECORD;
BEGIN
    IF p_released_by IS NULL OR btrim(p_released_by) = ''
    OR p_disposition IS NULL OR btrim(p_disposition) = '' THEN
        RAISE EXCEPTION 'g3_release_quarantined_topup: an attributed disposition is required';
    END IF;

    SELECT * INTO v_q FROM g3_topup_quarantine
     WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id
       AND released_at IS NULL
     FOR UPDATE;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'g3_release_quarantined_topup: no open quarantine for %/%',
            p_provider, p_provider_payment_id;
    END IF;

    -- Same writer, same stored payment_at. THIS is precondition A.
    SELECT * INTO v_out FROM public.g3_write_lot(
        p_tenant, 'topup', p_credits, p_ledger_op_id,
        'grant', p_provider, p_provider_payment_id, NULL);

    UPDATE g3_topup_quarantine
       SET released_at         = clock_timestamp(),
           released_by         = p_released_by,
           release_disposition = p_disposition,
           released_lot_id     = v_out.lot_id
     WHERE id = v_q.id;

    RETURN QUERY SELECT v_out.lot_id, v_out.acquired_at, v_out.rate_date,
                        v_out.is_priced, v_out.grandfathered, v_out.reason;
END
$$;

-- =====================================================================
-- 9. ITEM 5 -- journal treatment, per Decision 3 (AUTHORITATIVE).
--
--    cash-in   Dr Piutang settlement provider / Cr Liabilitas kontrak  @ SUPPLIER FEE
--    payout    Dr Bank, Dr/Cr selisih kurs     / Cr Piutang settlement
--    HELD      no GL entry            (subsidiary ledger only)
--    RELEASED  no GL entry            (subsidiary move HELD -> AVAILABLE)
--    CONSUMED  Dr Liabilitas kontrak  / Cr Pendapatan SaaS   <- the ONLY revenue moment
--    REFUNDED  unconsumed  -> Dr Liabilitas kontrak / Cr Piutang-Utang provider
--              recognised  -> Dr Kontra-pendapatan  / Cr Piutang-Utang provider
--
--    Wimba reverses NO PPN: under the Dodo MoR contract the customer tax is
--    Dodo's, and Wimba never recorded a PPN Keluaran to reverse.
-- =====================================================================

-- Two accounts Decision 3 requires and the chart did not have.
INSERT INTO gl_accounts (code, name, type) VALUES
    ('1150', 'Piutang Settlement Provider (MoR settlement receivable)', 'asset'),
    ('4900', 'Retur & Potongan Penjualan (contra-revenue)',             'revenue')
ON CONFLICT (code) DO NOTHING;

COMMENT ON TABLE gl_accounts IS
  'Chart of accounts. 1150 and 4900 added by 0086 for CR-29 Decision 3: cash-in lands on a settlement receivable at the SUPPLIER FEE (not customer gross, not Kas), and refunds of already-recognised revenue post to contra-revenue rather than debiting revenue directly. 4600 (Breakage) is UNUSED BY POLICY -- Decision 3 A7 disables breakage recognition.';

-- ── Cash-in ──────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.g3_post_cash_in(
    p_tenant              UUID,
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_op_id               TEXT
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v RECORD;
    v_entry UUID;
    v_amt NUMERIC(18,2);
BEGIN
    SELECT * INTO v FROM g3_provider_payments
     WHERE provider = p_provider AND provider_payment_id = p_provider_payment_id;
    IF NOT FOUND THEN
        RAISE EXCEPTION 'g3_post_cash_in: unknown payment %/%', p_provider, p_provider_payment_id;
    END IF;

    -- Decision 3 A6 fail-closed: never post with an unknown tax owner or channel.
    IF v.sales_channel IS NULL OR v.tax_owner IS NULL THEN
        RAISE EXCEPTION
            'g3_post_cash_in: fail-closed, sales_channel/tax_owner unknown for %/% -- '
            'Decision 3 A6 forbids posting until both are established',
            p_provider, p_provider_payment_id;
    END IF;
    IF v.supplier_fee_minor IS NULL OR v.supplier_fee_currency IS NULL THEN
        RAISE EXCEPTION
            'g3_post_cash_in: fail-closed, supplier fee not established for %/% -- '
            'the customer gross is NOT a substitute (Decision 3 A1/B2)',
            p_provider, p_provider_payment_id;
    END IF;
    IF v.supplier_fee_currency <> 'IDR' THEN
        RAISE EXCEPTION 'g3_post_cash_in: supplier fee is % -- IDR translation is not established here',
            v.supplier_fee_currency;
    END IF;
    IF v.payment_at IS NULL THEN
        RAISE EXCEPTION 'g3_post_cash_in: payment_at is not pinned for %/%; nothing may be dated from now()',
            p_provider, p_provider_payment_id;
    END IF;

    v_amt := v.supplier_fee_minor::NUMERIC(18,2);

    INSERT INTO journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (v.payment_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (v.payment_at AT TIME ZONE 'UTC')::date)::date,
            'topup', p_op_id,
            format('CR-29 cash-in at supplier fee (%s, tax owner %s)', v.sales_channel, v.tax_owner))
    RETURNING id INTO v_entry;

    INSERT INTO journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo) VALUES
        (v_entry, '1150', v_amt, 0, p_provider_payment_id, 'Piutang settlement provider'),
        (v_entry, '2000', 0, v_amt, p_provider_payment_id, 'Liabilitas kontrak - kredit HELD');

    -- NO PPN LEG. Decision 3 A6: on the Dodo MoR channel the customer tax is
    -- Dodo's own; Wimba records no PPN Keluaran and therefore has none to
    -- reverse later. wimba_output_vat_idr = 0, permanently, for this channel --
    -- this does NOT change when Wimba becomes PKP.
    RETURN v_entry;
END
$$;

-- ── Consumption: the ONLY revenue moment ─────────────────────────────────────
CREATE OR REPLACE FUNCTION public.g3_post_consumption(
    p_tenant         UUID,
    p_lot_id         UUID,
    p_credits        BIGINT,
    p_revenue_account TEXT,
    p_op_id          TEXT,
    p_occurred_at    TIMESTAMPTZ
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_lot RECORD;
    v_amt NUMERIC(18,2);
    v_entry UUID;
BEGIN
    SELECT * INTO v_lot FROM credit_lots WHERE id = p_lot_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'g3_post_consumption: unknown lot %', p_lot_id; END IF;

    -- An unpriced lot recognises nothing. Silent zero-revenue would be wrong in
    -- the other direction; this is loud.
    IF NOT v_lot.is_priced THEN
        RAISE EXCEPTION 'g3_post_consumption: lot % is unpriced (%); no revenue may be recognised',
            p_lot_id, v_lot.unpriced_reason;
    END IF;
    IF p_revenue_account NOT IN ('4100','4200','4300','4400','4500') THEN
        RAISE EXCEPTION 'g3_post_consumption: % is not a SaaS revenue account', p_revenue_account;
    END IF;

    -- Relieve to EXACTLY zero on full spend rather than credits x rounded rate.
    IF p_credits >= v_lot.credits_remaining THEN
        v_amt := v_lot.dpp_total_idr - v_lot.recognized_idr;
    ELSE
        v_amt := ROUND(v_lot.price_per_credit_idr * p_credits, 2);
    END IF;

    INSERT INTO journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (p_occurred_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (p_occurred_at AT TIME ZONE 'UTC')::date)::date,
            'consume', p_op_id, 'CR-29 revenue recognised on consumption')
    RETURNING id INTO v_entry;

    INSERT INTO journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo) VALUES
        (v_entry, '2000', v_amt, 0, p_lot_id::text, 'Liabilitas kontrak - kredit tersedia'),
        (v_entry, p_revenue_account, 0, v_amt, p_lot_id::text, 'Pendapatan SaaS');

    UPDATE credit_lots
       SET recognized_idr    = recognized_idr + v_amt,
           credits_remaining = credits_remaining - p_credits
     WHERE id = p_lot_id;

    RETURN v_entry;
END
$$;

COMMENT ON FUNCTION public.g3_post_consumption IS
  'Decision 3 A4: the ONLY moment revenue is recognised. RELEASED does not call this -- releasing credit from quarantine recognises nothing (A3).';

-- ── Refund ───────────────────────────────────────────────────────────────────
CREATE OR REPLACE FUNCTION public.g3_post_refund(
    p_tenant              UUID,
    p_lot_id              UUID,
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_refund_amount_minor BIGINT,   -- GROSS, as the provider reports it
    p_anchor_amount_minor BIGINT,   -- GROSS anchor, as the provider reports it
    p_op_id               TEXT,
    p_occurred_at         TIMESTAMPTZ
) RETURNS UUID
LANGUAGE plpgsql AS $$
DECLARE
    v_lot RECORD;
    v_ratio NUMERIC;
    v_total NUMERIC(18,2);
    v_from_revenue NUMERIC(18,2);
    v_from_liability NUMERIC(18,2);
    v_entry UUID;
BEGIN
    SELECT * INTO v_lot FROM credit_lots WHERE id = p_lot_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'g3_post_refund: unknown lot %', p_lot_id; END IF;
    IF NOT v_lot.is_priced THEN
        RAISE EXCEPTION 'g3_post_refund: lot % is unpriced (%); there is no carrying value to reverse',
            p_lot_id, v_lot.unpriced_reason;
    END IF;

    -- §C of the Decision-3 record, applied as the stated assumption: the ratio
    -- is GROSS-denominated (that is the only denomination the provider reports)
    -- and is applied to the NET carrying value. This preserves
    -- "full refund <=> refund_amount = anchor amount" while keeping the books
    -- on the supplier fee.
    IF p_anchor_amount_minor IS NULL OR p_anchor_amount_minor = 0
       OR p_refund_amount_minor IS NULL THEN
        RAISE EXCEPTION
            'g3_post_refund: fail-closed, the provider did not report both a refund amount and an '
            'anchor amount for %/%; the clawback base is a contract question and must not be guessed',
            p_provider, p_provider_payment_id;
    END IF;

    v_ratio := p_refund_amount_minor::NUMERIC / p_anchor_amount_minor::NUMERIC;
    IF v_ratio < 0 OR v_ratio > 1 THEN
        RAISE EXCEPTION 'g3_post_refund: refund ratio % is outside [0,1]', v_ratio;
    END IF;

    v_total := ROUND(v_lot.dpp_total_idr * v_ratio, 2);

    -- Split by consumption state (Decision 3 A5). Revenue already recognised
    -- reverses through CONTRA-REVENUE; the unconsumed remainder simply releases
    -- the contract liability. Do not reverse revenue that was never recognised.
    v_from_revenue   := LEAST(v_total, v_lot.recognized_idr);
    v_from_liability := v_total - v_from_revenue;

    INSERT INTO journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (p_occurred_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (p_occurred_at AT TIME ZONE 'UTC')::date)::date,
            'refund', p_op_id, 'CR-29 refund, split by consumption state')
    RETURNING id INTO v_entry;

    IF v_from_liability > 0 THEN
        INSERT INTO journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
        VALUES (v_entry, '2000', v_from_liability, 0, p_provider_payment_id, 'Liabilitas kontrak (belum dikonsumsi)');
    END IF;
    IF v_from_revenue > 0 THEN
        INSERT INTO journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
        VALUES (v_entry, '4900', v_from_revenue, 0, p_provider_payment_id, 'Kontra-pendapatan / refund');
    END IF;
    INSERT INTO journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
    VALUES (v_entry, '1150', 0, v_total, p_provider_payment_id, 'Piutang/Utang settlement provider');

    UPDATE credit_lots SET recognized_idr = recognized_idr - v_from_revenue WHERE id = p_lot_id;

    -- NO PPN REVERSAL. Decision 3 A5: Dodo is MoR, so Dodo corrects the
    -- customer's tax. Wimba never recorded a PPN Keluaran here.
    RETURN v_entry;
END
$$;

-- =====================================================================
-- 10. Correct the stale posting comment on payments.
--
--     It described DR Kas / CR Deferred-Rev / CR PPN, which Decision 3 makes
--     wrong on all three legs: cash-in lands on a settlement RECEIVABLE, at the
--     SUPPLIER FEE, with NO Wimba PPN leg on the MoR channel.
-- =====================================================================
COMMENT ON TABLE payments IS
  'Cash-in events. SUPERSEDED POSTING (0086, CR-29 Decision 3): cash-in is Dr 1150 Piutang settlement provider / Cr 2000 Liabilitas kontrak, valued at the SUPPLIER FEE from g3_provider_payments -- NOT Dr Kas, NOT the customer gross, and with NO Wimba PPN leg while Dodo is Merchant of Record. The former "DR Kas / CR Deferred-Rev / CR PPN" description is wrong on all three legs. Revenue is recognised on CONSUMPTION only.';
COMMENT ON COLUMN payments.dpp_idr IS
  'Dasar Pengenaan Pajak = gross - ppn (GENERATED). 🔴 NOT the CR-29 revenue base: Decision 3 anchors lot valuation to g3_provider_payments.supplier_fee_minor instead, because with ppn_idr = 0 on the MoR channel this column equals the full customer gross and would over-recognise revenue by Dodo''s tax, discount and fee.';

COMMIT;

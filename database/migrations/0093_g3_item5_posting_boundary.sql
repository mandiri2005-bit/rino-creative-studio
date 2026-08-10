-- =====================================================================
-- 0093_g3_item5_posting_boundary.sql
--
-- GATE 4: put the Item-5 journal path behind its own SECURITY DEFINER boundary.
--
-- 🔴 WHY THIS EXISTS, AND HOW IT WAS FOUND. `0091` fixed the birth binding — the
--    runtime pool is `app_user`, not `neondb_owner`, and the example file said
--    otherwise for long enough that the first cut granted the membership to the
--    wrong role. Repairing that and pointing the test pool at `app_user` made a
--    SECOND path fail for the same reason: `g3_post_cash_in`,
--    `g3_post_consumption` and `g3_post_refund` are SECURITY INVOKER, and
--    `app_user` holds SELECT and nothing else on `journal_entries` and
--    `credit_lots`. The Item-5 posting path had never once been executed as the
--    identity production actually uses, so nobody had discovered it cannot run.
--
--    The remedy is NOT to hand `app_user` INSERT/UPDATE. That would put the
--    books inside reach of every ordinary statement the application makes, and
--    the thing being protected here is the ledger. Instead the three functions
--    move behind a boundary of their own, on the same pattern `0091` used for
--    birth — and deliberately NOT the same role: `g3_birth_definer` is
--    contracted to own exactly one function, and widening it would make "the
--    birth boundary" a name for something else.
--
-- WHAT THE RUNTIME ENDS UP HOLDING
--   `g3_posting_engine` — EXECUTE on FOUR entrypoints and nothing else:
--     `g3_birth_lots`  (0091)  + `g3_post_cash_in` / `g3_post_consumption` /
--     `g3_post_refund` (here). No helper is executable, and no table privilege
--     is held by either the engine or `app_user`.
--
-- 🔴 THE TENANT BINDING IS THE POINT OF DANGER IN ANY DEFINER. Inside these
--    functions the identity is the boundary owner, so `tenant_isolation` is
--    evaluated against the transaction's `app.current_tenant_id` — NOT against
--    the `p_tenant` argument. A caller passing someone else's tenant id would
--    otherwise write into books the policy was checking on a different key. So
--    each function refuses unless `p_tenant` equals the transaction context, and
--    consumption/refund additionally prove the LOT they read belongs to that
--    tenant. The parameter never substitutes for RLS; it must agree with it.
--
-- 🔴 `search_path` IS PINNED TO `pg_catalog, pg_temp`, which is what forces
--    every reference below to be schema-qualified. An unqualified name in a
--    definer is a hijack: whoever can create `public.credit_lots` in an earlier
--    schema chooses what the ledger writes to.
--
-- The bodies are `0086`'s own text, machine-extracted, with exactly FIVE
-- transforms: the definer/search_path clause, schema qualification, the tenant
-- guard, the lot-ownership check, and the credited-anchor binding that makes
-- `payment_events` — not the caller — the authority on whose payment this is.
-- `0086` is accepted and is NOT edited.
-- FORWARD-ONLY. `G3_LOT_WRITER_ENABLED` stays `'0'`.
-- =====================================================================

BEGIN;

DO $guard$
BEGIN
    IF to_regproc('public.g3_post_cash_in') IS NULL THEN
        RAISE EXCEPTION 'ABORT 0093: apply 0086 first (g3_post_cash_in is absent)';
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'g3_posting_engine') THEN
        RAISE EXCEPTION 'ABORT 0093: role g3_posting_engine is absent (0088 creates it)';
    END IF;
END
$guard$;

-- ---------------------------------------------------------------------
-- 1. The posting boundary role. Separate from the birth boundary on purpose.
-- ---------------------------------------------------------------------
DO $mk$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'g3_posting_definer') THEN
        CREATE ROLE g3_posting_definer NOLOGIN NOSUPERUSER NOBYPASSRLS;
    END IF;
END
$mk$;

COMMENT ON ROLE g3_posting_definer IS
  'Owns the three Item-5 posting functions and nothing else. NOBYPASSRLS: tenant_isolation on journal_entries and credit_lots is still evaluated, against the transaction GUC, which is why each function refuses a p_tenant that disagrees with it. Deliberately NOT g3_birth_definer, which is contracted to own the birth entrypoint alone.';

GRANT g3_posting_definer TO CURRENT_USER WITH INHERIT FALSE, SET TRUE;

-- ---------------------------------------------------------------------
-- 2. The three functions, as 0086 wrote them plus the four transforms.
-- ---------------------------------------------------------------------
CREATE OR REPLACE FUNCTION public.g3_post_cash_in(
    p_tenant              UUID,
    p_provider            TEXT,
    p_provider_payment_id TEXT,
    p_op_id               TEXT
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v RECORD;
    v_entry UUID;
    v_amt NUMERIC(18,2);
BEGIN
    -- 🔴 A SECURITY DEFINER BOUNDARY MUST NOT LET A CALLER-SUPPLIED TENANT
    --    STAND IN FOR RLS. Inside this function the identity is the boundary
    --    owner, so `tenant_isolation` is evaluated against the transaction's
    --    `app.current_tenant_id`, not against `p_tenant`. If the two were
    --    allowed to differ, the parameter would be a way to write one tenant's
    --    books while the policy checked another's. They must agree, and the
    --    disagreement is an error rather than a silent no-op.
    IF p_tenant IS NULL
       OR p_tenant IS DISTINCT FROM
          nullif(pg_catalog.current_setting('app.current_tenant_id', true), '')::UUID THEN
        RAISE EXCEPTION
            'g3_post_cash_in: p_tenant % does not match the transaction tenant context; the caller '
            'parameter may not stand in for RLS',
            p_tenant;
    END IF;

    SELECT * INTO v FROM public.g3_provider_payments
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

    -- 🔴 `p_tenant = GUC` PROVES NOTHING ABOUT THIS PAYMENT. Both sides of that
    --    comparison are set by the caller, so a caller may nominate any tenant
    --    it likes and post another tenant's payment into its own books. The
    --    authority for "whose payment is this" is the CREDITED ANCHOR in
    --    `payment_events`, which `payment_events_credited_anchor_uniq` already
    --    guarantees is at most one row per (provider, provider_payment_id).
    --
    --    The read is deliberately left under RLS rather than bypassed: the
    --    `tenant_isolation` policy does the attribution itself, so a row for
    --    another tenant is INVISIBLE here and lands in the same refusal as no
    --    row at all. Both are fail-closed with zero journals, and the message
    --    says plainly that it cannot tell them apart — because it cannot, and
    --    claiming otherwise would be the more dangerous answer.
    PERFORM 1 FROM public.payment_events pe
      WHERE pe.provider = p_provider
        AND pe.provider_payment_id = p_provider_payment_id
        AND pe.credited;
    IF NOT FOUND THEN
        RAISE EXCEPTION
            'g3_post_cash_in: no credited payment_events anchor for %/% visible to tenant % -- '
            'either the payment was never credited, or it is anchored to a DIFFERENT tenant. '
            'The caller-supplied tenant is not evidence of ownership; nothing is posted.',
            p_provider, p_provider_payment_id, p_tenant;
    END IF;

    v_amt := v.supplier_fee_minor::NUMERIC(18,2);

    INSERT INTO public.journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (v.payment_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (v.payment_at AT TIME ZONE 'UTC')::date)::date,
            'topup', p_op_id,
            format('CR-29 cash-in at supplier fee (%s, tax owner %s)', v.sales_channel, v.tax_owner))
    RETURNING id INTO v_entry;

    INSERT INTO public.journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo) VALUES
        (v_entry, '1150', v_amt, 0, p_provider_payment_id, 'Piutang settlement provider'),
        (v_entry, '2000', 0, v_amt, p_provider_payment_id, 'Liabilitas kontrak - kredit HELD');

    -- NO PPN LEG. Decision 3 A6: on the Dodo MoR channel the customer tax is
    -- Dodo's own; Wimba records no PPN Keluaran and therefore has none to
    -- reverse later. wimba_output_vat_idr = 0, permanently, for this channel --
    -- this does NOT change when Wimba becomes PKP.
    RETURN v_entry;
END
$$;

CREATE OR REPLACE FUNCTION public.g3_post_consumption(
    p_tenant         UUID,
    p_lot_id         UUID,
    p_credits        BIGINT,
    p_revenue_account TEXT,
    p_op_id          TEXT,
    p_occurred_at    TIMESTAMPTZ
) RETURNS UUID
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_lot RECORD;
    v_amt NUMERIC(18,2);
    v_entry UUID;
BEGIN
    -- 🔴 A SECURITY DEFINER BOUNDARY MUST NOT LET A CALLER-SUPPLIED TENANT
    --    STAND IN FOR RLS. Inside this function the identity is the boundary
    --    owner, so `tenant_isolation` is evaluated against the transaction's
    --    `app.current_tenant_id`, not against `p_tenant`. If the two were
    --    allowed to differ, the parameter would be a way to write one tenant's
    --    books while the policy checked another's. They must agree, and the
    --    disagreement is an error rather than a silent no-op.
    IF p_tenant IS NULL
       OR p_tenant IS DISTINCT FROM
          nullif(pg_catalog.current_setting('app.current_tenant_id', true), '')::UUID THEN
        RAISE EXCEPTION
            'g3_post_consumption: p_tenant % does not match the transaction tenant context; the caller '
            'parameter may not stand in for RLS',
            p_tenant;
    END IF;

    SELECT * INTO v_lot FROM public.credit_lots WHERE id = p_lot_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'g3_post_consumption: unknown lot %', p_lot_id; END IF;
    IF v_lot.tenant_id IS DISTINCT FROM p_tenant THEN
        RAISE EXCEPTION 'g3_post_consumption: lot % belongs to another tenant; refusing to post against it',
            p_lot_id;
    END IF;

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

    INSERT INTO public.journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (p_occurred_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (p_occurred_at AT TIME ZONE 'UTC')::date)::date,
            'consume', p_op_id, 'CR-29 revenue recognised on consumption')
    RETURNING id INTO v_entry;

    INSERT INTO public.journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo) VALUES
        (v_entry, '2000', v_amt, 0, p_lot_id::text, 'Liabilitas kontrak - kredit tersedia'),
        (v_entry, p_revenue_account, 0, v_amt, p_lot_id::text, 'Pendapatan SaaS');

    UPDATE public.credit_lots
       SET recognized_idr    = recognized_idr + v_amt,
           credits_remaining = credits_remaining - p_credits
     WHERE id = p_lot_id;

    RETURN v_entry;
END
$$;

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
LANGUAGE plpgsql
SECURITY DEFINER
SET search_path = pg_catalog, pg_temp
AS $$
DECLARE
    v_lot RECORD;
    v_ratio NUMERIC;
    v_total NUMERIC(18,2);
    v_from_revenue NUMERIC(18,2);
    v_from_liability NUMERIC(18,2);
    v_entry UUID;
BEGIN
    -- 🔴 A SECURITY DEFINER BOUNDARY MUST NOT LET A CALLER-SUPPLIED TENANT
    --    STAND IN FOR RLS. Inside this function the identity is the boundary
    --    owner, so `tenant_isolation` is evaluated against the transaction's
    --    `app.current_tenant_id`, not against `p_tenant`. If the two were
    --    allowed to differ, the parameter would be a way to write one tenant's
    --    books while the policy checked another's. They must agree, and the
    --    disagreement is an error rather than a silent no-op.
    IF p_tenant IS NULL
       OR p_tenant IS DISTINCT FROM
          nullif(pg_catalog.current_setting('app.current_tenant_id', true), '')::UUID THEN
        RAISE EXCEPTION
            'g3_post_refund: p_tenant % does not match the transaction tenant context; the caller '
            'parameter may not stand in for RLS',
            p_tenant;
    END IF;

    SELECT * INTO v_lot FROM public.credit_lots WHERE id = p_lot_id FOR UPDATE;
    IF NOT FOUND THEN RAISE EXCEPTION 'g3_post_refund: unknown lot %', p_lot_id; END IF;
    IF v_lot.tenant_id IS DISTINCT FROM p_tenant THEN
        RAISE EXCEPTION 'g3_post_refund: lot % belongs to another tenant; refusing to post against it',
            p_lot_id;
    END IF;
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

    INSERT INTO public.journal_entries (tenant_id, entry_date, period, source_type, source_op_id, memo)
    VALUES (p_tenant, (p_occurred_at AT TIME ZONE 'UTC')::date,
            date_trunc('month', (p_occurred_at AT TIME ZONE 'UTC')::date)::date,
            'refund', p_op_id, 'CR-29 refund, split by consumption state')
    RETURNING id INTO v_entry;

    IF v_from_liability > 0 THEN
        INSERT INTO public.journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
        VALUES (v_entry, '2000', v_from_liability, 0, p_provider_payment_id, 'Liabilitas kontrak (belum dikonsumsi)');
    END IF;
    IF v_from_revenue > 0 THEN
        INSERT INTO public.journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
        VALUES (v_entry, '4900', v_from_revenue, 0, p_provider_payment_id, 'Kontra-pendapatan / refund');
    END IF;
    INSERT INTO public.journal_lines (entry_id, account_code, debit_idr, credit_idr, source_ref, memo)
    VALUES (v_entry, '1150', 0, v_total, p_provider_payment_id, 'Piutang/Utang settlement provider');

    UPDATE public.credit_lots SET recognized_idr = recognized_idr - v_from_revenue WHERE id = p_lot_id;

    -- NO PPN REVERSAL. Decision 3 A5: Dodo is MoR, so Dodo corrects the
    -- customer's tax. Wimba never recorded a PPN Keluaran here.
    RETURN v_entry;
END
$$;

-- ---------------------------------------------------------------------
-- 3. Ownership transfer + the minimum the boundary needs internally.
-- ---------------------------------------------------------------------
DO $own$
DECLARE
    v_had_create BOOLEAN := has_schema_privilege('g3_posting_definer', 'public', 'CREATE');
    v_had_usage  BOOLEAN := has_schema_privilege('g3_posting_definer', 'public', 'USAGE');
    v_fn TEXT;
BEGIN
    -- CREATE-on-schema is checked at ALTER ... OWNER TO time and never again.
    IF NOT v_had_create THEN EXECUTE 'GRANT CREATE ON SCHEMA public TO g3_posting_definer'; END IF;
    IF NOT v_had_usage  THEN EXECUTE 'GRANT USAGE  ON SCHEMA public TO g3_posting_definer'; END IF;

    FOREACH v_fn IN ARRAY ARRAY[
        'public.g3_post_cash_in(uuid,text,text,text)',
        'public.g3_post_consumption(uuid,uuid,bigint,text,text,timestamptz)',
        'public.g3_post_refund(uuid,uuid,text,text,bigint,bigint,text,timestamptz)'
    ] LOOP
        -- 🔴 REVOKE FIRST, HAND OVER SECOND — the same ordering trap 0091
        --    documents. `CREATE OR REPLACE` above re-created these owned by the
        --    migration role with PUBLIC EXECUTE restored by default; once
        --    ownership moves, this role is no longer the owner (the membership
        --    is INHERIT FALSE) and the REVOKE degrades to a WARNING that
        --    changes nothing. It did exactly that on the first run, and only
        --    the post-condition below caught it.
        EXECUTE format('REVOKE ALL ON FUNCTION %s FROM PUBLIC, app_user', v_fn);
        EXECUTE format('ALTER FUNCTION %s OWNER TO g3_posting_definer', v_fn);
    END LOOP;

    IF NOT v_had_create THEN EXECUTE 'REVOKE CREATE ON SCHEMA public FROM g3_posting_definer'; END IF;
    IF NOT v_had_usage  THEN EXECUTE 'REVOKE USAGE  ON SCHEMA public FROM g3_posting_definer'; END IF;
END
$own$;

-- The evidence the three functions read — including what their TRIGGERS read.
-- 🔴 A trigger fired from a SECURITY DEFINER runs as the DEFINER, not as the
--    original caller, so `enforce_period_open`'s lookup of the open accounting
--    period is a read this boundary must be allowed to make. Missing it fails
--    the posting with "permission denied for table accounting_periods" — a
--    refusal from a table the function body never mentions.
GRANT SELECT ON public.g3_provider_payments TO g3_posting_definer;
GRANT SELECT ON public.accounting_periods   TO g3_posting_definer;
GRANT SELECT ON public.payment_events       TO g3_posting_definer;

-- The books they write. SELECT on journal_entries is needed for `RETURNING id`.
GRANT SELECT, INSERT ON public.journal_entries TO g3_posting_definer;
-- SELECT on journal_lines is the BALANCE TRIGGER's read, not the function's:
-- enforce_journal_balanced sums the entry's lines to prove Dr = Cr. INSERT
-- alone gets "permission denied for table journal_lines" from a statement the
-- body never wrote.
GRANT SELECT, INSERT ON public.journal_lines   TO g3_posting_definer;

-- 🔴 THE LOT UPDATE IS COLUMN-SCOPED, and that is the difference between a
--    posting boundary and a second writer. Consumption and refund move
--    `recognized_idr` and `credits_remaining`; NOTHING here may touch a
--    birth-immutable field — not `dpp_total_idr`, not `price_per_credit_idr`,
--    not `is_priced`, not the provenance columns. No INSERT and no DELETE
--    either: this boundary posts against lots, it does not create or destroy
--    them. That is birth's job, behind its own door.
GRANT SELECT                                      ON public.credit_lots TO g3_posting_definer;
GRANT UPDATE (recognized_idr, credits_remaining)  ON public.credit_lots TO g3_posting_definer;

-- ---------------------------------------------------------------------
-- 3b. Pin the trigger functions the posting path fires.
--
-- 🔴 A TRIGGER WITH NO `search_path` OF ITS OWN INHERITS THE CALLER'S, and the
--    caller is now a definer pinned to `pg_catalog, pg_temp`. `enforce_period_open`
--    resolves `accounting_periods` unqualified, so the first posting through the
--    new boundary failed with "relation accounting_periods does not exist" —
--    the trigger was silently relying on whatever search_path happened to be in
--    force. That is the same hijack risk the pinning exists to remove, one frame
--    further down: a trigger whose table resolution depends on its caller can be
--    pointed at a different table by a caller who chooses the path.
--
--    Pinning them is forward-only and changes no logic. `0031` is not edited.
--    EVERY trigger on the tables this boundary writes, not just the first one
--    that failed: journal_lines carries two of its own.
--
-- 🔴 AND THEY ARE REDEFINED, NOT MERELY RE-PINNED. The first attempt pinned them
--    to `pg_catalog, public, pg_temp` — which fixed the error and kept the very
--    hazard the pinning exists to remove: `public` still on the path, every
--    table reference still unqualified, so the resolution is still positional.
--    Putting `public` back is not hardening, it is the same door with a lock
--    hanging off it. The bodies below are byte-for-byte the accepted logic with
--    exactly two changes — `public.` in front of every table, and the strict
--    search_path — and they stay SECURITY INVOKER: these are integrity triggers
--    that must run as whoever is writing, never with borrowed authority.
CREATE OR REPLACE FUNCTION public.enforce_period_open() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trg$
DECLARE
    v_status TEXT;
BEGIN
    -- source side: a CLOSED period the row currently lives in cannot be changed/erased.
    IF TG_OP IN ('UPDATE','DELETE') THEN
        SELECT status INTO v_status FROM public.accounting_periods WHERE period = OLD.period;
        IF v_status = 'closed' THEN
            RAISE EXCEPTION 'cannot % a journal_entry in CLOSED period %', TG_OP, OLD.period
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    -- destination side: cannot post/move INTO a CLOSED period.
    IF TG_OP IN ('INSERT','UPDATE') THEN
        SELECT status INTO v_status FROM public.accounting_periods WHERE period = NEW.period;
        IF v_status = 'closed' THEN
            RAISE EXCEPTION 'cannot post journal_entry % into CLOSED period %', NEW.id, NEW.period
                USING ERRCODE = 'check_violation';
        END IF;
    END IF;
    RETURN CASE WHEN TG_OP = 'DELETE' THEN OLD ELSE NEW END;
END;
$trg$;

CREATE OR REPLACE FUNCTION public.enforce_journal_header_complete() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trg$
DECLARE
    v_lines  BIGINT;
    v_debit  NUMERIC(18,2);
    v_credit NUMERIC(18,2);
BEGIN
    -- header may have been deleted within the same tx -- nothing to assert then.
    IF NOT EXISTS (SELECT 1 FROM public.journal_entries WHERE id = NEW.id) THEN
        RETURN NULL;
    END IF;
    SELECT count(*), COALESCE(sum(debit_idr),0), COALESCE(sum(credit_idr),0)
      INTO v_lines, v_debit, v_credit
      FROM public.journal_lines
     WHERE entry_id = NEW.id;
    IF v_lines < 2 THEN
        RAISE EXCEPTION
            'journal_entry % is incomplete: % line(s) (a posting needs >=2 lines)',
            NEW.id, v_lines
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_debit <> v_credit THEN
        RAISE EXCEPTION
            'journal_entry % is unbalanced at header: debit=% credit=%',
            NEW.id, v_debit, v_credit
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;
END;
$trg$;

CREATE OR REPLACE FUNCTION public.enforce_period_open_lines() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trg$
DECLARE
    v_period DATE;
    v_status TEXT;
BEGIN
    SELECT period INTO v_period FROM public.journal_entries
     WHERE id = COALESCE(NEW.entry_id, OLD.entry_id);
    IF v_period IS NULL THEN          -- parent gone in same tx; nothing to guard
        RETURN COALESCE(NEW, OLD);
    END IF;
    SELECT status INTO v_status FROM public.accounting_periods WHERE period = v_period;
    IF v_status = 'closed' THEN
        RAISE EXCEPTION 'cannot modify journal_lines of a CLOSED period %', v_period
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$trg$;

CREATE OR REPLACE FUNCTION public.enforce_journal_balanced() RETURNS trigger
LANGUAGE plpgsql SET search_path = pg_catalog, pg_temp AS $trg$
DECLARE
    v_entry  UUID;
    v_lines  BIGINT;
    v_debit  NUMERIC(18,2);
    v_credit NUMERIC(18,2);
BEGIN
    v_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    -- entry may have been deleted (ON DELETE CASCADE) -- nothing to assert then.
    IF NOT EXISTS (SELECT 1 FROM public.journal_entries WHERE id = v_entry) THEN
        RETURN NULL;
    END IF;
    SELECT count(*), COALESCE(sum(debit_idr),0), COALESCE(sum(credit_idr),0)
      INTO v_lines, v_debit, v_credit
      FROM public.journal_lines
     WHERE entry_id = v_entry;
    -- AUDIT FIX (schema/HIGH): re-assert COMPLETENESS on every line DML, not just
    -- header DML -- else DELETE-ing lines could strip a committed entry to 0/1 lines
    -- (0 lines is vacuously balanced and slipped past the old balance-only check).
    IF v_lines < 2 THEN
        RAISE EXCEPTION
            'journal_entry % left with % line(s): a posting must keep >=2 lines',
            v_entry, v_lines
            USING ERRCODE = 'check_violation';
    END IF;
    IF v_debit <> v_credit THEN
        RAISE EXCEPTION
            'journal_entry % is unbalanced: debit=% credit=% (Sum(debit) must equal Sum(credit))',
            v_entry, v_debit, v_credit
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NULL;   -- AFTER trigger; return value ignored
END;
$trg$;

-- ---------------------------------------------------------------------
-- 4. EXECUTE: the runtime reaches these ONLY by assuming the posting engine.
-- ---------------------------------------------------------------------
DO $door$
BEGIN
    EXECUTE 'SET LOCAL ROLE g3_posting_definer';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.g3_post_cash_in(uuid,text,text,text) TO g3_posting_engine';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.g3_post_consumption(uuid,uuid,bigint,text,text,timestamptz) TO g3_posting_engine';
    EXECUTE 'GRANT EXECUTE ON FUNCTION public.g3_post_refund(uuid,uuid,text,text,bigint,bigint,text,timestamptz) TO g3_posting_engine';
    EXECUTE 'RESET ROLE';
END
$door$;

REVOKE g3_posting_definer FROM CURRENT_USER;

-- ---------------------------------------------------------------------
-- 5. POST-CONDITIONS.
-- ---------------------------------------------------------------------
DO $verify$
DECLARE
    v_fn TEXT;
    v_open TEXT := '';
    v_cols TEXT;
BEGIN
    FOREACH v_fn IN ARRAY ARRAY[
        'public.g3_post_cash_in(uuid,text,text,text)',
        'public.g3_post_consumption(uuid,uuid,bigint,text,text,timestamptz)',
        'public.g3_post_refund(uuid,uuid,text,text,bigint,bigint,text,timestamptz)'
    ] LOOP
        IF has_function_privilege('public', v_fn, 'EXECUTE') THEN v_open := v_open || ' PUBLIC:' || v_fn; END IF;
        IF has_function_privilege('app_user', v_fn, 'EXECUTE') THEN v_open := v_open || ' app_user:' || v_fn; END IF;
        IF NOT has_function_privilege('g3_posting_engine', v_fn, 'EXECUTE') THEN
            RAISE EXCEPTION 'ABORT 0093: g3_posting_engine cannot execute %', v_fn;
        END IF;
    END LOOP;
    IF v_open <> '' THEN
        RAISE EXCEPTION 'ABORT 0093: posting EXECUTE survived the revoke —%', v_open;
    END IF;

    IF EXISTS (SELECT 1 FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace
                WHERE n.nspname='public' AND p.proname IN
                      ('g3_post_cash_in','g3_post_consumption','g3_post_refund')
                  AND (NOT p.prosecdef
                       OR pg_get_userbyid(p.proowner) <> 'g3_posting_definer'
                       OR NOT ('search_path=pg_catalog, pg_temp' = ANY(p.proconfig)))) THEN
        RAISE EXCEPTION
            'ABORT 0093: a posting function is not SECURITY DEFINER, not owned by '
            'g3_posting_definer, or does not pin search_path to pg_catalog, pg_temp';
    END IF;

    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname='g3_posting_definer'
                AND (rolcanlogin OR rolsuper OR rolbypassrls)) THEN
        RAISE EXCEPTION 'ABORT 0093: g3_posting_definer must be NOLOGIN NOSUPERUSER NOBYPASSRLS';
    END IF;

    -- Neither the engine nor the runtime may reach the books directly.
    FOREACH v_fn IN ARRAY ARRAY['g3_posting_engine','app_user'] LOOP
        IF has_table_privilege(v_fn, 'public.journal_entries', 'INSERT')
           OR has_table_privilege(v_fn, 'public.journal_lines', 'INSERT')
           OR has_table_privilege(v_fn, 'public.credit_lots', 'INSERT')
           OR has_table_privilege(v_fn, 'public.credit_lots', 'UPDATE')
           OR has_table_privilege(v_fn, 'public.credit_lots', 'DELETE') THEN
            RAISE EXCEPTION 'ABORT 0093: % holds direct write privilege on the books', v_fn;
        END IF;
    END LOOP;

    -- 🔴 THE UPDATE SET IS ASSERTED EXACTLY, NOT SAMPLED. Naming three forbidden
    --    columns proves only that those three are absent; a table-wide GRANT, or
    --    one extra column added later, passes a spot check untouched. The
    --    question is which columns the boundary CAN write, and the answer must be
    --    these two and no others.
    SELECT coalesce(string_agg(a.attname, ',' ORDER BY a.attname), '<none>')
      INTO v_cols
      FROM pg_attribute a
     WHERE a.attrelid = 'public.credit_lots'::regclass
       AND a.attnum > 0 AND NOT a.attisdropped
       AND has_column_privilege('g3_posting_definer', a.attrelid, a.attnum, 'UPDATE');
    IF v_cols <> 'credits_remaining,recognized_idr' THEN
        RAISE EXCEPTION
            'ABORT 0093: g3_posting_definer can UPDATE {%} on credit_lots. The posting boundary '
            'may move exactly credits_remaining and recognized_idr — no fewer (the postings stop '
            'working) and no more (a birth-immutable field becomes writable after the fact).',
            v_cols;
    END IF;
    IF has_table_privilege('g3_posting_definer', 'public.credit_lots', 'INSERT')
       OR has_table_privilege('g3_posting_definer', 'public.credit_lots', 'DELETE') THEN
        RAISE EXCEPTION 'ABORT 0093: the posting boundary can create or destroy lots';
    END IF;
END
$verify$;

COMMIT;

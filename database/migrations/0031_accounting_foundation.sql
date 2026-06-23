-- =====================================================================
-- 0031_accounting_foundation.sql
-- Double-entry accounting foundation (PSAK 72 "Pendapatan dari Kontrak
-- dengan Pelanggan" / PSAK 10 "Pengaruh Perubahan Kurs Valuta Asing").
--
-- WHY: 0029 gave us query-time GL TAGGING + decorative gl_accounts + a
-- v_deferred_revenue VIEW that GUESSES the liability from a per-plan flat
-- price. The audit wants a REAL, POSTED, balanced ledger so:
--   * deferred revenue is a posted 2000 liability, not a query-time guess
--     (v_deferred_revenue gets DEMOTED to a reconciliation check vs this);
--   * free-tier consumption books ONLY acquisition cost (no revenue) -- the
--     E4 / audit-#2 over-recognition fix, enforced structurally by lots;
--   * USD provider COGS, realized + unrealized FX (PSAK 10) have a home;
--   * PPN / PPh / faktur pajak scaffolding exists INERT so the PKP switch-on
--     (Phase 4) is config-only, no schema change, no backfill.
--
-- POSTING MODEL = incremental-derive (LOCKED). A deterministic engine REPLAYS
-- credit_ledger (+ payments + credit_lots) in created_at order, maintains a
-- per-tenant FIFO lot queue, and EMITS balanced journal_entries idempotent on
-- the source op_id. There is NO hot-path change to credit_apply / the credit
-- lifecycle fns -- this migration only adds the tables the engine writes into.
--
-- This migration is APPEND-ONLY, FULLY IDEMPOTENT, ADDITIVE (never drops/alters
-- an existing money table destructively) and FK-safe. Posting ledger tables are
-- GLOBAL (owner-posted, app_user reads); tenant-scoped artefacts (payments,
-- credit_lots) carry RLS with BOTH USING and WITH CHECK (audit #39 gap fix).
--
-- All money is IDR (functional currency). 1 credit = $0.01 USD cost basis.
-- KURS_IDR_USD lives in python/credit_catalog.py; per-row fx is captured here.
--
-- VERIFICATION FIXES FOLDED IN (vs the first candidate):
--   #1  zero-line / orphan journal_entries header now rejected at COMMIT by a
--       DEFERRABLE constraint trigger on journal_entries (>=2 lines AND balanced).
--   #2  journal_entries.source_type now has a CHECK enum (a typo'd source_type
--       was silently defeating the idempotency UNIQUE -> double-post risk).
--   #3  payments now CHECK (gross = dpp + ppn) and (net = gross - fee) so the
--       DPP revenue base reconciles to cash collected.
--   #4  journal_entries.period now CHECK (= first-of-month) and an IMMEDIATE
--       BEFORE trigger refuses INSERT into a CLOSED accounting_period (DB-enforced,
--       matching the balance/RLS/REVOKE DB-level posture; engine still also
--       refuses, belt-and-braces). Immediate (not deferred) so a month-close tx
--       that posts final adjusting entries THEN flips status='closed' still works.
--   #6  faktur_pajak no longer grants app_user SELECT -- it is owner-read-only
--       (reporting runs as the BYPASSRLS owner), closing the latent cross-tenant
--       tax/PII read leak the un-RLS'd + app_user-SELECT combo would ship.
-- =====================================================================

BEGIN;

-- =====================================================================
-- 1. CHART OF ACCOUNTS -- extend gl_accounts (the 16 rows from 0029 stay).
--    Equity class was entirely MISSING; add it + tax + breakage/true-up.
--    Decorative-no-longer: these codes are now POSTED into journal_lines.
-- =====================================================================
INSERT INTO gl_accounts (code, name, type) VALUES
    ('1100','Piutang Usaha (Accounts Receivable)',            'asset'),
    ('1300','PPN Masukan (Input VAT)',                        'asset'),
    ('3000','Modal Disetor (Paid-in Capital)',                'equity'),
    ('3100','Laba Ditahan (Retained Earnings)',               'equity'),
    ('2200','PPN Keluaran (Output VAT)',                      'liability'),
    ('2300','Utang PPh 23/26 (WHT Payable)',                  'liability'),
    ('2400','Utang PPh Final / Badan (Income Tax Payable)',   'liability'),
    ('4600','Pendapatan - Breakage Kredit',                   'revenue'),
    ('5900','COGS - Penyesuaian / True-up',                   'cogs'),
    ('6400','Beban Pajak Penghasilan (Income Tax Expense)',   'opex'),
    -- Provider prepaid float: buying API credits (LaoZhang/OpenAI/etc) is a
    -- PREPAID ASSET, not an expense; COGS is drawn down here as credits are
    -- consumed (per-call cost_usd in usage_logs). Postpaid invoices use 2100.
    ('1400','Prepaid Provider Upstream (USD float)',          'asset'),
    -- Operating expenses (G&A) — entered as manual Bills in the finance app
    -- (recurring vendors). Sit below gross profit → operating profit.
    ('6500','Beban Hosting & Infrastruktur (Railway/Neon/Upstash/R2/Qdrant)', 'opex'),
    ('6600','Beban Software & Tools (Clerk/domain/dev tools)',  'opex'),
    ('6700','Beban Gaji & Tunjangan',                          'opex'),
    ('6710','Beban Listrik, Air & Utilitas',                   'opex'),
    ('6720','Beban Internet & Telekomunikasi',                 'opex'),
    ('6730','Beban Sewa Kantor',                               'opex'),
    ('6750','Beban Pemasaran & Iklan',                         'opex'),
    ('6760','Beban Jasa Profesional (akuntan/legal/KAP)',      'opex'),
    ('6790','Beban Operasional Lain-lain',                     'opex'),
    ('6800','Beban Penyusutan (Depreciation)',                 'opex'),
    -- AUDIT FIX (coa/HIGH): depreciation (6800) needs a fixed-asset + contra
    -- partner or it cannot post balanced; add Aset Tetap + Akumulasi Penyusutan
    -- (contra-asset, normal-credit) + prepaid-expense + accrued-liability + PPN
    -- clearing so accrual accounting + asset depreciation are representable.
    ('1200','Biaya Dibayar Dimuka (Prepaid Expenses)',         'asset'),
    ('1500','Aset Tetap - Harga Perolehan (Fixed Assets)',     'asset'),
    ('1600','Akumulasi Penyusutan (Accumulated Depreciation, contra-asset)', 'asset'),
    ('2500','Beban yang Masih Harus Dibayar (Accrued Liabilities)', 'liability'),
    ('2600','Utang PPN / PPN Kurang Bayar (VAT clearing)',     'liability')
ON CONFLICT (code) DO NOTHING;
-- (6300 Selisih Kurs already seeded by 0029; not re-inserted.)

-- AUDIT FIX (coa/HIGH): 2100 'Utang / Prepaid Provider' conflated a PAYABLE and a
-- PREPAID ASSET in one liability code. 1400 is now the prepaid asset; 2100 is the
-- POSTPAID payable only. Rename for clarity (idempotent; ON CONFLICT seed kept it).
UPDATE gl_accounts SET name = 'Utang Provider Upstream (postpaid payable)'
 WHERE code = '2100';

-- =====================================================================
-- 2. JOURNAL ENTRIES -- one header per posted economic event.
--    GLOBAL (owner-posted by the derive engine). source_op_id is the
--    idempotency key tying a posting back to its source (ledger op_id,
--    payment external_ref, period-close key, ...) so a replay never doubles.
--
--    FIX #2: source_type carries a CHECK enum. Without it a typo'd
--    source_type changes the UNIQUE(source_type, source_op_id) key and lets
--    the SAME economic op post a SECOND time (double-post). The enum mirrors
--    the credit_ledger.reason / payments.status CHECK pattern.
--    FIX #4: period must be the first day of its month (canonical bucket).
-- =====================================================================
CREATE TABLE IF NOT EXISTS journal_entries (
    id            UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    -- AUDIT FIX (encoding/CRITICAL + HIGH): tenant_id makes the global idempotency
    -- key tenant-safe (two tenants can reuse the same op_id without one silently
    -- dropping the other) AND makes per-tenant financials derivable from the
    -- ledger. NULL = company-level entry (fx_reval, period close, depreciation).
    -- ON DELETE RESTRICT: posted books must survive a tenant delete (you anonymize
    -- a tenant, you do NOT erase their journal history) — also blunts the
    -- pre-live-reset CASCADE hazard for any tenant that already has postings.
    tenant_id     UUID        REFERENCES tenants(id) ON DELETE RESTRICT,
    entry_date    DATE        NOT NULL DEFAULT (now() AT TIME ZONE 'Asia/Jakarta')::date,
    period        DATE        NOT NULL,                 -- month bucket = date_trunc('month')::date
    source_type   TEXT        NOT NULL,                 -- enum enforced below
    source_op_id  TEXT        NOT NULL,                 -- idempotency key from the source event
    memo          TEXT,
    created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  journal_entries IS 'Posted double-entry headers. Derived by replaying credit_ledger+payments+lots; idempotent per (source_type,source_op_id).';
COMMENT ON COLUMN journal_entries.period       IS 'Accounting period bucket (first day of month). Joins accounting_periods.';
COMMENT ON COLUMN journal_entries.source_op_id IS 'Idempotency key linking the posting to its source row so a replay never double-posts.';

-- FIX #2: enumerate source_type (idempotency-defeating typos now rejected).
-- Added as a named, separately-droppable constraint so re-running the migration
-- (table already exists) still installs it. Guarded against duplicate add.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'journal_entries_source_type_check') THEN
        ALTER TABLE journal_entries
            ADD CONSTRAINT journal_entries_source_type_check
            CHECK (source_type IN ('topup','consume','refund','breakage',
                                   'fx_settle','fx_reval','tax','manual'));
    END IF;
END $$;

-- FIX #4: period must be the canonical first-of-month bucket.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'journal_entries_period_first_of_month') THEN
        ALTER TABLE journal_entries
            ADD CONSTRAINT journal_entries_period_first_of_month
            CHECK (period = date_trunc('month', period)::date);
    END IF;
END $$;

-- one posting per source event (the heart of idempotent derive/replay).
-- AUDIT FIX (encoding/CRITICAL): the key must be tenant-SCOPED for tenant events
-- (else tenant B's posting with the same op_id as tenant A is silently dropped),
-- but still globally unique for company-level events (tenant_id IS NULL).
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_entries_source_tenant
    ON journal_entries (tenant_id, source_type, source_op_id) WHERE tenant_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_journal_entries_source_company
    ON journal_entries (source_type, source_op_id) WHERE tenant_id IS NULL;
CREATE INDEX IF NOT EXISTS idx_journal_entries_period
    ON journal_entries (period, entry_date);
-- per-tenant statements / deferred-revenue derivable from the ledger:
CREATE INDEX IF NOT EXISTS idx_journal_entries_tenant
    ON journal_entries (tenant_id, period) WHERE tenant_id IS NOT NULL;

-- =====================================================================
-- 3. JOURNAL LINES -- the debit/credit legs. Sum(debit) = Sum(credit) per entry,
--    enforced at COMMIT by a DEFERRABLE constraint trigger (so a multi-INSERT
--    transaction can build an unbalanced-then-balanced entry mid-flight).
-- =====================================================================
CREATE TABLE IF NOT EXISTS journal_lines (
    id           BIGINT       GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    entry_id     UUID         NOT NULL REFERENCES journal_entries(id) ON DELETE CASCADE,
    account_code TEXT         NOT NULL REFERENCES gl_accounts(code),
    debit_idr    NUMERIC(18,2) NOT NULL DEFAULT 0,
    credit_idr   NUMERIC(18,2) NOT NULL DEFAULT 0,
    source_ref   TEXT,          -- AUDIT FIX (encoding): line-level drill-back to the
                                -- source row (e.g. 'credit_ledger:<id>','payment:<id>',
                                -- 'lot:<id>') so the audit-trail report can trace a leg.
    memo         TEXT,
    -- AUDIT FIX (trigger/LOW): EXACTLY one positive side. The old NOT(both>0) check
    -- accepted a 0/0 phantom line (passed >=2-lines + balanced) — now rejected.
    CONSTRAINT journal_lines_one_side  CHECK ((debit_idr > 0) <> (credit_idr > 0)),
    CONSTRAINT journal_lines_nonneg    CHECK (debit_idr >= 0 AND credit_idr >= 0)
);
COMMENT ON TABLE journal_lines IS 'Debit/credit legs of a journal_entry. One side per row; balance enforced per entry at COMMIT.';

CREATE INDEX IF NOT EXISTS idx_journal_lines_entry   ON journal_lines (entry_id);
CREATE INDEX IF NOT EXISTS idx_journal_lines_account ON journal_lines (account_code);

-- -- Balance enforcement: Sum(debit) = Sum(credit) for every touched entry, at COMMIT --
-- DEFERRABLE INITIALLY DEFERRED so the engine can INSERT the legs one at a
-- time; the check fires once per touched entry_id when the tx commits.
CREATE OR REPLACE FUNCTION enforce_journal_balanced()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_entry  UUID;
    v_lines  BIGINT;
    v_debit  NUMERIC(18,2);
    v_credit NUMERIC(18,2);
BEGIN
    v_entry := COALESCE(NEW.entry_id, OLD.entry_id);
    -- entry may have been deleted (ON DELETE CASCADE) -- nothing to assert then.
    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = v_entry) THEN
        RETURN NULL;
    END IF;
    SELECT count(*), COALESCE(sum(debit_idr),0), COALESCE(sum(credit_idr),0)
      INTO v_lines, v_debit, v_credit
      FROM journal_lines
     WHERE entry_id = v_entry;
    -- AUDIT FIX (schema/HIGH): re-assert COMPLETENESS on every line DML, not just
    -- header DML — else DELETE-ing lines could strip a committed entry to 0/1 lines
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
$$;

DROP TRIGGER IF EXISTS trg_journal_lines_balanced ON journal_lines;
CREATE CONSTRAINT TRIGGER trg_journal_lines_balanced
    AFTER INSERT OR UPDATE OR DELETE ON journal_lines
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_journal_balanced();

-- -- FIX #1: header-completeness guard (no orphan / zero-line headers) --
-- The per-line balance trigger above is FOR EACH ROW on journal_lines, so a
-- journal_entries header with ZERO lines never fires it and commits silently
-- (Sum=0=0 is vacuously balanced). A derive engine that writes a header then
-- dies before emitting legs leaves a permanent orphan no constraint rejects.
-- This DEFERRABLE INITIALLY DEFERRED constraint trigger on journal_entries
-- asserts, at COMMIT, that every header has >=2 lines AND ties Sum(debit)=Sum(credit).
-- Deferred so the engine may INSERT the header first, then its legs, in one tx.
CREATE OR REPLACE FUNCTION enforce_journal_header_complete()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_lines  BIGINT;
    v_debit  NUMERIC(18,2);
    v_credit NUMERIC(18,2);
BEGIN
    -- header may have been deleted within the same tx -- nothing to assert then.
    IF NOT EXISTS (SELECT 1 FROM journal_entries WHERE id = NEW.id) THEN
        RETURN NULL;
    END IF;
    SELECT count(*), COALESCE(sum(debit_idr),0), COALESCE(sum(credit_idr),0)
      INTO v_lines, v_debit, v_credit
      FROM journal_lines
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
$$;

DROP TRIGGER IF EXISTS trg_journal_entries_complete ON journal_entries;
CREATE CONSTRAINT TRIGGER trg_journal_entries_complete
    AFTER INSERT OR UPDATE ON journal_entries
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW EXECUTE FUNCTION enforce_journal_header_complete();

-- -- FIX #4: closed-period lock (DB-enforced, not engine-only) --
-- Reject a posting whose period is marked 'closed' in accounting_periods.
-- This check is IMMEDIATE (NOT DEFERRABLE) and reads status AS-OF the INSERT,
-- NOT at COMMIT. That ordering is deliberate: a month-close transaction posts
-- its final adjusting entries (period still 'open' at that instant) and THEN
-- flips accounting_periods.status to 'closed' in the same tx -- the adjusting
-- entries pass because they were inserted while the period was still open. A
-- DEFERRED check would instead read the now-closed status at COMMIT and reject
-- the very entries the close is posting. After commit, any NEW posting into the
-- closed period is rejected at its own INSERT. A period with no
-- accounting_periods row is treated as OPEN (permissive) so first-touch of a
-- fresh month works before the close job has seeded the row.
CREATE OR REPLACE FUNCTION enforce_period_open()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_status TEXT;
BEGIN
    SELECT status INTO v_status FROM accounting_periods WHERE period = NEW.period;
    IF v_status = 'closed' THEN
        RAISE EXCEPTION
            'cannot post journal_entry % into CLOSED period %', NEW.id, NEW.period
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_journal_entries_period_open ON journal_entries;
CREATE TRIGGER trg_journal_entries_period_open
    BEFORE INSERT OR UPDATE OF period ON journal_entries
    FOR EACH ROW EXECUTE FUNCTION enforce_period_open();

-- AUDIT FIX (schema/CRITICAL + trigger/HIGH): the header guard above only covers
-- journal_entries INSERT / period-reassignment. A closed/audited month's GL could
-- still be RESTATED by INSERT/UPDATE/DELETE of journal_lines on an entry that
-- already sits in a closed period. Guard the lines too. Same as-of-statement read
-- semantics: the close tx posts its adjusting lines while the period is still open,
-- then flips status='closed', so legitimate close postings pass; afterwards any
-- line mutation in the closed period is rejected (postings are immutable once closed).
CREATE OR REPLACE FUNCTION enforce_period_open_lines()
RETURNS TRIGGER LANGUAGE plpgsql AS $$
DECLARE
    v_period DATE;
    v_status TEXT;
BEGIN
    SELECT period INTO v_period FROM journal_entries
     WHERE id = COALESCE(NEW.entry_id, OLD.entry_id);
    IF v_period IS NULL THEN          -- parent gone in same tx; nothing to guard
        RETURN COALESCE(NEW, OLD);
    END IF;
    SELECT status INTO v_status FROM accounting_periods WHERE period = v_period;
    IF v_status = 'closed' THEN
        RAISE EXCEPTION 'cannot modify journal_lines of a CLOSED period %', v_period
            USING ERRCODE = 'check_violation';
    END IF;
    RETURN COALESCE(NEW, OLD);
END;
$$;

DROP TRIGGER IF EXISTS trg_journal_lines_period_open ON journal_lines;
CREATE TRIGGER trg_journal_lines_period_open
    BEFORE INSERT OR UPDATE OR DELETE ON journal_lines
    FOR EACH ROW EXECUTE FUNCTION enforce_period_open_lines();

-- =====================================================================
-- 4. PAYMENTS -- cash-in events (Midtrans/Stripe/manual top-up). Source of
--    truth for E1 (DR Kas / CR Deferred-Rev + CR PPN Keluaran). Tenant-scoped.
--    ppn_idr = 0 pre-PKP (the 2200 leg then drops; dpp = gross).
--
--    FIX #3: gross_idr = dpp_idr + ppn_idr is now a CHECK (the DPP base must
--    reconcile to cash), and net_idr (when present) = gross_idr - fee_idr.
-- =====================================================================
CREATE TABLE IF NOT EXISTS payments (
    id              UUID        PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id       UUID        NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    provider        TEXT        NOT NULL,                       -- 'midtrans'|'stripe'|'manual'|'admin'
    external_ref    TEXT        UNIQUE,                         -- gateway txn id (idempotency vs double webhook)
    gross_idr       NUMERIC(18,2) NOT NULL,                     -- amount charged to customer (incl PPN)
    ppn_idr         NUMERIC(18,2) NOT NULL DEFAULT 0,           -- output VAT portion (0 pre-PKP)
    fee_idr         NUMERIC(18,2) NOT NULL DEFAULT 0,           -- gateway processing fee (-> 6200)
    -- AUDIT FIX (numeric/MEDIUM): dpp + net are GENERATED, not stored+CHECK'd. The
    -- old exact-equality CHECK (gross = dpp + ppn) on three independently-rounded
    -- NUMERICs could reject a legit e-faktur off by 0.01, and (net = gross - fee)
    -- wrongly rejected partial refunds. Deriving them makes the tie hold BY
    -- CONSTRUCTION — set gross/ppn/fee, dpp & net follow exactly. App must not
    -- insert dpp_idr/net_idr (generated).
    dpp_idr         NUMERIC(18,2) GENERATED ALWAYS AS (gross_idr - ppn_idr) STORED,  -- recognizable base
    net_idr         NUMERIC(18,2) GENERATED ALWAYS AS (gross_idr - fee_idr) STORED,  -- cash settled
    currency        TEXT        NOT NULL DEFAULT 'IDR',
    fx_rate         NUMERIC(18,6),                              -- idr_per_usd if charged in USD (else NULL)
    credits_granted BIGINT      NOT NULL DEFAULT 0,             -- credits this payment funded (-> lot)
    ledger_op_id    TEXT,                                       -- op_id of the credit_ledger topup row it produced
    status          TEXT        NOT NULL DEFAULT 'pending'
                        CHECK (status IN ('pending','paid','failed','refunded','partial_refund')),
    paid_at         TIMESTAMPTZ,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT now()
);
COMMENT ON TABLE  payments IS 'Cash-in events. Drives E1 posting (DR Kas / CR Deferred-Rev / CR PPN) and the paid credit_lot.';
COMMENT ON COLUMN payments.dpp_idr      IS 'Dasar Pengenaan Pajak = gross - ppn (GENERATED). Revenue base recognized on consumption; ties to cash by construction.';
COMMENT ON COLUMN payments.ledger_op_id IS 'Links the payment to the credit_ledger topup row (idempotent derive join).';

CREATE INDEX IF NOT EXISTS idx_payments_tenant ON payments (tenant_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_payments_status ON payments (status);
CREATE INDEX IF NOT EXISTS idx_payments_ledger_op ON payments (ledger_op_id) WHERE ledger_op_id IS NOT NULL;

-- =====================================================================
-- 5. CREDIT LOTS -- FIFO inventory of granted credits. PAID lots carry a
--    price_per_credit_idr (DPP/credits) and relieve deferred revenue on
--    spend (E2); FREE lots have price 0 and book ONLY acquisition cost on
--    spend (E3 -- the structural fix that makes free consumption impossible
--    to over-recognize). Lots are DERIVABLE from grant/topup ledger rows.
-- =====================================================================
CREATE TABLE IF NOT EXISTS credit_lots (
    id                   UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    -- AUDIT FIX (derive/CRITICAL): a monotonic seq gives FIFO a DETERMINISTIC
    -- tiebreak when two lots share granted_at — without it, two derive runs could
    -- order lots differently and recognize different revenue. FIFO = ORDER BY
    -- granted_at, lot_seq.
    lot_seq              BIGINT       GENERATED ALWAYS AS IDENTITY,
    tenant_id            UUID         NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
    source               TEXT         NOT NULL,                 -- 'topup'|'monthly_grant'|'signup_grant'|'daily_claim'|'period_grant'|'admin_adjust'
    is_paid              BOOLEAN      NOT NULL DEFAULT FALSE,   -- true -> revenue lot; false -> free/acquisition lot
    credits_granted      BIGINT       NOT NULL,
    credits_remaining    BIGINT       NOT NULL,
    price_per_credit_idr NUMERIC(14,4) NOT NULL DEFAULT 0,      -- DPP/credits for paid; 0 for free (E3); reference only
    -- AUDIT FIX (numeric/MEDIUM): store the lot's EXACT total recognizable revenue
    -- (the payment DPP) + revenue recognized so far, so the engine relieves the
    -- 2000 liability to EXACTLY zero on full spend (last spend = dpp_total -
    -- recognized), instead of credits x rounded-per-credit leaving residual dust.
    dpp_total_idr        NUMERIC(18,2) NOT NULL DEFAULT 0,      -- exact revenue to recognize over the lot (0 = free)
    recognized_idr       NUMERIC(18,2) NOT NULL DEFAULT 0,      -- revenue recognized so far (engine-maintained)
    fx_rate_at_grant     NUMERIC(18,6),                         -- kurs snapshot at grant (audit vs ENV drift)
    payment_id           UUID         REFERENCES payments(id) ON DELETE SET NULL,
    ledger_op_id         TEXT,                                  -- the credit_ledger op_id that created this lot
    granted_at           TIMESTAMPTZ  NOT NULL DEFAULT now(),
    expires_at           TIMESTAMPTZ,                           -- NULL = no expiry; drives breakage (E5)
    CONSTRAINT credit_lots_remaining_bounds CHECK (credits_remaining >= 0
                                               AND credits_remaining <= credits_granted),
    CONSTRAINT credit_lots_recognized_bounds CHECK (recognized_idr >= 0
                                               AND recognized_idr <= dpp_total_idr)
);
COMMENT ON TABLE  credit_lots IS 'FIFO inventory of granted credits. Paid lots relieve deferred revenue on spend (E2); free lots book only acquisition cost (E3).';
COMMENT ON COLUMN credit_lots.price_per_credit_idr IS 'Revenue recognized per credit on consumption (DPP/credits). 0 for free lots -- structural E4/audit-#2 over-recognition fix.';
COMMENT ON COLUMN credit_lots.ledger_op_id         IS 'Source credit_ledger op_id; makes lot derivation idempotent on replay.';

-- FIFO spend: oldest lot with remaining>0 first, per tenant. lot_seq breaks
-- granted_at ties deterministically (AUDIT FIX derive/CRITICAL).
CREATE INDEX IF NOT EXISTS idx_credit_lots_fifo
    ON credit_lots (tenant_id, granted_at, lot_seq)
    WHERE credits_remaining > 0;
CREATE INDEX IF NOT EXISTS idx_credit_lots_tenant ON credit_lots (tenant_id, granted_at DESC);
-- one lot per source ledger row (idempotent derive)
CREATE UNIQUE INDEX IF NOT EXISTS uq_credit_lots_ledger_op
    ON credit_lots (tenant_id, ledger_op_id) WHERE ledger_op_id IS NOT NULL;

-- =====================================================================
-- 6. FX RATES -- daily idr_per_usd used to translate USD provider COGS and
--    revalue payables (PSAK 10). GLOBAL reference data.
-- =====================================================================
CREATE TABLE IF NOT EXISTS fx_rates (
    currency_pair TEXT          NOT NULL,                       -- e.g. 'USD/IDR'
    rate_date     DATE          NOT NULL,
    idr_per_usd   NUMERIC(18,6) NOT NULL,
    source        TEXT,                                         -- 'BI'|'manual'|'env'|provider
    created_at    TIMESTAMPTZ   NOT NULL DEFAULT now(),
    PRIMARY KEY (currency_pair, rate_date)
);
COMMENT ON TABLE fx_rates IS 'Daily FX rates (idr_per_usd). Translates USD provider COGS + revalues payables (PSAK 10). KURS env constant remains the live default.';

-- =====================================================================
-- 7. ACCOUNTING PERIODS -- open/closed lock so a closed month cannot accrue
--    new postings. Now DB-enforced via the trg_journal_entries_period_open
--    trigger (FIX #4) in addition to the derive engine refusing to post.
-- =====================================================================
CREATE TABLE IF NOT EXISTS accounting_periods (
    period     DATE        PRIMARY KEY,                         -- first day of month
    status     TEXT        NOT NULL DEFAULT 'open'
                   CHECK (status IN ('open','closed')),
    closed_at  TIMESTAMPTZ,
    closed_by  TEXT
);
COMMENT ON TABLE accounting_periods IS 'Period open/close lock. A closed period rejects new postings (enforced by trigger AND the derive engine).';

-- =====================================================================
-- 8. TAX / FX SCAFFOLDING -- INERT now so Phase 4 (PKP) is switch-on with no
--    backfill. tenants gains billing/tax identity columns; tax_rates holds
--    the PPN/PPh rates; provider_invoices accrues + settles USD payables;
--    faktur_pajak / wht_ledger / deferred_revenue_snapshots are reporting
--    artefacts written only once the relevant feature is switched on.
-- =====================================================================

-- 8a. Tenant billing / tax identity (additive; all default-safe & nullable) ------
ALTER TABLE tenants
    ADD COLUMN IF NOT EXISTS country            TEXT  NOT NULL DEFAULT 'ID',
    ADD COLUMN IF NOT EXISTS npwp               TEXT,            -- NPWP (corporate / PKP)
    ADD COLUMN IF NOT EXISTS nik                TEXT,            -- NIK (individual, post-NIK-as-NPWP)
    ADD COLUMN IF NOT EXISTS pkp_status         BOOLEAN NOT NULL DEFAULT FALSE,  -- is this tenant a PKP?
    ADD COLUMN IF NOT EXISTS billing_legal_name TEXT,
    ADD COLUMN IF NOT EXISTS billing_address    JSONB NOT NULL DEFAULT '{}'::jsonb;
COMMENT ON COLUMN tenants.pkp_status IS 'Pengusaha Kena Pajak flag. Inert until ceritaAI itself is PKP; then drives faktur pajak issuance.';

-- 8b. Tax rates (PPN / PPh) -- versioned by effective_from -------------------------
CREATE TABLE IF NOT EXISTS tax_rates (
    tax_type       TEXT        NOT NULL,                         -- 'ppn'|'pph23'|'pph26'|'pph_final'|'pph_badan'
    rate           NUMERIC(6,4) NOT NULL,                        -- fraction, e.g. 0.1100 = 11%
    effective_from DATE        NOT NULL,
    note           TEXT,
    created_at     TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (tax_type, effective_from)
);
COMMENT ON TABLE tax_rates IS 'Versioned tax rates. Inert pre-PKP (no rows applied); the engine reads the row effective at posting time.';

-- 8c. Provider invoices -- accrue USD COGS, settle, realize FX (E7/E8) -------------
CREATE TABLE IF NOT EXISTS provider_invoices (
    id               UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    provider         TEXT         NOT NULL,                      -- 'google'|'openai'|'laozhang'|...
    period           DATE         NOT NULL,                      -- usage month accrued
    amount_usd       NUMERIC(18,2) NOT NULL,
    accrued_rate     NUMERIC(18,6),                              -- idr_per_usd when the payable was booked
    settled_rate     NUMERIC(18,6),                              -- idr_per_usd when actually paid
    settled_at       TIMESTAMPTZ,
    amount_idr_paid  NUMERIC(18,2),                              -- cash out at settlement (amount_usd * settled_rate)
    created_at       TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE provider_invoices IS 'Upstream USD payables. accrued vs settled rate drives realized FX (E7); period-close revaluation drives unrealized FX (E8).';

CREATE INDEX IF NOT EXISTS idx_provider_invoices_period   ON provider_invoices (period, provider);
CREATE INDEX IF NOT EXISTS idx_provider_invoices_unsettled ON provider_invoices (provider) WHERE settled_at IS NULL;

-- 8d. Faktur Pajak -- issued e-faktur per taxable payment (tenant-tagged) ----------
--     SECURITY (FIX #6): tenant-tagged but a GLOBAL VAT artefact, deliberately
--     left un-RLS'd. Therefore app_user must NOT hold SELECT on it (see part 10):
--     faktur reporting runs ONLY as the BYPASSRLS owner, which is the same
--     "owner reads all tenants for reporting" model used for the journal tables.
--     This closes the latent cross-tenant tax/PII (NSFP/DPP/buyer) read leak
--     that an un-RLS'd table + app_user SELECT would ship dormant.
CREATE TABLE IF NOT EXISTS faktur_pajak (
    id             UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id      UUID         REFERENCES tenants(id) ON DELETE SET NULL,
    payment_id     UUID         REFERENCES payments(id) ON DELETE SET NULL,
    nsfp_number    TEXT         UNIQUE,                          -- Nomor Seri Faktur Pajak
    dpp_idr        NUMERIC(18,2) NOT NULL DEFAULT 0,
    ppn_idr        NUMERIC(18,2) NOT NULL DEFAULT 0,
    kode_transaksi TEXT,                                         -- e-faktur transaction code (e.g. '04','08')
    issued_at      TIMESTAMPTZ,
    coretax_status TEXT,                                         -- coretax/e-faktur submission state
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE faktur_pajak IS 'Issued e-faktur (PPN). Inert until ceritaAI is PKP. tenant-tagged for buyer identity; OWNER-READ-ONLY (no app_user SELECT) so cross-tenant tax/PII cannot leak via an un-RLS''d global table.';

CREATE INDEX IF NOT EXISTS idx_faktur_pajak_payment ON faktur_pajak (payment_id);

-- 8e. WHT ledger -- PPh withheld on provider/affiliate payouts ---------------------
CREATE TABLE IF NOT EXISTS wht_ledger (
    id           UUID         PRIMARY KEY DEFAULT gen_random_uuid(),
    provider     TEXT         NOT NULL,
    usage_period DATE         NOT NULL,
    income_type  TEXT,                                           -- 'royalty'|'service'|... (treaty article)
    treaty_rate  NUMERIC(6,4),                                   -- P3B / tax-treaty reduced rate
    cor_on_file  BOOLEAN      NOT NULL DEFAULT FALSE,            -- Certificate of Residence (DGT-1) held?
    wht_idr      NUMERIC(18,2) NOT NULL DEFAULT 0,
    created_at   TIMESTAMPTZ  NOT NULL DEFAULT now()
);
COMMENT ON TABLE wht_ledger IS 'PPh 23/26 withheld on outbound (provider) payments. Inert until cross-border WHT is switched on; treaty_rate gated by cor_on_file.';

CREATE INDEX IF NOT EXISTS idx_wht_ledger_period ON wht_ledger (usage_period, provider);

-- 8f. Deferred-revenue snapshots -- posted-liability reconciliation trail ----------
-- This is what DEMOTES v_deferred_revenue (0029) from "the number" to a CHECK:
-- compare this snapshot (sum of credit_lots * price, or the 2000 GL balance)
-- against the view to catch drift.
CREATE TABLE IF NOT EXISTS deferred_revenue_snapshots (
    period              DATE         NOT NULL,
    outstanding_credits BIGINT       NOT NULL DEFAULT 0,
    deferred_idr        NUMERIC(18,2) NOT NULL DEFAULT 0,         -- posted 2000 liability at snapshot time
    taken_at            TIMESTAMPTZ  NOT NULL DEFAULT now(),
    PRIMARY KEY (period, taken_at)
);
COMMENT ON TABLE deferred_revenue_snapshots IS 'Period snapshot of the POSTED deferred-revenue liability (2000). Reconciliation check; demotes the v_deferred_revenue guess to a comparison.';

-- =====================================================================
-- 9. ROW-LEVEL SECURITY
--    Tenant-scoped (payments, credit_lots): ENABLE + FORCE + a policy with
--    BOTH USING and WITH CHECK -- the audit-#39 fix (credit_ledger shipped
--    USING-only, so a tenant could INSERT another tenant_id). These two
--    tables are fail-closed from birth.
--    Everything else here is GLOBAL owner-posted reference/ledger data with
--    no tenant_id partition (or only a tag); they get GRANTs, not RLS --
--    and faktur_pajak (which DOES carry a tenant tag) is kept owner-read-only
--    rather than RLS'd (FIX #6), so app_user never reads it at all.
-- =====================================================================
ALTER TABLE payments    ENABLE ROW LEVEL SECURITY;
ALTER TABLE payments    FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON payments;
CREATE POLICY tenant_isolation ON payments
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

ALTER TABLE credit_lots ENABLE ROW LEVEL SECURITY;
ALTER TABLE credit_lots FORCE  ROW LEVEL SECURITY;
DROP POLICY IF EXISTS tenant_isolation ON credit_lots;
CREATE POLICY tenant_isolation ON credit_lots
    USING      (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID)
    WITH CHECK (tenant_id = current_setting('app.current_tenant_id', TRUE)::UUID);

-- =====================================================================
-- 10. GRANTS to app_user.
--     Posting ledger + global reference tables are app-READ-ONLY: the derive
--     engine posts as the DB OWNER (bypasses RLS, sees all tenants), never as
--     app_user. Tenant-scoped artefacts are full DML (RLS confines them).
--
--     IMPORTANT: 0016 set ALTER DEFAULT PRIVILEGES ... GRANT SELECT,INSERT,
--     UPDATE,DELETE ON TABLES TO app_user, so every owner-created table here
--     is auto-granted FULL DML. A bare `GRANT SELECT` is additive and does NOT
--     strip that. To make the posted ledger genuinely unforgeable by app
--     traffic we must REVOKE the inherited write privileges first, then grant
--     SELECT. (REVOKE is idempotent -- re-running just no-ops.)
--
--     FIX #6: faktur_pajak gets NO grant at all -- its inherited DML is fully
--     REVOKE'd and SELECT is deliberately withheld, so app_user cannot read
--     cross-tenant faktur (NSFP / DPP / buyer identity) through an un-RLS'd
--     table. Owner-run reporting (BYPASSRLS) still sees everything.
-- =====================================================================
-- Tenant-scoped, RLS-confined -> full DML:
GRANT SELECT, INSERT, UPDATE, DELETE ON payments    TO app_user;
-- credit_lots: app may CREATE lots (grant path) + READ, but the FIFO draw-down
-- (UPDATE credits_remaining/recognized) is OWNER/engine-only so the app cannot
-- edit a lot to over-relieve deferred revenue (AUDIT FIX derive/LOW immutability).
REVOKE UPDATE, DELETE ON credit_lots FROM app_user;
GRANT  SELECT, INSERT ON credit_lots TO   app_user;

-- Global posted ledger + reference -> app reads only (owner writes).
-- Strip the auto-inherited write grants on EVERY table, then grant SELECT
-- only where app_user is allowed to read. faktur_pajak is intentionally
-- omitted from the SELECT list (FIX #6) -> owner-read-only.
REVOKE INSERT, UPDATE, DELETE, TRUNCATE ON
    journal_entries, journal_lines, fx_rates, accounting_periods, tax_rates,
    provider_invoices, faktur_pajak, wht_ledger, deferred_revenue_snapshots
    FROM app_user;
-- Belt-and-braces: also strip the inherited SELECT on faktur_pajak so the
-- "no grant" intent survives even if some prior blanket GRANT touched it.
REVOKE SELECT ON faktur_pajak FROM app_user;

GRANT SELECT ON journal_entries            TO app_user;
GRANT SELECT ON journal_lines              TO app_user;
GRANT SELECT ON fx_rates                   TO app_user;
GRANT SELECT ON accounting_periods         TO app_user;
GRANT SELECT ON tax_rates                  TO app_user;
GRANT SELECT ON provider_invoices          TO app_user;
GRANT SELECT ON wht_ledger                 TO app_user;
GRANT SELECT ON deferred_revenue_snapshots TO app_user;
-- (faktur_pajak: NO GRANT -- owner-read-only, FIX #6.)

-- journal_lines uses an IDENTITY column -> app_user must NOT be able to write,
-- and the implicit sequence carries no separate grant to worry about (owner-only).

COMMIT;
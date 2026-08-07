-- =====================================================================
-- 0084_credited_anchor_uniqueness.sql
--
-- L2C TRANCHE 2: one credited payment anchor per (provider, provider_payment_id).
--
-- WHAT IS BROKEN
--   payment_events carries exactly one uniqueness rule today, from 0032:
--       UNIQUE (provider, idempotency_key)
--   That is RECEIPT identity — dodo: the webhook-id, midtrans: the order_id. It is NOT
--   payment-anchor identity, which is (provider, provider_payment_id). Nothing has ever
--   stopped TWO credited rows from existing for ONE provider payment, and a tranche-1 test
--   fixture demonstrated the schema permits exactly that.
--
--   The consequence is a silent under-reversal. payment_event_for_reversal (0043) resolves
--   the row to claw back with:
--       ORDER BY created_at DESC LIMIT 1
--   so with two credited rows it picks the NEWER and the older row's credits_granted is
--   never reversed. A refund then claws back less than was granted, the ledger balances
--   against a row nobody will look at again, and nothing errors.
--
-- WHAT THIS DOES
--   Adds a PARTIAL unique index over the anchor, scoped to rows that actually assert a
--   credit against a real provider payment.
--
--   WHY PARTIAL, and why each predicate is load-bearing:
--     * `credited` — an uncredited row is not an anchor. A pending/failed/expired row, or a
--       row still waiting to be flipped by topup_grant, legitimately shares a payment id with
--       the credited row that eventually settles it. Constraining all rows would reject
--       ordinary lifecycles. Only the CREDITED assertion must be unique.
--     * `provider_payment_id IS NOT NULL` — the column is nullable and most historical rows
--       leave it NULL. In Postgres NULLs never collide in a unique index, so this predicate
--       is not strictly required for correctness; it is stated anyway so the index does not
--       carry rows it can never constrain, and so the intent is readable at the schema level.
--
--   This is a CONSTRAINT, not a repair. It stops the second credited anchor from being
--   written. It deliberately does NOT merge, delete, or rewrite any existing row: those are
--   financial records, and 0071 already settled that unprovable rows are left alone rather
--   than inferred or plugged.
--
-- WHY IT FAILS CLOSED INSTEAD OF CLEANING UP
--   The guard below aborts the whole migration if a duplicate credited anchor already
--   exists, and names the offending pairs. A migration must not silently pick a winner
--   between two credited financial rows — which one is "right" is an accounting decision
--   with a real claw-back consequence, and it belongs to a human with the provider's records
--   in hand, not to a DDL script. Because migrate.js wraps every file in its own
--   transaction, the RAISE rolls the whole thing back and the index is not created.
--
--   Note the index itself would fail on duplicates anyway, with a bare 23505 naming no rows.
--   The guard exists to turn that into a message an operator can act on.
--
-- WHY NUMBER 0084 (not 0075)
--   Every mechanical check reports 0075 free and every one of them is misleading in the same
--   way 0072 was. 0075–0083 are allocated by L2C PLAN-044, a document in WIMBA_CONT_PROJECT/
--   — OUTSIDE this repository — so no `ls`, no `git` scan and no query of the production
--   `migrations` table can see the claim. PLAN-044's block is held rather than reclaimed even
--   though PLAN-044/MATRIX-044 are still DRAFT FOR RATIFICATION: a draft that is later
--   ratified must not find its numbers taken. Recorded in database/migrations/RESERVED.md,
--   anchored on branch fix/l2c-tranche2-anchor-uniqueness.
--
-- SCOPE OF THE FREE-NUMBER SEARCH, 2026-08-07 (per RESERVED.md's closing rule)
--   deploy branch feat/subscription-global @104d238c: 67 migrations, highest 0074 ·
--   production migrations table, read-only: 67 rows, highest 0074_platform_qc_metering.sql ·
--   all 17 remote heads enumerated, not sampled: no 0075–0084 on any of them ·
--   RESERVED.md: only 0072 reserved before today · PLAN-044: 0075–0083.
--
-- NOT IN SCOPE
--   Refund/dispute mutation semantics stay UNRESOLVED and untouched — a failed refund must
--   not reverse credits and a dispute lifecycle is not repeated negative mutations. Nothing
--   here invents a policy for either. This migration constrains identity only.
-- =====================================================================

-- ── Bounded lock acquisition ────────────────────────────────────────────────
-- CREATE INDEX takes a SHARE lock on payment_events, which conflicts with any concurrent
-- write. On a live billing table an unbounded wait is the wrong failure mode: it would queue
-- webhook writes behind the migration for as long as the lock is contended. Fail fast and let
-- the operator re-run in a quieter moment instead. SET LOCAL — scoped to migrate.js's own
-- transaction, reverted on COMMIT/ROLLBACK, never leaked to a pooled session.
SET LOCAL lock_timeout = '5s';
SET LOCAL statement_timeout = '30s';

-- ── Guard: refuse to proceed if the defect already has victims ──────────────
-- DELIBERATELY IDENTIFIER-FREE. An aborting migration's message lands in deploy logs,
-- shipped log sinks and terminal scrollback, none of which are the right home for provider
-- payment identifiers. The counts prove the condition; the HINT gives the operator the exact
-- query to enumerate offenders themselves, inside the database, under their own privilege.
DO $$
DECLARE
  v_pairs INT;
  v_rows  INT;
  v_provs TEXT;
BEGIN
  SELECT count(*), COALESCE(sum(n), 0),
         COALESCE(string_agg(DISTINCT provider, ', ' ORDER BY provider), '')
    INTO v_pairs, v_rows, v_provs
    FROM (
      SELECT provider, provider_payment_id, count(*) AS n
        FROM public.payment_events
       WHERE credited
         AND provider_payment_id IS NOT NULL
       GROUP BY provider, provider_payment_id
      HAVING count(*) > 1
    ) d;

  IF v_pairs > 0 THEN
    RAISE EXCEPTION
      'L2C 0084 ABORT: % provider payment(s) already carry more than one CREDITED payment_events row (% rows total, provider(s): %). This migration will not choose a winner between credited financial rows. Resolve each against the provider''s own records first, then re-run.',
      v_pairs, v_rows, v_provs
      USING ERRCODE = 'raise_exception',
            HINT = 'Enumerate them yourself (identifiers are deliberately not logged): SELECT provider, provider_payment_id, count(*), array_agg(id ORDER BY created_at) FROM payment_events WHERE credited AND provider_payment_id IS NOT NULL GROUP BY 1,2 HAVING count(*) > 1;';
  END IF;
END $$;

-- ── The constraint ──────────────────────────────────────────────────────────
-- Plain CREATE INDEX, not CONCURRENTLY: migrate.js runs every file inside one
-- transaction and CONCURRENTLY cannot run there. The table is small and, with the
-- lock_timeout above, the wait is bounded.
--
-- DEPLOY ORDER IS SOURCE FIRST, THEN THIS MIGRATION — the reverse of the usual rule, on
-- purpose. The index is a constraint only the NEW code handles gracefully: tranche-1 code
-- calls recordCreditedPaymentEvent with ON CONFLICT on the RECEIPT arbiter only, so a benign
-- concurrent redelivery that reaches this non-arbiter index first would surface as a raw
-- 23505 and 500 the webhook. Applying the index while old code is still serving is therefore
-- an old-code/new-index race. New code is compatible with BOTH schemas — without the index
-- its 23505 branch is simply never reached — so source-then-schema has no window at all.
CREATE UNIQUE INDEX IF NOT EXISTS payment_events_credited_anchor_uniq
    ON public.payment_events (provider, provider_payment_id)
    WHERE credited AND provider_payment_id IS NOT NULL;

COMMENT ON INDEX payment_events_credited_anchor_uniq IS
  'L2C tranche 2. Payment-ANCHOR identity: at most one CREDITED payment_events row per (provider, provider_payment_id). Distinct from the 0032 UNIQUE (provider, idempotency_key), which is RECEIPT identity (dodo webhook-id / midtrans order_id) — the two must never be collapsed. Partial on `credited` because uncredited rows for the same payment are a legitimate lifecycle, not a duplicate anchor.';

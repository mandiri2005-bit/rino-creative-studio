-- =====================================================================
-- 0035_reversal_events.sql
-- Capture the FULL refund/dispute event payload on the payment row so the
-- accounting engine can recognize precise contra-revenue under the NET model
-- (MSA §9.7: Dodo retains its fee on refund → the clawback ≠ the net revenue,
-- so contra-revenue needs the ACTUAL refund settlement figures, not gross).
--
-- payment_events.raw_event holds the original PAYMENT payload (refunds:[] at
-- grant time). reversal_events is an append-only array of the refund/dispute
-- event payloads (refund_id, settlement_amount/currency, total_amount/currency,
-- fee/tax, dispute details) added by reverse_entitlement when it reverses.
--
-- 0034 is already applied to staging — fix forward (never edit an applied file).
-- =====================================================================

BEGIN;

ALTER TABLE payment_events
  ADD COLUMN IF NOT EXISTS reversal_events JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN payment_events.reversal_events IS 'Append-only array of refund/dispute event payloads (Dodo Refund/Dispute object or Midtrans refund/chargeback notification) for NET contra-revenue. Captured by reverse_entitlement.';

COMMIT;

# `payment_events` — accounting seam reference (Phase-2 contract)

Authoritative contract for the accounting engine to consume the dual-rail
(Dodo + Midtrans) gateway. Frozen — the payment rail will not change these
without coordinating. Source of truth = migrations `0032` / `0034` / `0035` on
branch `feat/dodo-payments`. (Stripe keeps its own `subscriptions` /
`processed_stripe_events` / `credit_ledger` paths — untouched.)

## Table schema (post-0035)

| column | type | notes |
|---|---|---|
| `id` | uuid pk | |
| `tenant_id` | uuid NOT NULL | → tenants |
| `user_id` | uuid | nullable |
| `provider` | text NOT NULL | `'dodo'` \| `'midtrans'` |
| `idempotency_key` | text NOT NULL | Dodo = Standard-Webhooks `webhook-id`; Midtrans = `order_id`. **UNIQUE(provider, idempotency_key)** |
| `provider_payment_id` | text | Dodo = `data.payment_id`; Midtrans = `transaction_id` |
| `plan_key` | text | `'starter'` \| `'pro'` \| `'studio'` |
| `amount` | bigint | **GROSS, smallest unit** — see units below. Audit only |
| `currency` | text | charge currency |
| `status` | text NOT NULL | `pending`→`succeeded`→(`refunded`\|`disputed`); also `failed`\|`cancelled`\|`expired` |
| `credited` | bool NOT NULL | true once credits granted (exactly-once) |
| `credits_granted` | int | credits issued on success |
| `raw_event` | jsonb NOT NULL | the **PAYMENT** payload (Dodo Payment object / Midtrans settlement notif) |
| `reversed_at` | timestamptz | set when a refund/chargeback reversed the grant |
| `reversal_events` | jsonb NOT NULL `[]` | **append-only array** of refund/dispute event payloads |
| `created_at`, `updated_at` | timestamptz | |

## Money & units (CRITICAL for recognition)

- **`amount`** = gross charged to buyer, in the **smallest unit of `currency`** (×100):
  - Midtrans: whole IDR ×100 conceptually, but stored as the IDR integer Midtrans sends (e.g. `199000` = IDR 199.000). Treat as IDR.
  - Dodo: minor units (e.g. `184204733` = IDR 1.842.047,33 → ÷100).
- **NET revenue (Rino's decision: Dodo = NET / supplier-to-reseller)** comes from `raw_event` of a Dodo payment, NOT `amount`:
  - `raw_event.data.settlement_amount` = net credited to ceritaAI's Dodo balance, in **smallest unit of** `raw_event.data.settlement_currency` (e.g. `9900` = **USD 99.00**).
  - `raw_event.data.settlement_currency` may be **USD even when the buyer paid IDR** → **FX → IDR at txn date (PSAK 10)** via your fx_rates.
  - Also present: `data.total_amount` (gross), `data.tax`, `data.settlement_tax`, `data.discounts`/`discount_id`, `data.refunds`/`refund_status`, `data.subscription_id`, `data.invoice_id`/`invoice_url`, `data.payment_provider`, `data.card_network`.
  - Supplier Fee (Dodo) = gross − settlement (compared in the same currency).
- Midtrans NET: notif carries gross only; MDR fee comes from Midtrans settlement reports (separate, your side).

## Join to `credit_ledger` (the grant/contra ledger)

Every credit movement from the rail is a `credit_ledger` row:

- **Grant** (on success): `reason='topup'`, **`op_id = '${provider}:${idempotency_key}'`** (e.g. `midtrans:ceritai-…`, `dodo:<webhook-id>`), `metadata = {provider, plan_key, provider_payment_id, idempotency_key}`, `delta = +credits`.
- **Reversal** (refund/chargeback): `reason='refund'`, **`op_id = 'refund:${provider}:…'`** or `'dispute:${provider}:…'`, `delta = −credits`, `metadata = {provider, kind:'refund'|'chargeback', reversal_of, payment_event_id}`.
- Join: split `credit_ledger.op_id` on `:` → `(provider, idempotency_key)` → match `payment_events`. (Reversal op_ids are prefixed `refund:`/`dispute:`.)

## Refund / chargeback (NET contra-revenue)

- Status flips to `refunded`/`disputed`, `reversed_at` set, a negative `credit_ledger` row is written.
- **`reversal_events`** = append-only array of the **actual refund/dispute event payloads** (Dodo `Refund`/`Dispute` object, Midtrans refund/chargeback notif) → read `refund_id`, refund `settlement_amount`/`settlement_currency`, fee/tax, dispute detail here. Needed because under MSA §9.7 Dodo retains its fee on refund → clawback ≠ net revenue.
- Idempotent: a re-delivered refund does NOT re-append or double-reverse.

## Access / RLS

- `payment_events`, `credit_ledger`, `credit_balances` are **RLS ENABLE + FORCE** with `tenant_isolation` (predicate `tenant_id = current_setting('app.current_tenant_id', TRUE)::uuid`).
- The accounting engine reads **cross-tenant** → connect as a **BYPASSRLS owner role** (same as it reads credit_ledger/journal), then it reads `payment_events` directly. No per-tenant context needed.
- The rail's own writes go through `credit_apply()` (SECURITY INVOKER) as `app_user` (NOBYPASSRLS) with `app.current_tenant_id` set first → satisfies WITH CHECK. (Accounting's 0033 explicit WITH CHECK = same predicate the 0023 USING already enforces → no rail change.)
- SECURITY DEFINER helpers (app_user-callable, for the webhook handlers, NOT needed by the engine): `payment_event_lookup(provider, idempotency_key)`, `payment_event_for_reversal(provider, idempotency_key, provider_payment_id)`, `stale_pending_payments(provider, older_than_minutes)`.

## Provenance / merge

Migrations `0032_payment_events`, `0034_payment_reversal`, `0035_reversal_events`
live on `feat/dodo-payments`. Agreed merge order: **dodo → main first**, then
`feat/accounting-foundation` (which carries 0031 + 0033). Merge-test before prod:
re-run `0001→0035` on staging + smoke crediting/refund as `app_user` under 0033's
WITH CHECK.

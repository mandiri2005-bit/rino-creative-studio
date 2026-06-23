# Dual-rail payments — sandbox/test plan (Dodo Test Mode + Midtrans Sandbox)

"Live test in sandbox": real flow, fake funds, landing on ONE shared credit
balance. No real money. Stripe stays live & independent throughout.

## Pre-reqs (manual — Rino)

Both webhooks need a public HTTPS URL. Prefer **Railway staging** (stable URL).
If local: tunnel the **nginx** port (not Node directly) so routing matches prod;
ensure the raw body survives for Dodo's `unwrap()`. Cloudflare named tunnel =
stable hostname; ngrok = quickest but URL changes per restart (re-register both).

Set env on **staging** (never commit). Two INDEPENDENT toggles:

```
# Dodo (Test Mode)
DODO_PAYMENTS_API_KEY=test_...
DODO_PAYMENTS_WEBHOOK_SECRET=whsec_<test endpoint secret>
DODO_ENVIRONMENT=test_mode
DODO_PRODUCT_ID_STARTER/PRO/STUDIO=prod_<test ids>
# Midtrans (Sandbox)
MIDTRANS_IS_PRODUCTION=false
MIDTRANS_SERVER_KEY=<SANDBOX server key>
MIDTRANS_CLIENT_KEY=<SANDBOX client key>
APP_BASE_URL=https://<staging-domain>
```

- Dodo dashboard (**Test Mode**): create Starter/Pro/Studio products → copy
  `product_id`s to env; Webhooks → Add Endpoint = `<PUBLIC_URL>/dodo/webhook`,
  subscribe `payment.succeeded`/`payment.failed` (+refund) → copy signing secret.
- Midtrans dashboard (**Sandbox**): Settings → Access Keys → copy Sandbox keys;
  Settings → Configuration → Payment Notification URL = `<PUBLIC_URL>/midtrans/notification`;
  enable QRIS / VA / GoPay.

## Setup (Claude can run once you say go — NOT yet, gated)

```
cd backend && npm install                 # pulls dodopayments + midtrans-client
node database/migrate.js                   # against the STAGING DB branch, NOT prod
node --test ../tests/node/payments_core.test.mjs ../tests/node/dodo.test.mjs ../tests/node/midtrans.test.mjs
```

## Checklist

### Dodo (Test Mode)
| # | Step | Expected | Pass |
|---|------|----------|------|
| 1 | `POST /payments/dodo/create-checkout` `{plan_key:"pro"}` (Clerk auth) | returns Dodo hosted `url` | ☐ |
| 2 | Pay with a Dodo **test card** | `payment.succeeded` fires | ☐ |
| 3 | `/dodo/webhook` receives it | SDK `unwrap` verifies; `grant_entitlement` runs | ☐ |
| 4 | Check balance | increased by `creditsForPlan("pro")` for the right tenant | ☐ |
| 5 | Re-deliver same `webhook-id` | credited **once** (`applied:false` 2nd time) | ☐ |
| 6 | Tamper signature / stale `webhook-timestamp` | **403**, no grant | ☐ |

### Midtrans (Sandbox)
| # | Step | Expected | Pass |
|---|------|----------|------|
| 7 | `POST /payments/midtrans/create-transaction` `{plan_key:"starter"}` (Clerk auth) | returns Snap `token` + `clientKey`; a `pending` row exists | ☐ |
| 8 | Snap popup (sandbox snap.js) → pay via **simulator.sandbox.midtrans.com** (QRIS/VA) | `/midtrans/notification` hit, `transaction_status=settlement` | ☐ |
| 9 | Notification verified | SHA512 matches; `grant_entitlement` runs | ☐ |
| 10 | Check balance | increased by `creditsForPlan("starter")` for the right tenant | ☐ |
| 11 | Replay same `settlement` notification | credited **once** | ☐ |
| 12 | Tamper `signature_key`; or `pending`/`expire` status | **403** (tamper) / no grant (non-settlement) | ☐ |

### Shared-balance proof (the key test — Phase E)
| # | Step | Expected | Pass |
|---|------|----------|------|
| 13 | ONE tenant: a Midtrans Sandbox top-up **and** a Dodo Test top-up | both land on the SAME `credit_balances` row and **SUM** correctly — not doubled, not overwritten, not split | ☐ |

> Step 13 is what proves `grant_entitlement` + `credit_apply` are the single shared
> core. Verify with `SELECT delta, reason, op_id, metadata->>'provider' FROM credit_ledger WHERE tenant_id=… ORDER BY created_at;`
> — expect two `topup` rows, op_ids `dodo:…` and `midtrans:…`, balance = sum.

## Out of scope (this pass)
Production/live flip (only after Midtrans Business Review approved AND Dodo Bank
Verification = Verified); refund credit-reversal; subscription auto-renewal;
retiring Stripe (Phase F, separate gated PR); frontend rail selection (Phase G).

#!/usr/bin/env bash
# Dual-rail create-endpoint smoke (Phase B/D trigger). Hits the two Clerk-authed
# create endpoints and prints the checkout URL / Snap token. It does NOT pay —
# paying is done by Rino in the browser (Dodo test card / Midtrans sandbox sim).
#
# Usage:
#   BASE_URL=https://<staging-or-prod-node-service> \
#   CLERK_TOKEN=<a valid Clerk session JWT> \
#   ./tests/dual-rail-smoke.sh [plan_key]      # plan_key: starter|pro|studio (default pro)
#
# Get CLERK_TOKEN: log into the app, devtools → Application/Network → copy the
# Bearer token sent on an authed /api/* request (or Clerk.session.getToken()).
set -euo pipefail
: "${BASE_URL:?set BASE_URL to the deployed Node service (e.g. https://app.ceritai.com)}"
: "${CLERK_TOKEN:?set CLERK_TOKEN to a valid Clerk session JWT}"
PLAN="${1:-pro}"

echo "→ POST /payments/dodo/create-checkout  {plan_key:$PLAN}"
curl -sS -X POST "$BASE_URL/payments/dodo/create-checkout" \
  -H "Authorization: Bearer $CLERK_TOKEN" -H "Content-Type: application/json" \
  -d "{\"plan_key\":\"$PLAN\"}"; echo; echo

echo "→ POST /payments/midtrans/create-transaction  {plan_key:$PLAN}"
curl -sS -X POST "$BASE_URL/payments/midtrans/create-transaction" \
  -H "Authorization: Bearer $CLERK_TOKEN" -H "Content-Type: application/json" \
  -d "{\"plan_key\":\"$PLAN\"}"; echo; echo

echo "Next (Rino): open the Dodo 'url' and the Midtrans 'redirectUrl' in a browser and pay."
echo "Then CC runs the read-only payment_events verification query."

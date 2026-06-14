#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
admin_credits.py — manual credit administration (bootstrap, no Stripe yet).

Grant or inspect credits for a tenant without a paid top-up. Runs inside the
python-api container (it needs the same Redis + Postgres the app uses), so it
goes through the SAME durable layer (credit_apply + credit_ledger) and Redis
cache — fully consistent with live metering.

Usage (from the host):
  docker exec rino-creative-studio-python-api-1 \\
      python /app/admin_credits.py grant --email user@example.com --credits 5000
  docker exec rino-creative-studio-python-api-1 \\
      python /app/admin_credits.py grant --tenant <uuid> --credits 5000 --op-id launch-promo
  docker exec rino-creative-studio-python-api-1 \\
      python /app/admin_credits.py balance --email user@example.com

Notes:
  • --op-id makes a grant idempotent (re-running with the same id won't double-
    credit) — use it for promo codes. Omit it and every run grants again.
  • --reason ∈ admin_adjust (default) | topup | monthly_grant.
  • On Railway: `railway run python /app/admin_credits.py ...` (or the service shell).
"""
import argparse
import asyncio
import sys

import database as db
import redis_client as rc
import credits


async def _resolve_tenant(args) -> str | None:
    if args.tenant:
        return args.tenant
    if args.email:
        # tenant_id_by_email() is SECURITY DEFINER (migration 0024) so the
        # app role may look across tenants for this one lookup.
        tid = await db._q_fetchval("SELECT tenant_id_by_email($1)", args.email, tenant="")
        return str(tid) if tid else None
    return None


async def run(args) -> int:
    await db.init_db()
    await rc.init_redis()
    try:
        tid = await _resolve_tenant(args)
        if not tid:
            print("✖ tenant not found — pass --tenant <uuid> or --email <addr>", flush=True)
            return 2

        if args.cmd == "balance":
            live = await credits.get_balance(tid)
            durable = await credits.durable_balance(tid)
            rep = await credits.reconcile(tid)
            print(f"tenant   = {tid}")
            print(f"balance  = {live} (live)  {durable} (durable)")
            print(f"reconcile= {rep}")
            return 0

        # grant
        reason = args.reason if args.reason in ("admin_adjust", "topup", "monthly_grant") else "admin_adjust"
        if args.credits == 0:
            print("✖ --credits must be non-zero", flush=True)
            return 2
        bal = await credits.grant(tid, args.credits, reason=reason, op_id=args.op_id,
                                  metadata={"source": "admin_cli"})
        print(f"✔ granted {args.credits:+d} credits ({reason}"
              f"{', op_id='+args.op_id if args.op_id else ''}) → tenant={tid} balance={bal}", flush=True)
        return 0
    finally:
        await rc.close_redis()
        await db.close_db()


def main() -> int:
    ap = argparse.ArgumentParser(description="Manual credit admin (no Stripe).")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("grant", "balance"):
        p = sub.add_parser(name)
        g = p.add_mutually_exclusive_group(required=True)
        g.add_argument("--email", help="user email")
        g.add_argument("--tenant", help="tenant_id (uuid)")
        if name == "grant":
            p.add_argument("--credits", type=int, required=True,
                           help="credits to add (use a negative number to deduct)")
            p.add_argument("--op-id", dest="op_id", default=None,
                           help="stable id → idempotent grant (e.g. a promo code)")
            p.add_argument("--reason", default="admin_adjust",
                           help="admin_adjust (default) | topup | monthly_grant")
    args = ap.parse_args()
    return asyncio.run(run(args))


if __name__ == "__main__":
    sys.exit(main())

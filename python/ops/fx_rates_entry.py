"""
fx_rates_entry.py — the operational entry path for FX rates.

NORMATIVE SOURCE: `PLAN-046 §A/S10.G3.3-c(9)`.

    "Operational entry. `python/ops/fx_rates_entry.py`; no automated scraper in
     V1. Opens an explicit READ COMMITTED transaction, SET ROLE
     fx_rates_writer, then exactly ONE function call per transaction.
     Correction policy: a rate may be corrected while unused (audited); once
     any committed lot references it, it is immutable and a wrong rate is
     handled as an accounting-correction procedure outside L2C — never by
     retro-pricing."

Three things this module exists to guarantee, none of which the database can
enforce on its own:

1. **NO AUTOMATED SCRAPER.** Every rate is typed in by a human who has looked
   at the publication. There is no fetch, no URL, no schedule. `source_ref`
   names what they looked at.

2. **ONE CALL PER TRANSACTION.** `fx_rates_correct()` enforces one *correction*
   per transaction through `UNIQUE (correction_xid)`, but nothing in the
   database stops an operator batching twenty `fx_rates_enter()` calls into one
   transaction and losing the lot on a single failure. This module opens the
   transaction, makes exactly one call, and commits.

3. **NORMALISATION HAPPENS HERE, AND SHOWS ITS WORK.** `fx_rates_enter` takes a
   rate that is ALREADY IDR per ONE major unit — that is the whole point of the
   `idr_per_major_unit` rename (`G3.3-c(7)`). A Bank Indonesia transaction-rate
   series quoted per 100 units (JPY is the usual case) must be divided by its
   `Nilai` before it is entered. `normalize_bi_mid()` does that in exact
   decimal and prints the arithmetic, so the operator can check it against the
   published table rather than trusting a number that appeared from nowhere.

USAGE — the rate is never passed as an unexplained scalar for a BI mid series:

    python3 python/ops/fx_rates_entry.py enter \
        --pair USD/IDR --rate-date 2026-08-07 --rate 16250.000000 \
        --source jisdor --source-ref 'JISDOR publication 2026-08-07'

    python3 python/ops/fx_rates_entry.py enter \
        --pair JPY/IDR --rate-date 2026-08-07 \
        --jual 155000 --beli 154000 --nilai 100 \
        --source bi_transaction_mid --source-ref 'BI transaction rates 2026-08-07'

    python3 python/ops/fx_rates_entry.py correct \
        --pair USD/IDR --rate-date 2026-08-07 --new-rate 16251.000000 \
        --new-source jisdor --new-source-ref 'JISDOR revised 2026-08-07' \
        --reason 'transcription error, digit transposed' --i-have-checked-no-lot-uses-it

The connection is the established privileged operator connection
(`db-backup` / `BACKUP_DATABASE_URL`), which holds
`GRANT fx_rates_writer TO <ops role> WITH INHERIT FALSE, SET TRUE`: the
privilege exists only inside the deliberate `SET ROLE` this module performs,
never ambiently.
"""
from __future__ import annotations

import argparse
import asyncio
import datetime
import os
import sys
from decimal import Decimal, ROUND_HALF_UP

try:
    import asyncpg
except ImportError as exc:  # pragma: no cover - environment defect
    raise RuntimeError(
        "fx_rates_entry requires asyncpg, the driver the product already depends on "
        "(python/database.py)."
    ) from exc

WRITER_ROLE = "fx_rates_writer"
RATE_DP = Decimal("0.000001")          # NUMERIC(18,6)
SOURCES = ("jisdor", "bi_transaction_mid", "kmk_tax", "manual")


class OperatorError(RuntimeError):
    """A refusal raised before anything touches the database."""


# ─────────────────────────────────────────────────────────────────────────────
# Normalisation
# ─────────────────────────────────────────────────────────────────────────────
def normalize_bi_mid(jual, beli, nilai, *, echo=True) -> Decimal:
    """IDR per ONE major unit from a Bank Indonesia transaction-rate row.

        idr_per_major_unit = ((jual + beli) / 2) / Nilai

    `Nilai` is the quotation unit of the published series: 1 for USD, 100 for
    JPY. Dividing by it is what makes the stored figure per-ONE-unit, and
    skipping it stores a number 100x too large that every downstream valuation
    would then use silently. Exact decimal throughout, one terminal half-up
    rounding to 6 dp.
    """
    jual, beli, nilai = Decimal(str(jual)), Decimal(str(beli)), Decimal(str(nilai))
    if nilai <= 0:
        raise OperatorError(f"Nilai must be > 0 (got {nilai})")
    if jual <= 0 or beli <= 0:
        raise OperatorError(f"jual and beli must both be > 0 (got {jual}, {beli})")

    mid = (jual + beli) / Decimal(2)
    per_unit = (mid / nilai).quantize(RATE_DP, rounding=ROUND_HALF_UP)

    if echo:
        print(f"  BI transaction mid, normalised to ONE major unit:")
        print(f"    mid          = ({jual} + {beli}) / 2 = {mid}")
        print(f"    per 1 unit   = {mid} / {nilai} = {per_unit}")
        if nilai != 1:
            print(f"    NOTE: the published series is quoted per {nilai} units; "
                  f"entering {mid} unnormalised would overstate it {nilai}x.")
    return per_unit


def _validate(pair: str, rate_date: str, source: str, source_ref: str) -> datetime.date:
    if not pair or len(pair) != 7 or pair[3] != "/" or not pair.isupper():
        raise OperatorError(f"pair must look like 'USD/IDR' (got {pair!r})")
    if not pair.endswith("/IDR"):
        raise OperatorError(f"only IDR-quoted pairs are storable (got {pair!r})")
    if source not in SOURCES:
        raise OperatorError(f"source must be one of {SOURCES} (got {source!r})")
    if not source_ref or not source_ref.strip():
        raise OperatorError(
            "source_ref is required and names the publication you actually looked at. "
            "A rate with no citable source is not admissible as a valuation input."
        )
    try:
        return datetime.date.fromisoformat(rate_date)
    except ValueError as exc:
        raise OperatorError(f"rate_date must be ISO YYYY-MM-DD (got {rate_date!r})") from exc


# ─────────────────────────────────────────────────────────────────────────────
# The two operations. One function call per transaction, both of them.
# ─────────────────────────────────────────────────────────────────────────────
async def enter_rate(conn, pair, rate_date, rate, source, source_ref):
    """ONE `fx_rates_enter` call inside ONE explicit READ COMMITTED transaction."""
    day = _validate(pair, rate_date, source, source_ref)
    rate = Decimal(str(rate)).quantize(RATE_DP, rounding=ROUND_HALF_UP)
    if rate <= 0:
        raise OperatorError(f"rate must be > 0 (got {rate})")

    async with conn.transaction(isolation="read_committed"):
        await conn.execute(f"SET LOCAL ROLE {WRITER_ROLE}")
        row = await conn.fetchrow(
            "SELECT * FROM public.fx_rates_enter($1,$2,$3,$4,$5)",
            pair, day, rate, source, source_ref.strip(),
        )
    return row


async def correct_rate(conn, pair, rate_date, new_rate, new_source, new_source_ref, reason):
    """ONE `fx_rates_correct` call inside ONE explicit READ COMMITTED transaction.

    The database decides whether this is allowed: once any COMMITTED lot
    references the rate, `fx_rates_correct` raises `FX002` and the answer is an
    accounting correction outside L2C — never retro-pricing.
    """
    day = _validate(pair, rate_date, new_source, new_source_ref)
    new_rate = Decimal(str(new_rate)).quantize(RATE_DP, rounding=ROUND_HALF_UP)
    if new_rate <= 0:
        raise OperatorError(f"new rate must be > 0 (got {new_rate})")
    if not reason or not reason.strip():
        raise OperatorError("a reason is required; it goes into the permanent audit row")

    async with conn.transaction(isolation="read_committed"):
        await conn.execute(f"SET LOCAL ROLE {WRITER_ROLE}")
        row = await conn.fetchrow(
            "SELECT * FROM public.fx_rates_correct($1,$2,$3,$4,$5,$6)",
            pair, day, new_rate, new_source, new_source_ref.strip(), reason.strip(),
        )
    return row


# ─────────────────────────────────────────────────────────────────────────────
# CLI
# ─────────────────────────────────────────────────────────────────────────────
def _dsn() -> str:
    dsn = os.getenv("FX_OPS_DSN") or os.getenv("BACKUP_DATABASE_URL")
    if not dsn:
        raise OperatorError(
            "No connection. Set FX_OPS_DSN, or run through the established privileged "
            "connection: railway run -s db-backup -- sh -c "
            "'FX_OPS_DSN=\"$BACKUP_DATABASE_URL\" python3 python/ops/fx_rates_entry.py ...'"
        )
    return dsn


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="fx_rates_entry",
        description="Manual FX rate entry and correction. No scraper: a human reads the "
                    "publication and types what it says.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)

    e = sub.add_parser("enter", help="enter one published rate")
    e.add_argument("--pair", required=True)
    e.add_argument("--rate-date", required=True, help="the day being VALUED, not the publication day")
    e.add_argument("--rate", help="IDR per ONE major unit; omit when giving --jual/--beli/--nilai")
    e.add_argument("--jual", help="BI transaction rate, sell side")
    e.add_argument("--beli", help="BI transaction rate, buy side")
    e.add_argument("--nilai", help="BI quotation unit (1 for USD, 100 for JPY)")
    e.add_argument("--source", required=True, choices=SOURCES)
    e.add_argument("--source-ref", required=True,
                   help="the publication you looked at; for kmk_tax, the KMK covering rate-date")

    c = sub.add_parser("correct", help="correct one UNUSED rate")
    c.add_argument("--pair", required=True)
    c.add_argument("--rate-date", required=True)
    c.add_argument("--new-rate", required=True)
    c.add_argument("--new-source", required=True, choices=SOURCES)
    c.add_argument("--new-source-ref", required=True)
    c.add_argument("--reason", required=True)
    c.add_argument("--i-have-checked-no-lot-uses-it", action="store_true",
                   help="acknowledgement only; the database is the actual authority and "
                        "raises FX002 if a committed lot references the rate")
    return p


async def _amain(args) -> int:
    conn = await asyncpg.connect(_dsn())
    try:
        if args.cmd == "enter":
            if args.jual or args.beli or args.nilai:
                if not (args.jual and args.beli and args.nilai):
                    raise OperatorError("--jual, --beli and --nilai must be given together")
                if args.rate:
                    raise OperatorError(
                        "give either --rate or the --jual/--beli/--nilai triple, never both: "
                        "the per-major-unit figure is DERIVED, never asserted alongside its inputs"
                    )
                rate = normalize_bi_mid(args.jual, args.beli, args.nilai)
            else:
                if not args.rate:
                    raise OperatorError("--rate is required (or the --jual/--beli/--nilai triple)")
                rate = args.rate
            row = await enter_rate(conn, args.pair, args.rate_date, rate,
                                   args.source, args.source_ref)
            print(f"  {args.pair} {args.rate_date} -> {row['effective_rate']} ({row['outcome']})")
        else:
            if not args.i_have_checked_no_lot_uses_it:
                raise OperatorError(
                    "pass --i-have-checked-no-lot-uses-it. A correction is only possible while "
                    "the rate is unused; once a lot references it the answer is an accounting "
                    "correction outside L2C, never retro-pricing."
                )
            row = await correct_rate(conn, args.pair, args.rate_date, args.new_rate,
                                     args.new_source, args.new_source_ref, args.reason)
            print(f"  {args.pair} {args.rate_date}: {row['old_rate']} -> {row['new_rate']} (audited)")
    finally:
        await conn.close()
    return 0


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except OperatorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except asyncpg.PostgresError as exc:
        code = getattr(exc, "sqlstate", "?")
        hint = {
            "FX001": "one correction per transaction",
            "FX002": "a committed lot already references this rate — it is now immutable",
            "FX003": "the session is not READ COMMITTED",
            "FX005": "the KMK cited does not cover rate_date",
            "FX006": "that key already holds a different rate; correct it, do not re-enter it",
            "42501": "no EXECUTE — is this connection granted fx_rates_writer WITH SET TRUE?",
        }.get(code, "")
        print(f"database refused [{code}]: {exc}{' — ' + hint if hint else ''}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

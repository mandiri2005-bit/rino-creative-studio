"""
provider_ledger_ingest.py — the Balance Ledger ACQUISITION step.

NORMATIVE SOURCE: Decision 3A §5. The supplier-fee anchor is derived from the
Dodo Balance Ledger per payment, so those entries must be **acquired and
persisted before lot birth**. Without this step the birth path is correct and
permanently stalled: every top-up returns `FX011`
(`STALL-AWAITING-PROVIDER-LEDGER`) and no lot is ever born.

WHAT THIS DOES AND DOES NOT DO
------------------------------
With `--fetch` it walks **every page** of `/balances/ledger` for one payment —
offset paging, `page_number`/`page_size`, terminating on a short page, because
the endpoint returns `items` and offers no `has_more` or cursor to stop on —
validates that each entry's `reference_object_id` is the payment asked for, and
persists idempotently under a **read-back comparison** — what is stored is
re-read and compared field by field against what was fetched, **in both
directions and inside the same transaction as the inserts**, so a silent
truncation, a partial write or an unexpected stored entry rolls the batch back
rather than producing a smaller anchor.

It is a **manual tool**: one payment, one run, run by a person. There is no
scheduler, no daemon and no framework here, and adding one is out of scope.

⚠️ `--fetch` performs a real HTTP call to the provider. Nothing in this
repository invokes it automatically, and it was never executed against the live
API while this tool was written — the paging, validation and read-back logic
were exercised against a stubbed transport.

    # fetch every page and persist, in one manual run
    DODO_API_KEY=... FX_OPS_DSN=... \
      python3 python/ops/provider_ledger_ingest.py --payment-id <payment_id> --fetch

    # or persist an export you already have
    FX_OPS_DSN=... python3 python/ops/provider_ledger_ingest.py \
        --payment-id <payment_id> --file ledger.json

THE FENCE
---------
Every insert happens inside ONE transaction that first takes
`g3_fence_payment(provider, payment_id)` — the same `FOR UPDATE` on the payment
aggregate that `g3_resolve_request` takes. That is what stops an acquisition
landing in the middle of a birth: row locks on the ledger entries themselves
could not, because they cannot block an INSERT of a NEW entry.

If a birth is in flight this call BLOCKS rather than racing it, and the birth's
digest check is the backstop for any writer that skips the fence.

INPUT SHAPE
-----------
A JSON array, or an object with a `data`/`items`/`entries` array, of entries
carrying at least an id, an event type, an amount in MINOR units and a currency.
Field names are mapped leniently because the export format is the provider's,
not ours — but nothing is INVENTED: an entry missing any required field is
refused, loudly, and the whole batch rolls back.

⚠️ WHAT YOU PASS MUST BE THE COMPLETE LEDGER FOR THAT PAYMENT, not a top-up of
entries you think are missing. The read-back is a set equality, so a batch that
omits an already-stored entry is refused as readily as one that adds a stranger.
That is deliberate: the anchor is derived from every row under the payment, so
"the part I did not send is fine" is not a thing this tool can verify. Re-send
the whole export — re-ingesting unchanged entries is a no-op.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from urllib.parse import quote

try:
    import asyncpg
except ImportError as exc:  # pragma: no cover - environment defect
    raise RuntimeError("provider_ledger_ingest requires asyncpg") from exc


class OperatorError(RuntimeError):
    """A refusal raised before anything touches the database."""


_ID = ("id", "entry_id", "ledger_entry_id")
_TYPE = ("event_type", "type", "entry_type")
_AMOUNT = ("amount", "amount_minor", "value", "minor_amount")
_CCY = ("currency", "settlement_currency", "currency_code")


def _pick(entry: dict, names, what: str):
    for n in names:
        if n in entry and entry[n] is not None:
            return entry[n]
    raise OperatorError(
        f"entry is missing {what} (looked for {', '.join(names)}): {json.dumps(entry)[:200]}")


def normalise(payload) -> list[dict]:
    """Provider export → the rows this repo stores. Refuses; never invents."""
    if isinstance(payload, dict):
        for key in ("data", "items", "entries", "results"):
            if isinstance(payload.get(key), list):
                payload = payload[key]
                break
        else:
            raise OperatorError(
                "the payload is an object with no data/items/entries/results array")
    if not isinstance(payload, list):
        raise OperatorError("the payload must be a JSON array of ledger entries")
    if not payload:
        raise OperatorError(
            "the export contains ZERO entries. That is not the same as a payment with no fee — "
            "an empty ledger STALLS the anchor (FX011), and persisting nothing would leave it "
            "stalled with no record of why.")

    rows, seen = [], set()
    for entry in payload:
        if not isinstance(entry, dict):
            raise OperatorError(f"entry is not an object: {entry!r}")
        entry_id = str(_pick(entry, _ID, "an entry id"))
        if entry_id in seen:
            raise OperatorError(f"duplicate entry id {entry_id!r} inside one export")
        seen.add(entry_id)
        amount = _pick(entry, _AMOUNT, "an amount in MINOR units")
        if isinstance(amount, float) and amount != int(amount):
            raise OperatorError(
                f"entry {entry_id}: amount {amount} is fractional, so it is not minor units")
        rows.append({
            "entry_id": entry_id,
            "event_type": str(_pick(entry, _TYPE, "an event type")),
            "amount_minor": int(amount),
            "currency": str(_pick(entry, _CCY, "a currency")).upper(),
            "raw_entry": json.dumps(entry),
        })
    return rows


async def ingest(conn, provider: str, payment_id: str, rows: list[dict]) -> dict:
    """ONE transaction: fence, insert-if-absent, EXACT-SET read-back, digest.

    🔴 THE READ-BACK IS INSIDE THE TRANSACTION, AND THAT IS THE ENTIRE POINT OF
       IT. It used to run after `ingest()` returned — which is to say after the
       COMMIT — so the check could report a conflict perfectly and still have
       already published the rows it was objecting to. Anything reaching the
       payment next, lot birth included, would consume evidence this tool had
       just declared untrustworthy. A verification that cannot undo what it
       verifies is a log line, not a control.

       Rolled into the fence's transaction, a refusal now un-writes the batch:
       the inserts, the comparison and the digest either all stand or none of
       them happened.
    """
    async with conn.transaction():
        await conn.execute("SELECT public.g3_fence_payment($1,$2)", provider, payment_id)
        inserted = 0
        for r in rows:
            got = await conn.fetchval(
                "INSERT INTO public.g3_provider_balance_ledger"
                "(provider, provider_payment_id, entry_id, event_type, amount_minor,"
                " currency, raw_entry) VALUES ($1,$2,$3,$4,$5,$6,$7::jsonb) "
                "ON CONFLICT (provider, entry_id) DO NOTHING RETURNING id",
                provider, payment_id, r["entry_id"], r["event_type"],
                r["amount_minor"], r["currency"], r["raw_entry"])
            inserted += got is not None
        await read_back_and_compare(conn, provider, payment_id, rows)
        digest = await conn.fetchval(
            "SELECT public.g3_ledger_digest($1,$2)", provider, payment_id)
    return {"submitted": len(rows), "inserted": inserted,
            "already_present": len(rows) - inserted, "digest": digest}


DEFAULT_BASE = "https://live.dodopayments.com"
PAGE_SIZE = 100          # the provider's documented maximum
MAX_PAGES = 200          # a hard stop, so a broken pager cannot loop forever


def fetch_all_pages(payment_id: str, *, api_key: str, base_url: str, transport=None) -> list[dict]:
    """Every page of `GET /balances/ledger` for ONE payment.

    THE PROVIDER'S PAGINATION IS OFFSET-BASED, NOT CURSOR-BASED
    -----------------------------------------------------------
    `page_number` (0-based) and `page_size` (max 100) go out; the response comes
    back carrying `items` and nothing else. There is no `has_more`, no
    `next_cursor`, no total. The ONLY termination signal the contract offers is a
    SHORT PAGE: fewer than `page_size` items means there is no page after it.

    🔴 THIS FUNCTION USED TO PAGE ON `starting_after` / `next_cursor` /
       `has_more`. None of those fields exist on the real endpoint, so the first
       response satisfied `not cursor and not has_more` and the walk stopped at
       page one — silently. A ledger truncated to its first page does not look
       broken: it derives a perfectly plausible, and too small, supplier-fee
       anchor. The invented contract was never caught because the test stub
       spoke it back, and the stub was never asked what URL it was called with.

    `transport(url, headers) -> dict` is injected so the paging and validation
    logic is testable without touching the provider. The default performs a real
    request; nothing calls this without `--fetch`.
    """
    if transport is None:                                    # pragma: no cover - live path
        import urllib.request

        def transport(url, headers):
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode("utf-8"))

    headers = {"Authorization": f"Bearer {api_key}", "Accept": "application/json"}
    out, seen_ids = [], {}
    page = 0
    while page < MAX_PAGES:
        url = (f"{base_url.rstrip('/')}/balances/ledger"
               f"?reference_object_id={quote(payment_id, safe='')}"
               f"&page_size={PAGE_SIZE}&page_number={page}")
        body = transport(url, headers)
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise OperatorError(
                f"page {page}: the response carries no `items` array. That is the documented "
                "shape of GET /balances/ledger, and guessing at another one is how a truncated "
                f"ledger gets mistaken for a complete one. Got: {repr(body)[:200]}")
        items = body["items"]

        # Offset paging over a set that MOVES re-serves rows. A repeat is not a
        # curiosity: counting the same fee entry twice moves the anchor.
        for e in items:
            if not isinstance(e, dict):
                raise OperatorError(f"page {page}: entry is not an object: {e!r}")
            eid = e.get("id")
            if eid is not None and eid in seen_ids:
                raise OperatorError(
                    f"entry {eid!r} was returned on page {seen_ids[eid]} AND page {page}. "
                    "Offset paging re-served a row, so the underlying set moved mid-walk and "
                    "this read is not a consistent snapshot of the ledger.")
            if eid is not None:
                seen_ids[eid] = page
        out.extend(items)
        page += 1

        # The short page IS the end-of-collection signal. A full page means
        # there may be another, and stopping there is the truncation bug.
        if len(items) < PAGE_SIZE:
            break
    else:
        raise OperatorError(f"stopped after {MAX_PAGES} pages; the pager is not terminating")

    # Every entry must belong to the payment we asked for. A provider-side
    # filter bug would otherwise fold someone else's fee into this anchor.
    for e in out:
        ref = e.get("reference_object_id") or e.get("reference_id")
        if ref is None:
            raise OperatorError(
                f"entry {e.get('id')!r} carries no reference_object_id; it cannot be attributed")
        if str(ref) != payment_id:
            raise OperatorError(
                f"entry {e.get('id')!r} belongs to {ref!r}, not {payment_id!r} — refusing the batch")
    return out


async def read_back_and_compare(conn, provider: str, payment_id: str, rows: list[dict]) -> None:
    """What is stored must be EXACTLY what was fetched — an equality, both ways.

    🔴 CHECKING ONE DIRECTION IS NOT CHECKING THE SET. Proving every fetched row
       is present says nothing about rows the provider did NOT return, and both
       `g3_ledger_digest` and `g3_supplier_fee_anchor` read the FULL stored set
       for the payment, not the batch that was just submitted. So a single
       stale, mis-attributed or hand-inserted entry sitting under this payment
       passes a one-way check untouched and then lands in the anchor. The extra
       row is the dangerous direction: a missing row stalls loudly, an extra one
       just quietly changes the number.
    """
    stored = {r["entry_id"]: r for r in await conn.fetch(
        "SELECT entry_id, event_type, amount_minor, currency "
        "  FROM public.g3_provider_balance_ledger "
        " WHERE provider=$1 AND provider_payment_id=$2", provider, payment_id)}
    for r in rows:
        got = stored.get(r["entry_id"])
        if got is None:
            raise OperatorError(f"read-back: entry {r['entry_id']} is not stored")
        if (got["event_type"], got["amount_minor"], got["currency"]) != (
                r["event_type"], r["amount_minor"], r["currency"]):
            raise OperatorError(
                f"read-back MISMATCH on {r['entry_id']}: stored "
                f"({got['event_type']}, {got['amount_minor']}, {got['currency']}) vs fetched "
                f"({r['event_type']}, {r['amount_minor']}, {r['currency']}). The ledger already "
                "held a DIFFERENT entry under this id; the anchor would be derived from evidence "
                "that is not what the provider just returned.")

    extra = sorted(set(stored) - {r["entry_id"] for r in rows})
    if extra:
        raise OperatorError(
            f"read-back: {provider}:{payment_id} holds {len(extra)} stored entry/entries the "
            f"provider did NOT return: {', '.join(extra[:10])}"
            f"{' …' if len(extra) > 10 else ''}. The anchor and the digest are computed over "
            "every row under this payment, so evidence with no counterpart in the fetch would "
            "silently move the supplier fee. Refusing the batch rather than pricing off it.")


def _dsn() -> str:
    dsn = os.getenv("FX_OPS_DSN") or os.getenv("BACKUP_DATABASE_URL")
    if not dsn:
        raise OperatorError(
            "No connection. Set FX_OPS_DSN, or run through the established privileged "
            "connection (railway run -s db-backup ...).")
    return dsn


async def _amain(args) -> int:
    if args.fetch:
        key = os.getenv("DODO_API_KEY")
        if not key:
            raise OperatorError("--fetch needs DODO_API_KEY")
        payload = fetch_all_pages(args.payment_id, api_key=key,
                                  base_url=os.getenv("DODO_API_BASE", DEFAULT_BASE))
    elif args.file:
        raw = sys.stdin.read() if args.file == "-" else open(args.file, encoding="utf-8").read()
        payload = json.loads(raw)
    else:
        raise OperatorError("give either --fetch or --file")
    rows = normalise(payload)

    print(f"  {len(rows)} entry/entries for {args.provider}:{args.payment_id}")
    for r in rows:
        print(f"    {r['entry_id']:<28} {r['event_type']:<16} "
              f"{r['amount_minor']:>10} {r['currency']}")

    conn = await asyncpg.connect(_dsn())
    try:
        # The read-back runs INSIDE ingest()'s transaction — calling it again
        # here would only re-check rows that are already committed, which is the
        # ordering defect this tool used to have.
        result = await ingest(conn, args.provider, args.payment_id, rows)
        print(f"  inserted {result['inserted']}, already present "
              f"{result['already_present']}, digest {result['digest']}")
        try:
            anchor = await conn.fetchrow(
                "SELECT * FROM public.g3_supplier_fee_anchor($1,$2,$3)",
                args.provider, args.payment_id, args.settlement_tax_minor)
            print(f"  Decision-3A anchor: {anchor['anchor_minor']} {anchor['currency']} "
                  f"(payment - payment_fees - tax)")
        except asyncpg.PostgresError as exc:
            # Reported, not fatal: the evidence is persisted either way, and the
            # anchor being inadmissible is exactly the STALL the contract wants.
            print(f"  anchor NOT yet admissible [{getattr(exc, 'sqlstate', '?')}]: {exc}",
                  file=sys.stderr)
            return 3
    finally:
        await conn.close()
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(
        prog="provider_ledger_ingest",
        description="Persist Dodo Balance Ledger entries for one payment, under the "
                    "per-payment fence. Does NOT call the provider API.")
    p.add_argument("--provider", default="dodo")
    p.add_argument("--payment-id", required=True)
    p.add_argument("--file", help="exported JSON, or - for stdin")
    p.add_argument("--fetch", action="store_true",
                   help="walk every page of /balances/ledger for this payment (real HTTP call)")
    p.add_argument("--settlement-tax-minor", type=int, default=0,
                   help="settlement_tax from the payment record; a positive value REQUIRES a "
                        "tax entry in the ledger")
    args = p.parse_args(argv)
    try:
        return asyncio.run(_amain(args))
    except OperatorError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2
    except asyncpg.PostgresError as exc:
        print(f"database refused [{getattr(exc, 'sqlstate', '?')}]: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())

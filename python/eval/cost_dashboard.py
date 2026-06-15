# -*- coding: utf-8 -*-
"""
Project Dalang — cost dashboard (WS-9).

Reads `usage_logs` (reusing python/database.py — pure asyncpg, no new deps) and
prints a compact table of:

  * cache-hit ratio   — share of narration calls served from cache. usage_logs
    has no dedicated cache column, so a "cache hit" is inferred from a tunable
    convention (default: a row with tokens_in == 0 AND tokens_out == 0 AND
    cost_usd == 0 — i.e. an upstream call that was short-circuited by a cache).
    The marker is configurable via env so it can track however the runtime tags
    cached rows (e.g. provider='cache' or finish_reason='cache').
  * cost / job        — total cost_usd and credits divided by distinct jobs.
  * tokens, calls, p50/avg cost per call, broken out per endpoint and per model.

IMPORT-SAFE: importing this module never opens a DB connection. All DB access is
inside async functions guarded so that a missing DATABASE_URL / unreachable DB /
absent `database` module degrades to an EMPTY dashboard rather than raising.

USAGE
-----
    python3 -m eval.cost_dashboard                  # last 30 days, all endpoints
    python3 -m eval.cost_dashboard --days 7 --endpoint narasi
    python3 -m eval.cost_dashboard --tenant <uuid>  # one tenant
    python3 -m eval.cost_dashboard --json           # machine-readable

CACHE MARKER ENV
----------------
    EVAL_CACHE_PROVIDER   if set, a row counts as a cache hit when
                          provider == this value (e.g. "cache").
    EVAL_CACHE_FINISH     if set, a row counts as a cache hit when
                          finish_reason == this value (e.g. "cache_hit").
  (If neither is set, the zero-cost/zero-token heuristic is used.)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from dataclasses import dataclass, field
from typing import Any, Optional


# --------------------------------------------------------------------------- #
# Cache-hit predicate (SQL fragment) — tunable via env, defaults to a heuristic.
# --------------------------------------------------------------------------- #
def _cache_hit_sql() -> tuple[str, list]:
    """Return (sql_boolean_expression, params) identifying a cached row.

    Returns a fragment usable inside SELECT/WHERE. Params are positional and the
    caller is responsible for offsetting placeholders; we instead inline safe
    literals derived from env (validated) to avoid placeholder juggling."""
    prov = os.environ.get("EVAL_CACHE_PROVIDER", "").strip()
    finish = os.environ.get("EVAL_CACHE_FINISH", "").strip()
    if prov:
        # validate: alnum/_- only, then inline as a quoted literal
        safe = "".join(ch for ch in prov if ch.isalnum() or ch in "_-")
        return f"(provider = '{safe}')", []
    if finish:
        safe = "".join(ch for ch in finish if ch.isalnum() or ch in "_-")
        return f"(finish_reason = '{safe}')", []
    # default heuristic: upstream short-circuited (no tokens, no cost)
    return ("(COALESCE(tokens_in,0) = 0 AND COALESCE(tokens_out,0) = 0 "
            "AND COALESCE(cost_usd,0) = 0)"), []


# --------------------------------------------------------------------------- #
# Row container
# --------------------------------------------------------------------------- #
@dataclass
class GroupStats:
    key: str
    calls: int = 0
    cache_hits: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    cost_usd: float = 0.0
    credits: int = 0
    jobs: int = 0

    @property
    def cache_ratio(self) -> float:
        return (self.cache_hits / self.calls) if self.calls else 0.0

    @property
    def cost_per_job(self) -> float:
        return (self.cost_usd / self.jobs) if self.jobs else 0.0

    @property
    def cost_per_call(self) -> float:
        return (self.cost_usd / self.calls) if self.calls else 0.0

    def as_dict(self) -> dict[str, Any]:
        return {
            "key": self.key, "calls": self.calls, "cache_hits": self.cache_hits,
            "cache_ratio": round(self.cache_ratio, 4),
            "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6), "credits": self.credits,
            "jobs": self.jobs, "cost_per_job": round(self.cost_per_job, 6),
            "cost_per_call": round(self.cost_per_call, 6),
        }


@dataclass
class Dashboard:
    days: int
    endpoint: Optional[str]
    tenant: Optional[str]
    overall: GroupStats
    by_endpoint: list[GroupStats] = field(default_factory=list)
    by_model: list[GroupStats] = field(default_factory=list)
    available: bool = True
    note: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "window_days": self.days, "endpoint": self.endpoint,
            "tenant": self.tenant, "available": self.available, "note": self.note,
            "overall": self.overall.as_dict(),
            "by_endpoint": [g.as_dict() for g in self.by_endpoint],
            "by_model": [g.as_dict() for g in self.by_model],
        }


# --------------------------------------------------------------------------- #
# Data access — all guarded; never raises out of these functions.
# --------------------------------------------------------------------------- #
async def _fetch_rows(days: int, endpoint: Optional[str], tenant: Optional[str]) -> tuple[list, str]:
    """Fetch usage_logs rows in the window. Returns (rows, note). On any failure
    (no DB module, no DATABASE_URL, unreachable DB) returns ([], explanation)."""
    try:
        import database as db  # type: ignore
    except Exception as exc:  # noqa: BLE001
        return [], f"database module not importable ({type(exc).__name__})"

    if not (os.environ.get("DATABASE_URL") or os.environ.get("DATABASE_PUBLIC_URL")
            or os.environ.get("PGHOST")):
        return [], "no DATABASE_URL / PGHOST in env — dashboard is empty (set it to read usage_logs)"

    try:
        await db.init_db()
    except Exception as exc:  # noqa: BLE001
        return [], f"could not connect to DB ({type(exc).__name__}: {exc})"

    cache_expr, _ = _cache_hit_sql()
    where = [f"created_at >= NOW() - INTERVAL '{int(days)} days'"]
    params: list[Any] = []
    if endpoint:
        params.append(endpoint)
        where.append(f"endpoint = ${len(params)}")
    if tenant:
        params.append(tenant)
        where.append(f"tenant_id = ${len(params)}::uuid")
    where_sql = " AND ".join(where)

    sql = f"""
        SELECT
            endpoint,
            COALESCE(model_alias, model_upstream, 'unknown') AS model,
            COUNT(*)                                AS calls,
            SUM(CASE WHEN {cache_expr} THEN 1 ELSE 0 END) AS cache_hits,
            COALESCE(SUM(tokens_in), 0)             AS tokens_in,
            COALESCE(SUM(tokens_out), 0)            AS tokens_out,
            COALESCE(SUM(cost_usd), 0)              AS cost_usd,
            COALESCE(SUM(credits), 0)               AS credits,
            COUNT(DISTINCT job_id) FILTER (WHERE job_id IS NOT NULL) AS jobs
        FROM usage_logs
        WHERE {where_sql}
        GROUP BY endpoint, model
        ORDER BY cost_usd DESC
    """
    try:
        # _q_fetch is tenant-aware; pass tenant when filtering by one, else None.
        rows = await db._q_fetch(sql, *params, tenant=(tenant or None))
        return list(rows), ""
    except Exception as exc:  # noqa: BLE001
        return [], f"query failed ({type(exc).__name__}: {exc})"
    finally:
        try:
            await db.close_db()
        except Exception:  # noqa: BLE001
            pass


def _aggregate(rows: list, days: int, endpoint: Optional[str],
               tenant: Optional[str], note: str) -> Dashboard:
    """Fold grouped rows into overall + per-endpoint + per-model stats.

    Note on distinct-job counts: COUNT(DISTINCT job_id) per (endpoint,model)
    group cannot be summed to a global distinct count, so overall.jobs uses the
    MAX group jobs as a conservative floor; the per-endpoint rollup sums within
    an endpoint. The cost/job figures are therefore best read per-row.
    """
    by_em: dict[tuple[str, str], GroupStats] = {}
    ep_roll: dict[str, GroupStats] = {}
    model_roll: dict[str, GroupStats] = {}
    overall = GroupStats(key="ALL")

    for r in rows:
        ep = str(r["endpoint"] or "other")
        model = str(r["model"] or "unknown")
        calls = int(r["calls"] or 0)
        hits = int(r["cache_hits"] or 0)
        tin = int(r["tokens_in"] or 0)
        tout = int(r["tokens_out"] or 0)
        cost = float(r["cost_usd"] or 0.0)
        credits = int(r["credits"] or 0)
        jobs = int(r["jobs"] or 0)

        for bucket, key in ((ep_roll, ep), (model_roll, model)):
            g = bucket.setdefault(key, GroupStats(key=key))
            g.calls += calls; g.cache_hits += hits
            g.tokens_in += tin; g.tokens_out += tout
            g.cost_usd += cost; g.credits += credits
            g.jobs += jobs

        overall.calls += calls; overall.cache_hits += hits
        overall.tokens_in += tin; overall.tokens_out += tout
        overall.cost_usd += cost; overall.credits += credits
        overall.jobs = max(overall.jobs, jobs)

    return Dashboard(
        days=days, endpoint=endpoint, tenant=tenant, overall=overall,
        by_endpoint=sorted(ep_roll.values(), key=lambda g: -g.cost_usd),
        by_model=sorted(model_roll.values(), key=lambda g: -g.cost_usd),
        available=bool(rows), note=note,
    )


async def build_dashboard(days: int = 30, endpoint: Optional[str] = None,
                          tenant: Optional[str] = None) -> Dashboard:
    """Top-level: fetch + aggregate. Never raises."""
    rows, note = await _fetch_rows(days, endpoint, tenant)
    return _aggregate(rows, days, endpoint, tenant, note)


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #
def _fmt_group_row(g: GroupStats, label_w: int = 24) -> str:
    return (f"{g.key[:label_w]:<{label_w}}  "
            f"{g.calls:>6}  {g.cache_ratio*100:>6.1f}%  "
            f"{g.tokens_in:>9}  {g.tokens_out:>9}  "
            f"${g.cost_usd:>9.4f}  {g.credits:>8}  "
            f"${g.cost_per_call:>8.5f}")


def render(dash: Dashboard) -> str:
    """Render the dashboard as a printable table."""
    w = 24
    lines: list[str] = []
    lines.append("=" * 100)
    lines.append("PROJECT DALANG — COST DASHBOARD  (usage_logs)")
    scope = f"last {dash.days}d"
    if dash.endpoint:
        scope += f" · endpoint={dash.endpoint}"
    if dash.tenant:
        scope += f" · tenant={dash.tenant[:8]}…"
    lines.append(scope)
    lines.append("=" * 100)

    if not dash.available:
        lines.append("(no data)")
        if dash.note:
            lines.append(f"note: {dash.note}")
        lines.append("=" * 100)
        return "\n".join(lines)

    o = dash.overall
    lines.append(f"calls={o.calls}   cache-hit={o.cache_ratio*100:.1f}%   "
                 f"cost=${o.cost_usd:.4f}   credits={o.credits}   "
                 f"cost/job≈${o.cost_per_job:.4f}   cost/call=${o.cost_per_call:.5f}")
    lines.append("-" * 100)
    header = (f"{'KEY':<{w}}  {'CALLS':>6}  {'CACHE':>7}  "
              f"{'TOK_IN':>9}  {'TOK_OUT':>9}  {'COST':>10}  {'CREDITS':>8}  {'$/CALL':>9}")
    lines.append("BY ENDPOINT")
    lines.append(header)
    for g in dash.by_endpoint:
        lines.append(_fmt_group_row(g, w))
    lines.append("-" * 100)
    lines.append("BY MODEL")
    lines.append(header)
    for g in dash.by_model:
        lines.append(_fmt_group_row(g, w))
    lines.append("=" * 100)
    if dash.note:
        lines.append(f"note: {dash.note}")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="eval.cost_dashboard",
        description="Project Dalang cost dashboard — cache-hit ratio + cost/job from usage_logs.")
    p.add_argument("--days", type=int, default=30, help="Window in days (default 30).")
    p.add_argument("--endpoint", default=None,
                   help="Filter to one endpoint (e.g. narasi, chat, video).")
    p.add_argument("--tenant", default=None, help="Filter to one tenant UUID.")
    p.add_argument("--json", action="store_true", help="Emit JSON instead of a table.")
    return p


async def _amain(argv: Optional[list[str]] = None) -> int:
    args = build_arg_parser().parse_args(argv)
    dash = await build_dashboard(days=args.days, endpoint=args.endpoint, tenant=args.tenant)
    if args.json:
        print(json.dumps(dash.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(render(dash))
    return 0


def main(argv: Optional[list[str]] = None) -> int:
    return asyncio.run(_amain(argv))


if __name__ == "__main__":
    raise SystemExit(main())

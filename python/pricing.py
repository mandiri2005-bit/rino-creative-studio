"""pricing.py — the SMART resolver over pricing_catalog.json.

ONE catalog (pricing_catalog.json) holds every (model, provider, slug, unit, price) offering, grouped by
category (image | video | avatar | tts | fx). This module reads it and answers, dynamically:

  • which providers can serve a model, ordered CHEAPEST-FIRST (the failover sequence) — NOT hardcoded;
  • the cost bounds (min / max known price) for the billing floor (so a failover never goes negative-margin);
  • the cheapest provider/slug to dispatch to.

When a provider's price changes you edit ONE row in the catalog — every model's failover order and the
billing floor re-derive automatically. Unknown price (null) is treated as the MOST expensive (sorted last,
never silently chosen as "cheapest", logged for fill) so a missing number can never cause a wrong pick.
"""
from __future__ import annotations
import json
import logging
import math
import os
import threading
from typing import Optional

log = logging.getLogger("pricing")

_CATALOG_PATH = os.getenv("PRICING_CATALOG_PATH", os.path.join(os.path.dirname(__file__), "pricing_catalog.json"))
_lock = threading.Lock()
_cache: Optional[dict] = None
_mtime: Optional[float] = None


def _load() -> dict:
    """Load + cache the catalog; hot-reload when the file changes on disk (so a price edit / cron sync
    takes effect without a restart). Returns {} on any error (callers fall back to the legacy registry)."""
    global _cache, _mtime
    try:
        with _lock:                              # stat INSIDE the lock — no stale-read window (PRC-003)
            st = os.stat(_CATALOG_PATH)
            if _cache is None or st.st_mtime != _mtime:
                with open(_CATALOG_PATH) as f:
                    _cache = json.load(f)
                _mtime = st.st_mtime
            return _cache or {}
    except Exception as e:
        log.warning("pricing: catalog load failed (%s) — callers fall back to registry", e)
        return {}


def _rows(category: str) -> list:
    return ((_load().get("categories") or {}).get(category) or [])


def _price(row: dict) -> float:
    """Numeric price for sorting/bounds — unknown (None / ≤0) → +inf so it never reads as cheapest."""
    p = row.get("price")
    try:
        p = float(p)
        return p if p > 0 else math.inf
    except (TypeError, ValueError):
        return math.inf


def offerings(category: str, model: str, feature: Optional[str] = None) -> list:
    """All catalog rows for (category, model), optionally only those that serve `feature`."""
    out = []
    for r in _rows(category):
        if r.get("model") != model:
            continue
        if feature and feature not in (r.get("features") or []):
            continue
        out.append(r)
    return out


def cheapest_chain(category: str, model: str, feature: Optional[str] = None) -> list:
    """The failover sequence for (model, feature), CHEAPEST-FIRST — derived from the catalog at call time,
    never hardcoded. Unknown-price providers sort LAST (tried only as a last resort) and are logged so they
    get filled. Returns the rows in order (each carries provider/slug/price)."""
    rows = offerings(category, model, feature)
    ordered = sorted(rows, key=_price)
    unknown = [r["provider"] for r in ordered if _price(r) == math.inf]
    if unknown:
        log.info("pricing: %s/%s has UNKNOWN price for provider(s) %s — sorted last; fill the catalog",
                 category, model, unknown)
    return ordered


def best(category: str, model: str, feature: Optional[str] = None) -> Optional[dict]:
    """The single cheapest provider row for (model, feature), or None if the model isn't in the catalog."""
    ch = cheapest_chain(category, model, feature)
    return ch[0] if ch else None


def cost_bounds(category: str, model: str, feature: Optional[str] = None) -> dict:
    """Cost envelope for the model across its providers, for billing decisions:
      min_cost  — cheapest KNOWN price (what we most likely pay on the happy path)
      max_cost  — most expensive KNOWN price (worst-case failover; the BILLING FLOOR should cover this so a
                  failover never goes negative-margin) — None if no provider is priced
      has_unknown — True if any provider price is missing (so max_cost may understate the true worst case)
    """
    rows = offerings(category, model, feature)
    known = [float(r["price"]) for r in rows if _price(r) != math.inf]
    has_unknown = any(_price(r) == math.inf for r in rows)
    return {
        "min_cost": min(known) if known else None,
        "max_cost": max(known) if known else None,
        "has_unknown": has_unknown,
        "n_providers": len(rows),
        "unit": rows[0].get("unit") if rows else None,
    }


def has_model(category: str, model: str) -> bool:
    """Whether the catalog covers this model (so callers know to use it vs fall back to the registry)."""
    return bool(offerings(category, model))


# ── category-AGNOSTIC helpers (dispatch doesn't know/care which category a model lives in) ──
def _all_rows() -> list:
    out = []
    for rows in (_load().get("categories") or {}).values():
        out.extend(rows)
    return out


def cost_for_provider(model: str, provider: str, feature: Optional[str] = None) -> Optional[float]:
    """The catalog COGS for (model, provider) across any category — or None if absent/unknown. Used by
    dispatch to book the price of the provider that ACTUALLY served (not a cheapest-or-official guess).
    FEATURE-TOLERANT: price is per (model, provider) — a feature-specific row is preferred when present,
    else any row for the pair is used. This absorbs the registry naming split (video uses full feature
    names like 'image_to_video'; image uses short 'create'/'edit' that don't equal the dispatch 'create_raster')."""
    rows = [r for r in _all_rows() if r.get("model") == model and r.get("provider") == provider]
    if not rows:
        return None
    pick = next((r for r in rows if feature and feature in (r.get("features") or [])), None) or rows[0]
    p = pick.get("price")
    try:
        return float(p) if p and float(p) > 0 else None
    except (TypeError, ValueError):
        return None


def covers(model: str) -> bool:
    """True if the catalog has ANY priced row for this model (so dispatch should use catalog ordering)."""
    return any(r.get("model") == model and _price(r) != math.inf for r in _all_rows())


def reorder_steps(model: str, feature: Optional[str], steps: list) -> list:
    """Return the dispatch `steps` (registry chain entries: {provider, slug...}) re-sorted CHEAPEST-FIRST by
    the catalog price for (model, provider). Catalog doesn't cover the model → returns `steps` UNCHANGED
    (backward-compatible: the legacy registry order stands). A provider absent from the catalog sorts LAST
    (stable) so an unpriced step is only ever a last resort. Pure + side-effect-free."""
    if not covers(model):
        return steps

    def key(step):
        prov = step.get("provider") if isinstance(step, dict) else None   # tolerate a malformed step (FIND-010)
        c = cost_for_provider(model, prov, feature) if prov else None
        return c if c is not None else math.inf
    return sorted(steps, key=key)


def bounds_any(model: str, feature: Optional[str] = None) -> dict:
    """cost_bounds for a model searched across all categories (dispatch/billing convenience)."""
    rows = [r for r in _all_rows()
            if r.get("model") == model and (not feature or feature in (r.get("features") or []))]
    known = [float(r["price"]) for r in rows if _price(r) != math.inf]
    return {"min_cost": min(known) if known else None, "max_cost": max(known) if known else None,
            "has_unknown": any(_price(r) == math.inf for r in rows), "n_providers": len(rows)}

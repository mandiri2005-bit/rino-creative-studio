# -*- coding: utf-8 -*-
"""
credit_catalog.py — the single source of truth that maps an operation to its
**credit** cost.

WHY THIS EXISTS
---------------
Our COGS is ~95% variable upstream API fees. A flat subscription loses money on
every heavy user. The fix is a pre-funded credit gate: every paid operation is
priced in credits BEFORE it runs, the balance is held, and nothing executes at a
loss. This module is the price list.

THE MODEL (aligned with the ceritaAI pricing tool, June 2026)
-------------------------------------------------------------
    1 credit  ≡  CREDIT_USD_VALUE of upstream cost   (default $0.01)
    credits   =  ceil( upstream_usd * CREDIT_MARGIN / CREDIT_USD_VALUE )

The pricing tool tunes each op to break even at exactly $0.01/credit; the real
margin comes from tier pricing, gating and <100% utilisation — NOT a per-credit
markup. So CREDIT_MARGIN defaults to 1.0. Both knobs are env-configurable, so
re-pricing the whole catalog is a one-line change, never a code edit:

    CREDIT_USD_VALUE   USD value of one credit            (default 0.01)
    CREDIT_MARGIN      multiplier applied to upstream cost (default 1.0)

UPSTREAM COSTS
--------------
Token and per-image costs are delegated to the existing maps in laozhang_api.py
(`_calc_cost`, `_calc_image_cost`) so there is ONE pricing source and no drift.
Video (per second) and TTS (per character) maps live here because laozhang_api
currently logs video at $0.0 and has no per-char TTS rate.

`credit_cost(operation, model, units) -> int` is the public API. Run this file
directly (`python credit_catalog.py`) to print a sample cost table.
"""
from __future__ import annotations

import json
import math
import os
import sys
from typing import Union

# ── Tunable economics (env-configurable; defaults match the pricing tool) ──────
CREDIT_USD_VALUE = float(os.getenv("CREDIT_USD_VALUE", "0.01"))   # 1 credit = $0.01
CREDIT_MARGIN    = float(os.getenv("CREDIT_MARGIN",    "1.0"))    # break-even basis

# ── Hardcoded defaults ────────────────────────────────────────────────────────
# These are used verbatim when config/pricing.json is absent or a key is missing,
# so behaviour is unchanged without the file. The live values below are merged
# loaded-over-default, per key. NOTE: the pricing tool's top tier is "Studio"; the
# DB `plan` CHECK constraint calls it "enterprise" (display name "Studio").
_DEFAULT_TIER_MONTHLY_CREDITS: dict[str, int] = {
    "free":       100,
    "starter":    2500,
    "pro":        9000,
    "enterprise": 31200,   # "Studio"
}
# Longest-prefix match wins (e.g. "veo-3.1-fast" before "veo"). Rates mirror the
# pricing tool: Veo Standard ≈ $0.50/s, Fast ≈ $0.15/s, Sora ≈ $0.50/s, budget
# ≈ $0.05/s. Verify against the live provider before trusting any margin.
_DEFAULT_VIDEO_USD_PER_SEC: dict[str, float] = {
    "veo-3.1-fast":   0.15,
    "veo-3-fast":     0.15,
    "veo-fast":       0.15,
    "veo-3.1":        0.50,
    "veo-3":          0.50,
    "veo":            0.50,
    "sora-2":         0.50,
    "sora":           0.50,
    "kling":          0.05,
    "wan":            0.05,
    "runway":         0.20,
}
_DEFAULT_VIDEO_USD_PER_SEC_DEFAULT = 0.50
_DEFAULT_VIDEO_DEFAULT_SECONDS = 8     # a Veo/Sora clip is ~8s when length unknown
_DEFAULT_TTS_USD_PER_1K_CHARS = 0.10


# ── Pricing config loader ─────────────────────────────────────────────────────
# config/pricing.json is the single source of truth shared with backend/billing.mjs.
# Precedence: PRICING_CONFIG_JSON env (inline JSON) → file at PRICING_CONFIG_PATH,
# else a candidate path → {}. Any failure logs and returns {} so pricing always
# falls back to the hardcoded defaults above (never crashes on bad config).
def _load_pricing() -> dict:
    raw = os.getenv("PRICING_CONFIG_JSON")
    if raw:
        try:
            return json.loads(raw) or {}
        except Exception as e:                       # malformed inline JSON → defaults
            print(f"[credit_catalog] PRICING_CONFIG_JSON ignored ({e}); using defaults", file=sys.stderr)
            return {}
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.getenv("PRICING_CONFIG_PATH"),
        os.path.join(here, "..", "config", "pricing.json"),
        os.path.join(here, "config", "pricing.json"),
        "/app/config/pricing.json",
        os.path.join(os.getcwd(), "config", "pricing.json"),
    ]
    for p in candidates:
        try:
            if p and os.path.isfile(p):
                with open(p, "r", encoding="utf-8") as fh:
                    return json.load(fh) or {}
        except Exception as e:
            print(f"[credit_catalog] pricing.json at {p} ignored ({e}); using defaults", file=sys.stderr)
            return {}
    return {}


_PRICING = _load_pricing()

# ── Monthly credit allowance per plan (config-driven, per-key fallback) ────────
TIER_MONTHLY_CREDITS: dict[str, int] = {
    **_DEFAULT_TIER_MONTHLY_CREDITS,
    **(_PRICING.get("tier_monthly_credits") or {}),
}

# ── Video upstream cost, USD per second of generated video (config-driven) ─────
_VIDEO_USD_PER_SEC: dict[str, float] = {
    **_DEFAULT_VIDEO_USD_PER_SEC,
    **(_PRICING.get("video_usd_per_sec") or {}),
}
_VIDEO_USD_PER_SEC_DEFAULT = float(
    _PRICING.get("video_usd_per_sec_default", _DEFAULT_VIDEO_USD_PER_SEC_DEFAULT))
_VIDEO_DEFAULT_SECONDS = int(
    _PRICING.get("video_default_seconds", _DEFAULT_VIDEO_DEFAULT_SECONDS))

# ── TTS upstream cost, USD per 1k characters (env wins; JSON supplies default) ─
_TTS_USD_PER_1K_CHARS = float(
    os.getenv("TTS_USD_PER_1K_CHARS",
              str(_PRICING.get("tts_usd_per_1k_chars", _DEFAULT_TTS_USD_PER_1K_CHARS))))

# ── Chat estimate heuristics (pre-call holds, real tokens unknown yet) ─────────
_CHARS_PER_TOKEN = 4                # rough English/Indonesian average
_CHAT_DEFAULT_OUT_TOKENS = 800      # assume a medium answer when nothing better


# ══════════════════════════════════════════════════════════════════════════════
# Core conversion
# ══════════════════════════════════════════════════════════════════════════════
def usd_to_credits(usd: float) -> int:
    """USD upstream cost → whole credits, applying margin. Always ≥ 1 for any
    positive cost (you never charge 0 for real work), exactly 0 for free ops."""
    if not usd or usd <= 0:
        return 0
    raw = usd * CREDIT_MARGIN / CREDIT_USD_VALUE
    return max(1, math.ceil(raw))


def _video_usd_per_sec(model: str) -> float:
    m = (model or "").lower()
    key = max((k for k in _VIDEO_USD_PER_SEC if m.startswith(k)), key=len, default=None)
    return _VIDEO_USD_PER_SEC[key] if key else _VIDEO_USD_PER_SEC_DEFAULT


def operation_usd(operation: str, model: str, units: Union[int, float, dict]) -> float:
    """Upstream USD for one operation. `units` meaning depends on operation:
        chat / narasi : int total tokens, OR {'tokens_in':int,'tokens_out':int}
        image         : int image count
        video         : int/float seconds, OR {'seconds':float}
        tts           : int character count, OR {'chars':int}
    """
    op = (operation or "").lower()

    if op in ("chat", "narasi", "embedding"):
        if isinstance(units, dict):
            tok_in  = int(units.get("tokens_in", 0) or 0)
            tok_out = int(units.get("tokens_out", 0) or 0)
        else:
            # a bare token count is treated as output tokens (worst case)
            tok_in, tok_out = 0, int(units or 0)
        from laozhang_api import _calc_cost          # lazy: avoid import cycle
        return _calc_cost(model, tok_in, tok_out)

    if op == "image":
        count = int(units.get("count", 1)) if isinstance(units, dict) else int(units or 1)
        from laozhang_api import _calc_image_cost     # lazy: avoid import cycle
        return _calc_image_cost(model, count)

    if op == "video":
        secs = float(units.get("seconds", _VIDEO_DEFAULT_SECONDS)) if isinstance(units, dict) \
            else float(units or _VIDEO_DEFAULT_SECONDS)
        return round(_video_usd_per_sec(model) * max(0.0, secs), 6)

    if op == "tts":
        chars = int(units.get("chars", 0)) if isinstance(units, dict) else int(units or 0)
        return round(_TTS_USD_PER_1K_CHARS * max(0, chars) / 1000.0, 6)

    # unknown op → treat as token-priced 'other'
    tok = int(units or 0) if not isinstance(units, dict) else int(units.get("tokens_out", 0) or 0)
    from laozhang_api import _calc_cost
    return _calc_cost(model, 0, tok)


def credit_cost(operation: str, model: str, units: Union[int, float, dict]) -> int:
    """Credits to charge for `operation` on `model` consuming `units`.
    This is the function the metering middleware calls. See operation_usd for the
    meaning of `units` per operation."""
    return usd_to_credits(operation_usd(operation, model, units))


# ── Pre-call estimate helpers (for the HOLD before real units are known) ───────
def estimate_chat_credits(model: str, prompt_chars: int = 0, max_tokens: int = 0) -> int:
    """Conservative upfront hold for a chat/narasi turn. Output tokens are unknown
    pre-call, so assume the answer fills max_tokens (capped), refund the unused
    portion at commit time."""
    tok_in  = max(0, int(prompt_chars)) // _CHARS_PER_TOKEN
    tok_out = int(max_tokens) if max_tokens else _CHAT_DEFAULT_OUT_TOKENS
    tok_out = min(tok_out, 16000)        # don't hold absurd amounts
    return credit_cost(model=model, operation="chat",
                       units={"tokens_in": tok_in, "tokens_out": tok_out})


def estimate_tts_credits(model: str, text: str) -> int:
    return credit_cost(operation="tts", model=model, units={"chars": len(text or "")})


def estimate_video_credits(model: str, seconds: float = _VIDEO_DEFAULT_SECONDS) -> int:
    return credit_cost(operation="video", model=model, units={"seconds": seconds})


def estimate_image_credits(model: str, count: int = 1) -> int:
    return credit_cost(operation="image", model=model, units={"count": count})


# ══════════════════════════════════════════════════════════════════════════════
# Self-test / sample table — `python credit_catalog.py`
# ══════════════════════════════════════════════════════════════════════════════
if __name__ == "__main__":
    print(f"CREDIT_USD_VALUE=${CREDIT_USD_VALUE}  CREDIT_MARGIN={CREDIT_MARGIN}\n")
    samples = [
        ("chat",  "gpt-4o-mini",      {"tokens_in": 1000, "tokens_out": 1000}, "1k in + 1k out"),
        ("chat",  "claude-sonnet",    {"tokens_in": 1000, "tokens_out": 1000}, "1k in + 1k out"),
        ("chat",  "gemini-2.5-flash", {"tokens_in": 1000, "tokens_out": 1000}, "1k in + 1k out"),
        ("narasi","deepseek-chat",    {"tokens_in": 2000, "tokens_out": 4000}, "1 chapter ~6k tok"),
        ("image", "imagen-4.0",       1,                                       "1 image"),
        ("image", "flux-kontext-max", 4,                                       "4 images"),
        ("video", "veo-3.1",          8,                                       "8s Veo Standard"),
        ("video", "veo-3.1-fast",     8,                                       "8s Veo Fast"),
        ("video", "sora-2",           8,                                       "8s Sora"),
        ("tts",   "tts-1",            5000,                                    "5,000 chars"),
    ]
    # operation_usd for chat/image needs laozhang_api's maps; tolerate absence.
    print(f"{'op':7} {'model':18} {'units':22} {'USD':>9} {'credits':>8}")
    print("-" * 70)
    for op, model, units, note in samples:
        try:
            usd = operation_usd(op, model, units)
            cr  = usd_to_credits(usd)
            print(f"{op:7} {model:18} {note:22} {usd:9.5f} {cr:8d}")
        except Exception as e:
            print(f"{op:7} {model:18} {note:22}  (needs laozhang_api: {e})")

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
import re
import sys
from typing import Union

# ── Tunable economics (env-configurable; defaults match the pricing tool) ──────
CREDIT_USD_VALUE = float(os.getenv("CREDIT_USD_VALUE", "0.01"))   # 1 credit = $0.01
CREDIT_MARGIN    = float(os.getenv("CREDIT_MARGIN",    "1.0"))    # extra global multiplier

# ── Harga-jual (markup) tiers — ceritaAI official pricing, June 2026 ───────────
# Decision (Rino, 2026-06-18): image + video are charged at a MARKUP over upstream
# cost (not break-even), matching ceritaAI_official_pricing.xlsx so the credits a
# package buys = the credits actually billed. credits = ceil(usd * markup / $0.01).
#   image  : ×5 when ≤ $0.10/img, else ×3      (e.g. Nano Banana $0.039 → 20 cr)
#   video  : ×4 when ≤ $0.12/sec, else ×2.5    (Veo 8 s flat via _VIDEO_DEFAULT_SECONDS)
#   golpo  : ×3
# chat / tts / narasi / embedding stay ×1 (margin comes from tier pricing + utilisation).
IMG_MARKUP_LO       = float(os.getenv("IMG_MARKUP_LO",        "3"))     # ≤$0.10  (Rino 2026-06-18: 5→3)
IMG_MARKUP_HI       = float(os.getenv("IMG_MARKUP_HI",        "2.5"))   # >$0.10  (3→2.5)
IMG_MARKUP_USD_GATE = float(os.getenv("IMG_MARKUP_USD_GATE",  "0.10"))   # per-image threshold
VID_MARKUP_LO       = float(os.getenv("VID_MARKUP_LO",        "2.5"))   # ≤$0.12/s (4→2.5; critique: don't cut Veo to 2)
VID_MARKUP_HI       = float(os.getenv("VID_MARKUP_HI",        "2.5"))   # >$0.12/s (kept 2.5)
VID_MARKUP_USD_GATE = float(os.getenv("VID_MARKUP_USD_GATE",  "0.12"))   # per-second threshold
GOLPO_MARKUP        = float(os.getenv("GOLPO_MARKUP",         "3"))

# ── Financial reporting / GL tagging (lightweight — tags on usage_logs, NO journal) ──
# Single source for IDR translation + the weighted-average credit sale price used to
# recognize revenue at consumption. KURS is the ONLY FX constant in the backend; translate
# USD COGS → IDR here (and at month-end close), never snapshot a per-call rate.
KURS_IDR_USD          = float(os.getenv("KURS_IDR_USD",          "18000"))  # Rp/USD
CREDIT_SALE_PRICE_IDR = float(os.getenv("CREDIT_SALE_PRICE_IDR", "248"))    # blended fallback Rp/credit

# Package list price (Rp) per plan → recognise revenue at the ACTUAL price/credit the
# customer paid, so recognized revenue reconciles to cash (fixes flat-248 over-recognition).
_DEFAULT_TIER_PRICE_IDR = {"starter": 79000, "pro": 199000, "enterprise": 499000}
TIER_PRICE_IDR = dict(_DEFAULT_TIER_PRICE_IDR)   # pricing.json override merged after _PRICING (below)

def credit_sale_price_idr(plan: str) -> float:
    """Rp recognised per consumed credit = the plan's actual package price-per-credit
    (price / allowance), so recognized revenue == cash collected. Falls back to the
    blended CREDIT_SALE_PRICE_IDR for plans with no list price (e.g. a lapsed user
    spending leftover paid credits, or free)."""
    p = (plan or "").lower()
    price = TIER_PRICE_IDR.get(p); cr = TIER_MONTHLY_CREDITS.get(p)
    return round(price / cr, 4) if price and cr else CREDIT_SALE_PRICE_IDR

# ── Free leaky-bucket + carryover caps (Rino FINAL: ONE cap = 150 everywhere) ───
FREE_DAILY_GRANT   = int(os.getenv("FREE_DAILY_GRANT",   "15"))    # credits claimable per WIB day
FREE_CEILING       = int(os.getenv("FREE_CEILING",       "150"))   # max free balance via daily claim
PAID_CARRYOVER_CAP = int(os.getenv("PAID_CARRYOVER_CAP", "150"))   # leftover that survives a grant / lapse
FREE_TZ            = os.getenv("FREE_TZ", "Asia/Jakarta")          # WIB day boundary for the daily claim
# Global anti-farming kill-switch: total upstream USD across ALL free ops per WIB day.
# 0 / blank disables it. A spike of throwaway free accounts caps total loss here.
FREE_GLOBAL_DAILY_USD_CAP = float(os.getenv("FREE_GLOBAL_DAILY_USD_CAP", "10"))

# ── Per-plan concurrency caps (parallel jobs per tenant) — Phase 3 ──────────────
# Enforced at submit/enqueue (429 on exceed), NOT BullMQ worker threads. Shared with
# the Node video path via config/pricing.json 'concurrency_caps'.
_DEFAULT_CONCURRENCY_CAPS = {"free": 1, "starter": 2, "pro": 4, "enterprise": 8}
CONCURRENCY_CAPS = dict(_DEFAULT_CONCURRENCY_CAPS)   # pricing.json override merged after _PRICING loads (below)

# endpoint (base, before any '-VI' suffix) → GL account code. Default 4500/5500 (other).
_GL_REVENUE_CODE = {"image": "4100", "video": "4200", "chat": "4300", "tts": "4400"}
_GL_COGS_CODE    = {"image": "5100", "video": "5200", "chat": "5300", "tts": "5400"}

def gl_codes(endpoint: str) -> tuple:
    """(revenue_code, cogs_code) for an endpoint. Strips the Video-Instant '-VI' suffix
    so 'image' and 'image-VI' both map to the Image accounts."""
    base = (endpoint or "other").split("-")[0]
    return _GL_REVENUE_CODE.get(base, "4500"), _GL_COGS_CODE.get(base, "5500")


# ── Model-lock (loss-leader tier gating) ────────────────────────────────────────
# Per-model minimum plan. Free = cheap image only; Starter = + cheap video + GPT-Image-2;
# Pro = everything except ultra-premium; Studio(enterprise) = everything incl 4K + Sora Pro.
# 4K is RESOLUTION-locked to Studio (see video_min_tier). Override via pricing.json
# 'model_min_tier':{'image':{...},'video':{...}} so re-tiering ≠ code change.
TIER_RANK = {"free": 0, "starter": 1, "pro": 2, "enterprise": 3}

def tier_at_least(have_tier: str, min_tier: str) -> bool:
    """True if `have_tier` ranks >= `min_tier`. Unknown/None → rank 0 (free), fail-closed."""
    return TIER_RANK.get((have_tier or "free"), 0) >= TIER_RANK.get((min_tier or "free"), 0)

_DEFAULT_IMAGE_MIN_TIER = {
    "nano-banana": "free", "nano-banana-hd": "free",
    "seedream-4-0": "free", "seedream-4-5": "free", "gpt-image-1": "free",
    "nano-banana-2": "starter", "nano-banana-2-hd": "starter",
    "gpt-image-2": "starter", "gpt-image-2-vip": "starter", "gpt-image-2-official": "starter",
    "flux-kontext-pro": "pro", "flux-kontext-max": "pro", "sora-image": "pro",
    "nano-banana-pro": "enterprise", "nano-banana-pro-hd": "enterprise",
    # Vertex (google route) ids — nano-banana family via /generate-image/vertex.
    "gemini-2.5-flash-image": "free", "gemini-3.1-flash-image": "starter",
    "gemini-3-pro-image": "enterprise",
}
_DEFAULT_VIDEO_MIN_TIER = {   # longest-prefix match, like _VIDEO_USD_PER_SEC
    "veo-3.1-fast": "starter", "veo-3-fast": "starter", "veo-fast": "starter",
    "veo-3.1-lite": "starter", "veo-lite": "starter", "kling": "starter", "wan": "starter",
    "veo-3.1": "pro", "veo-3": "pro", "veo": "pro",
    "sora-2-character": "pro", "sora-character": "pro",
    "sora-2-pro": "enterprise", "runway-aleph": "enterprise",
}
# Built from code defaults here; pricing.json overrides merged after _PRICING loads (below).
IMAGE_MODEL_MIN_TIER = dict(_DEFAULT_IMAGE_MIN_TIER)
VIDEO_MODEL_MIN_TIER = dict(_DEFAULT_VIDEO_MIN_TIER)

def image_min_tier(model: str) -> str:
    return IMAGE_MODEL_MIN_TIER.get(model, "pro")   # unknown model → locked, not free

def video_min_tier(model: str, size: str = "") -> str:
    if size and ("2160" in str(size) or "3840" in str(size)):
        return "enterprise"        # 4K is resolution-locked to Studio, any base model
    m = (model or "").lower()
    key = max((k for k in VIDEO_MODEL_MIN_TIER if m.startswith(k)), key=len, default=None)
    return VIDEO_MODEL_MIN_TIER[key] if key else "pro"   # unknown → locked

# ── Hardcoded defaults ────────────────────────────────────────────────────────
# These are used verbatim when config/pricing.json is absent or a key is missing,
# so behaviour is unchanged without the file. The live values below are merged
# loaded-over-default, per key. NOTE: the pricing tool's top tier is "Studio"; the
# DB `plan` CHECK constraint calls it "enterprise" (display name "Studio").
_DEFAULT_TIER_MONTHLY_CREDITS: dict[str, int] = {
    "free":       0,       # leaky-bucket: free has NO monthly pool, accrues +FREE_DAILY_GRANT/day
    "starter":    320,
    "pro":        850,     # anchor regrade (was 800) → cost/credit descends
    "enterprise": 2500,    # "Studio" (was 2000) — "Best Value", lowest cost/credit anchor
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
    # Whiteboard render (Opt B): flat SELL price per second of output video (render
    # compute ≈ $0). Markup-exempt in _op_markup so credits == 3/sec exactly.
    "whiteboard":     0.03,
}
_DEFAULT_VIDEO_USD_PER_SEC_DEFAULT = 0.50
_DEFAULT_VIDEO_DEFAULT_SECONDS = 8     # a Veo/Sora clip is ~8s when length unknown
_DEFAULT_TTS_USD_PER_1K_CHARS = 0.10

# ── Resolution-aware video COGS (OPT-IN, additive) ─────────────────────────────
# Per-second upstream USD that varies by OUTPUT RESOLUTION, for models whose cost
# scales with resolution (Veo Standard / Fast / Lite). The flat _VIDEO_USD_PER_SEC
# above stays the fallback; a model becomes resolution-aware ONLY when it appears
# here or in pricing.json `video_usd_per_sec_by_res`. DEFAULT IS EMPTY {} → no model
# is resolution-aware unless the deployment opts in, so Indonesia (flat) is
# byte-for-byte unchanged. The GLOBAL config supplies the real per-resolution rates.
#   shape: { "<model-prefix>": { "720p": usd, "1080p": usd, "2160p": usd, "_default": usd } }
# Lookup: longest model-prefix match → normalised resolution bucket → "_default" →
# (no by-res hit) flat map. `_default` lets a deployment price a model even when the
# request omits a resolution (we fall to its 1080p rate). NOTE: Sora is resolution-
# invariant (stays on the flat map). Kling is per-CLIP not per-second, and Wan COGS
# is TBD — both intentionally absent here; see the global config notes.
_DEFAULT_VIDEO_USD_PER_SEC_BY_RES: dict[str, dict[str, float]] = {}


def _norm_video_res(size) -> str:
    """Normalise a size/resolution hint to a canonical bucket — '720p' | '1080p' |
    '1440p' | '2160p' — or '' when unknown. Accepts '1920x1080', '720x1280',
    '1080p', '1080', 1080, '4k', '2160', '3840x2160', 'uhd', etc. Order matters:
    the higher-res / compound tokens (uhd, fhd) are tested before the bare 'hd'."""
    s = str(size or "").strip().lower()
    if not s:
        return ""
    if "2160" in s or "3840" in s or "4k" in s or "uhd" in s:
        return "2160p"
    if "1440" in s or "2560" in s or "qhd" in s or "2k" in s:
        return "1440p"
    if "1080" in s or "1920" in s or "fhd" in s:
        return "1080p"
    if "720" in s or "1280" in s:
        return "720p"
    # bare numeric (e.g. "1080" already caught above; this catches odd "x"-forms):
    nums = [int(n) for n in re.findall(r"\d+", s)]
    if nums:
        h = min(nums) if len(nums) >= 2 else nums[0]   # WxH → the smaller is the height
        if h >= 2000: return "2160p"
        if h >= 1300: return "1440p"
        if h >= 1000: return "1080p"
        if h >= 600:  return "720p"
    return ""


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

# Model-lock: merge pricing.json 'model_min_tier' over the code defaults now that
# _PRICING is loaded, so re-tiering a model is a config edit, not a code change.
_mmt = (_PRICING.get("model_min_tier") or {})
IMAGE_MODEL_MIN_TIER.update(_mmt.get("image") or {})
VIDEO_MODEL_MIN_TIER.update(_mmt.get("video") or {})
CONCURRENCY_CAPS.update(_PRICING.get("concurrency_caps") or {})
TIER_PRICE_IDR.update(_PRICING.get("tier_price_idr") or {})
# Config-driven tier ladder. Default = the Indonesia 4-tier ranks; the GLOBAL
# subscription deployment overrides via pricing.json `tier_rank` to express its
# 5-tier ladder, e.g. {"free":0,"starter":1,"plus":2,"pro":3,"ultra":4}. Without an
# override the existing free/starter/pro/enterprise behaviour is unchanged. Pair it
# with model_min_tier entries that use the same tier names (Rino's product call).
TIER_RANK.update(_PRICING.get("tier_rank") or {})

# ── Per-deployment metering economics (config-driven; ENV still WINS) ──────────
# Dual-billing: the GLOBAL subscription deployment sells credits at $0.002 (vs
# Indonesia $0.01) and charges Veo/Sora at a 1.5× markup (50% margin) instead of
# 2.5×. These differ PER DEPLOYMENT by config (the global pricing.json sets them),
# mirroring how BILLING_MODE differs per deployment — no code branch. An explicit
# env var still overrides the config. Indonesia (no override) keeps $0.01 / 2.5×.
if not os.getenv("CREDIT_USD_VALUE") and _PRICING.get("credit_usd_value") is not None:
    CREDIT_USD_VALUE = float(_PRICING["credit_usd_value"])
if not os.getenv("VID_MARKUP_LO") and _PRICING.get("vid_markup_lo") is not None:
    VID_MARKUP_LO = float(_PRICING["vid_markup_lo"])
if not os.getenv("VID_MARKUP_HI") and _PRICING.get("vid_markup_hi") is not None:
    VID_MARKUP_HI = float(_PRICING["vid_markup_hi"])

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
# Resolution-aware override map (opt-in; empty default → flat behaviour). The GLOBAL
# deployment supplies per-resolution Veo rates here via pricing.json.
_VIDEO_USD_PER_SEC_BY_RES: dict[str, dict[str, float]] = {
    **_DEFAULT_VIDEO_USD_PER_SEC_BY_RES,
    **(_PRICING.get("video_usd_per_sec_by_res") or {}),
}

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
    # round to 6 dp BEFORE ceil: float dust (e.g. 0.40*1.5 = 0.6000000000000001)
    # otherwise pushes an exact boundary like 300.0 to 300.0000…6 → ceil 301. The
    # rounding only collapses sub-1e-6 noise; any real fractional credit is preserved.
    raw = round(usd * CREDIT_MARGIN / CREDIT_USD_VALUE, 6)
    return max(1, math.ceil(raw))


def _video_usd_per_sec(model: str, size: str = "") -> float:
    m = (model or "").lower()
    # Resolution-aware override (opt-in): longest model-prefix match into BY_RES.
    # On a hit, prefer the request's resolution bucket, else the model's "_default"
    # rate; only if neither is present do we fall through to the flat map.
    rkey = max((k for k in _VIDEO_USD_PER_SEC_BY_RES if m.startswith(k)), key=len, default=None)
    if rkey:
        by = _VIDEO_USD_PER_SEC_BY_RES[rkey]
        bucket = _norm_video_res(size)
        if bucket and bucket in by:
            return float(by[bucket])
        if "_default" in by:
            return float(by["_default"])
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
        if isinstance(units, dict):
            secs = float(units.get("seconds", _VIDEO_DEFAULT_SECONDS))
            size = units.get("size") or units.get("resolution") or ""
        else:
            secs = float(units or _VIDEO_DEFAULT_SECONDS)
            size = ""
        return round(_video_usd_per_sec(model, size) * max(0.0, secs), 6)

    if op == "tts":
        chars = int(units.get("chars", 0)) if isinstance(units, dict) else int(units or 0)
        return round(_TTS_USD_PER_1K_CHARS * max(0, chars) / 1000.0, 6)

    # unknown op → treat as token-priced 'other'
    tok = int(units or 0) if not isinstance(units, dict) else int(units.get("tokens_out", 0) or 0)
    from laozhang_api import _calc_cost
    return _calc_cost(model, 0, tok)


def _op_markup(operation: str, model: str, units: Union[int, float, dict], usd: float) -> float:
    """Markup multiplier applied to upstream USD before converting to credits.
    image/video tier on per-IMAGE / per-SECOND cost; golpo flat; everything else ×1."""
    op = (operation or "").lower()
    if op == "image":
        count = int(units.get("count", 1)) if isinstance(units, dict) else int(units or 1)
        per = (usd / count) if count else usd
        return IMG_MARKUP_LO if per <= IMG_MARKUP_USD_GATE else IMG_MARKUP_HI
    if op == "video":
        # Whiteboard render fee is a FLAT sell price (the per-second rate IS the price,
        # not a COGS to mark up) → no markup so credits == 3/sec exactly.
        if (model or "").lower().startswith("whiteboard"):
            return 1.0
        size = (units.get("size") or units.get("resolution") or "") if isinstance(units, dict) else ""
        return VID_MARKUP_LO if _video_usd_per_sec(model, size) <= VID_MARKUP_USD_GATE else VID_MARKUP_HI
    if op == "golpo":
        return GOLPO_MARKUP
    return 1.0   # chat / tts / narasi / embedding → break-even


def credit_cost(operation: str, model: str, units: Union[int, float, dict]) -> int:
    """Credits to charge for `operation` on `model` consuming `units`.
    This is the function the metering middleware calls. See operation_usd for the
    meaning of `units` per operation. Image/video carry a harga-jual markup."""
    usd = operation_usd(operation, model, units)
    return usd_to_credits(usd * _op_markup(operation, model, units, usd))


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
